# algo-trader

Index options trading bot for Indian markets (NIFTY, BANKNIFTY, SENSEX).
Phase 1: **backtesting** on 1-minute data from Upstox. Phase 2: **paper trading** on live Fyers data, reusing the same strategy code (see below). Live order placement is not implemented.

## Why Upstox (not Fyers) for data
Options backtests need candles for **expired** contracts. Upstox provides them through its Expired Instruments API, which requires **Upstox Plus** (currently free to activate). Fyers' history API only covers active contracts.

Limits to know about:
- Expired options history starts at **Oct 2024** (verified: NIFTY expiries 2024-10-03 onward). Index (spot) 1-min data goes back to Jan 2022.
- Rate limits: about 2000 requests per 30 minutes. The downloader throttles itself. A first-time 6-month NIFTY download makes a few thousand calls and takes a while, but it is resumable: re-running skips anything already saved.

## Setup
1. Activate **Upstox Plus** in the Upstox app.
2. Create an API app at https://account.upstox.com/developer/apps and set its redirect URL to `http://127.0.0.1:8765/callback`.
3. Configure and install:
   ```bash
   cd algo-trader
   cp .env.example .env          # fill in UPSTOX_API_KEY / UPSTOX_API_SECRET
   python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
   source .venv/bin/activate
   ```

## Usage
```bash
algo login                       # browser OAuth; token valid until 03:30 IST next day
algo status                      # checks token + Plus access + local data
algo download --ul NIFTY --from 2025-06-01 --to 2025-10-31
algo download --ul BANKNIFTY --ul SENSEX          # default: last ~6 months
algo strategies                  # list strategies + default params
algo backtest -s fvg_flow --ul NIFTY --from 2025-06-01 --to 2025-10-31
algo backtest -s fvg_supertrend --ul NIFTY --from 2025-06-01 --to 2025-10-31 -p st_tf=3min
algo check-data                  # gaps / anomalies in stored spot, option and futures data
algo sweep -s fvg_flow --ul NIFTY --grid grid.yaml --train 2022-01-01:2024-09-30 \
  --test 2024-10-01:2026-10-31 --top 3 --mode index
```
Each run writes `reports/<run>/` containing `report.html` (equity, drawdown, monthly P&L), `trades.csv`, `daily_pnl.csv` and `summary.json`.
`summary.json` also records `by_year`, `net_per_trade_stats` and `index_R_stats` (mean, bootstrap 95% CI, t-stat), the `settings` used and a `code_hash` of `src/algo`.

## Research workflow (sweeps, train/test, statistics)
- **Read the CI, not the mean.** With 60–150 trades and a per-trade R spread of about 1, the 95% CI of the mean is about ±0.15–0.25R. An "edge" of +0.1R whose CI straddles 0 can't be told apart from luck.
- **Selecting the best of many configs inflates in-sample results.** Example: a (since removed) liquidity-sweep strategy's best of 56 configs scored t = 3.5 in sample (+0.31R, CI [0.14, 0.49]) and +0.02R out of sample.
- **`algo sweep`** runs a grid file (YAML/JSON: a list of param dicts, or a dict of value lists for a cartesian product) on `--train`. The top `--top` configs then run once on `--test`. It runs in parallel (`--workers`), and results go to `reports/sweeps/<run>/` (`grid.json`, `train.csv`, `test.csv`).
  - `--mode index`: options are hidden and every plan an `IndexPlanMixin` strategy proposes (`plan_log`) is replayed on 1-min index bars (`src/algo/research/index_replay.py`).
    - The replay models stop first, then target, breakeven, trailing stop and time exit, one position at a time.
    - It needs only spot data, so it works from 2022 even though options start Oct 2024.
    - Results are R before option costs (≈0.1R per trade on NIFTY ITM1).
  - `--mode options`: a full option backtest at 1 lot, with risk rules and calendar filters off.
