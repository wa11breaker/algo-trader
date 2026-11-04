"""Paper broker: SimBroker's bookkeeping with fills from live quotes.

fill_model:
  * quote (default) - orders placed while processing bar t fill right after it (at the start of
    bar t+1, when the real order would reach the exchange): buys at the best ask, sells at the
    best bid. Without a two-sided quote: last price +/- the configured slippage.
  * bar_open - exactly the backtest model (open of the option's next 1-min bar +/- slippage).
SL / target checks, square-off and costs are SimBroker's. Every market fill is logged to
fills.csv with the quote at that moment, so slippage settings can be calibrated.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from algo.backtest.costs import CostModel
from algo.backtest.sim_broker import SimBroker, _tick
from algo.core.models import Position, Side, Trade

log = logging.getLogger(__name__)
FILL_FIELDS = ["time", "symbol", "side", "kind", "ltp", "bid", "ask", "fill", "backtest_model_fill",
               "slip_vs_ltp"]


class PaperBroker(SimBroker):
    def __init__(self, data, costs: CostModel, slippage_pct: float = 0.5, slippage_min: float = 0.5,
                 fill_model: str = "quote", fills_path: Path | None = None,
                 on_fill: Callable[[dict], None] | None = None) -> None:
        if fill_model not in ("quote", "bar_open"):
            raise ValueError("fill_model must be 'quote' or 'bar_open'")
        super().__init__(data, costs, slippage_pct, slippage_min)
        self.fill_model = fill_model
        self.fills_path = Path(fills_path) if fills_path else None
        self.on_fill = on_fill

    # ---- quote fills ----------------------------------------------------------
    def after_bar(self, ts: datetime) -> None:
        """Called after the strategy saw bar ts: fill new entries / exits at the live quote."""
        if self.fill_model != "quote" or not (self._pending or self._exits):
            return
        nxt = ts + timedelta(minutes=1)
        self._fill_entries(nxt)
        self._fill_exits(nxt)

    def _quote_fill(self, contract, side: Side, ts: datetime, kind: str) -> float | None:
        q = self.data.quote(contract, ts)
        if q is None:
            return None
        if side is Side.BUY and q.ask:
            price = _tick(q.ask)
        elif side is Side.SELL and q.bid:
            price = _tick(q.bid)
        else:
            price = self._exec_price(side, q.ltp)
        self._log_fill(ts, contract, side, kind, q.ltp, q.bid, q.ask, price)
        return price

    def _market_fill(self, contract, side: Side, ts: datetime, bar) -> float:
        if self.fill_model == "quote":
            p = self._quote_fill(contract, side, ts, "market")
            if p is not None:
                return p
        price = super()._market_fill(contract, side, ts, bar)
        self._log_fill(ts, contract, side, "market", bar[0], None, None, price)
        return price

    def _square_off_fill(self, pos: Position, ts: datetime, use_open: bool) -> float:
        if self.fill_model == "quote":
            p = self._quote_fill(pos.contract, pos.side.opposite, ts, "square_off")
            if p is not None:
                return p
        return super()._square_off_fill(pos, ts, use_open)

    def _log_fill(self, ts, contract, side: Side, kind: str, ltp, bid, ask, fill) -> None:
        row = {"time": ts, "symbol": contract.instrument_key or contract.symbol, "side": side.value,
               "kind": kind, "ltp": ltp, "bid": bid, "ask": ask, "fill": fill,
               "backtest_model_fill": self._exec_price(side, ltp) if ltp else None,
               "slip_vs_ltp": round(abs(fill - ltp), 2) if ltp else None}
        if self.fills_path:
            new = not self.fills_path.exists()
            self.fills_path.parent.mkdir(parents=True, exist_ok=True)
            with self.fills_path.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FILL_FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
        if self.on_fill:
            self.on_fill(row)

    # ---- restart support ------------------------------------------------------
    def restore(self, positions: list[Position], trades: list[Trade]) -> None:
        """Re-load open positions and today's closed trades after a restart."""
        self._positions = list(positions)
        self._trades = list(trades)
