"""Grid of session rules on train / val / Nov 4 / Nov 5 (exact replay, rules see only past trades).
    .venv/bin/python research/fvg/opt_session_grid.py DUMPDIR OUT.csv"""
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import opt_session_rules as R  # noqa: E402

DUMP = Path(sys.argv[1])
NAMES = ["train", "val", "2026-11-04", "2026-11-05"]


def gap_rule(lo, hi=99):
    def mk(z):
        sp = z["spot"]
        d = sp.groupby(sp.index.normalize()).agg(o=("open", "first"), c=("close", "last"))
        g = ((d.o / d.c.shift(1) - 1).abs() * 100).to_dict()
        return lambda pl, now, td: lo <= g.get(now.normalize(), 0) < hi
    return mk


def weekday_skip(wd):
    return lambda z: (lambda pl, now, td: now.dayofweek != wd)


RULES = {"baseline": None}
for m in [3, 5, 10, 15, 30, 60, 999]:
    RULES[f"no_rev_{m}m"] = R.no_reversal(m)
    RULES[f"no_rev_after_loss_{m}m"] = R.no_reversal(m, after_loss_only=True)
for m in [2, 5, 10, 15, 30]:
    RULES[f"cooldown_loss_{m}m"] = R.cooldown_after_loss(m)
for k in [1, 2, 3, 4, 5]:
    RULES[f"stop_{k}_consec_losses"] = R.stop_after_consec_losses(k)
    RULES[f"stop_{k}_consec_nonwins"] = R.stop_after_consec_losses(k, lambda r: r["R"] <= 0)
for x in [1, 2, 3, 4, 6]:
    RULES[f"day_stop_-{x}R"] = R.stop_after_lossR(x)
for y in [1, 2, 3, 4, 6]:
    RULES[f"lock_+{y}R"] = R.lock_profitR(y)
for k in [2, 3, 4, 5, 6, 8]:
    RULES[f"max_{k}_trades"] = R.max_trades(k)
for a, b in [("11:30", "13:00"), ("12:00", "13:00"), ("11:00", "13:30"), ("09:30", "10:00"),
             ("14:00", "15:00"), ("14:30", "15:00"), ("10:30", "11:00")]:
    RULES[f"skip_{a}-{b}"] = R.skip_window(a, b)
for wd in range(5):
    RULES[f"skip_wd{wd}"] = weekday_skip(wd)
for lo in [0.25, 0.4, 0.6]:
    RULES[f"only_gap>{lo}%"] = gap_rule(lo)
RULES["skip_gap<0.25%"] = gap_rule(0.25)  # same as only_gap>0.25 (kept for naming clarity)
del RULES["skip_gap<0.25%"]
RULES["no_rev_after_loss_10m+cooldown_5m"] = R.all_of(R.no_reversal(10, True), R.cooldown_after_loss(5))


def job(args):
    name, rule = args
    z = R.load(DUMP, name)
    nd = len([d for d in z["by_day"] if len(z["by_day"][d]) > 300]) if name in ("train", "val") else 1
    out = {}
    for rn in rule:
        f = RULES[rn]
        if f is not None and f.__code__.co_varnames[:1] == ("z",):
            f = f(z)
        s = R.stats(R.replay(z["plans"], z["by_day"], z["exit"], z["be"], f), nd)
        out[rn] = s
    return name, out


if __name__ == "__main__":
    names = list(RULES)
    jobs = [(n, names) for n in NAMES]
    rows = {}
    with ProcessPoolExecutor(2) as ex:
        for name, out in ex.map(job, jobs):
            for rn, s in out.items():
                rows.setdefault(rn, {"rule": rn})
                for k, v in s.items():
                    rows[rn][f"{name[-5:] if name[0] == '2' else name}_{k}"] = v
    df = pd.DataFrame(rows.values())
    df.to_csv(sys.argv[2], index=False)
    cols = ["rule"] + [f"{p}_{k}" for p in ["train", "val"] for k in ["netR", "sumR", "per_day", "t"]] + \
        [f"{p}_sumR" for p in ["11-04", "11-05"]] + ["11-04_n", "11-05_n"]
    with pd.option_context("display.width", 250):
        print(df[cols].to_string(index=False))
