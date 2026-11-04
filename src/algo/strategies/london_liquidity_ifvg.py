"""Liquidity sweep of the morning box + inverse fair value gap (IFVG) entry.

Port of the "London Liquidity / IFVG" futures strategy (rq_sdk / Pine). NSE has no London
session, so the "London box" is a morning range (default 09:15-10:15 IST).

    Box         high/low of the 5-min bars in [box_start, box_end)
    Sweep (LS)  inside [sweep_start, sweep_end), once per day:
                  bearish: a close above the box high, then a large-bodied bar (body > body_pct
                           of its range) closing back at/below it   -> ls_price = that bar's high
                  bullish: the mirror at the box low                 -> ls_price = that bar's low
    FVGs        3-bar gaps (bars[-3] vs the current bar) formed inside the box window or after
                the sweep bar; same day only, at most max_fvg_age_min old
    Entry       after the sweep, outside the box, inside the sweep window, once per day:
                  a bullish FVG closed below its lower edge (bearish IFVG) after a bearish LS,
                  with fvg.lower <= ls_price                                   -> buy PE
                  a bearish FVG closed above its upper edge (bullish IFVG) after a bullish LS,
                  with fvg.upper >= ls_price                                   -> buy CE
    Exits       on the INDEX. Stop: close -/+ sl_atr_mult x ATR (sl_type=atr) or the extreme of
                the last swing_lookback bars (sl_type=swing). Target: +/- tp_atr_mult x ATR
                (tp_type=atr), risk_reward x risk (rr) or the opposite box level (range).
                Time exit, premium safety-net stop.
levels=pdhl uses the previous day's high/low instead of the box; today's gaps before the sweep
then take the place of the box-session gaps.
Opt-in filters (off by default): ls_confirm=rejection|either (pin bar / engulfing sweep bar),
min_sweep_atr (sweep depth), min_fvg_atr (gap size), entry_body_pct (decisive entry bar), sl_type=ls (stop beyond the sweep extreme), max_bars_after_ls
(stale sweeps), fvg_source=after_ls (only gaps formed after the sweep), entry_start, min/max_risk_pts.
Sizing (fixed lots or % risk from capital) and the weekday filter come from the framework:
`lots` / `size_from_capital` and calendar.skip_weekdays / --skip-days.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from algo.core.context import Context
from algo.core.strategy import Strategy
from algo.indicators import atr, is_engulfing, is_pin_bar, previous_session
from algo.strategies.index_plan import IndexPlanMixin, hm

MAX_TRACKED_FVG = 64


class LondonLiquidityIfvg(IndexPlanMixin, Strategy):
    """Morning-box liquidity sweep, then trade the first IFVG that agrees with the sweep."""

    name = "london_liquidity_ifvg"
    index_replay_exact = True
    defaults = {
        "tf": "5min",
        "levels": "box",               # box (morning range) | pdhl (previous day high/low)
        "box_start": "09:15",          # the "London box" (levels=box only)
        "box_end": "10:15",
        "sweep_start": "10:15",        # sweeps and entries (13:30 = the risk manager's entry cutoff)
        "sweep_end": "13:30",
        "entry_start": None,           # e.g. "11:00": no IFVG entries before this (None = sweep_start)
        "max_bars_after_ls": None,     # IFVG must fire within N bars of the sweep bar (None = any time)
        "fvg_source": "both",          # both (box + after the sweep) | after_ls (only gaps after the sweep)
        "body_pct": 50.0,              # LS bar: body > this % of its range
        "ls_confirm": "body",          # body | rejection (pin bar / engulfing) | either
        "wick_ratio": 0.5,             # pin bar: rejection wick >= this fraction of the range
        "min_sweep_atr": None,         # the sweep must reach this x ATR beyond the level (None = off)
        "min_fvg_atr": None,           # gaps smaller than this x ATR are ignored (None = off)
        "entry_body_pct": None,        # entry bar body > this % of its range (None = off)
        "max_fvg_age_min": 600,
        "sl_type": "atr",              # atr | swing | ls (sweep extreme +/- sl_buffer_pts)
        "sl_atr_mult": 1.5,
        "sl_buffer_pts": 5.0,
        "min_risk_pts": None,          # skip setups whose index stop is closer / further than this
        "max_risk_pts": None,
        "swing_lookback": 5,
        "tp_type": "atr",              # atr | rr | range
        "tp_atr_mult": 1.5,
        "risk_reward": 2.0,
        "atr_period": 14,
        "breakeven_at_r": None,
        "target_r": None,              # unused: the target comes from tp_type
        "option_sl_pct": 35.0,
        "moneyness": "ITM1",
        "delta_est": 0.6,
        "expiry": "nearest",
        "min_dte": 0,
        "lots": 1,
        "size_from_capital": True,
        "exit_time": "15:00",
    }

    def __init__(self, **params):
        super().__init__(**params)
        if self.p["sl_type"] not in ("atr", "swing", "ls"):
            raise ValueError("sl_type must be atr | swing | ls")
        if self.p["levels"] not in ("box", "pdhl"):
            raise ValueError("levels must be box | pdhl")
        if self.p["fvg_source"] not in ("both", "after_ls"):
            raise ValueError("fvg_source must be both | after_ls")
        if self.p["ls_confirm"] not in ("body", "rejection", "either"):
            raise ValueError("ls_confirm must be body | rejection | either")
        if self.p["tp_type"] not in ("atr", "rr", "range"):
            raise ValueError("tp_type must be atr | rr | range")
        self.skips: Counter = Counter()
        self.records: list[dict] = []
        self.signals: list[dict] = []   # every confirmed setup, including ones that were not filled

    def on_day_start(self, ctx: Context) -> None:
        ctx.state.update(plans={}, last_bar=None, box_high=None, box_low=None, box_ready=False,
                         high_swept=False, low_swept=False, hi_ext=None, lo_ext=None, ls_dir=0, ls_price=None, ls_time=None,
                         done=False, fvgs=[])
        if self.p["levels"] == "pdhl":
            prev = previous_session(ctx.spot(lookback=800), ctx.today)
            if len(prev):
                ctx.state.update(box_high=float(prev["high"].max()), box_low=float(prev["low"].min()),
                                 box_ready=True)

    # ------------------------------------------------------------------ bar
    def on_bar(self, ctx: Context) -> None:
        p, st = self.p, ctx.state
        self._manage_minute_exits(ctx)
        bars = ctx.spot(p["tf"], lookback=200)
        if len(bars) < 3 or bars.index[-1] == st["last_bar"] or bars.index[-1].date() != ctx.today:
            return
        st["last_bar"] = ts = bars.index[-1]
        bar = bars.iloc[-1]
        t = ts.time()
        pdhl = p["levels"] == "pdhl"
        in_box = not pdhl and hm(p["box_start"]) <= t < hm(p["box_end"])
        in_sweep = hm(p["sweep_start"]) <= t < hm(p["sweep_end"])

        if in_box:
            hi, lo = float(bar["high"]), float(bar["low"])
            st["box_high"] = hi if st["box_high"] is None else max(st["box_high"], hi)
            st["box_low"] = lo if st["box_low"] is None else min(st["box_low"], lo)
        elif st["box_high"] is not None and t >= hm(p["box_end"]):
            st["box_ready"] = True

        a = float(atr(bars, p["atr_period"]).iloc[-1])
        if st["box_ready"] and in_sweep and st["ls_dir"] == 0:
            self._detect_sweep(st, bars, a)

        if not st["done"]:
            # pdhl: gaps formed today before the sweep play the role of the box-session gaps
            self._track_fvg(st, bars, st["ls_dir"] == 0 if pdhl else in_box, a)
            n = p["max_bars_after_ls"]
            if st["ls_time"] is not None and n is not None and ts - st["ls_time"] > n * pd.Timedelta(p["tf"]):
                st["done"] = True              # the sweep went stale without an IFVG
                self.skips["stale_sweep"] += 1
                return
            entry_ok = p["entry_start"] is None or t >= hm(p["entry_start"])
            if st["ls_dir"] != 0 and not in_box and in_sweep and entry_ok:
                self._check_inversions(ctx, st, bars, a)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _large_body(bar, pct: float) -> bool:
        rng = float(bar["high"] - bar["low"])
        return rng > 0 and abs(float(bar["close"] - bar["open"])) * 100 / rng > pct

    def _confirms(self, bar, prev, side: str) -> bool:
        """Sweep-back bar confirmation; side 'up' = a high was swept (bearish), 'down' = a low."""
        mode = self.p["ls_confirm"]
        body = self._large_body(bar, self.p["body_pct"])
        rejection = is_pin_bar(bar, side, self.p["wick_ratio"]) or is_engulfing(prev, bar, side)
        return body if mode == "body" else rejection if mode == "rejection" else (body or rejection)

    def _detect_sweep(self, st: dict, bars: pd.DataFrame, a: float) -> None:
        bar, prev, ts = bars.iloc[-1], bars.iloc[-2], bars.index[-1]
        close, hi, lo = float(bar["close"]), float(bar["high"]), float(bar["low"])
        min_depth = (self.p["min_sweep_atr"] or 0) * a
        if close > st["box_high"]:
            st["high_swept"] = True
        if st["high_swept"]:
            st["hi_ext"] = hi if st["hi_ext"] is None else max(st["hi_ext"], hi)
            if (close <= st["box_high"] and self._confirms(bar, prev, "up")
                    and st["hi_ext"] - st["box_high"] >= min_depth):
                st.update(ls_dir=-1, ls_price=hi, ls_time=ts)
                return
        if close < st["box_low"]:
            st["low_swept"] = True
        if st["low_swept"]:
            st["lo_ext"] = lo if st["lo_ext"] is None else min(st["lo_ext"], lo)
            if (close >= st["box_low"] and self._confirms(bar, prev, "down")
                    and st["box_low"] - st["lo_ext"] >= min_depth):
                st.update(ls_dir=1, ls_price=lo, ls_time=ts)

    def _track_fvg(self, st: dict, bars: pd.DataFrame, in_box: bool, a: float) -> None:
        after_ls = st["ls_time"] is not None and bars.index[-1] > st["ls_time"]
        if self.p["fvg_source"] == "after_ls":
            in_box = False
        if not (in_box or after_ls) or len(st["fvgs"]) >= MAX_TRACKED_FVG:
            return
        b2, bar = bars.iloc[-3], bars.iloc[-1]
        if bars.index[-3].date() != bars.index[-1].date():
            return   # a gap across the overnight open is not an FVG
        min_size = (self.p["min_fvg_atr"] or 0) * a
        if b2["high"] < bar["low"] and bar["low"] - b2["high"] >= min_size:
            st["fvgs"].append({"start": bars.index[-3], "lower": float(b2["high"]),
                               "upper": float(bar["low"]), "bullish": True})
        if b2["low"] > bar["high"] and b2["low"] - bar["high"] >= min_size:
            st["fvgs"].append({"start": bars.index[-3], "lower": float(bar["high"]),
                               "upper": float(b2["low"]), "bullish": False})

    def _check_inversions(self, ctx: Context, st: dict, bars: pd.DataFrame, a: float) -> None:
        p = self.p
        ts, close = bars.index[-1], float(bars["close"].iloc[-1])
        if p["entry_body_pct"] is not None and not self._large_body(bars.iloc[-1], p["entry_body_pct"]):
            return                                     # weak entry bar: wait for a decisive close
        max_age = pd.Timedelta(minutes=p["max_fvg_age_min"])
        st["fvgs"] = [f for f in st["fvgs"] if ts - f["start"] <= max_age]
        for f in reversed(st["fvgs"]):                 # newest first, as in the original
            if f["bullish"]:                           # closed below a bullish gap = bearish IFVG
                ok = close < f["lower"] and st["ls_dir"] == -1 and f["lower"] <= st["ls_price"]
            else:                                      # closed above a bearish gap = bullish IFVG
                ok = close > f["upper"] and st["ls_dir"] == 1 and f["upper"] >= st["ls_price"]
            if ok:
                st["done"], st["fvgs"] = True, []      # one setup per day
                self._enter(ctx, st, bars, -1 if f["bullish"] else 1, close, f, a)
                return

    def _enter(self, ctx: Context, st: dict, bars: pd.DataFrame, d: int, entry: float, fvg: dict,
               a: float) -> None:
        p = self.p
        recent = bars.iloc[-max(1, int(p["swing_lookback"])):]
        if p["sl_type"] == "atr":
            stop = entry - d * p["sl_atr_mult"] * a
        elif p["sl_type"] == "ls":
            stop = st["ls_price"] - d * p["sl_buffer_pts"]
        else:
            stop = float(recent["low"].min()) if d == 1 else float(recent["high"].max())
        risk = d * (entry - stop)
        if p["tp_type"] == "atr":
            target = entry + d * p["tp_atr_mult"] * a
        elif p["tp_type"] == "rr":
            target = entry + d * p["risk_reward"] * risk
        else:
            target = st["box_high"] if d == 1 else st["box_low"]
        side = "long" if d == 1 else "short"
        sig = {"date": ctx.today, "time": ctx.now.strftime("%H:%M"), "side": side, "entry": entry, "stop": stop,
               "target": target, "risk": risk, "atr": a, "box_high": st["box_high"], "box_low": st["box_low"],
               "ls_price": st["ls_price"], "fvg_lower": fvg["lower"], "fvg_upper": fvg["upper"],
               "valid": False}
        self.signals.append(sig)
        if risk <= 0 or d * (target - entry) <= 0 or pd.isna(a):
            self.skips["bad_levels"] += 1
            return
        if (p["min_risk_pts"] is not None and risk < p["min_risk_pts"]) or \
                (p["max_risk_pts"] is not None and risk > p["max_risk_pts"]):
            self.skips["risk_pts"] += 1
            return
        sig["valid"] = True                            # a tradable plan (used by index-only research)
        if ctx.active_positions or ctx.broker.pending_orders():
            self.skips["in_position"] += 1
            return
        self._buy_with_plan(ctx, d, entry, stop, f"ifvg_{side}", target=target,
                            extra={k: sig[k] for k in ("side", "atr", "box_high", "box_low", "ls_price",
                                                       "fvg_lower", "fvg_upper")})
