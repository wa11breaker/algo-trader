"""What-if replay of a paper day: run the paper strategy through the backtest engine on today's real
Fyers spot + option candles and the recorded footprint flow, with today's config (e.g. after a
calendar rule was removed), and write paper/<strategy>_NIFTY/whatif-<day>.html for the dashboard.

    .venv/bin/python research/fvg/whatif.py [--day 2026-11-06] [-s fvg_flow -s fvg_supertrend] [--expiry auto]

Needs a valid Fyers token (algo fyers-login --totp). Partial days are fine (min_session_bars = 1).
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import today_pnl as tp  # noqa: E402
from algo.backtest.data import HistoricalData  # noqa: E402
from algo.backtest.engine import run_backtest  # noqa: E402
from algo.config import get_settings  # noqa: E402
from algo.fyers.auth import get_token  # noqa: E402
from algo.fyers.client import FyersClient  # noqa: E402
from algo.strategies import get_strategy  # noqa: E402

ROOT = HERE.parents[1]


def nearest_expiry(day: date) -> tuple[date, object]:
    """The nearest NIFTY option expiry on `day` and the Fyers F&O universe (symbol master)."""
    from algo.fyers.symbols import FnoUniverse
    s = get_settings()
    u = s.underlying("NIFTY")
    uni = FnoUniverse(FyersClient.symbol_master(u.fyers_fo_master, s.data_dir / "fyers", day), u)
    return uni.expiries(day)[0], uni


def fetch(day: date, expiry: date, out: Path, uni=None) -> None:
    """spot_<day>.parquet (a week of warm-up) and options_<day>.parquet (day range +-450) into `out`."""
    from algo.core.models import OptionType
    c = FyersClient(*get_token())
    end = min(datetime.combine(day, datetime.strptime("15:29", "%H:%M").time()),
              datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=1))
    spot = c.history("NSE:NIFTY50-INDEX", datetime.combine(day - timedelta(days=9), datetime.min.time()), end)
    spot.to_parquet(out / f"spot_{day}.parquet")
    today = spot.loc[str(day)]
    lo, hi = int(today.low.min() // 50 * 50) - 450, int(today.high.max() // 50 * 50) + 500
    rows = []
    for k in range(lo, hi, 50):
        for t in ("CE", "PE"):
            time.sleep(1.5)   # Fyers Standard plan: 50 requests/min, shared with the live paper bot
            ct = uni.contract(expiry, float(k), OptionType(t)) if uni is not None else None
            sym = ct.instrument_key if ct is not None else f"NSE:NIFTY{expiry:%y}N{expiry:%d}{k}{t}"
            df = c.history(sym, datetime.combine(day, datetime.min.time()), end)
            if len(df):
                rows.append(df.reset_index().rename(columns={df.index.name or "index": "ts"}).assign(contract=f"{k}{t}"))
    pd.concat(rows).to_parquet(out / f"options_{day}.parquet")


def replay(day: date, expiry: date, strategy: str, capital: float, data_dir: Path) -> tuple[pd.DataFrame, dict]:
    s = get_settings()
    s.backtest["min_session_bars"] = 1
    ul = s.underlying("NIFTY")
    tp.HERE, tp.EXPIRY[day] = data_dir, expiry
    with tempfile.TemporaryDirectory() as tmp:
        data = HistoricalData(tp.build_store(day, Path(tmp), tp.LOT), ul, day, day)
        flow = tp.load_flow(day)
        data.order_flow = lambda until, tf="1min", symbol=None: flow[flow.index <= pd.Timestamp(until)]
        strat = get_strategy(strategy)
        t = run_backtest(strat, ul, day, day, s, data=data, capital=capital).trades_df()
    return t, dict(strat.skips)


def page(day: date, strategy: str, capital: float, t: pd.DataFrame, skips: dict, live: pd.DataFrame) -> str:
    def rows(df):
        if df.empty:
            return '<tr><td colspan="7" class="m">no trades</td></tr>'
        out = []
        for r in df.itertuples():
            cls = "up" if r.net_pnl > 0 else "dn"
            out.append(f"<tr><td>{pd.Timestamp(r.entry_time):%H:%M}</td><td>{html.escape(str(r.symbol))}</td>"
                       f"<td>{r.lots}</td><td>{r.entry_price:.2f}</td><td>{pd.Timestamp(r.exit_time):%H:%M} @ {r.exit_price:.2f}</td>"
                       f"<td>{html.escape(str(r.exit_reason))}</td><td class='{cls}'>{r.net_pnl:+,.0f}</td></tr>")
        return "".join(out)

    def total(df):
        n = df.net_pnl.sum() if len(df) else 0.0
        return f"<b class='{'up' if n > 0 else 'dn'}'>{n:+,.0f}</b> &middot; {len(df)} trades, {int((df.net_pnl > 0).sum()) if len(df) else 0} wins"

    head = "<tr><th>Entry</th><th>Option</th><th>Lots</th><th>Entry ₹</th><th>Exit</th><th>Reason</th><th>Net ₹</th></tr>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>What-if {day}</title>
<style>
:root{{--bg:#fff;--fg:#09090b;--m:#71717a;--b:#e4e4e7;--up:#16a34a;--dn:#dc2626}}
@media (prefers-color-scheme:dark){{:root{{--bg:#09090b;--fg:#fafafa;--m:#a1a1aa;--b:#27272a;--up:#22c55e;--dn:#ef4444}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,sans-serif}}
main{{max-width:900px;margin:0 auto;padding:24px 16px}} h1{{font-size:22px;margin:0}} h2{{font-size:16px;margin:28px 0 6px}}
p,.m{{color:var(--m)}} .wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--b);white-space:nowrap}} th{{color:var(--m);font-weight:500}}
.up{{color:var(--up)}} .dn{{color:var(--dn)}} a{{color:inherit}}
</style></head><body><main>
<h1>What-if: {strategy} on {day}</h1>
<p>Backtest engine on today's real Fyers option candles and recorded order flow, with the current config,
capital ₹{capital:,.0f}. Generated {datetime.now():%H:%M}.
Fills use candle prices + slippage, not live quotes. <a href="/">&larr; live dashboard</a></p>
<h2>If the bot had traded all day: {total(t)}</h2>
<div class="wrap"><table>{head}{rows(t)}</table></div>
<h2>What the live paper bot actually did: {total(live)}</h2>
<div class="wrap"><table>{head}{rows(live)}</table></div>
<p>Signals skipped by the strategy: {html.escape(json.dumps(skips))}</p>
</main></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=str(date.today()))
    ap.add_argument("--expiry", default="auto", help="option expiry, or auto = the nearest on --day")
    ap.add_argument("-s", "--strategy", action="append", help="paper strategy (repeatable; default fvg_flow)")
    ap.add_argument("--capital", type=float, help="default: the paper account's current equity")
    a = ap.parse_args()
    day = date.fromisoformat(a.day)
    expiry, uni = nearest_expiry(day) if a.expiry == "auto" else (date.fromisoformat(a.expiry), None)
    with tempfile.TemporaryDirectory() as d:
        fetch(day, expiry, Path(d), uni)                      # once: Fyers allows 50 requests/min
        for strategy in a.strategy or ["fvg_flow"]:
            acct = ROOT / "paper" / f"{strategy}_NIFTY"
            live_json = acct / "live.json"
            capital = a.capital or (json.loads(live_json.read_text()).get("equity", 100_000) if live_json.exists() else 100_000)
            t, skips = replay(day, expiry, strategy, capital, Path(d))
            live = pd.read_csv(acct / "trades.csv") if (acct / "trades.csv").exists() else pd.DataFrame()
            live = live[live.entry_time.str.startswith(str(day))] if len(live) else live
            acct.mkdir(parents=True, exist_ok=True)
            out = acct / f"whatif-{day}.html"
            out.write_text(page(day, strategy, capital, t, skips, live))
            print(f"{strategy}: {len(t)} what-if trades, net {t.net_pnl.sum() if len(t) else 0:+,.0f}; "
                  f"live {len(live)} trades -> {out}")


if __name__ == "__main__":
    main()
