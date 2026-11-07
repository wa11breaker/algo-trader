from datetime import date

import pandas as pd

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.core.risk import parse_weekdays

from .helpers import OrbBuy
from .conftest import NIFTY, make_settings


def test_parse_weekdays():
    assert parse_weekdays(["fri"]) == {4}
    assert parse_weekdays("mon,Friday") == {0, 4}
    assert parse_weekdays([4]) == {4}
    assert parse_weekdays("") == set() and parse_weekdays(None) == set()


def _entry_days(synthetic, calendar, use_risk=False):
    days = synthetic["days"]
    s = make_settings(synthetic["root"], calendar=calendar)
    res = run_backtest(OrbBuy(), NIFTY, days[0], days[-1], s,
                       data=HistoricalData(synthetic["store"], NIFTY, days[0], days[-1]), use_risk=use_risk)
    t = res.trades_df()
    return set(pd.to_datetime(t.entry_time).dt.date), res.risk_blocks


def test_no_entries_on_fridays_even_with_risk_off(synthetic):
    base, _ = _entry_days(synthetic, {})
    assert any(d.weekday() == 4 for d in base)            # the fixture does trade on Fridays
    got, blocks = _entry_days(synthetic, {"skip_weekdays": ["fri"]})
    assert got and not any(d.weekday() == 4 for d in got)
    assert blocks.get("weekday", 0) > 0


def test_no_entries_near_monthly_expiry(synthetic):
    # fixture option expiries: Thursdays 2025-01-09/16/23 -> the January "monthly" is 01-23.
    # A buffer of 8 days blocks 01-15 .. 01-23, which overlaps the fixture's last trading days.
    monthly = max(e for e in synthetic["store"].expiries("NIFTY") if (e.year, e.month) == (2025, 1))
    base, _ = _entry_days(synthetic, {})
    near = {d for d in base if 0 <= (monthly - d).days <= 8}
    assert near                                            # without the filter it trades those days
    got, blocks = _entry_days(synthetic, {"skip_days_before_monthly_expiry": 8})
    assert got and not (got & near)
    assert blocks["near_monthly_expiry"] > 0
