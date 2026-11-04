"""Paper-trading persistence (folder `paper/` by default).

  state.json            account + open positions, rewritten atomically after every minute
  trades.csv            every closed trade (all days), appended as it closes
  fills.csv             every simulated fill with the quote at that moment (PaperBroker)
  events-YYYY-MM-DD.jsonl  orders, fills, risk / order-flow blocks, heartbeats
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from algo.core.capital import Withdrawal
from algo.core.models import ExitReason, OptionContract, OptionType, Position, Side, Trade

TRADE_FIELDS = ["symbol", "underlying", "expiry", "strike", "type", "side", "lots", "lot_size", "qty",
                "entry_time", "entry_price", "exit_time", "exit_price", "exit_reason", "gross_pnl", "costs",
                "net_pnl", "tag", "group", "instrument_key", "exchange"]


def _contract_dict(c: OptionContract) -> dict:
    return {"underlying": c.underlying, "expiry": c.expiry.isoformat(), "strike": c.strike,
            "option_type": c.option_type.value, "lot_size": c.lot_size,
            "instrument_key": c.instrument_key, "exchange": c.exchange}


def _contract(d: dict) -> OptionContract:
    return OptionContract(underlying=d["underlying"], expiry=date.fromisoformat(str(d["expiry"])),
                          strike=float(d["strike"]), option_type=OptionType(d["option_type"]),
                          lot_size=int(d["lot_size"]), instrument_key=d.get("instrument_key") or "",
                          exchange=d.get("exchange") or "NSE")


def position_to_dict(p: Position) -> dict:
    return {"contract": _contract_dict(p.contract), "side": p.side.value, "lots": p.lots,
            "entry_price": p.entry_price, "entry_time": p.entry_time.isoformat(), "sl_price": p.sl_price,
            "target_price": p.target_price, "entry_costs": p.entry_costs, "tag": p.tag, "group": p.group}


def position_from_dict(d: dict) -> Position:
    return Position(contract=_contract(d["contract"]), side=Side(d["side"]), lots=int(d["lots"]),
                    entry_price=float(d["entry_price"]), entry_time=datetime.fromisoformat(d["entry_time"]),
                    sl_price=d.get("sl_price"), target_price=d.get("target_price"),
                    entry_costs=float(d.get("entry_costs", 0.0)), tag=d.get("tag", ""), group=d.get("group", ""))


def trade_row(t: Trade) -> dict:
    return {**t.to_dict(), "instrument_key": t.contract.instrument_key, "exchange": t.contract.exchange}


def trade_from_row(r: dict) -> Trade:
    c = _contract({"underlying": r["underlying"], "expiry": r["expiry"], "strike": r["strike"],
                   "option_type": r["type"], "lot_size": r["lot_size"],
                   "instrument_key": r.get("instrument_key", ""), "exchange": r.get("exchange", "NSE")})
    return Trade(contract=c, side=Side(r["side"]), lots=int(r["lots"]), quantity=int(r["qty"]),
                 entry_time=pd.Timestamp(r["entry_time"]).to_pydatetime(), entry_price=float(r["entry_price"]),
                 exit_time=pd.Timestamp(r["exit_time"]).to_pydatetime(), exit_price=float(r["exit_price"]),
                 exit_reason=ExitReason(r["exit_reason"]), gross_pnl=float(r["gross_pnl"]),
                 costs=float(r["costs"]), tag=r.get("tag") or "", group=r.get("group") or "")


@dataclass
class PaperState:
    strategy: str
    underlying: str
    params: dict
    initial_capital: float
    started: str = field(default_factory=lambda: date.today().isoformat())
    day: str | None = None                  # trading day the open positions / today's P&L belong to
    day_closed: bool = False                # end-of-day processing done for `day`
    realised_before_day: float = 0.0        # cumulative net P&L of all days before `day`
    withdrawals: list[dict] = field(default_factory=list)
    halted_on: str | None = None
    positions: list[dict] = field(default_factory=list)
    updated: str | None = None

    def matches(self, strategy: str, underlying: str, params: dict) -> bool:
        return self.strategy == strategy and self.underlying == underlying and self.params == params


class PaperStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def trades_path(self) -> Path:
        return self.root / "trades.csv"

    @property
    def fills_path(self) -> Path:
        return self.root / "fills.csv"

    def events_path(self, day: date) -> Path:
        return self.root / f"events-{day.isoformat()}.jsonl"

    def load(self) -> PaperState | None:
        if not self.state_path.exists():
            return None
        return PaperState(**json.loads(self.state_path.read_text()))

    def save(self, st: PaperState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        st.updated = datetime.now().isoformat(timespec="seconds")
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(st), indent=2, default=str))
        os.replace(tmp, self.state_path)

    def reset(self) -> None:
        """Archive the current paper account (never deletes): paper/ -> paper-archive-<timestamp>/."""
        if self.root.exists() and any(self.root.iterdir()):
            self.root.rename(self.root.with_name(f"{self.root.name}-archive-{datetime.now():%Y%m%d-%H%M%S}"))

    def append_trade(self, t: Trade) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        new = not self.trades_path.exists()
        with self.trades_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            if new:
                w.writeheader()
            w.writerow(trade_row(t))

    def trades(self) -> list[Trade]:
        if not self.trades_path.exists():
            return []
        with self.trades_path.open() as f:
            return [trade_from_row(r) for r in csv.DictReader(f)]

    def event(self, day: date, event: str, /, **data) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        rec = {"t": datetime.now().isoformat(timespec="seconds"), "event": event, **data}
        with self.events_path(day).open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")


def withdrawals_from(st: PaperState) -> list[Withdrawal]:
    return [Withdrawal(date.fromisoformat(w["on"]), float(w["amount"]), float(w["equity_before"]))
            for w in st.withdrawals]


def withdrawals_to(ws: list[Withdrawal]) -> list[dict]:
    return [{"on": w.on.isoformat(), "amount": w.amount, "equity_before": w.equity_before} for w in ws]
