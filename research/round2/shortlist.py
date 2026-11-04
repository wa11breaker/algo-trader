"""Pick the configs that go forward from each round-2 training sweep. Rules fixed before seeing any result:

    n >= 80 trades on the train period (NIFTY, index mode, Jan 2022 - Sep 2024)
    mean R >= +0.15 (option costs take ~0.1R per trade)
    mean R > 0 in each of 2022, 2023 and 2024
    plateau: >= 75% of the one-step neighbours (configs that differ in exactly one parameter; the
             exit style counts as one parameter) have mean R > 0
    at most 2 per strategy, best mean R first

Usage: python research/round2/shortlist.py  -> prints the table, writes research/round2/shortlist.json
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
STRATS = ["trend_pullback", "squeeze_breakout", "pdhl_retest", "vwap_structure", "orb_momentum"]
EXIT_KEYS = ("target_r", "trail_start_r", "trail_atr_mult")
MIN_N, MIN_R, PLATEAU, PER_STRAT = 80, 0.15, 0.75, 2


def latest_train(strategy: str) -> pd.DataFrame:
    runs = sorted((ROOT / "reports" / "sweeps").glob(f"*_{strategy}_NIFTY_index"))
    runs = [r for r in runs if (r / "train.csv").exists()
            and json.loads((r / "grid.json").read_text())["train"][0] == "2022-01-01"]
    if not runs:
        raise FileNotFoundError(f"no training sweep for {strategy}")
    df = pd.read_csv(runs[-1] / "train.csv")
    df["sweep"] = runs[-1].name
    return df


def key(params: dict) -> tuple[dict, str]:
    """(non-exit params, exit-style label)"""
    rest = {k: v for k, v in params.items() if k not in EXIT_KEYS}
    ex = json.dumps({k: params.get(k) for k in EXIT_KEYS}, sort_keys=True)
    return rest, ex


def neighbours(df: pd.DataFrame, i: int) -> pd.DataFrame:
    rest_i, ex_i = key(df.at[i, "p"])
    rows = []
    for j in df.index:
        if j == i:
            continue
        rest_j, ex_j = key(df.at[j, "p"])
        diff = sum(rest_i.get(k) != rest_j.get(k) for k in set(rest_i) | set(rest_j)) + (ex_i != ex_j)
        if diff == 1:
            rows.append(j)
    return df.loc[rows]


def evaluate(strategy: str) -> pd.DataFrame:
    df = latest_train(strategy).reset_index(drop=True)
    df["p"] = df["params"].map(json.loads)
    years = df["by_year"].fillna("{}").map(json.loads)
    df["years_pos"] = years.map(lambda y: all(y.get(str(k), -1) > 0 for k in (2022, 2023, 2024)))
    df["plateau"] = [float((neighbours(df, i)["R"] > 0).mean()) if df.at[i, "n"] else 0.0 for i in df.index]
    df["pass"] = (df["n"] >= MIN_N) & (df["R"] >= MIN_R) & df["years_pos"] & (df["plateau"] >= PLATEAU)
    df["strategy"] = strategy
    return df


def main() -> None:
    pd.set_option("display.width", 250, "display.max_colwidth", 110)
    picks, summary = [], []
    for s in STRATS:
        df = evaluate(s)
        best = df.sort_values("R", ascending=False)
        summary.append({"strategy": s, "configs": len(df), "best_R": best["R"].iloc[0], "best_n": best["n"].iloc[0],
                        "median_R": round(df["R"].median(), 3), "share_R>0": round((df["R"] > 0).mean(), 2),
                        "pass": int(df["pass"].sum())})
        print(f"\n== {s}: top 5 by R")
        print(best.head(5)[["n", "R", "ci95", "t", "win", "by_year", "plateau", "pass", "params"]].to_string(index=False))
        for _, r in df[df["pass"]].sort_values("R", ascending=False).head(PER_STRAT).iterrows():
            picks.append({"strategy": s, "params": r["p"], "train_R": r["R"], "train_n": int(r["n"]),
                          "train_ci95": r["ci95"], "by_year": r["by_year"]})
    print("\n== summary\n" + pd.DataFrame(summary).to_string(index=False))
    print(f"\n== shortlisted: {len(picks)}")
    for p in picks:
        print(f"  {p['strategy']:18s} R={p['train_R']:+.3f} n={p['train_n']} {json.dumps(p['params'])}")
    (Path(__file__).parent / "shortlist.json").write_text(json.dumps(picks, indent=1))


if __name__ == "__main__":
    main()
