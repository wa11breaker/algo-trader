from datetime import date

import pytest

from algo.analytics.metrics import compute_metrics, equity_curve
from algo.backtest.engine import BacktestResult
from algo.core.capital import CapitalManager, Withdrawal

D = [date(2025, 1, d) for d in (6, 7, 8, 9, 10)]


def test_withdraws_initial_once_when_equity_doubles():
    cm = CapitalManager(15000)
    assert cm.end_of_day(D[0], 10000) is None and cm.equity == 25000
    w = cm.end_of_day(D[1], 15500)                       # equity 30500 >= 30000
    assert w == Withdrawal(D[1], 15000, 30500)
    assert cm.equity == 15500 and cm.withdrawn == 15000
    assert cm.end_of_day(D[2], 40000) is None            # never twice
    assert cm.equity == 40000 and cm.withdrawn == 15000


def test_no_withdrawal_when_disabled():
    cm = CapitalManager(15000, withdraw_multiple=None)
    cm.end_of_day(D[0], 50000)
    assert cm.withdrawn == 0 and cm.equity == 65000


def test_kill_switch_before_and_after_withdrawal():
    cm = CapitalManager(10000, kill_switch_pct=50)
    cm.end_of_day(D[0], -4000)
    assert not cm.halted
    cm.end_of_day(D[1], -5100)                           # equity 4900 < 5000
    assert cm.halted_on == D[1] and cm.lots_for(100, 100) == 0

    cm = CapitalManager(10000, kill_switch_pct=50)
    cm.end_of_day(D[0], 12000)                           # 22000 -> withdraw 10000, 12000 left
    cm.end_of_day(D[1], 7000)                            # 7000 left: above 6000
    assert not cm.halted
    cm.end_of_day(D[2], 5900)                            # 5900 < 50% of 12000
    assert cm.halted


@pytest.mark.parametrize("equity,risk,cost,expected", [
    (10000, 1215, 5625, 0),      # 6% of 10k = 600 < one lot's risk
    (15000, 800, 9000, 1),       # 900 risk budget, 13.5k premium budget
    (50000, 1215, 5625, 2),      # 3000 / 1215
    (500000, 100, 100, 10),      # capped at max_lots
    (50000, 100, 46000, 0),      # premium too large (90% = 45k)
])
def test_lot_sizing(equity, risk, cost, expected):
    assert CapitalManager(equity, risk_pct=6, position_pct=90, max_lots=10).lots_for(risk, cost) == expected


def test_metrics_report_withdrawal_and_drawdown_on_total_wealth():
    res = BacktestResult(strategy="x", underlying="NIFTY", start=D[0], end=D[-1], params={},
                         initial_capital=10000, trades=[], trading_days=D,
                         withdrawals=[Withdrawal(D[2], 10000, 21000)])
    eq = equity_curve(res)
    assert eq["withdrawn"].tolist() == [0, 0, 10000, 10000, 10000]
    assert eq["drawdown"].min() == 0                     # a withdrawal is not a loss
    m = compute_metrics(res)
    assert m["withdrawn"] == 10000 and m["withdrawal_dates"] == [str(D[2])]
    assert m["days_to_recover_capital"] == 3
