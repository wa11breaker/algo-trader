import numpy as np
import pandas as pd
import pytest

from algo.forex.data import resample_server, to_server
from algo.forex.engulfing import GoldEngulfingParams, engulfing_masks, signals
from algo.forex.metrics import summarize
from algo.forex.sim import CostParams, simulate, swap_nights
from algo.indicators import is_engulfing

# 2024-01-08 22:00 UTC = Mon 17:00 New York = Tue 00:00 server (winter, UTC+2)
T0 = pd.Timestamp("2024-01-08 22:00")


def minutes(n_bars: int, price: float = 100.0, start=T0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n_bars * 240, freq="1min")
    return pd.DataFrame({"open": price, "high": price, "low": price, "close": price, "volume": 1.0}, index=idx)


def set_min(m1, bar, minute, o=None, h=None, l=None, c=None):
    i = bar * 240 + minute
    for col, v in (("open", o), ("high", h), ("low", l), ("close", c)):
        if v is not None:
            m1.iloc[i, m1.columns.get_loc(col)] = v


def manual_sig(h4, rows: dict) -> pd.DataFrame:
    sig = pd.DataFrame({"side": 0, "stop": np.nan, "atr": 0.0}, index=h4.index)
    for j, (side, stop) in rows.items():
        sig.iloc[j, 0], sig.iloc[j, 1] = side, stop
    return sig


P = GoldEngulfingParams()
Z = CostParams.zero()


# ---------- server-time bars ----------

def test_server_time_winter_and_summer():
    assert to_server(pd.DatetimeIndex(["2024-01-08 22:00"]))[0] == pd.Timestamp("2024-01-09 00:00")
    assert to_server(pd.DatetimeIndex(["2024-07-01 21:00"]))[0] == pd.Timestamp("2024-07-02 00:00")


def test_four_hour_buckets_follow_dst():
    # US DST starts 2024-03-10; the server day boundary moves from 22:00 to 21:00 UTC
    idx = pd.date_range("2024-03-08 20:00", "2024-03-12 00:00", freq="1min")
    idx = idx[idx.day_of_week < 5]
    m1 = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    h4 = resample_server(m1, 4)
    assert (h4.index.hour % 4 == 0).all()
    starts = set(h4["first_utc"].dt.strftime("%Y-%m-%d %H:%M"))
    assert "2024-03-08 20:00" in starts            # Fri 22:00 server bar, winter
    assert "2024-03-11 21:00" in starts            # Tue 00:00 server, summer
    assert (h4["i1"] - h4["i0"]).sum() == len(m1)


# ---------- signals ----------

def test_vectorised_engulfing_matches_helper():
    rng = np.random.default_rng(0)
    o = 100 + rng.normal(0, 1, 500).cumsum()
    c = o + rng.normal(0, 1, 500)
    o[1::7] = c[0:-1:7]                      # force some open == prev close cases
    h4 = pd.DataFrame({"open": o, "close": c, "high": np.maximum(o, c) + 0.5, "low": np.minimum(o, c) - 0.5})
    bull, bear = engulfing_masks(h4)
    assert bull.sum() > 10 and bear.sum() > 10
    for i in range(1, len(h4)):
        assert bull.iat[i] == is_engulfing(h4.iloc[i - 1], h4.iloc[i], "down")
        assert bear.iat[i] == is_engulfing(h4.iloc[i - 1], h4.iloc[i], "up")


def _trend_bars(n, up=True):
    base = np.arange(n, dtype=float) * (1 if up else -1) + 1000
    return pd.DataFrame({"open": base - 0.5 * (1 if up else -1), "close": base,
                         "high": base + 1, "low": base - 1})


def test_long_signal_rules_and_stop():
    h4 = _trend_bars(30)
    last = h4["close"].iat[-1]
    red = {"open": last + 2, "close": last + 0.5, "high": last + 2.5, "low": last}
    green = {"open": last + 0.4, "close": last + 3, "high": last + 3.2, "low": last + 0.1}
    h4 = pd.concat([h4, pd.DataFrame([red, green])], ignore_index=True)
    sig = signals(h4, GoldEngulfingParams(trend_sma=20, fast_sma=5))
    assert sig["side"].iat[-1] == 1
    assert sig["stop"].iat[-1] == pytest.approx(last + 0.1)
    # the same bullish pattern in a downtrend (below the SMAs) is filtered out
    dn = _trend_bars(30, up=False)
    l2 = dn["close"].iat[-1]
    dn = pd.concat([dn, pd.DataFrame([{"open": l2 + 2, "close": l2 + 0.5, "high": l2 + 2.5, "low": l2},
                                      {"open": l2 + 0.4, "close": l2 + 3, "high": l2 + 3.2, "low": l2 + 0.1}])],
                   ignore_index=True)
    assert signals(dn, GoldEngulfingParams(trend_sma=20, fast_sma=5))["side"].iat[-1] == 0


