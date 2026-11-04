"""Command line: algo login | status | download | backtest | strategies | fyers-login | paper | paper-report"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from datetime import date, datetime, timedelta

import typer

from algo.config import get_settings

app = typer.Typer(add_completion=False, help="Index options trading bot (Upstox data).")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _parse_params(items: list[str]) -> dict:
    out = {}
    for it in items:
        k, _, v = it.partition("=")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


@app.command()
def login(no_browser: bool = typer.Option(False, help="Only print the login URL")):
    """Log in to Upstox (OAuth) and cache today's access token."""
    from algo.upstox.auth import login as do_login
    do_login(open_browser=not no_browser)
    typer.echo("Logged in. Token cached in .token.json (valid until 03:30 IST tomorrow).")


@app.command()
def status():
    """Check the token, Upstox Plus access and what data is stored locally."""
    _setup_logging(False)
    from algo.data.store import DataStore
    from algo.upstox.auth import load_token
    from algo.upstox.client import UpstoxClient, UpstoxError

    s = get_settings()
    tok = load_token()
    if not tok:
        typer.echo("Token: missing/expired -> run `algo login`")
    else:
        c = UpstoxClient(tok)
        try:
            prof = c.profile()
            typer.echo(f"Token: OK ({prof.get('user_name')}, {prof.get('user_id')})")
            exps = c.expired_expiries(s.underlying("NIFTY").instrument_key)
            typer.echo(f"Upstox Plus expired data: OK (NIFTY expiries {exps[0]} .. {exps[-1]})" if exps
                       else "Upstox Plus expired data: OK but no expiries returned")
        except UpstoxError as e:
            typer.echo(f"API check failed: {e}")
    store = DataStore(s.data_dir)
    for ul in s.raw["underlyings"]:
        months = sorted(p.stem for p in (s.data_dir / "spot" / ul).glob("*.parquet"))
        exps = store.expiries(ul)
        fx = store.future_expiries(ul)
        typer.echo(f"{ul:10s} spot months: {months[0] + '..' + months[-1] if months else '-'}   "
                   f"option expiries: {f'{exps[0]}..{exps[-1]} ({len(exps)})' if exps else '-'}   "
                   f"futures: {f'{fx[0]}..{fx[-1]} ({len(fx)})' if fx else '-'}")


