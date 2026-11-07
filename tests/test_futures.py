from datetime import date, datetime, time

import pandas as pd

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest

from .conftest import NIFTY


def _data(synthetic, **kw):
    d = synthetic["days"]
    return HistoricalData(synthetic["store"], NIFTY, d[0], d[-1], **kw)


def test_continuous_futures_rolls_day_before_expiry(synthetic):
    fut = _data(synthetic).fut_1m
    by_day = fut.groupby(fut.index.date)["expiry"].unique()
    assert list(by_day[date(2025, 1, 8)]) == ["2025-01-09"]   # front month
    assert list(by_day[date(2025, 1, 9)]) == ["2025-01-30"]   # expiry day -> rolled
    # roll_days_before=0 keeps the expiring contract on its expiry day
    fut0 = _data(synthetic, futures_roll_days_before=0).fut_1m
    assert set(fut0[fut0.index.date == date(2025, 1, 9)]["expiry"]) == {"2025-01-09"}


def test_spot_bars_get_futures_volume(synthetic):
    data = _data(synthetic)
    ts = pd.Timestamp(datetime(2025, 1, 7, 10, 0))
    assert data.spot_1m.loc[ts, "volume"] == data.fut_1m.loc[ts, "volume"] > 0
    assert data.spot_1m.loc[ts, "oi"] == data.fut_1m.loc[ts, "oi"]
    # spot prices are untouched (futures carry a basis)
    assert data.spot_1m.loc[ts, "close"] == synthetic["spot"].loc[ts, "close"]


def test_futures_bars_respect_clock(synthetic):
    data = _data(synthetic)
    at = datetime(2025, 1, 7, 9, 19)
    b5 = data.futures_bars(at, "5min")
    assert b5.index[-1] == pd.Timestamp(datetime(2025, 1, 7, 9, 15))
    assert b5["volume"].iloc[-1] == data.fut_1m.loc["2025-01-07 09:15":"2025-01-07 09:19", "volume"].sum()


def test_continuous_futures_falls_back_to_next_contract_on_a_gap(tmp_path):
    """Upstox has no candles for some contracts (e.g. NIFTY Dec-2024): use the next month that day."""
    from datetime import date

    import pandas as pd

    from algo.data.store import DataStore

    st = DataStore(tmp_path)
    days = pd.date_range("2025-01-06 09:15", periods=3, freq="D")
    bars = lambda px: pd.DataFrame({"open": px, "high": px, "low": px, "close": px, "volume": 10.0, "oi": 1.0},
                                   index=days)
    st.save_future("NIFTY", date(2025, 1, 9), bars(100.0).iloc[[0, 2]],      # front: missing 01-07
                   {"instrument_key": "a", "expiry": "2025-01-09"})
    st.save_future("NIFTY", date(2025, 1, 30), bars(200.0), {"instrument_key": "b", "expiry": "2025-01-30"})
    cf = st.continuous_futures("NIFTY", date(2025, 1, 6), date(2025, 1, 8))
    assert cf["close"].tolist() == [100.0, 200.0, 100.0]
    assert cf["expiry"].tolist() == ["2025-01-09", "2025-01-30", "2025-01-09"]
