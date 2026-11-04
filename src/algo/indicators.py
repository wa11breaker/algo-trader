"""Causal indicators and resampling (no future data is used)."""
from __future__ import annotations

import numpy as np
import pandas as pd

OHLC_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "oi": "last",
            "buy_vol": "sum", "sell_vol": "sum", "delta": "sum"}


def tf_minutes(tf: str) -> int:
    tf = tf.lower().strip()
    for suffix in ("minute", "min", "m"):
        if tf.endswith(suffix):
            return int(tf[: -len(suffix)])
    raise ValueError(f"unsupported timeframe {tf!r} (use e.g. '5min')")


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Resample 1-min bars (index = bar start) to N-min bars per session.

    Bins are anchored to 09:15 each day so 5/15/75-min bars line up with
    exchange conventions. Returns bars labelled by start time plus a
    `bar_end` column (start + N min) so callers can drop incomplete bars.
    """
    n = tf_minutes(tf)
    if n == 1 or df.empty:
        out = df.copy()
        out["bar_end"] = out.index + pd.Timedelta(minutes=1)
        return out
    agg = {k: v for k, v in OHLC_AGG.items() if k in df.columns}
    day = df.index.normalize()
    session_start = day + pd.Timedelta(hours=9, minutes=15)
    offset_min = ((df.index - session_start) / pd.Timedelta(minutes=1)).astype(int)
    bucket = session_start + pd.to_timedelta((offset_min // n) * n, unit="min")
    out = df.groupby(bucket).agg(agg)
    out.index.name = "ts"
    # A bucket is complete once its last minute has closed (bar_end).
    out["bar_end"] = out.index + pd.Timedelta(minutes=n)
    return out


def ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period).mean()


def rsi(s: pd.Series, period: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """Returns columns `st` (line) and `st_dir` (+1 uptrend, -1 downtrend)."""
    a = atr(df, period).to_numpy()
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
    close = df["close"].to_numpy()
    upper = hl2 + multiplier * a
    lower = hl2 - multiplier * a
    n = len(df)
    fu, fl = upper.copy(), lower.copy()
    direction = np.ones(n, dtype=int)
    line = np.full(n, np.nan)
    for i in range(1, n):
        fu[i] = upper[i] if (upper[i] < fu[i - 1] or close[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lower[i] if (lower[i] > fl[i - 1] or close[i - 1] < fl[i - 1]) else fl[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] < fl[i] else 1
        else:
            direction[i] = 1 if close[i] > fu[i] else -1
        line[i] = fl[i] if direction[i] == 1 else fu[i]
    return pd.DataFrame({"st": line, "st_dir": direction}, index=df.index)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP. Needs real volume (futures, or spot bars with futures volume merged in).

    If volume is all zero (index without futures data) it falls back to a running
    typical-price mean, which is NOT a real VWAP.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3
    day = df.index.normalize()
    vol = df["volume"] if "volume" in df else pd.Series(0, index=df.index)
    if (vol > 0).any():
        return (tp * vol).groupby(day).cumsum() / vol.groupby(day).cumsum()
    return tp.groupby(day).expanding().mean().reset_index(level=0, drop=True)


def vwap_bands(df: pd.DataFrame) -> pd.DataFrame:
    """Session VWAP and the volume-weighted standard deviation of typical price around it.

    sd = sqrt(cum(v * tp^2) / cum(v) - vwap^2), reset every session; bands are vwap +/- k * sd.
    Days with no volume at all use equal weights (like `vwap`'s fallback), which is NOT a real VWAP.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3
    day = df.index.normalize()
    vol = df["volume"] if "volume" in df else pd.Series(0.0, index=df.index)
    has_vol = vol.groupby(day).transform("sum") > 0
    w = vol.where(has_vol, 1.0).astype(float)
    cw = w.groupby(day).cumsum()
    vw = (tp * w).groupby(day).cumsum() / cw
    var = (tp * tp * w).groupby(day).cumsum() / cw - vw * vw
    return pd.DataFrame({"vwap": vw, "sd": np.sqrt(var.clip(lower=0.0))}, index=df.index)


def crossed_above(a: pd.Series, b: pd.Series) -> bool:
    """True if a crossed above b on the latest bar."""
    if len(a) < 2:
        return False
    return bool(a.iloc[-2] <= b.iloc[-2] and a.iloc[-1] > b.iloc[-1])


def crossed_below(a: pd.Series, b: pd.Series) -> bool:
    if len(a) < 2:
        return False
    return bool(a.iloc[-2] >= b.iloc[-2] and a.iloc[-1] < b.iloc[-1])


# ---- candle patterns ----------------------------------------------------------
# `direction` is the side of the breakout being rejected:
#   "up"   -> look for bearish rejection (upper wick / bearish engulfing)
#   "down" -> look for bullish rejection (lower wick / bullish engulfing)

def is_pin_bar(bar, direction: str, wick_ratio: float = 0.5) -> bool:
    """Rejection wick: wick on the breakout side >= wick_ratio x range, close in the opposite half."""
    o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
    rng = h - l
    if rng <= 0:
        return False
    if direction == "up":
        return (h - max(o, c)) >= wick_ratio * rng and c <= l + rng / 2
    if direction == "down":
        return (min(o, c) - l) >= wick_ratio * rng and c >= l + rng / 2
    raise ValueError(direction)


def is_engulfing(prev, cur, direction: str) -> bool:
    """Bearish engulfing after an upside push (direction='up'), bullish after a downside push."""
    po, pc, co, cc = float(prev["open"]), float(prev["close"]), float(cur["open"]), float(cur["close"])
    if direction == "up":
        return pc > po and cc < co and co >= pc and cc <= po
    if direction == "down":
        return pc < po and cc > co and co <= pc and cc >= po
    raise ValueError(direction)


def previous_session(df: pd.DataFrame, day) -> pd.DataFrame:
    """Bars of the last session before `day` (a date)."""
    prior = df[df.index.normalize() < pd.Timestamp(day)]
    if prior.empty:
        return prior
    last = prior.index[-1].normalize()
    return prior[prior.index.normalize() == last]


def previous_week(df: pd.DataFrame, day) -> pd.DataFrame:
    """Bars of the last ISO week (Mon-Sun) before the week containing `day`."""
    d = pd.Timestamp(day)
    week_start = (d - pd.Timedelta(days=d.weekday())).normalize()
    prior = df[df.index < week_start]
    if prior.empty:
        return prior
    last = prior.index[-1]
    last_week_start = (last - pd.Timedelta(days=last.weekday())).normalize()
    return prior[prior.index >= last_week_start]



# ---- volatility / trend strength / structure --------------------------------------

def bollinger(close: pd.Series, period: int = 20, k: float = 2.0) -> pd.DataFrame:
    """Columns mid / upper / lower / width (width = (upper - lower) / mid)."""
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "width": (upper - lower) / mid},
                        index=close.index)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ADX."""
    up, dn = df["high"].diff(), -df["low"].diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    a = atr(df, period).replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / a
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / a
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0.0)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def swing_points(df: pd.DataFrame, left: int = 2, right: int = 2) -> pd.DataFrame:
    """Confirmed pivot highs / lows, without lookahead.

    A bar is a pivot high if its high is above the `left` bars before it and not below the `right`
    bars after it (so the first of two equal highs is the pivot). It only becomes KNOWN `right` bars later, so the result is reported on the
    confirming bar: columns `swing_high` / `swing_low` hold the pivot price on the row where it was
    confirmed (NaN elsewhere); `high_ts` / `low_ts` hold the pivot bar's own timestamp.
    """
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    sh, sl = np.full(n, np.nan), np.full(n, np.nan)
    hts = np.full(n, np.datetime64("NaT", "ns"))
    lts = hts.copy()
    idx = df.index.to_numpy()
    for i in range(left, n - right):
        # strictly beyond the bars before, at least equal to the bars after (the first of equal highs wins)
        if h[i] > h[i - left:i].max() and h[i] >= h[i + 1:i + right + 1].max():
            sh[i + right], hts[i + right] = h[i], idx[i]
        if l[i] < l[i - left:i].min() and l[i] <= l[i + 1:i + right + 1].min():
            sl[i + right], lts[i + right] = l[i], idx[i]
    return pd.DataFrame({"swing_high": sh, "swing_low": sl, "high_ts": hts, "low_ts": lts}, index=df.index)
