"""Build tiny hand-crafted markets for scenario tests."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

import pandas as pd

from algo.core.context import Context
from algo.core.models import OptionType
from algo.core.strategy import Strategy
from algo.data.store import DataStore

from .conftest import option_price


def expand(candles: list[tuple], day: date, start: time = time(9, 15)) -> pd.DataFrame:
    """5-min candles (o, h, l, c) -> 1-min bars whose 5-min aggregate is exactly that candle.

    Path inside each candle: o -> first extreme -> second extreme -> c -> c
    (high first for red candles, low first for green ones).
    """
    rows, ts = [], pd.Timestamp(datetime.combine(day, start))
    for o, h, l, c in candles:
        a, b = (h, l) if c <= o else (l, h)
        path = [o, a, b, c, c, c]
        for i in range(5):
            x, y = path[i], path[i + 1]
            rows.append((ts, x, max(x, y), min(x, y), y))
            ts += pd.Timedelta(minutes=1)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"]).set_index("ts")
    df["volume"] = 0.0
    df["oi"] = 0.0
    return df


def flat(price: float, n: int, wiggle: float = 4.0) -> list[tuple]:
    """n quiet 5-min candles around price."""
    return [(price, price + wiggle, price - wiggle, price + (1 if i % 2 else -1)) for i in range(n)]


def fill_day(candles: list[tuple], last_price: float) -> list[tuple]:
    """Pad to a full 75-candle session with quiet candles."""
    return candles + flat(last_price, 75 - len(candles))


def smooth_price(spot: float, strike: float, typ: str, minutes_left: float) -> float:
    """Intrinsic + time value that decays away from the strike (so spreads have value)."""
    intrinsic = max(spot - strike, 0) if typ == "CE" else max(strike - spot, 0)
    return round(intrinsic + 60 * math.exp(-abs(spot - strike) / 150), 2)


def build_market(root, days: dict[date, list[tuple]], expiry: date, ul: str = "NIFTY",
                 pricer=option_price) -> DataStore:
    return save_market(root, pd.concat(expand(c, d) for d, c in days.items()), expiry, ul, pricer)


def save_market(root, spot: pd.DataFrame, expiry: date, ul: str = "NIFTY", pricer=option_price) -> DataStore:
    """Store 1-min spot bars plus a strike ladder of options priced from the spot close."""
    store = DataStore(root)
    for (y, m), g in spot.groupby([spot.index.year, spot.index.month]):
        store.save_spot_month(ul, y, m, g)
    strikes = range(22500, 23550, 50)
    store.save_contracts(ul, expiry, [
        {"instrument_key": f"NSE_FO|{k}{t}", "trading_symbol": f"{ul} {k} {t}", "strike_price": float(k),
         "instrument_type": t, "lot_size": 75, "expiry": expiry.isoformat(), "exchange": "NSE",
         "segment": "NSE_FO", "tick_size": 5, "weekly": True, "underlying_key": "x"}
        for k in strikes for t in ("CE", "PE")])
    mins_left = ((pd.Timestamp(expiry) + pd.Timedelta(hours=15, minutes=30)) - spot.index) / pd.Timedelta(minutes=1)
    for k in strikes:
        for t in ("CE", "PE"):
            c = [pricer(s, k, t, m) for s, m in zip(spot["close"], mins_left)]
            store.save_option(ul, expiry, k, t, pd.DataFrame(
                {"open": c, "high": c, "low": c, "close": c, "volume": 100.0, "oi": 100.0}, index=spot.index))
    return store


# ---- a simple test-only strategy (was algo.strategies.orb_buy) --------------------------
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
