"""Session / trade-management rules for fvg_flow, replayed exactly on dumped plans
(research/fvg/opt_session_plans.py). A rule sees only the plan and the day's earlier (closed) trades,
so skipping a trade correctly frees the slot for a later plan (no look-ahead).

    .venv/bin/python research/fvg/opt_session_rules.py DUMPDIR
"""
from __future__ import annotations

import pickle
import sys
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

from algo.strategies.index_plan import hm

COST = 2.0


def replay(plans, by_day, exit_time, be_r, allow=None):
    rows, busy_until, cur_day, today = [], None, None, []
    for pl in plans:
        now = pd.Timestamp(pl["now"])
        if now.normalize() != cur_day:
            cur_day, today = now.normalize(), []
        if busy_until is not None and now < busy_until:
            continue
        d, entry, risk = pl["dir"], pl["entry"], pl["risk"]
        if not risk or risk <= 0:
            continue
        if allow is not None and not allow(pl, now, today):
            continue
        stop, target, best, be_done = pl["stop"], pl["target"], entry, False
        day = by_day.get(cur_day)
        day = day[day.index > now] if day is not None else None
        exit_px, why, exit_ts = None, "eod", now
        if day is not None:
            for ts, b in zip(day.index, day.itertuples(index=False)):
                exit_ts = ts
                if ts.time() >= exit_time:
                    exit_px, why = float(b.open), "time"; break
                if (d == 1 and b.low <= stop) or (d == -1 and b.high >= stop):
                    exit_px, why = stop, "breakeven" if be_done else "stop"; break
                if target is not None and ((d == 1 and b.high >= target) or (d == -1 and b.low <= target)):
                    exit_px, why = target, "target"; break
                best = max(best, b.high) if d == 1 else min(best, b.low)
                if be_r and not be_done and d * (best - entry) >= be_r * risk:
                    stop = max(stop, entry) if d == 1 else min(stop, entry); be_done = True
        if exit_px is None:
            exit_px = float(day["close"].iloc[-1]) if day is not None and len(day) else entry
        busy_until = exit_ts
        R = d * (exit_px - entry) / risk
        r = {"time": now, "exit_ts": exit_ts, "dir": d, "risk": risk, "why": why, "R": R, "net": R - COST / risk}
        rows.append(r); today.append(r)
    return pd.DataFrame(rows)


def load(dump: Path, name: str):
    z = pickle.load(open(dump / f"{name}.pkl", "rb"))
    sp = z["spot"]
    return {"plans": z["plans"], "by_day": {d: g for d, g in sp.groupby(sp.index.normalize())},
            "exit": hm(z["p"]["exit_time"]), "be": z["p"]["breakeven_at_r"], "spot": sp}


def stats(t: pd.DataFrame, ndays: int) -> dict:
    if t.empty:
        return {"n": 0, "netR": 0, "sumR": 0, "t": 0, "per_day": 0}
    n = t.net
    dd = (n.cumsum().cummax() - n.cumsum()).max()
    return {"n": len(t), "per_day": round(len(t) / ndays, 2), "netR": round(n.mean(), 4), "sumR": round(n.sum(), 1),
            "t": round(n.mean() / (n.std(ddof=1) / np.sqrt(len(n))), 2) if len(n) > 1 else 0, "maxDD": round(dd, 1),
            "win": round((t.R > 0).mean(), 3)}


# ------------------------------------------------------------------ rules (plan, now, today) -> bool
def skip_window(a, b):
    a, b = hm(a), hm(b)
    return lambda pl, now, td: not (a <= now.time() < b)


def max_trades(k):
    return lambda pl, now, td: len(td) < k


def stop_after_consec_losses(k, loss_if=lambda r: r["R"] < 0):
    def f(pl, now, td):
        c = 0
        for r in reversed(td):
            if loss_if(r): c += 1
            else: break
        return c < k
    return f


def stop_after_lossR(x):
    return lambda pl, now, td: sum(r["net"] for r in td) > -x


def lock_profitR(y):
    return lambda pl, now, td: sum(r["net"] for r in td) < y


def cooldown_after_loss(m):
    return lambda pl, now, td: not td or td[-1]["R"] >= 0 or now >= td[-1]["exit_ts"] + pd.Timedelta(minutes=m)


def no_reversal(m, after_loss_only=False):
    def f(pl, now, td):
        if not td or td[-1]["dir"] == pl["dir"]:
            return True
        if after_loss_only and td[-1]["R"] >= 0:
            return True
        return now >= td[-1]["exit_ts"] + pd.Timedelta(minutes=m)
    return f


def all_of(*fs):
    return lambda pl, now, td: all(f(pl, now, td) for f in fs)