def test_short_signal_requires_downtrend():
    h4 = _trend_bars(30, up=False)
    last = h4["close"].iat[-1]
    green = {"open": last - 2, "close": last - 0.5, "high": last, "low": last - 2.5}
    red = {"open": last - 0.4, "close": last - 3, "high": last - 0.1, "low": last - 3.2}
    h4 = pd.concat([h4, pd.DataFrame([green, red])], ignore_index=True)
    sig = signals(h4, GoldEngulfingParams(trend_sma=20, fast_sma=5))
    assert sig["side"].iat[-1] == -1
    assert sig["stop"].iat[-1] == pytest.approx(last - 0.1)
    up = _trend_bars(30)
    l2 = up["close"].iat[-1]
    up = pd.concat([up, pd.DataFrame([{"open": l2 - 2, "close": l2 - 0.5, "high": l2, "low": l2 - 2.5},
                                      {"open": l2 - 0.4, "close": l2 - 3, "high": l2 - 0.1, "low": l2 - 3.2}])],
                   ignore_index=True)
    assert signals(up, GoldEngulfingParams(trend_sma=20, fast_sma=5))["side"].iat[-1] == 0


def test_signals_have_no_lookahead():
    rng = np.random.default_rng(1)
    o = 100 + rng.normal(0, 1, 400).cumsum()
    c = o + rng.normal(0, 1, 400)
    h4 = pd.DataFrame({"open": o, "close": c, "high": np.maximum(o, c) + 0.3, "low": np.minimum(o, c) - 0.3})
    full = signals(h4, GoldEngulfingParams(trend_sma=50, fast_sma=14))
    part = signals(h4.iloc[:300], GoldEngulfingParams(trend_sma=50, fast_sma=14))
    pd.testing.assert_frame_equal(full.iloc[:300], part)


# ---------- simulator ----------

def test_long_hits_target_at_2r_entered_next_bar_open():
    m1 = minutes(4)
    set_min(m1, 2, 30, h=102.5)
    h4 = resample_server(m1)
    res = simulate(m1, h4, manual_sig(h4, {0: (1, 99.0)}), P, Z)
    t = res.trades.iloc[0]
    assert t["entry_utc"] == h4["first_utc"].iat[1]          # next bar, first minute
    assert t["lots"] == 10.0                                   # $1000 / ($1 x 100 oz)
    assert t["reason"] == "target" and t["exit"] == 102.0
    assert t["net"] == pytest.approx(2000.0) and t["R"] == pytest.approx(2.0)


def test_stop_wins_when_both_levels_in_one_minute():
    m1 = minutes(4)
    set_min(m1, 1, 10, h=103, l=98)
    h4 = resample_server(m1)
    t = simulate(m1, h4, manual_sig(h4, {0: (1, 99.0)}), P, Z).trades.iloc[0]
    assert t["reason"] == "stop" and t["exit"] == 99.0 and t["net"] == pytest.approx(-1000.0)


def test_gap_through_stop_fills_at_open():
    m1 = minutes(4)
    set_min(m1, 2, 0, o=97, h=97, l=97, c=97)
    h4 = resample_server(m1)
    t = simulate(m1, h4, manual_sig(h4, {0: (1, 99.0)}), P, Z).trades.iloc[0]
    assert t["reason"] == "stop" and t["exit"] == 97.0 and t["R"] == pytest.approx(-3.0)


def test_short_uses_ask_for_entry_exit_and_triggers():
    costs = CostParams(spread=0.3, commission_per_lot=0, swap_long=0, swap_short=0)
    m1 = minutes(4)
    set_min(m1, 2, 5, h=100.8)          # ask high 101.1 >= stop 101 -> stopped
    h4 = resample_server(m1)
    t = simulate(m1, h4, manual_sig(h4, {0: (-1, 101.0)}), P, costs).trades.iloc[0]
    assert t["entry"] == 100.0           # sold at bid
    assert t["lots"] == 10.0             # risk = stop 101 - entry 100 = $1/oz
    assert t["reason"] == "stop" and t["exit"] == 101.0