- **Suggested discipline:** tune in index mode on 2022–Sep 2024, then run the frozen top configs once on Oct 2024–Oct 2026 with options, on all three indices.

## Paper trading (live Fyers data, simulated fills)
Strategies run unchanged on live data: the same `TradingSession` loop as the backtest, with fills simulated by `PaperBroker`. **No real orders are ever sent.** Upstox stays the source for history and backtests.

### Setup (once)
1. Create a Fyers API app at https://myapi.fyers.in/dashboard and set its redirect URL to `http://127.0.0.1:8766/callback`.
2. Put `FYERS_APP_ID`, `FYERS_SECRET_ID` and `FYERS_REDIRECT_URI` in `.env` (see `.env.example`).
3. Install the Fyers SDK: `.venv/bin/pip install -e '.[live]'`.

### Every trading day
```bash
algo fyers-login                      # token expires daily at 06:30 IST
algo paper -s fvg_flow -s fvg_supertrend --capital 50000   # start before 09:15, or any time during the session
algo paper-report -s fvg_supertrend --ul NIFTY              # HTML report + live bid/ask and slippage stats (per account)
algo paper-ui                                               # dashboard for all accounts without a running session
```
- **Data:**
  - At start, `algo paper` loads 1-min history from Fyers REST for warm-up.
  - It then subscribes to the live feed: the index, the front-month future, and ATM ±`strikes_around` options. The strike window is re-centred every 5 minutes, and any contract a strategy selects is subscribed on demand.
  - 1-min bars are built from the feed. A minute the feed missed is back-filled from REST.
- **Fills (`paper.fill_model`):**
  - `quote` (default): orders fill right after the bar that triggered them, buying at the ask and selling at the bid.
  - `bar_open`: the backtest model. A test checks that it reproduces the backtest's trades exactly.
  - Each fill goes to `fills.csv`, with the quote at that moment.
- **State** lives in `paper/<strategy>_<UL>/`:
  - `state.json`: capital, withdrawals, kill switch and open positions, saved every minute.
  - `trades.csv`
  - `events-<day>.jsonl`
- **Restarts:**
  - The same day: positions and today's trades are restored. Strategy memory is not.
  - The next day: capital carries over.
  - Ctrl-C squares off intraday positions and closes the day.
  - A different strategy or parameter set needs `--reset`, which archives the old account.

### Running on a server (unattended)
A systemd timer starts `deploy/run-paper.sh` every weekday at 08:45 IST. The script:
- pulls the latest code from GitHub;
- logs in to Fyers headlessly (`algo fyers-login --totp`), retrying up to 5 times;
- paper trades `fvg_flow` on NIFTY (₹100,000, default params) until 15:30, then writes the report.

Weekends and holidays exit by themselves. Logs go to `logs/paper-<date>.log`.

1. **Turn on TOTP in Fyers:** in the Fyers web or app, under My Account > Security, enable External 2FA TOTP. Keep the secret key shown with the QR code.
2. **Set up the server:**
   ```
   git clone https://github.com/wa11breaker/algo-trader && cd algo-trader && bash deploy/setup-server.sh
   ```
   Then fill `.env`, which needs `FYERS_CLIENT_ID`, `FYERS_PIN` and `FYERS_TOTP_SECRET` as well as the app keys. Test the login with `TZ=Asia/Kolkata .venv/bin/algo fyers-login --totp`.
3. **Pull results on your laptop:**
   ```
   SERVER=user@host SSH_KEY=~/.ssh/key bash deploy/pull.sh
   ```
   This copies the paper accounts, reports, footprint ticks and logs into `server-data/`.

