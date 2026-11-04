"""Strategy base class. Subclass it and implement `on_bar`."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from algo.core.context import Context
    from algo.core.models import Trade


class Strategy:
    name: str = "base"
    #: default parameters; override per-run via `params`
    defaults: dict[str, Any] = {}
    #: False lets positions run overnight: the engine then only squares off contracts
    #: expiring that day (expired options cannot be carried)
    intraday: bool = True
    #: per-strategy overrides of settings.yaml `risk:` (e.g. a wider no_entry_before/after window)
    risk_overrides: dict[str, Any] = {}

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}; allowed: {sorted(self.defaults)}")
        self.p = {**self.defaults, **params}

    def on_start(self, ctx: "Context") -> None:
        """Called once before the first bar."""

    def on_day_start(self, ctx: "Context") -> None:
        """Called on the first bar of every trading day (before on_bar)."""

    def on_bar(self, ctx: "Context") -> None:
        """Called after every 1-minute bar closes. ctx.now is that bar's timestamp."""
        raise NotImplementedError

    def on_trade_closed(self, ctx: "Context", trade: "Trade") -> None:
        """Called when a round trip completes."""

    def on_day_end(self, ctx: "Context") -> None:
        """Called after the last bar of each trading day."""
