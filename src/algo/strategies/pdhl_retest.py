"""Previous-day high / low breakout, retest and continuation.

    Breakout (long)  a close above the previous session's high (PDH) by >= break_atr x ATR, coming from
                     inside (previous close <= PDH; a gap-up open counts)
    Retest           within `max_wait` bars: the low comes back within touch_atr x ATR of PDH (and not
                     more than pierce_atr x ATR below it) and the bar closes above PDH
    Confirmation     within `confirm_bars`: a green bar closing above the previous high; with
                     confirm_mode=any a bullish pin bar / engulfing retest bar confirms immediately
    Stop             below the retest low
A close back below PDH by more than pierce_atr x ATR kills the setup. Each level trades once per day.
Shorts use the previous day low (PDL), mirrored.
"""
from __future__ import annotations

import pandas as pd

from algo.core.context import Context
from algo.indicators import is_engulfing, is_pin_bar
from algo.strategies.bar_signal import COMMON, BarSignalStrategy


class PdhlRetest(BarSignalStrategy):
    """Break of the previous day's high/low, successful retest, continuation entry."""

    name = "pdhl_retest"
    tag_prefix = "pdr"
    choices = {"confirm_mode": ("break_high", "any")}
    defaults = {
        **COMMON,
        "break_atr": 0.0,       # breakout close must clear the level by this many ATRs
        "max_wait": 12,         # bars from the breakout to the retest
        "touch_atr": 0.3,
        "pierce_atr": 0.5,
        "confirm_bars": 3,
        "confirm_mode": "any",
        "wick_ratio": 0.5,
        "entry_start": "09:30",
    }

    def day_start(self, ctx: Context) -> None:
        ctx.state.update(watch={1: None, -1: None}, done=set())

    def signal(self, ctx: Context, bars: pd.DataFrame, today: pd.DataFrame) -> None:
        p, st = self.p, ctx.state
        if st["prev_high"] is None:
            return
        a = self.bar_atr(bars)
        bar, prev = bars.iloc[-1], bars.iloc[-2]
        close = float(bar["close"])
        for d, level in ((1, st["prev_high"]), (-1, st["prev_low"])):
            if d in st["done"]:
                continue
            w = st["watch"][d]
            if w is None:
                if d * (close - level) >= p["break_atr"] * a and d * (float(prev["close"]) - level) <= 0:
                    st["watch"][d] = {"stage": "broken", "bars": 0, "extreme": None}
                continue
            w["bars"] += 1
            if d * (close - level) < -p["pierce_atr"] * a:
                self._finish(st, d, "failed_back_inside")
                continue
            pull = float(bar["low"]) if d == 1 else float(bar["high"])
            if w["stage"] == "broken":
                touched = -p["pierce_atr"] * a <= d * (pull - level) <= p["touch_atr"] * a
                if touched and d * (close - level) > 0:
                    w.update(stage="retest", bars=0, extreme=pull)
                    if p["confirm_mode"] == "any" and self._pattern(bar, prev, d):
                        self._go(ctx, d, close, w, level, a)
                elif w["bars"] >= p["max_wait"]:
                    self._finish(st, d, "no_retest")
                continue
            w["extreme"] = min(w["extreme"], pull) if d == 1 else max(w["extreme"], pull)
            green = close > bar["open"] if d == 1 else close < bar["open"]
            if green and d * (close - float(prev["high"] if d == 1 else prev["low"])) > 0:
                self._go(ctx, d, close, w, level, a)
            elif w["bars"] >= p["confirm_bars"]:
                self._finish(st, d, "no_confirmation")

    def _go(self, ctx: Context, d: int, close: float, w: dict, level: float, a: float) -> None:
        self.enter(ctx, d, close, w["extreme"], a, extra={"level": level, "retest": w["extreme"]})
        self._finish(ctx.state, d, None)

    def _finish(self, st: dict, d: int, why: str | None) -> None:
        st["watch"][d] = None
        st["done"].add(d)
        if why:
            self.skips[why] += 1

    def _pattern(self, bar, prev, d: int) -> bool:
        side = "down" if d == 1 else "up"
        pin = is_pin_bar(bar, side, self.p["wick_ratio"]) and (bar["close"] > bar["open"] if d == 1
                                                              else bar["close"] < bar["open"])
        return pin or is_engulfing(prev, bar, side)
