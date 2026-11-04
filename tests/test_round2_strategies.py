"""Scenario tests for trend_pullback, squeeze_breakout, pdhl_retest, vwap_structure and orb_momentum."""
from datetime import date

import pandas as pd
import pytest

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.strategies import get_strategy

from .conftest import NIFTY, make_settings
from .helpers import build_market, fill_day, flat

D0, D1, EXP = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 9)


def run(tmp_path, name, day1, last, day0=None, **params):
    store = build_market(tmp_path, {D0: day0 or flat(23000, 75), D1: fill_day(day1, last)}, EXP)
    strat = get_strategy(name, size_from_capital=False, **{"max_trades_per_day": 1, **params})
    res = run_backtest(strat, NIFTY, D1, D1, make_settings(tmp_path), data=HistoricalData(store, NIFTY, D1, D1))
    return strat, res.trades_df()


def mirror(candles, pivot=23000):
    """Reflect (o, h, l, c) candles around `pivot`: an up move becomes a down move."""
    return [(2 * pivot - o, 2 * pivot - l, 2 * pivot - h, 2 * pivot - c) for o, h, l, c in candles]


def path(closes, wick=2):
    """Candles from a close path: open = previous close."""
    out, prev = [], closes[0]
    for c in closes:
        out.append((prev, max(prev, c) + wick, min(prev, c) - wick, c))
        prev = c
    return out


def check_entry(strat, t, kind, tag, at, stop_level):
    assert len(t) == 1
    tr, d = t.iloc[0], strat.diagnostics().iloc[0]
    assert tr["type"] == kind and tr["tag"] == tag
    assert pd.Timestamp(tr["entry_time"]) == pd.Timestamp(f"2025-01-07 {at}")      # next minute: no lookahead
    sign = 1 if kind == "CE" else -1
    assert d["init_stop"] == pytest.approx(stop_level - sign * 0.15 * d["atr"])
    assert d["target"] == pytest.approx(d["entry"] + sign * 2 * d["risk"])
    return tr, d


# ---------------------------------------------------------------- trend_pullback
QUIET = flat(23000, 6)                                                            # 09:15-09:45
IMPULSE = [(23000 + 18 * i, 23020 + 18 * i, 22998 + 18 * i, 23018 + 18 * i) for i in range(5)]
PULLBACK = [(23090, 23094, 23060, 23066), (23066, 23070, 23056, 23062)]
TOUCH_CONFIRM = [(23062, 23100, 23060, 23098)]       # touches EMA9 and closes above the prior high
RUN = [(23098, 23140, 23096, 23138), (23138, 23190, 23136, 23188), (23188, 23240, 23186, 23238)]


def test_trend_pullback_long(tmp_path):
    strat, t = run(tmp_path, "trend_pullback", QUIET + IMPULSE + PULLBACK + TOUCH_CONFIRM + RUN, 23238)
    tr, d = check_entry(strat, t, "CE", "tpb_up", "10:25", 23056)     # pullback low
    assert tr["exit_reason"] == "TARGET"


def test_trend_pullback_short_mirror(tmp_path):
    day = mirror(QUIET + IMPULSE + PULLBACK + TOUCH_CONFIRM + RUN)
    strat, t = run(tmp_path, "trend_pullback", day, 22762)
    check_entry(strat, t, "PE", "tpb_down", "10:25", 23000 * 2 - 23056)


def test_trend_pullback_needs_an_impulse(tmp_path):
    slow = [(23000 + 3 * i, 23006 + 3 * i, 22998 + 3 * i, 23003 + 3 * i) for i in range(5)]
    _, t = run(tmp_path, "trend_pullback", QUIET + slow + PULLBACK, 23062)
    assert t.empty


