from datetime import date

import pytest

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.strategies import get_strategy

from .conftest import NIFTY, make_settings
from .helpers import build_market, fill_day, flat

D0, D1, EXP = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 9)
DAY0 = fill_day([], 23000)

# Box 09:15-10:15 = 12 candles. The last three leave a bullish FVG [23004, 23012]; box high 23025, low 22996.
BOX_BULL_FVG = flat(23000, 9) + [(23000, 23004, 22996, 23003), (23003, 23020, 23002, 23018),
                                 (23018, 23025, 23012, 23022)]
# Mirror: bearish FVG [22988, 22996]; box high 23004, low 22975.
BOX_BEAR_FVG = flat(23000, 9) + [(23000, 23004, 22996, 22997), (22997, 22998, 22980, 22982),
                                 (22982, 22988, 22975, 22978)]

SWEEP_HIGH = [(23022, 23035, 23020, 23032),       # 10:15 closes above the box high
              (23032, 23036, 23010, 23012)]       # 10:20 large red bar back inside = bearish LS (ls_price 23036)
SHORT = SWEEP_HIGH + [(23012, 23014, 23000, 23001),   # 10:25 closes below the bullish FVG -> bearish IFVG
                      (23001, 23002, 22950, 22955), (22955, 22958, 22920, 22925), (22925, 22930, 22900, 22905)]
LONG = [(22978, 22980, 22965, 22968),              # 10:15 closes below the box low
        (22968, 22990, 22964, 22988),              # 10:20 large green bar back inside = bullish LS
        (22988, 23000, 22986, 22999),              # 10:25 closes above the bearish FVG -> bullish IFVG
        (22999, 23050, 22998, 23045), (23045, 23080, 23042, 23075), (23075, 23100, 23070, 23095)]


def run(tmp_path, day1, **params):
    store = build_market(tmp_path, {D0: DAY0, D1: fill_day(day1, day1[-1][3])}, EXP)
    strat = get_strategy("london_liquidity_ifvg", **{"size_from_capital": False, **params})
    res = run_backtest(strat, NIFTY, D1, D1, make_settings(tmp_path), data=HistoricalData(store, NIFTY, D1, D1))
    return strat, res.trades_df()


def test_high_sweep_then_bearish_ifvg_buys_pe(tmp_path):
    strat, trades = run(tmp_path, BOX_BULL_FVG + SHORT)
    assert len(strat.signals) == 1
    sig = strat.signals[0]
    assert sig["side"] == "short" and sig["time"] == "10:29"
    assert (sig["box_high"], sig["box_low"], sig["ls_price"]) == (23025, 22996, 23036)
    assert (sig["fvg_lower"], sig["fvg_upper"]) == (23004, 23012)
    assert sig["entry"] == 23001
    assert sig["stop"] == pytest.approx(23001 + 1.5 * sig["atr"])
    assert sig["target"] == pytest.approx(23001 - 1.5 * sig["atr"])
    t = trades.iloc[0]
    assert len(trades) == 1 and t["type"] == "PE" and t["tag"] == "ifvg_short" and t["exit_reason"] == "TARGET"
    assert t["gross_pnl"] > 0
    assert strat.diagnostics().iloc[0]["why"] == "target"


def test_low_sweep_then_bullish_ifvg_buys_ce(tmp_path):
    strat, trades = run(tmp_path, BOX_BEAR_FVG + LONG)
    sig = strat.signals[0]
    assert sig["side"] == "long" and sig["ls_price"] == 22964 and sig["entry"] == 22999
    assert (sig["fvg_lower"], sig["fvg_upper"]) == (22988, 22996)
    t = trades.iloc[0]
    assert t["type"] == "CE" and t["exit_reason"] == "TARGET" and t["gross_pnl"] > 0


def test_inversion_without_sweep_is_ignored(tmp_path):
    # drops through the bullish FVG but never closes beyond the box
    strat, trades = run(tmp_path, BOX_BULL_FVG + [(23022, 23024, 23000, 23001), (23001, 23003, 22998, 23000)])
    assert trades.empty and not strat.signals


def test_inversion_against_the_sweep_is_ignored(tmp_path):
    # bearish LS, then a bearish FVG [23013, 23020] is closed through UPWARDS (bullish IFVG): wrong direction
    rest = SWEEP_HIGH + [(23012, 23013, 23006, 23008), (23008, 23030, 23007, 23028)] + flat(23028, 6)
    strat, trades = run(tmp_path, BOX_BULL_FVG + rest)
    assert trades.empty and not strat.signals


def test_one_setup_per_day(tmp_path):
    # after the first trade, a fresh bullish FVG [22995, 22999] forms and is closed below again
    again = [(22905, 22908, 22900, 22904), (22904, 22995, 22903, 22990), (22990, 22994, 22988, 22992),
             (22992, 22995, 22990, 22994), (22994, 23010, 22993, 23008), (23008, 23020, 22999, 23018),
             (23018, 23019, 22985, 22987)]
    strat, trades = run(tmp_path, BOX_BULL_FVG + SHORT + again)
    assert len(strat.signals) == 1 and len(trades) == 1


