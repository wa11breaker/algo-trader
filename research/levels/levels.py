"""Support / resistance and supply / demand for one recorded day (NIFTY spot points).

    .venv/bin/python research/levels/levels.py [--day 2026-11-04]

Inputs: research/fvg/spot_<day>.parquet (Fyers 1-min index incl. prior days) and
data/footprint/NIFTY/<day>.csv (futures ticks with buy/sell volume).

  Reference  previous day H/L/C, the day's O/H/L/C, floor pivots for the NEXT day.
  Swings     confirmed 5-min swing highs/lows (3 bars each side) of the last 2 sessions, clustered
             within CLUSTER pts; strength = how many 5-min bars later touched the level (+/- TOUCH pts).
  Volume     futures volume by price (5-pt bins), shifted to spot by the median futures-spot basis:
             POC, 70% value area, high-volume nodes (acceptance = S/R), low-volume nodes (fast moves).
  Zones      demand / supply: a 1-3 bar 5-min base (small bodies) followed by an impulse of
             >= IMPULSE_ATR x ATR(14, 5-min). Zone = the base's range; "fresh" = not revisited later.
             The impulse's futures delta shows whether aggressive orders drove it.
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from algo.indicators import atr, resample, swing_points

ROOT = Path(__file__).resolve().parents[2]
CLUSTER, TOUCH, BIN, IMPULSE_ATR = 10.0, 5.0, 5.0, 1.5


def load(day: date):
    spot = pd.read_parquet(ROOT / "research/fvg" / f"spot_{day}.parquet")
    ticks = pd.read_csv(ROOT / "data/footprint/NIFTY" / f"{day}.csv", parse_dates=["ts"]).set_index("ts")
    return spot, ticks


def reference(spot: pd.DataFrame, day: date) -> dict:
    d = spot.groupby(spot.index.date).agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"))
    today, prev = d.loc[day], d[d.index < day].iloc[-1]
    p = (today.h + today.l + today.c) / 3
    return {"PDH": prev.h, "PDL": prev.l, "PDC": prev.c, "Open": today.o, "High": today.h, "Low": today.l,
            "Close": today.c, "Pivot": p, "R1": 2 * p - today.l, "S1": 2 * p - today.h,
            "R2": p + today.h - today.l, "S2": p - (today.h - today.l)}


def swings(spot: pd.DataFrame, day: date) -> pd.DataFrame:
    days = sorted(set(spot.index.date))
    m5 = resample(spot[spot.index.date >= days[days.index(day) - 1]], "5min").drop(columns="bar_end", errors="ignore")
    sw = swing_points(m5, 3, 3)
    pts = [(v, "high") for v in sw.swing_high.dropna()] + [(v, "low") for v in sw.swing_low.dropna()]
    pts.sort()
    clusters: list[list] = []
    for v, kind in pts:
        if clusters and v - clusters[-1][-1][0] <= CLUSTER:
            clusters[-1].append((v, kind))
        else:
            clusters.append([(v, kind)])
    rows = []
    for c in clusters:
        lvl = float(np.mean([v for v, _ in c]))
        touches = int(((m5.high >= lvl - TOUCH) & (m5.low <= lvl + TOUCH)).sum())
        rows.append({"level": round(lvl, 1), "pivots": len(c), "kinds": "/".join(sorted({k for _, k in c})),
                     "touches_5m": touches})
    return pd.DataFrame(rows)


def volume_profile(spot: pd.DataFrame, ticks: pd.DataFrame, day: date) -> tuple[pd.DataFrame, dict]:
    fut = ticks.ltp.resample("1min").last().dropna()
    sp = spot[spot.index.date == day].close
    basis = float((fut - sp.reindex(fut.index)).dropna().median())
    px = ((ticks.ltp - basis) / BIN).round() * BIN
    prof = pd.DataFrame({"vol": ticks.vol.groupby(px).sum(), "delta": (ticks.buy - ticks.sell).groupby(px).sum()})
    prof = prof[prof.vol > 0].sort_index()
    poc = float(prof.vol.idxmax())
    # value area: grow from the POC towards the bigger neighbour until 70% of volume
    order, total = prof.vol, prof.vol.sum()
    lo = hi = list(prof.index).index(poc)
    acc = order.iloc[lo]
    while acc < 0.7 * total and (lo > 0 or hi < len(order) - 1):
        up = order.iloc[hi + 1] if hi < len(order) - 1 else -1
        dn = order.iloc[lo - 1] if lo > 0 else -1
        if up >= dn:
            hi += 1; acc += up
        else:
            lo -= 1; acc += dn
    smooth = prof.vol.rolling(3, center=True, min_periods=1).mean()
    hvn = smooth[(smooth.shift(1) < smooth) & (smooth.shift(-1) <= smooth) & (smooth > smooth.median())]
    lvn = smooth[(smooth.shift(1) > smooth) & (smooth.shift(-1) >= smooth) & (smooth < smooth.median())]
    return prof, {"basis": basis, "POC": poc, "VAL": float(prof.index[lo]), "VAH": float(prof.index[hi]),
                  "HVN": list(hvn.sort_values(ascending=False).index[:6]), "LVN": list(lvn.index)}


def zones(spot: pd.DataFrame, ticks: pd.DataFrame, day: date, basis: float) -> pd.DataFrame:
    m5 = resample(spot[spot.index.date <= day], "5min").drop(columns="bar_end", errors="ignore")
    a = atr(m5, 14)
    m5 = m5[m5.index.date == day]
    a = a.reindex(m5.index)
    delta = (ticks.buy - ticks.sell).resample("5min").sum()
    rows, i = [], 0
    body = (m5.close - m5.open).abs()
    rng = (m5.high - m5.low).replace(0, np.nan)
    small = (body <= 0.5 * rng) | (rng <= 0.5 * a)
    while i < len(m5) - 1:
        if not small.iloc[i]:
            i += 1; continue
        j = i
        while j + 1 < len(m5) and small.iloc[j + 1] and j - i < 2:
            j += 1
        base = m5.iloc[i:j + 1]
        nxt = m5.iloc[j + 1:j + 4]
        if nxt.empty:
            break
        top, bot, A = base.high.max(), base.low.min(), a.iloc[j]
        up, dn = nxt.high.max() - top, bot - nxt.low.min()
        kind = "demand" if up >= IMPULSE_ATR * A and up > dn else "supply" if dn >= IMPULSE_ATR * A else None
        if kind:
            later = m5[m5.index > nxt.index[-1]]
            hits = later[(later.low <= top) & (later.high >= bot)]
            held = (later.close >= bot).all() if kind == "demand" else (later.close <= top).all()
            d = float(delta.reindex(nxt.index).sum())
            rows.append({"kind": kind, "from": base.index[0].strftime("%H:%M"), "zone_lo": round(bot, 1),
                         "zone_hi": round(top, 1), "impulse_pts": round(up if kind == "demand" else dn, 1),
                         "impulse_atr": round((up if kind == "demand" else dn) / A, 1), "impulse_delta": int(d),
                         "retests": len(hits), "first_retest": hits.index[0].strftime("%H:%M") if len(hits) else "-",
                         "status": "fresh" if hits.empty else ("held" if held else "broken")})
            i = j + 1
        else:
            i += 1
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-11-04")
    a = ap.parse_args()
    day = date.fromisoformat(a.day)
    spot, ticks = load(day)
    ref = reference(spot, day)
    print("== Reference levels"); print({k: round(v, 1) for k, v in ref.items()})
    prof, vp = volume_profile(spot, ticks, day)
    print(f"\n== Volume profile (futures shifted by basis {vp['basis']:.1f})")
    print({k: v for k, v in vp.items() if k != "basis"})
    print(prof.assign(vol_k=(prof.vol / 1000).round(0)).drop(columns="vol").to_string())
    print("\n== Swing clusters (5-min, last 2 sessions)"); print(swings(spot, day).to_string(index=False))
    print("\n== Supply / demand zones (5-min)"); print(zones(spot, ticks, day, vp["basis"]).to_string(index=False))


if __name__ == "__main__":
    main()
