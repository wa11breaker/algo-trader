"""Paper trading (no network): order-flow bars, live market data, paper broker, runner."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import pytest

from algo.backtest.data import HistoricalData
from algo.backtest.engine import run_backtest
from algo.config import Underlying
from algo.core.context import Context
from algo.core.flow_gate import OrderFlowGate
from algo.core.models import OptionType, Position, Side
from algo.fyers.symbols import FnoUniverse
from algo.live.data import LiveMarketData
from algo.live.orderflow import BarBuilder
from algo.live.runner import PaperRunner
from algo.live.state import PaperState, PaperStore, position_to_dict
from algo.strategies import get_strategy

from .conftest import make_settings

UL = Underlying("NIFTY", "NSE_INDEX|Nifty 50", "NSE", 50, fyers_symbol="NSE:NIFTY50-INDEX")
IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2025, 1, 7)


def at(h, m, s=0, d=DAY):
    return datetime.combine(d, time(h, m, s))


# ---- order-flow bar builder ---------------------------------------------------------
def test_bar_builder_ohlc_volume_and_delta():
    b = BarBuilder()
    t = at(9, 15)
    b.on_tick("X", t, 100.0, volume=1000, bid=99.5, ask=100.0)          # baseline: no volume yet
    b.on_tick("X", t + timedelta(seconds=5), 100.0, 1050, 99.5, 100.5)   # at prev ask -> buy 50
    b.on_tick("X", t + timedelta(seconds=9), 99.5, 1080, 99.0, 100.0)    # at prev bid -> sell 30
    b.on_tick("X", t + timedelta(seconds=20), 99.8, 1100, 99.5, 100.0)   # inside, up-tick -> buy 20
    b.on_tick("X", t + timedelta(seconds=30), 99.8, 1110, 99.5, 100.0)   # unchanged -> last side (buy)
    b.on_tick("X", t + timedelta(seconds=61), 101.0, 1200, 100.5, 101.0)  # next minute, at ask -> buy 90
    df = b.bars("X")
    first = df.loc[pd.Timestamp(t)]
    assert (first["open"], first["high"], first["low"], first["close"]) == (100.0, 100.0, 99.5, 99.8)
    assert first["volume"] == 110 and first["buy_vol"] == 80 and first["sell_vol"] == 30
    assert first["delta"] == 50
    assert df.loc[pd.Timestamp(t + timedelta(minutes=1)), "delta"] == 90
    assert b.bar("X", t) == (100.0, 100.0, 99.5, 99.8)
    q = b.quote("X")
    assert (q.ltp, q.bid, q.ask) == (101.0, 100.5, 101.0)
    assert len(b.bars("X", until=t)) == 1


# ---- fakes built from the synthetic store ---------------------------------------------
def fyers_symbol(exp: date, strike: float, typ: str) -> str:
    return f"NSE:NIFTY{exp:%y%m%d}{strike:g}{typ}"


def master_from_store(store) -> dict:
    m = {}
    for exp in store.expiries("NIFTY"):
        epoch = int(datetime.combine(exp, time(15, 30), IST).timestamp())
        for r in store.load_contracts("NIFTY", exp).itertuples():
            sym = fyers_symbol(exp, r.strike_price, r.instrument_type)
            m[sym] = {"symTicker": sym, "underSym": "NIFTY", "expiryDate": str(epoch), "optType": r.instrument_type,
                      "strikePrice": float(r.strike_price), "minLotSize": 75, "tradeStatus": 1, "exInstType": 14}
    for exp in store.future_expiries("NIFTY"):
        sym = f"NSE:NIFTY{exp:%y%m%d}FUT"
        m[sym] = {"symTicker": sym, "underSym": "NIFTY", "optType": "XX", "strikePrice": -1.0, "minLotSize": 75,
                  "expiryDate": str(int(datetime.combine(exp, time(15, 30), IST).timestamp())),
                  "tradeStatus": 1, "exInstType": 11}
    return m


class Source:
    """Symbol -> 1-min DataFrame, from the synthetic Parquet store."""

    def __init__(self, store, uni: FnoUniverse):
        self.store, self.uni, self.cache = store, uni, {}

    def frame(self, sym: str) -> pd.DataFrame | None:
        if sym not in self.cache:
            if sym == UL.fyers_symbol:
                df = self.store.load_spot("NIFTY", date(2024, 12, 1), date(2025, 2, 28))
            elif sym.endswith("FUT"):
                exp = next(e for e in self.store.future_expiries("NIFTY") if f"{e:%y%m%d}FUT" in sym)
                df = self.store.load_future("NIFTY", exp)
            else:
                c = self.uni.by_symbol(sym)
                df = self.store.load_option("NIFTY", c.expiry, c.strike, c.option_type.value) if c else None
            self.cache[sym] = df
        return self.cache[sym]


class FakeClient:
    def __init__(self, src: Source):
        self.src, self.history_calls = src, []

    def history(self, symbol, start, end, resolution="1", oi=False):
        self.history_calls.append((symbol, start, end))
        df = self.src.frame(symbol)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "oi"])
        return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]

    def quotes(self, symbols):
        return {}


class FakeFeed:
    def __init__(self, builder: BarBuilder, src: Source):
        self.builder, self.src, self.subscribed, self.messages = builder, src, set(), 0
        self.cum: dict[str, float] = {}

    def start(self, symbols):
        self.subscribe(symbols)

    def subscribe(self, symbols):
        self.subscribed.update(symbols)

    def stop(self):
        pass

    def push_minute(self, m: datetime) -> None:
        """Ticks reproducing each subscribed symbol's 1-min bar exactly (o, h, l, c, volume)."""
        for sym in sorted(self.subscribed):
            df = self.src.frame(sym)
            if df is None or pd.Timestamp(m) not in df.index:
                continue
            r = df.loc[pd.Timestamp(m)]
            index = sym == UL.fyers_symbol
            cum = self.cum.get(sym, 0.0)
            steps = [(1, r["open"], 0.0), (20, r["high"], r["volume"] / 3), (35, r["low"], r["volume"] / 3),
                     (50, r["close"], r["volume"] / 3)]
            for sec, px, dv in steps:
                cum += dv
                self.builder.on_tick(sym, m + timedelta(seconds=sec), float(px), None if index else cum,
                                     None if index else float(px) - 0.5, None if index else float(px) + 0.5)
                self.messages += 1
            self.cum[sym] = cum


