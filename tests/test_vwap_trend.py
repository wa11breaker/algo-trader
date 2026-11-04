from datetime import date

import pandas as pd
import pytest

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.strategies import get_strategy

from .conftest import NIFTY, make_settings
from .helpers import build_market, fill_day, flat

D0, D1, EXP = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 9)
DAY0 = flat(23000, 75)
# 8 rising candles 09:15-09:55; the test market has no volume, so VWAP is the running mean of
# typical prices: ~23041.7 after the 8th candle (close 23080)
RISE = [(23000 + 10 * i, 23012 + 10 * i, 22998 + 10 * i, 23010 + 10 * i) for i in range(8)]
RETEST = [(23080, 23082, 23050, 23060)]            # dips to 23050 (VWAP ~23044), closes above VWAP
CONFIRM = [(23060, 23086, 23058, 23084)]           # green, closes above the retest bar's high -> entry
P = {"max_risk_pts": 60.0, "use_htf_trend": True}


def run(tmp_path, rest, last, day1=None, **params):
    d1 = day1 if day1 is not None else RISE + rest
    store = build_market(tmp_path, {D0: DAY0, D1: fill_day(d1, last)}, EXP)
    strat = get_strategy("vwap_trend", size_from_capital=False, **{**P, **params})
    res = run_backtest(strat, NIFTY, D1, D1, make_settings(tmp_path), data=HistoricalData(store, NIFTY, D1, D1))
    return strat, res.trades_df()


def test_retest_and_confirmation_buys_call_then_trailing_stop_locks_profit(tmp_path):
    rally = [(23084, 23110, 23082, 23108), (23108, 23135, 23106, 23133), (23133, 23160, 23131, 23158),
             (23158, 23160, 23120, 23125)]                                   # pullback hits the trail
    strat, t = run(tmp_path, RETEST + CONFIRM + rally, 23125)
    assert len(t) == 1
    tr, d = t.iloc[0], strat.diagnostics().iloc[0]
    assert tr["type"] == "CE" and tr["tag"] == "vwt_up" and tr["strike"] == 23050   # ITM1 of ATM 23100
    assert d["entry"] == 23084 and d["init_stop"] == 23045 and d["risk"] == 39     # retest low 23050 - 5
    assert d["why"] == "trail" and d["index_R"] > 0.5 and tr["gross_pnl"] > 0   # trail locked ~+0.9R
    # filled on the minute after the confirmation candle closed (10:05): no lookahead
    assert pd.Timestamp(tr["entry_time"]) == pd.Timestamp("2025-01-07 10:05")


def test_index_stop_below_retest_low(tmp_path):
    _, t = run(tmp_path, RETEST + CONFIRM + [(23084, 23086, 23030, 23035)], 23035)
    assert t.iloc[0]["exit_reason"] == "SL" and t.iloc[0]["gross_pnl"] < 0


def test_retest_without_confirmation_is_skipped(tmp_path):
    drift = [(23060, 23070, 23056, 23062), (23062, 23072, 23058, 23064)]   # never closes above prev high
    # entry_end keeps the quiet filler candles after the scenario from producing a new setup
    strat, t = run(tmp_path, RETEST + drift, 23064, entry_end="10:12")
    assert t.empty and strat.skips["no_confirmation"] >= 1


def test_close_below_vwap_cancels_setup(tmp_path):
    strat, t = run(tmp_path, RETEST + [(23060, 23062, 23020, 23025)], 23025)
    assert t.empty and strat.skips["lost_vwap"] == 1


def test_downtrend_mirror_buys_put(tmp_path):
    fall = [(23000 - 10 * i, 23002 - 10 * i, 22988 - 10 * i, 22990 - 10 * i) for i in range(8)]
    retest = [(22920, 22950, 22918, 22940)]
    confirm = [(22940, 22942, 22914, 22916)]
    slide = [(22916, 22918, 22890, 22892), (22892, 22894, 22865, 22867), (22867, 22869, 22840, 22842)]
    strat, t = run(tmp_path, [], 22842, day1=fall + retest + confirm + slide)
    assert len(t) == 1 and t.iloc[0]["type"] == "PE" and t.iloc[0]["tag"] == "vwt_down"
    assert strat.diagnostics().iloc[0]["init_stop"] == 22955       # retest high 22950 + 5


def test_flat_day_no_trade(tmp_path):
    _, t = run(tmp_path, [], 23000, day1=flat(23000, 20, wiggle=6))
    assert t.empty


@pytest.mark.parametrize("bad", [{"confirm_mode": "x"}])
def test_bad_params(bad):
    with pytest.raises(ValueError):
        get_strategy("vwap_trend", **bad)


def test_futures_vwap_matches_spot_vwap_when_basis_is_constant(synthetic):
    """Synthetic futures = spot + a constant basis with the same volume, so futures VWAP minus the
    basis equals the volume-weighted spot VWAP: both sources must give identical trades."""
    from algo.backtest.data import HistoricalData as HD
    days = synthetic["days"]
    s = make_settings(synthetic["root"])
    out = {}
    for src in ("spot", "futures"):
        data = HD(synthetic["store"], NIFTY, days[0], days[-1])
        strat = get_strategy("vwap_trend", vwap_source=src, size_from_capital=False, max_risk_pts=200.0,
                             touch_pts=30.0, pierce_pts=30.0, use_htf_trend=False)
        t = run_backtest(strat, NIFTY, days[0], days[-1], s, data=data).trades_df()
        out[src] = t[["entry_time", "type", "strike"]].reset_index(drop=True) if len(t) else t
    assert len(out["spot"]) > 0
    pd.testing.assert_frame_equal(out["spot"], out["futures"])


def test_futures_source_without_futures_data_does_not_trade(tmp_path):
    rally = [(23084, 23110, 23082, 23108)]
    strat, t = run(tmp_path, RETEST + CONFIRM + rally, 23108, vwap_source="futures")
    assert t.empty and strat.skips["no_futures"] > 0
