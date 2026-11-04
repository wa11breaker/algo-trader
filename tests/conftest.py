"""Synthetic market data so tests run without Upstox access."""
from __future__ import annotations

import math
from pathlib import Path
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from algo.config import Settings, Underlying, get_settings
from algo.data.store import DataStore

NIFTY = Underlying("NIFTY", "NSE_INDEX|Nifty 50", "NSE", 50)
START = date(2025, 1, 6)   # Monday
N_DAYS = 10


def trading_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def minutes(d: date) -> pd.DatetimeIndex:
    return pd.date_range(datetime.combine(d, time(9, 15)), periods=375, freq="1min")


def spot_price(i: int) -> float:
    # oscillation (gives EMA crossovers) + slow drift
    return 23000 + 150 * math.sin(i / 45) + 0.05 * i


def option_price(spot: float, strike: float, typ: str, minutes_left: float) -> float:
    intrinsic = max(spot - strike, 0) if typ == "CE" else max(strike - spot, 0)
    return round(intrinsic + 20 + 0.004 * minutes_left, 2)


@pytest.fixture(scope="session")
def synthetic(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    store = DataStore(root)
    days = trading_days(START, N_DAYS)
    idx = pd.DatetimeIndex([t for d in days for t in minutes(d)], name="ts")
    px = [spot_price(i) for i in range(len(idx))]
    spot = pd.DataFrame({"open": px, "high": [p + 3 for p in px], "low": [p - 3 for p in px],
                         "close": px, "volume": 0.0, "oi": 0.0}, index=idx)
    for (y, m), g in spot.groupby([spot.index.year, spot.index.month]):
        store.save_spot_month("NIFTY", y, m, g)

    thursdays = [d for d in trading_days(START, N_DAYS + 5) if d.weekday() == 3]
    strikes = range(22600, 23500, 50)
    for exp in thursdays:
        contracts = [{"instrument_key": f"NSE_FO|{k}{t}|{exp:%d-%m-%Y}", "trading_symbol": f"NIFTY {k} {t}",
                      "strike_price": float(k), "instrument_type": t, "lot_size": 75,
                      "expiry": exp.isoformat(), "exchange": "NSE", "segment": "NSE_FO",
                      "tick_size": 5, "weekly": True, "underlying_key": "NSE_INDEX|Nifty 50"}
                     for k in strikes for t in ("CE", "PE")]
        store.save_contracts("NIFTY", exp, contracts)
        live = spot[spot.index.normalize() <= pd.Timestamp(exp)]
        mins_left = ((pd.Timestamp(exp) + pd.Timedelta(hours=15, minutes=30)) - live.index) / pd.Timedelta(minutes=1)
        for k in strikes:
            for t in ("CE", "PE"):
                c = [option_price(s, k, t, m) for s, m in zip(live["close"], mins_left)]
                df = pd.DataFrame({"open": c, "high": [x + 1 for x in c], "low": [max(x - 1, 0.05) for x in c],
                                   "close": c, "volume": 1000.0, "oi": 5000.0}, index=live.index)
                store.save_option("NIFTY", exp, k, t, df)
    # Two "monthly" futures to exercise the roll: basis 30 (exp 01-09) vs 80 (exp 01-30);
    # volume spikes every 37 minutes so the VWAP/volume strategy has signals.
    for exp, basis, vol0 in [(date(2025, 1, 9), 30.0, 1000.0), (date(2025, 1, 30), 80.0, 2000.0)]:
        live = spot[spot.index.normalize() <= pd.Timestamp(exp)]
        vol = [vol0 * (4 if i % 37 == 0 else 1) for i in range(len(live))]
        fut = pd.DataFrame({"open": live["open"] + basis, "high": live["high"] + basis, "low": live["low"] + basis,
                            "close": live["close"] + basis, "volume": vol,
                            "oi": [1e6 + i for i in range(len(live))]}, index=live.index)
        store.save_future("NIFTY", exp, fut, {"instrument_key": f"NSE_FO|F{exp:%m%d}", "trading_symbol": "NIFTY FUT",
                                               "expiry": exp.isoformat(), "lot_size": 75})
    return {"store": store, "root": root, "days": days, "spot": spot}


def make_settings(data_dir, risk: dict | None = None, calendar: dict | None = None) -> Settings:
    """Settings pointing at a test data dir. Risk manager and calendar filters OFF unless given."""
    raw = {**get_settings().raw, "data_dir": str(data_dir),
           "risk": {"enabled": False} if risk is None else {"enabled": True, **risk},
           "calendar": calendar or {}}
    return Settings(raw=raw, root=Path(data_dir).parent)


@pytest.fixture
def settings(synthetic) -> Settings:
    return make_settings(synthetic["root"])
