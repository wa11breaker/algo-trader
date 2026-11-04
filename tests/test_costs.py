import pytest

from algo.backtest.costs import CostModel
from algo.config import get_settings
from algo.core.models import Side


# Fixed rates so the tests don't depend on your config/settings.yaml edits.
RATES = {"brokerage_per_order": 20.0, "stt_sell_pct": 0.1, "txn_pct": {"NSE": 0.03503, "BSE": 0.0325},
         "sebi_per_crore": 10.0, "stamp_buy_pct": 0.003, "gst_pct": 18.0}


@pytest.fixture
def model():
    return CostModel(RATES)


def test_config_costs_load():
    CostModel(get_settings().costs)  # user config stays parseable


def test_buy_leg_nse(model):
    # 75 qty @ 100 -> turnover 7500
    c = model.charges(Side.BUY, 100, 75, "NSE")
    assert c.brokerage == 20
    assert c.stt == 0
    assert c.txn == pytest.approx(7500 * 0.0003503)          # 2.62725
    assert c.sebi == pytest.approx(7500 * 10 / 1e7)          # 0.0075
    assert c.stamp == pytest.approx(7500 * 0.00003)          # 0.225
    assert c.gst == pytest.approx(0.18 * (20 + 2.62725 + 0.0075))
    assert c.total == pytest.approx(26.934, abs=1e-3)


def test_sell_leg_nse(model):
    # 75 qty @ 120 -> turnover 9000; STT on sell, no stamp
    c = model.charges(Side.SELL, 120, 75, "NSE")
    assert c.stt == pytest.approx(9.0)
    assert c.stamp == 0
    assert c.total == pytest.approx(20 + 9 + 3.1527 + 0.009 + 0.18 * (20 + 3.1527 + 0.009), abs=1e-3)


def test_bse_uses_bse_txn_rate(model):
    nse = model.charges(Side.BUY, 200, 20, "NSE").txn
    bse = model.charges(Side.BUY, 200, 20, "BSE").txn
    assert bse == pytest.approx(4000 * 0.000325)
    assert bse < nse
