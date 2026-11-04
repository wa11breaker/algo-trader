"""Performance summary and FTMO 2-Step challenge replay for forex SimResults."""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from algo.forex.sim import SimResult


def _r(x, n=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), n)


def summarize(res: SimResult) -> dict:
    t, cap = res.trades, res.capital
    out: dict = {"initial_capital": cap, "trades": len(t), "skips": dict(res.skips)}
    if t.empty:
        return out
    net = t["net"]
    wins, losses = net[net > 0], net[net <= 0]
    eq = cap + net.cumsum()
    dd_closed = float((np.maximum.accumulate(np.concatenate(([cap], eq))) [1:] - eq).max())
    day = t["exit_srv"].dt.normalize()
    daily = net.groupby(day).sum()
    month = net.groupby(t["exit_srv"].dt.to_period("M")).sum()
    yr = t.groupby(t["exit_srv"].dt.year)
    out.update({
        "period": f"{t['entry_utc'].min():%Y-%m-%d} to {t['exit_utc'].max():%Y-%m-%d}",
        "net_usd": _r(net.sum()),
        "return_pct": _r(net.sum() / cap * 100),
        "final_balance": _r(cap + net.sum()),
        "win_rate_pct": _r(len(wins) / len(t) * 100, 1),
        "avg_win": _r(wins.mean()) if len(wins) else None,
        "avg_loss": _r(losses.mean()) if len(losses) else None,
        "win_loss_ratio": _r(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else None,
        "profit_factor": _r(wins.sum() / -losses.sum()) if losses.sum() < 0 else None,
        "avg_R": _r(t["R"].mean(), 3),
        "max_dd_closed_usd": _r(dd_closed),
        "max_dd_closed_pct": _r(dd_closed / cap * 100),
        "max_dd_open_usd": _r(res.max_dd_open),
        "max_dd_open_pct": _r(res.max_dd_open / cap * 100),
        "worst_day_usd": _r(daily.min()),
        "worst_day_pct": _r(daily.min() / cap * 100),
        "months_with_trades": int(len(month)),
        "months_profitable": int((month > 0).sum()),
        "longs": int((t["side"] > 0).sum()),
        "shorts": int((t["side"] < 0).sum()),
        "net_long": _r(net[t["side"] > 0].sum()),
        "net_short": _r(net[t["side"] < 0].sum()),
        "costs": {"commission": _r(t["commission"].sum()), "swap": _r(t["swap"].sum())},
        "exit_reasons": t["reason"].value_counts().to_dict(),
        "avg_hold_hours": _r((t["exit_utc"] - t["entry_utc"]).dt.total_seconds().mean() / 3600, 1),
        "by_year": {int(y): {"trades": len(g), "net": _r(g["net"].sum()),
                             "win_pct": _r((g["net"] > 0).mean() * 100, 1)} for y, g in yr},
    })
    return out


def monthly(res: SimResult) -> pd.DataFrame:
    t = res.trades
    if t.empty:
        return pd.DataFrame()
    g = t.groupby(t["exit_srv"].dt.to_period("M"))
    return pd.DataFrame({"trades": g.size(), "net": g["net"].sum(),
                         "wins": g["net"].apply(lambda s: int((s > 0).sum()))})


def _run_step(t: pd.DataFrame, start: pd.Timestamp, target: float, max_loss: float,
              daily_loss: float, min_days: int, deadline: pd.Timestamp):
    """Replay one challenge step from server time `start`. Returns (status, end_srv)."""
    sub = t[t["entry_srv"] >= start]
    bal, last_close_day, day_start_bal = 0.0, None, 0.0
    days: set = set()
    for r in sub.itertuples():
        if r.entry_srv > deadline:
            break
        days.add(r.entry_srv.normalize())
        wday = r.worst_utc_srv.normalize()
        dsb = day_start_bal if wday == last_close_day else bal
        eq = bal + r.worst_pct
        if eq <= -max_loss or eq <= dsb - daily_loss:
            return ("fail_daily" if eq > -max_loss else "fail_max"), r.worst_utc_srv
        eday = r.exit_srv.normalize()
        if eday != last_close_day:
            day_start_bal, last_close_day = bal, eday
        bal += r.net_pct
        days.add(eday)
        if bal >= target and len(days) >= min_days:
            return "pass", r.exit_srv
    if not sub.empty and sub["exit_srv"].max() >= deadline:
        return "unfinished", None
    return "censored", None


def ftmo_2step(res: SimResult, start, end, step1=10.0, step2=5.0, max_loss=10.0,
               daily_loss=5.0, min_days=4, horizon_days=365) -> dict:
    """Start a challenge on every weekday in [start, end]; Step 2 starts the next day
    after Step 1 passes. 'unfinished' = not passed or failed within `horizon_days`."""
    t = res.trades.copy()
    if t.empty:
        return {}
    from algo.forex.data import to_server
    t["worst_utc_srv"] = to_server(pd.DatetimeIndex(t["worst_utc"]))
    t["worst_pct"] = t["worst_unreal"] / res.capital * 100
    t["net_pct"] = t["net"] / res.capital * 100
    results, days_to_pass = {"pass": 0, "fail_daily": 0, "fail_max": 0, "unfinished": 0}, []
    for d in pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end)):
        deadline = d + timedelta(days=horizon_days)
        s1, e1 = _run_step(t, d, step1, max_loss, daily_loss, min_days, deadline)
        if s1 == "censored":
            continue
        if s1 != "pass":
            results[s1] += 1
            continue
        s2, e2 = _run_step(t, e1.normalize() + timedelta(days=1), step2, max_loss, daily_loss, min_days, deadline)
        if s2 == "censored":
            continue
        results[s2] += 1
        if s2 == "pass":
            days_to_pass.append((e2 - d).days)
    total = sum(results.values())
    return {
        "challenges": total,
        **{f"{k}_pct": _r(v / total * 100, 1) if total else None for k, v in results.items()},
        "median_days_to_pass": _r(np.median(days_to_pass), 0) if days_to_pass else None,
    }
