"""Paper-trading loop: live Fyers data, the backtest's TradingSession, simulated fills.

Every minute, `poll_delay_s` seconds after the boundary, the bar that just closed is made
visible (`LiveMarketData.advance`) and processed by the same `TradingSession.step` the backtest
uses; then new orders fill at the live quote (PaperBroker). State is saved after every minute.

Restarts:
  * same day: open positions and today's closed trades are restored, so SL / target /
    square-off and the risk limits (trades per day, daily loss) keep working. A strategy's own
    in-memory state (ctx.state) is lost, so its signal-based exits may not fire for positions
    opened before the restart; the engine still squares them off at square_off_time.
  * next day: capital, realised P&L and withdrawals carry over (the withdrawal / kill-switch
    rules work across weeks of paper trading).
Ctrl-C squares off intraday positions at the live quote and closes the day.
"""
from __future__ import annotations

import json
import logging
import os
import time as _time
from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from algo.backtest.costs import CostModel
from algo.backtest.engine import TradingSession
from algo.config import Settings, Underlying
from algo.core.flow_gate import OrderFlowGate
from algo.core.models import Trade
from algo.core.strategy import Strategy
from algo.live.data import LiveMarketData
from algo.live.paper_broker import PaperBroker
from algo.live.state import (PaperState, PaperStore, position_from_dict, position_to_dict,
                             withdrawals_from, withdrawals_to)

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
OPEN, LAST_BAR, CLOSE = time(9, 15), time(15, 29), time(15, 30)


def ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


