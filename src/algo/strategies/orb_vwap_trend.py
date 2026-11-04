"""ORB + VWAP trend continuation.

Idea: on a trending day the opening range breaks in the direction of the trend, price
pulls back to support (OR edge / VWAP / EMA9) and then resumes. We don't chase the
breakout; we buy the first resumption after the pullback.

    Opening range   09:15 - 09:15+range_minutes (spot high/low)
    Day filter      OR width between or_min_atr and or_max_atr x daily ATR
    Bias (long)     15-min EMA20 rising, spot > previous close, 5-min close > session VWAP
    Breakout        5-min close beyond the OR in the bias direction, volume >= vol_mult x avg
                    (arms the setup; no entry yet)
    Pullback        within max_wait bars, a bar's low comes within touch_pts of OR high / VWAP / EMA9
    Entry           a 5-min close above the previous bar's high, still above VWAP and with bias intact
    Shorts          mirror image (buy PE)

Exits are on the INDEX:
    stop        pullback extreme -/+ sl_buffer_pts (skip if risk is outside min/max_risk_pts)
    breakeven   stop to entry once +breakeven_at_r R
    target      target_r x risk (None = trail only)
    trail       after +trail_at_r R, a 5-min close back through EMA9
    thesis      a 5-min close back through VWAP
    time        exit_time
plus a premium stop (option_sl_pct) as a safety net.

Sizing: if the context has a CapitalManager, lots come from current equity
(per-lot risk = min(index risk x delta_est, premium x option_sl_pct) x lot size).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta

import pandas as pd

from algo.core.context import Context
from algo.core.models import ExitReason
from algo.core.strategy import Strategy
from algo.indicators import ema, vwap
from algo.strategies.index_plan import IndexPlanMixin, hm as _hm

SESSION_OPEN = time(9, 15)


class OrbVwapTrend(IndexPlanMixin, Strategy):
    """ORB in the trend direction + VWAP filter; enter on the first pullback resumption; index SL/target."""

    name = "orb_vwap_trend"
    defaults = {
        "range_minutes": 15,
        "tf": "5min",
        "or_min_atr": 0.2,          # skip the day if OR width < this x daily ATR (chop)
        "or_max_atr": 0.9,          # ... or > this x daily ATR (move already spent)
        "atr_days": 14,
        "trend_ema": 20,            # 15-min EMA for the trend bias
        "trend_slope_bars": 2,      # EMA must be higher (lower) than this many 15-min bars ago
        "use_prev_close": True,     # require spot above (below) the previous close for longs (shorts)
        "break_buffer_pts": 0.0,
        "vol_mult": 1.2,            # breakout bar volume >= vol_mult x avg of the last vol_lookback bars
        "vol_lookback": 20,
        "ema_fast": 9,              # 5-min EMA used as pullback support and trail
        "touch_pts": 8.0,           # pullback counts when the low gets within this of a support level
        "fail_pts": 15.0,           # setup fails on a close this far back inside the OR
        "max_wait": 6,              # bars after the breakout to wait for pullback + resumption
        "sl_buffer_pts": 5.0,
        "min_risk_pts": 8.0,
        "max_risk_pts": 35.0,
        "breakeven_at_r": 1.0,      # None = off
        "target_r": 2.0,            # None = trail only
        "trail_at_r": 1.5,          # None = off
        "vwap_exit": True,
        "option_sl_pct": 35.0,      # premium safety-net stop
        "moneyness": "ITM1",
        "delta_est": 0.6,           # for sizing: option points ~= index points x delta
        "expiry": "nearest",
        "min_dte": 0,
        "lots": 1,                  # fixed lots when size_from_capital is off (or no CapitalManager)
        "size_from_capital": True,  # False: always trade `lots` (for measuring the per-lot edge)
        "max_trades_per_day": 2,
        "entry_start": "09:35",
        "entry_end": "13:30",
        "exit_time": "15:00",
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.records: list[dict] = []
        self.skips: Counter = Counter()
        self._vol_noted: set = set()

    # ------------------------------------------------------------------ day
    def on_day_start(self, ctx: Context) -> None:
        ctx.state.update(orng=None, skip_day=False, setups={}, done=set(), plans={}, last_bar=None,
                         prev_close=None, datr=None)
        hist = ctx.spot(lookback=(self.p["atr_days"] + 2) * 380)
        hist = hist[hist.index.normalize() < pd.Timestamp(ctx.today)]
        if hist.empty:
            return
        daily = hist.groupby(hist.index.normalize()).agg(high=("high", "max"), low=("low", "min"),
                                                         close=("close", "last"))
        pc = daily["close"].shift()
        tr = pd.concat([daily["high"] - daily["low"], (daily["high"] - pc).abs(), (daily["low"] - pc).abs()],
                       axis=1).max(axis=1)
        ctx.state["prev_close"] = float(daily["close"].iloc[-1])
        ctx.state["datr"] = float(tr.tail(self.p["atr_days"]).mean())

    # ------------------------------------------------------------------ bar
    def on_bar(self, ctx: Context) -> None:
        p, st = self.p, ctx.state
        self._manage_minute_exits(ctx)
        self._update_opening_range(ctx)
        if st["orng"] is None:
            return

        bars = ctx.spot(p["tf"], lookback=150)
        if len(bars) < 2 or bars.index[-1] == st["last_bar"] or bars.index[-1].date() != ctx.today:
            return
        st["last_bar"] = bars.index[-1]
        today = bars[bars.index.normalize() == pd.Timestamp(ctx.today)]
        vw = float(vwap(today).iloc[-1])
        e9 = float(ema(bars["close"], p["ema_fast"]).iloc[-1])
        bar, prev = today.iloc[-1], bars.iloc[-2]
        close = float(bar["close"])

        self._manage_bar_exits(ctx, close, vw, e9)
        if st["skip_day"]:
            return

        trend = self._trend(ctx)
        for side in ("up", "down"):
            d = 1 if side == "up" else -1
            bias = (trend == d and d * (close - vw) > 0
                    and (not p["use_prev_close"] or st["prev_close"] is None or d * (close - st["prev_close"]) > 0))
            s = st["setups"].get(side)
            if s is None:
                if side in st["done"] or not bias:
                    continue
                level = st["orng"][0] + p["break_buffer_pts"] if d == 1 else st["orng"][1] - p["break_buffer_pts"]
                if d * (close - level) <= 0:
                    continue
                ok, ratio = self._volume_ok(bars, bar)
                if not ok:
                    if (ctx.today, side) not in self._vol_noted:   # count once per side per day
                        self._vol_noted.add((ctx.today, side))
                        self.skips["low_volume"] += 1
                    continue
                st["setups"][side] = {"bars": 0, "touched": False, "extreme": None, "vol_ratio": ratio,
                                      "or_edge": st["orng"][0] if d == 1 else st["orng"][1],
                                      "armed_at": bars.index[-1]}
                continue

            s["bars"] += 1
            hi, lo = float(bar["high"]), float(bar["low"])
            pull = lo if d == 1 else hi
            s["extreme"] = pull if s["extreme"] is None else (min(s["extreme"], pull) if d == 1 else max(s["extreme"], pull))
            if d * (close - vw) < 0 or d * (close - s["or_edge"]) < -p["fail_pts"]:
                self._drop(st, side, "setup_failed")
                continue
            supports = (s["or_edge"], vw, e9)
            if any(d * (pull - lvl) <= p["touch_pts"] for lvl in supports):
                s["touched"] = True
            resumed = d * (close - float(prev["high"] if d == 1 else prev["low"])) > 0
            if s["touched"] and resumed and bias:
                self._drop(st, side, None)
                self._enter(ctx, side, s, close, vw)
            elif s["bars"] >= p["max_wait"]:
                self._drop(st, side, "no_pullback_entry")

    # ------------------------------------------------------------ helpers
    def _drop(self, st: dict, side: str, why: str | None) -> None:
        st["setups"].pop(side, None)
        st["done"].add(side)
        if why:
            self.skips[why] += 1

    def _update_opening_range(self, ctx: Context) -> None:
        p, st = self.p, ctx.state
        if st["orng"] is not None:
            return
        or_end = datetime.combine(ctx.today, SESSION_OPEN) + timedelta(minutes=p["range_minutes"])
        if ctx.bar_close_time < or_end:
            return
        first = ctx.spot(lookback=400, today_only=True)
        first = first[first.index < or_end]
        if first.empty:
            return
        hi, lo = float(first["high"].max()), float(first["low"].min())
        st["orng"] = (hi, lo)
        datr = st["datr"]
        if datr:
            w = (hi - lo) / datr
            st["or_width_atr"] = w
            if not (p["or_min_atr"] <= w <= p["or_max_atr"]):
                st["skip_day"] = True
                self.skips["or_width"] += 1

    def _trend(self, ctx: Context) -> int:
        p = self.p
        b = ctx.spot("15min", lookback=p["trend_ema"] * 4)
        if len(b) <= p["trend_slope_bars"]:
            return 0
        e = ema(b["close"], p["trend_ema"])
        diff = float(e.iloc[-1] - e.iloc[-1 - p["trend_slope_bars"]])
        return 1 if diff > 0 else -1 if diff < 0 else 0

    def _volume_ok(self, bars: pd.DataFrame, bar) -> tuple[bool, float]:
        p = self.p
        if self.p["vol_mult"] <= 0 or "volume" not in bars:
            return True, float("nan")
        avg = float(bars["volume"].iloc[-p["vol_lookback"] - 1:-1].mean())
        if not avg or avg <= 0:
            return True, float("nan")   # no volume data (index without futures): filter off
        ratio = float(bar["volume"]) / avg
        return ratio >= p["vol_mult"], ratio

    def _enter(self, ctx: Context, side: str, s: dict, entry: float, vw: float) -> None:
        p, st = self.p, ctx.state
        t = ctx.now.time()
        if (ctx.active_positions or ctx.broker.pending_orders()
                or not (_hm(p["entry_start"]) <= t < _hm(p["entry_end"]))
                or ctx.trades_today() >= p["max_trades_per_day"]):
            self.skips["busy_or_window"] += 1
            return
        d = 1 if side == "up" else -1
        stop = s["extreme"] - d * p["sl_buffer_pts"]
        risk = d * (entry - stop)
        if not (p["min_risk_pts"] <= risk <= p["max_risk_pts"]):
            self.skips["risk_pts"] += 1
            return
        self._buy_with_plan(ctx, d, entry, stop, f"orbvt_{side}", extra={
            "side": side, "vwap": vw, "or_high": st["orng"][0], "or_low": st["orng"][1],
            "or_width_atr": st.get("or_width_atr"), "vol_ratio": s["vol_ratio"]})

    # ------------------------------------------------------------- exits
    def _manage_bar_exits(self, ctx: Context, close: float, vw: float, e9: float) -> None:
        p = self.p
        for pos, plan in self._plans(ctx):
            d = plan["dir"]
            best_r = d * (plan["best"] - plan["entry"]) / plan["risk"]
            if p["vwap_exit"] and d * (close - vw) < 0:
                ctx.exit(pos, ExitReason.SIGNAL)
                plan["why"] = "vwap"
            elif p["trail_at_r"] and best_r >= p["trail_at_r"] and d * (close - e9) < 0:
                ctx.exit(pos, ExitReason.SIGNAL)
                plan["why"] = "trail"
