"""Failed Breakout Reversal (a.k.a. false breakout / fakeout / bull-bear trap / liquidity sweep).

Idea: stops and breakout-buyers sit just beyond obvious levels. When price pokes
through a level, triggers them and cannot hold, the trapped traders exit and price
snaps back. We fade the move:

    upside sweep  + rejection -> buy PE
    downside sweep + rejection -> buy CE

Levels:   OR (opening range), PD (previous day high/low), SW (intraday swing high/low) by default;
           optional PDC (previous day close) and PW (previous week high/low)
Rejection: close back inside the level, or a rejection candle (pin bar / engulfing)
Exits:     on the INDEX - stop beyond the sweep extreme, target = rr x risk (default 1:2)
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from algo.core.context import Context
from algo.core.models import ExitReason, OptionType
from algo.core.strategy import Strategy
from algo.indicators import atr, is_engulfing, is_pin_bar, previous_session, previous_week

import pandas as pd

SESSION_OPEN = time(9, 15)


class FailedBreakoutReversal(Strategy):
    """Fade failed breakouts of OR / previous-day / swing levels (+ optional PDC, PW); index SL, 1:2 RR."""

    name = "failed_breakout"
    defaults = {
        "tf": "5min",
        "levels": "OR,PD,SW",          # any of OR, PD, SW, PDC, PW (comma separated)
        "range_minutes": 15,           # opening range length
        "swing_lookback": 12,          # SW level = extreme of the previous N tf-bars (today only)
        "break_buffer_pts": 2.0,       # price must exceed the level by this much to count as a breakout
        "max_bars_after_break": 3,     # rejection must come within N bars after the breakout bar
        "max_break_atr": 1.0,          # breakout running > this x ATR beyond the level = real breakout, ignore
        "rejection_mode": "any",       # any | close_inside | pattern
        "wick_ratio": 0.5,             # pin bar: rejection wick >= this fraction of the candle range
        "sl_buffer_pts": 5.0,          # index stop = sweep extreme +/- this
        "rr": 2.0,                     # risk:reward 1:rr  -> target = entry -/+ rr x risk
        "min_risk_pts": 8.0,           # skip if the index risk is tiny (noise)
        "max_risk_atr": 2.0,           # skip if the index risk > this x ATR
        "atr_period": 14,
        "breakeven_at_r": None,        # e.g. 1.0 -> move stop to entry once +1R (None = off)
        "option_sl_pct": 40.0,         # safety-net stop on the option premium
        "moneyness": "ATM",
        "expiry": "nearest",
        "min_dte": 0,
        "lots": 1,
        "max_trades_per_day": 3,
        "entry_start": "09:30",
        "entry_end": "14:30",
    }

    def __init__(self, **params):
        super().__init__(**params)
        self.levels = {s.strip().upper() for s in str(self.p["levels"]).split(",") if s.strip()}
        bad = self.levels - {"OR", "PD", "PDC", "PW", "SW"}
        if bad or self.p["rejection_mode"] not in ("any", "close_inside", "pattern"):
            raise ValueError(f"bad levels {bad} or rejection_mode {self.p['rejection_mode']!r}")
        self.signals: list[dict] = []   # every trade plan (including orders that never filled)
        self.records: list[dict] = []   # one row per closed trade: plan + outcome (see diagnostics())

    # ------------------------------------------------------------------ day
    def on_day_start(self, ctx: Context) -> None:
        ctx.state.update(fixed={}, watches={}, done=set(), plans={}, last_bar=None, or_ready=False)
        fixed = ctx.state["fixed"]
        if self.levels & {"PD", "PDC"}:
            prev = previous_session(ctx.spot(lookback=800), ctx.today)
            if len(prev):
                if "PD" in self.levels:
                    fixed[("PD", "up")] = float(prev["high"].max())
                    fixed[("PD", "down")] = float(prev["low"].min())
                if "PDC" in self.levels:   # a close can be swept from either side
                    fixed[("PDC", "up")] = fixed[("PDC", "down")] = float(prev["close"].iloc[-1])
        if "PW" in self.levels:
            wk = previous_week(ctx.spot(lookback=4500), ctx.today)
            if len(wk):
                fixed[("PW", "up")] = float(wk["high"].max())
                fixed[("PW", "down")] = float(wk["low"].min())

    # ------------------------------------------------------------------ bar
    def on_bar(self, ctx: Context) -> None:
        self._manage_exits(ctx)
        self._update_opening_range(ctx)

        p, st = self.p, ctx.state
        bars = ctx.spot(p["tf"], lookback=max(80, p["swing_lookback"] + 30))
        if len(bars) < 3 or bars.index[-1] == st["last_bar"]:
            return
        st["last_bar"] = bars.index[-1]
        if bars.index[-1].date() != ctx.today:
            return
        today = bars[bars.index.normalize() == bars.index[-1].normalize()]
        bar, prev = bars.iloc[-1], bars.iloc[-2]
        a = float(atr(bars, p["atr_period"]).iloc[-1])

        for key, level in self._levels(ctx, today).items():
            # done is keyed by level value: OR/PD trade once per side per day, while a
            # new swing level (SW) can be traded again
            if (*key, round(level, 2)) in st["done"]:
                continue
            name, side = key
            w = st["watches"].get(key)
            beyond = bar["high"] > level + p["break_buffer_pts"] if side == "up" \
                else bar["low"] < level - p["break_buffer_pts"]
            if w is None:
                # a breakout must come from inside: the previous bar closed on the near side
                came_from_inside = prev["close"] <= level if side == "up" else prev["close"] >= level
                if not (beyond and came_from_inside):
                    continue
                w = st["watches"][key] = {"level": level, "extreme": float(bar["high"] if side == "up" else bar["low"]),
                                          "bars": 0, "since": bars.index[-1]}
            else:
                w["bars"] += 1
                w["extreme"] = max(w["extreme"], float(bar["high"])) if side == "up" \
                    else min(w["extreme"], float(bar["low"]))

            if abs(w["extreme"] - w["level"]) > p["max_break_atr"] * a:
                self._kill(st, key)           # it ran too far: a real breakout, not a trap
                continue
            if self._rejected(bar, prev, w["level"], side, beyond):
                self._kill(st, key)
                self._enter(ctx, name, side, w, float(bar["close"]), a)
            elif w["bars"] >= p["max_bars_after_break"]:
                self._kill(st, key)           # held beyond the level: no trap

    # ------------------------------------------------------------ helpers
    def _update_opening_range(self, ctx: Context) -> None:
        st = ctx.state
        if st["or_ready"] or "OR" not in self.levels:
            return
        or_end = datetime.combine(ctx.today, SESSION_OPEN) + timedelta(minutes=self.p["range_minutes"])
        if ctx.bar_close_time < or_end:
            return
        first = ctx.spot(lookback=400, today_only=True)
        first = first[first.index < or_end]
        if len(first):
            st["fixed"][("OR", "up")] = float(first["high"].max())
            st["fixed"][("OR", "down")] = float(first["low"].min())
        st["or_ready"] = True

    def _levels(self, ctx: Context, today) -> dict:
        lv = dict(ctx.state["fixed"])
        if "OR" in self.levels:   # OR is only tradable after it has formed
            or_end = datetime.combine(ctx.today, SESSION_OPEN) + timedelta(minutes=self.p["range_minutes"])
            if today.index[-1] < or_end:
                lv.pop(("OR", "up"), None)
                lv.pop(("OR", "down"), None)
        n = self.p["swing_lookback"]
        if "SW" in self.levels and len(today) > n:
            window = today.iloc[-n - 1:-1]          # previous n bars, excluding the current one
            lv[("SW", "up")] = float(window["high"].max())
            lv[("SW", "down")] = float(window["low"].min())
        # an active watch keeps the level it was opened with (swing levels move)
        for key, w in ctx.state["watches"].items():
            lv[key] = w["level"]
        return lv

    @staticmethod
    def _kill(st: dict, key) -> None:
        w = st["watches"].pop(key, None)
        if w is not None:
            st["done"].add((*key, round(w["level"], 2)))

    def _rejected(self, bar, prev, level: float, side: str, beyond: bool) -> bool:
        mode = self.p["rejection_mode"]
        inside = bar["close"] < level if side == "up" else bar["close"] > level
        pattern = beyond and (is_pin_bar(bar, side, self.p["wick_ratio"]) or is_engulfing(prev, bar, side))
        return (mode in ("any", "close_inside") and inside) or (mode in ("any", "pattern") and pattern)

    def _enter(self, ctx: Context, name: str, side: str, w: dict, entry: float, a: float) -> None:
        p = self.p
        t = ctx.now.strftime("%H:%M")
        if (ctx.active_positions or ctx.broker.pending_orders() or not (p["entry_start"] <= t < p["entry_end"])
                or ctx.trades_today() >= p["max_trades_per_day"]):
            return
        if side == "up":   # fade the upside: index is expected to fall
            stop = w["extreme"] + p["sl_buffer_pts"]
            risk = stop - entry
            target = entry - p["rr"] * risk
            kind = OptionType.PE
        else:
            stop = w["extreme"] - p["sl_buffer_pts"]
            risk = entry - stop
            target = entry + p["rr"] * risk
            kind = OptionType.CE
        if risk < p["min_risk_pts"] or risk > p["max_risk_atr"] * a:
            return
        opt = ctx.select_option(kind, p["moneyness"], p["expiry"], p["min_dte"])
        if opt is None:
            return
        tag = f"fb_{name}_{side}"
        plan = {"date": ctx.today, "time": t, "level_name": name, "side": side, "level": w["level"],
                "extreme": w["extreme"], "entry": entry, "stop": stop, "target": target, "risk": risk,
                "dir": -1 if side == "up" else 1, "be_done": False}
        ctx.state["plans"][tag] = plan
        self.signals.append(dict(plan))
        ctx.buy(opt, lots=p["lots"], sl_pct=p["option_sl_pct"], target_pct=None, tag=tag)

    # --------------------------------------------------------- diagnostics
    def on_trade_closed(self, ctx: Context, trade) -> None:
        plan = ctx.state.get("plans", {}).pop(trade.tag, None)
        if plan is None:
            return
        bars = ctx.data.spot_bars(trade.exit_time, lookback=400)
        held = bars[bars.index >= pd.Timestamp(trade.entry_time)]
        d, risk = plan["dir"], plan["risk"]
        idx_exit = float(held["open"].iloc[-1]) if len(held) else float("nan")
        best = (held["high"].max() if d == 1 else held["low"].min()) if len(held) else float("nan")
        self.records.append({
            **{k: v for k, v in plan.items() if k not in ("dir", "be_done")},
            "tag": trade.tag, "option": trade.contract.symbol, "entry_time": trade.entry_time,
            "exit_time": trade.exit_time, "exit_reason": trade.exit_reason.value,
            "index_exit": idx_exit, "index_R": d * (idx_exit - plan["entry"]) / risk,
            "mfe_R": d * (best - plan["entry"]) / risk,
            "minutes_held": (pd.Timestamp(trade.exit_time) - pd.Timestamp(trade.entry_time)).total_seconds() / 60,
            "option_entry": trade.entry_price, "option_exit": trade.exit_price, "net_pnl": round(trade.net_pnl, 2),
        })

    def diagnostics(self) -> pd.DataFrame:
        return pd.DataFrame(self.records)

    def _manage_exits(self, ctx: Context) -> None:
        if not ctx.active_positions:
            return
        b = ctx.spot(lookback=1).iloc[-1]
        hi, lo = float(b["high"]), float(b["low"])
        for pos in ctx.active_positions:
            plan = ctx.state["plans"].get(pos.tag)
            if plan is None:
                continue
            d = plan["dir"]   # +1: we profit when the index rises (CE), -1: when it falls (PE)
            stop_hit = lo <= plan["stop"] if d == 1 else hi >= plan["stop"]
            tgt_hit = hi >= plan["target"] if d == 1 else lo <= plan["target"]
            if stop_hit:      # pessimistic: stop first if both in one bar
                ctx.exit(pos, ExitReason.STOP_LOSS)
            elif tgt_hit:
                ctx.exit(pos, ExitReason.TARGET)
            elif self.p["breakeven_at_r"] and not plan["be_done"]:
                best = hi if d == 1 else lo
                if d * (best - plan["entry"]) >= self.p["breakeven_at_r"] * plan["risk"]:
                    plan["stop"], plan["be_done"] = plan["entry"], True
