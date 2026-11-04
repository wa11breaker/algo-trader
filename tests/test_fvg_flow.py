from datetime import date, datetime, time

import pandas as pd
import pytest

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.strategies import get_strategy

from .conftest import NIFTY, make_settings
from .helpers import save_market, smooth_price

D0, D1, EXP = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 9)

# 1-min bars (o, h, l, c) from 10:00 on D1, after quiet bars at 23000
GAP = [(23000, 23004, 22998, 23003),     # 10:00 b1
       (23003, 23020, 23002, 23019),     # 10:01 impulse
       (23019, 23030, 23010, 23028)]     # 10:02 b3: bullish FVG 23004-23010
NO_TOUCH = [(23028, 23029, 23015, 23020)]                  # 10:03 stays above the gap
RETEST = [(23020, 23022, 23007, 23014)]                    # 10:04 dips into the gap, closes above -> entry
RALLY = [(23014, 23026, 23012, 23025), (23025, 23045, 23020, 23040)]   # target 23014 + 2 x 12 = 23038


def bars(day: date, start: time, rows: list[tuple]) -> pd.DataFrame:
    idx = pd.date_range(datetime.combine(day, start), periods=len(rows), freq="1min")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df.index.name = "ts"
    return df.assign(volume=0.0, oi=0.0)


def quiet(price: float, n: int, w: float) -> list[tuple]:
    return [(price, price + w, price - w, price + (1 if i % 2 else -1)) for i in range(n)]


def day(rows: list[tuple], last: float) -> pd.DataFrame:
    """D1: quiet 09:15-09:59, the scenario from 10:00, then quiet (wide bars: no new gaps) to 15:29."""
    rows = quiet(23000, 45, 4) + rows
    return bars(D1, time(9, 15), rows + quiet(last, 375 - len(rows), 16))


def run(tmp_path, monkeypatch, rows, last, delta=None, **params):
    spot = pd.concat([bars(D0, time(9, 15), quiet(23000, 375, 4)), day(rows, last)])
    store = save_market(tmp_path, spot, EXP, pricer=smooth_price)
    if delta is not None:   # fake live order flow: every 1-min futures bar has this delta
        def order_flow(self, until, tf="1min", symbol=None):
            idx = pd.date_range(pd.Timestamp(until).normalize() + pd.Timedelta(hours=9, minutes=15),
                                pd.Timestamp(until), freq="1min")
            return pd.DataFrame({"volume": 100.0, "buy_vol": 50.0 + delta / 2, "sell_vol": 50.0 - delta / 2,
                                 "delta": float(delta)}, index=idx)
        monkeypatch.setattr(HistoricalData, "order_flow", order_flow)
    strat = get_strategy("fvg_flow", size_from_capital=False, **params)
    res = run_backtest(strat, NIFTY, D1, D1, make_settings(tmp_path), data=HistoricalData(store, NIFTY, D1, D1))
    return strat, res.trades_df()


def test_retest_with_buying_delta_buys_otm_call_to_target(tmp_path, monkeypatch):
    strat, t = run(tmp_path, monkeypatch, GAP + NO_TOUCH + RETEST + RALLY, 23040, delta=500)
    assert len(t) == 1
    tr, d = t.iloc[0], strat.diagnostics().iloc[0]
    assert tr["type"] == "CE" and tr["strike"] == 23050 and tr["tag"] == "fvg_up"   # OTM1 of ATM 23000
    assert (d["fvg_lower"], d["fvg_upper"]) == (23004, 23010)
    assert d["entry"] == 23014 and d["init_stop"] == 23002 and d["delta"] == 500
    assert d["why"] == "target" and tr["gross_pnl"] > 0
    assert pd.Timestamp(tr["entry_time"]) == pd.Timestamp("2025-01-07 10:05")   # the minute after the retest


def test_selling_delta_blocks_the_long(tmp_path, monkeypatch):
    strat, t = run(tmp_path, monkeypatch, GAP + NO_TOUCH + RETEST + RALLY, 23040, delta=-500)
    assert t.empty and strat.skips["flow"] == 1


def test_expensive_option_steps_further_otm(tmp_path, monkeypatch):
    # smooth_price: OTM1 23050 ~47, OTM2 23100 ~34 at spot 23014
    _, t = run(tmp_path, monkeypatch, GAP + NO_TOUCH + RETEST + RALLY, 23040, delta=500, max_premium=40)
    assert t.iloc[0]["strike"] == 23100
    strat, t = run(tmp_path / "b", monkeypatch, GAP + NO_TOUCH + RETEST + RALLY, 23040, delta=500, max_premium=1)
    assert t.empty and strat.skips["too_expensive"] == 1


def test_backtest_without_flow_data_still_trades(tmp_path, monkeypatch):
    strat, t = run(tmp_path, monkeypatch, GAP + NO_TOUCH + RETEST + RALLY, 23040)
    assert len(t) == 1 and strat.skips["no_flow_data"] == 1


