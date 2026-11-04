"""Fade the session VWAP +/- N standard-deviation bands (default 3 sd).

    Bands         session VWAP of SPOT prices weighted by front-month futures volume (`ctx.spot`
                  merges it; futures data exists from 2024-09-19) +/- band_sd x volume-weighted sd
    Stretch       a `tf` bar closes beyond a band (sd >= min_sd_pts); the stretch extreme (highest
                  high above the upper band / lowest low below the lower band) is tracked
    Trigger       confirm=reentry (default): the first close back inside the band, provided the
                  stretch lasted at most max_stretch_bars closes (a longer one is a trend day: no fade)
                  confirm=touch: enter on the stretch close itself
    Entry         upper-band stretch -> buy PE, lower-band stretch -> buy CE, at the trigger close
    Stop          stretch extreme +/- sl_buffer_pts (min_risk_pts .. max_risk_pts)
    Target        fixed at entry: tp_type=band -> VWAP + target_sd x sd in the trade direction
                  (0 = VWAP, -1 = the near 1 sd band, +1 = 1 sd past VWAP), skipped
                  if that is under min_rr x risk away; tp_type=rr -> target_r x risk
    Time exit     exit_time. Every exit lives in the index plan, so `algo sweep --mode index` is exact.
With require_volume=True (default) days without futures volume are skipped: before Sep 2024 the
"VWAP" would only be an equal-weight mean.
"""
from __future__ import annotations

from collections import Counter


from algo.core.context import Context
from algo.core.strategy import Strategy
from algo.indicators import vwap_bands
from algo.strategies.index_plan import IndexPlanMixin, hm


class VwapSdFade(IndexPlanMixin, Strategy):
    """Mean reversion: close beyond VWAP +/- 3 sd, then back inside -> buy the option toward VWAP."""

    name = "vwap_sd_fade"
    index_replay_exact = True
    defaults = {
        "tf": "5min",
        "band_sd": 3.0,             # stretch band: VWAP +/- band_sd x sd
        "confirm": "reentry",       # reentry | touch
        "max_stretch_bars": 6,      # reentry: closes outside the band before the fade is called off
        "min_sd_pts": 10.0,         # no setups while the band is this tight (quiet days, early session)
        "sl_buffer_pts": 5.0,
        "min_risk_pts": 8.0,
        "max_risk_pts": 60.0,
        "tp_type": "band",          # band (VWAP +/- target_sd x sd) | rr (target_r x risk)
        "target_sd": 0.0,           # 0 = VWAP, -1 = the 1 sd band short of VWAP, +1 = 1 sd past VWAP
        "min_rr": 1.0,              # band target must be at least this many R away
        "target_r": 1.5,            # tp_type=rr only
        "breakeven_at_r": None,
        "trail_start_r": None,
        "require_volume": True,
        "option_sl_pct": 35.0,
        "moneyness": "ITM1",
        "delta_est": 0.6,
        "expiry": "nearest",
        "min_dte": 0,
        "lots": 1,
        "size_from_capital": True,
        "max_trades_per_day": 2,
        "entry_start": "10:00",
        "entry_end": "14:30",
        "exit_time": "15:00",
    }

    def __init__(self, **params):
        super().__init__(**params)
        if self.p["confirm"] not in ("reentry", "touch"):
            raise ValueError("confirm must be reentry | touch")
        if self.p["tp_type"] not in ("band", "rr"):
            raise ValueError("tp_type must be band | rr")
        self.skips: Counter = Counter()
        self.records: list[dict] = []

    def on_day_start(self, ctx: Context) -> None:
        ctx.state.update(plans={}, stretch=None, last_bar=None, no_volume=False)

    def on_bar(self, ctx: Context) -> None:
        p, st = self.p, ctx.state
        self._manage_minute_exits(ctx)
        today = ctx.spot(p["tf"], today_only=True)
        if today.empty or today.index[-1] == st["last_bar"] or today.index[-1].date() != ctx.today:
            return
        st["last_bar"] = today.index[-1]
        if st["no_volume"]:
            return
        if p["require_volume"] and not ("volume" in today and (today["volume"] > 0).any()):
            if len(today) >= 3:          # the first bars of a day can legitimately be thin
                st["no_volume"] = True
                self.skips["no_volume"] += 1
            return
        b = vwap_bands(today)
        bar = today.iloc[-1]
        close, hi, lo = float(bar["close"]), float(bar["high"]), float(bar["low"])
        v, sd = float(b["vwap"].iloc[-1]), float(b["sd"].iloc[-1])
        k = p["band_sd"]
        side = 1 if close > v + k * sd else -1 if close < v - k * sd else 0   # +1 = above upper band
        if sd < p["min_sd_pts"]:
            side = 0

        s = st["stretch"]
        if side != 0:
            if s is None or s["side"] != side:
                s = st["stretch"] = {"side": side, "extreme": hi if side == 1 else lo, "bars": 0}
            s["extreme"] = max(s["extreme"], hi) if side == 1 else min(s["extreme"], lo)
            s["bars"] += 1
            if p["confirm"] == "touch":
                st["stretch"] = None
                self._enter(ctx, -side, close, s["extreme"], v, sd)
            return
        if s is None:
            return
        st["stretch"] = None
        if s["bars"] > p["max_stretch_bars"]:
            self.skips["stretch_too_long"] += 1
            return
        extreme = max(s["extreme"], hi) if s["side"] == 1 else min(s["extreme"], lo)
        self._enter(ctx, -s["side"], close, extreme, v, sd)

    def _enter(self, ctx: Context, d: int, entry: float, extreme: float, v: float, sd: float) -> None:
        p = self.p
        if ctx.active_positions or ctx.broker.pending_orders():
            self.skips["in_position"] += 1
            return
        if not (hm(p["entry_start"]) <= ctx.now.time() < hm(p["entry_end"])) \
                or ctx.trades_today() >= p["max_trades_per_day"]:
            self.skips["window_or_max_trades"] += 1
            return
        stop = extreme - d * p["sl_buffer_pts"]
        risk = d * (entry - stop)
        if not (p["min_risk_pts"] <= risk <= p["max_risk_pts"]):
            self.skips["risk_pts"] += 1
            return
        if p["tp_type"] == "band":
            target = v + d * p["target_sd"] * sd
            if d * (target - entry) < p["min_rr"] * risk:
                self.skips["target_too_close"] += 1
                return
        else:
            target = entry + d * p["target_r"] * risk
        side = "up" if d == 1 else "down"
        self._buy_with_plan(ctx, d, entry, stop, f"vsd_{side}", target=target,
                            extra={"side": side, "vwap": v, "sd": sd, "band_sd": p["band_sd"]})
