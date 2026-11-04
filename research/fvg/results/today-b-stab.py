import sys, json, copy
sys.path.insert(0, "/Users/akshayasok/development/projects/algo-trader/research/fvg")
from datetime import date
import pandas as pd
import today
from algo.research.index_replay import replay
from algo.strategies.index_plan import hm
DAY = date(2026, 11, 4); DATA = today.make_data(DAY)

def run(params, slip=0.0):
    t, strat = today.evaluate(params, DAY, DATA)
    if slip:
        p = strat.p; pl = copy.deepcopy(strat.plan_log)
        for x in pl:
            x["entry"] += x["dir"] * slip; x["risk"] = x["dir"] * (x["entry"] - x["stop"])
        t = replay(pl, DATA.spot_1m, hm(p["exit_time"]), p.get("breakeven_at_r"), p.get("trail_start_r"))
    return t

def net(t, c): return (t.R - c / t.risk)

def row(params):
    t = run(params); n2 = net(t, 2)
    ts = run(params, slip=1.0)
    return {"n": len(t), "c2": round(n2.sum(), 2), "c3": round(net(t, 3).sum(), 2), "c4": round(net(t, 4).sum(), 2),
            "slip1": round(net(ts, 2).sum(), 2), "ex_best": round(n2.sum() - n2.max(), 2),
            "pts_net": round((t.dir * (t.exit - t.entry)).sum() - 2 * len(t), 1),
            "med_risk": round(t.risk.median(), 1)}

cands = json.loads(open(sys.argv[1]).read())
steps = {"sl_atr": 0.1, "sl_buffer_pts": 1, "tp_atr": 0.25, "target_r": 0.25, "tp_max_r": 0.5, "tp_min_r": 0.25}
out = []
for name, c in cands.items():
    out.append({"cand": name, "var": "BASE", **row(c)})
    for k, s in steps.items():
        if k in c and c[k] is not None:
            for m in (-2, -1, 1, 2):
                v = round(c[k] + m * s, 2)
                if v <= 0: continue
                out.append({"cand": name, "var": f"{k}={v}", **row({**c, k: v})})
    for b in [None, 0.5, 1, 1.5]:
        if b != c.get("breakeven_at_r"):
            out.append({"cand": name, "var": f"be={b}", **row({**c, "breakeven_at_r": b})})
df = pd.DataFrame(out); df.to_csv(sys.argv[2], index=False)
pd.set_option("display.width", 250); print(df.to_string(index=False))