class Clock:
    def __init__(self, start: datetime, feed: FakeFeed):
        self.t, self.feed = start, feed
        self.pushed = start.replace(second=0) - timedelta(minutes=1)

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=max(seconds, 0))
        while self.pushed + timedelta(minutes=2) <= self.t:   # minute m is complete at m + 1 min
            self.pushed += timedelta(minutes=1)
            self.feed.push_minute(self.pushed)


@pytest.fixture
def live(synthetic):
    store = synthetic["store"]
    uni = FnoUniverse(master_from_store(store), UL)
    src = Source(store, uni)
    builder = BarBuilder()
    feed = FakeFeed(builder, src)
    client = FakeClient(src)
    data = LiveMarketData(client, uni, UL, builder, feed=feed, warmup_days=10)
    return {"store": store, "uni": uni, "builder": builder, "feed": feed, "client": client, "data": data}


def test_universe_from_master(live):
    uni = live["uni"]
    assert uni.expiries(DAY)[0] == date(2025, 1, 9)
    c = uni.contract(date(2025, 1, 9), 23000, OptionType.CE)
    assert c.instrument_key == fyers_symbol(date(2025, 1, 9), 23000, "CE") and c.lot_size == 75
    assert uni.front_future(DAY) == "NSE:NIFTY250109FUT"
    assert uni.front_future(date(2025, 1, 9)) == "NSE:NIFTY250130FUT"   # rolls on expiry day
    assert len(uni.near_atm(23010, DAY, 2)) == 10      # 5 strikes x CE/PE, nearest expiry


# ---- live market data -------------------------------------------------------------------
def test_live_data_merges_history_feed_and_backfills_gaps(live):
    data, clock_feed = live["data"], live["feed"]
    feed = clock_feed
    data.warm_up(at(10, 0, 30))            # history up to 09:59
    data.mark_live(at(10, 0, 40))          # feed covers 10:01 onwards; 10:00 must be back-filled
    feed.subscribe([UL.fyers_symbol, data.fut_sym])
    for m in (at(10, 1), at(10, 2)):
        clock_feed.push_minute(m)
    data.advance(at(10, 2))
    sp = data.spot_bars(at(10, 2))
    today = sp[sp.index.normalize() == pd.Timestamp(DAY)]
    assert today.index[0] == pd.Timestamp(at(9, 15)) and today.index[-1] == pd.Timestamp(at(10, 2))
    assert len(today) == 48                                  # 09:15..10:02, no gap
    assert any(c[1] == at(10, 0) for c in live["client"].history_calls)   # the back-fill call
    src = live["store"].load_spot("NIFTY", DAY, DAY)
    assert sp.loc[pd.Timestamp(at(10, 1)), "close"] == src.loc[pd.Timestamp(at(10, 1)), "close"]
    fut = data.futures_bars(at(10, 2))
    assert sp.loc[pd.Timestamp(at(10, 1)), "volume"] == pytest.approx(fut.loc[pd.Timestamp(at(10, 1)), "volume"])
    flow = data.order_flow(at(10, 2))
    assert list(flow.index) == [pd.Timestamp(at(10, 1)), pd.Timestamp(at(10, 2))]
    assert {"buy_vol", "sell_vol", "delta"} <= set(flow.columns)


