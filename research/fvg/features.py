"""Do extra indicators separate fvg_flow's winners from its losers? Feature study on the plan log.

    .venv/bin/python research/fvg/features.py CACHE_DIR            # bucket tables
    .venv/bin/python research/fvg/features.py CACHE_DIR --filter 'vwap_dist>0' --filter 'ema15>0'

Plans come from opt_session_plans.dump (current fvg_flow defaults; train/val have no order flow,
flowdev_proxy uses the close-location proxy, recorded days use real footprint delta). Each plan is
replayed on its own (index R, 2-pt cost) for the bucket tables; --filter drops plans, then replays
them with the one-position-at-a-time rule like the live bot.

Features are computed at the signal bar (ctx.now = start of the last CLOSED 1-min bar) from closed
bars only, directional (positive = agrees with the trade). Session average = typical-price TWAP: the
history index has no volume, so the live volume-weighted VWAP would not match the backtest.
"""
from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import opt_session_plans as osp  # noqa: E402
from algo.indicators import adx, atr, bollinger, ema, resample, rsi, supertrend, vwap  # noqa: E402
from algo.research.index_replay import replay  # noqa: E402
from algo.strategies.index_plan import hm  # noqa: E402

COST = 2.0
SETS = ["train", "val", "flowdev_proxy", "2026-11-04", "2026-11-05", "2026-11-06"]
DAYS = SETS[3:]


def load(cache: Path, name: str) -> dict:
    f = cache / f"{name}.pkl"
    if not f.exists():
        osp.dump(name, cache)
    return pickle.load(open(f, "rb"))


def avail(df: pd.DataFrame, cols) -> pd.DataFrame:
    """Re-index N-min indicator rows by the 1-min bar start at which they are closed (bar_end - 1 min)."""
    out = df[cols].copy()
    out.index = df["bar_end"] - pd.Timedelta(minutes=1)
    return out


def features(spot: pd.DataFrame, plans: list[dict]) -> pd.DataFrame:
    spot = spot.assign(volume=0.0)                        # TWAP everywhere (see docstring)
    m1 = spot.assign(ema50=ema(spot.close, 50), atr1=atr(spot, 14), twap=vwap(spot))
    b5 = resample(spot, "5min")
    b5 = b5.assign(adx5=adx(b5, 14), rsi5=rsi(b5.close, 14), st5=supertrend(b5, 10, 3.0).st_dir,
                   atr5=atr(b5, 14))
    bb = bollinger(b5.close, 20, 2.0)
    b5["pctb"] = (b5.close - bb.lower) / (bb.upper - bb.lower)
    b15 = resample(spot, "15min")
    e15 = ema(b15.close, 20)
    b15["ema15_slope"] = e15 - e15.shift(2)
    daily = spot.groupby(spot.index.normalize()).agg(open=("open", "first"), high=("high", "max"),
                                                     low=("low", "min"), close=("close", "last"))
    datr = atr(daily, 14).shift(1)                        # yesterday's daily ATR: known at the open
    prev_close = daily.close.shift(1)
    f5, f15 = avail(b5, ["adx5", "rsi5", "st5", "pctb", "atr5"]), avail(b15, ["ema15_slope"])
    rows = []
    for pl in plans:
        now, d = pd.Timestamp(pl["now"]), pl["dir"]
        day = now.normalize()
        if now not in m1.index or day not in daily.index:
            continue
        b = m1.loc[now]
        i5, i15 = f5.index.searchsorted(now, "right") - 1, f15.index.searchsorted(now, "right") - 1
        x5 = f5.iloc[i5] if i5 >= 0 else None
        x15 = f15.iloc[i15] if i15 >= 0 else None
        today = spot.loc[day:now]
        orng = today[today.index < day + pd.Timedelta(hours=9, minutes=30)]
        da = datr.get(day, np.nan)
        rows.append({
            "now": now, "dir": d,
            "vwap_dist": d * (b.close - b.twap) / (x5.atr5 if x5 is not None else np.nan),
            "ema15": np.sign(d * x15.ema15_slope) if x15 is not None else np.nan,
            "st5": d * x5.st5 if x5 is not None else np.nan,
            "adx5": x5.adx5 if x5 is not None else np.nan,
            "rsi5": (x5.rsi5 if d == 1 else 100 - x5.rsi5) if x5 is not None else np.nan,
            "pctb": (x5.pctb if d == 1 else 1 - x5.pctb) if x5 is not None else np.nan,
            "ema50_dist": d * (b.close - b.ema50) / b.atr1 if b.atr1 else np.nan,
            "day_move": d * (b.close - daily.open[day]) / da,
            "gap": d * (daily.open[day] - prev_close.get(day, np.nan)) / da,
            "abs_gap_pct": abs(daily.open[day] / prev_close.get(day, np.nan) - 1) * 100,
            "or_pos": d * ((b.close - orng.low.min()) / max(orng.high.max() - orng.low.min(), 1e-9) - 0.5)
            if len(orng) else np.nan,
            "hour": now.hour + now.minute / 60,
        })
    return pd.DataFrame(rows)


