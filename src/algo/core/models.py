"""Broker-agnostic domain models shared by backtest, paper and live modes."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class ExitReason(str, Enum):
    STOP_LOSS = "SL"
    TARGET = "TARGET"
    SIGNAL = "SIGNAL"
    SQUARE_OFF = "SQUARE_OFF"
    END_OF_DATA = "END_OF_DATA"


@dataclass(frozen=True)
class OptionContract:
    underlying: str
    expiry: date
    strike: float
    option_type: OptionType
    lot_size: int
    instrument_key: str = ""
    exchange: str = "NSE"

    @property
    def symbol(self) -> str:
        return f"{self.underlying} {self.expiry:%d%b%y}".upper() + f" {self.strike:g} {self.option_type.value}"


_order_ids = itertools.count(1)


@dataclass
class Order:
    contract: OptionContract
    side: Side
    lots: int
    placed_at: datetime
    sl_pct: float | None = None       # stop-loss as % of entry price (for long: below)
    target_pct: float | None = None   # target as % of entry price (for long: above)
    tag: str = ""
    group: str = ""                   # legs of one spread share a group; single orders get their id
    id: int = field(default_factory=lambda: next(_order_ids))
    status: OrderStatus = OrderStatus.PENDING
    fill_price: float | None = None
    filled_at: datetime | None = None

    @property
    def quantity(self) -> int:
        return self.lots * self.contract.lot_size


@dataclass
class Position:
    contract: OptionContract
    side: Side
    lots: int
    entry_price: float
    entry_time: datetime
    sl_price: float | None = None
    target_price: float | None = None
    entry_costs: float = 0.0
    tag: str = ""
    group: str = ""

    @property
    def quantity(self) -> int:
        return self.lots * self.contract.lot_size

    def unrealized(self, ltp: float) -> float:
        sign = 1 if self.side is Side.BUY else -1
        return sign * (ltp - self.entry_price) * self.quantity


@dataclass
class Trade:
    """A completed round trip."""
    contract: OptionContract
    side: Side
    lots: int
    quantity: int
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: ExitReason
    gross_pnl: float
    costs: float
    tag: str = ""
    group: str = ""

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.costs

    def to_dict(self) -> dict:
        return {
            "symbol": self.contract.symbol,
            "underlying": self.contract.underlying,
            "expiry": self.contract.expiry.isoformat(),
            "strike": self.contract.strike,
            "type": self.contract.option_type.value,
            "side": self.side.value,
            "lots": self.lots,
            "lot_size": self.contract.lot_size,
            "qty": self.quantity,
            "entry_time": self.entry_time,
            "entry_price": round(self.entry_price, 2),
            "exit_time": self.exit_time,
            "exit_price": round(self.exit_price, 2),
            "exit_reason": self.exit_reason.value,
            "gross_pnl": round(self.gross_pnl, 2),
            "costs": round(self.costs, 2),
            "net_pnl": round(self.net_pnl, 2),
            "tag": self.tag,
            "group": self.group,
        }
