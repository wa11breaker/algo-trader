"""Intraday defined-risk premium selling (credit spreads / iron condor) on NIFTY weeklies.

Why it can work: intraday, short-dated options lose time value and implied volatility
usually drifts lower after the open, so a sold out-of-the-money option expires (or is bought
back) cheaper on most days -> high win rate. The danger is the rare big trend day, so
every short leg has a bought wing (capped loss) and a stop on the spread's buyback cost.

Modes:
  condor       sell a CE spread above and a PE spread below (each side managed separately)
  directional  sell only the side AGAINST the trend: uptrend -> PE spread, downtrend -> CE spread
               (trend = 5-min close vs session VWAP and 15-min EMA slope agree)
  fade         sell only the side WITH the trend's exhaustion: opposite of directional (control)

Entry at `entry_time` on days with days-to-expiry <= dte_max.
Exits per spread: buyback >= credit x (1 + stop_mult) -> stop;  buyback <= credit x (1 - target_pct%)
-> target;  exit_time -> close.
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


class ShortPremium(Strategy):
    """Intraday credit spreads / iron condor on weeklies; buyback-cost stop and target."""

    name = "short_premium"
    defaults = {
        "mode": "condor",           # condor | directional | fade
        "entry_time": "10:00",
        "exit_time": "15:10",
        "short_steps": 2,           # sold strike distance from ATM, in strike steps (50 pts on NIFTY)
        "wing_steps": 2,            # bought wing distance beyond the sold strike
        "dte_max": 6,               # trade only when days to the nearest expiry <= this (0 = expiry day)
        "min_dte": 0,
        "stop_mult": 1.0,           # stop when buyback cost >= credit x (1 + stop_mult)
        "target_pct": None,         # e.g. 70 -> take profit once 70% of the credit is captured
        "min_credit": 3.0,          # skip a side whose credit (pts) is below this
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
        self.skips: Counter = Counter()
        self.records: list[dict] = []
        self._agg: dict[str, dict] = {}

    def on_day_start(self, ctx: Context) -> None:
        ctx.state.update(entered=False, spreads=[])

    # ------------------------------------------------------------------ bar
    def on_bar(self, ctx: Context) -> None:
        p, st = self.p, ctx.state
        t = ctx.now.time()
        self._manage(ctx, late=t >= _hm(p["exit_time"]))
        if st["entered"] or t < _hm(p["entry_time"]):
            return
        st["entered"] = True
        exp = ctx.pick_expiry("nearest", p["min_dte"])
        if exp is None or (exp - ctx.today).days > p["dte_max"]:
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
        trend = self._trend(ctx)
        if trend == 0:
            return []
        up = trend == 1
        if self.p["mode"] == "fade":
            up = not up
        return [OptionType.PE if up else OptionType.CE]   # sell puts in an uptrend

    def _trend(self, ctx: Context) -> int:
        p = self.p
        b5 = ctx.spot("5min", lookback=100, today_only=True)
        b15 = ctx.spot("15min", lookback=p["trend_ema"] * 4)
        if b5.empty or len(b15) <= p["trend_slope_bars"]:
            return 0
        above = float(b5["close"].iloc[-1]) > float(vwap(b5).iloc[-1])
        e = ema(b15["close"], p["trend_ema"])
        rising = float(e.iloc[-1] - e.iloc[-1 - p["trend_slope_bars"]])
        if above and rising > 0:
            return 1
        if not above and rising < 0:
            return -1
        return 0

    def _open(self, ctx: Context, kind: OptionType) -> None:
        p = self.p
        lots = p["lots"]
        probe = ctx.select_option(kind, f"OTM{p['short_steps']}" if p["short_steps"] else "ATM", "nearest", p["min_dte"])
        if probe is None:
            self.skips["no_option"] += 1
            return
        wing = ctx.data.contract(probe.expiry, probe.strike + ctx.underlying.strike_step * p["wing_steps"]
                                 * (1 if kind is OptionType.CE else -1), kind)
        sp, wp = ctx.ltp(probe), (ctx.ltp(wing) if wing else None)
        if sp is None or wp is None:
            self.skips["no_option"] += 1
            return
        credit = sp - wp
        if credit < p["min_credit"]:
            self.skips["low_credit"] += 1
            return
        width = ctx.underlying.strike_step * p["wing_steps"]
        per_lot_loss = min(width - credit, credit * p["stop_mult"] * 1.5) * probe.lot_size   # stop + gap allowance
        per_lot_margin = width * probe.lot_size
        if p["size_from_capital"] and ctx.capital is not None:
            lots = ctx.capital.lots_for(per_lot_loss, per_lot_margin)
            if lots == 0:
                self.skips["too_small_to_size"] += 1
                return
        sp_obj = ctx.open_credit_spread(kind, p["short_steps"], p["wing_steps"], "nearest", p["min_dte"], lots,
                                        tag=f"sp_{kind.value}", max_loss=per_lot_loss * lots,
                                        margin=per_lot_margin * lots)
        if sp_obj is None:
            self.skips["risk_manager"] += 1
            return
        sp_obj.meta.update(dte=(probe.expiry - ctx.today).days, est_credit=credit)
        ctx.state["spreads"].append(sp_obj)

    # ------------------------------------------------------------- exits
    def _manage(self, ctx: Context, late: bool) -> None:
        p = self.p
        for s in ctx.state["spreads"]:
            if s.closing:
                continue
            if late:
                ctx.close_spread(s, ExitReason.SIGNAL)
                s.meta["why"] = "time"
                continue
            if ctx.spread_pending(s):
                if (pd.Timestamp(ctx.now) - pd.Timestamp(s.opened_at)).total_seconds() > p["fill_timeout_min"] * 60:
                    ctx.close_spread(s, ExitReason.SIGNAL)
                    s.meta["why"] = "unfilled"
                continue
            debit = ctx.spread_entry_debit(s)
            val = ctx.spread_value(s)
            if debit is None or val is None:
                continue
            credit, buyback = -debit, -val
            if credit <= 0:
                continue
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
        sign = -1 if trade.side is Side.BUY else 1
        a["credit"] += sign * trade.entry_price
        a["tag"], a["exit_time"], a["entry_time"] = trade.tag, trade.exit_time, trade.entry_time
        if a["legs"] == 2:
            s = next((x for x in ctx.state.get("spreads", []) if x.group == trade.group), None)
            self.records.append({"date": trade.entry_time.date(), "tag": a["tag"], "entry_time": a["entry_time"],
                                 "exit_time": a["exit_time"], "credit": round(a["credit"], 2),
                                 "dte": s.meta.get("dte") if s else None,
                                 "why": s.meta.get("why", "square_off") if s else "?",
                                 "net_pnl": round(a["net"], 2), "lots": trade.lots})
            del self._agg[trade.group]

    def diagnostics(self) -> pd.DataFrame:
        return pd.DataFrame(self.records)
