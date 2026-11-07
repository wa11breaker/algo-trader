"""Rupee P&L of fvg_flow on one recorded day: full backtest engine with REAL option prices.

    .venv/bin/python research/fvg/today_pnl.py [--day 2026-11-04] [--capital 100000] [-p key=value ...]

Inputs (research/fvg/): spot_<day>.parquet (Fyers 1-min index), options_<day>.parquet (Fyers 1-min
option candles, columns contract/ts/ohlcv; weekly expiry EXPIRY), and data/footprint/NIFTY/<day>.csv
(real order flow). Options are written to a throw-away DataStore so the normal engine picks strikes
(OTM1, stepping out while > Rs150), sizes lots, fills with slippage and charges costs.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.config import get_settings
from algo.data.store import DataStore
from algo.strategies import get_strategy

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from today import load_flow  # noqa: E402

LOT = 65   # NIFTY lot size in Nov 2026 (the live paper fills)
EXPIRY = {date(2026, 11, 4): date(2026, 11, 9), date(2026, 11, 5): date(2026, 11, 9), date(2026, 11, 6): date(2026, 11, 9)}
LEGACY = {"min_gap_atr": None, "trend": "none", "tp_mode": "r", "breakeven_at_r": 1.0, "min_delta_ratio": None}


def build_store(day: date, root: Path, lot: int) -> DataStore:
    store = DataStore(root)
    spot = pd.read_parquet(HERE / f"spot_{day}.parquet")
    for (y, m), g in spot.groupby([spot.index.year, spot.index.month]):
        store.save_spot_month("NIFTY", y, m, g)
    opts = pd.read_parquet(HERE / f"options_{day}.parquet")
    exp = EXPIRY[day]
    rows = []
    for c, g in opts.groupby("contract"):
        k, t = int(c[:-2]), c[-2:]
        key = f"NSE:NIFTY{exp:%y}N{exp:%d}{k}{t}"
        rows.append({"instrument_key": key, "trading_symbol": f"NIFTY {k} {t}", "strike_price": float(k),
                     "instrument_type": t, "lot_size": lot, "expiry": exp.isoformat(), "exchange": "NSE",
                     "segment": "NSE_FO", "tick_size": 0.05, "weekly": True, "underlying_key": "NSE_INDEX|Nifty 50"})
        store.save_option("NIFTY", exp, k, t, g.set_index("ts")[["open", "high", "low", "close", "volume", "oi"]])
    store.save_contracts("NIFTY", exp, rows)
    return store


def run(day: date, params: dict, capital: float):
    s = get_settings()
    ul = s.underlying("NIFTY")
    with tempfile.TemporaryDirectory() as tmp:
        store = build_store(day, Path(tmp), LOT)
        data = HistoricalData(store, ul, day, day)
        flow = load_flow(day)
        data.order_flow = lambda until, tf="1min", symbol=None: flow[flow.index <= pd.Timestamp(until)]
        strat = get_strategy("fvg_flow", **params)
        res = run_backtest(strat, ul, day, day, s, data=data, capital=capital)
        return res.trades_df(), strat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-11-04")
    ap.add_argument("--capital", type=float, default=100_000)
    a = ap.parse_args()
    day = date.fromisoformat(a.day)
    for name, params in [("original rules", LEGACY), ("tuned (new defaults)", {})]:
        for label, extra in [("1 lot", {"size_from_capital": False, "lots": 1}), (f"Rs{a.capital:,.0f} sizing", {})]:
            t, strat = run(day, {**params, **extra}, a.capital)
            print(f"\n== {name}, {label}: {len(t)} trades, net Rs {t.net_pnl.sum():,.0f} "
                  f"(gross {t.gross_pnl.sum():,.0f}, costs {t.costs.sum():,.0f}), wins {(t.net_pnl > 0).sum()}")
            cols = ["entry_time", "type", "strike", "lots", "entry_price", "exit_time", "exit_price", "exit_reason", "net_pnl"]
            print(t[cols].to_string(index=False))


if __name__ == "__main__":
    main()
