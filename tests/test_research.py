from datetime import date, time

import pandas as pd
import pytest

from algo.analytics.metrics import mean_ci
from algo.research.index_replay import replay, run_index_only
from algo.research.sweep import load_grid, parse_period

from .conftest import make_settings
from .helpers import save_market, smooth_price
from .test_fvg_flow import D0, D1, EXP, GAP, LEGACY, NO_TOUCH, RALLY, RETEST, bars as fvg_bars, day, quiet

DAY = pd.Timestamp("2025-01-07")


def bars(rows, start="09:16"):
    idx = pd.date_range(f"{DAY.date()} {start}", periods=len(rows), freq="1min")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def plan(**kw):
    p = {"now": DAY + pd.Timedelta(hours=9, minutes=15), "tag": "t", "dir": 1, "entry": 100.0,
         "stop": 90.0, "target": 120.0, "risk": 10.0, "trail_dist": None}
    return {**p, **kw}


def test_replay_stop_target_time_and_pessimism():
    up = bars([(100, 110, 99, 109), (109, 121, 108, 120)])
    assert replay([plan()], up, time(15, 0)).iloc[0][["why", "R"]].tolist() == ["target", 2.0]
    both = bars([(100, 125, 85, 100)])                              # stop and target in one bar: stop
    assert replay([plan()], both, time(15, 0)).iloc[0]["why"] == "stop"
    late = bars([(100, 101, 99, 100), (104, 105, 103, 104)], start="14:58")
    t = replay([plan(now=DAY + pd.Timedelta(hours=14, minutes=57))], late, time(14, 59)).iloc[0]
    assert t["why"] == "time" and t["R"] == pytest.approx(0.4)


def test_replay_breakeven_and_one_position_at_a_time():
    path = bars([(100, 111, 100, 110), (110, 110, 99, 99), (99, 100, 85, 86)])
    t = replay([plan(), plan(now=DAY + pd.Timedelta(hours=9, minutes=16))], path, time(15, 0), breakeven_at_r=1.0)
    assert len(t) == 1                                             # second plan overlaps the first trade
    assert t.iloc[0]["why"] == "breakeven" and t.iloc[0]["R"] == 0.0


def test_index_only_run_needs_no_options(tmp_path):
    spot = pd.concat([fvg_bars(D0, time(9, 15), quiet(23000, 375, 4)), day(GAP + NO_TOUCH + RETEST + RALLY, 23040)])
    save_market(tmp_path, spot, EXP, pricer=smooth_price)
    trades, strat = run_index_only("fvg_flow", LEGACY, "NIFTY", D1, D1, make_settings(tmp_path))
    assert len(strat.plan_log) == 1 and strat.skips["no_option"] == 1     # options hidden
    assert trades.iloc[0]["why"] == "target" and trades.iloc[0]["R"] == pytest.approx(2.0)


def test_grid_formats(tmp_path):
    (tmp_path / "a.yaml").write_text("risk_reward: [1, 1.5]\nsl_atr_mult: [1.5, 2]\ntp_type: rr\n")
    g = load_grid(tmp_path / "a.yaml")
    assert len(g) == 4 and all(x["tp_type"] == "rr" for x in g)
    (tmp_path / "b.json").write_text('[{"x": 1}, {"x": 2, "y": "a"}]')
    assert load_grid(tmp_path / "b.json") == [{"x": 1}, {"x": 2, "y": "a"}]
    assert parse_period("2022-01-01:2024-09-30") == (date(2022, 1, 1), date(2024, 9, 30))


def test_mean_ci():
    noise = mean_ci([1.0, -1.0] * 50)
    assert noise["mean"] == 0 and noise["ci95"][0] < 0 < noise["ci95"][1] and noise["t_stat"] == 0
    edge = mean_ci([1.0] * 70 + [-1.0] * 30)
    assert edge["ci95"][0] > 0 and edge["t_stat"] > 4
