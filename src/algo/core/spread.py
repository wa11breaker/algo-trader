"""Two-leg debit spread (buy near strike, sell further OTM strike), managed as one trade."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from algo.core.models import OptionContract, OptionType


@dataclass
class Spread:
    kind: OptionType
    long: OptionContract
    short: OptionContract
    lots: int
    tag: str
    opened_at: datetime
    est_debit: float                  # per unit, at signal time
    width: float                      # strike distance in points
    group: str = ""                   # shared by both legs' orders / positions / trades
    meta: dict = field(default_factory=dict)
    closing: bool = False

    @property
    def quantity(self) -> int:
        return self.lots * self.long.lot_size

    @property
    def max_profit_per_unit(self) -> float:
        return self.width - self.est_debit
