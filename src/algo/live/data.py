"""MarketData over live Fyers data (paper trading).

Bars:
  * history (REST, 1-min) for the warm-up days and today up to the moment the feed started
  * live bars built from the websocket feed (BarBuilder) from then on
  * any closed minute the feed missed (outage, reconnect) is back-filled from REST once
Spot bars borrow the front-month future's volume, like the backtest. Futures bars from the feed
also carry order-flow columns (buy_vol, sell_vol, delta); history minutes have NaN there.

`advance(ts)` must be called once per closed minute, before the trading session processes it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from algo.backtest.data import BarFrames, regular_session
from algo.config import Underlying
from algo.core.broker import MarketData
from algo.core.models import OptionContract, OptionType
from algo.fyers.symbols import FnoUniverse
from algo.indicators import resample
from algo.live.orderflow import BarBuilder, Quote

log = logging.getLogger(__name__)
SESSION_OPEN = time(9, 15)
FLOW_COLUMNS = ["volume", "buy_vol", "sell_vol", "delta"]


def _minute(t: datetime) -> datetime:
    return pd.Timestamp(t).floor("min").to_pydatetime()


class LiveMarketData(BarFrames, MarketData):
    def __init__(self, client, universe: FnoUniverse, underlying: Underlying, builder: BarBuilder,
                 feed=None, warmup_days: int = 10, roll_days_before: int = 1, quote_max_age_s: int = 90) -> None:
        """client: FyersClient-like (history, quotes); feed: FyersFeed-like (subscribe) or None."""
        if not underlying.fyers_symbol:
            raise RuntimeError(f"No fyers_symbol configured for {underlying.name} in settings.yaml")
        self.client, self.uni, self.ul, self.builder, self.feed = client, universe, underlying, builder, feed
        self.warmup_days, self.roll = warmup_days, roll_days_before
        self.quote_max_age = timedelta(seconds=quote_max_age_s)
        self.spot_sym = underlying.fyers_symbol
        self.fut_sym: str | None = None
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"],
                             index=pd.DatetimeIndex([], name="ts"), dtype=float)
        self.hist = {"spot": empty, "fut": empty}
        self.spot_1m, self.fut_1m = empty, empty
        self._tf: dict = {}
        self.live_from: datetime | None = None     # first minute fully covered by the feed
        self.now: datetime | None = None           # bar start of the last advanced minute
        self._backfilled: set = set()
        self._rest_quotes: dict[tuple[str, datetime], dict] = {}
        self.options_enabled = True

    # ---- lifecycle ----------------------------------------------------------
    def warm_up(self, now: datetime) -> None:
        """Load REST history: warm-up days + today up to the last closed minute."""
        today = now.date()
        self.fut_sym = self.uni.front_future(today, self.roll)
        if self.fut_sym is None:
            log.warning("no %s future found in the symbol master: spot volume and order flow unavailable",
                        self.ul.name)
        start = datetime.combine(today - timedelta(days=self.warmup_days), SESSION_OPEN)
        last_closed = _minute(now) - timedelta(minutes=1)
        self.hist["spot"] = regular_session(self.client.history(self.spot_sym, start, last_closed))
        if self.fut_sym:
            self.hist["fut"] = regular_session(self.client.history(self.fut_sym, start, last_closed, oi=True))
        log.info("history: %d spot bars, %d futures bars (%s)", len(self.hist["spot"]), len(self.hist["fut"]),
                 self.fut_sym or "-")

    def mark_live(self, subscribed_at: datetime) -> None:
        """The feed is subscribed: minutes from the next full minute on come from it."""
        self.live_from = _minute(subscribed_at) + timedelta(minutes=1)

    def subscriptions(self, spot: float, on: date, strikes_around: int, n_expiries: int = 1) -> list[str]:
        syms = [self.spot_sym] + ([self.fut_sym] if self.fut_sym else [])
        return syms + self.uni.near_atm(spot, on, strikes_around, n_expiries)

    def last_spot(self) -> float | None:
        q = self.builder.quote(self.spot_sym)
        if q:
            return q.ltp
        s = self.hist["spot"]
        return float(s["close"].iloc[-1]) if len(s) else None

    def advance(self, ts: datetime) -> None:
        """Make the 1-min bar starting at ts (now closed) visible to the strategy."""
        ts = _minute(ts)
        self.now = ts
        self.spot_1m = self._compose("spot", self.spot_sym, ts)
        self.fut_1m = self._compose("fut", self.fut_sym, ts) if self.fut_sym else self.hist["fut"]
        if len(self.fut_1m) and len(self.spot_1m):
            sp = self.spot_1m.copy()
            sp["volume"] = self.fut_1m["volume"].reindex(sp.index).fillna(0.0)
            sp["oi"] = (self.fut_1m["oi"].reindex(sp.index).groupby(sp.index.normalize()).ffill().fillna(0.0)
                        if "oi" in self.fut_1m else 0.0)
            self.spot_1m = sp
        self._tf = {}
        if ts.minute == 0:
            self.builder.prune(ts - timedelta(hours=2))
            self._rest_quotes = {k: v for k, v in self._rest_quotes.items() if k[1] >= ts - timedelta(minutes=5)}

    def _compose(self, key: str, symbol: str, ts: datetime) -> pd.DataFrame:
        hist = self.hist[key]
        live = pd.DataFrame()
        if self.live_from is not None and ts >= self.live_from:
            live = regular_session(self.builder.bars(symbol, start=self.live_from, until=ts))
        self._backfill(key, symbol, ts, live)
        hist = self.hist[key]
        hist = hist[hist.index <= pd.Timestamp(ts)]
        if live.empty:
            return hist
        if "oi" not in live:
            live = live.assign(oi=np.nan)
        out = pd.concat([hist[~hist.index.isin(live.index)], live]).sort_index()
        if "oi" in out:
            out["oi"] = out["oi"].groupby(out.index.normalize()).ffill()
        return out

    def _backfill(self, key: str, symbol: str, ts: datetime, live: pd.DataFrame) -> None:
        """Fetch closed minutes of today that neither history nor the feed has (once per minute)."""
        day_open = datetime.combine(ts.date(), SESSION_OPEN)
        have = set(self.hist[key].index[self.hist[key].index >= pd.Timestamp(day_open)]) | set(live.index)
        want = pd.date_range(day_open, ts, freq="1min")
        missing = [t for t in want if t not in have and (symbol, t) not in self._backfilled]
        if not missing:
            return
        try:
            got = regular_session(self.client.history(symbol, missing[0].to_pydatetime(), ts, oi=key == "fut"))
        except Exception as e:   # noqa: BLE001 - a failed back-fill must not stop the session
            log.warning("back-fill of %s %s..%s failed: %s", symbol, missing[0], ts, e)
            got = None
        # REST may not have the just-closed minute yet: retry the last 2 minutes next time
        retry_after = pd.Timestamp(ts) - pd.Timedelta(minutes=2)
        for t in missing:
            if t < retry_after or (got is not None and t in got.index):
                self._backfilled.add((symbol, t))
        if got is not None and len(got):
            h = self.hist[key]
            self.hist[key] = pd.concat([h[~h.index.isin(got.index)], got]).sort_index()
            log.info("back-filled %d %s bars from REST", len(got), symbol)

    # ---- order flow ---------------------------------------------------------
    def order_flow(self, until: datetime, tf: str = "1min", symbol: str | None = None) -> pd.DataFrame:
        """Feed-built buy / sell volume and delta of `symbol` (default: the front-month future)
        for completed bars up to the close of the 1-min bar starting at `until`."""
        sym = symbol or self.fut_sym
        if sym is None or self.live_from is None:
            return pd.DataFrame(columns=FLOW_COLUMNS, index=pd.DatetimeIndex([], name="ts"), dtype=float)
        df = regular_session(self.builder.bars(sym, start=self.live_from, until=_minute(until)))[FLOW_COLUMNS]
        r = resample(df, tf)
        return self._slice((r.drop(columns="bar_end"), r["bar_end"].to_numpy()), until, None)

    # ---- options ------------------------------------------------------------
    def expiries(self, on: date) -> list[date]:
        return self.uni.expiries(on)

    def strikes(self, expiry: date) -> list[float]:
        return self.uni.strikes(expiry)

    def contract(self, expiry: date, strike: float, option_type: OptionType) -> OptionContract | None:
        c = self.uni.contract(expiry, strike, option_type)
        if c is not None and self.feed is not None:
            self.feed.subscribe([c.instrument_key])   # no-op if already subscribed
        return c

    def quote(self, c: OptionContract, at: datetime | None = None) -> Quote | None:
        """Latest feed quote if fresh, else a REST quote (cached per minute)."""
        sym = c.instrument_key
        ref = (at or self.now or datetime.now()) + timedelta(minutes=1)
        q = self.builder.quote(sym)
        if q is not None and (q.ts is None or ref - q.ts <= self.quote_max_age):
            return q
        key = (sym, _minute(ref))
        if key not in self._rest_quotes:
            try:
                self._rest_quotes[key] = self.client.quotes([sym]).get(sym) or {}
            except Exception as e:   # noqa: BLE001
                log.warning("quote for %s failed: %s", sym, e)
                self._rest_quotes[key] = {}
        v = self._rest_quotes[key]
        if not v.get("lp"):
            return q
        return Quote(ltp=float(v["lp"]), bid=float(v.get("bid") or 0) or None,
                     ask=float(v.get("ask") or 0) or None, ts=ref)

    def option_ltp(self, c: OptionContract, at: datetime) -> float | None:
        q = self.quote(c, at)
        return None if q is None else q.ltp

    def option_bar(self, c: OptionContract, ts: datetime) -> tuple[float, float, float, float] | None:
        """Feed bar for the minute; if the option did not trade in it, a flat bar at its last price."""
        b = self.builder.bar(c.instrument_key, _minute(ts))
        if b is not None:
            return b
        q = self.quote(c, ts)
        return None if q is None else (q.ltp, q.ltp, q.ltp, q.ltp)
