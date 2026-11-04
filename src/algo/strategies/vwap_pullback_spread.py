"""VWAP pullback in the direction of the intraday trend, traded as a debit spread.

Trend (5-min, today):  close above session VWAP + opening-range high broken today + EMA20 rising
                       (mirror for downtrend).
Setup:                 a pullback bar touches VWAP or EMA20 (within touch_pts) but closes on the
                       trend side of VWAP.
Trigger:               the next bar closes back in the trend direction beyond the pullback bar
                       (green and above its high for longs).
Instrument:            instrument="spread": buy ATM, sell 1 strike OTM (CE for up, PE for down)
                       instrument="option": single ATM option (for comparison)
Exits:                 spread value >= target_frac x width  |  value <= debit x (1 - stop_pct)  |
                       5-min close back through VWAP by fail_pts  |  exit_time.
VWAP needs volume: futures data must be downloaded (index has no volume of its own).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd

from algo.core.context import Context
from algo.core.models import ExitReason, OptionType
from algo.core.strategy import Strategy
from algo.indicators import ema, vwap


class VwapPullbackSpread(Strategy):
    """Trend-day VWAP/EMA pullback, entered with a debit spread (or a single option)."""

    name = "vwap_pullback_spread"
    defaults = {
        "tf": "5min",
        "range_minutes": 15,
        "ema_period": 20,
        "ema_slope_bars": 3,       # EMA must be higher (lower) than N bars ago
        "touch_pts": 10.0,         # pullback low within this distance of VWAP/EMA counts as a touch
        "fail_pts": 10.0,          # 5-min close this far through VWAP against us -> exit
        "instrument": "spread",    # spread | option
        "width_steps": 1,          # spread width in strike steps (1 = 50 pts on NIFTY)
        "expiry": "nearest",
        "min_dte": 1,              # never hold expiring options
        "lots": 1,
        "target_frac": 0.75,       # spread target: value reaches 75% of the width
        "stop_pct": 40.0,          # stop: lose 40% of the debit / premium
        "opt_target_pct": 50.0,    # option mode target: +50% premium
        "max_trades_per_day": 2,
        "entry_start": "09:45",
        "entry_end": "13:30",
        "exit_time": "15:00",
        "fill_timeout_min": 5,     # close a half-filled spread after this long
    }

    def __init__(self, **params):
        super().__init__(**params)
        if self.p["instrument"] not in ("spread", "option"):
            raise ValueError("instrument must be 'spread' or 'option'")
        self.records: list[dict] = []
        self._meta: dict[str, dict] = {}
        self._agg: dict[str, dict] = {}

    # ---------------------------------------------------------------- day
    def on_day_start(self, ctx: Context) -> None:
        ctx.state.update(or_hi=None, or_lo=None, broke_up=False, broke_dn=False,
                         pull={}, open=None, fail=False, trades=0, last_bar=None)

    # ---------------------------------------------------------------- bar
    def on_bar(self, ctx: Context) -> None:
        self._manage(ctx)
        p, st = self.p, ctx.state
        bars = ctx.spot(p["tf"], lookback=200)
        if bars.empty or bars.index[-1] == st["last_bar"] or bars.index[-1].date() != ctx.today:
            return
        st["last_bar"] = bars.index[-1]
        today = bars[bars.index.normalize() == pd.Timestamp(ctx.today)]
        n_or = max(1, p["range_minutes"] // 5)
        if len(today) <= n_or:
            return
        if st["or_hi"] is None:
            st["or_hi"], st["or_lo"] = float(today["high"].iloc[:n_or].max()), float(today["low"].iloc[:n_or].min())

        bar = today.iloc[-1]
        vw = float(vwap(today).iloc[-1])
        e = ema(bars["close"], p["ema_period"])
        e_now = float(e.iloc[-1])
        slope = e_now - float(e.iloc[-1 - p["ema_slope_bars"]])
        close = float(bar["close"])
        st["broke_up"] |= close > st["or_hi"]
        st["broke_dn"] |= close < st["or_lo"]
        trend = {"up": close > vw and st["broke_up"] and slope > 0,
                 "down": close < vw and st["broke_dn"] and slope < 0}

        # exit signal: trade's thesis (price on the right side of VWAP) failed
        o = st["open"]
        if o and ((o["dir"] == "up" and close < vw - p["fail_pts"]) or
                  (o["dir"] == "down" and close > vw + p["fail_pts"])):
            st["fail"] = True

        for side in ("up", "down"):
            sign = 1 if side == "up" else -1
            pull = st["pull"].get(side)
            triggered = (pull is not None and trend[side]
                         and sign * (close - float(bar["open"])) > 0
                         and (close > pull["high"] if side == "up" else close < pull["low"]))
            if triggered:
                st["pull"].pop(side)
                self._try_enter(ctx, side, close, vw)
                continue
            ref = max(vw, e_now) if side == "up" else min(vw, e_now)
            touched = (float(bar["low"]) <= ref + p["touch_pts"]) if side == "up" \
                else (float(bar["high"]) >= ref - p["touch_pts"])
            holds = close > vw if side == "up" else close < vw
            if trend[side] and touched and holds:
                st["pull"][side] = {"high": float(bar["high"]), "low": float(bar["low"]), "ts": bars.index[-1]}
            else:
                st["pull"].pop(side, None)

    # ---------------------------------------------------------- entries
    def _try_enter(self, ctx: Context, side: str, index_px: float, vw: float) -> None:
        p, st = self.p, ctx.state
        t = ctx.now.strftime("%H:%M")
        if st["open"] or st["trades"] >= p["max_trades_per_day"] or not (p["entry_start"] <= t < p["entry_end"]):
            return
        kind = OptionType.CE if side == "up" else OptionType.PE
        tag = f"vwp_{side}"
        meta = {"date": ctx.today, "time": t, "side": side, "index_entry": index_px, "vwap": vw}
        if p["instrument"] == "spread":
            sp = ctx.open_debit_spread(kind, p["width_steps"], "ATM", p["expiry"], p["min_dte"], p["lots"],
                                       tag=tag, stop_frac=p["stop_pct"] / 100)
            if sp is None:
                return
            st["open"] = {"kind": "spread", "spread": sp, "dir": side, "t0": ctx.now}
            self._meta[sp.group] = {**meta, "est_debit": sp.est_debit, "width": sp.width, "legs": 2,
                                    "long": sp.long.symbol, "short": sp.short.symbol}
        else:
            c = ctx.select_option(kind, "ATM", p["expiry"], p["min_dte"])
            ltp = ctx.ltp(c) if c else None
            if not ltp:
                return
            order = ctx.buy(c, p["lots"], tag=tag, max_loss=ltp * p["lots"] * c.lot_size * p["stop_pct"] / 100)
            if order is None:
                return
            st["open"] = {"kind": "option", "group": str(order.id), "contract": c, "dir": side, "t0": ctx.now}
            self._meta[str(order.id)] = {**meta, "est_premium": ltp, "legs": 1, "long": c.symbol}
        st["trades"] += 1
        st["fail"] = False

    # ------------------------------------------------------------ exits
    def _manage(self, ctx: Context) -> None:
        p, st = self.p, ctx.state
        o = st.get("open")
        if not o:
            return
        late = ctx.now.strftime("%H:%M") >= p["exit_time"]
        if o["kind"] == "spread":
            sp = o["spread"]
            lp, sh = ctx.spread_legs(sp)
            if sp.closing:
                if lp is None and sh is None and not ctx.spread_pending(sp):
                    st["open"] = None
                return
            if lp is None or sh is None:            # still filling (or one leg failed)
                waited = ctx.now - o["t0"] > timedelta(minutes=p["fill_timeout_min"])
                if not ctx.spread_pending(sp) or waited:
                    ctx.close_spread(sp, ExitReason.SIGNAL)
                return
            entry = lp.entry_price - sh.entry_price
            value = ctx.spread_value(sp)
            if value is None:
                return
            reason = None
            if value <= entry * (1 - p["stop_pct"] / 100):
                reason = ExitReason.STOP_LOSS
            elif value >= p["target_frac"] * sp.width:
                reason = ExitReason.TARGET
            elif st["fail"]:
                reason = ExitReason.SIGNAL
            elif late:
                reason = ExitReason.SQUARE_OFF
            if reason:
                ctx.close_spread(sp, reason)
        else:
            pos = next((x for x in ctx.positions if x.group == o["group"]), None)
            if pos is None:
                if not any(x.group == o["group"] for x in ctx.broker.pending_orders()):
                    st["open"] = None
                return
            if ctx.broker.exit_requested(pos):
                return
            ltp = ctx.ltp(pos.contract)
            if ltp is None:
                return
            r = ltp / pos.entry_price - 1
            reason = (ExitReason.STOP_LOSS if r <= -p["stop_pct"] / 100 else
                      ExitReason.TARGET if r >= p["opt_target_pct"] / 100 else
                      ExitReason.SIGNAL if st["fail"] else
                      ExitReason.SQUARE_OFF if late else None)
            if reason:
                ctx.exit(pos, reason)

    # ------------------------------------------------------ diagnostics
    def on_trade_closed(self, ctx: Context, trade) -> None:
        meta = self._meta.get(trade.group)
        if meta is None:
            return
        a = self._agg.setdefault(trade.group, {"net": 0.0, "gross": 0.0, "costs": 0.0, "legs": 0,
                                                "entry": 0.0, "exit": 0.0})
        sign = 1 if trade.side.value == "BUY" else -1
        a["net"] += trade.net_pnl
        a["gross"] += trade.gross_pnl
        a["costs"] += trade.costs
        a["entry"] += sign * trade.entry_price
        a["exit"] += sign * trade.exit_price
        a["legs"] += 1
        a["reason"], a["exit_time"], a["entry_time"] = trade.exit_reason.value, trade.exit_time, trade.entry_time
        a["lot_size"] = trade.contract.lot_size
        if a["legs"] >= meta["legs"]:
            self._meta.pop(trade.group)
            self._agg.pop(trade.group)
            self.records.append({
                **meta, "group": trade.group, "entry_time": a["entry_time"], "exit_time": a["exit_time"],
                "exit_reason": a["reason"], "entry_value": round(a["entry"], 2), "exit_value": round(a["exit"], 2),
                "minutes_held": (pd.Timestamp(a["exit_time"]) - pd.Timestamp(a["entry_time"])).total_seconds() / 60,
                "lot_size": a["lot_size"], "gross_pnl": round(a["gross"], 2), "costs": round(a["costs"], 2),
                "net_pnl": round(a["net"], 2), "net_lot65": round(a["net"] * 65 / a["lot_size"], 2),
            })

    def diagnostics(self) -> pd.DataFrame:
        return pd.DataFrame(self.records)
