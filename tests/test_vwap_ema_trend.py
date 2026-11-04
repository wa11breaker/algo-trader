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
# 8 rising 5-min candles 09:15-09:55: close above VWAP (~23042, no volume in the test market) and
# EMA9 above EMA21 (both warmed up on the flat previous day)
RISE = [(23000 + 10 * i, 23012 + 10 * i, 22998 + 10 * i, 23010 + 10 * i) for i in range(8)]
PULLBACK = [(23080, 23082, 23050, 23060)]          # dips to VWAP / EMA9, closes above VWAP
CONFIRM = [(23060, 23086, 23058, 23084)]           # green, closes above the pullback high -> entry
RALLY = [(23084, 23110, 23082, 23108), (23108, 23135, 23106, 23133), (23133, 23160, 23131, 23158)]


def run(tmp_path, rest, last, day1=None, **params):
    d1 = day1 if day1 is not None else RISE + rest
    store = build_market(tmp_path, {D0: DAY0, D1: fill_day(d1, last)}, EXP)
    # one trade per scenario: the quiet filler candles after it would otherwise allow a re-entry
    strat = get_strategy("vwap_ema_trend", size_from_capital=False, **{"max_trades_per_day": 1, **params})
    res = run_backtest(strat, NIFTY, D1, D1, make_settings(tmp_path), data=HistoricalData(store, NIFTY, D1, D1))
    return strat, res.trades_df()


def test_pullback_and_bullish_candle_buys_call_and_hits_premium_target(tmp_path):
    strat, t = run(tmp_path, PULLBACK + CONFIRM + RALLY, 23158)
    assert len(t) == 1
    tr, d = t.iloc[0], strat.diagnostics().iloc[0]
    assert tr["type"] == "CE" and tr["tag"] == "vet_up" and tr["strike"] == 23050      # ITM1 of ATM 23100
    # filled on the minute after the confirmation candle closed (10:05): no lookahead
    assert pd.Timestamp(tr["entry_time"]) == pd.Timestamp("2025-01-07 10:05")
    assert tr["exit_reason"] == "TARGET" and d["why"] == "target"
    assert d["option_ret_pct"] >= 49      # target = +50% (1:2 on a 25% stop); a gap past it fills at the open
    assert d["pullback_extreme"] == 23050


def test_premium_stop_loss(tmp_path):
    strat, t = run(tmp_path, PULLBACK + CONFIRM + [(23084, 23086, 23062, 23064)], 23064)
    assert t.iloc[0]["exit_reason"] == "SL" and t.iloc[0]["gross_pnl"] < 0
    # test option bars are point prices, so the stop fills at the next bar's open: never better than -25%
    assert strat.diagnostics().iloc[0]["option_ret_pct"] <= -24


def test_close_below_vwap_exits_on_trend_loss(tmp_path):
    # wide premium stop so the 5-min close back under VWAP is what takes us out
    strat, t = run(tmp_path, PULLBACK + CONFIRM + [(23084, 23086, 23020, 23025)], 23025, sl_pct=90.0, rr=1.0)
    assert t.iloc[0]["exit_reason"] == "SIGNAL" and strat.diagnostics().iloc[0]["why"] == "vwap"


def test_pullback_without_confirmation_is_skipped(tmp_path):
    drift = [(23060, 23070, 23056, 23062), (23062, 23072, 23058, 23064)]   # never closes above prev high
    strat, t = run(tmp_path, PULLBACK + drift, 23064, entry_end="10:12")
    assert t.empty and strat.skips["no_confirmation"] >= 1


def test_downtrend_mirror_buys_put(tmp_path):
    fall = [(23000 - 10 * i, 23002 - 10 * i, 22988 - 10 * i, 22990 - 10 * i) for i in range(8)]
    pullback = [(22920, 22950, 22918, 22940)]
    confirm = [(22940, 22942, 22914, 22916)]
    slide = [(22916, 22918, 22890, 22892), (22892, 22894, 22865, 22867), (22867, 22869, 22840, 22842)]
    _, t = run(tmp_path, [], 22842, day1=fall + pullback + confirm + slide)
    assert len(t) == 1 and t.iloc[0]["type"] == "PE" and t.iloc[0]["tag"] == "vet_down"


def test_index_mode_uses_pullback_stop(tmp_path):
    strat, t = run(tmp_path, PULLBACK + CONFIRM + RALLY, 23158, exit_mode="index")
    d = strat.diagnostics().iloc[0]
    assert len(t) == 1 and d["init_stop"] == 23045 and d["risk"] == 39             # pullback low 23050 - 5
    assert d["target"] == 23084 + 2 * 39


def test_signal_logged_for_index_research(tmp_path):
    strat, _ = run(tmp_path, PULLBACK + CONFIRM + RALLY, 23158)
    assert len(strat.plan_log) >= 1 and strat.plan_log[0]["stop"] == 23045


def test_flat_day_no_trade(tmp_path):
    _, t = run(tmp_path, [], 23000, day1=flat(23000, 20, wiggle=6))
    assert t.empty


@pytest.mark.parametrize("bad", [{"confirm_mode": "x"}, {"exit_mode": "x"}, {"pullback_to": "x"},
                                 {"structure_exit": "x"}, {"sl_pct": 0}])
def test_bad_params(bad):
    with pytest.raises(ValueError):
        get_strategy("vwap_ema_trend", **bad)
