from datetime import date, datetime, time

import pandas as pd
import pytest

from algo.backtest.costs import CostModel
from algo.backtest.data import HistoricalData
from algo.backtest.sim_broker import SimBroker
from algo.config import get_settings
from algo.core.context import Context
from algo.core.models import ExitReason, OptionContract, OptionType, Side, Trade
from algo.core.risk import RiskManager

from .conftest import NIFTY

RISK = {"enabled": True, "max_risk_per_trade_pct": 4, "max_position_pct": 50, "daily_loss_limit_pct": 6,
        "max_consecutive_losses": 3, "max_trades_per_day": 2, "no_entry_before": "09:45",
        "no_entry_after": "13:30"}


def _ctx(synthetic, risk=None, capital=20000):
    d = synthetic["days"]
    data = HistoricalData(synthetic["store"], NIFTY, d[0], d[-1])
    broker = SimBroker(data, CostModel(get_settings().costs), 0.5, 0.5)
    return Context(NIFTY, data, broker, risk=RiskManager(risk, capital)), broker


# ---- spreads ---------------------------------------------------------------------
def test_debit_spread_open_value_close(synthetic):
    ctx, broker = _ctx(synthetic)
    t0 = pd.Timestamp(datetime(2025, 1, 7, 10, 0))
    ctx.now = t0
    # the session fixture's option model has flat time value, so use an ITM long leg
    sp = ctx.open_debit_spread("CE", width_steps=2, moneyness="ITM2", tag="t")
    assert sp is not None and sp.width == 100 and sp.est_debit > 0
    assert sp.short.strike == sp.long.strike + 100 and sp.long.strike == ctx.atm_strike() - 100
    assert len(broker.pending_orders()) == 2

    t1 = t0 + pd.Timedelta(minutes=1)
    broker.process_bar(t1)
    ctx.now = t1
    lp, sh = ctx.spread_legs(sp)
    assert lp.side is Side.BUY and sh.side is Side.SELL and lp.group == sh.group == sp.group
    entry = ctx.spread_entry_debit(sp)
    assert entry == pytest.approx(lp.entry_price - sh.entry_price)

    t5 = t0 + pd.Timedelta(minutes=5)
    ctx.now = t5
    ctx.close_spread(sp, ExitReason.TARGET)
    broker.process_bar(t5 + pd.Timedelta(minutes=1))
    trades = broker.trades()
    assert len(trades) == 2 and {t.group for t in trades} == {sp.group}
    long_t = next(t for t in trades if t.side is Side.BUY)
    short_t = next(t for t in trades if t.side is Side.SELL)
    exit_value = long_t.exit_price - short_t.exit_price
    qty = long_t.quantity
    assert sum(t.gross_pnl for t in trades) == pytest.approx((exit_value - entry) * qty)
    assert sum(t.net_pnl for t in trades) == pytest.approx((exit_value - entry) * qty - sum(t.costs for t in trades))
    assert all(t.exit_reason is ExitReason.TARGET for t in trades)


def test_close_spread_cancels_unfilled_legs(synthetic):
    ctx, broker = _ctx(synthetic)
    ctx.now = pd.Timestamp(datetime(2025, 1, 7, 10, 0))
    sp = ctx.open_debit_spread("PE", moneyness="ITM2", tag="t")
    ctx.close_spread(sp)
    assert not broker.pending_orders() and sp.closing


# ---- risk manager ------------------------------------------------------------------
class StubBroker:
    def __init__(self, trades=()):
        self._t = list(trades)
    def trades(self): return self._t
    def positions(self): return []
    def pending_orders(self): return []


class StubData:
    def option_ltp(self, c, at): return None


C = OptionContract("NIFTY", date(2025, 1, 9), 23000, OptionType.CE, 75)


def trade(net, day=date(2025, 1, 7), hh=10, mm=0, grp=None):
    t = datetime.combine(day, time(hh, mm))
    return Trade(C, Side.BUY, 1, 75, t, 100, t, 100, ExitReason.STOP_LOSS, net, 0.0, tag="x", group=grp or f"{t}{net}")


def check(trades=(), at=datetime(2025, 1, 7, 11, 0), max_loss=500, cost=2000, cfg=RISK, events=None):
    rm = RiskManager(cfg, capital=20000)
    if events:
        rm.event_days = set(events)
    ctx = Context(NIFTY, StubData(), StubBroker(trades), risk=rm)
    ctx.now = at
    return rm, rm.check(ctx, max_loss, cost)


def test_risk_allows_normal_trade():
    assert check()[1] is None


def test_risk_per_trade_and_position_size():
    assert check(max_loss=801)[1] == "risk_per_trade"         # 4% of 20k = 800
    assert check(cost=10_001)[1] == "position_size"           # 50% of 20k


def test_time_window_and_event_days():
    assert check(at=datetime(2025, 1, 7, 9, 30))[1] == "time_window"
    assert check(at=datetime(2025, 1, 7, 13, 30))[1] == "time_window"
    assert check(events=[date(2025, 1, 7)])[1] == "event_day"


def test_max_trades_per_day_resets_next_day():
    rm = RiskManager(RISK, 20000)
    ctx = Context(NIFTY, StubData(), StubBroker(), risk=rm)
    ctx.now = datetime(2025, 1, 7, 10, 0)
    assert rm.check(ctx, 100, 100) is None and rm.check(ctx, 100, 100) is None
    assert rm.check(ctx, 100, 100) == "max_trades_per_day"
    ctx.now = datetime(2025, 1, 8, 10, 0)
    assert rm.check(ctx, 100, 100) is None


def test_loss_streak_counts_today_only():
    losses = [trade(-100, mm=m) for m in (0, 10, 20)]
    assert check(losses)[1] == "loss_streak"
    assert check([trade(-100, mm=0), trade(50, mm=5), trade(-100, mm=10)])[1] is None
    yesterday = [trade(-100, day=date(2025, 1, 6), mm=m) for m in (0, 10, 20)]
    assert check(yesterday)[1] is None


def test_spread_legs_count_as_one_round_trip():
    # one losing spread = 2 legs in the same group; should count as ONE loss
    legs = [trade(-100, mm=0, grp="g1"), trade(20, mm=0, grp="g1"),
            trade(-100, mm=10, grp="g2"), trade(20, mm=10, grp="g2")]
    assert check(legs)[1] is None          # 2 losing round trips < 3


def test_daily_loss_limit():
    assert check([trade(-1250)])[1] == "daily_loss_limit"     # 6% of 20k = 1200
    assert check([trade(-1250, day=date(2025, 1, 6))])[1] is None


def test_disabled_risk_allows_everything():
    assert check(max_loss=1e9, cfg={"enabled": False})[1] is None
