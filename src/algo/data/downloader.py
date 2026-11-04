"""Download index spot + expired option candles from Upstox into the local store.

Resumable: files that already exist are skipped, so you can re-run after an
interruption or to extend the date range.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta

import pandas as pd

from algo.config import Underlying
from algo.data.store import DataStore, candles_to_df
from algo.upstox.client import UpstoxClient, UpstoxError

log = logging.getLogger(__name__)
INTRADAY_START = date(2022, 1, 1)   # Upstox V3 minute candles start here


def month_starts(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield date(y, m, 1)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def month_end(d: date) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def date_chunks(start: date, end: date, days: int):
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=days - 1), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


class Downloader:
    def __init__(self, client: UpstoxClient, store: DataStore, strikes_around: int = 10) -> None:
        self.client = client
        self.store = store
        self.strikes_around = strikes_around

    # ---- spot -------------------------------------------------------------
    def download_spot(self, ul: Underlying, start: date, end: date, force: bool = False) -> int:
        today = date.today()
        end = min(end, today)
        if start < INTRADAY_START:
            log.info("spot %s: 1-min data starts %s; clamping start (was %s)", ul.name, INTRADAY_START, start)
            start = INTRADAY_START
        rows = 0
        for ms in month_starts(start, end):
            me = month_end(ms)
            path = self.store.spot_path(ul.name, ms.year, ms.month)
            complete = me < today
            if path.exists() and complete and not force:
                log.info("spot %s %s: cached", ul.name, ms.strftime("%Y-%m"))
                continue
            try:
                candles = self.client.historical_candles(ul.instrument_key, "minutes", 1, min(me, today), ms)
            except UpstoxError as e:
                if "UDAPI100011" in e.codes:  # invalid instrument key: no point continuing
                    raise
                log.warning("spot %s %s: failed, skipping month (%s)", ul.name, ms.strftime("%Y-%m"), e)
                continue
            df = candles_to_df(candles)
            self.store.save_spot_month(ul.name, ms.year, ms.month, df)
            rows += len(df)
            log.info("spot %s %s: %d bars (%d days)", ul.name, ms.strftime("%Y-%m"), len(df),
                     df.index.normalize().nunique() if len(df) else 0)
        return rows

    # ---- options ----------------------------------------------------------
    def available_expiries(self, ul: Underlying) -> list[date]:
        return sorted(date.fromisoformat(e) for e in self.client.expired_expiries(ul.instrument_key))

    def download_options(self, ul: Underlying, start: date, end: date,
                         option_types: tuple[str, ...] = ("CE", "PE")) -> dict:
        all_exps = self.available_expiries(ul)
        if not all_exps:
            log.warning("%s: Upstox returned no expired expiries", ul.name)
            return {}
        log.info("%s: Upstox has expired expiries %s .. %s (%d)", ul.name, all_exps[0], all_exps[-1], len(all_exps))
        targets = [e for e in all_exps if start <= e <= end]
        if not targets:
            log.warning("%s: no expiries between %s and %s. Available: %s .. %s",
                        ul.name, start, end, all_exps[0], all_exps[-1])
        summary = {}
        for exp in targets:
            idx = all_exps.index(exp)
            prev = all_exps[idx - 1] if idx > 0 else None
            # Window where this contract is the nearest or next expiry.
            win_start = (prev - timedelta(days=7)) if prev else exp - timedelta(days=35)
            summary[exp] = self._download_expiry(ul, exp, win_start, option_types)
        return summary

    def _download_expiry(self, ul: Underlying, exp: date, win_start: date, option_types) -> dict:
        contracts = self.store.load_contracts(ul.name, exp)
        if contracts is None:
            raw = self.client.expired_option_contracts(ul.instrument_key, exp)
            if not raw:
                log.warning("%s %s: no contracts returned", ul.name, exp)
                return {"contracts": 0}
            contracts = self.store.save_contracts(ul.name, exp, raw)

        spot = self.store.load_spot(ul.name, win_start, exp)
        if spot.empty:
            self.download_spot(ul, win_start, exp)
            spot = self.store.load_spot(ul.name, win_start, exp)
        if spot.empty:
            log.warning("%s %s: no spot data for %s..%s; skipping", ul.name, exp, win_start, exp)
            return {"contracts": len(contracts), "downloaded": 0}

        step = ul.strike_step
        lo = math.floor(spot["low"].min() / step) * step - self.strikes_around * step
        hi = math.ceil(spot["high"].max() / step) * step + self.strikes_around * step
        sel = contracts[(contracts["strike_price"] >= lo) & (contracts["strike_price"] <= hi)
                        & (contracts["instrument_type"].isin(option_types))]
        empty = self.store.empty_set(ul.name, exp)
        done = skipped = 0
        log.info("%s %s: %d contracts in strike range %g-%g (window from %s)",
                 ul.name, exp, len(sel), lo, hi, win_start)
        for _, c in sel.iterrows():
            key, strike, typ = c["instrument_key"], float(c["strike_price"]), c["instrument_type"]
            if key in empty or self.store.option_path(ul.name, exp, strike, typ).exists():
                skipped += 1
                continue
            frames = []
            try:
                for a, b in date_chunks(win_start, exp, 28):
                    frames.append(candles_to_df(self.client.expired_candles(key, "1minute", b, a)))
            except UpstoxError as e:
                if "UDAPI1149" in e.codes:
                    raise
                log.warning("%s: %s", key, e)
                continue
            df = pd.concat(frames).sort_index() if frames else candles_to_df([])
            df = df[~df.index.duplicated(keep="last")]
            if df.empty:
                empty.add(key)
                self.store.mark_empty(ul.name, exp, empty)
            else:
                self.store.save_option(ul.name, exp, strike, typ, df)
            done += 1
        log.info("%s %s: downloaded %d, cached/skipped %d, empty %d", ul.name, exp, done, skipped, len(empty))
        return {"contracts": len(sel), "downloaded": done, "skipped": skipped, "empty": len(empty)}

    # ---- futures ----------------------------------------------------------
    def download_futures(self, ul: Underlying, start: date, end: date) -> int:
        """Expired monthly futures (for volume / OI / futures VWAP)."""
        all_exps = self.available_expiries(ul)
        by_month: dict[tuple, list[date]] = {}
        for e in all_exps:
            by_month.setdefault((e.year, e.month), []).append(e)
        fut_exps: list[tuple[date, dict]] = []
        for (y, m), exps in sorted(by_month.items()):
            if date(y, m, 1) > end or month_end(date(y, m, 1)) < start:
                continue
            # the monthly future expires on the month's last expiry; fall back to earlier ones
            for e in sorted(exps, reverse=True):
                cached = self.store.load_future(ul.name, e)
                if cached is not None and len(cached):     # an empty file = a failed download: retry
                    fut_exps.append((e, {}))
                    break
                contracts = self.client.expired_future_contracts(ul.instrument_key, e)
                if contracts:
                    fut_exps.append((e, contracts[0]))
                    break
            else:
                log.warning("%s: no futures contract found for %04d-%02d", ul.name, y, m)
        n = 0
        gap_from: date | None = None   # set when a contract came back empty: the next one covers its life
        for i, (e, meta) in enumerate(fut_exps):
            if not meta:
                log.info("futures %s %s: cached", ul.name, e)
                gap_from = None
                continue
            # front-month life starts after the previous month's expiry; add a week so the
            # roll-over days are covered too
            prev = fut_exps[i - 1][0] if i > 0 else e - timedelta(days=35)
            win_start = min(prev - timedelta(days=7), gap_from) if gap_from else prev - timedelta(days=7)
            gap_from = None
            frames = [candles_to_df(self.client.expired_candles(meta["instrument_key"], "1minute", b, a))
                      for a, b in date_chunks(win_start, e, 28)]
            df = pd.concat(frames).sort_index()
            df = df[~df.index.duplicated(keep="last")]
            if df.empty:   # never cache an empty result: it would hide the gap from later runs
                log.warning("futures %s %s (%s): Upstox returned no candles; not saved, will retry next run. "
                            "The next contract's window is extended to cover these days.",
                            ul.name, e, meta.get("instrument_key"))
                gap_from = win_start
                continue
            self.store.save_future(ul.name, e, df, meta)
            n += 1
            log.info("futures %s %s: %d bars (%s .. %s)", ul.name, e, len(df), win_start, e)
        last = fut_exps[-1][0] if fut_exps else None
        if last is None or end > last - timedelta(days=1):
            n += self.download_active_futures(ul, end, after=last)
        return n

    def download_active_futures(self, ul: Underlying, end: date, after: date | None = None) -> int:
        """The current (unexpired) front-month future, which the expired-instruments API can't serve.

        The contract comes from Upstox's instrument master; candles from the regular historical API.
        It is re-downloaded on every run because it is still trading.
        """
        from zoneinfo import ZoneInfo
        from datetime import datetime
        try:
            master = self.client.instrument_master(ul.exchange)
        except Exception as e:   # noqa: BLE001 - optional extra; never break the download
            log.warning("futures %s: instrument master unavailable (%s); active contract skipped", ul.name, e)
            return 0
        ist = ZoneInfo("Asia/Kolkata")
        futs = sorted(
            ((datetime.fromtimestamp(x["expiry"] / 1000, ist).date(), x) for x in master
             if x.get("instrument_type") == "FUT" and x.get("underlying_key") == ul.instrument_key),
            key=lambda t: t[0])
        today = date.today()
        active = next(((e, x) for e, x in futs if e >= today and (after is None or e > after)), None)
        if active is None:
            log.warning("futures %s: no active contract in the instrument master", ul.name)
            return 0
        exp, x = active
        win_start = (after or today - timedelta(days=35)) - timedelta(days=7)
        to = min(end, today)
        frames = [candles_to_df(self.client.historical_candles(x["instrument_key"], "minutes", 1, b, a))
                  for a, b in date_chunks(win_start, to, 28)]
        df = pd.concat(frames).sort_index() if frames else pd.DataFrame()
        df = df[~df.index.duplicated(keep="last")] if len(df) else df
        if df.empty:
            log.warning("futures %s %s (active, %s): no candles returned", ul.name, exp, x["instrument_key"])
            return 0
        self.store.save_future(ul.name, exp, df, {"instrument_key": x["instrument_key"], "expiry": exp.isoformat(),
                                                  "trading_symbol": x.get("trading_symbol"),
                                                  "lot_size": x.get("lot_size")})
        log.info("futures %s %s (active): %d bars (%s .. %s)", ul.name, exp, len(df), win_start, to)
        return 1

