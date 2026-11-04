"""Account capital: compounding, one-time withdrawal of the initial stake, kill switch, lot sizing.

Lifecycle (config `capital:` in settings.yaml):
  * equity = initial + realised net P&L - withdrawn
  * once equity >= withdraw_multiple x initial, the initial stake is withdrawn (once);
    from then on only profits are at risk
  * kill switch: if equity falls below kill_switch_pct % of its reference (the initial
    capital, or the equity left right after the withdrawal), no new entries are allowed
  * lots_for(): the largest lot count whose planned loss and premium fit the risk limits
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date


@dataclass
class Withdrawal:
    on: date
    amount: float
    equity_before: float


@dataclass
class CapitalManager:
    initial: float
    withdraw_multiple: float | None = 2.0      # None: never withdraw (pure compounding)
    kill_switch_pct: float | None = 50.0       # None: no kill switch
    max_lots: int = 10
    risk_pct: float = 6.0                      # planned max loss per trade, % of equity
    position_pct: float = 90.0                 # premium paid per trade, % of equity
    realised: float = 0.0
    withdrawals: list[Withdrawal] = field(default_factory=list)
    halted_on: date | None = None

    def __post_init__(self) -> None:
        self._reference = self.initial

    @classmethod
    def from_config(cls, initial: float, cfg: dict | None, risk_cfg: dict | None,
                    withdraw: bool = True) -> "CapitalManager":
        cfg, risk_cfg = cfg or {}, risk_cfg or {}
        return cls(initial=initial,
                   withdraw_multiple=cfg.get("withdraw_multiple", 2.0) if withdraw else None,
                   kill_switch_pct=cfg.get("kill_switch_pct", 50.0),
                   max_lots=int(cfg.get("max_lots", 10)),
                   risk_pct=float(risk_cfg.get("max_risk_per_trade_pct", 6.0)),
                   position_pct=float(risk_cfg.get("max_position_pct", 90.0)))

    @property
    def withdrawn(self) -> float:
        return sum(w.amount for w in self.withdrawals)

    @property
    def equity(self) -> float:
        """Capital still in the trading account."""
        return self.initial + self.realised - self.withdrawn

    @property
    def halted(self) -> bool:
        return self.halted_on is not None

    def end_of_day(self, day: date, realised_total: float) -> Withdrawal | None:
        """Update realised P&L (cumulative), then apply the withdrawal and kill-switch rules."""
        self.realised = realised_total
        w = None
        if (self.withdraw_multiple and not self.withdrawals
                and self.equity >= self.withdraw_multiple * self.initial):
            w = Withdrawal(day, self.initial, self.equity)
            self.withdrawals.append(w)
            self._reference = self.equity
        if (self.kill_switch_pct is not None and not self.halted
                and self.equity < self.kill_switch_pct / 100 * self._reference):
            self.halted_on = day
        return w

    def lots_for(self, per_lot_risk: float, per_lot_cost: float) -> int:
        """Largest lot count with lots x risk <= risk_pct and lots x cost <= position_pct of equity."""
        if self.halted or self.equity <= 0 or per_lot_risk <= 0 or per_lot_cost <= 0:
            return 0
        by_risk = self.risk_pct / 100 * self.equity / per_lot_risk
        by_cost = self.position_pct / 100 * self.equity / per_lot_cost
        return max(0, min(self.max_lots, math.floor(min(by_risk, by_cost))))
