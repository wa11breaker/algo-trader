"""Overnight defined-risk premium selling.

Why it can work: much of an option's time decay happens while the market is closed
(overnight, and 3 days over a weekend), and implied volatility tends to ease after the
open. Sell a hedged credit spread (or iron condor) near the close, buy it back the next
morning. The bought wing caps the gap risk.

    entry      entry_time (e.g. 15:00) on allowed weekdays; nearest expiry with >= min_dte days left
    sides      condor (CE + PE spread) | directional (sell against the day's trend) | fade (control)
    exit       exit_time on the next session (after hold_sessions sessions);
               stop when buyback >= credit x (1 + stop_mult), checked from the next open;
               target when buyback <= credit x (1 - target_pct%)
"""
from __future__ import annotations

from collections import Counter
from datetime import time

import pandas as pd

from algo.core.context import Context
from algo.core.models import ExitReason, OptionType, Side
from algo.core.strategy import Strategy
from algo.indicators import ema, vwap


def _hm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


class OvernightCredit(Strategy):
    """Sell a hedged credit spread / condor near the close, buy it back next morning."""

    name = "overnight_credit"
    intraday = False
    defaults = {
        "mode": "condor",           # condor | directional | fade
        "entry_time": "15:00",
        "exit_time": "09:30",       # on the exit session
        "hold_sessions": 1,
        "weekdays": "0,1,2,3,4",    # Mon=0 ... Fri=4 entry days
        "short_steps": 2,
        "wing_steps": 2,
        "min_dte": 1,               # never sell the contract expiring today
        "max_dte": 7,
        "stop_mult": 1.0,
        "target_pct": None,
        "min_credit": 3.0,
        "trend_ema": 20,
        "trend_slope_bars": 2,
        "lots": 1,
        "size_from_capital": True,
        "fill_timeout_min": 5,
    }

    def __init__(self, **params):
        super().__init__(**params)
        if self.p["mode"] not in ("condor", "directional", "fade"):
            raise ValueError("mode must be condor | directional | fade")
        self.days = {int(x) for x in str(self.p["weekdays"]).split(",") if x.strip() != ""}
        self.skips: Counter = Counter()
        self.records: list[dict] = []
        self._agg: dict[str, dict] = {}
        self.open_spreads: list = []    # survives across days (ctx.state resets daily)
        self._sessions = 0
        self._entered_today = False

    def on_day_start(self, ctx: Context) -> None:
        self._sessions += 1
        self._entered_today = False
        self.open_spreads = [s for s in self.open_spreads if not self._gone(ctx, s)]

    @staticmethod
    def _gone(ctx: Context, s) -> bool:
        return not ctx.spread_pending(s) and ctx.spread_legs(s) == (None, None)

    # ------------------------------------------------------------------ bar
    def on_bar(self, ctx: Context) -> None:
        p = self.p
        t = ctx.now.time()
        self._manage(ctx, t)
        if self._entered_today or t < _hm(p["entry_time"]):
            return
        self._entered_today = True
        self.open_spreads = [s for s in self.open_spreads if not self._gone(ctx, s)]
        if ctx.today.weekday() not in self.days or self.open_spreads:
            self.skips["weekday_or_busy"] += 1
            return
        exp = ctx.pick_expiry("nearest", p["min_dte"])
        if exp is None or (exp - ctx.today).days > p["max_dte"]:
            self.skips["dte"] += 1
            return
        sides = self._sides(ctx)
        if not sides:
            self.skips["no_trend"] += 1
            return
        for kind in sides:
            self._open(ctx, kind)

    def _sides(self, ctx: Context) -> list[OptionType]:
        if self.p["mode"] == "condor":
            return [OptionType.CE, OptionType.PE]
        p = self.p
        b5 = ctx.spot("5min", lookback=100, today_only=True)
        b15 = ctx.spot("15min", lookback=p["trend_ema"] * 4)
        if b5.empty or len(b15) <= p["trend_slope_bars"]:
            return []
        above = float(b5["close"].iloc[-1]) > float(vwap(b5).iloc[-1])
        e = ema(b15["close"], p["trend_ema"])
        slope = float(e.iloc[-1] - e.iloc[-1 - p["trend_slope_bars"]])
        if above and slope > 0:
            up = True
        elif not above and slope < 0:
            up = False
        else:
            return []
        if p["mode"] == "fade":
            up = not up
        return [OptionType.PE if up else OptionType.CE]

    def _open(self, ctx: Context, kind: OptionType) -> None:
        p = self.p
        probe = ctx.select_option(kind, f"OTM{p['short_steps']}" if p["short_steps"] else "ATM", "nearest", p["min_dte"])
        if probe is None:
            self.skips["no_option"] += 1
            return
        wing = ctx.data.contract(probe.expiry, probe.strike + ctx.underlying.strike_step * p["wing_steps"]
                                 * (1 if kind is OptionType.CE else -1), kind)
        sp, wp = ctx.ltp(probe), (ctx.ltp(wing) if wing else None)
        if sp is None or wp is None or sp - wp < p["min_credit"]:
            self.skips["low_credit_or_no_option"] += 1
            return
        credit = sp - wp
        width = ctx.underlying.strike_step * p["wing_steps"]
        per_lot_loss = (width - credit) * probe.lot_size          # overnight gap: assume the worst
        per_lot_margin = width * probe.lot_size
        lots = p["lots"]
        if p["size_from_capital"] and ctx.capital is not None:
            lots = ctx.capital.lots_for(per_lot_loss, per_lot_margin)
            if lots == 0:
                self.skips["too_small_to_size"] += 1
                return
        s = ctx.open_credit_spread(kind, p["short_steps"], p["wing_steps"], "nearest", p["min_dte"], lots,
                                   tag=f"on_{kind.value}", max_loss=per_lot_loss * lots, margin=per_lot_margin * lots)
        if s is None:
            self.skips["risk_manager"] += 1
            return
        s.meta.update(session=self._sessions, dte=(probe.expiry - ctx.today).days, weekday=ctx.today.weekday())
        self.open_spreads.append(s)

    # ------------------------------------------------------------- exits
    def _manage(self, ctx: Context, t: time) -> None:
        p = self.p
        for s in self.open_spreads:
            if s.closing:
                continue
            if ctx.spread_pending(s):
                if (pd.Timestamp(ctx.now) - pd.Timestamp(s.opened_at)).total_seconds() > p["fill_timeout_min"] * 60:
                    ctx.close_spread(s, ExitReason.SIGNAL)
                    s.meta["why"] = "unfilled"
                continue
            held = self._sessions - s.meta["session"]
            if held >= p["hold_sessions"] and t >= _hm(p["exit_time"]):
                ctx.close_spread(s, ExitReason.SIGNAL)
                s.meta["why"] = "time"
                continue
            if held == 0:
                continue      # same evening: let it sit until the next session
            debit, val = ctx.spread_entry_debit(s), ctx.spread_value(s)
            if debit is None or val is None or -debit <= 0:
                continue
            credit, buyback = -debit, -val
            if buyback >= credit * (1 + p["stop_mult"]):
                ctx.close_spread(s, ExitReason.STOP_LOSS)
                s.meta["why"] = "stop"
            elif p["target_pct"] and buyback <= credit * (1 - p["target_pct"] / 100):
                ctx.close_spread(s, ExitReason.TARGET)
                s.meta["why"] = "target"

    # --------------------------------------------------------- diagnostics
    def on_trade_closed(self, ctx: Context, trade) -> None:
        a = self._agg.setdefault(trade.group, {"legs": 0, "net": 0.0, "credit": 0.0})
        a["legs"] += 1
        a["net"] += trade.net_pnl
        a["credit"] += (-1 if trade.side is Side.BUY else 1) * trade.entry_price
        a.update(tag=trade.tag, exit_time=trade.exit_time, entry_time=trade.entry_time)
        if a["legs"] == 2:
            s = next((x for x in self.open_spreads if x.group == trade.group), None)
            self.records.append({"date": trade.entry_time.date(), "tag": a["tag"], "entry_time": a["entry_time"],
                                 "exit_time": a["exit_time"], "credit": round(a["credit"], 2),
                                 "weekday": s.meta.get("weekday") if s else None,
                                 "dte": s.meta.get("dte") if s else None,
                                 "why": s.meta.get("why", "square_off") if s else "?",
                                 "net_pnl": round(a["net"], 2), "lots": trade.lots})
            del self._agg[trade.group]

    def diagnostics(self) -> pd.DataFrame:
        return pd.DataFrame(self.records)
