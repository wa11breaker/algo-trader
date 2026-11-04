"""Index futures / options of one underlying, from the Fyers symbol master."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

from algo.config import Underlying
from algo.core.models import OptionContract, OptionType

IST = timezone(timedelta(hours=5, minutes=30))


def _expiry_date(v) -> date:
    return datetime.fromtimestamp(int(float(v)), IST).date()


class FnoUniverse:
    """Tradable options and futures of `underlying` (rows of the Fyers F&O symbol master)."""

    def __init__(self, master: dict, underlying: Underlying) -> None:
        self.ul = underlying
        opts, futs = [], []
        for sym, r in master.items():
            if r.get("underSym") != underlying.name or not r.get("expiryDate"):
                continue
            if str(r.get("tradeStatus", 1)) != "1":
                continue
            row = {"symbol": r.get("symTicker") or sym, "expiry": _expiry_date(r["expiryDate"]),
                   "lot_size": int(r.get("minLotSize") or 0)}
            ot = r.get("optType")
            if ot in ("CE", "PE"):
                opts.append({**row, "strike": float(r["strikePrice"]), "type": ot})
            elif str(r.get("exInstType")) == "11" or sym.endswith("FUT"):
                futs.append(row)
        self.options = pd.DataFrame(opts, columns=["symbol", "expiry", "lot_size", "strike", "type"])
        self.futures = pd.DataFrame(futs, columns=["symbol", "expiry", "lot_size"]).sort_values("expiry")
        self._by_key = {(r.expiry, r.strike, r.type): r for r in self.options.itertuples(index=False)}
        self._by_symbol = {r.symbol: r for r in self.options.itertuples(index=False)}

    def expiries(self, on: date) -> list[date]:
        return sorted(e for e in self.options["expiry"].unique() if e >= on)

    def strikes(self, expiry: date) -> list[float]:
        return sorted(self.options.loc[self.options["expiry"] == expiry, "strike"].unique())

    def contract(self, expiry: date, strike: float, option_type: OptionType) -> OptionContract | None:
        r = self._by_key.get((expiry, float(strike), OptionType(option_type).value))
        return None if r is None else self._to_contract(r)

    def by_symbol(self, symbol: str) -> OptionContract | None:
        r = self._by_symbol.get(symbol)
        return None if r is None else self._to_contract(r)

    def _to_contract(self, r) -> OptionContract:
        return OptionContract(underlying=self.ul.name, expiry=r.expiry, strike=float(r.strike),
                              option_type=OptionType(r.type), lot_size=int(r.lot_size),
                              instrument_key=r.symbol, exchange=self.ul.exchange)

    def front_future(self, on: date, roll_days_before: int = 1) -> str | None:
        """Nearest future with on <= expiry - roll_days_before (same rule as the backtest's
        continuous futures)."""
        for r in self.futures.itertuples(index=False):
            if on <= r.expiry - timedelta(days=roll_days_before):
                return r.symbol
        return None

    def near_atm(self, spot: float, on: date, around: int, n_expiries: int = 1) -> list[str]:
        """Option symbols within `around` strike steps of ATM for the nearest n expiries."""
        step = self.ul.strike_step
        atm = round(spot / step) * step
        lo, hi = atm - around * step, atm + around * step
        exps = self.expiries(on)[:n_expiries]
        o = self.options
        sel = o[o["expiry"].isin(exps) & (o["strike"] >= lo) & (o["strike"] <= hi)]
        return sel["symbol"].tolist()
