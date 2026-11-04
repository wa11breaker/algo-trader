"""VWAP + EMA 9/21 trend following with pullback entries.

    Trend (long)     5-min close above session VWAP and EMA9 above EMA21 (optionally EMA9 rising
                     over `ema_slope_bars`); the previous close was above VWAP too
    Pullback         a bar's low comes within `touch_pts` of VWAP and/or EMA9 (`pullback_to`), not
                     piercing it by more than `pierce_pts`, and the bar closes above VWAP
    Confirmation     within `confirm_bars` after the pullback: a green bar closing above the previous
                     bar's high; with confirm_mode=any a bullish pin bar / engulfing pullback bar also
                     confirms. The setup is dropped if the close loses VWAP or EMA9 crosses EMA21.
    Entry            buy CE (PE for the mirror image) at the confirmation close
    Exits            exit_mode=premium (default): option stop `sl_pct` % of premium, target `rr` x sl_pct %
                     exit_mode=index: index stop = pullback extreme -/+ sl_buffer_pts, target `target_r` R,
                                      premium safety-net stop `option_sl_pct`
                     both modes: trend-structure exit on a 5-min close back through VWAP and/or an EMA9/21
                     cross against the trade (`structure_exit`), and a time exit at `exit_time`

Every signal is also logged to `plan_log` as an index plan (pullback stop, target_r target), so
`algo sweep --mode index` can test the signal on spot data. For exit_mode=premium that replay is only
an approximation of the real exits.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from algo.core.context import Context
from algo.core.models import ExitReason, OptionType
from algo.core.strategy import Strategy
from algo.indicators import ema, is_engulfing, is_pin_bar, vwap
from algo.strategies.index_plan import IndexPlanMixin, hm


class VwapEmaTrend(IndexPlanMixin, Strategy):
    """VWAP + EMA9/21 trend: pullback to VWAP/EMA9 + bullish/bearish confirmation; buy CE/PE."""

    name = "vwap_ema_trend"
    defaults = {
        "tf": "5min",
        "fast": 9,
        "slow": 21,
        "ema_slope_bars": 0,        # >0: EMA9 must also be rising (falling) over this many bars
        "pullback_to": "either",    # vwap | ema | either
        "touch_pts": 8.0,           # pullback: low within this of the level ...
        "pierce_pts": 10.0,         # ... and not more than this below it
        "confirm_bars": 2,          # bars after the pullback to wait for confirmation
        "confirm_mode": "any",      # break_high | any (break_high or pin bar / engulfing pullback bar)
        "wick_ratio": 0.5,
        "exit_mode": "premium",     # premium | index
        "sl_pct": 25.0,             # premium mode: stop as % of option premium
        "rr": 2.0,                  # premium mode: target = rr x sl_pct (% of premium)
        "structure_exit": "either", # vwap | ema | either | none
        "breakdown_pts": 0.0,       # VWAP exit: close this far through VWAP against the trade
        "sl_buffer_pts": 5.0,       # index stop beyond the pullback extreme
        "min_risk_pts": 8.0,        # index mode only
        "max_risk_pts": 40.0,       # index mode only
        "target_r": 2.0,            # index mode target (also used for plan_log)
        "breakeven_at_r": None,
        "option_sl_pct": 35.0,      # index mode premium safety net
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

    _CHOICES = {"pullback_to": ("vwap", "ema", "either"), "confirm_mode": ("break_high", "any"),
                "exit_mode": ("premium", "index"), "structure_exit": ("vwap", "ema", "either", "none")}

    def __init__(self, **params):
        super().__init__(**params)
        for k, allowed in self._CHOICES.items():
            if self.p[k] not in allowed:
                raise ValueError(f"{k} must be one of {' | '.join(allowed)}")
        if self.p["sl_pct"] <= 0 or self.p["rr"] <= 0:
            raise ValueError("sl_pct and rr must be > 0")
        self.skips: Counter = Counter()
        self.records: list[dict] = []

    def on_day_start(self, ctx: Context) -> None:
        ctx.state.update(plans={}, meta={}, setup=None, last_bar=None)

    # ------------------------------------------------------------------ bar
    def on_bar(self, ctx: Context) -> None:
        p, st = self.p, ctx.state
        self._minute_exits(ctx)
        bars = ctx.spot(p["tf"], lookback=150)
        if len(bars) < p["slow"] + 2 or bars.index[-1] == st["last_bar"] or bars.index[-1].date() != ctx.today:
            return
        st["last_bar"] = bars.index[-1]
        today = bars[bars.index.normalize() == pd.Timestamp(ctx.today)]
        if len(today) < 2:
            return
        close_s = bars["close"]
        e_fast, e_slow = ema(close_s, p["fast"]), ema(close_s, p["slow"])
        vw = vwap(today)
        bar, prev = bars.iloc[-1], bars.iloc[-2]
        close, v = float(bar["close"]), float(vw.iloc[-1])
        ef, es = float(e_fast.iloc[-1]), float(e_slow.iloc[-1])

        self._structure_exit(ctx, close, v, ef, es)
        if ctx.active_positions or ctx.broker.pending_orders():
            return

        s = st["setup"]
        if s is not None:
            d = s["dir"]
            s["bars"] += 1
            s["extreme"] = min(s["extreme"], float(bar["low"])) if d == 1 else max(s["extreme"], float(bar["high"]))
            if d * (close - v) <= 0:
                self._drop(st, "lost_vwap")
            elif d * (ef - es) <= 0:
                self._drop(st, "ema_flip")
            elif self._break_confirms(bar, prev, d):
                self._drop(st, None)
                self._enter(ctx, d, close, s["extreme"], v, ef, es)
            elif s["bars"] >= p["confirm_bars"]:
                self._drop(st, "no_confirmation")
            return

        d = self._trend(close, v, float(today["close"].iloc[-2]), float(vw.iloc[-2]), e_fast, ef, es)
        if d == 0:
            return
        extreme = float(bar["low"]) if d == 1 else float(bar["high"])
        if not self._touched(extreme, d, v, ef):
            return
        if p["confirm_mode"] == "any" and self._pattern_confirms(bar, prev, d):
            self._enter(ctx, d, close, extreme, v, ef, es)
            return
        st["setup"] = {"dir": d, "extreme": extreme, "bars": 0}

    # ------------------------------------------------------------ signal helpers
    def _drop(self, st: dict, why: str | None) -> None:
        st["setup"] = None
        if why:
            self.skips[why] += 1

    def _trend(self, close: float, v: float, prev_close: float, prev_vwap: float, e_fast: pd.Series,
               ef: float, es: float) -> int:
        """+1 / -1 if price is on one side of VWAP (now and on the previous bar) and the EMAs agree."""
        d = 1 if close > v and ef > es else -1 if close < v and ef < es else 0
        if d == 0 or d * (prev_close - prev_vwap) <= 0:
            return 0
        k = self.p["ema_slope_bars"]
        if k and (len(e_fast) <= k or d * float(e_fast.iloc[-1] - e_fast.iloc[-1 - k]) <= 0):
            return 0
        return d

    def _touched(self, extreme: float, d: int, v: float, ef: float) -> bool:
        """Did the bar's low (high for shorts) pull back to VWAP and/or EMA9?"""
        p = self.p
        levels = {"vwap": [v], "ema": [ef], "either": [v, ef]}[p["pullback_to"]]
        # distance of the extreme beyond the level, measured against the trade: <0 = still above (long)
        return any(-p["touch_pts"] <= d * (lvl - extreme) <= p["pierce_pts"] for lvl in levels)

    @staticmethod
    def _break_confirms(bar, prev, d: int) -> bool:
        if d == 1:
            return bar["close"] > bar["open"] and bar["close"] > prev["high"]
        return bar["close"] < bar["open"] and bar["close"] < prev["low"]

    def _pattern_confirms(self, bar, prev, d: int) -> bool:
        side = "down" if d == 1 else "up"     # a bullish rejection = long lower wick
        pin = is_pin_bar(bar, side, self.p["wick_ratio"]) and (bar["close"] > bar["open"] if d == 1
                                                              else bar["close"] < bar["open"])
        return pin or is_engulfing(prev, bar, side)

    # ------------------------------------------------------------ entry
    def _enter(self, ctx: Context, d: int, entry: float, extreme: float, v: float, ef: float, es: float) -> None:
        p = self.p
        t = ctx.now.time()
        if not (hm(p["entry_start"]) <= t < hm(p["entry_end"])) or ctx.trades_today() >= p["max_trades_per_day"]:
            self.skips["window_or_max_trades"] += 1
            return
        side = "up" if d == 1 else "down"
        tag = f"vet_{side}"
        stop = extreme - d * p["sl_buffer_pts"]
        info = {"side": side, "vwap": v, "ema_fast": ef, "ema_slow": es, "pullback_extreme": extreme}
        if p["exit_mode"] == "index":
            risk = d * (entry - stop)
            if not (p["min_risk_pts"] <= risk <= p["max_risk_pts"]):
                self.skips["risk_pts"] += 1
                return
            self._buy_with_plan(ctx, d, entry, stop, tag, extra=info)
            return
        self._buy_premium(ctx, d, entry, stop, tag, info)

    def _buy_premium(self, ctx: Context, d: int, entry: float, stop: float, tag: str, info: dict) -> None:
        p = self.p
        risk = d * (entry - stop)
        # index view of the signal for `algo sweep --mode index` (an approximation of premium exits)
        self.__dict__.setdefault("plan_log", []).append({
            "now": ctx.now, "tag": tag, "dir": d, "entry": entry, "stop": stop,
            "target": entry + d * p["target_r"] * risk if risk > 0 and p["target_r"] else None,
            "risk": risk, "trail_dist": None})
        kind = OptionType.CE if d == 1 else OptionType.PE
        opt = ctx.select_option(kind, p["moneyness"], p["expiry"], p["min_dte"])
        ltp = ctx.ltp(opt) if opt else None
        if not ltp:
            self.skips["no_option"] += 1
            return
        per_lot_risk = ltp * p["sl_pct"] / 100 * opt.lot_size
        lots = p["lots"]
        if p["size_from_capital"] and ctx.capital is not None:
            lots = ctx.capital.lots_for(per_lot_risk, ltp * opt.lot_size)
            if lots == 0:
                self.skips["too_small_to_size"] += 1
                return
        order = ctx.buy(opt, lots=lots, sl_pct=p["sl_pct"], target_pct=p["rr"] * p["sl_pct"], tag=tag,
                        max_loss=per_lot_risk * lots)
        if order is None:
            self.skips["risk_manager"] += 1
            return
        ctx.state["meta"][tag] = {"date": ctx.today, "time": ctx.now.strftime("%H:%M"), "dir": d,
                                  "entry": entry, "lots": lots, **info}

    # ------------------------------------------------------------ exits
    def _minute_exits(self, ctx: Context) -> None:
        if self.p["exit_mode"] == "index":
            self._manage_minute_exits(ctx)
            return
        if ctx.active_positions and ctx.now.time() >= hm(self.p["exit_time"]):
            for pos in ctx.active_positions:
                ctx.exit(pos, ExitReason.SIGNAL)
                self._mark(ctx, pos, "time")

    def _structure_exit(self, ctx: Context, close: float, v: float, ef: float, es: float) -> None:
        mode = self.p["structure_exit"]
        if mode == "none":
            return
        for pos in ctx.active_positions:
            d = 1 if pos.contract.option_type is OptionType.CE else -1
            if mode in ("vwap", "either") and d * (close - v) < -self.p["breakdown_pts"]:
                why = "vwap"
            elif mode in ("ema", "either") and d * (ef - es) < 0:
                why = "ema"
            else:
                continue
            ctx.exit(pos, ExitReason.SIGNAL)
            self._mark(ctx, pos, why)

    @staticmethod
    def _mark(ctx: Context, pos, why: str) -> None:
        for store in ("plans", "meta"):
            rec = ctx.state.get(store, {}).get(pos.tag)
            if rec is not None:
                rec["why"] = why

    # ------------------------------------------------------------ diagnostics
    def on_trade_closed(self, ctx: Context, trade) -> None:
        if trade.tag in ctx.state.get("plans", {}):
            super().on_trade_closed(ctx, trade)
            return
        meta = ctx.state.get("meta", {}).pop(trade.tag, None)
        if meta is None:
            return
        bars = ctx.data.spot_bars(trade.exit_time, lookback=1)
        idx_exit = float(bars["open"].iloc[-1]) if len(bars) else float("nan")
        d = meta.pop("dir")
        why = meta.pop("why", None) or trade.exit_reason.value.lower()
        self.records.append({
            **meta, "why": why,
            "tag": trade.tag, "option": trade.contract.symbol, "entry_time": trade.entry_time,
            "exit_time": trade.exit_time, "exit_reason": trade.exit_reason.value,
            "index_exit": idx_exit, "index_move": d * (idx_exit - meta["entry"]),
            "minutes_held": (pd.Timestamp(trade.exit_time) - pd.Timestamp(trade.entry_time)).total_seconds() / 60,
            "option_entry": trade.entry_price, "option_exit": trade.exit_price,
            "option_ret_pct": round(100 * (trade.exit_price / trade.entry_price - 1), 2) if trade.entry_price else None,
            "net_pnl": round(trade.net_pnl, 2),
        })