**Other options:**
- **Change the strategies or capital:** set `Environment="STRATEGIES=fvg_flow fvg_supertrend"` or `CAPITAL=...` in `/etc/systemd/system/algo-paper.service` (defaults: both strategies, ₹50,000 each for new accounts).
- **Restarts are safe:** `systemctl restart algo-paper`, a crash (`Restart=on-failure`) or a reboot during market hours resumes the day: open positions, today's trades and each strategy's memory (`paper/<acct>/ctx-<day>.pkl`) are restored. The day is closed only after 15:29.
- **View the dashboard:** use `ssh -L 8770:127.0.0.1:8770 user@host`, then open http://127.0.0.1:8770.
- **Make the dashboard public (read-only, NO password):** run `bash deploy/expose-ui.sh` on the server, then allow TCP 8770 in the Oracle Cloud security list. It runs `algo paper-ui --host 0.0.0.0` as an always-on service, so it still shows the last session after hours.

### Several strategies at once
One `algo paper` process runs every `-s` strategy as an independent account on one shared Fyers feed (about 44 symbols in total, under Fyers' 100-per-user cap). Each account keeps its own capital, trades, state and reports in `paper/<strategy>_<UL>/`:
```bash
algo paper -s fvg_flow -s fvg_supertrend --ul NIFTY --capital 50000
algo paper-report -s fvg_flow                          # one report per account
algo paper-report -s fvg_supertrend
```
- **Dashboard:** one page for all accounts, with a strategy selector and a summary card per account (`/accounts.json`, `/live.json?account=<folder>`). A link to `whatif-<day>.html` appears once `research/fvg/whatif.py` has run (the server runs it after the close).
- **`--reset`:** only needed when you change a strategy's parameters (it archives every account in that run). `-p` works with a single `-s` only; give a variant its own strategy class instead (like `fvg_supertrend`).

### Order flow (delta) confirmation
Fyers has no true tick-by-tick trade feed. Its data socket sends snapshots about once a second for the NIFTY future (measured 2026-11-04: median 1.0 s, timestamps in whole seconds), and the 50-depth "TBT" socket only carries the order book. `src/algo/live/orderflow.py` therefore *approximates* footprint-style delta:
- Each snapshot's new volume is classified as a buy if the price is at or above the previous ask, and as a sell if it is at or below the previous bid.
- Otherwise the tick rule decides: up-tick = buy, down-tick = sell.

Trades merged between snapshots make this noisier than a broker's own footprint chart.
- **In a strategy:** `ctx.order_flow(tf="1min")` returns `volume, buy_vol, sell_vol, delta, cum_delta` of the front-month future. Pass `contract=` for an option. Backtests raise `NotImplementedError`, because stored candles have no buy/sell split.
- **For any strategy without code changes:** set `paper.order_flow_gate.enabled: true` in `config/settings.yaml`.
  - Bullish entries (buy CE / sell PE) then need positive delta over the last `bars` minutes, at least `min_delta`.
  - Bearish entries need the opposite.
  - Optionally, today's cumulative delta must also be on the trade's side.
  - Blocked entries show up as `order_flow` in the risk blocks.
- **Recorded for later backtests:** `algo paper` appends every front-month future snapshot to `data/footprint/<UL>/<day>.csv` (`ts, symbol, ltp, bid, ask, cum_vol, vol, buy, sell`). Rebuild the footprint with `footprint(pd.read_csv(path), tf="1min", step=1.0)` from `algo.live.orderflow`, which gives buy / sell / delta per (bar, price level). Raw bid/ask is kept, so ticks can be re-classified later.

## Data notes (`algo check-data`)
- **Off-session bars.**
  - Upstox index candles for Mar 24–Jul 22 2022 include a 09:07 pre-open bar and a 15:31 closing-price bar, and 5 days in Apr–May 2025 have bars up to 15:59.
  - The backtest keeps only **09:15–15:29** bars (`regular_session` in `backtest/data.py`). Before this fix, the engine skipped those 84 days of 2022 as "special sessions", and the closing bars could distort previous-day high/low.
  - Days with fewer than 300 regular bars are skipped as special sessions (`backtest.min_session_bars`). These are Muhurat evenings and DR-drill Saturdays (2024-03-02, 2024-05-18).
- **Option coverage near the money (nearest expiry, ATM/ITM1, sampled).**
  - NIFTY: 99.4% present.
  - BANKNIFTY: 94%; most of the gaps are in Dec 2024.
  - SENSEX: options were only stored for Oct–Nov 2024 until the Nov 2026 download.
  - A missing contract is a silent `no_option` skip, so check `strategy_skips` in every summary.
- **Spot history:** NIFTY from Jan 2022. BANKNIFTY and SENSEX from Jan 2022 after the Nov 2026 `--spot-only` download.
- **Not modelled:** bid/ask spreads (slippage is a config guess; calibrate from paper fills) and option prices before Oct 2024 (not available from Upstox).

## How the backtest simulates trading
- Strategies see a bar only after it has closed. Higher timeframes (5/15-min) include only completed bars, so the backtest never uses future data.
- Orders fill at the **next 1-min bar's open of the option**, adjusted for slippage (`execution` in `config/settings.yaml`).
- Stop-loss and target are checked against each option bar's high and low. If both are hit in the same bar, the backtest assumes the **SL** hit first. If a bar opens past the SL, the fill is at that open (worse than the SL).
- All positions are squared off at 15:15. Nothing is held overnight.
- Charges include brokerage, STT, exchange transaction charges, SEBI fee, stamp duty and GST (`costs` in the config). Check them against your broker's contract notes.
- Lot sizes and expiry dates come from Upstox contract data, so past changes to lot sizes and expiry days are handled automatically.

## Volume, OI and futures
The index itself has no volume, so the downloader also fetches **expired monthly futures** (from Oct 2024; skip them with `--no-futures`).
- `ctx.spot()` bars get the **front-month futures volume and OI** merged in, so you can use volume filters on spot prices.
- `ctx.futures(tf)` returns the continuous front-month future itself, for futures VWAP and similar. It is **unadjusted**: prices jump by the basis (~100+ pts on NIFTY) at each roll, so use session-reset indicators (VWAP) on it, not multi-day EMAs.
- The series switches to the next contract one calendar day before expiry (`futures_roll_days_before` on `HistoricalData`).
- Index data from 2022 to Sep 2024 has **no volume** (there are no futures for that period). Volume strategies only make sense from Oct 2024 onwards.
- **Source gap:** Upstox has **no candles for the NIFTY Dec-2024 future** (`NSE_FO|35005`), not even daily ones. On days where the front month has no bars, `continuous_futures` uses the next month instead. The downloader never caches an empty download, and it extends the next contract's window to cover the gap.
- **Active contract:** the current, unexpired front-month future comes from Upstox's instrument master and the regular historical API (`download_active_futures`). It is re-downloaded on every run.
- After these fixes, every trading day from Oct 2024 to Oct 2026 has futures volume. Before them, 23 days (Nov 28 – Dec 24 2024 and Oct 27–30 2026) had none, and VWAP was undefined on those days.

## Removed strategies
On 2026-11-07 every strategy except fvg_flow / fvg_supertrend was deleted: ema_cross_buy, orb_buy, vwap_volume_buy, failed_breakout, vwap_pullback_spread, orb_vwap_trend, short_premium, overnight_credit, vwap_trend, vwap_ema_trend, london_liquidity_ifvg, vwap_sd_fade, trend_pullback, squeeze_breakout, pdhl_retest, vwap_structure, orb_momentum. None held up out of sample after costs. Their code, tests and research notes are in git history (before commit "Keep only fvg_flow and fvg_supertrend").

## Strategy: fvg_flow (1-min FVG retest + order-flow confirmation)
- **Gap:** a 3-bar fair value gap on 1-min NIFTY spot, at least `min_gap_pts` (3) wide. Gaps expire after `max_age_min` (30), and a close through the far edge kills a gap.
- **Entry:** a later bar dips into the gap and closes back out on its side. A bullish gap buys a CE, a bearish one buys a PE.
- **Confirmation:** the 1-min front-month future delta (last `flow_bars`) must agree, with an optional `min_delta` and `require_cum_delta`. Backtests have no order flow, so there the confirmation is skipped (`skips["no_flow_data"]`).
- **Plan (on the index):** stop at the far edge of the gap ± `sl_buffer_pts`, target `target_r` (2R), breakeven at 1R, square-off at 15:15.
- **Entry window:** 09:30–15:00. The strategy overrides the global `risk.no_entry_before/after` through its `risk_overrides`, raises `max_trades_per_day` to 100 (effectively no cap), and turns off the daily loss limit and the consecutive-loss stop, so it trades all day. The 50% capital kill switch and the per-trade risk caps still apply.
- **Option:** starts at `moneyness` OTM1 and steps further OTM (up to `max_otm_steps`) while the premium is above `max_premium` (₹150). `max_premium` works for every index-plan strategy.
- **FVG-only backtest** (Jun–Oct 2025, ₹10k): 51 trades, PF 0.68. The index result was +0.13R/trade but not significant (CI −0.26..+0.49), and the kill switch fired on 2025-07-24. The edge, if any, has to come from the order-flow filter, which can only be measured in paper trading or on recorded `data/footprint` ticks.
- **Defaults tuned on 2026-11-04** (the one day with recorded order flow; `research/fvg/results/today-report.md`): gaps of at least 0.45 ATR, EMA-50 trend filter, target 4 × ATR, breakeven at 0.5R, delta at least 10% of the bar's volume. Replay of that day: +10.8R vs +2.4R for the original rules. On 2024-01..2025-06 history it still loses (−0.10R/trade), so treat it as an experiment. The original rules: `-p min_gap_atr=null -p trend=none -p tp_mode=r -p breakeven_at_r=1 -p min_delta_ratio=null`.
- **2026-11-05 sizing fix:** lots are sized from a planned loss of `delta_est` (0.6) × index risk with the option safety stop matched to it (`sl_k` 1.5) plus a `loss_buf` (10%), and options under `min_premium` (₹20) are skipped, so a single stop stays within the 6% risk cap (was up to 12% with delta 0.4 and a flat 40% option stop). Entry tweaks: `min_gap_atr` 0.40, `min_delta_ratio` 0.05.
- **fvg_supertrend** = fvg_flow + `st_tf: 5min`: the 5-min Supertrend (10, 3) must agree with the trade. The only extra indicator that improved results in all three history periods (research/fvg/features.py); still about break-even on history, so it is an experiment.
- Changing params makes an existing paper account refuse to start until you pass `--reset`.

## Capital management (withdraw the initial stake)
`capital:` in `config/settings.yaml`, implemented in `src/algo/core/capital.py`, applies to every backtest:
- The risk manager's capital follows equity, so the limits compound with profits and shrink with losses.
- **Withdrawal:** once equity reaches `withdraw_multiple` (2×) the starting capital, the initial stake is withdrawn once. Trading continues on profits only.
- **Kill switch:** no new entries once equity falls below 50% of its start, or of what remained after the withdrawal.
- **Lot sizing:** `ctx.capital.lots_for(per_lot_risk, per_lot_cost)` returns the largest lot count within the per-trade risk % and position % limits.
- **Report:** the summary shows `withdrawn`, `withdrawal_dates`, `days_to_recover_capital`, `final_trading_capital`, `total_wealth` and a 1-lot `monte_carlo_1lot` estimate (the chance of reaching 2× before the kill switch). Drawdown is measured on total wealth, so a withdrawal doesn't count as a loss.
- `--no-withdraw` turns the withdrawal off, for pure compounding.

## Calendar filters (all strategies)
In `calendar:` in `config/settings.yaml`. These block new entries even with `--no-risk`:
- `skip_weekdays: []` (e.g. `[fri]`)
- `skip_days_before_monthly_expiry: null` (e.g. `2`: the monthly expiry day and the 2 calendar days before it)

Override per run with `--skip-days mon,fri` (or `none`) and `--monthly-expiry-buffer N` (`-1` turns it off).
Results documented above that were run before this filter existed did not use it.

## Risk manager (all strategies)
`risk:` in `config/settings.yaml` applies to every entry, and the same rules will carry into paper and live trading:
- **6% per trade:** the planned max loss must be ≤ 6% of capital
- **90% position size:** premium paid ≤ 90% of capital (one NIFTY lot costs ₹6–12k)
- **6% daily loss limit:** no new entries once the day is down 6%
- **3 losses in a row:** stop for the day
- **2 trades/day:** a spread counts as one
- **Entries 09:45–13:30 only**
- **No trading on event days** in `config/event_days.yaml` (RBI policy, Budget; verify and extend the list)

Blocked entries are counted in `risk_blocks` in the summary. Use `--capital 20000` to set the account size and `--no-risk` to turn the rules off.
With ₹10–20k, a single ATM NIFTY option with a 30–40% stop can still break the 6% rule, so those trades get blocked. That's intended.
- **Kill switch** (from the capital manager): no new entries once equity falls below 50% of its reference.

## Spreads
`ctx.open_debit_spread("CE", width_steps=1)` buys the ATM strike and sells the strike `width_steps` further out, checking risk once for the whole spread. Helpers: `ctx.spread_value`, `ctx.spread_legs`, `ctx.close_spread`. Both legs share a `group`, and metrics combine them into one round trip. Results are also reported **normalised to a 65 lot** (`net_pnl_lot65`), because the NIFTY lot size changed from 25 to 75 to 65 within the data.

## Slippage assumption
`execution.slippage_min: 0.5` (₹ per leg) is pessimistic for liquid NIFTY ATM options; real bid-ask is often ₹0.05–0.20. It matters a lot for spreads (4 legs). Paper fills so far: median |fill − LTP| ≈ ₹0.10. Calibrate it from real fills during paper trading before trusting any result.

## Writing a strategy
```python
from algo.core.strategy import Strategy
from algo.indicators import ema, crossed_above

class MyStrat(Strategy):
    name = "my_strat"
    defaults = {"lots": 1}

    def on_bar(self, ctx):
        bars = ctx.spot("15min", lookback=100)        # completed bars only
        if len(bars) > 20 and crossed_above(bars.close, ema(bars.close, 20)) and not ctx.active_positions:
            opt = ctx.select_option("CE", "ATM", expiry="nearest", min_days_to_expiry=1)
            if opt:
                ctx.buy(opt, lots=self.p["lots"], sl_pct=25, target_pct=50)
```
Register it in `src/algo/strategies/__init__.py`.

## Layout
```
src/algo/core/        models, abstract Broker/MarketData, Strategy, Context, RiskManager, CapitalManager, Spread (shared by all modes)
src/algo/upstox/      OAuth login + REST client (rate limited)
src/algo/data/        Parquet store + downloader
src/algo/backtest/    HistoricalData, SimBroker, costs, engine
src/algo/analytics/   metrics (incl. bootstrap CIs, by-year) + HTML report
src/algo/research/    index-only replay + parameter sweeps (algo sweep)
src/algo/strategies/  fvg_flow, fvg_supertrend (+ index_plan helper)
src/algo/live/        Fyers paper trading: feed, bars/order flow, PaperBroker, multi-account runner, dashboard (ui.html)
```

## Tests
`pytest` runs the suite on synthetic data (no API needed). It covers the cost calculations, fills and SL/target logic, no-lookahead checks, strike selection, and fvg_flow end to end (core engine tests use a small test-only ORB strategy in tests/helpers.py).

## Roadmap
- Paper trading: a `PaperBroker` that uses live Upstox market data (WebSocket) with simulated fills
- Live trading: an `UpstoxBroker` that implements the same `Broker` interface
- Daily option-chain recorder, to keep our own history independent of Upstox