# ---- paper broker fills --------------------------------------------------------------------
def test_quote_fills_buy_at_ask_sell_at_bid(live, synthetic, tmp_path):
    from algo.backtest.costs import CostModel
    from algo.core.models import Order
    from algo.live.paper_broker import PaperBroker

    data, feed = live["data"], live["feed"]
    s = make_settings(synthetic["root"])
    c = data.contract(date(2025, 1, 9), 23000, OptionType.CE)
    data.mark_live(at(9, 59, 30))
    feed.push_minute(at(10, 0))
    q = data.quote(c, at(10, 0))
    broker = PaperBroker(data, CostModel(s.costs), fill_model="quote", fills_path=tmp_path / "fills.csv")
    broker.place_order(Order(contract=c, side=Side.BUY, lots=1, placed_at=at(10, 0)))
    broker.after_bar(at(10, 0))
    pos = broker.positions()[0]
    assert pos.entry_price == pytest.approx(q.ask, abs=0.026) and pos.entry_time == at(10, 1)
    broker.exit_position(pos)
    broker.after_bar(at(10, 0))
    t = broker.trades()[0]
    assert t.exit_price == pytest.approx(q.bid, abs=0.026)
    fills = pd.read_csv(tmp_path / "fills.csv")
    assert list(fills["side"]) == ["BUY", "SELL"] and (fills["slip_vs_ltp"].sub(0.5).abs() < 0.03).all()


# ---- order-flow gate ------------------------------------------------------------------------
class FlowData(HistoricalData):
    delta = [0.0]

    def order_flow(self, until, tf="1min", symbol=None):
        idx = pd.date_range(pd.Timestamp(until) - pd.Timedelta(minutes=len(self.delta) - 1), periods=len(self.delta),
                            freq="1min")
        return pd.DataFrame({"volume": 1.0, "buy_vol": 0.0, "sell_vol": 0.0, "delta": self.delta}, index=idx)


def test_order_flow_gate_blocks_entries_against_delta(synthetic, settings):
    from algo.backtest.costs import CostModel
    from algo.backtest.sim_broker import SimBroker

    data = FlowData(synthetic["store"], UL, DAY, DAY)
    ctx = Context(UL, data, SimBroker(data, CostModel(settings.costs)),
                  flow_gate=OrderFlowGate({"enabled": True, "bars": 2, "min_delta": 100}))
    ctx.now = at(10, 0)
    ce = ctx.select_option(OptionType.CE)
    pe = ctx.select_option(OptionType.PE)
    data.delta = [-50.0, -200.0]           # selling pressure
    assert ctx.buy(ce) is None             # bullish entry blocked
    assert ctx.sell(pe) is None            # selling a PE is bullish too
    assert ctx.buy(pe) is not None         # bearish entry allowed
    assert ctx.open_debit_spread(OptionType.PE, moneyness="ITM2") is not None   # bearish
    assert ctx.open_debit_spread(OptionType.CE, moneyness="ITM2") is None       # bullish
    assert ctx.risk.blocked["order_flow"] == 3
    data.delta = [40.0, 50.0]              # +90 < min_delta 100
    assert ctx.buy(ce) is None
    data.delta = [60.0]                    # fewer bars than required: no blind entries
    assert ctx.buy(ce) is None


def test_context_order_flow_adds_cumulative_delta(synthetic):
    data = FlowData(synthetic["store"], UL, DAY, DAY)
    data.delta = [10.0, -4.0, 6.0]
    ctx = Context(UL, data, None)
    ctx.now = at(10, 0)
    assert list(ctx.order_flow()["cum_delta"]) == [10.0, 6.0, 12.0]
    bt = Context(UL, HistoricalData(synthetic["store"], UL, DAY, DAY), None)
    bt.now = at(10, 0)
    with pytest.raises(NotImplementedError):
        bt.order_flow()


