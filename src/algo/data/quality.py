"""Data-quality report for the local store (`algo check-data`).

Checks what the backtest silently depends on:
  * spot: trading days, full 375-bar days, bars outside 09:15-15:29 (pre-open / closing-price /
    Muhurat bars; the backtest drops them), short sessions (skipped as special), missing minutes,
    inconsistent OHLC
  * options: whether the nearest-expiry ATM / ITM1 contracts exist with candles around the money,
    sampled on every `step`-th day at 10:00, 12:00 and 14:30 (a missing contract becomes a silent
    `no_option` skip in a backtest)
  * futures: days with spot data but no front-month futures bars (no volume for VWAP filters)
"""
from __future__ import annotations

from datetime import date, time

import pandas as pd

from algo.data.store import DataStore

FULL_DAY = 375


def spot_report(store: DataStore, ul: str, start: date, end: date) -> dict:
    s = store.load_spot(ul, start, end)
    if s.empty:
        return {"days": 0}
    t = s.index.time
    regular = (t >= time(9, 15)) & (t <= time(15, 29))
    day = s.index.normalize()
    off = pd.Series(~regular, index=s.index).groupby(day).sum()
    reg_n = pd.Series(regular, index=s.index).groupby(day).sum()
    bad_ohlc = int(((s["high"] < s[["open", "close"]].max(axis=1)) |
                    (s["low"] > s[["open", "close"]].min(axis=1))).sum())
    short = reg_n[reg_n < 300]
    gaps = reg_n[(reg_n >= 300) & (reg_n < FULL_DAY)]
    off_days = off[off > 0]
    return {
        "range": f"{s.index[0].date()}..{s.index[-1].date()}",
        "days": int(len(reg_n)),
        "full_days": int((reg_n == FULL_DAY).sum()),
        "days_with_missing_minutes": int(len(gaps)),
        "short_sessions_skipped": [str(d.date()) for d in short.index],
        "days_with_off_session_bars": int(len(off_days)),
        "off_session_months": sorted({d.strftime("%Y-%m") for d in off_days.index}),
        "bad_ohlc_bars": bad_ohlc,
        "volume_nonzero_pct": round(float((s.get("volume", pd.Series(0.0, index=s.index)) > 0).mean() * 100), 1),
    }


def option_report(store: DataStore, ul: str, step_pts: int, start: date, end: date, every: int = 3) -> dict:
    spot = store.load_spot(ul, start, end)
    exps = sorted(store.expiries(ul))
    if spot.empty or not exps:
        return {"expiries": len(exps)}
    rows = []
    days = sorted(set(spot.index.normalize()))
    for d in days[::every]:
        nxt = [e for e in exps if e >= d.date()]
        if not nxt:
            rows.append((d, "no_expiry"))
            continue
        e = nxt[0]
        for hhmm in ("10:00", "12:00", "14:30"):
            ts = pd.Timestamp(f"{d.date()} {hhmm}")
            if ts not in spot.index:
                continue
            atm = round(float(spot.loc[ts, "close"]) / step_pts) * step_pts
            for k, typ in ((atm, "CE"), (atm, "PE"), (atm - step_pts, "CE"), (atm + step_pts, "PE")):
                ok = store.option_path(ul, e, k, typ).exists()
                rows.append((d, "ok" if ok else "missing"))
    r = pd.DataFrame(rows, columns=["d", "status"])
    miss = r[r.status != "ok"]
    return {
        "expiries": f"{len(exps)} ({exps[0]}..{exps[-1]})",
        "samples": int(len(r)),
        "missing_pct": round(len(miss) / max(len(r), 1) * 100, 1),
        "missing_by_month": {str(k): int(v) for k, v in miss.d.dt.to_period("M").value_counts().sort_index().items()},
    }


def futures_report(store: DataStore, ul: str, start: date, end: date) -> dict:
    spot = store.load_spot(ul, start, end)
    fut = store.continuous_futures(ul, start, end, 1)
    if spot.empty:
        return {}
    sd = set(spot.index.normalize())
    fd = set(fut.index.normalize()) if len(fut) else set()
    first = min(fd) if fd else None
    missing = sorted(d for d in sd - fd if first is not None and d >= first)
    return {"first_day": str(first.date()) if first is not None else None,
            "spot_days_without_futures": [str(d.date()) for d in missing]}
