"""The object a strategy sees. Same API in backtest, paper and live modes."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd

from algo.config import Underlying
from algo.core.broker import Broker, MarketData
from algo.core.capital import CapitalManager
from algo.core.flow_gate import OrderFlowGate
from algo.core.models import ExitReason, OptionContract, OptionType, Order, Position, Side
from algo.core.risk import RiskManager
from algo.core.spread import Spread


class Context:
    def __init__(self, underlying: Underlying, data: MarketData, broker: Broker,
                 square_off_time: time = time(15, 15), risk: RiskManager | None = None,
                 capital: CapitalManager | None = None, flow_gate: OrderFlowGate | None = None) -> None:
        self.underlying = underlying
        self.flow_gate = flow_gate   # order-flow confirmation of entries (paper/live); None = off
        self.risk = risk or RiskManager(None, capital=0)   # disabled by default
        self.capital = capital   # account equity + lot sizing; None = strategy uses fixed lots
        self.data = data
        self.broker = broker
        self.square_off_time = square_off_time
        self.now: datetime | None = None
        self.state: dict = {}          # scratch space for strategies, reset daily
        self._spot_cache: dict = {}
        self._spot_cache_ts: datetime | None = None

    # ---- time -------------------------------------------------------------
    @property
    def today(self) -> date:
        return self.now.date()

    @property
    def bar_close_time(self) -> datetime:
        """Bars are labelled by start time; a 1-min bar closes 1 minute later."""
        return self.now + timedelta(minutes=1)

    # ---- spot data --------------------------------------------------------
    def spot(self, tf: str = "1min", lookback: int | None = None, today_only: bool = False) -> pd.DataFrame:
        """Closed spot bars up to now. tf like '1min', '5min', '15min'.

        Higher timeframes only include *completed* bars, so there is no lookahead.
        `lookback` limits to the last N bars (much faster for indicators).
        `volume` / `oi` come from the front-month future when futures data is stored
        (the index itself has no volume); otherwise they are 0.
        """
        key = (tf, lookback, today_only)
        hit = self._spot_cache.get(key) if self._spot_cache_ts == self.now else None
        if hit is not None:
            return hit
        if self._spot_cache_ts != self.now:
            self._spot_cache, self._spot_cache_ts = {}, self.now
        df = self.data.spot_bars(self.now, tf=tf, lookback=lookback)
        if today_only:
            df = df[df.index.normalize() == pd.Timestamp(self.today)]
        self._spot_cache[key] = df
        return df

    def futures(self, tf: str = "1min", lookback: int | None = None, today_only: bool = False) -> pd.DataFrame:
        """Closed bars of the continuous front-month future (unadjusted; has real volume/OI).

        Column `expiry` (1-min only) shows which contract each bar came from; prices
        jump at roll-overs, so prefer session-based indicators (VWAP) on futures.
        """
        key = ("fut", tf, lookback, today_only)
        if self._spot_cache_ts != self.now:
            self._spot_cache, self._spot_cache_ts = {}, self.now
        if key not in self._spot_cache:
            df = self.data.futures_bars(self.now, tf=tf, lookback=lookback)
            if today_only:
                df = df[df.index.normalize() == pd.Timestamp(self.today)]
            self._spot_cache[key] = df
        return self._spot_cache[key]

    def order_flow(self, tf: str = "1min", lookback: int | None = None, today_only: bool = True,
                   contract: OptionContract | None = None) -> pd.DataFrame:
        """Live order flow per closed bar: volume, buy_vol, sell_vol, delta, cum_delta (since the
        day's first bar). Default source: the front-month future; pass `contract` for an option.
        Raises NotImplementedError in backtests (stored candles have no buy/sell split)."""
        df = self.data.order_flow(self.now, tf, contract.instrument_key if contract else None)
        if today_only:
            df = df[df.index.normalize() == pd.Timestamp(self.today)]
        df = df.copy()
        df["cum_delta"] = df["delta"].groupby(df.index.normalize()).cumsum()
        return df if lookback is None else df.iloc[-lookback:]

    def _flow_block(self, bullish: bool) -> bool:
        if self.flow_gate is None:
            return False
        reason = self.flow_gate.check(self, bullish)
        if reason:
            self.risk.blocked[reason] += 1
        return reason is not None

    @property
    def spot_price(self) -> float:
        return float(self.spot(lookback=1)["close"].iloc[-1])

    # ---- option selection -------------------------------------------------
    def atm_strike(self, spot: float | None = None) -> float:
        step = self.underlying.strike_step
        s = self.spot_price if spot is None else spot
        return round(s / step) * step

    def pick_expiry(self, which: str = "nearest", min_days_to_expiry: int = 0) -> date | None:
        """which: 'nearest' | 'next' | 'monthly' (last expiry in the calendar month)."""
        exps = [e for e in self.data.expiries(self.today) if (e - self.today).days >= min_days_to_expiry]
        if not exps:
            return None
        if which == "nearest":
            return exps[0]
        if which == "next":
            return exps[1] if len(exps) > 1 else None
        if which == "monthly":
            first = exps[0]
            same_month = [e for e in exps if (e.year, e.month) == (first.year, first.month)]
            return same_month[-1]
        raise ValueError(f"unknown expiry selector {which!r}")

    def select_option(self, kind: str | OptionType, moneyness: str = "ATM",
                      expiry: str = "nearest", min_days_to_expiry: int = 0) -> OptionContract | None:
        """moneyness: 'ATM', 'ITM1', 'ITM2', 'OTM1', 'OTM3', ... (strike steps from ATM)."""
        ot = OptionType(kind) if not isinstance(kind, OptionType) else kind
        exp = self.pick_expiry(expiry, min_days_to_expiry)
        if exp is None:
            return None
        m = moneyness.upper()
        shift = 0 if m == "ATM" else int(m[3:])
        if m.startswith("ITM"):
            shift = -shift
        elif not (m.startswith("OTM") or m == "ATM"):
            raise ValueError(f"bad moneyness {moneyness!r}")
        # OTM for CE = higher strike; OTM for PE = lower strike
        direction = 1 if ot is OptionType.CE else -1
        strike = self.atm_strike() + direction * shift * self.underlying.strike_step
        return self.data.contract(exp, strike, ot)

    def ltp(self, contract: OptionContract) -> float | None:
        return self.data.option_ltp(contract, self.now)

    # ---- orders / positions -----------------------------------------------
    def buy(self, contract: OptionContract, lots: int = 1, sl_pct: float | None = None,
            target_pct: float | None = None, tag: str = "", max_loss: float | None = None) -> Order | None:
        """Returns None if the risk manager blocks the entry.

        max_loss: planned worst-case loss in rupees. Default: premium x qty x sl_pct (or the
        full premium without a stop).
        """
        return self._order(contract, Side.BUY, lots, sl_pct, target_pct, tag, max_loss)

    def sell(self, contract: OptionContract, lots: int = 1, sl_pct: float | None = None,
             target_pct: float | None = None, tag: str = "", max_loss: float | None = None) -> Order | None:
        return self._order(contract, Side.SELL, lots, sl_pct, target_pct, tag, max_loss)

    def _order(self, contract, side, lots, sl_pct, target_pct, tag, max_loss=None, check_risk=True,
               group: str = "") -> Order | None:
        if self.now.time() >= self.square_off_time:
            raise RuntimeError("order placed after square-off time")
        if check_risk:
            bullish = (side is Side.BUY) == (contract.option_type is OptionType.CE)
            if self._flow_block(bullish):
                return None
            ltp = self.ltp(contract) or 0.0
            qty = lots * contract.lot_size
            if max_loss is None:
                if side is Side.BUY:
                    max_loss = ltp * qty * (sl_pct / 100 if sl_pct else 1.0)
                else:   # naked short without a declared max loss: treat as unlimited
                    max_loss = float("inf") if not sl_pct else ltp * qty * sl_pct / 100
            if self.risk.check(self, max_loss, ltp * qty if side is Side.BUY else 0.0):
                return None
        order = Order(contract=contract, side=side, lots=lots, placed_at=self.now,
                      sl_pct=sl_pct, target_pct=target_pct, tag=tag, group=group)
        return self.broker.place_order(order)

    # ---- spreads ------------------------------------------------------------
    def open_debit_spread(self, kind: str | OptionType, width_steps: int = 1, moneyness: str = "ATM",
                          expiry: str = "nearest", min_days_to_expiry: int = 0, lots: int = 1,
                          tag: str = "spread", stop_frac: float = 1.0) -> Spread | None:
        """Buy the `moneyness` strike and sell the strike `width_steps` further OTM.

        Risk is checked once for the whole spread: max loss = est. debit x qty x stop_frac.
        Returns None if a strike is missing, the debit is not positive, or risk blocks it.
        """
        ot = OptionType(kind) if not isinstance(kind, OptionType) else kind
        long_c = self.select_option(ot, moneyness, expiry, min_days_to_expiry)
        if long_c is None:
            return None
        step = self.underlying.strike_step * width_steps * (1 if ot is OptionType.CE else -1)
        short_c = self.data.contract(long_c.expiry, long_c.strike + step, ot)
        lp, sp = (self.ltp(long_c), self.ltp(short_c)) if short_c else (None, None)
        if short_c is None or lp is None or sp is None or lp - sp <= 0:
            return None
        debit = lp - sp
        qty = lots * long_c.lot_size
        if self._flow_block(ot is OptionType.CE):
            return None
        if self.risk.check(self, debit * qty * stop_frac, debit * qty):
            return None
        group = f"{tag}@{self.now:%Y%m%d%H%M}"
        self._order(long_c, Side.BUY, lots, None, None, tag, check_risk=False, group=group)
        self._order(short_c, Side.SELL, lots, None, None, tag, check_risk=False, group=group)
        return Spread(kind=ot, long=long_c, short=short_c, lots=lots, tag=tag, opened_at=self.now,
                      est_debit=debit, width=abs(step), group=group)

    def open_credit_spread(self, kind: str | OptionType, short_steps: int = 2, wing_steps: int = 2,
                           expiry: str = "nearest", min_days_to_expiry: int = 0, lots: int = 1,
                           tag: str = "credit", max_loss: float | None = None,
                           margin: float | None = None) -> Spread | None:
        """Sell the strike `short_steps` OTM and buy the wing `wing_steps` further OTM.

        The returned Spread has `long` = wing (bought), `short` = sold strike and a negative
        est_debit (= -credit). Risk is checked once: max_loss defaults to (width - credit) x qty,
        cost (position size) is `margin` (default: width x qty).
        """
        ot = OptionType(kind) if not isinstance(kind, OptionType) else kind
        short_c = self.select_option(ot, f"OTM{short_steps}" if short_steps else "ATM", expiry, min_days_to_expiry)
        if short_c is None:
            return None
        step = self.underlying.strike_step * wing_steps * (1 if ot is OptionType.CE else -1)
        wing_c = self.data.contract(short_c.expiry, short_c.strike + step, ot)
        sp, wp = (self.ltp(short_c), self.ltp(wing_c)) if wing_c else (None, None)
        if wing_c is None or sp is None or wp is None or sp - wp <= 0:
            return None
        credit, width = sp - wp, abs(step)
        qty = lots * short_c.lot_size
        if max_loss is None:
            max_loss = (width - credit) * qty
        if self._flow_block(ot is OptionType.PE):
            return None
        if self.risk.check(self, max_loss, width * qty if margin is None else margin):
            return None
        group = f"{tag}@{self.now:%Y%m%d%H%M}"
        self._order(wing_c, Side.BUY, lots, None, None, tag, check_risk=False, group=group)
        self._order(short_c, Side.SELL, lots, None, None, tag, check_risk=False, group=group)
        return Spread(kind=ot, long=wing_c, short=short_c, lots=lots, tag=tag, opened_at=self.now,
                      est_debit=-credit, width=width, group=group)

    def spread_legs(self, spread: Spread) -> tuple[Position | None, Position | None]:
        long_p = short_p = None
        for p in self.broker.positions():
            if p.group == spread.group:
                if p.side is Side.BUY:
                    long_p = p
                else:
                    short_p = p
        return long_p, short_p

    def spread_pending(self, spread: Spread) -> bool:
        return any(o.group == spread.group for o in self.broker.pending_orders())

    def spread_entry_debit(self, spread: Spread) -> float | None:
        lp, sp = self.spread_legs(spread)
        return None if lp is None or sp is None else lp.entry_price - sp.entry_price

    def spread_value(self, spread: Spread) -> float | None:
        """Current long - short price per unit (what the spread would sell for)."""
        a, b = self.ltp(spread.long), self.ltp(spread.short)
        return None if a is None or b is None else a - b

    def close_spread(self, spread: Spread, reason: ExitReason = ExitReason.SIGNAL) -> None:
        """Exit both legs (and cancel any unfilled leg orders)."""
        for o in list(self.broker.pending_orders()):
            if o.group == spread.group:
                self.broker.cancel(o)
        for p in self.broker.positions():
            if p.group == spread.group:
                self.broker.exit_position(p, reason)
        spread.closing = True

    @property
    def positions(self) -> list[Position]:
        return self.broker.positions()

    @property
    def active_positions(self) -> list[Position]:
        """Open positions that do not already have an exit pending."""
        return [p for p in self.broker.positions() if not self.broker.exit_requested(p)]

    @property
    def has_position(self) -> bool:
        return bool(self.broker.positions()) or bool(self.broker.pending_orders())

    def exit(self, position: Position, reason: ExitReason = ExitReason.SIGNAL) -> None:
        self.broker.exit_position(position, reason)

    def exit_all(self, reason: ExitReason = ExitReason.SIGNAL) -> None:
        for p in list(self.broker.positions()):
            self.broker.exit_position(p, reason)

    def trades_today(self) -> int:
        """Entries today, including open positions and pending orders."""
        closed = sum(1 for t in self.broker.trades() if t.entry_time.date() == self.today)
        open_ = sum(1 for p in self.broker.positions() if p.entry_time.date() == self.today)
        return closed + open_ + len(self.broker.pending_orders())

    @property
    def can_enter(self) -> bool:
        return self.now.time() < self.square_off_time
