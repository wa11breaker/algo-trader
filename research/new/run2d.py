"""Shared harness: backtest ANY Strategy instance on the two recorded days (2026-11-04, 2026-11-05)
with real Fyers option candles, real footprint order flow and the normal engine (fills, slippage, costs).

    from run2d import run2d
    trades = run2d(MyStrategy(**params), capital=50_000)     # DataFrame of trades, both days

Data per day: research/fvg/spot_<day>.parquet (1-min index, ~a week of warm-up), options_<day>.parquet
(weekly expiry 2026-11-09; strikes 23400-24350 on Nov 4, 23900-25000 on Nov 5), and
data/footprint/NIFTY/<day>.csv via ctx.order_flow(). No futures candles (ctx.futures is empty).
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fvg"))
import today_pnl as tp  # noqa: E402
from algo.backtest.data import HistoricalData  # noqa: E402
from algo.backtest.engine import run_backtest  # noqa: E402
from algo.config import get_settings  # noqa: E402

DAYS = [date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)]
COLS = ["entry_time", "type", "strike", "lots", "entry_price", "exit_time", "exit_price", "exit_reason", "net_pnl", "tag"]


def run2d(strategy, capital: float = 50_000, days=DAYS) -> pd.DataFrame:
    s = get_settings()
    ul = s.underlying("NIFTY")
    out = []
    for day in days:
        with tempfile.TemporaryDirectory() as tmp:
            data = HistoricalData(tp.build_store(day, Path(tmp), tp.LOT), ul, day, day)
            flow = tp.load_flow(day)
            data.order_flow = lambda until, tf="1min", symbol=None, f=flow: f[f.index <= pd.Timestamp(until)]
            out.append(run_backtest(strategy, ul, day, day, s, data=data, capital=capital).trades_df())
    out = [t for t in out if len(t)]
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=COLS)


def show(name: str, t: pd.DataFrame) -> None:
    print(f"\n== {name}: {len(t)} trades, net Rs {t.net_pnl.sum() if len(t) else 0:,.0f}, wins {(t.net_pnl > 0).sum() if len(t) else 0}")
    if len(t):
        t = t.assign(day=pd.to_datetime(t.entry_time).dt.date)
        print(t.groupby("day").net_pnl.agg(["count", "sum"]).to_string())
        print(t[[c for c in COLS if c in t]].to_string(index=False))


if __name__ == "__main__":
    import argparse
    import yaml
    from algo.strategies import get_strategy
    ap = argparse.ArgumentParser(description="Backtest a strategy on the recorded days with real option prices")
    ap.add_argument("-s", "--strategy", default="fvg_flow")
    ap.add_argument("-c", "--capital", type=float, default=50_000)
    ap.add_argument("-p", "--param", action="append", default=[], help="key=value, e.g. -p st_tf=5min")
    a = ap.parse_args()
    params = {k: yaml.safe_load(v) for k, v in (x.split("=", 1) for x in a.param)}
    show(f"{a.strategy} {params} at Rs {a.capital:,.0f}", run2d(get_strategy(a.strategy, **params), a.capital))