def outcomes(spot, plans, p) -> pd.DataFrame:
    """Each plan replayed on its own (no one-position rule): the signal's own result."""
    ex, be, tr = hm(p["exit_time"]), p.get("breakeven_at_r"), p.get("trail_start_r")
    by_day = {d: g for d, g in spot.groupby(spot.index.normalize())}
    out = []
    for pl in plans:
        day = by_day.get(pd.Timestamp(pl["now"]).normalize())
        t = replay([pl], day if day is not None else spot.iloc[0:0], ex, be, tr)
        out.append(t.iloc[0].R - COST / pl["risk"] if len(t) else np.nan)
    return pd.Series(out)


def table(frames: dict, cuts: dict) -> None:
    for f, edges in cuts.items():
        print(f"\n### {f}   (netR per signal; n)")
        rows = {}
        for name, df in frames.items():
            b = pd.cut(df[f], edges, include_lowest=True)
            g = df.groupby(b, observed=False).netR
            rows[name] = [f"{m:+.3f} ({n})" if n else "-" for m, n in zip(g.mean(), g.count())] if name not in DAYS \
                else [f"{s:+.1f}R ({n})" if n else "-" for s, n in zip(g.sum(), g.count())]
            labels = [str(i) for i in g.mean().index]
        print(pd.DataFrame(rows, index=labels).to_string())


def stats(t: pd.DataFrame) -> str:
    if t.empty:
        return "0 trades"
    net = t.R - COST / t.risk
    return f"{len(t):5d} trades  netR {net.mean():+.3f}  sum {net.sum():+8.1f}R  win {(t.R > 0).mean():.0%}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cache", type=Path)
    ap.add_argument("--filter", action="append", default=[], help="pandas query on the features, e.g. 'vwap_dist>0'")
    a = ap.parse_args()
    a.cache.mkdir(parents=True, exist_ok=True)
    data, frames = {}, {}
    for name in SETS:
        dd = load(a.cache, name)
        fe = a.cache / f"{name}.features.pkl"
        if fe.exists():
            df = pd.read_pickle(fe)
        else:
            df = features(dd["spot"], dd["plans"])
            df["netR"] = outcomes(dd["spot"], [pl for pl in dd["plans"] if pd.Timestamp(pl["now"]) in set(df.now)],
                                  dd["p"]).values
            df.to_pickle(fe)
        data[name], frames[name] = dd, df
        print(f"{name}: {len(df)} signals, netR {df.netR.mean():+.3f}", flush=True)
    if not a.filter:
        q = frames["train"].quantile([1 / 3, 2 / 3])
        cuts = {f: [-np.inf, *q[f].round(2).tolist(), np.inf] for f in
                ["vwap_dist", "adx5", "rsi5", "pctb", "ema50_dist", "day_move", "or_pos", "hour"]}
        cuts |= {"ema15": [-1.5, -0.5, 0.5, 1.5], "st5": [-1.5, 0, 1.5], "gap": [-np.inf, -0.3, 0.3, np.inf],
                 "abs_gap_pct": [-np.inf, 0.2, 0.4, np.inf]}
        table(frames, cuts)
        return
    query = " and ".join(f"({f})" for f in a.filter)
    print(f"\nfilter: {query}")
    for name in SETS:
        dd, df = data[name], frames[name]
        keep = set(df.query(query).now)
        p = dd["p"]
        for label, plans in [("all", dd["plans"]), ("kept", [pl for pl in dd["plans"] if pd.Timestamp(pl["now"]) in keep])]:
            t = replay(plans, dd["spot"], hm(p["exit_time"]), p.get("breakeven_at_r"), p.get("trail_start_r"))
            print(f"  {name:14s} {label:4s} {stats(t)}")


if __name__ == "__main__":
    main()