class PaperRunner:
    def __init__(self, strategy: Strategy, underlying: Underlying, settings: Settings, data: LiveMarketData,
                 feed, store: PaperStore, capital: float | None = None, fill_model: str | None = None,
                 reset: bool = False, now_fn: Callable[[], datetime] = ist_now,
                 sleep_fn: Callable[[float], None] = _time.sleep, echo: Callable[[str], None] = print) -> None:
        self.strategy, self.ul, self.settings, self.data, self.feed = strategy, underlying, settings, data, feed
        self.store, self.now_fn, self.sleep, self.echo = store, now_fn, sleep_fn, echo
        self.cfg = settings.paper
        self.fill_model = fill_model or self.cfg.get("fill_model", "quote")
        self.poll_delay = float(self.cfg.get("poll_delay_s", 2))
        self.capital_arg, self.reset = capital, reset
        self.session: TradingSession | None = None
        self.state: PaperState | None = None
        self.today: date | None = None
        self._known_positions: set[int] = set()

    # ---- setup ------------------------------------------------------------------
    def _load_state(self, today: date) -> PaperState:
        st = self.store.load()
        s = self.strategy
        if st is not None and not st.matches(s.name, self.ul.name, s.p):
            if not self.reset:
                raise RuntimeError(
                    f"{self.store.state_path} belongs to {st.strategy} {st.underlying} {st.params}. "
                    "Pass --reset to archive it and start a new paper account.")
        if st is not None and self.reset:
            self.store.reset()
            st = None
        if st is None:
            cap = float(self.capital_arg if self.capital_arg is not None
                        else self.settings.backtest["initial_capital"])
            st = PaperState(strategy=s.name, underlying=self.ul.name, params=dict(s.p), initial_capital=cap,
                            started=today.isoformat())
            self.store.save(st)
            self.echo(f"New paper account: {s.name} on {self.ul.name}, capital {cap:,.0f}")
        elif self.capital_arg is not None and float(self.capital_arg) != st.initial_capital:
            log.warning("ignoring --capital %.0f: existing paper account started with %.0f (use --reset)",
                        self.capital_arg, st.initial_capital)
        return st

    def _build_session(self, st: PaperState, today: date) -> TradingSession:
        trades_all = self.store.trades()
        positions = [position_from_dict(d) for d in st.positions]
        day = date.fromisoformat(st.day) if st.day else None
        today_trades: list[Trade] = []
        if day is not None and not st.day_closed:
            day_trades = [t for t in trades_all if t.exit_time.date() == day]
            if day == today:
                today_trades = day_trades
            else:   # the process died on an earlier day before closing it
                st.realised_before_day += sum(t.net_pnl for t in day_trades)
                if self.strategy.intraday and positions:
                    self.echo(f"WARNING: dropping {len(positions)} intraday position(s) left open on {day} "
                              "(the session ended without a square-off); their P&L is not counted.")
                    self.store.event(today, "stale_positions_dropped", from_day=day, positions=st.positions)
                    positions = []
                st.day, st.day_closed = None, False
        ex = self.settings.execution
        broker = PaperBroker(self.data, CostModel(self.settings.costs), ex["slippage_pct"], ex["slippage_min"],
                             fill_model=self.fill_model, fills_path=self.store.fills_path,
                             on_fill=lambda row: self.store.event(self.today, "fill", **row))
        gate = OrderFlowGate(self.cfg.get("order_flow_gate"))
        session = TradingSession(self.strategy, self.ul, self.settings, self.data, st.initial_capital,
                                 broker=broker, realised_offset=st.realised_before_day,
                                 flow_gate=gate if gate.enabled else None)
        cm = session.cm
        cm.withdrawals = withdrawals_from(st)
        if cm.withdrawals:
            w = cm.withdrawals[-1]
            cm._reference = w.equity_before - w.amount
        cm.halted_on = date.fromisoformat(st.halted_on) if st.halted_on else None
        cm.realised = st.realised_before_day + sum(t.net_pnl for t in today_trades)
        session.sync_capital()
        broker.restore(positions, today_trades)
        if today_trades or positions:
            # risk limits count today's entries (a spread = one entry)
            groups = {t.group for t in today_trades if t.entry_time.date() == today}
            groups |= {p.group for p in positions if p.entry_time.date() == today}
            session.risk._day, session.risk._entries = today, len(groups)
            self.echo(f"Restored {len(positions)} open position(s) and {len(today_trades)} closed trade(s) "
                      "from today. Strategy memory is not restored: SL/target/square-off still apply.")
        prev_cb = broker.on_trade_closed
        def on_closed(t: Trade) -> None:
            self.store.append_trade(t)
            self.store.event(self.today, "trade_closed", **{k: v for k, v in t.to_dict().items()})
            self.echo(f"{t.exit_time:%H:%M} EXIT  {t.contract.symbol} {t.side.value} x{t.quantity} "
                      f"{t.entry_price:.2f} -> {t.exit_price:.2f} ({t.exit_reason.value})  net {t.net_pnl:+,.0f}")
            if prev_cb:
                prev_cb(t)
        broker.on_trade_closed = on_closed
        self._known_positions = {id(p) for p in positions}
        return session

    # ---- per minute ---------------------------------------------------------------
    def _persist(self) -> None:
        st, s = self.state, self.session
        st.day = self.today.isoformat()
        st.positions = [position_to_dict(p) for p in s.broker.positions()]
        st.withdrawals = withdrawals_to(s.cm.withdrawals)
        st.halted_on = s.cm.halted_on.isoformat() if s.cm.halted_on else None
        self.store.save(st)

    def _write_live(self) -> None:
        try:
            self._write_live_snapshot()
        except Exception:   # noqa: BLE001 - the dashboard must never break trading
            log.exception("could not write live.json")

    def _write_live_snapshot(self) -> None:
        """Snapshot for `algo paper-ui` (live.json). Chart times are IST wall-clock as epoch seconds."""
        def ep(t) -> int:
            return int(pd.Timestamp(t).timestamp())

        def candles(df: pd.DataFrame) -> list[dict]:
            df = df[df.index.normalize() == pd.Timestamp(self.today)]
            return [{"time": ep(t), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
                    for t, r in df.iterrows()]

        s, b = self.session, self.data.builder
        positions, unreal = [], 0.0
        for p in s.broker.positions():
            q = b.quote(p.contract.instrument_key)
            pnl = p.unrealized(q.ltp) if q else None
            unreal += pnl or 0.0
            positions.append({**position_to_dict(p), "symbol": p.contract.symbol, "side": p.side.value,
                              "qty": p.quantity, "entry_ts": ep(p.entry_time), "ltp": q.ltp if q else None,
                              "pnl": pnl, "bars": candles(b.bars(p.contract.instrument_key))})
        trades = [t.to_dict() | {"entry_ts": ep(t.entry_time), "exit_ts": ep(t.exit_time)}
                  for t in s.broker.trades() if t.exit_time.date() == self.today]
        ev = self.store.events_path(self.today)
        events = [json.loads(line) for line in ev.read_text().splitlines()[-50:]] if ev.exists() else []
        snap = {
            "ts": self.now_fn().isoformat(timespec="seconds"), "strategy": self.strategy.name,
            "underlying": self.ul.name, "params": self.strategy.p, "fill_model": self.fill_model,
            "initial_capital": self.state.initial_capital, "withdrawn": s.cm.withdrawn,
            # cm.equity only books realised P&L at day close; this is the same total, live
            "equity": s.cm.initial + s.realised_offset + sum(t.net_pnl for t in s.broker.trades()) - s.cm.withdrawn,
            "realised_today": sum(t["net_pnl"] for t in trades), "unrealised": unreal,
            "halted_on": s.cm.halted_on, "day_closed": self.state.day_closed,
            "feed_messages": getattr(self.feed, "messages", 0),
            "spot": candles(self.data.spot_1m), "positions": positions, "trades": trades, "events": events,
        }
        path = self.store.root / "live.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, default=str))
        os.replace(tmp, path)

    def try_step(self, ts: datetime, now: datetime) -> bool:
        """Process minute ts if its spot bar has arrived (or it is > 3 min old: a data gap).
        False = not yet, try again at the next poll."""
        self.data.advance(ts)
        sp = self.data.spot_1m
        if (not len(sp) or sp.index[-1] != pd.Timestamp(ts)) and now - ts < timedelta(minutes=3):
            return False
        try:
            self.step(ts)
        except Exception as e:   # noqa: BLE001 - one bad minute must not end the session
            log.exception("error processing %s", ts)
            self.store.event(self.today, "error", ts=ts, error=repr(e))
            self.echo(f"{ts:%H:%M} ERROR {e!r} (see log); continuing")
        return True

    def step(self, ts: datetime) -> None:
        blocked_before = dict(self.session.risk.blocked)
        self.session.step(pd.Timestamp(ts))
        for k, v in self.session.risk.blocked.items():
            if v > blocked_before.get(k, 0):
                self.store.event(self.today, "entry_blocked", ts=ts, rule=k)
                self.echo(f"{ts:%H:%M} entry blocked: {k}")
        for p in self.session.broker.positions():
            if id(p) not in self._known_positions:
                self._known_positions.add(id(p))
                self.store.event(self.today, "entry", ts=p.entry_time, **position_to_dict(p))
                self.echo(f"{p.entry_time:%H:%M} ENTRY {p.contract.symbol} {p.side.value} x{p.quantity} "
                          f"@ {p.entry_price:.2f}" + (f"  SL {p.sl_price}" if p.sl_price else "")
                          + (f"  TGT {p.target_price}" if p.target_price else ""))
        self._persist()
        self._write_live()

    def _close_day(self) -> None:
        s = self.session
        s.end_day()
        self.state.realised_before_day = s.cm.realised
        self.state.day_closed = True
        self._persist()
        self._write_live()
        today = [t for t in s.broker.trades() if t.exit_time.date() == self.today]
        net = sum(t.net_pnl for t in today)
        self.store.event(self.today, "day_closed", trades=len(today), net=net, equity=s.cm.equity)
        self.echo(f"Day closed: {len(today)} trade(s), net {net:+,.0f}, equity {s.cm.equity:,.0f}"
                  + (f"  (KILL SWITCH since {s.cm.halted_on})" if s.cm.halted else ""))

    def _recentre(self, around: int, n_exp: int) -> None:
        spot = self.data.last_spot()
        if spot:
            self.feed.subscribe(self.data.subscriptions(spot, self.today, around, n_exp))

    # ---- main loop ------------------------------------------------------------------
    def run(self) -> None:
        now = self.now_fn()
        self.today = today = now.date()
        if today.weekday() >= 5:
            self.echo("Weekend: market closed.")
            return
        if now.time() >= CLOSE:
            self.echo("Market already closed for today (15:30).")
            return
        self.state = self._load_state(today)
        if self.state.day == today.isoformat() and self.state.day_closed:
            self.echo("Today's paper session is already closed. Run `algo paper-report` for results.")
            return
        self.session = self._build_session(self.state, today)
        if now.time() < time(9, 14):
            wait = (datetime.combine(today, time(9, 14)) - now).total_seconds()
            self.echo(f"Waiting {wait / 60:.0f} min for the market to open...")
            self.sleep(wait)
        now = self.now_fn()
        self.data.warm_up(now)
        around, n_exp = int(self.cfg.get("strikes_around", 10)), int(self.cfg.get("expiries", 1))
        spot = self.data.last_spot()
        subs = self.data.subscriptions(spot, today, around, n_exp) if spot else [self.data.spot_sym]
        self.feed.start(subs)
        self.data.mark_live(self.now_fn())
        self.echo(f"Paper trading {self.strategy.name} on {self.ul.name} ({len(self.feed.subscribed)} symbols, "
                  f"fills: {self.fill_model}, equity {self.session.cm.equity:,.0f}). Ctrl-C to stop.")
        self.session.start()

        first = datetime.combine(today, OPEN)
        nxt = max(first, pd.Timestamp(self.now_fn()).floor("min").to_pydatetime() - timedelta(minutes=1))
        last_bar = datetime.combine(today, LAST_BAR)
        try:
            while nxt <= last_bar:
                due = nxt + timedelta(minutes=1, seconds=self.poll_delay)
                wait = (due - self.now_fn()).total_seconds()
                if wait > 0:
                    self.sleep(wait)
                now = self.now_fn()
                closed = pd.Timestamp(now).floor("min").to_pydatetime() - timedelta(minutes=1)
                while nxt <= min(closed, last_bar) and self.try_step(nxt, now):
                    nxt += timedelta(minutes=1)
                if self.session.cur_day is None and self._holiday(today):
                    self.echo("No market data by 09:20: treating today as a holiday.")
                    return
                if nxt <= closed:   # waiting for a late bar: poll again shortly
                    self.sleep(1)
                    continue
                if nxt.minute % 5 == 0:
                    self._recentre(around, n_exp)
                silent = getattr(self.feed, "seconds_since_message", lambda: 0.0)()
                if silent > 60 and hasattr(self.feed, "resubscribe"):
                    self.echo(f"{nxt:%H:%M} no feed data for {silent:.0f}s: resubscribing (REST back-fill covers the gap)")
                    self.store.event(self.today, "feed_silent", seconds=silent)
                    self.feed.resubscribe()
                if nxt.minute % 15 == 0:
                    eq = self.session.cm.equity
                    self.echo(f"{nxt:%H:%M} heartbeat: {len(self.session.broker.positions())} open, "
                              f"equity {eq:,.0f}, feed msgs {getattr(self.feed, 'messages', 0)}")
        except KeyboardInterrupt:
            self.echo("\nStopping: squaring off intraday positions and closing the day.")
        finally:
            try:
                if self.session.cur_day is not None:
                    self._close_day()
            finally:
                self.feed.stop()

    def _holiday(self, today: date) -> bool:
        if self.now_fn().time() < time(9, 20):
            return False
        sp = self.data.spot_1m
        return not len(sp) or sp.index[-1].date() != today
