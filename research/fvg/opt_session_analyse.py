"""Baseline trade list of fvg_flow (tuned defaults) broken down by hour, trade # of day, weekday,
previous trade outcome, opening gap. .venv/bin/python research/fvg/opt_session_analyse.py DUMPDIR"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from opt_session_rules import load, replay, stats  # noqa: E402

dump = Path(sys.argv[1])
for name in ["2026-11-04", "2026-11-05", "train", "val"]:
    z = load(dump, name)
    t = replay(z["plans"], z["by_day"], z["exit"], z["be"])
    nd = len([d for d in z["by_day"] if d.year >= 2022 and len(z["by_day"][d]) > 300]) if name in ("train", "val") else 1
    print(f"\n===== {name} baseline", stats(t, nd))
    t.to_pickle(dump / f"trades_{name}.pkl")
    if name not in ("train", "val"):
        print(t.round(2).to_string()); continue
    t["hh"] = t.time.dt.strftime("%H") + ":" + (t.time.dt.minute // 30 * 30).astype(str).str.zfill(2)
    t["k"] = t.groupby(t.time.dt.date).cumcount().clip(upper=9) + 1
    t["wd"] = t.time.dt.dayofweek
    prev = t.groupby(t.time.dt.date)
    t["prevR"] = prev.R.shift(1)
    t["prev"] = pd.cut(t.prevR, [-99, -0.01, 0.01, 99], labels=["loss", "be", "win"]).astype(str)
    t["rev"] = (t.dir != prev.dir.shift(1)).where(t.prevR.notna())
    t["gapmin"] = ((t.time - prev.exit_ts.shift(1)).dt.total_seconds() / 60).clip(upper=60)
    t["cumbefore"] = prev.net.cumsum() - t.net
    sp = z["spot"]; dly = sp.groupby(sp.index.normalize()).agg(o=("open", "first"), c=("close", "last"))
    gap = (dly.o / dly.c.shift(1) - 1) * 100
    t["gap"] = pd.cut(t.time.dt.normalize().map(gap).abs(), [-1, 0.25, 0.6, 99], labels=["<.25%", ".25-.6%", ">.6%"]).astype(str)
    agg = lambda g: g.agg(n=("net", "size"), netR=("net", "mean"), sumR=("net", "sum"), win=("R", lambda x: (x > 0).mean())).round(3)
    for col in ["hh", "k", "wd", "prev", "rev", "gap", "dir"]:
        print(f"\n-- {name} by {col}"); print(agg(t.groupby(col)).T.to_string())
    print(f"\n-- {name} by prev outcome x reversal"); print(agg(t.groupby(["prev", "rev"])).to_string())
    print(f"\n-- {name} by minutes since prev exit (prev loss)"); print(agg(t[t.prev == "loss"].groupby(pd.cut(t.gapmin, [-1, 2, 5, 10, 20, 61]))).to_string())
    print(f"\n-- {name} by cum net R before trade"); print(agg(t.groupby(pd.cut(t.cumbefore, [-99, -3, -2, -1, 0, 1, 2, 3, 99]))).to_string())
    print(f"\n-- {name} exits"); print(t.why.value_counts().to_string())
