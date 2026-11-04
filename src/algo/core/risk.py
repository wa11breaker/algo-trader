"""Account-level risk manager applied to every entry (backtest now, paper/live later).

Rules (config `risk:` in settings.yaml):
  * max_risk_per_trade_pct - planned max loss of a trade must be <= this % of capital
  * max_position_pct       - premium paid for a trade must be <= this % of capital
  * daily_loss_limit_pct   - no new entries once today's realised + open P&L <= -this %
  * max_consecutive_losses - no new entries today after N losing round trips in a row
  * max_trades_per_day     - entries (a spread counts as one) per day
  * no_entry_before/after  - entry time window
  * event_days_file        - YAML list of dates with no trading (RBI policy, Budget, ...)
Calendar filters (config `calendar:`) apply to every entry even with the risk rules off:
  * skip_weekdays                    - e.g. [fri]
  * skip_days_before_monthly_expiry  - no entries from N calendar days before the month's last expiry
`capital` follows account equity (updated daily by the engine from CapitalManager).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from algo.core.context import Context

log = logging.getLogger(__name__)


def _t(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def load_event_days(path: Path | None) -> set[date]:
    if not path or not Path(path).exists():
        return set()
    raw = yaml.safe_load(Path(path).read_text()) or {}
    out = set()
    for items in raw.values() if isinstance(raw, dict) else [raw]:
        for d in items or []:
            out.add(d if isinstance(d, date) else date.fromisoformat(str(d)))
    return out


WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_weekdays(v) -> set[int]:
    """[4] / ["fri"] / "fri,mon" -> {4} / {4} / {4, 0}."""
    if v is None or v == "":
        return set()
    items = v.split(",") if isinstance(v, str) else v
    out = set()
    for x in items:
        x = str(x).strip().lower()
        if x:
            out.add(WEEKDAYS[x[:3]] if x[:3] in WEEKDAYS else int(x))
    return out


class RiskManager:
    def __init__(self, cfg: dict | None, capital: float, root: Path | None = None,
                 calendar: dict | None = None) -> None:
        cfg = cfg or {}
        # calendar filters apply even when the risk rules are disabled
        cal = calendar or {}
        self.skip_weekdays = parse_weekdays(cal.get("skip_weekdays"))
        mb = cal.get("skip_days_before_monthly_expiry")
        self.monthly_buffer = None if mb is None else int(mb)
        self.enabled = bool(cfg.get("enabled", False))
        self.capital = float(capital)
        self.max_risk_pct = float(cfg.get("max_risk_per_trade_pct", 100))
        self.max_position_pct = float(cfg.get("max_position_pct", 100))
        self.daily_loss_pct = float(cfg.get("daily_loss_limit_pct", 100))
        self.max_losses = int(cfg.get("max_consecutive_losses", 10_000))
        self.max_trades = int(cfg.get("max_trades_per_day", 10_000))
        self.start = _t(cfg.get("no_entry_before", "09:15"))
        self.end = _t(cfg.get("no_entry_after", "15:30"))
        ev = cfg.get("event_days_file")
        self.event_days = load_event_days((root / ev) if (root and ev) else (Path(ev) if ev else None))
        self.halted = False   # set by the capital manager's kill switch
        self.blocked: Counter = Counter()
        self._day: date | None = None
        self._entries = 0

    def _roll(self, d: date) -> None:
        if d != self._day:
            self._day, self._entries = d, 0

    def calendar_block(self, ctx: "Context") -> str | None:
        """'weekday' on a skipped weekday; 'near_monthly_expiry' on the monthly expiry day
        or within skip_days_before_monthly_expiry calendar days before it."""
        if ctx.today.weekday() in self.skip_weekdays:
            return "weekday"
        if self.monthly_buffer is not None:
            m = ctx.pick_expiry("monthly")
            if m is not None and 0 <= (m - ctx.today).days <= self.monthly_buffer:
                return "near_monthly_expiry"
        return None

    def day_pnl(self, ctx: "Context") -> float:
        realised = sum(t.net_pnl for t in ctx.broker.trades() if t.exit_time.date() == ctx.today)
        open_ = 0.0
        for p in ctx.broker.positions():
            ltp = ctx.ltp(p.contract)
            if ltp is not None:
                open_ += p.unrealized(ltp) - p.entry_costs
        return realised + open_

    def losing_streak(self, ctx: "Context") -> int:
        """Trailing losing round trips today (legs of one spread share a `group`)."""
        groups: dict[str, list] = {}
        for t in ctx.broker.trades():
            if t.entry_time.date() == ctx.today:
                groups.setdefault(t.group, []).append(t)
        open_groups = {p.group for p in ctx.broker.positions()}
        groups = {k: v for k, v in groups.items() if k not in open_groups}   # unfinished spreads
        ordered = sorted(groups.values(), key=lambda g: max(t.exit_time for t in g))
        streak = 0
        for g in reversed(ordered):
            if sum(t.net_pnl for t in g) <= 0:
                streak += 1
            else:
                break
        return streak

    def check(self, ctx: "Context", max_loss: float, cost: float) -> str | None:
        """Return None if the entry is allowed, else the name of the rule that blocks it."""
        cal = self.calendar_block(ctx)
        if cal:
            self.blocked[cal] += 1
            return cal
        if not self.enabled:
            return None
        self._roll(ctx.today)
        t = ctx.now.time()
        reason = None
        if self.halted:
            reason = "kill_switch"
        elif ctx.today in self.event_days:
            reason = "event_day"
        elif not (self.start <= t < self.end):
            reason = "time_window"
        elif self._entries >= self.max_trades:
            reason = "max_trades_per_day"
        elif self.losing_streak(ctx) >= self.max_losses:
            reason = "loss_streak"
        elif self.day_pnl(ctx) <= -self.daily_loss_pct / 100 * self.capital:
            reason = "daily_loss_limit"
        elif max_loss > self.max_risk_pct / 100 * self.capital:
            reason = "risk_per_trade"
        elif cost > self.max_position_pct / 100 * self.capital:
            reason = "position_size"
        if reason:
            self.blocked[reason] += 1
            log.debug("%s entry blocked: %s (max_loss %.0f, cost %.0f)", ctx.now, reason, max_loss, cost)
            return reason
        self._entries += 1
        return None
