from datetime import date

import pandas as pd

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.strategies import get_strategy

from .conftest import NIFTY, make_settings
from .helpers import build_market, flat, smooth_price

D0, D1, D2, EXP = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 9)


def _run(tmp_path, name, **params):
    days = {d: flat(23000, 75) for d in (D0, D1, D2)}
    store = build_market(tmp_path, days, EXP, pricer=smooth_price)
    strat = get_strategy(name, size_from_capital=False, min_credit=0.0, **params)
    res = run_backtest(strat, NIFTY, D0, D2, make_settings(tmp_path), data=HistoricalData(store, NIFTY, D0, D2))
    return strat, res.trades_df()


def test_condor_sells_otm_and_buys_further_wing_closed_same_day(tmp_path):
    strat, t = _run(tmp_path, "short_premium", mode="condor", entry_time="09:30", dte_max=10)
    assert t.group.nunique() == 6                      # CE + PE spread on each of 3 days
    for _, g in t.groupby("group"):
        assert sorted(g["side"]) == ["BUY", "SELL"]
        sold, wing = g[g.side == "SELL"].iloc[0], g[g.side == "BUY"].iloc[0]
        assert sold["type"] == wing["type"]
        assert sold["strike"] == (23100 if sold["type"] == "CE" else 22900)       # 2 steps OTM
        assert wing["strike"] == (23200 if sold["type"] == "CE" else 22800)       # 2 steps further
    assert (pd.to_datetime(t.entry_time).dt.date == pd.to_datetime(t.exit_time).dt.date).all()
    assert len(strat.diagnostics()) == 6


def test_overnight_holds_to_next_session(tmp_path):
    _, t = _run(tmp_path, "overnight_credit", entry_time="15:00", exit_time="09:30", stop_mult=100.0)
    held = t[t.exit_reason == "SIGNAL"]
    assert held.group.nunique() == 4                   # D0->D1 and D1->D2, CE + PE
    entry, exit_ = pd.to_datetime(held.entry_time), pd.to_datetime(held.exit_time)
    assert ((exit_.dt.normalize() - entry.dt.normalize()).dt.days == 1).all()
    assert (exit_.dt.strftime("%H:%M") >= "09:30").all()
