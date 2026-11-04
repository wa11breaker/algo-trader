"""Performance metrics from a BacktestResult.

A "trade" here is a round trip: legs of one spread (same `group`) are combined.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from algo.backtest.engine import BacktestResult

REFERENCE_LOT = 65   # current NIFTY lot size; used for lot-normalised figures


def round_trips(result: BacktestResult) -> pd.DataFrame:
    """One row per round trip (spread legs combined)."""
    df = result.trades_df()
    if df.empty:
        return df
    df["group"] = df["group"].replace("", np.nan).fillna(pd.Series(df.index.astype(str), index=df.index))
    df["net_lot65"] = df["net_pnl"] * REFERENCE_LOT / df["lot_size"]
    g = df.groupby("group", sort=False)
    out = pd.DataFrame({
        "tag": g["tag"].first(),
        "legs": g.size(),
        "entry_time": g["entry_time"].min(),
        "exit_time": g["exit_time"].max(),
        "exit_reason": g["exit_reason"].first(),
        "lot_size": g["lot_size"].first(),
        "lots": g["lots"].first(),
        "gross_pnl": g["gross_pnl"].sum(),
        "costs": g["costs"].sum(),
        "net_pnl": g["net_pnl"].sum(),
        "net_lot65": g["net_lot65"].sum(),
    })
    return out.sort_values("exit_time").reset_index()


def daily_pnl(result: BacktestResult, col: str = "net_pnl") -> pd.Series:
    days = pd.DatetimeIndex(pd.to_datetime(result.trading_days), name="date")
    if not result.trades:
        return pd.Series(0.0, index=days, name=col)
    rt = round_trips(result)
    s = rt.groupby(pd.to_datetime(rt["exit_time"]).dt.normalize())[col].sum()
    return s.reindex(days.union(s.index), fill_value=0.0).rename(col)


def equity_curve(result: BacktestResult) -> pd.DataFrame:
    """equity = money in the trading account; total_wealth = equity + withdrawn.

    Drawdown is measured on total_wealth, so a withdrawal is not counted as a loss.
    """
    d = daily_pnl(result)
    wd = pd.Series(0.0, index=d.index)
    for w in getattr(result, "withdrawals", []) or []:
        ts = pd.Timestamp(w.on)
        if ts in wd.index:
            wd[ts] += w.amount
    withdrawn = wd.cumsum()
    wealth = result.initial_capital + d.cumsum()
    peak = wealth.cummax()
    return pd.DataFrame({"net_pnl": d, "equity": wealth - withdrawn, "withdrawn": withdrawn,
                         "total_wealth": wealth, "drawdown": wealth - peak,
                         "drawdown_pct": (wealth / peak - 1) * 100})


def mean_ci(x, n_boot: int = 2000, seed: int = 7, digits: int = 3) -> dict:
    """Mean with a bootstrap 95% CI and a t-stat. With ~100 trades the CI is usually wide:
    an 'edge' whose CI straddles 0 is not distinguishable from luck."""
    a = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(a)
    if n < 2:
        return {"n": n, "mean": round(float(a.mean()), digits) if n else None}
    rng = np.random.default_rng(seed)
    boots = rng.choice(a, size=(n_boot, n), replace=True).mean(axis=1)
    sd = a.std(ddof=1)
    return {"n": n, "mean": round(float(a.mean()), digits),
            "ci95": [round(float(np.percentile(boots, 2.5)), digits), round(float(np.percentile(boots, 97.5)), digits)],
            "t_stat": round(float(a.mean() / (sd / np.sqrt(n))), 2) if sd > 0 else None}


def by_year(rt: pd.DataFrame) -> dict:
    out = {}
    for y, g in rt.groupby(pd.to_datetime(rt["exit_time"]).dt.year):
        w, l = g.net_pnl[g.net_pnl > 0].sum(), -g.net_pnl[g.net_pnl <= 0].sum()
        out[int(y)] = {"trades": int(len(g)), "net_pnl": round(float(g.net_pnl.sum()), 2),
                       "pf": round(float(w / l), 2) if l > 0 else "inf"}
    return out


def _max_streak(flags: list[bool]) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def _max_dd(daily: pd.Series) -> float:
    eq = daily.cumsum()
    return float((eq - eq.cummax().clip(lower=0)).min()) if len(eq) else 0.0


def compute_metrics(result: BacktestResult) -> dict:
    eq = equity_curve(result)
    cap = result.initial_capital
    out: dict = {
        "strategy": result.strategy,
        "underlying": result.underlying,
        "period": f"{result.start} to {result.end}",
        "trading_days": len(result.trading_days),
        "initial_capital": cap,
        "trades": 0,
        "cancelled_orders": result.cancelled_orders,
        "risk_blocks": result.risk_blocks,
    }
    if getattr(result, "skips", None):
        out["strategy_skips"] = result.skips
    wds = getattr(result, "withdrawals", []) or []
    out.update({
        "withdrawn": round(sum(w.amount for w in wds), 2),
        "withdrawal_dates": [str(w.on) for w in wds],
        "days_to_recover_capital": (result.trading_days.index(wds[0].on) + 1) if wds else None,
        "final_trading_capital": round(float(eq["equity"].iloc[-1]), 2) if len(eq) else cap,
        "total_wealth": round(float(eq["total_wealth"].iloc[-1]), 2) if len(eq) else cap,
        "kill_switch_on": str(result.halted_on) if getattr(result, "halted_on", None) else None,
    })
    if not result.trades:
        return out
    rt = round_trips(result)
    net = rt["net_pnl"]
    wins, losses = net[net > 0], net[net <= 0]
    d = eq["net_pnl"]
    std = d.std(ddof=1)
    d65 = daily_pnl(result, "net_lot65")
    out.update({
        "trades": len(rt),
        "legs": int(rt["legs"].sum()),
        "net_pnl": round(net.sum(), 2),
        "gross_pnl": round(rt["gross_pnl"].sum(), 2),
        "total_costs": round(rt["costs"].sum(), 2),
        "return_pct": round(net.sum() / cap * 100, 2) if cap else 0.0,
        "win_rate_pct": round(len(wins) / len(rt) * 100, 2),
        "avg_win": round(wins.mean(), 2) if len(wins) else 0.0,
        "avg_loss": round(losses.mean(), 2) if len(losses) else 0.0,
        "largest_win": round(net.max(), 2),
        "largest_loss": round(net.min(), 2),
        "profit_factor": round(wins.sum() / -losses.sum(), 2) if losses.sum() < 0 else math.inf,
        "expectancy_per_trade": round(net.mean(), 2),
        "max_consecutive_losses": _max_streak((net <= 0).tolist()),
        "max_drawdown": round(eq["drawdown"].min(), 2),
        "max_drawdown_pct_of_capital": round(eq["drawdown"].min() / cap * 100, 2) if cap else 0.0,
        "worst_day": round(d.min(), 2),
        "sharpe_daily_annualized": round(d.mean() / std * np.sqrt(252), 2) if std and std > 0 else 0.0,
        "profitable_days_pct": round((d > 0).sum() / max((d != 0).sum(), 1) * 100, 2),
        f"net_pnl_lot{REFERENCE_LOT}": round(rt["net_lot65"].sum(), 2),
        f"max_drawdown_lot{REFERENCE_LOT}": round(_max_dd(d65), 2),
        f"worst_day_lot{REFERENCE_LOT}": round(d65.min(), 2),
        "net_per_trade_stats": mean_ci(net, digits=2),
        "by_year": by_year(rt),
        "exit_reasons": rt["exit_reason"].value_counts().to_dict(),
        "pnl_by_tag": {
            tag: {"trades": int(len(g)), "net_pnl": round(float(g["net_pnl"].sum()), 2),
                  "win_rate_pct": round(float((g["net_pnl"] > 0).mean() * 100), 1)}
            for tag, g in rt.groupby(rt["tag"].fillna(""))
        },
    })
    diag = getattr(result, "diagnostics", None)
    if diag is not None and len(diag) and "index_R" in diag:
        out["index_R_stats"] = mean_ci(diag["index_R"])   # option-independent signal quality
    return out


def monte_carlo(result: BacktestResult, runs: int = 1000, capital: float | None = None,
                withdraw_multiple: float = 2.0, kill_switch_pct: float = 50.0, seed: int = 7) -> dict:
    """Reshuffle the order of 1-lot (lot-65 normalised, per-lot) round-trip results.

    Each run starts from `capital`, trades 1 lot per trade, and stops when equity reaches
    withdraw_multiple x capital (success) or falls below kill_switch_pct % of it (ruin).
    Ignores compounding and position sizing on purpose: it asks "does the edge survive a
    bad ordering of the same trades?".
    """
    rt = round_trips(result)
    if rt.empty:
        return {}
    per_lot = (rt["net_lot65"] / rt["lots"].clip(lower=1)).to_numpy()
    cap = float(capital or result.initial_capital)
    goal, floor = withdraw_multiple * cap, kill_switch_pct / 100 * cap
    rng = np.random.default_rng(seed)
    hits, ruins, n_to_goal, finals = 0, 0, [], []
    trades_per_day = len(rt) / max(len(result.trading_days), 1)
    for _ in range(runs):
        eq = cap + np.cumsum(rng.permutation(per_lot))
        g = np.argmax(eq >= goal) if (eq >= goal).any() else None
        r = np.argmax(eq < floor) if (eq < floor).any() else None
        if g is not None and (r is None or g < r):
            hits += 1
            n_to_goal.append(g + 1)
        elif r is not None:
            ruins += 1
        finals.append(eq[-1])
    med = float(np.median(n_to_goal)) if n_to_goal else None
    return {
        "runs": runs, "capital": cap,
        "p_reach_withdrawal": round(hits / runs, 3),
        "p_kill_switch_first": round(ruins / runs, 3),
        "median_trades_to_withdrawal": med,
        "median_days_to_withdrawal": round(med / trades_per_day) if med and trades_per_day else None,
        "median_final_equity_1lot": round(float(np.median(finals)), 2),
        "p5_final_equity_1lot": round(float(np.percentile(finals, 5)), 2),
    }
