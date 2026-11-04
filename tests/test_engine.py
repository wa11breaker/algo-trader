from datetime import date, datetime, time

import pandas as pd
import pytest

from algo.analytics.metrics import compute_metrics
from algo.analytics.report import write_report
from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.config import Underlying
from algo.core.broker import MarketData
from algo.core.context import Context
from algo.core.models import OptionType
from algo.core.strategy import Strategy
from algo.strategies import get_strategy

from .conftest import NIFTY


def _data(synthetic):
    days = synthetic["days"]
    return HistoricalData(synthetic["store"], NIFTY, days[0], days[-1])


# ---- no lookahead -----------------------------------------------------------
def test_resampled_bars_only_include_completed(synthetic):
    data = _data(synthetic)
    d = synthetic["days"][1]
    at = lambda h, m: datetime.combine(d, time(h, m))
    # 1-min bar 09:18 closes at 09:19 -> the 09:15 5-min bar (ends 09:20) is not complete yet
    assert data.spot_bars(at(9, 18), "5min").index[-1] < pd.Timestamp(at(9, 15))
    # after the 09:19 bar closes (09:20), it is
    assert data.spot_bars(at(9, 19), "5min").index[-1] == pd.Timestamp(at(9, 15))
    assert data.spot_bars(at(9, 19)).index[-1] == pd.Timestamp(at(9, 19))
    # values of the 5-min bar only use its own 5 minutes
    one = synthetic["spot"].loc[at(9, 15):at(9, 19)]
    bar = data.spot_bars(at(9, 19), "5min").iloc[-1]
    assert bar["high"] == one["high"].max() and bar["close"] == one["close"].iloc[-1]


class Probe(Strategy):
    name = "probe"

    def on_start(self, ctx):
        self.checked = 0

    def on_bar(self, ctx):
        assert ctx.spot().index[-1] == pd.Timestamp(ctx.now)
        b5 = ctx.spot("5min", lookback=3)
        if len(b5):
            assert b5.index[-1] + pd.Timedelta(minutes=5) <= pd.Timestamp(ctx.bar_close_time)
        self.checked += 1


def test_engine_never_exposes_future_bars(synthetic, settings):
    s = Probe()
    days = synthetic["days"]
    run_backtest(s, NIFTY, days[0], days[2], settings, data=_data(synthetic))
    assert s.checked > 0


# ---- strike selection ---------------------------------------------------------
class FixedSpot(MarketData):
    def __init__(self, price):
        self.price = price
    def spot_bars(self, until, tf="1min", lookback=None):
        return pd.DataFrame({"close": [self.price]}, index=[pd.Timestamp(until)])
    def expiries(self, on):
        return [date(2025, 1, 9), date(2025, 1, 16), date(2025, 1, 30)]
    def strikes(self, expiry):
        return []
    def contract(self, expiry, strike, option_type):
        return (expiry, strike, option_type)
    def option_ltp(self, c, at):
        return None


@pytest.mark.parametrize("ul,price,atm", [
    (NIFTY, 23024, 23000), (NIFTY, 23026, 23050),
    (Underlying("BANKNIFTY", "x", "NSE", 100), 51149, 51100),
    (Underlying("SENSEX", "x", "BSE", 100), 76351, 76400),
])
def test_atm_rounding(ul, price, atm):
    ctx = Context(ul, FixedSpot(price), broker=None)
    ctx.now = datetime(2025, 1, 6, 10, 0)
    assert ctx.atm_strike() == atm


def test_moneyness_and_expiry_selection():
    ctx = Context(NIFTY, FixedSpot(23010), broker=None)
    ctx.now = datetime(2025, 1, 9, 10, 0)
    assert ctx.select_option("CE", "OTM1")[1] == 23050
    assert ctx.select_option("PE", "OTM1")[1] == 22950
    assert ctx.select_option("CE", "ITM2")[1] == 22900
    assert ctx.select_option("PE", "ITM1")[1] == 23050
    assert ctx.select_option("CE")[0] == date(2025, 1, 9)
    assert ctx.select_option("CE", min_days_to_expiry=1)[0] == date(2025, 1, 16)
    assert ctx.select_option("CE", expiry="next")[0] == date(2025, 1, 16)
    assert ctx.select_option("CE", expiry="monthly")[0] == date(2025, 1, 30)


# ---- end-to-end -------------------------------------------------------------------
@pytest.mark.parametrize("name", ["ema_cross_buy", "orb_buy"])
def test_strategies_end_to_end(name, synthetic, settings, tmp_path):
    days = synthetic["days"]
    res = run_backtest(get_strategy(name), NIFTY, days[0], days[-1], settings, data=_data(synthetic))
    assert res.trades, "expected the synthetic oscillation to produce trades"
    df = res.trades_df()
    assert (pd.to_datetime(df["exit_time"]).dt.time <= time(15, 15)).all()
    assert (pd.to_datetime(df["entry_time"]).dt.date == pd.to_datetime(df["exit_time"]).dt.date).all()
    assert df["net_pnl"].sum() == pytest.approx(df["gross_pnl"].sum() - df["costs"].sum(), abs=0.05)
    m = compute_metrics(res)
    assert m["trades"] == len(df)
    out = write_report(res, tmp_path)
    assert (out / "report.html").exists() and (out / "trades.csv").exists()


# ---- session normalisation ---------------------------------------------------
def _spot_with(days_bars: dict) -> pd.DataFrame:
    rows = []
    for d, stamps in days_bars.items():
        for t in stamps:
            rows.append((pd.Timestamp(datetime.combine(d, t)), 100.0, 101.0, 99.0, 100.0))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"]).set_index("ts")
    df["volume"] = 0.0
    df["oi"] = 0.0
    return df


def _minutes(n: int, start=time(9, 15)) -> list:
    t0 = datetime.combine(date(2000, 1, 1), start)
    return [(t0 + pd.Timedelta(minutes=i)).time() for i in range(n)]


class DayCounter(Strategy):
    name = "day_counter"

    def on_start(self, ctx):
        self.days, self.first_bars = [], []

    def on_day_start(self, ctx):
        self.days.append(ctx.today)
        self.first_bars.append(ctx.now.time())

    def on_bar(self, ctx):
        pass


def test_pre_open_and_post_close_bars_are_dropped_not_the_day(synthetic, settings):
    d1, d2, d3 = date(2022, 3, 24), date(2022, 3, 25), date(2024, 3, 2)
    spot = _spot_with({
        d1: [time(9, 7)] + _minutes(375) + [time(15, 31)],   # Upstox 2022 style: pre-open + closing bar
        d2: _minutes(375),
        d3: _minutes(105),                                    # DR-drill Saturday: skipped
    })
    spot.loc[pd.Timestamp(datetime.combine(d1, time(15, 31))), ["high", "close"]] = [150.0, 150.0]
    data = HistoricalData(synthetic["store"], NIFTY, d1, d3, spot=spot)
    assert data.spot_1m.index.min().time() == time(9, 15)
    assert data.spot_bars(datetime.combine(d1, time(15, 29)))["high"].max() == 101.0   # no 15:31 spike
    s = DayCounter()
    run_backtest(s, NIFTY, d1, d3, settings, data=data)
    assert s.days == [d1, d2] and set(s.first_bars) == {time(9, 15)}
