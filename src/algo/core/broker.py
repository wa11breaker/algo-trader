"""Abstract interfaces that every mode (backtest / paper / live) implements.

Strategies only talk to these interfaces (through `Context`), so the same
strategy code runs unchanged against SimBroker today and a PaperBroker /
UpstoxBroker later.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime

import pandas as pd

from algo.core.models import ExitReason, OptionContract, OptionType, Order, Position, Trade


class Broker(ABC):
    @abstractmethod
    def place_order(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel(self, order: Order) -> None:
        """Cancel a pending (unfilled) order."""

    @abstractmethod
    def exit_position(self, position: Position, reason: ExitReason = ExitReason.SIGNAL) -> None:
        """Request to close a position (executes at the next available price)."""

    @abstractmethod
    def positions(self) -> list[Position]: ...

    @abstractmethod
    def exit_requested(self, position: Position) -> bool:
        """True if an exit for this position is pending."""

    @abstractmethod
    def pending_orders(self) -> list[Order]: ...

    @abstractmethod
    def trades(self) -> list[Trade]: ...


class MarketData(ABC):
    """Read-only market data, always bounded by the current clock (no lookahead)."""

    @abstractmethod
    def spot_bars(self, until: datetime, tf: str = "1min", lookback: int | None = None) -> pd.DataFrame:
        """Spot bars that have fully closed by the close of the 1-min bar starting at `until`.

        Index = bar start time. For tf > 1min, the in-progress bar is excluded.
        """

    def futures_bars(self, until: datetime, tf: str = "1min", lookback: int | None = None) -> pd.DataFrame:
        """Continuous front-month futures bars (same completion rules as spot_bars)."""
        raise NotImplementedError("this data source has no futures")

    @abstractmethod
    def expiries(self, on: date) -> list[date]:
        """Option expiries tradable on `on` (>= on), ascending."""

    @abstractmethod
    def contract(self, expiry: date, strike: float, option_type: OptionType) -> OptionContract | None: ...

    @abstractmethod
    def strikes(self, expiry: date) -> list[float]: ...

    def order_flow(self, until: datetime, tf: str = "1min", symbol: str | None = None) -> pd.DataFrame:
        """Buy / sell volume and delta per completed bar (live feeds only)."""
        raise NotImplementedError("this data source has no order flow")

    def option_bar(self, contract: OptionContract, ts: datetime) -> tuple[float, float, float, float] | None:
        """(open, high, low, close) of the contract's 1-min bar starting at ts, or None."""
        raise NotImplementedError

    @abstractmethod
    def option_ltp(self, contract: OptionContract, at: datetime) -> float | None:
        """Last traded price (close of the latest bar with ts <= at)."""
