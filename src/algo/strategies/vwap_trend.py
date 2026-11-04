"""VWAP trend following with VWAP-retest entries.

    Trend (long)     the last `trend_bars` 5-min closes are above session VWAP, VWAP is rising
                     over `vwap_slope_bars`, and (optional) the 15-min EMA20 is rising
    Retest           a bar's low comes within `touch_pts` of VWAP (may pierce it by up to
                     `pierce_pts`) but the bar closes above VWAP
    Confirmation     within `confirm_bars` after the retest: a green bar closing above the
                     previous bar's high (and above VWAP); with confirm_mode=any a bullish
                     pin bar / engulfing retest bar also confirms
    Entry            buy CE (PE for the mirror image) at the confirmation close
    Exits            index stop = retest extreme -/+ sl_buffer_pts
                     VWAP breakdown: a 5-min close back through VWAP by breakdown_pts
                     trailing stop: once +trail_start_r R, stop trails trail_atr_mult x ATR(5-min)
                     behind the best price; breakeven at +breakeven_at_r R
                     optional fixed target (target_r); time exit; premium safety-net stop
VWAP (vwap_source):
    spot     spot prices weighted by front-month futures volume (`ctx.spot` merges it)
    futures  the real futures VWAP. It is converted into spot terms by subtracting the current
             basis (futures close - spot close), so `spot - vwap` == `futures - futures VWAP` at each
             close. Candles, stops and strikes stay on spot; the VWAP slope uses the raw futures VWAP.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from algo.core.context import Context
from algo.core.models import ExitReason
from algo.core.strategy import Strategy
from algo.indicators import atr, ema, is_engulfing, is_pin_bar, vwap
from algo.strategies.index_plan import IndexPlanMixin, hm


class VwapTrend(IndexPlanMixin, Strategy):
    """VWAP trend following: retest of VWAP + bullish/bearish confirmation; exit on VWAP break or trail."""

    name = "vwap_trend"
    defaults = {
        "tf": "5min",
        "vwap_source": "spot",      # spot | futures (see module docstring)
        "trend_bars": 3,            # closes on the trend side of VWAP before the retest
        "vwap_slope_bars": 3,       # VWAP must be higher (lower) than this many bars ago
        "use_htf_trend": True,      # also require the 15-min EMA to slope the same way
        "htf_ema": 20,
        "htf_slope_bars": 2,
        "touch_pts": 8.0,           # retest: low within this of VWAP ...
        "pierce_pts": 10.0,         # ... and not more than this below it
        "confirm_bars": 2,          # bars after the retest to wait for confirmation
        "confirm_mode": "any",      # break_high | any (break_high or pin bar / engulfing retest)
        "wick_ratio": 0.5,
        "sl_buffer_pts": 5.0,
        "min_risk_pts": 8.0,
        "max_risk_pts": 40.0,
        "breakdown_pts": 0.0,       # VWAP exit: close this far through VWAP against the trade
        "atr_period": 14,
        "trail_atr_mult": 1.5,
        "trail_start_r": 1.0,       # None = no trailing stop
        "breakeven_at_r": 1.0,
        "target_r": None,           # trend following: no fixed target by default
        "option_sl_pct": 35.0,
        "moneyness": "ITM1",
        "delta_est": 0.6,
        "expiry": "nearest",
        "min_dte": 0,
        "lots": 1,
        "size_from_capital": True,
        "max_trades_per_day": 2,
        "entry_start": "09:45",
        "entry_end": "14:00",
        "exit_time": "15:00",
    }

    def __init__(self, **params):
        super().__init__(**params)
        if self.p["confirm_mode"] not in ("break_high", "any"):
            raise ValueError("confirm_mode must be break_high | any")
        if self.p["vwap_source"] not in ("spot", "futures"):
            raise ValueError("vwap_source must be spot | futures")
        self.skips: Counter = Counter()
        self.records: list[dict] = []

    def on_day_start(self, ctx: Context) -> None:
        ctx.state.update(plans={}, setup=None, last_bar=None)

    # ------------------------------------------------------------------ bar
    def on_bar(self, ctx: Context) -> None:
        p, st = self.p, ctx.state
        self._manage_minute_exits(ctx)
        bars = ctx.spot(p["tf"], lookback=150)
        if len(bars) < 3 or bars.index[-1] == st["last_bar"] or bars.index[-1].date() != ctx.today:
            return
        st["last_bar"] = bars.index[-1]
        today = bars[bars.index.normalize() == pd.Timestamp(ctx.today)]
        if len(today) < p["trend_bars"] + 1:
            return
        vw, vw_slope = self._vwap(ctx, today)
        if vw is None:
            return
        bar, prev = today.iloc[-1], bars.iloc[-2]
        close, v = float(bar["close"]), float(vw.iloc[-1])

        self._vwap_exit(ctx, close, v)
        if ctx.active_positions or ctx.broker.pending_orders():
            return

        s = st["setup"]
        if s is not None:
            d = s["dir"]
            s["bars"] += 1
            s["extreme"] = min(s["extreme"], float(bar["low"])) if d == 1 else max(s["extreme"], float(bar["high"]))
            if d * (close - v) <= 0:
                self._drop(st, "lost_vwap")
            elif self._break_confirms(bar, prev, d):
                self._drop(st, None)
                self._enter(ctx, d, close, s["extreme"], bars, v)
            elif s["bars"] >= p["confirm_bars"]:
                self._drop(st, "no_confirmation")
            return

        d = self._trend(ctx, today, vw, vw_slope)
        if d == 0:
            return
        lo, hi = float(bar["low"]), float(bar["high"])
        if d == 1:
            touched = lo <= v + p["touch_pts"] and lo >= v - p["pierce_pts"] and close > v
        else:
            touched = hi >= v - p["touch_pts"] and hi <= v + p["pierce_pts"] and close < v
        if not touched:
            return
        extreme = lo if d == 1 else hi
        if p["confirm_mode"] == "any" and self._pattern_confirms(bar, prev, d):
            self._enter(ctx, d, close, extreme, bars, v)
            return
        st["setup"] = {"dir": d, "extreme": extreme, "bars": 0}

    # ------------------------------------------------------------ helpers
    def _drop(self, st: dict, why: str | None) -> None:
        st["setup"] = None
        if why:
            self.skips[why] += 1

    def _vwap(self, ctx: Context, today: pd.DataFrame) -> tuple[pd.Series | None, pd.Series | None]:
        """(VWAP in spot terms, series for the VWAP slope). (None, None) if futures data is missing."""
        if self.p["vwap_source"] == "spot":
            vw = vwap(today)
            return vw, vw
        try:
            fut = ctx.futures(self.p["tf"], lookback=150, today_only=True)
        except RuntimeError:
            fut = None
        if fut is None or fut.empty:
            self.skips["no_futures"] += 1
            return None, None
        fv = vwap(fut).reindex(today.index)
        basis = fut["close"].reindex(today.index) - today["close"]
        adj = fv - basis
        if pd.isna(adj.iloc[-1]):
            self.skips["no_futures"] += 1
            return None, None
        return adj, fv

    def _trend(self, ctx: Context, today: pd.DataFrame, vw: pd.Series, vw_slope: pd.Series) -> int:
        """+1 / -1 if the closes BEFORE the current bar are all on one side of VWAP and VWAP slopes that way."""
        p = self.p
        n, k = p["trend_bars"], p["vwap_slope_bars"]
        side = (today["close"] - vw).iloc[-n - 1:-1]
        if len(side) < n or len(vw_slope) <= k or side.isna().any():
            return 0
        d = 1 if (side > 0).all() else -1 if (side < 0).all() else 0
        if d == 0 or d * float(vw_slope.iloc[-1] - vw_slope.iloc[-1 - k]) <= 0:
            return 0
        if p["use_htf_trend"]:
            b = ctx.spot("15min", lookback=p["htf_ema"] * 4)
            if len(b) <= p["htf_slope_bars"]:
                return 0
            e = ema(b["close"], p["htf_ema"])
            if d * float(e.iloc[-1] - e.iloc[-1 - p["htf_slope_bars"]]) <= 0:
                return 0
        return d

    @staticmethod
    def _break_confirms(bar, prev, d: int) -> bool:
        if d == 1:
            return bar["close"] > bar["open"] and bar["close"] > prev["high"]
        return bar["close"] < bar["open"] and bar["close"] < prev["low"]

    def _pattern_confirms(self, bar, prev, d: int) -> bool:
        # a bullish rejection of VWAP = long LOWER wick: is_pin_bar(.., "down") in the indicator's terms
        side = "down" if d == 1 else "up"
        pin = is_pin_bar(bar, side, self.p["wick_ratio"]) and (bar["close"] > bar["open"] if d == 1
                                                              else bar["close"] < bar["open"])
        return pin or is_engulfing(prev, bar, side)

    def _enter(self, ctx: Context, d: int, entry: float, extreme: float, bars: pd.DataFrame, v: float) -> None:
        p = self.p
        t = ctx.now.time()
        if not (hm(p["entry_start"]) <= t < hm(p["entry_end"])) or ctx.trades_today() >= p["max_trades_per_day"]:
            self.skips["window_or_max_trades"] += 1
            return
        stop = extreme - d * p["sl_buffer_pts"]
        risk = d * (entry - stop)
        if not (p["min_risk_pts"] <= risk <= p["max_risk_pts"]):
            self.skips["risk_pts"] += 1
            return
        a = float(atr(bars, p["atr_period"]).iloc[-1])
        side = "up" if d == 1 else "down"
        self._buy_with_plan(ctx, d, entry, stop, f"vwt_{side}", trail_dist=p["trail_atr_mult"] * a,
                            extra={"side": side, "vwap": v, "atr": a})

    def _vwap_exit(self, ctx: Context, close: float, v: float) -> None:
        for pos, plan in self._plans(ctx):
            if plan["dir"] * (close - v) < -self.p["breakdown_pts"]:
                ctx.exit(pos, ExitReason.SIGNAL)
                plan["why"] = "vwap"
