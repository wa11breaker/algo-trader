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
algo backtest -s ema_cross_buy --ul NIFTY --from 2025-06-01 --to 2025-10-31
algo backtest -s orb_buy --ul BANKNIFTY --from 2025-06-01 --to 2025-10-31 -p sl_pct=25 -p moneyness=ITM1
algo check-data                  # gaps / anomalies in stored spot, option and futures data
algo sweep -s london_liquidity_ifvg --ul NIFTY --grid grid.yaml --train 2022-01-01:2024-09-30 \
  --test 2024-10-01:2026-10-31 --top 3 --mode index -p levels=pdhl
```
Each run writes `reports/<run>/` containing `report.html` (equity, drawdown, monthly P&L), `trades.csv`, `daily_pnl.csv` and `summary.json`.
`summary.json` also records `by_year`, `net_per_trade_stats` and `index_R_stats` (mean, bootstrap 95% CI, t-stat), the `settings` used and a `code_hash` of `src/algo`.

## Research workflow (sweeps, train/test, statistics)
- **Read the CI, not the mean.** With 60–150 trades and a per-trade R spread of about 1, the 95% CI of the mean is about ±0.15–0.25R. An "edge" of +0.1R whose CI straddles 0 can't be told apart from luck.
- **Selecting the best of many configs inflates in-sample results.** Example: london_liquidity_ifvg's best of 56 configs scored t = 3.5 in sample (+0.31R, CI [0.14, 0.49]) and +0.02R out of sample.
- **`algo sweep`** runs a grid file (YAML/JSON: a list of param dicts, or a dict of value lists for a cartesian product) on `--train`. The top `--top` configs then run once on `--test`. It runs in parallel (`--workers`), and results go to `reports/sweeps/<run>/` (`grid.json`, `train.csv`, `test.csv`).
  - `--mode index`: options are hidden and every plan an `IndexPlanMixin` strategy proposes (`plan_log`) is replayed on 1-min index bars (`src/algo/research/index_replay.py`).
    - The replay models stop first, then target, breakeven, trailing stop and time exit, one position at a time.
    - It needs only spot data, so it works from 2022 even though options start Oct 2024.
    - Results are R before option costs (≈0.1R per trade on NIFTY ITM1).
    - The replay is exact only for strategies with `index_replay_exact = True` (currently london_liquidity_ifvg and vwap_sd_fade). Others have extra signal exits, such as a VWAP break, that it ignores.
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
algo paper -s orb_buy --ul NIFTY --capital 15000          # start before 09:15, or any time during the session
algo paper-report -s orb_buy --ul NIFTY                   # HTML report + live bid/ask and slippage stats
algo paper-ui -s orb_buy --ul NIFTY                       # dashboard without a running session (`algo paper` prints its own link)
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

### Several strategies at once
Run one `algo paper` process per strategy, each in its own terminal. Every strategy keeps its own account in `paper/<strategy>_<UL>/`, so capital, trades, reports and dashboards stay independent:
```bash
algo paper -s fvg_flow --ul NIFTY --capital 15000      # terminal 1
algo paper -s orb_buy  --ul NIFTY --capital 15000      # terminal 2
# each session prints its dashboard link: http://127.0.0.1:8770, :8771, ... (the next free port)
algo paper-report -s fvg_flow                          # one report per strategy
algo paper-report -s orb_buy
```
- **Fyers symbol cap:** each process opens its own websocket with about 44 symbols (spot, future and ATM ±10 CE/PE). Fyers allows 100 per user, so with 3 or more strategies, lower `paper.strikes_around` (for example to 6). Symbols over the cap fall back to slower REST quotes.
- **Footprint data:** only the first process records it (a lock file in `data/footprint/<UL>/`), so the tick CSV has no duplicates.
- **`--reset`:** only needed when you change a strategy's parameters. A different strategy gets its own folder.

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

## Strategy: failed_breakout (Failed Breakout Reversal)
Also known as a false breakout, fakeout or bull/bear trap; in SMC terms a liquidity sweep. Price pokes beyond a key level, gets rejected, and the strategy trades the other way: an upside sweep that fails → **buy PE**, a downside sweep that fails → **buy CE**.
- **Levels:** opening range (`OR`), previous day high/low (`PD`), intraday swing high/low (`SW`), all on 5-min index candles. Optional: previous day close (`PDC`) and previous week high/low (`PW`), e.g. `-p levels=PD,PDC,PW`.
- A breakout only counts if the previous candle closed on the inside of the level.
- **Rejection:** a close back inside the level, or a pin bar / engulfing candle, within 3 bars of the breakout. A breakout that runs more than 1×ATR beyond the level is treated as real and ignored.
- **Exits** use the **index** price: the stop is the sweep extreme ± 5 pts, the target is **2× risk (1:2)**. The option also has a 40% premium stop as a safety net.
- Per-level results appear as `pnl_by_tag` in the summary, and each run writes `diagnostics.csv`: one row per trade with the level, stop, target, realised index R, best move before the exit (MFE in R) and minutes held.
- **Status (NIFTY, Oct 2024–Oct 2025):** no measurable edge. About +0.01R per trade on the index before costs; neither a time-of-day filter nor key-levels-only survived a hold-out test. Re-test on BANKNIFTY/SENSEX before using it.
```bash
algo backtest -s failed_breakout --ul NIFTY --from 2025-08-01 --to 2025-10-27
algo backtest -s failed_breakout --ul NIFTY --from 2025-08-01 --to 2025-10-27 -p levels=OR,PD -p rejection_mode=close_inside
```

## Strategy: orb_vwap_trend (ORB + VWAP trend continuation)
Trades only when the opening range breakout, VWAP and the 15-min trend agree, and enters on the first pullback after the breakout instead of chasing it.
- **Opening range:** 09:15 to 09:30, configurable with `range_minutes`. The day is skipped if the range width is outside 0.2–0.9× the 14-day ATR.
- **Bias (long):** the 15-min EMA20 is rising, spot is above the previous close, and the 5-min close is above the session VWAP. Shorts are the mirror image.
- **Breakout:** a 5-min close beyond the opening range, on futures volume ≥ `vol_mult` × the 20-bar average. This only arms the setup.
- **Entry:** within 6 bars, price pulls back to the range edge, VWAP or the 5-min EMA9, then a 5-min close beyond the previous bar's high (or low for shorts). It buys an ITM1 CE (long) or PE (short).
- **Exits (on the index):**
  - stop at the pullback extreme ± 5 pts;
  - breakeven at +1R, target at 2R, trailing exit on EMA9 after +1.5R;
  - exit on a 5-min close back through VWAP, and at 15:00;
  - a 35% premium stop as a safety net.
- **Sizing:** lots are calculated from current equity through the capital manager (`-p size_from_capital=false` trades a fixed `lots`).
- **Status (NIFTY, dev period Oct 2024–Sep 2025): fails. Not run on the hold-out.**
  - A 24-run grid (range 15/30 min, `vol_mult` 0/1.0/1.2, `min_dte` 0/1, `target_r` 1.5/2) lost money in every combination. The best profit factor was 0.75.
  - On the index the signal averages **0.00R per trade** (17 targets at +1.55R against 25 stops at −1R). Option costs, slippage and theta turn that into a steady loss.
  - The default settings at ₹15k: 14 trades, −₹2,059.
  - Longs did better than shorts (+0.35R vs −0.20R with the extra exits off). That observation was found by data mining and needs a fresh test before anyone relies on it.
```bash
algo backtest -s orb_vwap_trend --ul NIFTY --from 2025-10-01 --to 2026-10-31 --capital 15000
```

## Strategy: short_premium (intraday credit spreads)
Sells a hedged credit spread at `entry_time` and buys it back by 15:10. Modes:
- `directional`: sell the side against the morning trend. In an uptrend it sells a put spread; trend = 5-min close vs VWAP plus the 15-min EMA slope.
- `condor`: sell both sides.
- `fade`: the control case, selling the side the trend is moving towards.

Exits:
- **Stop:** buyback cost ≥ credit × (1 + `stop_mult`).
- **Target** (optional): `target_pct` of the credit captured.
- **Time:** 15:10.

**Status (NIFTY): failed the hold-out.**
- **Dev (Oct 2024–Sep 2025):** condor and fade lost in every combination. Directional mode at a 09:30 entry close to expiry did very well. Best config: ATM short, 200-pt wing, `stop_mult` 3, `dte_max` 2 → 80 trades, 69% win, PF 2.79.
  - The edge fades smoothly with later entries (09:30 → 10:00 goes from strongly positive to negative).
  - Fills were checked: the ATM options are liquid and fills are within about 1.4 pts of the next 10 minutes' median.
- **Hold-out (Oct 2025–Oct 2026), parameters frozen:**
  - config A: 70 trades, 61% win, PF 0.96 (−₹3.4k per lot);
  - config B (OTM1, 100-pt wing): PF 0.76.
  - By quarter, 3 of 5 hold-out quarters were positive, but Q1 2026 lost ₹12.9k.
  - NIFTY's weekly expiry moved from Thursday to Tuesday in Sep 2025, which may have changed expiry-day behaviour.
- **BANKNIFTY (no tuning):** +₹17.9k on 19 trades, then −₹10.8k on 5 trades. Too few trades to tell.
- **Margin/size:** an ATM short with a 200-pt wing can lose about ₹9k per lot, and brokers need margin well above ₹15k. Real-world margin was not modelled.
```bash
algo backtest -s short_premium --ul NIFTY --from 2025-10-01 --to 2026-10-31 --no-risk \
  -p mode=directional -p entry_time=09:30 -p short_steps=0 -p wing_steps=4 -p dte_max=2 -p stop_mult=3.0 -p size_from_capital=false
