"""Round-2 stages after the shortlist (configs frozen in research/round2/shortlist.json).

    python research/round2/run_stage.py robust   index mode, BANKNIFTY + SENSEX, Jan 2022 - Sep 2024;
                                                 a config is dropped if its R < -0.05 on either
    python research/round2/run_stage.py test     the survivors, ONCE, Oct 2024 - Oct 2026:
                                                 index mode and 1-lot options, NIFTY + BANKNIFTY

Results go to research/round2/{robust,test}.json (one row per config x index x mode).
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

from algo.research.sweep import run_one

HERE = Path(__file__).parent
TRAIN = (date(2022, 1, 1), date(2024, 9, 30))
TEST = (date(2024, 10, 1), date(2026, 10, 31))
DROP_R = -0.05


def run_jobs(jobs: list[tuple], meta: list[dict]) -> list[dict]:
    with ProcessPoolExecutor(max_workers=7) as ex:
        rows = list(ex.map(run_one, jobs))
    return [{**m, **r} for m, r in zip(meta, rows)]


def robust() -> None:
    picks = json.loads((HERE / "shortlist.json").read_text())
    jobs, meta = [], []
    for i, p in enumerate(picks):
        for ul in ("BANKNIFTY", "SENSEX"):
            jobs.append(("index", p["strategy"], ul, *TRAIN, p["params"]))
            meta.append({"pick": i, "strategy": p["strategy"], "ul": ul, "mode": "index", "period": "train"})
    rows = run_jobs(jobs, meta)
    for i, p in enumerate(picks):
        rs = [r for r in rows if r["pick"] == i]
        p["robust"] = {r["ul"]: {"n": r.get("n"), "R": r.get("R")} for r in rs}
        p["survives"] = all((r.get("R") is not None and r["R"] >= DROP_R) for r in rs)
    (HERE / "robust.json").write_text(json.dumps(picks, indent=1))
    for i, p in enumerate(picks):
        print(f"{i} {p['strategy']:18s} NIFTY R={p['train_R']:+.3f} | " +
              " | ".join(f"{ul} n={v['n']} R={v['R']}" for ul, v in p["robust"].items()) +
              f" -> {'KEEP' if p['survives'] else 'DROP'}")


def test() -> None:
    picks = [p for p in json.loads((HERE / "robust.json").read_text()) if p["survives"]]
    jobs, meta = [], []
    for i, p in enumerate(picks):
        for ul in ("NIFTY", "BANKNIFTY"):
            for mode in ("index", "options"):
                jobs.append((mode, p["strategy"], ul, *TEST, p["params"]))
                meta.append({"pick": i, "strategy": p["strategy"], "params": p["params"], "train_R": p["train_R"],
                             "ul": ul, "mode": mode, "period": "test"})
    rows = run_jobs(jobs, meta)
    (HERE / "test.json").write_text(json.dumps(rows, indent=1, default=str))
    for r in rows:
        if r["mode"] == "index":
            print(f"{r['pick']} {r['strategy']:18s} {r['ul']:9s} index   n={r.get('n')} R={r.get('R')} "
                  f"ci={r.get('ci95')} t={r.get('t')} years={r.get('by_year')}")
        else:
            print(f"{r['pick']} {r['strategy']:18s} {r['ul']:9s} options n={r.get('n')} pf={r.get('pf')} "
                  f"net={r.get('net_pnl')} per_trade={r.get('net_per_trade')} ci={r.get('net_ci95')} "
                  f"years={r.get('by_year')} {r.get('error', '')}")


if __name__ == "__main__":
    {"robust": robust, "test": test}[sys.argv[1]]()