def test_range_target_and_swing_stop(tmp_path):
    strat, _ = run(tmp_path, BOX_BULL_FVG + SHORT, tp_type="range", sl_type="swing")
    sig = strat.signals[0]
    assert sig["target"] == 22996          # opposite box level (box low)
    assert sig["stop"] == 23036            # highest high of the last 5 bars


def test_bad_params():
    with pytest.raises(ValueError):
        get_strategy("london_liquidity_ifvg", sl_type="x")
    with pytest.raises(ValueError):
        get_strategy("london_liquidity_ifvg", tp_type="x")


def test_ls_stop_is_the_sweep_extreme(tmp_path):
    strat, _ = run(tmp_path, BOX_BULL_FVG + SHORT, sl_type="ls", sl_buffer_pts=5)
    assert strat.signals[0]["stop"] == 23036 + 5


def test_stale_sweep_is_dropped(tmp_path):
    strat, trades = run(tmp_path, BOX_BULL_FVG + SHORT, max_bars_after_ls=0)
    assert trades.empty and not strat.signals and strat.skips["stale_sweep"] == 1
    strat, trades = run(tmp_path / "b", BOX_BULL_FVG + SHORT, max_bars_after_ls=1)
    assert len(strat.signals) == 1


def test_after_ls_ignores_box_fvgs(tmp_path):
    strat, trades = run(tmp_path, BOX_BULL_FVG + SHORT, fvg_source="after_ls")
    assert trades.empty and not strat.signals


def test_entry_start_and_risk_pts_filters(tmp_path):
    strat, _ = run(tmp_path, BOX_BULL_FVG + SHORT, entry_start="11:00")
    assert strat.signals[0]["time"] >= "11:00"          # still below the FVG at 11:00, so it enters then
    strat, trades = run(tmp_path / "b", BOX_BULL_FVG + SHORT, max_risk_pts=5)
    assert trades.empty and strat.skips["risk_pts"] == 1


def test_previous_day_levels(tmp_path):
    day0 = fill_day([(23000, 23040, 22960, 23000)], 23000)          # PDH 23040, PDL 22960
    day1 = flat(23000, 3) + [(23000, 23004, 22996, 23003), (23003, 23020, 23002, 23018),
                             (23018, 23030, 23012, 23028),           # bullish FVGs [23004, 23012] and (with the next bar) [23020, 23026]
                             (23028, 23045, 23026, 23043),           # 09:45 closes above PDH
                             (23043, 23047, 23020, 23022),           # 09:50 large red back inside = LS (23047)
                             (23022, 23024, 23000, 23001),           # 09:55 closes below the FVG -> PE
                             (23001, 23002, 22950, 22955), (22955, 22958, 22920, 22925)]
    store = build_market(tmp_path, {D0: day0, D1: fill_day(day1, 22925)}, EXP)
    strat = get_strategy("london_liquidity_ifvg", levels="pdhl", sweep_start="09:45", size_from_capital=False)
    trades = run_backtest(strat, NIFTY, D1, D1, make_settings(tmp_path),
                          data=HistoricalData(store, NIFTY, D1, D1)).trades_df()
    sig = strat.signals[0]
    assert (sig["box_high"], sig["box_low"], sig["ls_price"], sig["side"]) == (23040, 22960, 23047, "short")
    assert (sig["fvg_lower"], sig["fvg_upper"]) == (23020, 23026) and sig["time"] == "09:59"   # newest gap first
    assert trades.iloc[0]["type"] == "PE"
    with pytest.raises(ValueError):
        get_strategy("london_liquidity_ifvg", levels="x")


def test_rejection_confirmation(tmp_path):
    # the SHORT sweep-back bar is a bearish engulfing of the breakout bar -> passes "rejection"
    strat, _ = run(tmp_path, BOX_BULL_FVG + SHORT, ls_confirm="rejection")
    assert len(strat.signals) == 1
    # a plain close back inside with a small body and no rejection shape -> no sweep
    weak = [(23022, 23035, 23020, 23032), (23032, 23034, 23018, 23024),
            (23024, 23026, 23000, 23001)] + [(23001, 23001, 23000, 23000.5)] * 40   # no wicks until 13:30
    strat, _ = run(tmp_path / "b", BOX_BULL_FVG + weak, ls_confirm="rejection")
    assert not strat.signals


def test_sweep_depth_gap_size_and_entry_body_filters(tmp_path):
    strat, _ = run(tmp_path / "a", BOX_BULL_FVG + SHORT, min_sweep_atr=10)
    assert not strat.signals
    strat, _ = run(tmp_path / "b", BOX_BULL_FVG + SHORT, min_fvg_atr=10)
    assert not strat.signals
    strat, _ = run(tmp_path / "c", BOX_BULL_FVG + SHORT, entry_body_pct=90)   # entry bars are 78-88% bodies
    assert not strat.signals
    strat, _ = run(tmp_path / "d", BOX_BULL_FVG + SHORT, entry_body_pct=70)
    assert strat.signals[0]["time"] == "10:29" and strat.signals[0]["valid"]
