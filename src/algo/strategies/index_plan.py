"""Shared plumbing for option-buying strategies whose stop/target live on the INDEX.

A strategy decides direction, entry and index stop; this mixin
  * picks the option, sizes lots from capital (or fixed `lots`) and places the order
    with a premium safety-net stop,
  * manages the index plan every minute: stop (pessimistic: checked before target),
    target (target_r x risk), breakeven (breakeven_at_r), trailing stop (trail_start_r /
    plan["trail_dist"]) and the time exit,
  * records one diagnostics row per closed trade (index R, MFE, exit reason, ...).

Required params: moneyness, expiry, min_dte, delta_est, option_sl_pct, lots, size_from_capital,
target_r, breakeven_at_r, exit_time. Optional: trail_start_r.
The strategy must define `self.skips` (Counter) and `self.records` (list).
"""
from __future__ import annotations

from datetime import time

import pandas as pd

from algo.core.context import Context
from algo.core.models import ExitReason, OptionType


def hm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


class IndexPlanMixin:
    p: dict
    # True when every exit lives in the index plan (stop / target / breakeven / trail / time), so the
    # index-only replay (algo.research.index_replay) reproduces the strategy exactly. Strategies with
    # extra signal exits (e.g. a VWAP break) leave it False; their replay is an approximation.
    index_replay_exact = False

    def _buy_with_plan(self, ctx: Context, d: int, entry: float, stop: float, tag: str,
                       extra: dict | None = None, trail_dist: float | None = None,
                       target: float | None = None) -> bool:
        """Buy CE (d=+1) / PE (d=-1) with an index plan. Returns True if an order was placed.

        `target` (index level) overrides the target_r x risk target."""
        p = self.p
        risk = d * (entry - stop)
        if target is None and p["target_r"]:
            target = entry + d * p["target_r"] * risk
        # every proposed plan, before option selection / sizing / risk checks (index-only research)
        self.__dict__.setdefault("plan_log", []).append({
            "now": ctx.now, "tag": tag, "dir": d, "entry": entry, "stop": stop, "target": target,
            "risk": risk, "trail_dist": trail_dist})
        kind = OptionType.CE if d == 1 else OptionType.PE
        opt, ltp = self._pick_option(ctx, kind)
        if not ltp:
            self.skips["no_option"] += 1
            return False
        if p.get("max_premium") and ltp > p["max_premium"]:
            self.skips["too_expensive"] += 1
            return False
        if p.get("min_premium") and ltp < p["min_premium"]:
            self.skips["too_cheap"] += 1           # fixed slippage + costs overshoot the plan on cheap options
            return False
        sl_pct = p["option_sl_pct"]
        if p.get("sl_k"):                         # option safety stop matched to the planned loss
            sl_pct = max(p.get("min_sl_pct", 5.0), min(sl_pct, 100 * p["sl_k"] * risk * p["delta_est"] / ltp))
            unit = ltp * sl_pct / 100
        else:
            unit = min(risk * p["delta_est"], ltp * sl_pct / 100)
        per_lot_risk = unit * (1 + p.get("loss_buf", 0.0)) * opt.lot_size
        lots = p["lots"]
        if p["size_from_capital"] and ctx.capital is not None:
            lots = ctx.capital.lots_for(per_lot_risk, ltp * opt.lot_size)
            if lots == 0:
                self.skips["too_small_to_size"] += 1
                return False
        order = ctx.buy(opt, lots=lots, sl_pct=sl_pct, target_pct=None, tag=tag,
                        max_loss=per_lot_risk * lots)
        if order is None:
            self.skips["risk_manager"] += 1
            return False
        ctx.state.setdefault("plans", {})[tag] = {
            "date": ctx.today, "time": ctx.now.strftime("%H:%M"), "dir": d, "entry": entry, "stop": stop,
            "init_stop": stop, "target": target, "risk": risk, "best": entry, "be_done": False,
            "trail_dist": trail_dist, "lots": lots, **(extra or {})}
        return True

    def _pick_option(self, ctx: Context, kind: OptionType):
        """(contract, ltp) at p["moneyness"]; with p["max_premium"], step further OTM (up to
        p["max_otm_steps"]) while the premium is above the cap."""
        p = self.p
        m = p["moneyness"].upper()
        opt = ctx.select_option(kind, m, p["expiry"], p["min_dte"])
        ltp = ctx.ltp(opt) if opt else None
        cap = p.get("max_premium")
        if cap:
            start = int(m[3:]) if m.startswith("OTM") else -int(m[3:]) if m.startswith("ITM") else 0
            for k in range(start + 1, start + 1 + int(p.get("max_otm_steps", 5))):
                if ltp and ltp <= cap:
                    break
                m = f"OTM{k}" if k > 0 else "ATM" if k == 0 else f"ITM{-k}"
                nxt = ctx.select_option(kind, m, p["expiry"], p["min_dte"])
                nxt_ltp = ctx.ltp(nxt) if nxt else None
                if not nxt_ltp:
                    break                       # ran out of listed strikes: keep the last one
                opt, ltp = nxt, nxt_ltp
        return opt, ltp

    def _plans(self, ctx: Context):
        """(position, plan) pairs for open positions that have a plan."""
        plans = ctx.state.get("plans", {})
        return [(pos, plans[pos.tag]) for pos in ctx.active_positions if pos.tag in plans]

    def _manage_minute_exits(self, ctx: Context) -> None:
        if not ctx.active_positions:
            return
        p = self.p
        b = ctx.spot(lookback=1).iloc[-1]
        hi, lo = float(b["high"]), float(b["low"])
        late = ctx.now.time() >= hm(p["exit_time"])
        for pos, plan in self._plans(ctx):
            d = plan["dir"]
            if late:
                ctx.exit(pos, ExitReason.SIGNAL)
                plan["why"] = "time"
                continue
            if (d == 1 and lo <= plan["stop"]) or (d == -1 and hi >= plan["stop"]):   # stop first (pessimistic)
                ctx.exit(pos, ExitReason.STOP_LOSS)
                plan["why"] = "trail" if plan.get("trailing") else "breakeven" if plan["be_done"] else "stop"
                continue
            t = plan["target"]
            if t is not None and ((d == 1 and hi >= t) or (d == -1 and lo <= t)):
                ctx.exit(pos, ExitReason.TARGET)
                plan["why"] = "target"
                continue
            plan["best"] = max(plan["best"], hi) if d == 1 else min(plan["best"], lo)
            gain = d * (plan["best"] - plan["entry"])
            if p["breakeven_at_r"] and not plan["be_done"] and gain >= p["breakeven_at_r"] * plan["risk"]:
                plan["stop"], plan["be_done"] = (max(plan["stop"], plan["entry"]) if d == 1
                                                 else min(plan["stop"], plan["entry"])), True
            start = p.get("trail_start_r")
            if plan["trail_dist"] and start is not None and gain >= start * plan["risk"]:
                trail = plan["best"] - d * plan["trail_dist"]
                if d * (trail - plan["stop"]) > 0:      # only ever tighten
                    plan["stop"], plan["trailing"] = trail, True

    def on_trade_closed(self, ctx: Context, trade) -> None:
        plan = ctx.state.get("plans", {}).pop(trade.tag, None)
        if plan is None:
            return
        bars = ctx.data.spot_bars(trade.exit_time, lookback=400)
        held = bars[bars.index >= pd.Timestamp(trade.entry_time)]
        d, risk = plan["dir"], plan["risk"]
        idx_exit = float(held["open"].iloc[-1]) if len(held) else float("nan")
        best = (held["high"].max() if d == 1 else held["low"].min()) if len(held) else float("nan")
        self.records.append({
            **{k: v for k, v in plan.items() if k not in ("dir", "be_done", "best", "trailing")},
            "why": plan.get("why", trade.exit_reason.value.lower()),
            "tag": trade.tag, "option": trade.contract.symbol, "entry_time": trade.entry_time,
            "exit_time": trade.exit_time, "exit_reason": trade.exit_reason.value,
            "index_exit": idx_exit, "index_R": d * (idx_exit - plan["entry"]) / risk,
            "mfe_R": d * (best - plan["entry"]) / risk,
            "minutes_held": (pd.Timestamp(trade.exit_time) - pd.Timestamp(trade.entry_time)).total_seconds() / 60,
            "option_entry": trade.entry_price, "option_exit": trade.exit_price, "net_pnl": round(trade.net_pnl, 2),
        })

    def diagnostics(self) -> pd.DataFrame:
        return pd.DataFrame(self.records)
