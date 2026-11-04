from datetime import date

import numpy as np
import pandas as pd
import pytest

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.indicators import vwap_bands
from algo.strategies import get_strategy

from .conftest import NIFTY, make_settings
from .helpers import build_market, fill_day, flat

D0, D1, EXP = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 9)
DAY0 = flat(23000, 75)
# 09:15-09:55: candles alternating around 22985 / 23015 -> VWAP ~23000, sd ~15, upper 3 sd band ~23043
CHOP = [(m, m + 5, m - 5, m) for m in (23000 + (15 if i % 2 else -15) for i in range(9))]
SPIKE = [(23015, 23122, 23010, 23120)]          # 10:00 closes above VWAP + 3 sd (~23097)
BACK = [(23120, 23122, 23075, 23080)]           # 10:05 closes back inside -> buy PE at 23080
DROP = [(23080, 23082, 23000, 23005)]           # falls through VWAP -> target
P = {"require_volume": False}


def mirror(candles):
    return [(46000 - o, 46000 - l, 46000 - h, 46000 - c) for o, h, l, c in candles]


def run(tmp_path, day1, last, **params):
    store = build_market(tmp_path, {D0: DAY0, D1: fill_day(day1, last)}, EXP)
    strat = get_strategy("vwap_sd_fade", size_from_capital=False, **{**P, **params})
    res = run_backtest(strat, NIFTY, D1, D1, make_settings(tmp_path), data=HistoricalData(store, NIFTY, D1, D1))
    return strat, res.trades_df()


def test_vwap_bands_match_hand_computation():
    idx = pd.date_range("2025-01-07 09:15", periods=3, freq="1min")
    df = pd.DataFrame({"high": [11, 21, 31], "low": [9, 19, 29], "close": [10, 20, 30],
                       "volume": [1.0, 1.0, 2.0]}, index=idx)
    b = vwap_bands(df)
    tp, w = np.array([10, 20, 30.0]), np.array([1, 1, 2.0])
    m = (tp * w).sum() / w.sum()
    assert b["vwap"].iloc[-1] == pytest.approx(m)
    assert b["sd"].iloc[-1] == pytest.approx(np.sqrt((w * (tp - m) ** 2).sum() / w.sum()))
    assert b["sd"].iloc[0] == 0


def test_upper_band_stretch_then_reentry_buys_put_targeting_vwap(tmp_path):
    strat, t = run(tmp_path, CHOP + SPIKE + BACK + DROP, 23005)
    assert len(t) == 1 and t.iloc[0]["type"] == "PE" and t.iloc[0]["tag"] == "vsd_down"
    d = strat.diagnostics().iloc[0]
    assert d["entry"] == 23080 and d["init_stop"] == 23127              # spike high 23122 + 5
    assert d["target"] == pytest.approx(d["vwap"]) and d["why"] == "target"
    assert pd.Timestamp(t.iloc[0]["entry_time"]) == pd.Timestamp("2025-01-07 10:10")   # no lookahead


def test_lower_band_mirror_buys_call(tmp_path):
    strat, t = run(tmp_path, mirror(CHOP + SPIKE + BACK + DROP), 22995)
    assert len(t) == 1 and t.iloc[0]["type"] == "CE"
    d = strat.diagnostics().iloc[0]
    assert d["entry"] == 22920 and d["init_stop"] == 22873 and d["why"] == "target"


def test_touch_mode_enters_on_the_stretch_close(tmp_path):
    strat, t = run(tmp_path, CHOP + SPIKE + BACK + DROP, 23005, confirm="touch", min_risk_pts=5)
    assert strat.diagnostics().iloc[0]["entry"] == 23120 and t.iloc[0]["type"] == "PE"


def test_long_stretch_is_a_trend_day_not_faded(tmp_path):
    # two +150 candles keep closing beyond the (fast-widening) band: 3 closes outside in a row
    trend = [(23120 + 150 * i, 23122 + 150 * (i + 1), 23118 + 150 * i, 23120 + 150 * (i + 1)) for i in range(2)]
    strat, t = run(tmp_path, CHOP + SPIKE + trend + [(23420, 23425, 23250, 23260)], 23260,
                   max_stretch_bars=2, entry_end="10:30")
    assert t.empty and strat.skips["stretch_too_long"] == 1


def test_min_rr_and_entry_window_block_trades(tmp_path):
    strat, t = run(tmp_path, CHOP + SPIKE + BACK + DROP, 23005, min_rr=3.0, entry_end="10:30")
    assert t.empty and strat.skips["target_too_close"] == 1
    strat, t = run(tmp_path, CHOP + SPIKE + BACK + DROP, 23005, entry_start="11:00", entry_end="12:00")
    assert t.empty and strat.skips["window_or_max_trades"] >= 1


def test_no_volume_means_no_trades(tmp_path):
    strat, t = run(tmp_path, CHOP + SPIKE + BACK + DROP, 23005, require_volume=True)
    assert t.empty and strat.skips["no_volume"] == 1


@pytest.mark.parametrize("bad", [{"confirm": "x"}, {"tp_type": "x"}])
def test_bad_params(bad):
    with pytest.raises(ValueError):
        get_strategy("vwap_sd_fade", **bad)
