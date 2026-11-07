"""Evaluate sizing variants of fvg_flow (opt_size_strat.FvgSize).

    .venv/bin/python research/fvg/opt_size_eval.py 2d   NAME k=v ...      # two recorded days
    .venv/bin/python research/fvg/opt_size_eval.py hist NAME start end k=v ...  # local store, flow_proxy

Appends one summary line to research/fvg/results/opt-size-runs.jsonl and the per-trade rows to
research/fvg/results/opt-size-<NAME>-<mode>.csv.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "new"))
from opt_size_strat import FvgSize  # noqa: E402

CAP = 50_000


def parse(kv):
    out = {}
    for a in kv:
        k, v = a.split("=", 1)
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            pass
        out[k] = v
    return out


def summarize(df: pd.DataFrame, name: str, mode: str) -> dict:
    if df.empty:
        return {"name": name, "mode": mode, "n": 0}
    df = df.sort_values("entry_time")
    eq = CAP + df.net_pnl.cumsum()
    dd = (eq.cummax() - eq).max()
    loss_pct = -df.net_pnl / df.equity * 100
    ratio = -df.net_pnl / df.planned
    losers = df[df.net_pnl < 0]
    return {
        "name": name, "mode": mode, "n": len(df), "net": round(df.net_pnl.sum()),
        "worst": round(df.net_pnl.min()), "worst_pct_eq": round(loss_pct.max(), 2),
        "max_dd": round(dd), "over6": round((loss_pct > 6).mean() * 100, 2),
        "over6_n": int((loss_pct > 6).sum()), "over8_n": int((loss_pct > 8).sum()),
        "loss_ratio_p50": round(ratio[df.net_pnl < 0].median(), 2) if len(losers) else None,
        "loss_ratio_p90": round(ratio[df.net_pnl < 0].quantile(0.9), 2) if len(losers) else None,
        "loss_ratio_max": round(ratio.max(), 2), "avg_lots": round(df.lots.mean(), 2),
        "sum_ret_pct": round((df.net_pnl / df.equity * 100).sum(), 1),
        "win": round((df.net_pnl > 0).mean() * 100, 1), "final_eq": round(eq.iloc[-1]),
    }


def main():
    mode, name = sys.argv[1], sys.argv[2]
    t0 = time.time()
    if mode == "2d":
        from run2d import run2d
        params = parse(sys.argv[3:])
        s = FvgSize(**params)
        run2d(s, capital=CAP)
    else:
        from algo.backtest.engine import run_backtest
        from algo.config import get_settings
        start, end = date.fromisoformat(sys.argv[3]), date.fromisoformat(sys.argv[4])
        params = {"flow_proxy": True, **parse(sys.argv[5:])}
        if params.pop("fixed", True):      # size every trade off the fixed starting capital
            from algo.core.capital import CapitalManager
            CapitalManager.equity = property(lambda self: self.initial)
        s = FvgSize(**params)
        st = get_settings()
        st.raw.setdefault("capital", {})["kill_switch_pct"] = None     # trade the whole period
        run_backtest(s, st.underlying("NIFTY"), start, end, st, capital=CAP, withdraw=False)
    df = s.diagnostics()
    out = HERE / "results" / f"opt-size-{name}-{mode}.csv"
    df.to_csv(out, index=False)
    r = {**summarize(df, name, mode), "params": params, "secs": round(time.time() - t0)}
    with open(HERE / "results" / "opt-size-runs.jsonl", "a") as f:
        f.write(json.dumps(r, default=str) + "\n")
    print(json.dumps(r, default=str))


if __name__ == "__main__":
    main()
