"""VWAP + market structure (higher highs / higher lows).

    Structure (long)  today's last two confirmed swing highs are rising (HH) and the last two confirmed
                      swing lows are rising (HL); swings are `swing_len`-bar pivots, known only
                      `swing_len` bars after the pivot (no lookahead)
    Trigger           a NEW higher low is confirmed on this bar, it held above VWAP - pierce_atr x ATR
                      and (vwap_touch_atr set) came within vwap_touch_atr x ATR of VWAP; the current
                      close is above VWAP and above the pivot bar's high (continuation)
    Rejection         rejection=pattern: the pivot bar is a bullish pin bar or engulfing bar
    Volume            vol_mult > 0: the current bar's volume >= vol_mult x the 20-bar average
                      (futures volume, Oct 2024+; with no volume data the filter is skipped)
    Stop              below the higher low
Shorts: lower highs / lower lows below VWAP, mirrored.
"""
from __future__ import annotations

import pandas as pd

from algo.core.context import Context
from algo.indicators import is_engulfing, is_pin_bar, swing_points, vwap
from algo.strategies.bar_signal import COMMON, BarSignalStrategy


class VwapStructure(BarSignalStrategy):
    """Trend structure (HH+HL / LH+LL) on the right side of VWAP; enter on a fresh higher low / lower high."""

    name = "vwap_structure"
    tag_prefix = "vms"
    choices = {"rejection": ("none", "pattern")}
    defaults = {
        **COMMON,
        "swing_len": 2,
        "vwap_touch_atr": None,  # None = HL anywhere above VWAP; else must be within this of VWAP
        "pierce_atr": 0.3,
        "rejection": "none",
        "wick_ratio": 0.5,
        "vol_mult": 0.0,
        "vol_lookback": 20,
    }

    def signal(self, ctx: Context, bars: pd.DataFrame, today: pd.DataFrame) -> None:
        p = self.p
        k = p["swing_len"]
        if len(today) < 2 * k + 3:
            return
        sw = swing_points(today, k, k)
        new_low, new_high = pd.notna(sw["swing_low"].iloc[-1]), pd.notna(sw["swing_high"].iloc[-1])
        if not (new_low or new_high):
            return
        vw = vwap(today)
        v, close = float(vw.iloc[-1]), float(today["close"].iloc[-1])
        a = self.bar_atr(bars)
        highs, lows = sw["swing_high"].dropna(), sw["swing_low"].dropna()
        for d, fresh in ((1, new_low), (-1, new_high)):
            if not fresh:
                continue
            same = lows if d == 1 else highs          # the pivots that just formed the HL (LH)
            other = highs if d == 1 else lows
            if len(same) < 2 or len(other) < 2:
                continue
            if not (d * (same.iloc[-1] - same.iloc[-2]) > 0 and d * (other.iloc[-1] - other.iloc[-2]) > 0):
                continue
            pivot = float(same.iloc[-1])
            pivot_ts = sw["low_ts" if d == 1 else "high_ts"].iloc[-1]
            pbar = today.loc[pivot_ts]
            pv = float(vw.loc[pivot_ts])
            gap = d * (pivot - pv)                   # >0: the HL held above VWAP
            if gap < -p["pierce_atr"] * a or (p["vwap_touch_atr"] is not None and gap > p["vwap_touch_atr"] * a):
                self.skips["vwap_distance"] += 1
                continue
            if d * (close - v) <= 0 or d * (close - float(pbar["high"] if d == 1 else pbar["low"])) <= 0:
                self.skips["no_continuation"] += 1
                continue
            if p["rejection"] == "pattern" and not self._pattern(today, pivot_ts, d):
                self.skips["no_rejection"] += 1
                continue
            if not self._volume_ok(bars):
                self.skips["low_volume"] += 1
                continue
            self.enter(ctx, d, close, pivot, a, extra={"pivot": pivot, "vwap": v})

    def _pattern(self, today: pd.DataFrame, pivot_ts, d: int) -> bool:
        i = today.index.get_loc(pivot_ts)
        if i < 1:
            return False
        bar, prev = today.iloc[i], today.iloc[i - 1]
        side = "down" if d == 1 else "up"
        return is_pin_bar(bar, side, self.p["wick_ratio"]) or is_engulfing(prev, bar, side)
