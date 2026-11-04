"""Base class for index-plan option buyers that decide on completed N-min spot bars.

Subclasses implement `signal(ctx, bars, today)` and call `self.enter(...)`. The base class handles
  * bar de-duplication (one call per newly completed `tf` bar of today),
  * daily context in ctx.state: prev_high / prev_low / prev_close (previous session) and datr (daily ATR),
  * entry gating: flat (no open position / pending order), entry window, max plans per day, risk limits,
  * an ATR trailing distance for the IndexPlanMixin trail.

Distances (stop buffer, risk limits) are in multiples of the `tf` ATR, so one config means the same
thing on NIFTY, BANKNIFTY and SENSEX. Every exit lives in the index plan (stop / target / breakeven /
trail / time), so the index-only replay used by `algo sweep --mode index` is exact.

`max_trades_per_day` counts plans proposed today, not filled trades, so the cap behaves the same in an
option backtest and in the index-only replay (where options are hidden and nothing fills).
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from algo.core.context import Context
from algo.core.strategy import Strategy
from algo.indicators import atr
from algo.strategies.index_plan import IndexPlanMixin, hm

COMMON = {
    "tf": "5min",
    "atr_period": 14,
    "atr_days": 14,
    "sl_buffer_atr": 0.15,      # stop this many ATRs beyond the structural level
    "min_risk_atr": 0.3,
    "max_risk_atr": 3.0,
    "target_r": 2.0,            # None = no fixed target
    "breakeven_at_r": None,
    "trail_start_r": None,      # None = no trail
    "trail_atr_mult": 1.5,
    "option_sl_pct": 35.0,      # premium safety net
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


class BarSignalStrategy(IndexPlanMixin, Strategy):
    index_replay_exact = True
    lookback = 200                 # tf bars fetched per call (multi-day, so indicators are warm at 09:15)
    choices: dict[str, tuple] = {}
    tag_prefix = "sig"

    def __init__(self, **params):
        super().__init__(**params)
        for k, allowed in self.choices.items():
            if self.p[k] not in allowed:
                raise ValueError(f"{self.name}: {k} must be one of {' | '.join(map(str, allowed))}")
        self.skips: Counter = Counter()
        self.records: list[dict] = []

    # ------------------------------------------------------------------ day
    def on_day_start(self, ctx: Context) -> None:
        st = ctx.state
        st.update(plans={}, last_bar=None, n_plans=0, prev_high=None, prev_low=None, prev_close=None, datr=None)
        hist = ctx.spot(lookback=(self.p["atr_days"] + 2) * 380)
        hist = hist[hist.index.normalize() < pd.Timestamp(ctx.today)]
        if not hist.empty:
            daily = hist.groupby(hist.index.normalize()).agg(high=("high", "max"), low=("low", "min"),
                                                             close=("close", "last"))
            pc = daily["close"].shift()
            tr = pd.concat([daily["high"] - daily["low"], (daily["high"] - pc).abs(), (daily["low"] - pc).abs()],
                           axis=1).max(axis=1)
            last = daily.iloc[-1]
            st.update(prev_high=float(last["high"]), prev_low=float(last["low"]), prev_close=float(last["close"]),
                      datr=float(tr.tail(self.p["atr_days"]).mean()))
        self.day_start(ctx)

    def day_start(self, ctx: Context) -> None:
        """Hook: reset per-day strategy state."""

    # ------------------------------------------------------------------ bar
    def on_bar(self, ctx: Context) -> None:
        st = ctx.state
        self._manage_minute_exits(ctx)
        bars = ctx.spot(self.p["tf"], lookback=self.lookback)
        if len(bars) < 3 or bars.index[-1] == st["last_bar"] or bars.index[-1].date() != ctx.today:
            return
        st["last_bar"] = bars.index[-1]
        today = bars[bars.index.normalize() == pd.Timestamp(ctx.today)]
        self.signal(ctx, bars, today)

    def signal(self, ctx: Context, bars: pd.DataFrame, today: pd.DataFrame) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------ helpers
    def bar_atr(self, bars: pd.DataFrame) -> float:
        return float(atr(bars, self.p["atr_period"]).iloc[-1])

    def _volume_ok(self, bars: pd.DataFrame) -> bool:
        """Current bar volume >= vol_mult x the average of the previous vol_lookback bars.

        Passes when vol_mult is 0/None or there is no volume (index data before Oct 2024)."""
        p = self.p
        if not p.get("vol_mult") or "volume" not in bars or not (bars["volume"] > 0).any():
            return True
        avg = float(bars["volume"].iloc[-p["vol_lookback"] - 1:-1].mean())
        return avg > 0 and float(bars["volume"].iloc[-1]) >= p["vol_mult"] * avg

    @staticmethod
    def flat(ctx: Context) -> bool:
        return not ctx.active_positions and not ctx.broker.pending_orders()

    def enter(self, ctx: Context, d: int, entry: float, level: float, a: float, extra: dict | None = None) -> bool:
        """Propose a trade: buy CE (d=+1) / PE (d=-1), stop = `level` -/+ sl_buffer_atr x ATR."""
        p, st = self.p, ctx.state
        if not self.flat(ctx):
            self.skips["in_position"] += 1
            return False
        t = ctx.now.time()
        if not (hm(p["entry_start"]) <= t < hm(p["entry_end"])):
            self.skips["outside_window"] += 1
            return False
        if st["n_plans"] >= p["max_trades_per_day"]:
            self.skips["max_trades"] += 1
            return False
        stop = level - d * p["sl_buffer_atr"] * a
        risk = d * (entry - stop)
        if not (p["min_risk_atr"] * a <= risk <= p["max_risk_atr"] * a):
            self.skips["risk_size"] += 1
            return False
        st["n_plans"] += 1
        side = "up" if d == 1 else "down"
        trail = p["trail_atr_mult"] * a if p["trail_start_r"] is not None else None
        return self._buy_with_plan(ctx, d, entry, stop, f"{self.tag_prefix}_{side}", trail_dist=trail,
                                   extra={"side": side, "atr": a, **(extra or {})})
