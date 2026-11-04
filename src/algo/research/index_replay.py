"""Index-only evaluation of IndexPlanMixin strategies.

Options history only starts in Oct 2024, but these strategies decide entries, stops and targets on
the INDEX. Running them with options hidden (`HistoricalData(..., options=False)`) still produces
every plan in `strategy.plan_log`; `replay` then walks each plan forward on 1-min spot bars:

    stop first (pessimistic), then target, breakeven at +breakeven_at_r, trailing stop from
    +trail_start_r, time exit at exit_time (at that bar's open). One position at a time: a plan
    proposed while the previous replayed trade is still open is skipped (as the live strategy
    would be in position).

The result is R per trade before option effects (delta < 1, theta, slippage, charges), which cost
roughly 0.1R per trade on NIFTY ITM1 options. Exact only for strategies with
`index_replay_exact = True`; others have extra signal exits that the replay ignores.
"""
from __future__ import annotations

from datetime import date, time

import pandas as pd

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.config import Settings
from algo.data.store import DataStore
from algo.strategies import get_strategy
from algo.strategies.index_plan import hm

NO_CALENDAR = {"skip_weekdays": "", "skip_days_before_monthly_expiry": None}


def replay(plans: list[dict], spot: pd.DataFrame, exit_time: time, breakeven_at_r: float | None = None,
           trail_start_r: float | None = None) -> pd.DataFrame:
    rows, busy_until = [], None
    by_day = {d: g for d, g in spot.groupby(spot.index.normalize())}
    for pl in plans:
        now = pd.Timestamp(pl["now"])
        if busy_until is not None and now < busy_until:
            continue
        d, entry, risk = pl["dir"], pl["entry"], pl["risk"]
        if not risk or risk <= 0:
            continue
        stop, target, best, be_done, trailing = pl["stop"], pl["target"], entry, False, False
        day = by_day.get(now.normalize())
        day = day[day.index > now] if day is not None else spot.iloc[0:0]
        exit_px, why, exit_ts = None, "eod", now
        for ts, b in zip(day.index, day.itertuples(index=False)):
            exit_ts = ts
            if ts.time() >= exit_time:
                exit_px, why = float(b.open), "time"
                break
            if (d == 1 and b.low <= stop) or (d == -1 and b.high >= stop):
                exit_px, why = stop, "trail" if trailing else "breakeven" if be_done else "stop"
                break
            if target is not None and ((d == 1 and b.high >= target) or (d == -1 and b.low <= target)):
                exit_px, why = target, "target"
                break
            best = max(best, b.high) if d == 1 else min(best, b.low)
            gain = d * (best - entry)
            if breakeven_at_r and not be_done and gain >= breakeven_at_r * risk:
                stop = max(stop, entry) if d == 1 else min(stop, entry)
                be_done = True
            if pl.get("trail_dist") and trail_start_r is not None and gain >= trail_start_r * risk:
                trail = best - d * pl["trail_dist"]
                if d * (trail - stop) > 0:
                    stop, trailing = trail, True
        if exit_px is None:
            exit_px = float(day["close"].iloc[-1]) if len(day) else entry
        busy_until = exit_ts
        rows.append({"time": now, "tag": pl["tag"], "dir": d, "entry": entry, "stop": pl["stop"],
                     "target": target, "risk": risk, "exit": exit_px, "why": why,
                     "R": d * (exit_px - entry) / risk})
    return pd.DataFrame(rows)


def run_index_only(strategy_name: str, params: dict, ul_name: str, start: date, end: date,
                   settings: Settings) -> tuple[pd.DataFrame, object]:
    """Run the strategy with options hidden and replay its plans on the index."""
    ul = settings.underlying(ul_name)
    data = HistoricalData(DataStore(settings.data_dir), ul, start, end, options=False)
    strat = get_strategy(strategy_name, **{"size_from_capital": False, **params})
    if not hasattr(strat, "_buy_with_plan"):
        raise ValueError(f"{strategy_name} does not use IndexPlanMixin: index-only mode is not supported")
    run_backtest(strat, ul, start, end, settings, data=data, use_risk=False, calendar=NO_CALENDAR)
    p = strat.p
    trades = replay(getattr(strat, "plan_log", []), data.spot_1m, hm(p["exit_time"]),
                    p.get("breakeven_at_r"), p.get("trail_start_r"))
    return trades, strat
