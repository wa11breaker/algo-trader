"""Trend + first pullback (continuation).

    Impulse (long)   since the anchor (day start, or the last used/failed pullback), the move from the
                     lowest low to the highest high is >= impulse_atr x ATR, the high is at most
                     `fresh_bars` old, close > EMA(trend_ema), EMA(pb_ema) > EMA(trend_ema) (when they
                     differ) and optionally ADX >= adx_min
    First pullback   the low comes within touch_atr x ATR of EMA(pb_ema) while closes stay above
                     EMA(trend_ema); within `max_wait` bars of the impulse
    Confirmation     within `confirm_bars` after the touch: a green bar closing above the previous high
    Entry / stop     buy CE at the confirmation close; stop below the pullback low (the lowest low since
                     the impulse high)
After an entry or a failed setup the anchor moves to that bar, so the next trade needs a fresh impulse.
Shorts are the mirror image.
"""
from __future__ import annotations

import pandas as pd

from algo.core.context import Context
from algo.indicators import adx, ema
from algo.strategies.bar_signal import COMMON, BarSignalStrategy


class TrendPullback(BarSignalStrategy):
    """Strong directional impulse, then the FIRST pullback to the EMA and a continuation bar."""

    name = "trend_pullback"
    tag_prefix = "tpb"
    defaults = {
        **COMMON,
        "impulse_atr": 3.0,     # impulse size in tf ATRs
        "fresh_bars": 3,        # the impulse extreme must be this recent when the setup arms
        "trend_ema": 20,
        "pb_ema": 9,            # pullback level (9 or 20)
        "touch_atr": 0.3,       # touch = within this many ATRs of the pullback EMA (or through it)
        "adx_min": 0.0,         # 0 = off
        "max_wait": 8,          # bars from the impulse to the pullback touch
        "confirm_bars": 3,      # bars from the touch to the confirmation
    }

    def day_start(self, ctx: Context) -> None:
        ctx.state.update(setup={1: None, -1: None}, anchor={1: None, -1: None})

    def signal(self, ctx: Context, bars: pd.DataFrame, today: pd.DataFrame) -> None:
        p, st = self.p, ctx.state
        if len(today) < 2:
            return
        close_s = bars["close"]
        e_trend = float(ema(close_s, p["trend_ema"]).iloc[-1])
        e_pb = float(ema(close_s, p["pb_ema"]).iloc[-1])
        a = self.bar_atr(bars)
        bar, prev = bars.iloc[-1], bars.iloc[-2]
        close = float(bar["close"])
        strength = float(adx(bars, 14).iloc[-1]) if p["adx_min"] else None
        now = bars.index[-1]

        for d in (1, -1):
            s = st["setup"][d]
            if s is None:
                if self._impulse(today, st["anchor"][d], d, a, close, e_trend, e_pb, strength):
                    st["setup"][d] = {"bars": 0, "touched": False, "pb": None, "wait": 0, "low": None,
                                       "ext": float(bar["high"] if d == 1 else bar["low"])}
                continue
            s["bars"] += 1
            if d * (close - e_trend) < 0:
                self._reset(st, d, now, "too_deep")
                continue
            pull = float(bar["low"]) if d == 1 else float(bar["high"])
            push = float(bar["high"]) if d == 1 else float(bar["low"])
            touch_now = d * (pull - e_pb) <= p["touch_atr"] * a
            if not s["touched"] and not touch_now and d * (push - s["ext"]) > 0:
                s.update(ext=push, low=None, bars=0)   # impulse still extending: no pullback yet
                continue
            # the pullback extreme: lowest low (highest high) since the impulse extreme
            s["low"] = pull if s["low"] is None else (min(s["low"], pull) if d == 1 else max(s["low"], pull))
            if not s["touched"]:
                if not touch_now:
                    if s["bars"] >= p["max_wait"]:
                        self._reset(st, d, now, "no_pullback")
                    continue
                s["touched"] = True                   # the touch bar itself may also confirm
            else:
                s["wait"] += 1
            s["pb"] = s["low"]
            green = close > bar["open"] if d == 1 else close < bar["open"]
            if green and d * (close - float(prev["high"] if d == 1 else prev["low"])) > 0:
                self.enter(ctx, d, close, s["pb"], a, extra={"pullback": s["pb"], "ema_pb": e_pb})
                self._reset(st, d, now, None)
            elif s["wait"] >= p["confirm_bars"]:
                self._reset(st, d, now, "no_confirmation")

    def _impulse(self, today: pd.DataFrame, anchor, d: int, a: float, close: float, e_trend: float,
                 e_pb: float, strength: float | None) -> bool:
        p = self.p
        seg = today if anchor is None else today[today.index >= anchor]
        if len(seg) < 2 or a <= 0:
            return False
        if d == 1:
            ext_ts = seg["high"].idxmax()
            move = float(seg["high"].max()) - float(seg.loc[:ext_ts, "low"].min())
        else:
            ext_ts = seg["low"].idxmin()
            move = float(seg.loc[:ext_ts, "high"].max()) - float(seg["low"].min())
        fresh = len(seg.loc[ext_ts:]) <= p["fresh_bars"]
        trend_ok = d * (close - e_trend) > 0 and (p["pb_ema"] == p["trend_ema"] or d * (e_pb - e_trend) > 0)
        return (move >= p["impulse_atr"] * a and fresh and trend_ok
                and (strength is None or strength >= p["adx_min"]))

    def _reset(self, st: dict, d: int, now, why: str | None) -> None:
        st["setup"][d] = None
        st["anchor"][d] = now
        if why:
            self.skips[why] += 1