```

## Strategy: overnight_credit (sell at the close, buy back next morning)
Holds positions overnight (`intraday = False`). The engine then only squares off contracts expiring that day.
**Status: fails** in every dev combination (condor and directional; exit at 09:20 or 10:30). Options are priced higher at the next morning's open than at 15:00, so the overnight time decay never shows up. The same opening premium is what made the 09:30 intraday sale look good on the dev period.

## Strategy: vwap_trend (VWAP trend following, retest entries)
- **Trend:** the last 3 five-minute closes are on one side of VWAP, VWAP slopes that way, and the 15-min EMA20 agrees (`use_htf_trend`).
- **Entry:** a VWAP retest (the low comes within 8 pts of VWAP but the bar closes on the trend side), then a confirmation:
  - a bar that closes beyond the previous bar's high (or low for shorts);
  - or, with `confirm_mode=any`, a pin bar or engulfing bar on the retest itself.
  - It buys an ITM1 CE in an uptrend or an ITM1 PE in a downtrend.
- **Exits:** stop beyond the retest extreme, a 5-min close back through VWAP, an ATR trailing stop (1.5× ATR from +1R), breakeven at +1R, and 15:00. Sizing and exits use the shared `strategies/index_plan.py` helper.
- **Status (NIFTY, with the Friday and monthly-expiry calendar filters): failed the hold-out.**
  - **Dev (Oct 2024–Sep 2025):** the defaults were positive. With ₹20k and the risk rules: 103 trades, +₹18.7k, PF 1.28. At 1 lot: PF 1.10. These figures are re-run with complete volume data; before the fix they were 93 trades, +₹14.1k, PF 1.21.
  - **Dev grid (48 runs):** pattern confirmation (`any`) and the 15-min trend filter were clearly better. Trailing-stop settings made little consistent difference. Best: trail 2.5× ATR from +0.5R, PF 1.30.
  - **Hold-out (Oct 2025–Oct 2026), frozen settings:**
    - best config at 1 lot: PF 0.80 (−₹18.3k);
    - defaults at 1 lot: PF 0.98 (−₹1.6k);
    - with ₹20k and the risk rules, both lose about ₹10.4k, leaving about ₹9.6k.
  - **`vwap_source=futures`** (real futures VWAP, converted to spot terms by subtracting the basis), compared on dev only:
    - 1 lot: PF 0.82, −₹12.6k, against spot's PF 1.10, +₹6.4k;
    - ₹20k: PF 0.70 against PF 1.28.
    - Spot stays the default. A difference of a few points in the VWAP level is enough to flip the result, which is another sign the edge isn't robust.
```bash
algo backtest -s vwap_trend --ul NIFTY --from 2025-10-01 --to 2026-10-31 --capital 20000
```

## Strategy: vwap_ema_trend (VWAP + EMA 9/21 trend following, pullback entries)
- **Trend (long):** the 5-min close is above session VWAP (on this bar and the previous one) and EMA9 is above EMA21. Optionally EMA9 must also be rising (`ema_slope_bars`). Shorts are the mirror image.
- **Pullback:** a bar's low comes within `touch_pts` (8) of VWAP or EMA9 (`pullback_to=vwap|ema|either`), pierces it by no more than `pierce_pts` (10), and the bar closes above VWAP.
- **Entry:** within `confirm_bars` (2), a green bar closes above the previous bar's high. With `confirm_mode=any`, a bullish pin bar or engulfing pullback bar confirms immediately. It buys an ITM1 CE (long) or PE (short).
  - The setup is dropped if price closes back through VWAP (`lost_vwap`) or the EMAs cross (`ema_flip`).
- **Exits, `exit_mode=premium` (default):**
  - stop at `sl_pct` (25%) of premium and target at `rr` × `sl_pct` (+50%);
  - **trend-structure exit** on a 5-min close back through VWAP or an EMA9/21 cross against the trade (`structure_exit=vwap|ema|either|none`);
  - time exit at 15:00.
- **`exit_mode=index`:** index stop at the pullback extreme ± 5 pts, target 2R, and a 35% premium safety net. Every signal is logged to `plan_log`, so `algo sweep --mode index` works; for premium exits the replay is only an approximation.
- **Status (NIFTY, dev period Oct 2024–Sep 2025, 1 lot, `--no-risk`): no edge.**
  - Defaults (25% SL, 1:2): 290 trades, 32% win, PF 0.96, −₹9.3k. Mean per trade −₹32, CI [−294, +245].
  - `sl_pct=20`: 304 trades, PF 1.02, +₹6.2k. The CI still straddles 0.
  - `exit_mode=index`: PF 0.80, −₹44.8k. The index signal averages +0.03R, CI [−0.14, +0.19], before option costs.
  - Every variant made money in Q4 2024 (PF 1.1–1.9) and lost in 2025 (PF 0.75–0.93).
  - With the default ₹10k and the risk rules, only 18 trades happened, all in 2024. After that, one lot was too large to size (1,341 `too_small_to_size` skips).
  - Not run on the hold-out.
```bash
algo backtest -s vwap_ema_trend --ul NIFTY --from 2024-10-01 --to 2025-09-30 --no-risk -p size_from_capital=false
algo backtest -s vwap_ema_trend --ul NIFTY --from 2024-10-01 --to 2025-09-30 -p sl_pct=20 -p exit_mode=premium
```

## Round 2: five option-buying candidates (trend_pullback, squeeze_breakout, pdhl_retest, vwap_structure, orb_momentum)
All five share `strategies/bar_signal.py`:
- **Signals and exits:** decisions on completed 5-min bars; every exit is on the index plan (stop, target, ATR trail, 15:00), so `index_replay_exact = True`.
- **Distances in ATR multiples:** stop buffer and risk limits scale with ATR, so one config works on NIFTY, BANKNIFTY and SENSEX.
- **Indicators:** new `bollinger`, `adx` and `swing_points` in `indicators.py`. Swing points have no lookahead: a pivot is reported only on the bar that confirms it.

| Strategy | Entry (long; short is the mirror) | Stop |
|---|---|---|
| `trend_pullback` | impulse ≥ `impulse_atr`×ATR (optional ADX filter), then the **first** pullback to EMA9/20 and a bar closing above the previous high | pullback low |
| `squeeze_breakout` | Bollinger width at or below its `bw_pct` percentile for ≥ `min_squeeze_bars`, then a close beyond the box on a wide bar | box low or box middle |
| `pdhl_retest` | close above the previous day's high, a retest that holds, then confirmation | retest low |
| `vwap_structure` | HH + HL on confirmed swings, above VWAP; enter when a new higher low is confirmed and price is moving on | the higher low |
| `orb_momentum` | close beyond the opening range on the breakout bar, with a big body, RSI and VWAP side agreeing | OR middle or the opposite edge |

**Method** (scripts and grids in `research/round2/`, rules fixed before any results were seen):
1. 256 configs tuned in index mode on NIFTY, Jan 2022 – Sep 2024 (`run_train.sh`).
2. Shortlist (`shortlist.py`): n ≥ 80, R ≥ +0.15, each year positive, ≥ 75% of neighbouring configs positive, at most 2 per strategy.
3. Robustness check on BANKNIFTY and SENSEX for the same period (`run_stage.py robust`).
4. One frozen test, Oct 2024 – Oct 2026: index mode plus 1-lot options on NIFTY and BANKNIFTY (`run_stage.py test`).

**Status: all five fail.**
- **Shortlist:**
  - squeeze_breakout: no config passed (its best had a negative 2024);
  - pdhl_retest: passed on NIFTY but lost on BANKNIFTY and SENSEX (−0.07R each);
  - the other six configs reached the test.

| Config (frozen) | Train R (NIFTY) | Test index R, NIFTY / BANKNIFTY | Test options PF, NIFTY / BANKNIFTY |
|---|---|---|---|
| trend_pullback (ADX≥25, impulse 4 ATR, EMA9, trail) | +0.19 | +0.03 / −0.07 | 0.67 / 0.52 |
| trend_pullback (same, 3R target) | +0.18 | +0.08 / −0.07 | 0.76 / 0.52 |
| vwap_structure (swing 2, 3R) | +0.15 | +0.09 / −0.08 | 0.84 / 0.57 |
| orb_momentum (30-min OR, body 1 ATR, RSI 65, mid stop, 3R) | +0.24 | +0.18 / +0.22 | 0.84 / 0.95 |
| orb_momentum (same, stop at the opposite edge) | +0.20 | +0.17 / +0.06 | 0.71 / 0.83 |

- trend_pullback and vwap_structure had clean training results (t > 2, every year positive) and lost their edge on the test period. This is the same pattern as the earlier strategies.
- **orb_momentum is the only signal that held up on the index out of sample** (+0.18R, t 1.4; +0.22R, t 1.8; not significant), but it still lost with options.
  - Cause: the stop at the OR middle averages 83 NIFTY pts, about 38% of the ITM1 premium at delta 0.6. So the **35% premium safety net fires before the index stop**.
  - 33 of 90 trades were cut at −0.45R on average; the 39 trades that reached the time exit averaged +0.86R.
  - A fix (a safety net set wider than the index risk) can only be checked on the same Oct 2024+ option data, so it would no longer be out of sample. It would need paper trading to confirm.

## Strategy: london_liquidity_ifvg (liquidity sweep + inverse FVG)
A port of the "London Liquidity / IFVG" futures strategy. NSE has no London session, so the "London box" is a **morning box, 09:15–10:15 IST** (`box_start`/`box_end`).
- **Sweep (LS):** between 10:15 and 13:30 (`sweep_start`/`sweep_end`), a 5-min close beyond the box high, then a large-bodied bar (body > 50% of its range, `body_pct`) closing back inside, is a bearish sweep. The mirror move at the box low is a bullish sweep. Only one sweep counts per day.
- **IFVG entry:** the strategy tracks 3-bar fair value gaps that form inside the box or after the sweep. It enters when price closes through one in the opposite direction and that agrees with the sweep: after a high sweep, a close below a bullish FVG buys a **PE**; after a low sweep, a close above a bearish FVG buys a **CE**. One setup per day.
- **Exits (on the index):**
  - stop: `sl_type=atr` (`sl_atr_mult` × ATR14, 5-min) or `swing` (the extreme of the last `swing_lookback` bars);
  - target: `tp_type=atr` (`tp_atr_mult` × ATR), `rr` (`risk_reward` × risk) or `range` (the opposite box level);
  - time exit at 15:00, plus a 35% premium stop as a safety net.
- **Mapping from the original:**
  - fixed lots and % risk sizing are `lots` / `size_from_capital`; the "$ risk" mode was dropped;
  - the weekday filter is the calendar filter (`--skip-days`);
  - there is no charting. Each run writes `diagnostics.csv` instead.
  - The MNQ preset's exits are `-p sl_atr_mult=2 -p tp_atr_mult=0.2`.
- **Status (NIFTY, 1 lot, `--no-risk --skip-days none`, default params): fails.**
  - **Dev (Oct 2024–Sep 2025):** 81 trades, 43% win, PF 0.63, −₹25.2k. The index signal averages **−0.12R per trade**.
  - **Hold-out (Oct 2025–Oct 2026):** 88 trades, PF 0.67, −₹26.5k, −0.06R per trade.
  - Only the defaults were run; there was no tuning.
  - With ₹20k and the risk rules on the dev period: 15 trades, PF 0.30, −₹7.4k.
  - **Dev grid (32 runs, 1 lot):**
    - the grid tried the stop beyond the sweep extreme (`sl_type=ls`), 1:1 and 1:1.5 targets, stale-sweep cutoff `max_bars_after_ls=6`, `fvg_source=after_ls` and `entry_start=11:00`;
    - every combination lost money after costs, with PF between 0.26 and 0.91;
    - the best with ≥40 trades was ATR stop, `risk_reward=1.5`, `entry_start=11:00`: −0.03R per trade, PF 0.91;
    - the `ls` stop is *wider* than 1.5×ATR (≈55 vs 37 pts), and `after_ls` made results worse;
    - the hold-out was not run, because no config passed on dev.
  - Diagnostics: only 1–4% of trades ever reach +2R, and option drag (delta, slippage, charges, theta) costs about 0.1R per trade.
- **`-p levels=pdhl`: the previous day's high/low instead of the morning box.** Today's gaps before the sweep take the place of the box-session gaps. Use `-p sweep_start=09:45`, because the risk manager blocks entries before 09:45. **Status: passed one NIFTY hold-out, then failed the wider out-of-sample test below.**
  - **Dev (Oct 2024–Sep 2025, 1 lot, no risk rules), 6 variants:**
    - defaults: +0.11R per trade, 62 trades, PF 1.02;
    - best was `entry_start=11:00`: +0.16R, 58 trades, PF 1.12.
  - **Hold-out (Oct 2025–Oct 2026), frozen:**

    | config | trades | index R/trade | win | PF | net |
    |---|---|---|---|---|---|
    | defaults, 1 lot | 66 | +0.11 | 53% | 0.90 | −₹5.8k |
    | `entry_start=11:00`, 1 lot | 64 | +0.20 | 58% | 1.15 | +₹7.0k (max DD −₹7.8k) |
    | `entry_start=11:00`, ₹1L, risk rules on | 54 | +0.24 | 59% | 1.21 | +₹41.7k (max DD −₹39.7k) |
  - **Caveats:**
    - 11:00 was chosen from 6 variants on dev;
    - long and short swapped roles: longs carried dev, shorts carried the hold-out, and hold-out longs were ≈0R;
    - about 60 trades a year;
    - the drawdown at ₹1L is as large as the profit.
  - Paper-trade it before risking money.
- **Entry filters (opt-in):**
  - `ls_confirm=rejection|either`: a pin bar or engulfing candle on the sweep;
  - `min_sweep_atr`: how far the sweep must go past the level;
  - `min_fvg_atr`: minimum gap size;
  - `entry_body_pct`: a strong entry candle.
- **Optimisation on fresh data (Nov 2026). Status: fails out of sample.**
  - **Method:** tuned on NIFTY **index-only** data, Jan 2022–Sep 2024. That's data downloaded just for this, which the strategy had never seen. Each setup's index stop, target and 15:00 exit were replayed on 1-min bars, with no options.
    - Round 1: 48 entry-filter combinations.
    - Round 2: 8 stop/target variants.
  - **In-sample:**
    - every one of the 48 combinations was positive;
    - plain previous-day levels: +0.13R per trade;
    - best, `ls_confirm=either min_fvg_atr=0.15 sl_atr_mult=2 tp_type=rr risk_reward=1.5`: +0.30R over 156 trades, positive every year;
    - `min_sweep_atr` had no effect, `entry_body_pct` and `entry_start=11:00` hurt.
  - **Out of sample (Oct 2024–Oct 2026), index R per trade:**

    | config | NIFTY | BANKNIFTY | SENSEX |
    |---|---|---|---|
    | plain previous-day levels | +0.08 | −0.05 | +0.01 |
    | + `either` + `min_fvg_atr=0.15` | +0.13 | +0.00 | +0.02 |
    | fully tuned (also 2×ATR stop, 1.5R target) | +0.02 | +0.14 | +0.03 |

  - **With options, fully tuned:**
    - NIFTY 1 lot: PF 1.00 (−₹0.6k);
    - BANKNIFTY 1 lot: PF 1.03 (+₹4.1k, max DD −₹34k);
    - NIFTY ₹1L with the risk rules on: −₹50k.
  - **Conclusion:** the in-sample edge of +0.2–0.3R shrinks to about 0–0.14R out of sample, and which config looks best changes with the index. Option costs (≈0.1R) eat what's left. No config has a reliable edge.
  - **Re-run after the session-data fix** (84 days of 2022 had been skipped). In sample: tuned +0.31R over 173 trades, CI [0.14, 0.49]. Out-of-sample numbers are unchanged, and every out-of-sample 95% CI on all three indices includes 0.
```bash
algo backtest -s london_liquidity_ifvg --ul NIFTY --from 2025-10-01 --to 2026-10-31 --capital 100000 \
  -p levels=pdhl -p sweep_start=09:45 -p entry_start=11:00
