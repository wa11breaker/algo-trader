import numpy as np
import pandas as pd
import pytest

from algo.indicators import adx, bollinger, swing_points


def frame(highs, lows, closes=None):
    idx = pd.date_range("2025-01-07 09:15", periods=len(highs), freq="5min")
    closes = closes or [(h + l) / 2 for h, l in zip(highs, lows)]
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes}, index=idx)


def test_bollinger_width_zero_on_constant_prices():
    b = bollinger(pd.Series([100.0] * 30), 20, 2)
    assert b["width"].iloc[-1] == 0 and b["mid"].iloc[-1] == 100
    assert np.isnan(b["width"].iloc[18])                          # not enough bars yet


def test_bollinger_bands():
    s = pd.Series([1.0, 2, 3, 4, 5])
    b = bollinger(s, 5, 2)
    sd = np.std([1, 2, 3, 4, 5])
    assert b["upper"].iloc[-1] == pytest.approx(3 + 2 * sd) and b["lower"].iloc[-1] == pytest.approx(3 - 2 * sd)


def test_adx_high_in_trend_low_in_chop():
    up = frame([100 + 2 * i + 1 for i in range(60)], [100 + 2 * i - 1 for i in range(60)])
    chop = frame([101 + (i % 2) for i in range(60)], [99 - (i % 2) for i in range(60)])
    assert adx(up).iloc[-1] > 50 and adx(chop).iloc[-1] < 20


def test_swing_points_are_reported_only_when_confirmed():
    highs = [10, 11, 15, 12, 11, 13, 14]
    lows = [9, 10, 14, 8, 10, 12, 13]
    sw = swing_points(frame(highs, lows), 2, 2)
    # pivot high 15 at bar 2 is known at bar 4; pivot low 8 at bar 3 is known at bar 5
    assert np.isnan(sw["swing_high"].iloc[2]) and np.isnan(sw["swing_high"].iloc[3])
    assert sw["swing_high"].iloc[4] == 15 and sw["high_ts"].iloc[4] == sw.index[2]
    assert sw["swing_low"].iloc[5] == 8 and sw["low_ts"].iloc[5] == sw.index[3]
    assert sw["swing_high"].notna().sum() == 1 and sw["swing_low"].notna().sum() == 1


def test_swing_points_no_lookahead():
    highs = [10, 11, 15, 12, 11, 13, 14]
    lows = [9, 10, 14, 8, 10, 12, 13]
    full = swing_points(frame(highs, lows), 2, 2)
    for n in range(3, len(highs)):
        part = swing_points(frame(highs[:n], lows[:n]), 2, 2)
        pd.testing.assert_frame_equal(part, full.iloc[:n])
