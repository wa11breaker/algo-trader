"""Write backtest artifacts: trades.csv, daily_pnl.csv, summary.json, report.html."""
from __future__ import annotations

import json
import math
from datetime import datetime
from html import escape
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from algo.analytics.metrics import compute_metrics, equity_curve, monte_carlo
from algo.backtest.engine import BacktestResult


def _jsonable(v):
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    return v


def write_report(result: BacktestResult, reports_dir: Path) -> Path:
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}_{result.strategy}_{result.underlying}"
    out = Path(reports_dir) / run_id
    n = 1
    while True:   # parallel runs can start in the same second: never share a folder
        try:
            out.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            n += 1
            out = Path(reports_dir) / f"{run_id}-{n}"

    metrics = compute_metrics(result)
    if result.trades:
        metrics["monte_carlo_1lot"] = monte_carlo(result)
    metrics["params"] = result.params
    metrics["code_hash"] = result.code_hash
    metrics["settings"] = result.settings_snapshot
    eq = equity_curve(result)
    trades = result.trades_df()
    trades.to_csv(out / "trades.csv", index=False)
    eq.to_csv(out / "daily_pnl.csv")
    if result.diagnostics is not None and len(result.diagnostics):
        result.diagnostics.to_csv(out / "diagnostics.csv", index=False)
    (out / "summary.json").write_text(json.dumps({k: _jsonable(v) for k, v in metrics.items()}, indent=2, default=str))

    fig = make_subplots(rows=3, cols=1, shared_xaxes=False, vertical_spacing=0.08,
                        row_heights=[0.45, 0.25, 0.30],
                        subplot_titles=("Equity (dashed line = initial capital withdrawn)", "Drawdown", "Monthly net P&L"))
    fig.add_trace(go.Scatter(x=eq.index, y=eq["equity"], name="Trading capital", line=dict(color="#2a6fdb")), 1, 1)
    if eq["withdrawn"].any():
        fig.add_trace(go.Scatter(x=eq.index, y=eq["total_wealth"], name="Total (incl. withdrawn)",
                                 line=dict(color="#2e9e5b", dash="dot")), 1, 1)
        for w in result.withdrawals:
            fig.add_vline(x=str(w.on), line=dict(color="#2e9e5b", dash="dash"), row=1, col=1)
    fig.add_trace(go.Scatter(x=eq.index, y=eq["drawdown"], name="Drawdown", fill="tozeroy",
                             line=dict(color="#d64545")), 2, 1)
    if not eq.empty:
        monthly = eq["net_pnl"].groupby(eq.index.to_period("M")).sum()
        fig.add_trace(go.Bar(x=monthly.index.astype(str), y=monthly.values, name="Monthly",
                             marker_color=["#2e9e5b" if v >= 0 else "#d64545" for v in monthly.values]), 3, 1)
    fig.update_layout(height=900, showlegend=False, template="plotly_white",
                      margin=dict(l=50, r=20, t=40, b=40))

    rows = "".join(f"<tr><th>{escape(str(k))}</th><td>{escape(str(v))}</td></tr>"
                   for k, v in metrics.items())
    trade_rows = trades.tail(200).to_html(index=False, classes="trades", border=0) if len(trades) else "<p>No trades.</p>"
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{escape(result.strategy)} {escape(result.underlying)} backtest</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#222;background:#fff}}
 table{{border-collapse:collapse;font-size:13px}} th,td{{padding:4px 10px;border-bottom:1px solid #eee;text-align:left}}
 .grid{{display:grid;grid-template-columns:minmax(280px,380px) 1fr;gap:24px}}
 .trades{{width:100%;margin-top:12px}} h1{{font-size:20px}}
 @media(max-width:800px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>{escape(result.strategy)} on {escape(result.underlying)}: {result.start} to {result.end}</h1>
<div class="grid"><div><table>{rows}</table></div><div>{fig.to_html(full_html=False, include_plotlyjs=True)}</div></div>
<h2>Trades (last 200)</h2>{trade_rows}
</body></html>"""
    (out / "report.html").write_text(html)
    return out