```
```bash
algo backtest -s london_liquidity_ifvg --ul NIFTY --from 2025-10-01 --to 2026-10-31 --capital 20000
algo backtest -s london_liquidity_ifvg --ul NIFTY --from 2025-10-01 --to 2026-10-31 -p tp_type=rr -p risk_reward=2
```

## Strategy: vwap_sd_fade (fade the VWAP +/- 3 sd bands)

VWAP of NIFTY **spot** weighted by front-month futures volume, with volume-weighted standard-deviation
bands (`vwap_bands` in `indicators.py`, reset daily). A 5-min close beyond VWAP +/- `band_sd` x sd
(default 3) followed by the first close back inside (`confirm=reentry`; `touch` enters on the stretch
close) buys an ITM1 option toward VWAP: PE after an upper-band stretch, CE after a lower one.
Stop = stretch extreme +/- 5 pts; target fixed at entry = VWAP + `target_sd` x sd in the trade
direction (0 = VWAP, -1 = the near 1 sd band), skipped if under `min_rr` x risk. A stretch longer than
`max_stretch_bars` closes is treated as a trend day (no fade). Entries 10:00-14:30, exit 15:00.
Needs futures volume, i.e. Sep 2024 onward (`require_volume`). Every exit is in the index plan, so
`algo sweep --mode index` is exact.

**Result: no edge.** Index mode, train Oct 2024-Sep 2025, test Oct 2025-Oct 2026, 12 configs
(`band_sd` 2/2.5/3 x `target_sd` 0/-1 x `confirm`):

- 3 sd fires only ~10 times a year. The best train config (+1.24R, 7 trades) scored -0.11R out of sample.
- 2 sd has enough trades (250-540 a year) and loses on both halves (-0.04 to -0.20R).
- The best out-of-sample row, 2.5 sd touch (+0.22R on 84 trades), has a CI of [-0.37, +0.88].
- The one-lot option backtest, Oct 2024-Oct 2026 at the defaults: 26 trades, +Rs 4.5k, PF 1.29, per-trade CI [-450, +862].

```
algo backtest -s vwap_sd_fade --ul NIFTY --from 2024-10-01 --to 2026-10-31 --no-risk -p size_from_capital=false
```

## Strategy: fvg_flow (1-min FVG retest + order-flow confirmation)
- **Gap:** a 3-bar fair value gap on 1-min NIFTY spot, at least `min_gap_pts` (3) wide. Gaps expire after `max_age_min` (30), and a close through the far edge kills a gap.
- **Entry:** a later bar dips into the gap and closes back out on its side. A bullish gap buys a CE, a bearish one buys a PE.
- **Confirmation:** the 1-min front-month future delta (last `flow_bars`) must agree, with an optional `min_delta` and `require_cum_delta`. Backtests have no order flow, so there the confirmation is skipped (`skips["no_flow_data"]`).
- **Plan (on the index):** stop at the far edge of the gap ± `sl_buffer_pts`, target `target_r` (2R), breakeven at 1R, square-off at 15:15.
- **Entry window:** 09:30–15:00. The strategy overrides the global `risk.no_entry_before/after` through its `risk_overrides`, raises `max_trades_per_day` to 100 (effectively no cap), and turns off the daily loss limit and the consecutive-loss stop, so it trades all day. The 50% capital kill switch and the per-trade risk caps still apply.
- **Option:** starts at `moneyness` OTM1 and steps further OTM (up to `max_otm_steps`) while the premium is above `max_premium` (₹150). `max_premium` works for every index-plan strategy.
- **FVG-only backtest** (Jun–Oct 2025, ₹10k): 51 trades, PF 0.68. The index result was +0.13R/trade but not significant (CI −0.26..+0.49), and the kill switch fired on 2025-07-24. The edge, if any, has to come from the order-flow filter, which can only be measured in paper trading or on recorded `data/footprint` ticks.

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
- `skip_weekdays: [fri]`
- `skip_days_before_monthly_expiry: 2`: the monthly expiry day and the 2 calendar days before it

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

## Strategy: vwap_pullback_spread
Trades a trend-day pullback to VWAP/EMA20, entering with a 50-pt debit spread (`-p instrument=option` uses a single ATM option instead). Exits:
- **Target:** spread worth 75% of its width
- **Stop:** −40% of the debit
- **Thesis failed:** NIFTY closes back through VWAP
- **Time:** 15:00

**Status (NIFTY, ₹20k, risk on): fails.** Dev Oct 2024–Jun 2025: −₹42k on 179 trades. Hold-out Jul–Oct 2025: −₹11.5k on 67 trades. Even with zero slippage and zero charges it only breaks even (PF 1.01), so there's no edge.

## Slippage assumption
`execution.slippage_min: 0.5` (₹ per leg) is pessimistic for liquid NIFTY ATM options; real bid-ask is often ₹0.05–0.20. It matters a lot for spreads (4 legs): on vwap_pullback_spread, ₹0.1 slippage halved the loss. Calibrate it from real fills during paper trading before trusting any result.

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
src/algo/strategies/  ema_cross_buy, orb_buy, vwap_volume_buy, failed_breakout, vwap_pullback_spread, orb_vwap_trend,
                      short_premium, overnight_credit, vwap_trend, london_liquidity_ifvg, vwap_sd_fade (+ index_plan helper)
```

## Tests
`pytest` runs the suite on synthetic data (no API needed). It covers the cost calculations, fills and SL/target logic, no-lookahead checks, strike selection, and both strategies end to end.

## Roadmap
- Paper trading: a `PaperBroker` that uses live Upstox market data (WebSocket) with simulated fills
- Live trading: an `UpstoxBroker` that implements the same `Broker` interface
- Daily option-chain recorder, to keep our own history independent of Upstox
