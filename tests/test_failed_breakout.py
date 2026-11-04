from datetime import date

import pandas as pd
import pytest

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.config import Settings, get_settings
from algo.indicators import is_engulfing, is_pin_bar
from algo.strategies import get_strategy

from .conftest import NIFTY, make_settings
from .helpers import build_market, fill_day, flat

D0, D1, EXP = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 9)
DAY0 = [(23000, 23200, 22800, 23000)] + flat(23000, 74)          # PDH 23200 / PDL 22800 (far away)
OPEN = [(23000, 23015, 22990, 23005), (23005, 23010, 22985, 22995), (22995, 23008, 22990, 23000),  # OR 22985-23015
        (23000, 23008, 22995, 23005), (23005, 23010, 23000, 23008), (23008, 23012, 23004, 23010)]
# loose ATR filters: the hand-made market is very quiet, so ATR is tiny
LOOSE = {"levels": "OR", "max_break_atr": 10, "max_risk_atr": 20}


def run(tmp_path, day1_rest, last, **params):
    store = build_market(tmp_path, {D0: DAY0, D1: fill_day(OPEN + day1_rest, last)}, EXP)
    s = make_settings(tmp_path)
    strat = get_strategy("failed_breakout", **{**LOOSE, **params})
    res = run_backtest(strat, NIFTY, D1, D1, s, data=HistoricalData(store, NIFTY, D1, D1))
    return strat, res.trades_df()


def test_upside_fakeout_close_inside_buys_pe_and_hits_2r_target(tmp_path):
    rest = [(23010, 23035, 23005, 23008),                   # sweeps OR high 23015, closes back inside
            (23008, 23010, 22990, 22992), (22992, 22995, 22960, 22965), (22965, 22968, 22940, 22945)]
    strat, trades = run(tmp_path, rest, 22945)
    sig = strat.signals[0]
    assert (sig["level_name"], sig["side"], sig["level"]) == ("OR", "up", 23015)
    assert sig["entry"] == 23008 and sig["extreme"] == 23035
    assert sig["stop"] == 23040 and sig["risk"] == 32
    assert sig["target"] == pytest.approx(23008 - 2 * 32)     # 1:2
    t = trades.iloc[0]
    assert t["type"] == "PE" and t["tag"] == "fb_OR_up" and t["exit_reason"] == "TARGET"
    assert t["strike"] == 23000                                # ATM at entry
    assert t["gross_pnl"] > 0


def test_index_stop_loss(tmp_path):
    rest = [(23010, 23035, 23005, 23008), (23008, 23045, 23005, 23040)]   # rejects, then runs through the stop
    _, trades = run(tmp_path, rest, 23040)
    assert trades.iloc[0]["exit_reason"] == "SL" and trades.iloc[0]["gross_pnl"] < 0


def test_genuine_breakout_is_not_faded(tmp_path):
    rest = [(23010, 23032, 23008, 23030), (23030, 23050, 23028, 23048),
            (23048, 23070, 23045, 23068), (23068, 23090, 23065, 23088)]
    strat, trades = run(tmp_path, rest, 23088)
    assert trades.empty and not strat.signals


PIN = [(23005, 23040, 23003, 23017),                         # pin bar: long upper wick, closes just above level
       (23017, 23024, 23016, 23020), (23020, 23026, 23017, 23022), (23022, 23027, 23018, 23021)]


def test_pin_bar_rejection_only_in_pattern_modes(tmp_path):
    _, pattern = run(tmp_path / "a", PIN, 23021, rejection_mode="pattern")
    _, inside = run(tmp_path / "b", PIN, 23021, rejection_mode="close_inside")
    assert len(pattern) == 1 and pattern.iloc[0]["type"] == "PE"
    assert inside.empty


def test_downside_fakeout_buys_ce(tmp_path):
    rest = [(22990, 22995, 22965, 22992),                   # sweeps OR low 22985, closes back inside
            (22992, 23010, 22990, 23008), (23008, 23040, 23005, 23035), (23035, 23060, 23030, 23058)]
    strat, trades = run(tmp_path, rest, 23058)
    sig = strat.signals[0]
    assert sig["side"] == "down" and sig["stop"] == 22960 and sig["target"] == pytest.approx(22992 + 2 * 32)
    assert trades.iloc[0]["type"] == "CE" and trades.iloc[0]["exit_reason"] == "TARGET"


