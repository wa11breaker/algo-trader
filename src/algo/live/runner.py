"""Paper-trading loop: live Fyers data, the backtest's TradingSession, simulated fills.

One process runs one or more independent paper accounts (PaperAccount: its own strategy, capital,
trades and state files under paper/<account>_<UL>/) on one shared Fyers feed. Every minute,
`poll_delay_s` seconds after the boundary, the bar that just closed is made visible
(`LiveMarketData.advance`) and processed by each account's `TradingSession.step` (the same code the
backtest uses); new orders fill at the live quote (PaperBroker). State is saved after every minute.

Restarts (systemd restart, server reboot, Ctrl-C):
  * stopped before the close: the day stays open. Open positions, today's closed trades and the
    strategy's own memory (ctx.state, e.g. fvg_flow's index stop / target plans) are saved every
    minute and restored by a restart the same day, so SL / target / square-off keep working.
  * the day is closed (positions squared off, day P&L booked) only after the last bar (15:29).
  * next day: capital, realised P&L and withdrawals carry over.
"""
from __future__ import annotations

import json
import logging
import os
import pickle
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


class PaperAccount:
    """One strategy's paper account: state files, TradingSession, per-minute bookkeeping."""

    def __init__(self, strategy: Strategy, store: PaperStore, capital: float | None = None,
                 reset: bool = False, name: str | None = None) -> None:
        self.strategy, self.store, self.capital_arg, self.reset = strategy, store, capital, reset
        self.name = name or strategy.name
        self.session: TradingSession | None = None
        self.state: PaperState | None = None
        self._known_positions: set[int] = set()
        self.r: PaperRunner | None = None     # set by PaperRunner

    def echo(self, msg: str) -> None:
        self.r.echo(f"[{self.name}] {msg}" if len(self.r.accounts) > 1 else msg)

    @property
    def today(self) -> date:
        return self.r.today

    # ---- setup ------------------------------------------------------------------
    def _load_state(self, today: date) -> PaperState:
        st = self.store.load()
        s, ul = self.strategy, self.r.ul
        if st is not None and not st.matches(s.name, ul.name, s.p):
            if not self.reset:
                raise RuntimeError(
                    f"{self.store.state_path} belongs to {st.strategy} {st.underlying} {st.params}. "
                    "Pass --reset to archive it and start a new paper account.")
        if st is not None and self.reset:
            self.store.reset()
            st = None
        if st is None:
            cap = float(self.capital_arg if self.capital_arg is not None
                        else self.r.settings.backtest["initial_capital"])
            st = PaperState(strategy=s.name, underlying=ul.name, params=dict(s.p), initial_capital=cap,
                            started=today.isoformat())
            self.store.save(st)
            self.echo(f"New paper account: {s.name} on {ul.name}, capital {cap:,.0f}")
        elif self.capital_arg is not None and float(self.capital_arg) != st.initial_capital:
            log.warning("%s: ignoring --capital %.0f: existing paper account started with %.0f (use --reset)",
                        self.name, self.capital_arg, st.initial_capital)
        return st

    def open(self, today: date) -> bool:
        """Load the account for `today`. False if today's session is already closed."""
        self.state = self._load_state(today)
        if self.state.day == today.isoformat() and self.state.day_closed:
            self.echo("Today's paper session is already closed. Run `algo paper-report` for results.")
            return False
        self.session = self._build_session(self.state, today)
        return True

    def _build_session(self, st: PaperState, today: date) -> TradingSession:
        r = self.r
        trades_all = self.store.trades()
        positions = [position_from_dict(d) for d in st.positions]
        day = date.fromisoformat(st.day) if st.day else None
        today_trades: list[Trade] = []
        resumed = False
        if day is not None and not st.day_closed:
            day_trades = [t for t in trades_all if t.exit_time.date() == day]
            if day == today:
                today_trades, resumed = day_trades, True
            else:   # the process died on an earlier day before closing it
                st.realised_before_day += sum(t.net_pnl for t in day_trades)
                if self.strategy.intraday and positions:
                    self.echo(f"WARNING: dropping {len(positions)} intraday position(s) left open on {day} "
                              "(the session ended without a square-off); their P&L is not counted.")
                    self.store.event(today, "stale_positions_dropped", from_day=day, positions=st.positions)
                    positions = []
                st.day, st.day_closed = None, False
        ex = r.settings.execution
        broker = PaperBroker(r.data, CostModel(r.settings.costs), ex["slippage_pct"], ex["slippage_min"],
                             fill_model=r.fill_model, fills_path=self.store.fills_path,
                             on_fill=lambda row: self.store.event(self.today, "fill", **row))
        gate = OrderFlowGate(r.cfg.get("order_flow_gate"))
        session = TradingSession(self.strategy, r.ul, r.settings, r.data, st.initial_capital,
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
        if resumed:
            session.resume_state = self._load_ctx(today)
        if today_trades or positions:
            # risk limits count today's entries (a spread = one entry)
            groups = {t.group for t in today_trades if t.entry_time.date() == today}
            groups |= {p.group for p in positions if p.entry_time.date() == today}
            session.risk._day, session.risk._entries = today, len(groups)
            self.echo(f"Restored {len(positions)} open position(s), {len(today_trades)} closed trade(s)"
                      + (" and the strategy's memory" if session.resume_state else "") + " from today.")
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

    # ---- strategy memory (ctx.state) ----------------------------------------------
    def _ctx_path(self, day: date):
        return self.store.root / f"ctx-{day}.pkl"

    def _save_ctx(self) -> None:
        if self.session.cur_day is None:
            return
        try:
            path = self._ctx_path(self.today)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(pickle.dumps(self.session.ctx.state))
            os.replace(tmp, path)
        except Exception:   # noqa: BLE001 - an unpicklable strategy state must not stop trading
            log.exception("%s: could not save the strategy state", self.name)

    def _load_ctx(self, day: date) -> dict | None:
        path = self._ctx_path(day)
        if not path.exists():
            return None
        try:
            return pickle.loads(path.read_bytes())   # our own file, written by _save_ctx
        except Exception:   # noqa: BLE001
            log.exception("%s: could not load %s", self.name, path)
            return None

    # ---- per minute ---------------------------------------------------------------
    def persist(self) -> None:
        st, s = self.state, self.session
        st.day = self.today.isoformat()
        st.positions = [position_to_dict(p) for p in s.broker.positions()]
        st.withdrawals = withdrawals_to(s.cm.withdrawals)
        st.halted_on = s.cm.halted_on.isoformat() if s.cm.halted_on else None
        self.store.save(st)
        self._save_ctx()

    def write_live(self) -> None:
        try:
            self._write_live_snapshot()
        except Exception:   # noqa: BLE001 - the dashboard must never break trading
            log.exception("%s: could not write live.json", self.name)

    def _write_live_snapshot(self) -> None:
        """Snapshot for the dashboard (live.json). Chart times are IST wall-clock as epoch seconds."""
        r = self.r

        def ep(t) -> int:
            return int(pd.Timestamp(t).timestamp())

        def candles(df: pd.DataFrame) -> list[dict]:
            df = df[df.index.normalize() == pd.Timestamp(self.today)]
            return [{"time": ep(t), "open": row.open, "high": row.high, "low": row.low, "close": row.close}
                    for t, row in df.iterrows()]

        s, b = self.session, r.data.builder
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
        events = [json.loads(line) for line in ev.read_text().splitlines()[-80:]] if ev.exists() else []
        snap = {
            "ts": r.now_fn().isoformat(timespec="seconds"), "account": self.name, "strategy": self.strategy.name,
            "underlying": r.ul.name, "params": self.strategy.p, "fill_model": r.fill_model,
            "initial_capital": self.state.initial_capital, "withdrawn": s.cm.withdrawn,
            # cm.equity only books realised P&L at day close; this is the same total, live
            "equity": s.cm.initial + s.realised_offset + sum(t.net_pnl for t in s.broker.trades()) - s.cm.withdrawn,
            "realised_today": sum(t["net_pnl"] for t in trades), "unrealised": unreal,
            "halted_on": s.cm.halted_on, "day_closed": self.state.day_closed,
            "feed_messages": getattr(r.feed, "messages", 0),
            "spot": candles(r.data.spot_1m), "positions": positions, "trades": trades, "events": events,
        }
        path = self.store.root / "live.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, default=str))
        os.replace(tmp, path)

    def step(self, ts: datetime) -> None:
        try:
            self._step(ts)
        except Exception as e:   # noqa: BLE001 - one bad minute (or account) must not end the session
            log.exception("%s: error processing %s", self.name, ts)
            self.store.event(self.today, "error", ts=ts, error=repr(e))
            self.echo(f"{ts:%H:%M} ERROR {e!r} (see log); continuing")

    def _step(self, ts: datetime) -> None:
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
        self.persist()
        self.write_live()

    def close_day(self) -> None:
        s = self.session
        s.end_day()
        self.state.realised_before_day = s.cm.realised
        self.state.day_closed = True
        self.persist()
        self.write_live()
        today = [t for t in s.broker.trades() if t.exit_time.date() == self.today]
        net = sum(t.net_pnl for t in today)
        self.store.event(self.today, "day_closed", trades=len(today), net=net, equity=s.cm.equity)
        self.echo(f"Day closed: {len(today)} trade(s), net {net:+,.0f}, equity {s.cm.equity:,.0f}"
                  + (f"  (KILL SWITCH since {s.cm.halted_on})" if s.cm.halted else ""))

    def stop_open(self) -> None:
        """Stopped before the close: keep the day open so a restart today resumes."""
        self.persist()
        self.write_live()
        self.echo(f"Stopped at {self.r.now_fn():%H:%M} before the close: day left open, positions and "
                  "strategy memory saved. Restart today to resume.")


class PaperRunner:
    """Shared clock + feed loop for one or more PaperAccounts on one underlying."""

    def __init__(self, accounts: list[PaperAccount], underlying: Underlying, settings: Settings,
                 data: LiveMarketData, feed, fill_model: str | None = None,
                 now_fn: Callable[[], datetime] = ist_now, sleep_fn: Callable[[float], None] = _time.sleep,
                 echo: Callable[[str], None] = print) -> None:
        names = [a.name for a in accounts]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate paper account names: {names}")
        self.accounts, self.ul, self.settings, self.data, self.feed = accounts, underlying, settings, data, feed
        self.now_fn, self.sleep, self.echo = now_fn, sleep_fn, echo
        self.cfg = settings.paper
        self.fill_model = fill_model or self.cfg.get("fill_model", "quote")
        self.poll_delay = float(self.cfg.get("poll_delay_s", 2))
        self.today: date | None = None
        self.active: list[PaperAccount] = []
        for a in accounts:
            a.r = self

    def _event(self, name: str, **kw) -> None:
        for a in self.active:
            a.store.event(self.today, name, **kw)

    def try_step(self, ts: datetime, now: datetime) -> bool:
        """Process minute ts for every account if its spot bar has arrived (or it is > 3 min old:
        a data gap). False = not yet, try again at the next poll."""
        self.data.advance(ts)
        sp = self.data.spot_1m
        if (not len(sp) or sp.index[-1] != pd.Timestamp(ts)) and now - ts < timedelta(minutes=3):
            return False
        for a in self.active:
            a.step(ts)
        return True

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
        self.active = [a for a in self.accounts if a.open(today)]
        if not self.active:
            return
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
        for a in self.active:
            self.echo(f"Paper trading {a.name} ({a.strategy.name}) on {self.ul.name} "
                      f"({len(self.feed.subscribed)} symbols, fills: {self.fill_model}, "
                      f"equity {a.session.cm.equity:,.0f}). Ctrl-C to stop.")
            a.session.start()

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
                if all(a.session.cur_day is None for a in self.active) and self._holiday(today):
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
                    self._event("feed_silent", seconds=silent)
                    self.feed.resubscribe()
                if nxt.minute % 15 == 0:
                    for a in self.active:
                        a.echo(f"{nxt:%H:%M} heartbeat: {len(a.session.broker.positions())} open, "
                               f"equity {a.session.cm.equity:,.0f}, feed msgs {getattr(self.feed, 'messages', 0)}")
        except KeyboardInterrupt:
            self.echo("\nStopping.")
        finally:
            try:
                done = nxt > last_bar or self.now_fn().time() >= CLOSE
                for a in self.active:
                    if a.session.cur_day is None:
                        continue
                    try:
                        a.close_day() if done else a.stop_open()
                    except Exception:   # noqa: BLE001 - finish the other accounts
                        log.exception("%s: could not finish the day", a.name)
            finally:
                self.feed.stop()

    def _holiday(self, today: date) -> bool:
        if self.now_fn().time() < time(9, 20):
            return False
        sp = self.data.spot_1m
        return not len(sp) or sp.index[-1].date() != today
