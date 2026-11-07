"""Rupee P&L (real option prices, 50k capital) of exit-param variants of fvg_flow on 2026-11-04/05.

    cd research/new && ../../.venv/bin/python ../fvg/opt_exit_rs.py '<json list of param dicts>'
"""
import json
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")
from run2d import run2d  # noqa: E402
from algo.strategies import get_strategy  # noqa: E402

import pandas as pd  # noqa: E402

rows = []
for prm in json.loads(sys.argv[1]):
    t = run2d(get_strategy("fvg_flow", **prm), capital=50_000)
    day = pd.to_datetime(t.entry_time).dt.date.astype(str)
    by = t.net_pnl.groupby(day).sum()
    rows.append({"params": json.dumps(prm), "n": len(t), "Rs_total": round(t.net_pnl.sum()),
                 "Rs_nov4": round(by.get("2026-11-04", 0)), "Rs_nov5": round(by.get("2026-11-05", 0)),
                 "worst": round(t.net_pnl.min()) if len(t) else 0})
    if len(sys.argv) > 2:
        print(prm); print(t[["entry_time", "type", "lots", "entry_price", "exit_time", "exit_price", "exit_reason", "net_pnl"]].to_string(index=False))
print(pd.DataFrame(rows).to_string(index=False))