def test_candle_patterns():
    bar = lambda o, h, l, c: pd.Series({"open": o, "high": h, "low": l, "close": c})
    assert is_pin_bar(bar(100, 120, 98, 101), "up")
    assert not is_pin_bar(bar(100, 120, 98, 118), "up")
    assert is_pin_bar(bar(100, 102, 80, 99), "down")
    assert is_engulfing(bar(100, 106, 99, 105), bar(106, 107, 98, 99), "up")
    assert is_engulfing(bar(105, 106, 99, 100), bar(99, 107, 98, 106), "down")
    assert not is_engulfing(bar(100, 106, 99, 105), bar(104, 105, 101, 102), "up")


# ---- v2: key levels, inside-prior-close rule, diagnostics ---------------------------
PDC = 23001   # DAY0's last close
BELOW = [(22980, 22988, 22972, 22985)] * 6
ABOVE = [(23020, 23028, 23012, 23018)] * 6


def test_pdc_swept_from_below_buys_pe(tmp_path):
    rest = [(22985, 23020, 22982, 22990)]                    # pokes above PDC, closes back below
    store = build_market(tmp_path, {D0: DAY0, D1: fill_day(BELOW + rest, 22990)}, EXP)
    s = make_settings(tmp_path)
    strat = get_strategy("failed_breakout", **{**LOOSE, "levels": "PDC"})
    trades = run_backtest(strat, NIFTY, D1, D1, s, data=HistoricalData(store, NIFTY, D1, D1)).trades_df()
    sig = strat.signals[0]
    assert (sig["level_name"], sig["side"], sig["level"]) == ("PDC", "up", PDC)
    assert trades.iloc[0]["type"] == "PE"


def test_pdc_swept_from_above_buys_ce(tmp_path):
    rest = [(23018, 23022, 22985, 23015)]                    # dips below PDC, closes back above
    store = build_market(tmp_path, {D0: DAY0, D1: fill_day(ABOVE + rest, 23015)}, EXP)
    s = make_settings(tmp_path)
    strat = get_strategy("failed_breakout", **{**LOOSE, "levels": "PDC"})
    trades = run_backtest(strat, NIFTY, D1, D1, s, data=HistoricalData(store, NIFTY, D1, D1)).trades_df()
    assert strat.signals[0]["side"] == "down" and trades.iloc[0]["type"] == "CE"


def test_price_sitting_beyond_level_is_not_a_breakout(tmp_path):
    # the whole day trades above PDC: bars are "beyond" it, but none came from inside
    store = build_market(tmp_path, {D0: DAY0, D1: fill_day(ABOVE, 23018)}, EXP)
    s = make_settings(tmp_path)
    strat = get_strategy("failed_breakout", **{**LOOSE, "levels": "PDC"})
    trades = run_backtest(strat, NIFTY, D1, D1, s, data=HistoricalData(store, NIFTY, D1, D1)).trades_df()
    assert trades.empty and not strat.signals


def test_previous_week():
    from algo.indicators import previous_week
    days = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-13"]
    idx = pd.DatetimeIndex([pd.Timestamp(d) + pd.Timedelta(hours=10) for d in days])
    df = pd.DataFrame({"high": [1, 2, 3, 4, 5], "low": [1, 2, 3, 4, 5]}, index=idx)
    assert list(previous_week(df, date(2025, 1, 7))["high"]) == [1, 2]      # week of 30 Dec
    assert list(previous_week(df, date(2025, 1, 13))["high"]) == [3, 4]     # week of 6 Jan
    assert previous_week(df, date(2025, 1, 2)).empty


def test_diagnostics_one_row_per_trade(tmp_path):
    rest = [(23010, 23035, 23005, 23008), (23008, 23010, 22990, 22992),
            (22992, 22995, 22960, 22965), (22965, 22968, 22940, 22945)]
    strat, trades = run(tmp_path, rest, 22945)
    diag = strat.diagnostics()
    assert len(diag) == len(trades) >= 1
    row = diag.iloc[0]
    assert row["exit_reason"] == "TARGET" and row["index_R"] >= 2.0 and row["mfe_R"] >= 2.0
