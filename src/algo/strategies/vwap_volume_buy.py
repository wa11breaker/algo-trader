"""VWAP + volume breakout (needs futures data for volume)."""
from __future__ import annotations

from algo.core.context import Context
from algo.core.models import ExitReason, OptionType
from algo.core.strategy import Strategy
from algo.indicators import vwap


class VwapVolumeBuy(Strategy):
    """Futures close crosses session VWAP on a volume spike -> buy CE (above) / PE (below)."""

    name = "vwap_volume_buy"
    defaults = {
        "tf": "5min",
        "vol_lookback": 20,        # bars for average volume
        "vol_mult": 1.5,           # signal bar volume must be >= vol_mult x average
        "moneyness": "ATM",
        "expiry": "nearest",
        "min_dte": 0,
        "lots": 1,
        "sl_pct": 30.0,
        "target_pct": 60.0,
        "max_trades_per_day": 3,
        "entry_start": "09:30",
        "entry_end": "14:45",
        "exit_on_vwap_cross": True,  # exit a CE when price closes back below VWAP (and vice versa)
    }

    def on_start(self, ctx: Context) -> None:
        self._last_bar = None

    def on_bar(self, ctx: Context) -> None:
        p = self.p
        bars = ctx.futures(p["tf"], lookback=400)
        if bars.empty or bars.index[-1] == self._last_bar:
            return
        self._last_bar = bars.index[-1]
        today = bars[bars.index.normalize() == bars.index[-1].normalize()]
        if len(today) < 2:
            return
        vw = vwap(today)
        close, prev_close = today["close"].iloc[-1], today["close"].iloc[-2]
        above, was_above = close > vw.iloc[-1], prev_close > vw.iloc[-2]

        if p["exit_on_vwap_cross"]:
            for pos in ctx.active_positions:
                if (pos.contract.option_type is OptionType.CE and not above) or \
                   (pos.contract.option_type is OptionType.PE and above):
                    ctx.exit(pos, ExitReason.SIGNAL)

        if above == was_above:
            return  # no VWAP cross on this bar
        avg_vol = bars["volume"].iloc[-p["vol_lookback"] - 1:-1].mean()
        if not avg_vol or today["volume"].iloc[-1] < p["vol_mult"] * avg_vol:
            return
        t = ctx.now.strftime("%H:%M")
        if (ctx.active_positions or ctx.broker.pending_orders() or not (p["entry_start"] <= t < p["entry_end"])
                or ctx.trades_today() >= p["max_trades_per_day"]):
            return
        want = OptionType.CE if above else OptionType.PE
        opt = ctx.select_option(want, p["moneyness"], p["expiry"], p["min_dte"])
        if opt:
            ctx.buy(opt, lots=p["lots"], sl_pct=p["sl_pct"], target_pct=p["target_pct"], tag=f"vwap_{want.value}")
