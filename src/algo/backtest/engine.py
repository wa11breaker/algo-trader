"""Minute-by-minute backtest loop."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from algo.backtest.costs import CostModel
from algo.backtest.data import HistoricalData
from algo.backtest.sim_broker import SimBroker
from algo.config import Settings, Underlying
from algo.core.broker import MarketData
from algo.core.context import Context
from algo.core.models import ExitReason, Trade
from algo.core.strategy import Strategy

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    strategy: str
    underlying: str
    start: date
    end: date
    params: dict
    initial_capital: float
    trades: list[Trade]
    trading_days: list[date] = field(default_factory=list)
    cancelled_orders: int = 0
    diagnostics: pd.DataFrame | None = None   # optional per-trade details from the strategy
    risk_blocks: dict = field(default_factory=dict)   # entries blocked, by risk rule
    withdrawals: list = field(default_factory=list)   # CapitalManager Withdrawal records
    halted_on: date | None = None                     # kill switch date, if it fired
    skips: dict = field(default_factory=dict)         # optional: why a strategy skipped setups
    settings_snapshot: dict = field(default_factory=dict)   # costs/execution/risk/calendar used
    code_hash: str = ""                               # sha1 of src/algo/**/*.py (which code produced this)

    def trades_df(self) -> pd.DataFrame:
        return pd.DataFrame([t.to_dict() for t in self.trades])


def code_hash() -> str:
    """Short sha1 over the package source, so a report can be tied to the code that produced it."""
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha1()
    for p in sorted(root.rglob("*.py")):
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def _parse_time(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class TradingSession:
    """The per-minute trading loop shared by backtests and paper trading.

    Call `step(ts)` for every 1-min bar in time order, then `finish()`. Day boundaries,
    square-off, fills, strategy callbacks and the end-of-day capital update happen here, so a
    paper session behaves exactly like a backtest over the same bars.
    """

    def __init__(self, strategy: Strategy, underlying: Underlying, settings: Settings, data: MarketData,
                 capital: float, use_risk: bool | None = None, withdraw: bool = True,
                 calendar: dict | None = None, broker: SimBroker | None = None,
                 realised_offset: float = 0.0, flow_gate=None) -> None:
        """broker defaults to a SimBroker; realised_offset = net P&L realised before this session
        (paper trading across days), so the capital manager's cumulative P&L stays right;
        flow_gate: optional OrderFlowGate checked before entries (paper trading)."""
        from algo.core.capital import CapitalManager
        from algo.core.risk import RiskManager

        self.strategy, self.underlying, self.settings, self.data = strategy, underlying, settings, data
        self.capital = float(capital)
        self.risk_cfg = {**(settings.raw.get("risk") or {}), **strategy.risk_overrides}
        if use_risk is not None:
            self.risk_cfg["enabled"] = use_risk
        self.cal_cfg = {**(settings.raw.get("calendar") or {}), **(calendar or {})}
        self.risk = RiskManager(self.risk_cfg, self.capital, root=settings.root, calendar=self.cal_cfg)
        self.cm = CapitalManager.from_config(self.capital, settings.raw.get("capital"), self.risk_cfg,
                                             withdraw=withdraw)
        ex = settings.execution
        self.broker = broker or SimBroker(data, CostModel(settings.costs), ex["slippage_pct"], ex["slippage_min"])
        self.sq = _parse_time(ex["square_off_time"])
        self.ctx = Context(underlying, data, self.broker, square_off_time=self.sq, risk=self.risk, capital=self.cm,
                           flow_gate=flow_gate)
        self.realised_offset = realised_offset
        self.broker.on_trade_closed = lambda t: strategy.on_trade_closed(self.ctx, t)
        self.days: list[date] = []
        self.cur_day: date | None = None
        self.last_ts: datetime | None = None
        self.squared = False
        self.started = False

    def sync_capital(self) -> None:
        """Push the capital manager's equity / kill switch into the risk manager."""
        self.risk.capital, self.risk.halted = self.cm.equity, self.cm.halted

    def _close_day(self, day: date) -> None:
        self.cm.end_of_day(day, self.realised_offset + sum(t.net_pnl for t in self.broker.trades()))
        self.sync_capital()

    def _expiring(self, day: date):
        return lambda pos: pos.contract.expiry <= day

    def start(self) -> None:
        self.strategy.on_start(self.ctx)
        self.started = True

    def end_day(self) -> None:
        """Close the current day: square off what is still open (the session ended before
        square-off time), on_day_end, end-of-day capital update."""
        if self.cur_day is None:
            return
        b = self.broker
        if b.positions() or b.pending_orders():
            b.square_off(self.last_ts, ExitReason.SQUARE_OFF, use_open=False,
                         only=None if self.strategy.intraday else self._expiring(self.cur_day))
        self.strategy.on_day_end(self.ctx)
        self._close_day(self.cur_day)
        self.cur_day = None

    def step(self, ts) -> None:
        """Process the 1-min bar starting at ts (it has closed)."""
        if not self.started:
            self.start()
        ctx, b = self.ctx, self.broker
        d = ts.date()
        if d != self.cur_day:
            self.end_day()
            self.cur_day, self.squared = d, False
            self.days.append(d)
            ctx.now, ctx.state = ts, {}
            self.strategy.on_day_start(ctx)
        ctx.now = ts
        if not self.squared and ts.time() >= self.sq:
            b.square_off(ts, only=None if self.strategy.intraday else self._expiring(d))
            self.squared = True
        else:
            b.process_bar(ts)
        if ts.time() < self.sq:
            self.strategy.on_bar(ctx)
        after = getattr(b, "after_bar", None)   # PaperBroker: fill new orders at live quotes
        if after is not None:
            after(ts)
        self.last_ts = ts

    def finish(self) -> None:
        """End of data (backtest): close everything, then the last day's callbacks."""
        b = self.broker
        if b.positions() or b.pending_orders():
            b.square_off(self.last_ts, ExitReason.END_OF_DATA, use_open=False)
        self.strategy.on_day_end(self.ctx)
        if self.cur_day is not None:
            self._close_day(self.cur_day)
            self.cur_day = None

    def result(self, start: date, end: date) -> BacktestResult:
        s, cm = self.strategy, self.cm
        return BacktestResult(
            strategy=s.name, underlying=self.underlying.name, start=start, end=end, params=s.p,
            initial_capital=self.capital, risk_blocks=dict(self.risk.blocked), trades=self.broker.trades(),
            trading_days=list(self.days), cancelled_orders=len(self.broker.rejected),
            withdrawals=list(cm.withdrawals), halted_on=cm.halted_on,
            skips=dict(getattr(s, "skips", {}) or {}),
            settings_snapshot={"costs": self.settings.costs, "execution": self.settings.execution,
                               "risk": self.risk_cfg, "calendar": self.cal_cfg,
                               "capital": self.settings.raw.get("capital"), "backtest": self.settings.backtest},
            code_hash=code_hash(),
            diagnostics=s.diagnostics() if hasattr(s, "diagnostics") else None)


