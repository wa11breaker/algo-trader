"""Opening Range Breakout: buy CE on a close above the opening range high, PE below the low."""
from __future__ import annotations

from datetime import datetime, time, timedelta

from algo.core.context import Context
from algo.core.models import OptionType
from algo.core.strategy import Strategy

SESSION_OPEN = time(9, 15)


class OrbBuy(Strategy):
    """Opening range breakout: close above range high -> buy CE, below range low -> buy PE."""

    name = "orb_buy"
    defaults = {
        "range_minutes": 15,
        "confirm_tf": "5min",      # breakout must close beyond the range on this timeframe
        "buffer_pct": 0.0,         # extra % beyond the range high/low
        "moneyness": "ATM",
        "expiry": "nearest",
        "min_dte": 0,
        "lots": 1,
        "sl_pct": 30.0,
        "target_pct": 60.0,
        "max_trades_per_day": 2,   # at most one per direction
        "entry_end": "14:30",
    }

    def on_day_start(self, ctx: Context) -> None:
        ctx.state.update(range=None, done=set(), last_bar=None)

    def on_bar(self, ctx: Context) -> None:
        p, st = self.p, ctx.state
        range_end = datetime.combine(ctx.today, SESSION_OPEN) + timedelta(minutes=p["range_minutes"])
        if ctx.bar_close_time < range_end:
            return
        if st["range"] is None:
            today = ctx.spot(lookback=400, today_only=True)
            first = today[today.index < range_end]
            if first.empty:
                return
            buf = p["buffer_pct"] / 100
            st["range"] = (first["high"].max() * (1 + buf), first["low"].min() * (1 - buf))
            return

        bars = ctx.spot(p["confirm_tf"], lookback=2, today_only=True)
        if bars.empty or bars.index[-1] == st["last_bar"] or bars.index[-1] < range_end:
            return
        st["last_bar"] = bars.index[-1]
        hi, lo = st["range"]
        close = float(bars["close"].iloc[-1])
        want = OptionType.CE if close > hi else OptionType.PE if close < lo else None
        if (want is None or want in st["done"] or ctx.positions or ctx.broker.pending_orders()
                or ctx.now.strftime("%H:%M") >= p["entry_end"]
                or ctx.trades_today() >= p["max_trades_per_day"]):
            return
        opt = ctx.select_option(want, p["moneyness"], p["expiry"], p["min_dte"])
        if opt is None:
            return
        st["done"].add(want)
        ctx.buy(opt, lots=p["lots"], sl_pct=p["sl_pct"], target_pct=p["target_pct"], tag=f"orb_{want.value}")
