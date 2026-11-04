"""EMA crossover on the index -> buy CE (bullish) / PE (bearish).

Signals are evaluated once per completed `tf` bar of the spot index.
"""
from __future__ import annotations

from algo.core.context import Context
from algo.core.models import ExitReason, OptionType
from algo.core.strategy import Strategy
from algo.indicators import crossed_above, crossed_below, ema, supertrend


class EmaCrossBuy(Strategy):
    """EMA crossover on the index (per completed tf bar): bullish -> buy CE, bearish -> buy PE."""

    name = "ema_cross_buy"
    defaults = {
        "tf": "5min",
        "fast": 9,
        "slow": 21,
        "use_supertrend": False,   # require supertrend direction to agree
        "st_period": 10,
        "st_mult": 3.0,
        "moneyness": "ATM",        # ATM / ITM1 / OTM1 ...
        "expiry": "nearest",       # nearest / next / monthly
        "min_dte": 0,              # skip expiries closer than N days (1 = avoid expiry-day options)
        "lots": 1,
        "sl_pct": 30.0,
        "target_pct": 60.0,
        "max_trades_per_day": 3,
        "entry_start": "09:30",
        "entry_end": "14:45",
        "exit_on_reverse": True,   # close an open CE on a bearish cross (and vice versa)
    }

    def on_start(self, ctx: Context) -> None:
        self._last_bar = None

    def on_bar(self, ctx: Context) -> None:
        p = self.p
        bars = ctx.spot(p["tf"], lookback=max(250, p["slow"] * 10))
        if len(bars) < p["slow"] + 2 or bars.index[-1] == self._last_bar:
            return  # wait for a newly completed bar
        self._last_bar = bars.index[-1]

        close = bars["close"]
        f, s = ema(close, p["fast"]), ema(close, p["slow"])
        bull, bear = crossed_above(f, s), crossed_below(f, s)
        if not (bull or bear):
            return
        crossed = OptionType.CE if bull else OptionType.PE
        if p["exit_on_reverse"]:  # exits use the raw crossover, unfiltered
            for pos in ctx.positions:
                if pos.contract.option_type is not crossed:
                    ctx.exit(pos, ExitReason.SIGNAL)
        if p["use_supertrend"]:
            st_dir = int(supertrend(bars, p["st_period"], p["st_mult"])["st_dir"].iloc[-1])
            bull, bear = bull and st_dir == 1, bear and st_dir == -1

        want = OptionType.CE if bull else OptionType.PE if bear else None
        if want is None or ctx.active_positions or ctx.broker.pending_orders():
            return

        t = ctx.now.strftime("%H:%M")
        if not (p["entry_start"] <= t < p["entry_end"]) or ctx.trades_today() >= p["max_trades_per_day"]:
            return
        opt = ctx.select_option(want, p["moneyness"], p["expiry"], p["min_dte"])
        if opt is None:
            return
        ctx.buy(opt, lots=p["lots"], sl_pct=p["sl_pct"], target_pct=p["target_pct"],
                tag="bull" if bull else "bear")