def run_backtest(strategy: Strategy, underlying: Underlying, start: date, end: date,
                 settings: Settings, data: HistoricalData | None = None,
                 capital: float | None = None, use_risk: bool | None = None,
                 withdraw: bool = True, calendar: dict | None = None) -> BacktestResult:
    """capital overrides backtest.initial_capital; use_risk overrides risk.enabled;
    withdraw=False disables the withdrawal of the initial stake (pure compounding);
    calendar overrides the `calendar:` config (skip weekdays / days near monthly expiry)."""
    from algo.data.store import DataStore

    capital = float(capital if capital is not None else settings.backtest["initial_capital"])
    data = data or HistoricalData(DataStore(settings.data_dir), underlying, start, end)
    session = TradingSession(strategy, underlying, settings, data, capital, use_risk=use_risk,
                             withdraw=withdraw, calendar=calendar)

    spot = data.spot_1m
    timeline = spot.index[(spot.index >= pd.Timestamp(start)) & (spot.index < pd.Timestamp(end) + pd.Timedelta(days=1))]
    if settings.backtest.get("skip_special_sessions", True) and len(timeline):
        # Special sessions are not normal days: Muhurat trading (an evening hour, removed by the
        # 09:15-15:29 session filter), DR-drill Saturdays (~105 bars), or a late start.
        days_idx = timeline.normalize()
        per_day = pd.Series(timeline, index=timeline).groupby(days_idx)
        first_bar, n_bars = per_day.min(), per_day.size()
        min_bars = int(settings.backtest.get("min_session_bars", 300))
        special = first_bar[(first_bar.dt.time != time(9, 15)) | (n_bars < min_bars)].index
        if len(special):
            log.info("skipping special sessions: %s", ", ".join(str(d.date()) for d in special))
            timeline = timeline[~days_idx.isin(special)]
    if timeline.empty:
        raise RuntimeError(f"No {underlying.name} spot data between {start} and {end}. Run `algo download` first.")
    if getattr(data, "options_enabled", True) and not data.expiries(start):
        log.warning("No option expiries stored for %s on/after %s - strategy will find no contracts.", underlying.name, start)

    session.start()
    for ts in timeline:
        session.step(ts)
    session.finish()
    return session.result(start, end)
