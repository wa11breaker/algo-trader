from datetime import date

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.strategies import get_strategy

from .conftest import NIFTY, make_settings
from .helpers import build_market, fill_day, flat, smooth_price

D0, D1, EXP = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 9)
DAY0 = [(23000, 23200, 22800, 23000)] + flat(23000, 74)
OPEN = [(23000, 23015, 22990, 23005), (23005, 23010, 22985, 22995), (22995, 23008, 22990, 23000)]


def run(tmp_path, rest, last, **params):
    store = build_market(tmp_path, {D0: DAY0, D1: fill_day(OPEN + rest, last)}, EXP, pricer=smooth_price)
    strat = get_strategy("vwap_pullback_spread", **params)
    res = run_backtest(strat, NIFTY, D1, D1, make_settings(tmp_path), data=HistoricalData(store, NIFTY, D1, D1))
    return strat, res


UP = [(23000, 23032, 22998, 23030), (23030, 23052, 23028, 23050), (23050, 23072, 23048, 23070),
      (23070, 23092, 23068, 23090),
      (23090, 23092, 23040, 23060),            # pullback: touches VWAP/EMA zone, closes above VWAP
      (23060, 23100, 23058, 23098),            # trigger: green, closes above pullback high
      (23098, 23140, 23095, 23138), (23138, 23180, 23135, 23178), (23178, 23215, 23175, 23212)]


def test_uptrend_pullback_buys_ce_spread_and_hits_target(tmp_path):
    strat, res = run(tmp_path, UP, 23212)
    diag = res.diagnostics
    assert len(diag) == 1
    row = diag.iloc[0]
    assert row["side"] == "up" and row["time"] == "09:59" and row["exit_reason"] == "TARGET"
    assert "CE" in row["long"] and "CE" in row["short"]
    legs = res.trades_df()
    assert set(legs["side"]) == {"BUY", "SELL"} and legs["group"].nunique() == 1
    assert row["net_pnl"] > 0


def test_downtrend_pullback_buys_pe_spread(tmp_path):
    down = [(23000, 23002, 22968, 22970), (22970, 22972, 22948, 22950), (22950, 22952, 22928, 22930),
            (22930, 22932, 22908, 22910),
            (22910, 22960, 22908, 22940),      # pullback up toward VWAP, closes below it
            (22940, 22942, 22900, 22902),      # trigger: red, closes below pullback low
            (22902, 22905, 22860, 22862), (22862, 22865, 22820, 22822), (22822, 22825, 22785, 22788)]
    _, res = run(tmp_path, down, 22788)
    row = res.diagnostics.iloc[0]
    assert row["side"] == "down" and "PE" in row["long"]


def test_choppy_day_inside_opening_range_no_trade(tmp_path):
    chop = [(23000, 23010, 22990, 23008 if i % 2 else 22992) for i in range(20)]
    _, res = run(tmp_path, chop, 23000)
    assert not res.trades


def test_option_mode_buys_single_atm(tmp_path):
    _, res = run(tmp_path, UP, 23212, instrument="option")
    legs = res.trades_df()
    assert len(legs) == 1 and legs.iloc[0]["type"] == "CE" and legs.iloc[0]["side"] == "BUY"
