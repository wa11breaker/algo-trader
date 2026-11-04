"""Transaction charges for Indian index options (per executed leg)."""
from __future__ import annotations

from dataclasses import dataclass

from algo.core.models import Side


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: float
    stt: float
    txn: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.txn + self.sebi + self.stamp + self.gst


class CostModel:
    def __init__(self, cfg: dict) -> None:
        self.brokerage = float(cfg["brokerage_per_order"])
        self.stt_sell = float(cfg["stt_sell_pct"]) / 100
        self.txn = {k: float(v) / 100 for k, v in cfg["txn_pct"].items()}
        self.sebi = float(cfg["sebi_per_crore"]) / 1e7
        self.stamp_buy = float(cfg["stamp_buy_pct"]) / 100
        self.gst = float(cfg["gst_pct"]) / 100

    def charges(self, side: Side, price: float, quantity: int, exchange: str = "NSE") -> ChargeBreakdown:
        turnover = price * quantity
        brokerage = self.brokerage if turnover > 0 else 0.0
        stt = turnover * self.stt_sell if side is Side.SELL else 0.0
        txn = turnover * self.txn[exchange]
        sebi = turnover * self.sebi
        stamp = turnover * self.stamp_buy if side is Side.BUY else 0.0
        gst = (brokerage + txn + sebi) * self.gst
        return ChargeBreakdown(brokerage, stt, txn, sebi, stamp, gst)

    def cost(self, side: Side, price: float, quantity: int, exchange: str = "NSE") -> float:
        return self.charges(side, price, quantity, exchange).total
