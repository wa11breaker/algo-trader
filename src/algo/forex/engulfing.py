"""Gold Engulfing Strategy (4H) - signal generation.

Rules (from the zerotrade.ai "Gold Engulfing Strategy" description):
- Long: a green candle whose body covers the previous red candle's body, closing above
  the trend SMA (200) and above the fast SMA (14, the Bollinger middle line).
- Short: the mirror image.
- Stop at the engulfing candle's low (long) / high (short); target = rr x risk.
- Risk a fixed % of the starting balance; skip if the stop is closer than
  min_stop_atr x ATR(14); an opposite signal closes the open trade.
"""
from __future__ import annotations

from dataclasses import dataclass, fields

import numpy as np
import pandas as pd

from algo.indicators import atr, sma


@dataclass
class GoldEngulfingParams:
    trend_sma: int = 200
    fast_sma: int = 14
    atr_len: int = 14
    min_stop_atr: float = 0.25
    rr: float = 2.0
    risk_pct: float = 1.0
    exit_on_opposite: bool = True
    # --- optional refinements (defaults = the original rules) ---
    side_mode: str = "both"         # both | long | short
    min_body_atr: float = 0.0       # engulfing body must be >= this x ATR
    pattern_stop: bool = False      # stop beyond both candles instead of the engulfing one only
    stop_buffer_atr: float = 0.0    # extra stop distance in ATR
    max_stop_atr: float = 0.0       # skip if stop distance > this x ATR (0 = off)
    slope_bars: int = 0             # trend SMA must be rising (long) / falling (short) over N bars
    hours: str = ""                 # allowed signal-bar server start hours, e.g. "8,12" ("" = all)
    breakeven_r: float = 0.0        # move stop to entry once price reaches +x R (0 = off)
    max_bars: int = 0               # close at a bar open after N bars in the trade (0 = off)

    @classmethod
    def from_dict(cls, d: dict) -> "GoldEngulfingParams":
        names = {f.name for f in fields(cls)}
        bad = set(d) - names
        if bad:
            raise ValueError(f"Unknown parameter(s) {sorted(bad)}; valid: {sorted(names)}")
        return cls(**d)


def engulfing_masks(h4: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """(bullish, bearish) body-engulfing, same test as algo.indicators.is_engulfing."""
    o, c = h4["open"], h4["close"]
    po, pc = o.shift(), c.shift()
    bull = (pc < po) & (c > o) & (o <= pc) & (c >= po)
    bear = (pc > po) & (c < o) & (o >= pc) & (c <= po)
    return bull, bear


def signals(h4: pd.DataFrame, p: GoldEngulfingParams) -> pd.DataFrame:
    """Per closed bar: `side` (+1 long, -1 short, 0 none), `stop`, `atr`.

    Uses only that bar and earlier bars; the trade is entered at the next bar's open.
    """
    c = h4["close"]
    t = sma(c, p.trend_sma)
    f = sma(c, p.fast_sma)
    bull, bear = engulfing_masks(h4)
    a = atr(h4, p.atr_len)
    long_ = bull & (c > t) & (c > f)
    short = bear & (c < t) & (c < f)
    if p.min_body_atr > 0:
        big = (c - h4["open"]).abs() >= p.min_body_atr * a
        long_, short = long_ & big, short & big
    if p.slope_bars > 0:
        long_ = long_ & (t > t.shift(p.slope_bars))
        short = short & (t < t.shift(p.slope_bars))
    if p.hours:
        ok = pd.Series(h4.index.hour.isin([int(x) for x in str(p.hours).split(",")]), index=h4.index)
        long_, short = long_ & ok, short & ok
    if p.side_mode == "long":
        short = short & False
    elif p.side_mode == "short":
        long_ = long_ & False
    side = np.where(long_, 1, np.where(short, -1, 0))
    lo, hi = h4["low"], h4["high"]
    if p.pattern_stop:
        lo, hi = np.minimum(lo, lo.shift()), np.maximum(hi, hi.shift())
    buf = p.stop_buffer_atr * a
    stop = np.where(side == 1, lo - buf, np.where(side == -1, hi + buf, np.nan))
    return pd.DataFrame({"side": side, "stop": stop, "atr": a,
                         "trend_sma": t, "fast_sma": f}, index=h4.index)