def test_opposite_signal_closes_and_reverses():
    m1 = minutes(5)
    h4 = resample_server(m1)
    res = simulate(m1, h4, manual_sig(h4, {0: (1, 99.0), 2: (-1, 101.0)}), P, Z)
    t = res.trades
    assert list(t["reason"]) == ["opposite", "end_of_data"]
    assert list(t["side"]) == [1, -1]
    assert t["exit_utc"].iat[0] == t["entry_utc"].iat[1] == h4["first_utc"].iat[3]


def test_stop_too_close_vs_atr_is_skipped():
    m1 = minutes(3)
    h4 = resample_server(m1)
    sig = manual_sig(h4, {0: (1, 99.0)})
    sig["atr"] = 10.0                     # 0.25 x 10 = 2.5 > 1.0 stop distance
    res = simulate(m1, h4, sig, P, Z)
    assert res.trades.empty and res.skips["stop_too_close"] == 1


def test_lot_rounding_spread_and_commission():
    costs = CostParams(spread=0.3, commission_per_lot=5.0, swap_long=0, swap_short=0)
    m1 = minutes(4)
    set_min(m1, 2, 0, l=98.9)
    h4 = resample_server(m1)
    t = simulate(m1, h4, manual_sig(h4, {0: (1, 99.0)}), P, costs).trades.iloc[0]
    assert t["entry"] == pytest.approx(100.3)
    assert t["lots"] == pytest.approx(7.69)              # floor(1000 / 130, 0.01)
    assert t["commission"] == pytest.approx(7.69 * 5)
    assert t["net"] == pytest.approx(-1.3 * 7.69 * 100 - 7.69 * 5)


def test_swap_nights_triple_wednesday_and_weekend():
    tue = pd.Timestamp("2024-01-09 10:00")   # server time
    assert swap_nights(tue, pd.Timestamp("2024-01-09 20:00")) == 0
    assert swap_nights(tue, pd.Timestamp("2024-01-11 10:00")) == 4      # Tue 1 + Wed 3
    assert swap_nights(pd.Timestamp("2024-01-12 10:00"), pd.Timestamp("2024-01-15 10:00")) == 1  # Fri only


def test_summary_counts():
    m1 = minutes(4)
    set_min(m1, 2, 30, h=102.5)
    h4 = resample_server(m1)
    s = summarize(simulate(m1, h4, manual_sig(h4, {0: (1, 99.0)}), P, Z))
    assert s["trades"] == 1 and s["win_rate_pct"] == 100.0 and s["return_pct"] == 2.0


def test_breakeven_moves_stop_to_entry():
    m1 = minutes(4)
    set_min(m1, 1, 10, h=101.2)          # +1.2R -> stop to entry 100
    set_min(m1, 1, 20, l=99.9)           # back below entry -> breakeven exit
    h4 = resample_server(m1)
    from dataclasses import replace
    t = simulate(m1, h4, manual_sig(h4, {0: (1, 99.0)}), replace(P, breakeven_r=1.0), Z).trades.iloc[0]
    assert t["reason"] == "breakeven" and t["exit"] == 100.0 and t["net"] == pytest.approx(0.0)


def test_time_exit_after_max_bars():
    m1 = minutes(6)
    set_min(m1, 3, 0, o=100.5, h=100.5, l=100.5, c=100.5)
    h4 = resample_server(m1)
    from dataclasses import replace
    t = simulate(m1, h4, manual_sig(h4, {0: (1, 99.0)}), replace(P, max_bars=2), Z).trades.iloc[0]
    assert t["reason"] == "time" and t["exit_utc"] == h4["first_utc"].iat[3] and t["exit"] == 100.5


def test_max_stop_atr_skips_wide_stops():
    m1 = minutes(3)
    h4 = resample_server(m1)
    sig = manual_sig(h4, {0: (1, 90.0)})
    sig["atr"] = 4.0                       # stop distance 10 > 2 x 4
    from dataclasses import replace
    res = simulate(m1, h4, sig, replace(P, max_stop_atr=2.0), Z)
    assert res.trades.empty and res.skips["stop_too_wide"] == 1
