"""Fair value gap retest on 1-min candles, confirmed by 1-min order flow.

  FVG     3-bar gap on 1-min spot: bullish when bars[-3].high < bars[-1].low (gap = that range),
          bearish when bars[-3].low > bars[-1].high. Gaps < min_gap_pts are ignored.
  Retest  a later bar trades back into the gap and closes out of it on the gap's side
          (bullish: low <= upper and close > upper -> buy CE; bearish mirror -> buy PE).
          A close through the far edge invalidates the gap.
  Flow    the summed delta of the last `flow_bars` 1-min futures bars must be on the trade's side
          (and >= min_delta; optionally today's cumulative delta too). Backtests have no order
          flow: there the confirmation is skipped and counted as skips["no_flow_data"].
  Plan    stop = far edge of the gap -/+ sl_buffer_pts, target = target_r x risk (on the index).
  Option  starts at `moneyness` (OTM1) and steps further OTM while the premium > max_premium.

Research switches (defaults = the behaviour above; ATR = ATR14 on 1-min spot):
  min_gap_atr / disp_atr    gap >= k x ATR / impulse (middle) bar body >= k x ATR
  retest_depth              "edge" (touch the near edge) | "mid" (touch the gap's 50% level)
  trend                     "none" | "vwap" (trade on the VWAP side) | "ema" (EMA-50 slope over 5 bars)
  sl_mode                   "gap" (far edge -/+ sl_buffer_pts) | "atr" (entry -/+ sl_atr x ATR)
                            | "gap_atr" (far edge -/+ sl_atr x ATR)
  tp_mode                   "r" (target_r x risk) | "atr" (tp_atr x ATR) | "liquidity" (nearest
                            confirmed swing / today's high-low beyond tp_min_r, capped at tp_max_r)
  trail_atr                 trailing stop distance = k x ATR, active from trail_start_r
  flow_proxy                backtests: close-location delta from futures candles,
                            volume x (2c - h - l) / (h - l), instead of skipping the flow check
  min_delta_ratio / delta_z |summed delta| / volume >= x / last bar's delta z-score (20 bars) >= z
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from algo.core.context import Context
from algo.core.strategy import Strategy
from algo.indicators import atr, ema, swing_points, vwap
from algo.strategies.index_plan import IndexPlanMixin, hm

MAX_GAPS = 5


class FvgFlow(IndexPlanMixin, Strategy):
    """1-min FVG retest with order-flow (delta) confirmation; buys OTM options under a premium cap."""

    name = "fvg_flow"
    index_replay_exact = True
    risk_overrides = {"no_entry_before": "09:30", "no_entry_after": "15:00", "max_trades_per_day": 100,
                      "daily_loss_limit_pct": 100.0, "max_consecutive_losses": 10_000}   # no daily stop: trade all day
    defaults = {
        "min_gap_pts": 3.0,
        "max_age_min": 30,             # gaps older than this are dropped
        "sl_buffer_pts": 2.0,
        "min_risk_pts": None,
        "max_risk_pts": None,
        "target_r": 2.0,
        "breakeven_at_r": 1.0,
        "flow_bars": 1,                # 1-min futures bars whose delta is summed (the retest bar itself)
        "min_delta": 0.0,              # in contracts
        "require_cum_delta": False,
        "entry_start": "09:30",
        "entry_end": "15:00",
        "exit_time": "15:15",          # = the engine's square-off; entries run until 15:00
        "max_trades_per_day": 100,
        "moneyness": "OTM1",
        "max_premium": 150.0,          # Rs per unit; step further OTM while the premium is above this
        "max_otm_steps": 6,
        "delta_est": 0.4,
        "option_sl_pct": 40.0,
        "expiry": "nearest",
        "min_dte": 0,
        "lots": 1,
        "size_from_capital": True,
        # research switches (see the module docstring); defaults keep the original behaviour
        "min_gap_atr": None,
        "disp_atr": None,
        "retest_depth": "edge",
        "trend": "none",
        "sl_mode": "gap",
        "sl_atr": 1.0,
        "tp_mode": "r",
        "tp_atr": 2.0,
        "tp_min_r": 1.0,
        "tp_max_r": 4.0,
        "trail_atr": None,
        "trail_start_r": None,
        "flow_proxy": False,
        "min_delta_ratio": None,
        "delta_z": None,
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.skips: Counter = Counter()
        self.records: list[dict] = []

    def on_day_start(self, ctx: Context) -> None:
        ctx.state.update(plans={}, fvgs=[], last_bar=None)

    def on_bar(self, ctx: Context) -> None:
        p, st = self.p, ctx.state
        self._manage_minute_exits(ctx)
        bars = ctx.spot("1min", lookback=3, today_only=True)
        if bars.empty or bars.index[-1] == st["last_bar"]:
            return
        st["last_bar"] = ts = bars.index[-1]
        bar = bars.iloc[-1]
        st["fvgs"] = [g for g in st["fvgs"] if ts - g["formed"] <= pd.Timedelta(minutes=p["max_age_min"])]
        signal = self._retest(st, ts, bar, p["retest_depth"])
        if len(bars) == 3:
            self._track(st, bars, ctx)
        if signal is None:
            return
        d, gap = signal
        t = ts.time()
        if not (hm(p["entry_start"]) <= t < hm(p["entry_end"])):
            self.skips["outside_hours"] += 1
            return
        if ctx.active_positions or ctx.broker.pending_orders():
            self.skips["in_position"] += 1
            return
        if ctx.trades_today() >= p["max_trades_per_day"]:
            self.skips["max_trades"] += 1
            return
        if p["trend"] != "none" and not self._trend_ok(ctx, d, float(bar["close"])):
            self.skips["trend"] += 1
            return
        delta = self._flow_ok(ctx, d)
        if delta is False:
            return
        entry = float(bar["close"])
        a = self._atr(ctx) if p["sl_mode"] != "gap" or p["tp_mode"] == "atr" or p["trail_atr"] else None
        far = gap["lower"] if d == 1 else gap["upper"]
        stop = {"gap": far - d * p["sl_buffer_pts"], "atr": entry - d * p["sl_atr"] * (a or 0),
                "gap_atr": far - d * p["sl_atr"] * (a or 0)}[p["sl_mode"]]
        risk = d * (entry - stop)
        if risk <= 0:
            self.skips["risk_pts"] += 1
            return
        if (p["min_risk_pts"] is not None and risk < p["min_risk_pts"]) or \
                (p["max_risk_pts"] is not None and risk > p["max_risk_pts"]):
            self.skips["risk_pts"] += 1
            return
        target = None
        if p["tp_mode"] == "atr":
            target = entry + d * p["tp_atr"] * a
        elif p["tp_mode"] == "liquidity":
            target = self._liquidity_target(ctx, d, entry, risk)
        self._buy_with_plan(ctx, d, entry, stop, "fvg_up" if d == 1 else "fvg_dn",
                            extra={"fvg_lower": gap["lower"], "fvg_upper": gap["upper"], "delta": delta},
                            target=target, trail_dist=p["trail_atr"] * a if p["trail_atr"] else None)

    # ------------------------------------------------------------------ gaps
    def _track(self, st: dict, bars: pd.DataFrame, ctx: Context | None = None) -> None:
        p = self.p
        b1, b2, b3 = bars.iloc[-3], bars.iloc[-2], bars.iloc[-1]
        if b1["high"] < b3["low"]:
            g = {"formed": bars.index[-1], "lower": float(b1["high"]), "upper": float(b3["low"]), "bullish": True}
        elif b1["low"] > b3["high"]:
            g = {"formed": bars.index[-1], "lower": float(b3["high"]), "upper": float(b1["low"]), "bullish": False}
        else:
            return
        size = g["upper"] - g["lower"]
        if size < p["min_gap_pts"]:
            return
        if (p["min_gap_atr"] or p["disp_atr"]) and ctx is not None:
            a = self._atr(ctx)
            if (p["min_gap_atr"] and size < p["min_gap_atr"] * a) or \
                    (p["disp_atr"] and abs(float(b2["close"]) - float(b2["open"])) < p["disp_atr"] * a):
                return
        st["fvgs"] = (st["fvgs"] + [g])[-MAX_GAPS:]

    @staticmethod
    def _retest(st: dict, ts, bar, depth: str = "edge") -> tuple[int, dict] | None:
        """(direction, gap) of the newest gap this bar retested, after dropping invalidated gaps."""
        lo, hi, close = float(bar["low"]), float(bar["high"]), float(bar["close"])
        hit, keep = None, []
        for g in st["fvgs"]:
            if g["bullish"] and close < g["lower"] or not g["bullish"] and close > g["upper"]:
                continue                                         # closed through the far edge: gap is dead
            keep.append(g)
            if g["formed"] < ts:
                mid = (g["lower"] + g["upper"]) / 2
                touch_up = lo <= (mid if depth == "mid" else g["upper"])
                touch_dn = hi >= (mid if depth == "mid" else g["lower"])
                if g["bullish"] and touch_up and close > g["upper"]:
                    hit = (1, g)
                elif not g["bullish"] and touch_dn and close < g["lower"]:
                    hit = (-1, g)
        if hit:
            keep.remove(hit[1])                                  # each gap trades once
        st["fvgs"] = keep
        return hit

    # ------------------------------------------------------------------ helpers
    def _atr(self, ctx: Context) -> float:
        return float(atr(ctx.spot("1min", lookback=60)).iloc[-1])

    def _trend_ok(self, ctx: Context, d: int, close: float) -> bool:
        if self.p["trend"] == "vwap":
            return d * (close - float(vwap(ctx.spot("1min", today_only=True)).iloc[-1])) > 0
        e = ema(ctx.spot("1min", lookback=200)["close"], 50)
        return d * (float(e.iloc[-1]) - float(e.iloc[-6])) > 0

    def _liquidity_target(self, ctx: Context, d: int, entry: float, risk: float) -> float:
        """Nearest confirmed swing (or today's high / low) beyond tp_min_r x risk, capped at tp_max_r."""
        p = self.p
        day = ctx.spot("1min", today_only=True)
        sw = swing_points(day)
        levels = list(sw["swing_high" if d == 1 else "swing_low"].dropna()) + \
            [float(day["high"].max() if d == 1 else day["low"].min())]
        lo_t, hi_t = entry + d * p["tp_min_r"] * risk, entry + d * p["tp_max_r"] * risk
        beyond = [x for x in levels if d * (x - lo_t) >= 0]
        t = min(beyond, key=lambda x: d * x) if beyond else hi_t
        return min(t, hi_t) if d == 1 else max(t, hi_t)

    def _proxy_flow(self, ctx: Context, n: int) -> pd.DataFrame:
        """Close-location delta from futures candles (backtests only): vol x (2c - h - l) / (h - l)."""
        f = ctx.futures("1min", lookback=n + 400, today_only=True).iloc[-n:]
        rng = (f["high"] - f["low"]).where(lambda r: r > 0)
        delta = (f["volume"] * (2 * f["close"] - f["high"] - f["low"]) / rng).fillna(0.0)
        day = ctx.futures("1min", lookback=400, today_only=True)
        drng = (day["high"] - day["low"]).where(lambda r: r > 0)
        cum = float((day["volume"] * (2 * day["close"] - day["high"] - day["low"]) / drng).fillna(0.0).sum())
        return pd.DataFrame({"volume": f["volume"], "delta": delta, "cum_delta": cum}, index=f.index)

    def _flow_ok(self, ctx: Context, d: int):
        """Summed delta if order flow agrees, None if there is no flow data (backtest), else False."""
        p = self.p
        n = max(p["flow_bars"], 21) if p["delta_z"] is not None else p["flow_bars"]
        try:
            flow = ctx.order_flow("1min", lookback=n)
        except NotImplementedError:
            flow = None
            if p["flow_proxy"]:
                try:
                    flow = self._proxy_flow(ctx, n)
                except RuntimeError:            # no futures stored for this period
                    flow = None
            if flow is None:
                self.skips["no_flow_data"] += 1
                return None
        if len(flow) < p["flow_bars"]:
            self.skips["flow"] += 1
            return False
        last = flow.iloc[-p["flow_bars"]:]
        delta = float(last["delta"].sum())
        ok = d * delta > 0 and abs(delta) >= p["min_delta"]
        if ok and p["min_delta_ratio"] is not None:
            vol = float(last["volume"].sum())
            ok = vol > 0 and abs(delta) / vol >= p["min_delta_ratio"]
        if ok and p["delta_z"] is not None:
            hist = flow["delta"].iloc[:-1]
            sd = float(hist.std()) if len(hist) >= 5 else 0.0
            ok = sd > 0 and d * (float(flow["delta"].iloc[-1]) - float(hist.mean())) / sd >= p["delta_z"]
        if ok and p["require_cum_delta"]:
            ok = d * float(flow["cum_delta"].iloc[-1]) > 0
        if not ok:
            self.skips["flow"] += 1
            return False
        return delta