# ---- full paper day on a fake clock ------------------------------------------------------
def _paper_day(live, synthetic, tmp_path, fill_model, start=at(9, 10)):
    strat = get_strategy("orb_buy")
    s = make_settings(synthetic["root"])
    store = PaperStore(tmp_path / "paper")
    clock = Clock(start, live["feed"])
    PaperRunner(strat, UL, s, live["data"], live["feed"], store, fill_model=fill_model,
                now_fn=clock.now, sleep_fn=clock.sleep, echo=lambda *_: None).run()
    return store


def test_paper_day_bar_open_matches_backtest(live, synthetic, tmp_path):
    store = _paper_day(live, synthetic, tmp_path, "bar_open")
    paper = [t.to_dict() for t in store.trades()]
    bt = run_backtest(get_strategy("orb_buy"), UL, DAY, DAY, make_settings(synthetic["root"]),
                      data=HistoricalData(synthetic["store"], UL, DAY, DAY))
    expected = [t.to_dict() for t in bt.trades]
    assert expected, "the synthetic day should produce trades"
    for d in paper + expected:
        d["entry_time"], d["exit_time"] = pd.Timestamp(d["entry_time"]), pd.Timestamp(d["exit_time"])
        d.pop("group")   # order ids come from a process-wide counter
    assert paper == expected
    st = store.load()
    assert st.day == DAY.isoformat() and st.day_closed and st.positions == []
    assert st.realised_before_day == pytest.approx(sum(t.net_pnl for t in bt.trades))
    assert store.events_path(DAY).exists()


def test_paper_day_quote_fills_at_ask(live, synthetic, tmp_path):
    store = _paper_day(live, synthetic, tmp_path, "quote")
    trades = store.trades()
    assert trades
    for t in trades:
        bar = synthetic["store"].load_option("NIFTY", t.contract.expiry, t.contract.strike, t.contract.option_type.value)
        prev_close = bar.loc[pd.Timestamp(t.entry_time) - pd.Timedelta(minutes=1), "close"]
        assert t.entry_price == pytest.approx(prev_close + 0.5, abs=0.026)    # bought at the ask
    assert store.fills_path.exists()
    live_snap = json.loads((store.root / "live.json").read_text())
    assert live_snap["day_closed"] and live_snap["positions"] == []
    assert len(live_snap["spot"]) > 300 and {"time", "open", "high", "low", "close"} <= set(live_snap["spot"][0])
    assert len(live_snap["trades"]) == len(trades)
    assert live_snap["realised_today"] == pytest.approx(sum(t.net_pnl for t in trades), abs=0.05)
    assert live_snap["equity"] == pytest.approx(store.load().initial_capital + live_snap["realised_today"], abs=0.05)


def test_closed_day_is_not_rerun_and_other_strategy_needs_reset(live, synthetic, tmp_path):
    store = _paper_day(live, synthetic, tmp_path, "bar_open")
    n = len(store.trades())
    _paper_day(live, synthetic, tmp_path, "bar_open", start=at(11, 0))   # same day again: no-op
    assert len(store.trades()) == n
    with pytest.raises(RuntimeError, match="--reset"):
        PaperRunner(get_strategy("orb_buy", sl_pct=10.0), UL, make_settings(synthetic["root"]), live["data"],
                    live["feed"], store, now_fn=lambda: at(9, 0, d=date(2025, 1, 8))).run()


def test_restart_same_day_restores_positions_and_trades(live, synthetic, tmp_path):
    store = PaperStore(tmp_path / "paper")
    c = live["uni"].contract(date(2025, 1, 9), 23000, OptionType.CE)
    pos = Position(contract=c, side=Side.BUY, lots=1, entry_price=50.0, entry_time=at(9, 40), sl_price=35.0,
                   tag="x", group="g1")
    strat = get_strategy("orb_buy")
    store.save(PaperState(strategy=strat.name, underlying="NIFTY", params=dict(strat.p), initial_capital=10000,
                          day=DAY.isoformat(), realised_before_day=500.0, positions=[position_to_dict(pos)]))
    runner = PaperRunner(strat, UL, make_settings(synthetic["root"]), live["data"], live["feed"], store,
                         echo=lambda *_: None)
    runner.today = DAY
    runner.state = store.load()
    session = runner._build_session(runner.state, DAY)
    restored = session.broker.positions()
    assert len(restored) == 1 and restored[0].sl_price == 35.0 and restored[0].contract == c
    assert session.cm.equity == pytest.approx(10500)
    assert session.risk._entries == 1


