"""Volatility compression (squeeze) breakout.

    Squeeze      Bollinger(bb_period, bb_k) width at or below its `bw_pct` percentile over the last
                 `bw_lookback` bars. Consecutive squeeze bars today build a box (high / low).
    Breakout     once the box has >= min_squeeze_bars bars: a close beyond the box (long: above the
                 high) on a bar whose range is >= expand_atr x ATR and that closes in the breakout
                 direction. The box stays valid for `max_wait` bars after the squeeze ends.
    Stop         the opposite side of the box (stop_at=box) or the box middle (stop_at=mid)
"""
from __future__ import annotations

import pandas as pd

from algo.core.context import Context
from algo.indicators import bollinger
from algo.strategies.bar_signal import COMMON, BarSignalStrategy


class SqueezeBreakout(BarSignalStrategy):
    """Low-volatility box (Bollinger width percentile), then a range-expansion breakout."""

    name = "squeeze_breakout"
    tag_prefix = "sqz"
    lookback = 260
    choices = {"stop_at": ("box", "mid")}
    defaults = {
        **COMMON,
        "bb_period": 20,
        "bb_k": 2.0,
        "bw_lookback": 150,     # bars (~2 sessions of 5-min) for the width percentile
        "bw_pct": 20.0,         # squeeze = width at or below this percentile
        "min_squeeze_bars": 6,
        "expand_atr": 1.0,      # breakout bar range >= this x ATR
        "max_wait": 3,          # bars after the squeeze ends that a breakout still counts
        "stop_at": "box",
        "max_risk_atr": 4.0,
    }

    def day_start(self, ctx: Context) -> None:
        ctx.state["box"] = None

    def signal(self, ctx: Context, bars: pd.DataFrame, today: pd.DataFrame) -> None:
        p, st = self.p, ctx.state
        w = bollinger(bars["close"], p["bb_period"], p["bb_k"])["width"]
        hist = w.iloc[-p["bw_lookback"]:].dropna()
        if len(hist) < p["bw_lookback"] // 2:
            return
        in_sq = float(w.iloc[-1]) <= float(hist.quantile(p["bw_pct"] / 100))
        bar = bars.iloc[-1]
        hi, lo, o, c = (float(bar[k]) for k in ("high", "low", "open", "close"))
        a = self.bar_atr(bars)
        box = st["box"]

        if box is not None and box["n"] >= p["min_squeeze_bars"]:
            d = 1 if c > box["hi"] else -1 if c < box["lo"] else 0
            if d and d * (c - o) > 0 and (hi - lo) >= p["expand_atr"] * a:
                level = (box["lo"] if d == 1 else box["hi"]) if p["stop_at"] == "box" else (box["hi"] + box["lo"]) / 2
                self.enter(ctx, d, c, level, a, extra={"box_hi": box["hi"], "box_lo": box["lo"], "box_n": box["n"]})
                st["box"] = None
                return
            if d:
                self.skips["weak_breakout"] += 1
        if in_sq:
            if box is None or box["age"] > 0:     # a new squeeze after the last one ended starts a new box
                box = st["box"] = {"hi": hi, "lo": lo, "n": 0, "age": 0}
            box.update(hi=max(box["hi"], hi), lo=min(box["lo"], lo), n=box["n"] + 1)
        elif box is not None:
            box["age"] += 1
            if box["age"] > p["max_wait"] or box["n"] < p["min_squeeze_bars"]:
                st["box"] = None
