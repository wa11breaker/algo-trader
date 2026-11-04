"""Replay fvg_flow on ONE recorded day with the REAL order flow (footprint ticks), index-only.

    .venv/bin/python research/fvg/today.py --grid '{"target_r":[2,3]}' [--base '{}'] [--show]

Spot: research/fvg/spot_<day>.parquet (Fyers 1-min NIFTY50 index, incl. a week of warm-up).
Flow: data/footprint/NIFTY/<day>.csv -> 1-min volume / buy / sell / delta (recording starts 09:32:52).
Metric: net R = R - COST_PTS / risk, summed over the day (sum_netR) and per trade.
Warning: one day = a handful of trades; anything tuned here is fitted to this day.
"""
from __future__ import annotations

import argparse
import itertools
import json
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.config import get_settings
from algo.data.store import DataStore
from algo.research.index_replay import NO_CALENDAR, replay
from algo.strategies import get_strategy
from algo.strategies.index_plan import hm

ROOT = Path(__file__).resolve().parents[2]
COST_PTS = 2.0


def load_flow(day: date) -> pd.DataFrame:
    t = pd.read_csv(ROOT / "data/footprint/NIFTY" / f"{day}.csv", parse_dates=["ts"]).set_index("ts")
    g = t.resample("1min")
    return pd.DataFrame({"volume": g.vol.sum(), "buy_vol": g.buy.sum(), "sell_vol": g.sell.sum(),
                         "delta": (t.buy - t.sell).resample("1min").sum()}).dropna()


def make_data(day: date) -> HistoricalData:
    s = get_settings()
    spot = pd.read_parquet(Path(__file__).parent / f"spot_{day}.parquet")
    data = HistoricalData(DataStore(s.data_dir / "_none"), s.underlying("NIFTY"), day, day, spot=spot,
                          options=False)
    flow = load_flow(day)

    def order_flow(until, tf="1min", symbol=None):        # closed 1-min bars up to `until` (bar start)
        return flow[flow.index <= pd.Timestamp(until)]
    data.order_flow = order_flow
    return data


def evaluate(params: dict, day: date, data: HistoricalData | None = None):
    s = get_settings()
    data = data or make_data(day)
    strat = get_strategy("fvg_flow", **{"size_from_capital": False, **params})
    run_backtest(strat, s.underlying("NIFTY"), day, day, s, data=data, use_risk=False, calendar=NO_CALENDAR)
    p = strat.p
    t = replay(getattr(strat, "plan_log", []), data.spot_1m, hm(p["exit_time"]), p.get("breakeven_at_r"),
               p.get("trail_start_r"))
    return t, strat


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"n": 0, "sum_netR": 0.0}
    net = t.R - COST_PTS / t.risk
    return {"n": len(t), "wins": int((t.R > 0).sum()), "sum_netR": round(net.sum(), 2),
            "netR": round(net.mean(), 3), "pts": round((t.dir * (t.exit - t.entry)).sum(), 1),
            "long": round(net[t.dir == 1].sum(), 2), "short": round(net[t.dir == -1].sum(), 2),
            "max_dd_R": round((net.cumsum().cummax() - net.cumsum()).max(), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", default="[{}]")
    ap.add_argument("--base", default="{}")
    ap.add_argument("--day", default="2026-11-04")
    ap.add_argument("--show", action="store_true", help="print the trades of every config")
    ap.add_argument("--out")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    day = date.fromisoformat(a.day)
    g = yaml.safe_load(Path(a.grid).read_text() if Path(a.grid).exists() else a.grid)
    combos = [dict(zip(g, c)) for c in itertools.product(*[v if isinstance(v, list) else [v] for v in g.values()])] \
        if isinstance(g, dict) else g
    base, data, rows = json.loads(a.base), make_data(day), []
    for c in combos:
        t, strat = evaluate({**base, **c}, day, data)
        rows.append({"params": json.dumps(c, sort_keys=True), **stats(t), "skips": json.dumps(dict(strat.skips))})
        if a.show:
            print(json.dumps(c)); print(t.round(1).to_string() if len(t) else "  no trades"); print()
    df = pd.DataFrame(rows).sort_values("sum_netR", ascending=False)
    if a.out:
        df.to_csv(a.out, index=False)
    with pd.option_context("display.width", 250, "display.max_colwidth", 120):
        print(df.drop(columns="skips").head(a.top).to_string(index=False))


if __name__ == "__main__":
    main()