def test_previous_day_crash_drops_stale_intraday_positions(live, synthetic, tmp_path):
    store = PaperStore(tmp_path / "paper")
    c = live["uni"].contract(date(2025, 1, 9), 23000, OptionType.CE)
    pos = Position(contract=c, side=Side.BUY, lots=1, entry_price=50.0, entry_time=at(9, 40), group="g1")
    strat = get_strategy("orb_buy")
    store.save(PaperState(strategy=strat.name, underlying="NIFTY", params=dict(strat.p), initial_capital=10000,
                          day=DAY.isoformat(), positions=[position_to_dict(pos)]))
    runner = PaperRunner(strat, UL, make_settings(synthetic["root"]), live["data"], live["feed"], store,
                         echo=lambda *_: None)
    nxt = date(2025, 1, 8)
    runner.today = nxt
    runner.state = store.load()
    session = runner._build_session(runner.state, nxt)
    assert session.broker.positions() == []
    assert store.events_path(nxt).exists()


def test_tick_recorder_and_footprint(tmp_path):
    import pandas as pd
    from algo.live.orderflow import TickRecorder, footprint
    rec = TickRecorder(tmp_path, want=lambda s: s == "FUT")
    b = BarBuilder(recorder=rec)
    t0 = datetime(2026, 11, 3, 9, 15, 1)
    b.on_tick("FUT", t0, 100.0, volume=1000, bid=99.9, ask=100.0)
    b.on_tick("SPOT", t0, 50.0, volume=10)                                           # not recorded
    b.on_tick("FUT", t0.replace(second=2), 100.0, volume=1075, bid=99.9, ask=100.0)  # lifts ask: buy 75
    b.on_tick("FUT", t0.replace(second=3), 99.9, volume=1100, bid=99.9, ask=100.0)   # hits bid: sell 25
    b.on_tick("FUT", t0.replace(minute=16), 101.2, volume=1150, bid=101.1, ask=101.2)  # next bar: buy 50
    ticks = pd.read_csv(tmp_path / "2026-11-03.csv")
    assert len(ticks) == 4 and set(ticks["symbol"]) == {"FUT"}
    fp = footprint(ticks, step=1.0)
    assert fp.loc[(pd.Timestamp("2026-11-03 09:15"), 100.0)].tolist() == [75.0, 25.0, 50.0]
    assert fp.loc[(pd.Timestamp("2026-11-03 09:16"), 101.0), "buy"] == 50.0
    assert fp["buy"].sum() - fp["sell"].sum() == b.bars("FUT")["delta"].sum()


def test_live_json_shows_open_position_midday(live, synthetic, tmp_path, monkeypatch):
    seen = []
    orig = PaperRunner._write_live_snapshot

    def spy(self):
        orig(self)
        snap = json.loads((self.store.root / "live.json").read_text())
        if snap["positions"]:
            seen.append(snap)
    monkeypatch.setattr(PaperRunner, "_write_live_snapshot", spy)
    _paper_day(live, synthetic, tmp_path, "quote")
    assert seen, "the synthetic day should hold a position at some minute"
    p = seen[0]["positions"][0]
    assert p["ltp"] is not None and p["pnl"] is not None and p["bars"]
    assert seen[0]["unrealised"] == pytest.approx(sum(x["pnl"] for x in seen[0]["positions"]))


def test_only_one_tick_recorder_writes_per_folder(tmp_path):
    import pandas as pd
    from algo.live.orderflow import TickRecorder
    first, second = TickRecorder(tmp_path, want=lambda s: True), TickRecorder(tmp_path, want=lambda s: True)
    t = datetime(2026, 11, 4, 9, 15, 1)
    first.write("FUT", t, 100.0, 99.9, 100.0, 1000, 0.0, 0.0, 0.0)
    second.write("FUT", t, 100.0, 99.9, 100.0, 1000, 0.0, 0.0, 0.0)    # a 2nd paper process: skipped
    first.write("FUT", t.replace(second=2), 100.1, 100.0, 100.1, 1010, 10.0, 10.0, 0.0)
    assert len(pd.read_csv(tmp_path / "2026-11-04.csv")) == 2


def test_dashboards_take_the_next_free_port(tmp_path):
    import threading
    import urllib.request
    from algo.cli import _dashboard
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    (tmp_path / "a" / "live.json").write_text('{"strategy": "a"}')
    a, url_a = _dashboard(tmp_path / "a", 18770)
    b, url_b = _dashboard(tmp_path / "b", 18770)      # a second paper session: port taken -> next one
    try:
        for s in (a, b):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        assert url_a != url_b
        assert json.loads(urllib.request.urlopen(url_a + "/live.json").read()) == {"strategy": "a"}
        assert b"<title>" in urllib.request.urlopen(url_b + "/").read()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(url_a + "/state.json")
    finally:
        a.shutdown(), b.shutdown()
