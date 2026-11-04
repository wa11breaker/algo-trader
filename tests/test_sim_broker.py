from datetime import date, datetime

import pandas as pd
import pytest

from algo.backtest.costs import CostModel
from algo.backtest.sim_broker import SimBroker
from algo.config import get_settings
from algo.core.models import ExitReason, OptionContract, OptionType, Order, Side

C = OptionContract("NIFTY", date(2025, 1, 9), 23000, OptionType.CE, 75)
T = [datetime(2025, 1, 6, 10, m) for m in range(10)]


class FakeData:
    def __init__(self, bars):  # {ts: (o,h,l,c)}
        self.bars = bars

    def option_bar(self, c, ts):
        return self.bars.get(ts)

    def option_ltp(self, c, at):
        prior = [v for k, v in self.bars.items() if k <= at]
        return prior[-1][3] if prior else None


def make(bars, slippage_pct=0.0, slippage_min=0.0):
    return SimBroker(FakeData(bars), CostModel(get_settings().costs), slippage_pct, slippage_min)


def buy(b, sl=30, tgt=60):
    return b.place_order(Order(C, Side.BUY, 1, T[0], sl_pct=sl, target_pct=tgt))


def test_fills_at_next_bar_open_with_slippage():
    b = make({T[1]: (100, 101, 99, 100)}, slippage_pct=1.0, slippage_min=0.5)
    buy(b)
    b.process_bar(T[1])
    pos = b.positions()[0]
    assert pos.entry_price == pytest.approx(101.0)       # 100 + max(1%, 0.5)
    assert pos.sl_price == pytest.approx(70.7)
    assert pos.target_price == pytest.approx(161.6)


def test_sl_wins_when_both_hit_in_same_bar():
    b = make({T[1]: (100, 101, 99, 100), T[2]: (100, 200, 50, 100)})
    buy(b)
    b.process_bar(T[1])
    b.process_bar(T[2])
    (t,) = b.trades()
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.exit_price == pytest.approx(70.0)
    assert t.gross_pnl == pytest.approx(-30 * 75)


def test_gap_through_sl_fills_at_open():
    b = make({T[1]: (100, 101, 99, 100), T[2]: (60, 65, 55, 60)})
    buy(b)
    b.process_bar(T[1])
    b.process_bar(T[2])
    assert b.trades()[0].exit_price == pytest.approx(60.0)


def test_target_hit():
    b = make({T[1]: (100, 101, 99, 100), T[2]: (150, 170, 149, 165)})
    buy(b)
    b.process_bar(T[1])
    b.process_bar(T[2])
    t = b.trades()[0]
    assert t.exit_reason is ExitReason.TARGET
    assert t.exit_price == pytest.approx(160.0)
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.costs)
    assert t.costs > 0


def test_order_waits_for_illiquid_bar_then_cancels():
    b = make({T[3]: (100, 101, 99, 100)})
    buy(b)
    b.process_bar(T[1])
    assert b.pending_orders() and not b.positions()
    b.process_bar(T[3])
    assert b.positions()[0].entry_time == T[3]

    b2 = make({})
    buy(b2)
    for t in T[1:]:
        b2.process_bar(t)
    assert not b2.pending_orders() and len(b2.rejected) == 1


def test_signal_exit_and_square_off():
    bars = {t: (100 + i, 101 + i, 99 + i, 100 + i) for i, t in enumerate(T)}
    b = make(bars)
    buy(b, sl=None, tgt=None)
    b.process_bar(T[1])
    b.exit_position(b.positions()[0])
    b.process_bar(T[2])
    assert b.trades()[0].exit_price == 102 and b.trades()[0].exit_reason is ExitReason.SIGNAL

    buy(b, sl=None, tgt=None)
    b.process_bar(T[3])
    b.square_off(T[5])
    assert b.trades()[1].exit_reason is ExitReason.SQUARE_OFF and b.trades()[1].exit_price == 105
