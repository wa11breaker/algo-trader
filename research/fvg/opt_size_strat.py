"""Prototype: fvg_flow with alternative position sizing / option safety stop (research only).

size_mode
  "index"    baseline: per-lot loss = min(index_risk x delta_est, premium x option_sl_pct)
  "option"   per-lot loss = premium x option_sl_pct  (the option bracket is the hard bound)
  "matched"  option safety stop moved to k x the planned loss: sl_pct = min(option_sl_pct,
             100 x sl_k x index_risk x delta_est / premium), per-lot loss = premium x sl_pct
  loss_buf   multiply per-lot loss by (1 + loss_buf) for slippage / gaps through the stop
Every closed trade records equity at entry, the planned loss and the option premium.
"""
from __future__ import annotations

from algo.core.context import Context
from algo.core.models import OptionType
from algo.strategies.fvg_flow import FvgFlow


class FvgSize(FvgFlow):
    name = "fvg_flow_size"
    defaults = {**FvgFlow.defaults, "size_mode": "index", "sl_k": 1.0, "loss_buf": 0.0,
                "min_sl_pct": 5.0, "min_premium": None}

    def _buy_with_plan(self, ctx: Context, d, entry, stop, tag, extra=None, trail_dist=None, target=None):
        p = self.p
        risk = d * (entry - stop)
        if target is None and p["target_r"]:
            target = entry + d * p["target_r"] * risk
        kind = OptionType.CE if d == 1 else OptionType.PE
        opt, ltp = self._pick_option(ctx, kind)
        if not ltp:
            self.skips["no_option"] += 1
            return False
        if p.get("max_premium") and ltp > p["max_premium"]:
            self.skips["too_expensive"] += 1
            return False
        if p["min_premium"] and ltp < p["min_premium"]:
            self.skips["too_cheap"] += 1
            return False
        sl_pct = p["option_sl_pct"]
        mode = p["size_mode"]
        if mode == "index":
            unit = min(risk * p["delta_est"], ltp * sl_pct / 100)
        elif mode == "option":
            unit = ltp * sl_pct / 100
        elif mode == "matched":
            sl_pct = max(p["min_sl_pct"], min(sl_pct, 100 * p["sl_k"] * risk * p["delta_est"] / ltp))
            unit = ltp * sl_pct / 100
        else:
            raise ValueError(mode)
        per_lot_risk = unit * (1 + p["loss_buf"]) * opt.lot_size
        lots = p["lots"]
        eq = ctx.capital.equity if ctx.capital is not None else None
        if p["size_from_capital"] and ctx.capital is not None:
            lots = ctx.capital.lots_for(per_lot_risk, ltp * opt.lot_size)
            if lots == 0:
                self.skips["too_small_to_size"] += 1
                return False
        order = ctx.buy(opt, lots=lots, sl_pct=sl_pct, target_pct=None, tag=tag, max_loss=per_lot_risk * lots)
        if order is None:
            self.skips["risk_manager"] += 1
            return False
        ctx.state.setdefault("plans", {})[tag] = {
            "date": ctx.today, "time": ctx.now.strftime("%H:%M"), "dir": d, "entry": entry, "stop": stop,
            "init_stop": stop, "target": target, "risk": risk, "best": entry, "be_done": False,
            "trail_dist": trail_dist, "lots": lots, "equity": eq, "planned": per_lot_risk * lots,
            "ltp0": ltp, "sl_pct_used": sl_pct, "lot_size": opt.lot_size,
            "strike": opt.strike, "spot0": ctx.spot(lookback=1).iloc[-1]["close"], **(extra or {})}
        return True
