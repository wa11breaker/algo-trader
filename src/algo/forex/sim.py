"""Bar-signal / minute-execution simulator for one spot instrument quoted in USD.

- Signals come from closed higher-timeframe bars; entries fill at the next bar's
  first-minute open. Stops/targets are resolved on 1-minute bars.
- Prices in the data are bid. Ask = bid + spread. Longs buy at ask and sell at bid;
  shorts sell at bid and buy back at ask (their stop/target trigger on the ask).
- If the stop and target are both touched inside one minute, the stop is assumed
  first (conservative). A minute that opens beyond a level fills at that open.
- One position at a time. Swap is charged per night held at the server-day rollover
  (17:00 New York), Monday-Friday nights only, triple on Wednesday night.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from algo.forex.data import to_server
from algo.forex.engulfing import GoldEngulfingParams


@dataclass
class CostParams:
    spread: float = 0.30            # USD per oz (ask - bid)
    commission_per_lot: float = 5.0  # USD round trip per lot
    swap_long: float = -20.0        # USD per lot per night (negative = paid)
    swap_short: float = 5.0
    contract_size: float = 100.0    # oz per lot
    lot_step: float = 0.01

    @classmethod
    def zero(cls) -> "CostParams":
        return cls(spread=0.0, commission_per_lot=0.0, swap_long=0.0, swap_short=0.0)


@dataclass
class SimResult:
    trades: pd.DataFrame
    capital: float
    max_dd_open: float           # USD, mark-to-market incl. open trade (worst intrabar)
    skips: Counter = field(default_factory=Counter)


def swap_nights(entry_srv: pd.Timestamp, exit_srv: pd.Timestamp) -> int:
    """Swap-charged nights between two server times (Wed night counts 3, Sat/Sun 0)."""
    n = 0
    d = entry_srv.normalize() + timedelta(days=1)
    end = exit_srv.normalize()
    while d <= end:
        wd = (d - timedelta(days=1)).weekday()  # the night being rolled
        n += 3 if wd == 2 else (1 if wd < 5 else 0)
        d += timedelta(days=1)
    return n


def _srv(ts) -> pd.Timestamp:
    return to_server(pd.DatetimeIndex([ts]))[0]


def simulate(m1: pd.DataFrame, h4: pd.DataFrame, sig: pd.DataFrame, p: GoldEngulfingParams,
             costs: CostParams, capital: float = 100_000.0, trade_from=None) -> SimResult:
    mo, mh, ml, mc = (m1[c].to_numpy() for c in ("open", "high", "low", "close"))
    utc = m1.index
    sp, cs = costs.spread, costs.contract_size
    i0, i1 = h4["i0"].to_numpy(), h4["i1"].to_numpy()
    side, stop_lvl, atr_v = sig["side"].to_numpy(), sig["stop"].to_numpy(), sig["atr"].to_numpy()
    first_ok = 0 if trade_from is None else int(np.searchsorted(h4["first_utc"].to_numpy(),
                                                                np.datetime64(pd.Timestamp(trade_from)), "left"))
    risk_usd = capital * p.risk_pct / 100.0
    skips: Counter = Counter()
    trades: list[dict] = []
    closed_eq, peak, max_dd = capital, capital, 0.0
    pos: dict | None = None

    def unreal(prices, d):  # exit-side PnL per position, before costs
        return d * ((prices + (sp if d < 0 else 0.0)) - pos["entry"]) * pos["lots"] * cs

    def close(k, price, reason):
        nonlocal closed_eq, peak, pos
        d, lots = pos["side"], pos["lots"]
        exit_utc = utc[k]
        nights = swap_nights(pos["entry_srv"], _srv(exit_utc))
        gross = d * (price - pos["entry"]) * lots * cs
        comm = costs.commission_per_lot * lots
        swap = (costs.swap_long if d > 0 else costs.swap_short) * lots * nights
        net = gross - comm + swap
        trades.append({
            **{k_: v for k_, v in pos.items() if k_ not in ("k", "bar")},
            "exit_utc": exit_utc, "exit_srv": _srv(exit_utc), "exit": price, "reason": reason,
            "gross": gross, "commission": comm, "swap": swap, "nights": nights, "net": net,
            "R": net / risk_usd,
        })
        closed_eq += net
        peak = max(peak, closed_eq)
        pos = None

    def mark(a, b):
        """Mark-to-market drawdown bookkeeping over minutes [a, b) of the open position."""
        nonlocal peak, max_dd
        if a >= b:
            return
        d = pos["side"]
        worst = unreal((ml if d > 0 else mh)[a:b], d)
        eq_close = closed_eq + unreal(mc[a:b], d)
        prev_peak = np.maximum(np.maximum.accumulate(np.concatenate(([peak], eq_close[:-1]))), peak)
        max_dd = max(max_dd, float((prev_peak - (closed_eq + worst)).max()))
        w = int(np.argmin(worst))
        if worst[w] < pos["worst_unreal"]:
            pos["worst_unreal"], pos["worst_utc"] = float(worst[w]), utc[a + w]
        peak = max(peak, float(eq_close.max()))

    def scan(s, e):
        """Walk minutes [s, e) of the open position; close it if a level is hit."""
        while s < e:
            d, sl, tp = pos["side"], pos["stop"], pos["target"]
            h, l = mh[s:e], ml[s:e]
            if d > 0:
                hit_sl, hit_tp = l <= sl, h >= tp
            else:
                hit_sl, hit_tp = h + sp >= sl, l + sp <= tp
            hit = hit_sl | hit_tp
            stop_at = int(np.argmax(hit)) if hit.any() else e - s
            if p.breakeven_r > 0 and not pos["be"]:
                lvl = pos["entry"] + d * p.breakeven_r * pos["risk_pts"]
                fav = (h >= lvl) if d > 0 else (l + sp <= lvl)
                be_at = int(np.argmax(fav)) if fav.any() else e - s
                if be_at < stop_at:  # stop moves to entry from the next minute on
                    mark(s, s + be_at + 1)
                    pos["stop"], pos["be"] = pos["entry"], True
                    s = s + be_at + 1
                    continue
            mark(s, s + stop_at)
            if hit.any():
                _exit(s + stop_at, hit_sl[stop_at], hit_tp[stop_at])
            return

    def _exit(k, sl_hit, tp_hit):
        nonlocal max_dd
        d, sl, tp = pos["side"], pos["stop"], pos["target"]
        op = mo[k] + (sp if d < 0 else 0.0)
        stop_name = "breakeven" if pos["be"] else "stop"
        if sl_hit and tp_hit:
            gap_tp = op >= tp if d > 0 else op <= tp
            price, reason = (op, "target") if gap_tp else (op if (op < sl if d > 0 else op > sl) else sl, stop_name)
        elif sl_hit:
            price, reason = (min(sl, op) if d > 0 else max(sl, op)), stop_name
        else:
            price, reason = (max(tp, op) if d > 0 else min(tp, op)), "target"
        exit_pnl = d * (price - pos["entry"]) * pos["lots"] * cs
        if exit_pnl < pos["worst_unreal"]:
            pos["worst_unreal"], pos["worst_utc"] = exit_pnl, utc[k]
        max_dd = max(max_dd, peak - (closed_eq + exit_pnl))
        close(k, price, reason)

    n = len(h4)
    for j in range(n):
        if pos is not None and p.max_bars > 0 and j - pos["bar"] >= p.max_bars:
            k0 = i0[j]
            close(k0, mo[k0] + (sp if pos["side"] < 0 else 0.0), "time")
        if pos is not None:
            scan(max(i0[j], pos["k"]), i1[j])
        if j == n - 1 or j < first_ok - 1:
            continue
        s = side[j]
        if s == 0:
            continue
        k = i0[j + 1]
        if pos is not None:
            if pos["side"] == -s and p.exit_on_opposite:
                close(k, mo[k] + (sp if pos["side"] < 0 else 0.0), "opposite")
            else:
                skips["in_position"] += 1
                continue
        entry = mo[k] + (sp if s > 0 else 0.0)
        stop = stop_lvl[j]
        chart_dist = abs(mo[k] - stop)
        risk = s * (entry - stop)
        if risk <= 0:
            skips["stop_beyond_entry"] += 1
            continue
        if not np.isnan(atr_v[j]) and chart_dist < p.min_stop_atr * atr_v[j]:
            skips["stop_too_close"] += 1
            continue
        if p.max_stop_atr > 0 and not np.isnan(atr_v[j]) and chart_dist > p.max_stop_atr * atr_v[j]:
            skips["stop_too_wide"] += 1
            continue
        lots = np.floor(risk_usd / (risk * cs) / costs.lot_step + 1e-9) * costs.lot_step
        if lots < costs.lot_step:
            skips["below_min_lot"] += 1
            continue
        pos = {
            "side": int(s), "signal_srv": h4.index[j], "entry_utc": utc[k], "entry_srv": _srv(utc[k]),
            "entry": float(entry), "stop": float(stop), "target": float(entry + s * p.rr * risk),
            "risk_pts": float(risk), "lots": round(float(lots), 2), "k": int(k), "bar": j + 1, "be": False,
            "worst_unreal": 0.0, "worst_utc": utc[k],
        }
    if pos is not None:
        k = len(mo) - 1
        close(k, mc[k] + (sp if pos["side"] < 0 else 0.0), "end_of_data")
    return SimResult(pd.DataFrame(trades), capital, max_dd, skips)
