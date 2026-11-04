"""Shared evaluator for the fvg_flow rework (index-only replay; see the plan in research/fvg/README.md).

    .venv/bin/python research/fvg/eval.py --grid '{"min_gap_pts":[6,10]}' --base '{"target_r":3}' \
        --period train --workers 2 --out research/fvg/results/a-r1-grid1.csv

Metric: net R per trade = R - COST_PTS / risk (risk in index points). COST_PTS = 2 approximates option
charges + slippage on the 2026-11-04 paper fills (~Rs 0.8 per unit round trip / ~0.4 option delta).
The holdout (2025-07-01 onward) is locked: periods touching it need --holdout (final phase only).
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from algo.config import get_settings
from algo.research.index_replay import run_index_only

COST_PTS = 2.0
HOLDOUT_START = date(2025, 7, 1)
PERIODS = {
    "train": (date(2022, 1, 1), date(2023, 12, 31)),
    "val": (date(2024, 1, 1), date(2025, 6, 30)),
    "dev": (date(2022, 1, 1), date(2025, 6, 30)),
    "flowdev": (date(2024, 10, 1), date(2025, 6, 30)),      # futures (for flow_proxy) start 2024-09
    "holdout": (HOLDOUT_START, date(2026, 10, 30)),
}


def parse_period(s: str, allow_holdout: bool) -> tuple[date, date]:
    start, end = PERIODS[s] if s in PERIODS else map(date.fromisoformat, s.split(":"))
    if end >= HOLDOUT_START and not allow_holdout:
        sys.exit(f"period {s} reaches the locked holdout ({HOLDOUT_START}+); end it by 2025-06-30")
    return start, end


def stats(t: pd.DataFrame, cost: float = COST_PTS) -> dict:
    if t.empty:
        return {"n": 0}
    net = t.R - cost / t.risk
    yr = net.groupby(t.time.dt.year).mean()
    out = {"n": len(t), "per_day": round(len(t) / t.time.dt.normalize().nunique(), 2),
           "R": round(t.R.mean(), 4), "netR": round(net.mean(), 4),
           "t": round(net.mean() / (net.std(ddof=1) / np.sqrt(len(net))), 2) if len(net) > 1 else 0.0,
           "win": round((t.R > 0).mean(), 3), "risk_med": round(t.risk.median(), 1),
           "long": round(net[t.dir == 1].mean(), 4), "short": round(net[t.dir == -1].mean(), 4),
           "worst_year": round(yr.min(), 4), "pos_years": f"{int((yr > 0).sum())}/{len(yr)}"}
    for c in (1, 3, 4):
        out[f"netR_c{c}"] = round((t.R - c / t.risk).mean(), 4)
    out["by_year"] = json.dumps(yr.round(3).to_dict())
    return out


def evaluate(params: dict, start: date, end: date) -> tuple[dict, pd.DataFrame]:
    t, strat = run_index_only("fvg_flow", params, "NIFTY", start, end, get_settings())
    s = stats(t)
    s["skips"] = json.dumps(dict(strat.skips))
    return s, t


def _job(args):
    params, start, end = args
    try:
        s, _ = evaluate(params, start, end)
    except Exception as e:  # keep the grid going; the row shows the error
        s = {"n": 0, "error": repr(e)}
    return {"params": json.dumps(params, sort_keys=True), **s}


def load_grid(spec: str) -> list[dict]:
    g = yaml.safe_load(Path(spec).read_text() if Path(spec).exists() else spec)
    if isinstance(g, dict):
        vals = [v if isinstance(v, list) else [v] for v in g.values()]
        return [dict(zip(g, c)) for c in itertools.product(*vals)]
    return [dict(x) for x in g]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", default="[{}]", help="JSON/YAML (inline or file): dict of lists or list of dicts")
    ap.add_argument("--base", default="{}", help="JSON params merged under every grid entry")
    ap.add_argument("--period", default="train", help=f"{list(PERIODS)} or FROM:TO")
    ap.add_argument("--holdout", action="store_true", help="allow the locked holdout (final phase only)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", help="CSV path for all rows")
    ap.add_argument("--sort", default="netR")
    a = ap.parse_args()
    start, end = parse_period(a.period, a.holdout)
    base = json.loads(a.base)
    combos = [{**base, **g} for g in load_grid(a.grid)]
    print(f"{len(combos)} configs, {start}..{end}", flush=True)
    with ProcessPoolExecutor(a.workers) as ex:
        rows = list(ex.map(_job, [(c, start, end) for c in combos]))
    df = pd.DataFrame(rows)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(a.out, index=False)
    cols = [c for c in ["params", "n", "per_day", "R", "netR", "t", "win", "risk_med", "long", "short",
                        "worst_year", "pos_years", "netR_c1", "netR_c3", "error"] if c in df]
    with pd.option_context("display.width", 250, "display.max_colwidth", 140):
        print(df.sort_values(a.sort, ascending=False)[cols].head(25).to_string(index=False))


if __name__ == "__main__":
    main()
