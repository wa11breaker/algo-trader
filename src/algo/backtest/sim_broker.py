"""Simulated broker for backtests.

Fill model (deliberately conservative):
  * Orders placed on bar t's close fill at the OPEN of the option's bar t+1,
    adjusted by slippage (buy higher / sell lower).
  * If the option has no bar at that minute (illiquid), the order waits;
    it is cancelled if still unfilled after `max_wait_bars`.
  * SL / target are evaluated against each option bar's high/low. If both
    are touched in the same bar, the SL is assumed to have hit first.
  * Gaps: if a bar opens beyond the SL, the fill is at the open (worse);
    if it opens beyond the target, the fill is at the open (better).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

import pandas as pd

from algo.backtest.costs import CostModel
from algo.core.broker import Broker, MarketData
from algo.core.models import ExitReason, Order, OrderStatus, Position, Side, Trade

log = logging.getLogger(__name__)
TICK = 0.05


def _tick(p: float) -> float:
    return max(round(round(p / TICK) * TICK, 2), TICK)


class SimBroker(Broker):
    def __init__(self, data: MarketData, costs: CostModel, slippage_pct: float = 0.5,
                 slippage_min: float = 0.5, max_wait_bars: int = 5) -> None:
        self.data = data
        self.costs = costs
        self.slip_pct = slippage_pct / 100
        self.slip_min = slippage_min
        self.max_wait = pd.Timedelta(minutes=max_wait_bars)
        self._pending: list[Order] = []
        self._exits: dict[int, ExitReason] = {}   # id(position) -> reason
        self._positions: list[Position] = []
        self._trades: list[Trade] = []
        self.rejected: list[Order] = []
        self.on_trade_closed: Callable[[Trade], None] | None = None

    # ---- Broker interface -------------------------------------------------
    def place_order(self, order: Order) -> Order:
        self._pending.append(order)
        return order

    def cancel(self, order: Order) -> None:
        if order in self._pending:
            self._pending.remove(order)
            order.status = OrderStatus.CANCELLED

    def exit_position(self, position: Position, reason: ExitReason = ExitReason.SIGNAL) -> None:
        if position in self._positions:
            self._exits.setdefault(id(position), reason)

    def positions(self) -> list[Position]:
        return list(self._positions)

    def exit_requested(self, position: Position) -> bool:
        return id(position) in self._exits

    def pending_orders(self) -> list[Order]:
        return list(self._pending)

    def trades(self) -> list[Trade]:
        return list(self._trades)

    # ---- simulation -------------------------------------------------------
    def _slip(self, price: float) -> float:
        return max(price * self.slip_pct, self.slip_min)

    def _exec_price(self, side: Side, price: float) -> float:
        return _tick(price + self._slip(price) if side is Side.BUY else price - self._slip(price))

    def _market_fill(self, contract, side: Side, ts: datetime, bar) -> float:
        """Price of a market order executed during the bar starting at ts (bar = its OHLC).
        Backtest: the bar's open +/- slippage. PaperBroker overrides this with live quotes."""
        return self._exec_price(side, bar[0])

    def _square_off_fill(self, pos: Position, ts: datetime, use_open: bool) -> float:
        bar = self.data.option_bar(pos.contract, ts) if use_open else None
        ref = bar[0] if bar else self.data.option_ltp(pos.contract, ts)
        if ref is None:
            ref = pos.entry_price
            log.warning("no price for %s at %s; closing at entry", pos.contract.symbol, ts)
        return self._exec_price(pos.side.opposite, ref)

    def process_bar(self, ts: datetime) -> None:
        """Run fills and SL/target checks for the 1-min bar starting at ts."""
        self._fill_entries(ts)
        self._fill_exits(ts)
        self._check_brackets(ts)

    def _fill_entries(self, ts: datetime) -> None:
        still = []
        for o in self._pending:
            bar = self.data.option_bar(o.contract, ts)
            if bar is None:
                if pd.Timestamp(ts) - pd.Timestamp(o.placed_at) > self.max_wait:
                    o.status = OrderStatus.CANCELLED
                    self.rejected.append(o)
                    log.debug("cancelled unfilled order %s (no option bars)", o.contract.symbol)
                else:
                    still.append(o)
                continue
            price = self._market_fill(o.contract, o.side, ts, bar)
            o.status, o.fill_price, o.filled_at = OrderStatus.FILLED, price, ts
            sign = 1 if o.side is Side.BUY else -1
            sl = _tick(price * (1 - sign * o.sl_pct / 100)) if o.sl_pct else None
            tgt = _tick(price * (1 + sign * o.target_pct / 100)) if o.target_pct else None
            self._positions.append(Position(
                contract=o.contract, side=o.side, lots=o.lots, entry_price=price, entry_time=ts,
                sl_price=sl, target_price=tgt, tag=o.tag, group=o.group or str(o.id),
                entry_costs=self.costs.cost(o.side, price, o.quantity, o.contract.exchange)))
        self._pending = still

    def _fill_exits(self, ts: datetime) -> None:
        for pos in list(self._positions):
            reason = self._exits.get(id(pos))
            if reason is None:
                continue
            bar = self.data.option_bar(pos.contract, ts)
            if bar is None:
                continue  # wait for a tradable bar
            self._close(pos, ts, self._market_fill(pos.contract, pos.side.opposite, ts, bar), reason)

    def _check_brackets(self, ts: datetime) -> None:
        for pos in list(self._positions):
            if pos.sl_price is None and pos.target_price is None:
                continue
            bar = self.data.option_bar(pos.contract, ts)
            if bar is None:
                continue
            o, h, l, _ = bar
            long = pos.side is Side.BUY
            sl_hit = pos.sl_price is not None and (l <= pos.sl_price if long else h >= pos.sl_price)
            tgt_hit = pos.target_price is not None and (h >= pos.target_price if long else l <= pos.target_price)
            if sl_hit:  # pessimistic: SL wins ties
                trigger = min(o, pos.sl_price) if long else max(o, pos.sl_price)
                self._close(pos, ts, self._exec_price(pos.side.opposite, trigger), ExitReason.STOP_LOSS)
            elif tgt_hit:
                fill = max(o, pos.target_price) if long else min(o, pos.target_price)
                self._close(pos, ts, _tick(fill), ExitReason.TARGET)

    def square_off(self, ts: datetime, reason: ExitReason = ExitReason.SQUARE_OFF, use_open: bool = True,
                   only=None) -> None:
        """Cancel pending entries and close everything (at bar open, or last known price).

        only: optional predicate(Position) -> bool; when given, close just those positions.
        """
        for o in self._pending:
            o.status = OrderStatus.CANCELLED
        self._pending = []
        for pos in list(self._positions):
            if only is not None and not only(pos):
                continue
            self._close(pos, ts, self._square_off_fill(pos, ts, use_open), reason)

    def _close(self, pos: Position, ts: datetime, price: float, reason: ExitReason) -> None:
        exit_side = pos.side.opposite
        exit_cost = self.costs.cost(exit_side, price, pos.quantity, pos.contract.exchange)
        sign = 1 if pos.side is Side.BUY else -1
        trade = Trade(
            contract=pos.contract, side=pos.side, lots=pos.lots, quantity=pos.quantity,
            entry_time=pos.entry_time, entry_price=pos.entry_price, exit_time=ts, exit_price=price,
            exit_reason=reason, gross_pnl=sign * (price - pos.entry_price) * pos.quantity,
            costs=pos.entry_costs + exit_cost, tag=pos.tag, group=pos.group)
        self._positions.remove(pos)
        self._exits.pop(id(pos), None)
        self._trades.append(trade)
        if self.on_trade_closed:
            self.on_trade_closed(trade)
