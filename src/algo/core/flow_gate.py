"""Order-flow confirmation before an entry (paper / live only; backtests have no flow data).

Config `paper.order_flow_gate` in settings.yaml:
  enabled           - turn the gate on
  bars              - number of recent 1-min bars whose delta is summed
  min_delta         - bullish entries need summed delta >= max(min_delta, >0); bearish <= -that
  require_cum_delta - also require today's cumulative delta on the trade's side

Direction of an entry: buying a CE or selling a PE is bullish; buying a PE or selling a CE is
bearish. A debit spread follows its long leg, a credit spread is the opposite of its sold leg.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from algo.core.context import Context

log = logging.getLogger(__name__)


class OrderFlowGate:
    def __init__(self, cfg: dict | None) -> None:
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.bars = max(1, int(cfg.get("bars", 3)))
        self.min_delta = float(cfg.get("min_delta", 0))
        self.require_cum = bool(cfg.get("require_cum_delta", False))
        self.log: list[dict] = []    # every decision, for the events log

    def check(self, ctx: "Context", bullish: bool) -> str | None:
        """None if the entry may go ahead, else 'order_flow'."""
        if not self.enabled:
            return None
        try:
            flow = ctx.order_flow(today_only=True)
        except NotImplementedError:
            return None   # data source without order flow (backtest): gate does not apply
        recent = flow["delta"].iloc[-self.bars:] if len(flow) else flow.get("delta")
        if recent is None or len(recent) < self.bars:
            ok, delta, cum = False, None, None   # not enough live bars yet: do not trade blind
        else:
            delta = float(recent.sum())
            cum = float(flow["cum_delta"].iloc[-1])
            need = max(self.min_delta, 0.0)
            ok = (delta > 0 and delta >= need) if bullish else (delta < 0 and -delta >= need)
            if ok and self.require_cum:
                ok = cum > 0 if bullish else cum < 0
        self.log.append({"ts": ctx.now, "bullish": bullish, "delta": delta, "cum_delta": cum, "ok": ok})
        log.info("order-flow %s %s entry: delta(%d bars)=%s cum=%s", "OK" if ok else "BLOCKS",
                 "bullish" if bullish else "bearish", self.bars,
                 "n/a" if delta is None else f"{delta:+.0f}", "n/a" if cum is None else f"{cum:+.0f}")
        return None if ok else "order_flow"
