"""End-to-end Gold Engulfing backtest + report writing."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from algo.forex.data import load_m1, resample_server
from algo.forex.engulfing import GoldEngulfingParams, signals
from algo.forex.metrics import ftmo_2step, monthly, summarize
from algo.forex.sim import CostParams, SimResult, simulate

WARMUP_DAYS = 90  # > 200 four-hour bars for the trend SMA


def run_engulfing(data_dir: Path, symbol: str, start: date, end: date, params: GoldEngulfingParams,
                  costs: CostParams, capital: float = 100_000.0, hours: int = 4, m1=None) -> SimResult:
    if m1 is None:
        m1 = load_m1(data_dir, symbol, start - timedelta(days=WARMUP_DAYS), end)
    else:
        m1 = m1.loc[str(start - timedelta(days=WARMUP_DAYS)): str(end)]
    h4 = resample_server(m1, hours)
    sig = signals(h4, params)
    return simulate(m1, h4, sig, params, costs, capital, trade_from=str(start))


def write_fx_report(res: SimResult, reports_dir: Path, name: str, meta: dict) -> tuple[Path, dict]:
    out = Path(reports_dir) / f"{datetime.now():%Y%m%d-%H%M%S}_{name}"
    n = 1
    while out.exists():
        out = out.with_name(f"{out.name}-{n}")
        n += 1
    out.mkdir(parents=True)
    summary = {**meta, **summarize(res)}
    if not res.trades.empty:
        res.trades.to_csv(out / "trades.csv", index=False)
        monthly(res).to_csv(out / "monthly.csv")
        eq = res.trades[["exit_utc", "net"]].copy()
        eq["equity"] = res.capital + eq["net"].cumsum()
        eq.to_csv(out / "equity.csv", index=False)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return out, summary