@app.command()
def download(
    ul: list[str] = typer.Option(["NIFTY"], "--ul", help="Underlying(s): NIFTY, BANKNIFTY, SENSEX"),
    from_: str = typer.Option(None, "--from", help="YYYY-MM-DD (default: 6 months ago)"),
    to: str = typer.Option(None, "--to", help="YYYY-MM-DD (default: today)"),
    spot_only: bool = typer.Option(False, help="Only download index candles"),
    futures: bool = typer.Option(True, help="Also download expired monthly futures (volume / OI)"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Download 1-min index candles and expired option candles into ./data."""
    _setup_logging(verbose)
    from algo.data.downloader import Downloader
    from algo.data.store import DataStore
    from algo.upstox.auth import get_token
    from algo.upstox.client import UpstoxClient

    s = get_settings()
    end = _d(to) if to else date.today()
    start = _d(from_) if from_ else end - timedelta(days=183)
    dl_cfg = s.download
    client = UpstoxClient(get_token(), rps=dl_cfg["requests_per_second"], rpm=dl_cfg["requests_per_minute"],
                          r30m=dl_cfg.get("requests_per_30min", 1800))
    dl = Downloader(client, DataStore(s.data_dir), strikes_around=dl_cfg["strikes_around"])
    for name in ul:
        u = s.underlying(name)
        # extra spot history before `start` for indicator warm-up and option windows
        dl.download_spot(u, start - timedelta(days=40), end)
        if not spot_only:
            if futures:
                dl.download_futures(u, start, end)
            dl.download_options(u, start, end)
    typer.echo("Done. Run `algo status` to see stored data.")


@app.command("check-data")
def check_data(
    ul: list[str] = typer.Option(["NIFTY", "BANKNIFTY", "SENSEX"], "--ul"),
    from_: str = typer.Option("2022-01-01", "--from"),
    to: str = typer.Option(None, "--to", help="YYYY-MM-DD (default: today)"),
):
    """Report gaps and anomalies in the stored spot, option and futures data."""
    from algo.data import quality
    from algo.data.store import DataStore

    s = get_settings()
    store = DataStore(s.data_dir)
    start, end = _d(from_), _d(to) if to else date.today()
    for name in ul:
        u = s.underlying(name)
        typer.echo(f"\n== {name}")
        for section, rep in (("spot", quality.spot_report(store, name, start, end)),
                             ("options (nearest expiry, ATM/ITM1)",
                              quality.option_report(store, name, u.strike_step, max(start, date(2024, 10, 1)), end)),
                             ("futures", quality.futures_report(store, name, start, end))):
            typer.echo(f"  {section}:")
            for k, v in rep.items():
                typer.echo(f"    {k:28s} {v}")


@app.command()
def sweep(
    strategy: str = typer.Option(..., "--strategy", "-s"),
    ul: str = typer.Option("NIFTY", "--ul"),
    grid: str = typer.Option(..., "--grid", help="YAML/JSON: list of param dicts, or dict of value lists"),
    train: str = typer.Option(..., "--train", help="FROM:TO, e.g. 2022-01-01:2024-09-30"),
    test: str = typer.Option(None, "--test", help="FROM:TO; only the --top train configs run here, once"),
    mode: str = typer.Option("index", "--mode", help="index (index-only replay) | options (1-lot option backtest)"),
    top: int = typer.Option(3, "--top"),
    workers: int = typer.Option(6, "--workers"),
    param: list[str] = typer.Option([], "--param", "-p", help="Base params merged under every grid entry"),
):
    """Run a parameter grid on a train period, then the best configs once on a test period."""
    import pandas as pd
    from algo.research.sweep import load_grid, parse_period, run_sweep

    if mode not in ("index", "options"):
        raise typer.BadParameter("mode must be index | options")
    s = get_settings()
    g = load_grid(grid)
    typer.echo(f"{len(g)} configs, mode={mode}, train={train}" + (f", test={test} (top {top})" if test else ""))
    out = run_sweep(strategy, ul, g, _parse_params(param), parse_period(train),
                    parse_period(test) if test else None, mode, top, workers, Path(s.reports_dir) / "sweeps")
    pd.set_option("display.width", 250, "display.max_colwidth", 120, "display.max_rows", 500)
    for name in ("train", "test"):
        f = out / f"{name}.csv"
        if f.exists():
            typer.echo(f"\n== {name}\n" + pd.read_csv(f).drop(columns=["by_year"], errors="ignore").to_string(index=False))
    typer.echo(f"\nResults: {out}")


@app.command()
def strategies():
    """List strategies and their default parameters."""
    from algo.strategies import REGISTRY
    for name, cls in REGISTRY.items():
        typer.echo(f"\n{name}: {(cls.__doc__ or '').strip()}")
        for k, v in cls.defaults.items():
            typer.echo(f"   {k} = {v!r}")


@app.command()
def backtest(
    strategy: str = typer.Option(..., "--strategy", "-s"),
    ul: str = typer.Option("NIFTY", "--ul"),
    from_: str = typer.Option(..., "--from"),
    to: str = typer.Option(..., "--to"),
    param: list[str] = typer.Option([], "--param", "-p", help="Override a parameter, e.g. -p sl_pct=25 -p tf=15min"),
    capital: float = typer.Option(None, help="Account capital (default: backtest.initial_capital in config)"),
    no_risk: bool = typer.Option(False, "--no-risk", help="Disable the account risk manager for this run"),
    no_withdraw: bool = typer.Option(False, "--no-withdraw", help="Never withdraw the initial stake (pure compounding)"),
    skip_days: str = typer.Option(None, "--skip-days", help="Weekdays with no entries, e.g. fri or mon,fri ('none' = trade all days)"),
    monthly_buffer: int = typer.Option(None, "--monthly-expiry-buffer",
                                       help="No entries from N days before the monthly expiry (-1 = off)"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Run a backtest and write a report to ./reports."""
    _setup_logging(verbose)
    from algo.analytics.metrics import compute_metrics
    from algo.analytics.report import write_report
    from algo.backtest.engine import run_backtest
    from algo.strategies import get_strategy

    s = get_settings()
    strat = get_strategy(strategy, **_parse_params(param))
    cal = {}
    if skip_days is not None:
        cal["skip_weekdays"] = "" if skip_days.lower() == "none" else skip_days
    if monthly_buffer is not None:
        cal["skip_days_before_monthly_expiry"] = None if monthly_buffer < 0 else monthly_buffer
    t0 = datetime.now()
    result = run_backtest(strat, s.underlying(ul), _d(from_), _d(to), s, capital=capital,
                          use_risk=False if no_risk else None, withdraw=not no_withdraw, calendar=cal)
    out = write_report(result, s.reports_dir)
    m = compute_metrics(result)
    typer.echo(f"\nBacktest finished in {(datetime.now() - t0).total_seconds():.1f}s")
    for k, v in m.items():
        typer.echo(f"  {k:26s} {v}")
    typer.echo(f"\nReport: {out / 'report.html'}")


@app.command("fyers-login")
def fyers_login(no_browser: bool = typer.Option(False, help="Only print the login URL"),
                totp: bool = typer.Option(False, "--totp", help="Headless: FYERS_CLIENT_ID/PIN/TOTP_SECRET from .env")):
    """Log in to Fyers (live data for paper trading). Tokens expire daily at 06:30 IST."""
    from algo.fyers.auth import login as do_login, totp_login
    totp_login() if totp else do_login(open_browser=not no_browser)
    typer.echo("Logged in to Fyers. Token cached in .fyers_token.json (valid until 06:30 IST tomorrow).")


@app.command()
def paper(
    strategy: str = typer.Option(..., "--strategy", "-s"),
    ul: str = typer.Option("NIFTY", "--ul"),
    param: list[str] = typer.Option([], "--param", "-p", help="Strategy parameter, e.g. -p mode=directional"),
    capital: float = typer.Option(None, help="Starting capital of a NEW paper account (default: backtest.initial_capital)"),
    fill_model: str = typer.Option(None, "--fill-model", help="quote (bid/ask) | bar_open (backtest model)"),
    reset: bool = typer.Option(False, "--reset", help="Archive the existing paper account and start fresh"),
    ui_port: int = typer.Option(8770, "--ui-port", help="Dashboard port (the next free one if taken)"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Paper trade a strategy on live Fyers data (simulated fills, no real orders)."""
    _setup_logging(verbose)
    from algo.fyers.auth import get_token
    from algo.fyers.client import FyersClient
    from algo.fyers.symbols import FnoUniverse
    from algo.live.data import LiveMarketData
    from algo.live.feed import FyersFeed
    from algo.live.orderflow import BarBuilder, TickRecorder
    from algo.live.runner import PaperRunner, ist_now
    from algo.live.state import PaperStore
    from algo.strategies import get_strategy

    s = get_settings()
    cfg = s.paper
    u = s.underlying(ul)
    strat = get_strategy(strategy, **_parse_params(param))
    app_id, token = get_token()
    client = FyersClient(app_id, token)
    prof = client.profile()
    typer.echo(f"Fyers: logged in as {prof.get('name') or prof.get('fy_id') or '?'}")
    store = PaperStore(s.root / cfg.get("state_dir", "paper") / f"{strat.name}_{u.name}")
    master = FyersClient.symbol_master(u.fyers_fo_master, s.data_dir / "fyers", ist_now().date())
    builder = BarBuilder()
    feed = FyersFeed(client.socket_token, builder, log_path=str(store.root), max_symbols=int(cfg.get("max_symbols", 100)))
    data = LiveMarketData(client, FnoUniverse(master, u), u, builder, feed=feed,
                          warmup_days=int(cfg.get("warmup_days", 10)))
    # record the front-month future's ticks for footprint backtests (data/footprint/<UL>/<day>.csv)
    builder.recorder = TickRecorder(s.data_dir / "footprint" / u.name, want=lambda sym: sym == data.fut_sym)
    store.root.mkdir(parents=True, exist_ok=True)
    import threading
    srv, url = _dashboard(store.root, ui_port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    typer.echo(f"Dashboard: {url}")
    PaperRunner(strat, u, s, data, feed, store, capital=capital, fill_model=fill_model, reset=reset,
                echo=typer.echo).run()
    typer.echo(f"State and logs: {store.root}")


def _dashboard(root: Path, port: int, host: str = "127.0.0.1"):
    """Read-only dashboard server for one paper account: serves ui.html and root/live.json on
    `host` (127.0.0.1 = this machine only; 0.0.0.0 = public, no password), at `port` or the next
    free port (several paper sessions can run at once)."""
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    page = Path(__file__).parent / "live" / "ui.html"

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page.read_bytes())
            elif self.path.split("?")[0] == "/live.json":
                self.path = "/live.json"
                super().do_GET()
            else:
                self.send_error(404)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, *args):
            pass

    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer((host, p), partial(Handler, directory=str(root)))
            return srv, f"http://{host}:{p}"
        except OSError:   # port taken (another paper session's dashboard): try the next one
            continue
    raise RuntimeError(f"no free port in {port}-{port + 19} for the dashboard")


@app.command("paper-ui")
def paper_ui(
    strategy: str = typer.Option(..., "--strategy", "-s"),
    ul: str = typer.Option("NIFTY", "--ul"),
    port: int = typer.Option(8770, "--port"),
    host: str = typer.Option("127.0.0.1", "--host", help="0.0.0.0 serves it publicly (no password)"),
    no_browser: bool = typer.Option(False, help="Do not open the browser"),
):
    """Dashboard for a paper account without a running session (`algo paper` serves its own)."""
    import webbrowser

    s = get_settings()
    root = s.root / s.paper.get("state_dir", "paper") / f"{strategy}_{ul.upper()}"
    srv, url = _dashboard(root, port, host)
    typer.echo(f"Dashboard for {root}: {url}  (Ctrl-C to stop)")
    if not (root / "live.json").exists():
        typer.echo("live.json not written yet: it appears after `algo paper` processes its first minute.")
    if not no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


@app.command("paper-report")
def paper_report(
    strategy: str = typer.Option(..., "--strategy", "-s"),
    ul: str = typer.Option("NIFTY", "--ul"),
):
    """Write the usual HTML report for a paper account, plus fill / slippage statistics."""
    import pandas as pd
    from algo.analytics.metrics import compute_metrics
    from algo.analytics.report import write_report
    from algo.backtest.engine import BacktestResult, code_hash
    from algo.live.state import PaperStore, withdrawals_from

    s = get_settings()
    store = PaperStore(s.root / s.paper.get("state_dir", "paper") / f"{strategy}_{ul.upper()}")
    st = store.load()
    if st is None:
        raise RuntimeError(f"No paper account at {store.root}")
    trades = store.trades()
    days = sorted(date.fromisoformat(p.stem.removeprefix("events-")) for p in store.root.glob("events-*.jsonl"))
    start = days[0] if days else date.fromisoformat(st.started)
    end = days[-1] if days else date.today()
    result = BacktestResult(strategy=st.strategy, underlying=st.underlying, start=start, end=end, params=st.params,
                            initial_capital=st.initial_capital, trades=trades, trading_days=days,
                            withdrawals=withdrawals_from(st),
                            halted_on=date.fromisoformat(st.halted_on) if st.halted_on else None,
                            settings_snapshot={"paper": s.paper, "costs": s.costs, "execution": s.execution},
                            code_hash=code_hash())
    out = write_report(result, s.reports_dir / "paper")
    for k, v in compute_metrics(result).items():
        typer.echo(f"  {k:26s} {v}")
    if store.fills_path.exists():
        f = pd.read_csv(store.fills_path)
        q = f[f["bid"].notna() & f["ask"].notna()]
        if len(q):
            spread = q["ask"] - q["bid"]
            typer.echo(f"\nFills with a two-sided quote: {len(q)} of {len(f)}")
            typer.echo(f"  bid/ask spread     median {spread.median():.2f}  p90 {spread.quantile(0.9):.2f}")
            typer.echo(f"  |fill - ltp|       median {q['slip_vs_ltp'].median():.2f}  p90 {q['slip_vs_ltp'].quantile(0.9):.2f}")
            typer.echo(f"  -> execution.slippage_min of about {q['slip_vs_ltp'].median():.2f} matches live fills "
                       f"(now {s.execution['slippage_min']})")
    typer.echo(f"\nReport: {out / 'report.html'}")


@app.command("fx-backtest")
def fx_backtest(
    from_: str = typer.Option(..., "--from"),
    to: str = typer.Option(..., "--to"),
    symbol: str = typer.Option("XAUUSD", "--symbol"),
    param: list[str] = typer.Option([], "--param", "-p", help="Strategy param, e.g. -p trend_sma=100 -p rr=2"),
    cost: list[str] = typer.Option([], "--cost", "-c", help="Cost param, e.g. -c spread=0.4 -c swap_long=-30"),
    no_costs: bool = typer.Option(False, "--no-costs", help="Zero spread, commission and swap"),
    capital: float = typer.Option(100_000.0, help="Starting balance in USD"),
    ftmo: bool = typer.Option(False, "--ftmo", help="Also replay FTMO 2-Step challenges started in the period"),
    tf_hours: int = typer.Option(4, "--tf-hours", help="Signal timeframe in hours (server-time aligned)"),
):
    """Gold Engulfing (4H) backtest on Dukascopy 1-minute data; report to ./reports."""
    from dataclasses import asdict, replace

    from algo.forex.engulfing import GoldEngulfingParams
    from algo.forex.metrics import ftmo_2step
    from algo.forex.run import run_engulfing, write_fx_report
    from algo.forex.sim import CostParams

    s = get_settings()
    params = GoldEngulfingParams.from_dict(_parse_params(param))
    costs = CostParams.zero() if no_costs else CostParams()
    costs = replace(costs, **{k: float(v) for k, v in _parse_params(cost).items()})
    t0 = datetime.now()
    res = run_engulfing(s.data_dir, symbol, _d(from_), _d(to), params, costs, capital, hours=tf_hours)
    meta = {"strategy": "gold_engulfing", "symbol": symbol, "from": from_, "to": to, "tf_hours": tf_hours,
            "params": asdict(params), "costs": asdict(costs)}
    if ftmo:
        meta["ftmo_2step"] = ftmo_2step(res, from_, to)
    out, summary = write_fx_report(res, s.reports_dir, f"gold_engulfing_{symbol}", meta)
    typer.echo(f"\nBacktest finished in {(datetime.now() - t0).total_seconds():.1f}s")
    for k, v in summary.items():
        if k not in ("params", "costs", "by_year"):
            typer.echo(f"  {k:22s} {v}")
    for y, v in summary.get("by_year", {}).items():
        typer.echo(f"  {y}: {v}")
    typer.echo(f"\nReport: {out}")


def main() -> None:
    from algo.upstox.client import UpstoxError
    try:
        app()
    except (RuntimeError, UpstoxError, KeyError, ValueError) as e:   # FyersError is a RuntimeError
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