def test_close_through_the_gap_invalidates_it(tmp_path, monkeypatch):
    rows = GAP + [(23028, 23029, 22995, 22999),     # closes below the gap's lower edge
                  (22999, 23015, 22998, 23013)]     # would be a retest if the gap were alive
    strat, t = run(tmp_path, monkeypatch, rows, 23013, delta=500)
    assert t.empty


def test_entry_window_overrides_global_risk_window(tmp_path, monkeypatch):
    # global risk window 09:45-13:30 would block this 14:04 entry; fvg_flow widens it to 09:30-15:00
    shift = [(23000, 23004, 22998, 23003)] * 280                    # quiet until 14:00
    rows = shift + GAP + NO_TOUCH + RETEST + RALLY
    spot = pd.concat([bars(D0, time(9, 15), quiet(23000, 375, 4)),
                      bars(D1, time(9, 15), quiet(23000, 5, 4) + rows + quiet(23040, 375 - 5 - len(rows), 16))])
    store = save_market(tmp_path, spot, EXP, pricer=smooth_price)
    s = make_settings(tmp_path, risk={"no_entry_before": "09:45", "no_entry_after": "13:30"})
    res = run_backtest(get_strategy("fvg_flow", size_from_capital=False), NIFTY, D1, D1, s,
                       data=HistoricalData(store, NIFTY, D1, D1))
    t = res.trades_df()
    assert len(t) == 1 and pd.Timestamp(t.iloc[0]["entry_time"]) == pd.Timestamp("2025-01-07 14:05")


# ---------------------------------------------------------------- research switches
def test_mid_retest_needs_the_gaps_50pct_level(tmp_path, monkeypatch):
    shallow = [(23020, 23022, 23008, 23014)]                       # touches 23010 edge, not the 23007 mid
    _, t = run(tmp_path, monkeypatch, GAP + NO_TOUCH + shallow + RALLY, 23040, delta=500)
    assert len(t) == 1
    _, t = run(tmp_path / "b", monkeypatch, GAP + NO_TOUCH + shallow + RALLY, 23040, delta=500, retest_depth="mid")
    assert t.empty
    _, t = run(tmp_path / "c", monkeypatch, GAP + NO_TOUCH + RETEST + RALLY, 23040, delta=500, retest_depth="mid")
    assert len(t) == 1                                              # RETEST low 23007 = mid


def test_atr_stop_and_target(tmp_path, monkeypatch):
    strat, _ = run(tmp_path, monkeypatch, GAP + NO_TOUCH + RETEST + RALLY, 23040, delta=500,
                   sl_mode="atr", sl_atr=1.0, tp_mode="atr", tp_atr=2.0)
    d = strat.diagnostics().iloc[0]
    assert d["init_stop"] != 23002 and d["init_stop"] < d["entry"]
    assert abs((d["target"] - d["entry"]) - 2 * (d["entry"] - d["init_stop"])) < 1e-6


def test_liquidity_target_is_clamped_between_min_and_max_r(tmp_path, monkeypatch):
    strat, _ = run(tmp_path, monkeypatch, GAP + NO_TOUCH + RETEST + RALLY, 23040, delta=500,
                   tp_mode="liquidity", tp_min_r=1.0, tp_max_r=4.0)
    d = strat.diagnostics().iloc[0]
    assert d["entry"] + d["risk"] <= d["target"] <= d["entry"] + 4 * d["risk"]


def _with_futures(tmp_path, retest_close):
    """Market where futures copy spot, except the retest bar's close (drives the close-location delta)."""
    from algo.data.store import DataStore
    rows = GAP + NO_TOUCH + [(23020, 23022, 23007, retest_close)] + RALLY
    spot = pd.concat([bars(D0, time(9, 15), quiet(23000, 375, 4)), day(GAP + NO_TOUCH + RETEST + RALLY, 23040)])
    store = save_market(tmp_path, spot, EXP, pricer=smooth_price)
    fut = pd.concat([bars(D0, time(9, 15), quiet(23000, 375, 4)), day(rows, 23040)]).assign(volume=100.0)
    DataStore(tmp_path).save_future("NIFTY", date(2025, 1, 30), fut,
                                     {"instrument_key": "F", "trading_symbol": "NIFTY FUT", "expiry": "2025-01-30",
                                      "lot_size": 75})
    return store


@pytest.mark.parametrize("retest_close,trades", [(23020, 1), (23008, 0)])
def test_flow_proxy_uses_futures_close_location(tmp_path, retest_close, trades):
    store = _with_futures(tmp_path, retest_close)
    strat = get_strategy("fvg_flow", size_from_capital=False, flow_proxy=True)
    res = run_backtest(strat, NIFTY, D1, D1, make_settings(tmp_path), data=HistoricalData(store, NIFTY, D1, D1))
    assert len(res.trades_df()) == trades and strat.skips["no_flow_data"] == 0
