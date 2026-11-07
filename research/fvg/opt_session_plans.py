"""Dump fvg_flow (tuned defaults) plan_log + 1-min spot for train / val / Nov 4 / Nov 5 so session
rules can be replayed quickly (research/fvg/opt_session_rules.py).

    .venv/bin/python research/fvg/opt_session_plans.py OUTDIR
"""
from __future__ import annotations

import pickle
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import eval as ev  # noqa: E402
import today as td  # noqa: E402
from algo.backtest.data import HistoricalData  # noqa: E402
from algo.backtest.engine import run_backtest  # noqa: E402
from algo.config import get_settings  # noqa: E402
from algo.data.store import DataStore  # noqa: E402
from algo.research.index_replay import NO_CALENDAR  # noqa: E402
from algo.strategies import get_strategy  # noqa: E402


def dump(name: str, out: Path) -> None:
    s = get_settings()
    ul = s.underlying("NIFTY")
    params = {"size_from_capital": False}
    if name == "flowdev_proxy":                       # flow_proxy: closer to the live (flow-filtered) stream
        params["flow_proxy"] = True
        start, end = ev.parse_period("flowdev", False)
        data = HistoricalData(DataStore(s.data_dir), ul, start, end, options=False)
    elif name in ("train", "val"):
        start, end = ev.parse_period(name, False)
        data = HistoricalData(DataStore(s.data_dir), ul, start, end, options=False)
    else:
        start = end = date.fromisoformat(name)
        data = td.make_data(start)
    strat = get_strategy("fvg_flow", **params)
    run_backtest(strat, ul, start, end, s, data=data, use_risk=False, calendar=NO_CALENDAR)
    pickle.dump({"plans": strat.plan_log, "spot": data.spot_1m, "p": strat.p, "skips": dict(strat.skips)},
                open(out / f"{name}.pkl", "wb"))
    print(name, len(strat.plan_log), dict(strat.skips), flush=True)


if __name__ == "__main__":
    out = Path(sys.argv[1])
    for n in sys.argv[2:] or ["2026-11-04", "2026-11-05", "train", "val"]:
        dump(n, out)