# ---------------------------------------------------------------- squeeze_breakout
WAVY = path([23000 + 40 * ((i % 10) - 5 if (i // 10) % 2 == 0 else 5 - (i % 10)) for i in range(75)], wick=6)
TIGHT = [(23000, 23003, 22997, 23000 + (1 if i % 2 else -1)) for i in range(14)]    # 09:15-10:20
BREAK_UP = [(23001, 23040, 23000, 23038)]
SQ = {"bw_lookback": 60, "entry_start": "09:15"}   # one prior day of history in the test market
AFTER = [(23038, 23060, 23036, 23058), (23058, 23080, 23056, 23078)]


def test_squeeze_breakout_long(tmp_path):
    strat, t = run(tmp_path, "squeeze_breakout", TIGHT + BREAK_UP + AFTER, 23078, day0=WAVY, **SQ)
    _, d = check_entry(strat, t, "CE", "sqz_up", "10:30", 22997)       # opposite side of the box
    assert d["box_hi"] == 23003 and d["box_n"] >= 6


def test_squeeze_breakout_mid_stop_and_short(tmp_path):
    day = TIGHT + mirror(BREAK_UP + AFTER)
    strat, t = run(tmp_path, "squeeze_breakout", day, 22922, day0=WAVY, stop_at="mid", **SQ)
    check_entry(strat, t, "PE", "sqz_down", "10:30", 23000)


def test_squeeze_breakout_needs_a_squeeze(tmp_path):
    _, t = run(tmp_path, "squeeze_breakout", WAVY[:20], 23000, day0=WAVY, **SQ)
    assert t.empty


# ---------------------------------------------------------------- pdhl_retest   (previous day: 22996-23004)
OPEN = [(23000, 23003, 22997, 23000)] * 3                                         # 09:15-09:30
BREAK = [(23000, 23032, 22999, 23030), (23030, 23046, 23028, 23044)]               # close above PDH 23004
RETEST = [(23044, 23046, 23005, 23020)]                                           # back to the level, holds
CONFIRM = [(23020, 23050, 23018, 23048)]
GO = [(23048, 23080, 23046, 23078), (23078, 23110, 23076, 23108)]


def test_pdhl_retest_long(tmp_path):
    strat, t = run(tmp_path, "pdhl_retest", OPEN + BREAK + RETEST + CONFIRM + GO, 23108, confirm_mode="break_high", max_risk_atr=5)
    _, d = check_entry(strat, t, "CE", "pdr_up", "09:50", 23005)      # retest low
    assert d["level"] == 23004


def test_pdhl_retest_short_mirror(tmp_path):
    day = mirror(OPEN + BREAK + RETEST + CONFIRM + GO)
    strat, t = run(tmp_path, "pdhl_retest", day, 22892, confirm_mode="break_high", max_risk_atr=5)
    check_entry(strat, t, "PE", "pdr_down", "09:50", 22995)


def test_pdhl_breakout_that_fails_back_inside_is_skipped(tmp_path):
    fail = [(23044, 23046, 22960, 22965)]
    strat, t = run(tmp_path, "pdhl_retest", OPEN + BREAK + fail + RETEST, 23000)
    assert t.empty and strat.skips["failed_back_inside"] >= 1


# ---------------------------------------------------------------- vwap_structure
# zigzag up: H 23040, L 23020, H 23070, L 23045 (HH + HL), then continuation
ZIG = path([23000, 23010, 23020, 23030, 23040, 23032, 23026, 23020, 23035, 23050, 23060, 23070,
            23062, 23054, 23045, 23055, 23068, 23080, 23092, 23104, 23116], wick=0)


def test_vwap_structure_long_on_confirmed_higher_low(tmp_path):
    strat, t = run(tmp_path, "vwap_structure", ZIG, 23116, entry_start="09:15")
    # the HL at 23045 (10:25 bar) is confirmed 2 bars later, at the 10:35 close
    _, d = check_entry(strat, t, "CE", "vms_up", "10:40", 23045)
    assert d["pivot"] == 23045


def test_vwap_structure_short_mirror(tmp_path):
    strat, t = run(tmp_path, "vwap_structure", mirror(ZIG), 22884, entry_start="09:15")
    check_entry(strat, t, "PE", "vms_down", "10:40", 22955)


def test_vwap_structure_flat_day_no_trade(tmp_path):
    _, t = run(tmp_path, "vwap_structure", flat(23000, 30, wiggle=6), 23000, entry_start="09:15")
    assert t.empty


# ---------------------------------------------------------------- orb_momentum
TREND_DAY0 = path([22900 + 2 * i for i in range(75)], wick=10)                   # daily range ~170 pts
ORB = [(23000, 23015, 22990, 23002), (23002, 23008, 22985, 23000), (23000, 23009, 22991, 23004)]   # 22985-23015
MOM = [(23004, 23042, 23003, 23040)]                                            # 09:30 breakout, big body
FOLLOW = [(23040, 23070, 23038, 23068), (23068, 23095, 23066, 23094)]


def test_orb_momentum_long_on_breakout_bar(tmp_path):
    strat, t = run(tmp_path, "orb_momentum", ORB + MOM + FOLLOW, 23094, day0=TREND_DAY0, rsi_min=55)
    _, d = check_entry(strat, t, "CE", "orm_up", "09:35", 23000)      # OR middle (22985 + 23015) / 2
    assert d["or_hi"] == 23015


def test_orb_momentum_weak_breakout_skipped(tmp_path):
    weak = [(23010, 23020, 23008, 23018)]                                     # closes above the OR, small body
    strat, t = run(tmp_path, "orb_momentum", ORB + weak, 23018, day0=TREND_DAY0, rsi_min=55)
    assert t.empty and strat.skips["weak_body"] >= 1


def test_orb_momentum_skips_too_wide_range(tmp_path):
    strat, t = run(tmp_path, "orb_momentum", ORB + MOM + FOLLOW, 23094, day0=TREND_DAY0, or_max_atr=0.05)
    assert t.empty and strat.skips["or_width"] == 1


# ---------------------------------------------------------------- params
@pytest.mark.parametrize("name,bad", [("squeeze_breakout", {"stop_at": "x"}), ("pdhl_retest", {"confirm_mode": "x"}),
                                      ("vwap_structure", {"rejection": "x"}), ("orb_momentum", {"stop_at": "x"}),
                                      ("trend_pullback", {"nope": 1})])
def test_bad_params(name, bad):
    with pytest.raises(ValueError):
        get_strategy(name, **bad)
