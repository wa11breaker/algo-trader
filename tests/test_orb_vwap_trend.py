from datetime import date, datetime

import pandas as pd
import pytest

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.strategies import get_strategy

from .conftest import NIFTY, make_settings
from .helpers import build_market, fill_day, flat

D0, D1, EXP = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 9)
DAY0 = [(23000, 23200, 22800, 23000)] + flat(23000, 74)
OPEN = [(23000, 23015, 22990, 23010), (23010, 23020, 23000, 23015), (23015, 23025, 23008, 23020)]  # OR 22990-23025
BREAKOUT = [(23020, 23045, 23018, 23042)]                 # 09:30 bar closes above the OR high -> arms
PULLBACK_RESUME = [(23042, 23046, 23028, 23030),          # dips to 23028, near the OR high (touch)
                   (23030, 23052, 23029, 23050)]          # closes above the previous high -> entry
# the hand-made market is quiet and only has one prior day, so loosen the day filter
LOOSE = {"or_min_atr": 0.0, "or_max_atr": 10.0}


def run(tmp_path, rest, last, capital=50000, **params):
    store = build_market(tmp_path, {D0: DAY0, D1: fill_day(OPEN + rest, last)}, EXP)
    strat = get_strategy("orb_vwap_trend", **{**LOOSE, **params})
    res = run_backtest(strat, NIFTY, D1, D1, make_settings(tmp_path), capital=capital,
                       data=HistoricalData(store, NIFTY, D1, D1))
    return strat, res.trades_df()


def test_breakout_pullback_resume_buys_itm_call_and_hits_target(tmp_path):
    rally = [(23050, 23070, 23048, 23068), (23068, 23090, 23066, 23088), (23088, 23110, 23086, 23108)]
    strat, trades = run(tmp_path, BREAKOUT + PULLBACK_RESUME + rally, 23108)
    assert len(trades) == 1
    t = trades.iloc[0]
    assert t["type"] == "CE" and t["strike"] == 23000          # ITM1 of ATM 23050
    assert t["tag"] == "orbvt_up" and t["exit_reason"] == "TARGET" and t["gross_pnl"] > 0
    # entry fills on the bar after the signal bar closes (09:45): no lookahead
    assert pd.Timestamp(t["entry_time"]) == pd.Timestamp(datetime(2025, 1, 7, 9, 45))
    d = strat.diagnostics().iloc[0]
    assert d["entry"] == 23050 and d["stop"] == 23050          # stop moved to breakeven after +1R
    assert d["init_stop"] == 23023 and d["risk"] == 27         # pullback low 23028 - 5
    assert d["target"] == pytest.approx(23050 + 2 * 27)
    # sized from capital: 6% of 50k = 3000 / (27 x 0.6 x 75 = 1215 per lot) -> 2 lots
    assert t["lots"] == 2


def test_index_stop_loss(tmp_path):
    drop = [(23050, 23052, 23015, 23018)]                      # through the 23023 stop
    _, trades = run(tmp_path, BREAKOUT + PULLBACK_RESUME + drop, 23018)
    assert trades.iloc[0]["exit_reason"] == "SL" and trades.iloc[0]["gross_pnl"] < 0


def test_breakout_that_falls_back_below_vwap_is_not_traded(tmp_path):
    fail = [(23042, 23044, 22995, 23000), (23000, 23030, 22998, 23028), (23028, 23050, 23026, 23048)]
    strat, trades = run(tmp_path, BREAKOUT + fail, 23048)
    assert trades.empty and strat.skips["setup_failed"] == 1


def test_breakout_without_pullback_is_not_chased(tmp_path):
    run_away = [(23042 + 25 * i, 23070 + 25 * i, 23040 + 25 * i, 23067 + 25 * i) for i in range(7)]
    strat, trades = run(tmp_path, BREAKOUT + run_away, 23217)
    assert trades.empty and strat.skips["no_pullback_entry"] == 1


def test_no_breakout_no_trade(tmp_path):
    strat, trades = run(tmp_path, flat(23005, 10, wiggle=6), 23005)
    assert trades.empty


def test_account_too_small_for_one_lot_skips(tmp_path):
    rally = [(23050, 23070, 23048, 23068)]
    strat, trades = run(tmp_path, BREAKOUT + PULLBACK_RESUME + rally, 23068, capital=10000)
    assert trades.empty and strat.skips["too_small_to_size"] == 1
