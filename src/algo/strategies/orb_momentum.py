"""Opening range breakout with momentum confirmation (enters on the breakout bar).

    Opening range  09:15 + range_minutes. The day is skipped if the OR width is outside
                   or_min_atr .. or_max_atr x the daily ATR.
    Breakout       a close above the OR high (long), coming from inside (previous close <= OR high)
    Momentum       the breakout bar's body >= body_atr x ATR, RSI(rsi_period) >= rsi_min (<= 100 - rsi_min
                   for shorts), close on the trade side of VWAP; optional volume >= vol_mult x the
                   20-bar average (futures volume, Oct 2024+)
    Stop           OR middle (stop_at=mid) or the opposite OR edge (stop_at=range)
Each side trades at most once per day; a breakout that fails the momentum filters doesn't use it up.
"""
from __future__ import annotations

import pandas as pd

from algo.core.context import Context
from algo.indicators import rsi, vwap
from algo.strategies.bar_signal import COMMON, BarSignalStrategy


class OrbMomentum(BarSignalStrategy):
    """Opening range breakout confirmed by a strong bar, RSI, VWAP side and (optional) volume."""

    name = "orb_momentum"
    tag_prefix = "orm"
    choices = {"stop_at": ("mid", "range")}
    defaults = {
        **COMMON,
        "range_minutes": 15,
        "or_min_atr": 0.15,
        "or_max_atr": 0.8,
        "body_atr": 0.8,
        "rsi_period": 14,
        "rsi_min": 60.0,
        "vol_mult": 0.0,
        "vol_lookback": 20,
        "stop_at": "mid",
        "max_risk_atr": 6.0,
        "entry_start": "09:30",
        "entry_end": "12:00",
    }

    def day_start(self, ctx: Context) -> None:
        ctx.state.update(orng=None, skip_day=False, done=set())

    def signal(self, ctx: Context, bars: pd.DataFrame, today: pd.DataFrame) -> None:
        p, st = self.p, ctx.state
        if st["skip_day"]:
            return
        or_end = pd.Timestamp(ctx.today) + pd.Timedelta(hours=9, minutes=15 + p["range_minutes"])
        if st["orng"] is None:
            orb = today[today.index < or_end]
            if len(today) == len(orb):          # range not complete yet
                return
            hi, lo = float(orb["high"].max()), float(orb["low"].min())
            datr = st["datr"]
            if datr and not (p["or_min_atr"] * datr <= hi - lo <= p["or_max_atr"] * datr):
                st["skip_day"] = True
                self.skips["or_width"] += 1
                return
            st["orng"] = (hi, lo)
        hi, lo = st["orng"]
        bar, prev = bars.iloc[-1], bars.iloc[-2]
        o, c = float(bar["open"]), float(bar["close"])
        for d, edge in ((1, hi), (-1, lo)):
            if d in st["done"] or d * (c - edge) <= 0 or d * (float(prev["close"]) - edge) > 0:
                continue
            a = self.bar_atr(bars)
            r = float(rsi(bars["close"], p["rsi_period"]).iloc[-1])
            v = float(vwap(today).iloc[-1])
            if d * (c - o) < p["body_atr"] * a:
                self.skips["weak_body"] += 1
                continue
            if (r < p["rsi_min"]) if d == 1 else (r > 100 - p["rsi_min"]):
                self.skips["rsi"] += 1
                continue
            if d * (c - v) <= 0:
                self.skips["vwap_side"] += 1
                continue
            if not self._volume_ok(bars):
                self.skips["low_volume"] += 1
                continue
            level = (hi + lo) / 2 if p["stop_at"] == "mid" else (lo if d == 1 else hi)
            st["done"].add(d)
            self.enter(ctx, d, c, level, a, extra={"or_hi": hi, "or_lo": lo, "rsi": r})
