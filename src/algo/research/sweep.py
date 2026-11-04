"""Parameter sweeps with a train / test split (`algo sweep`).

Grid file (YAML or JSON), either
    a list of param dicts:          [{"tp_type": "rr", "risk_reward": 1.5}, ...]
    or a dict of value lists:       {"risk_reward": [1, 1.5], "sl_atr_mult": [1.5, 2]}   (cartesian product)
Base params (-p) are merged under every grid entry.

Modes
    index    IndexPlanMixin strategies only: options hidden, plans replayed on 1-min index bars
             (R per trade before option costs; works on any period with spot data, e.g. 2022+)
    options  full option backtest at 1 lot, risk rules and calendar filters off (needs option data)

Every config runs on --train; the top-N by the ranking metric then run once on --test.
"""
from __future__ import annotations

import itertools
import json
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yaml

from algo.config import get_settings

RANK = {"index": "R", "options": "net_per_trade"}


def load_grid(path: str | Path) -> list[dict]:
    text = Path(path).read_text()
    spec = yaml.safe_load(text)
    if isinstance(spec, list):
        return [dict(x) for x in spec]
    if isinstance(spec, dict):
        keys = list(spec)
        vals = [v if isinstance(v, list) else [v] for v in spec.values()]
        return [dict(zip(keys, combo)) for combo in itertools.product(*vals)]
    raise ValueError("grid must be a list of param dicts or a dict of value lists")


def parse_period(s: str) -> tuple[date, date]:
    a, _, b = s.partition(":")
    return date.fromisoformat(a), date.fromisoformat(b)


def _summarise_index(t: pd.DataFrame) -> dict:
    from algo.analytics.metrics import mean_ci
    if t.empty:
        return {"n": 0}
    st = mean_ci(t["R"])
    yearly = t.groupby(pd.to_datetime(t["time"]).dt.year)["R"].mean().round(3)
    return {"n": st["n"], "R": st["mean"], "ci95": st.get("ci95"), "t": st.get("t_stat"),
            "win": round(float((t["R"] > 0).mean()), 3),
            "long_R": round(float(t[t.dir == 1].R.mean()), 3) if (t.dir == 1).any() else None,
            "short_R": round(float(t[t.dir == -1].R.mean()), 3) if (t.dir == -1).any() else None,
            "by_year": json.dumps({int(k): float(v) for k, v in yearly.items()})}


def run_one(job: tuple) -> dict:
    """One config on one period (top-level so it can run in a worker process)."""
    mode, strategy, ul, start, end, params = job
    s = get_settings()
    row = {"params": json.dumps(params, sort_keys=True)}
    try:
        if mode == "index":
            from algo.research.index_replay import run_index_only
            trades, _ = run_index_only(strategy, params, ul, start, end, s)
            row.update(_summarise_index(trades))
        else:
            from algo.analytics.metrics import compute_metrics
            from algo.backtest.engine import run_backtest
            from algo.research.index_replay import NO_CALENDAR
            from algo.strategies import get_strategy
            strat = get_strategy(strategy, **{"size_from_capital": False, **params})
            res = run_backtest(strat, s.underlying(ul), start, end, s, use_risk=False, calendar=NO_CALENDAR)
            m = compute_metrics(res)
            row.update({"n": m.get("trades", 0), "net_pnl": m.get("net_pnl"), "pf": m.get("profit_factor"),
                        "net_per_trade": m.get("expectancy_per_trade"),
                        "net_ci95": (m.get("net_per_trade_stats") or {}).get("ci95"),
                        "index_R": (m.get("index_R_stats") or {}).get("mean"),
                        "max_dd": m.get("max_drawdown"),
                        "by_year": json.dumps(m.get("by_year", {}))})
    except Exception as e:   # one bad config must not kill the sweep
        row.update({"n": 0, "error": f"{type(e).__name__}: {e}"})
    return row


def run_sweep(strategy: str, ul: str, grid: list[dict], base: dict, train: tuple[date, date],
              test: tuple[date, date] | None, mode: str, top: int, workers: int, out_root: Path) -> Path:
    configs = [{**base, **g} for g in grid]
    out = Path(out_root) / f"{datetime.now():%Y%m%d-%H%M%S}_{strategy}_{ul}_{mode}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "grid.json").write_text(json.dumps({"base": base, "grid": grid, "train": [str(x) for x in train],
                                               "test": [str(x) for x in test] if test else None}, indent=1))
    key = RANK[mode]

    def run(period, cfgs) -> pd.DataFrame:
        jobs = [(mode, strategy, ul, period[0], period[1], c) for c in cfgs]
        with ProcessPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(run_one, jobs))
        df = pd.DataFrame(rows)
        if key in df:
            df = df.sort_values(key, ascending=False, na_position="last")
        return df

    tr = run(train, configs)
    tr.to_csv(out / "train.csv", index=False)
    if test is not None and key in tr:
        best = [json.loads(p) for p in tr.dropna(subset=[key]).head(top)["params"]]
        te = run(test, best)
        te.to_csv(out / "test.csv", index=False)
    return out
