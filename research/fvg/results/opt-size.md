# fvg_flow: position sizing and strike selection (opt-size)

Files: `research/fvg/opt_size_strat.py` (subclass `FvgSize`), `research/fvg/opt_size_eval.py` (runner),
`results/opt-size-runs.jsonl` (one summary line per run), `results/opt-size-<name>-<2d|hist>.csv` (per trade).

Setup: capital Rs 50,000, cap = 6% of equity per trade (Rs 3,000).
- **2d** = the two recorded days (run2d: real Fyers option candles and footprint), compounding as in live.
- **hist** = 2024-10-01..2025-06-30, local Upstox store, `flow_proxy: true`. Each trade is sized off a
  fixed Rs 50k, with the kill switch and withdrawal off, so trades are comparable and none are dropped.
- "over cap" means realised loss > 6% of equity at entry. "ratio" means realised loss / planned loss.

## Root cause
`_buy_with_plan` sizes from `min(index_risk x delta_est, premium x option_sl_pct)`, which with defaults is
almost always `index_risk x 0.4`. That number is not a bound:
1. **The effective delta is higher than 0.4.** On stopped trades the option moved 0.49 x the index move
   (median), 0.54 for 100-150 premiums, and the IQR is 0.45-0.66. The index stop also fills late: the
   median exit is at -1.08R because the exit fills at the next bar's open, plus slippage and costs.
2. **The option safety stop (40% of premium) sits far beyond the planned loss**, often 5-7x for small index
   risk. Anything the index stop does not catch can run to 40% of premium x lots: an IV crush, a
   premium drop while the index is flat, a breakeven exit after theta, or a gap. The history worst case was
   2025-04-17. Premium went 73.5 -> 43.7 (an option SL hit) while the index was +0.16R. The trade lost
   Rs 15,771 (31.5%) against a planned Rs 2,950.
3. **Cheap expiry-day options (< Rs 20)** add a fixed 0.5 slippage plus costs, which is 5-10% of the
   premium. They sized at 9-10 lots and overshot the plan by 15-20%.

## Results (all variants run)
| variant | 2d Rs | 2d worst | hist Rs | hist worst (% eq) | hist over cap | loss ratio p90 / max | avg lots |
|---|---|---|---|---|---|---|---|
| **baseline** (index, delta 0.4, opt SL 40%) | +16,524 | -6,288 | -416,418 | -15,771 (31.5%) | 19.5% | 1.95 / 5.71 | 6.8 |
| delta_est 0.6 | +18,938 | -4,990 | -333,939 | -9,022 (18.0%) | 9.5% | 1.33 / 3.81 | 5.8 |
| delta_est 0.7 | +20,071 | -3,997 | -307,661 | -9,022 (18.0%) | 6.5% | 1.16 / 3.26 | 5.4 |
| delta_est 0.8 | +20,619 | -3,004 | -280,518 | -6,772 (13.5%) | 3.9% | 1.08 / 3.22 | 5.0 |
| size from option SL 40% | 0 trades | – | -104,986 | -3,476 (7.0%) | 1.2% | 0.95 / 3.22 | 2.9 (skips 25%) |
| ATM, cap 250, delta 0.6 | +17,037 | -4,820 | -331,486 | -8,138 (16.3%) | 14.6% | 1.52 / 5.1 | 5.4 |
| ITM1, cap 350, delta 0.7 | +18,444 | -3,636 | -336,931 | -8,739 (17.5%) | 13.6% | 1.53 / 5.0 | 4.8 |
| matched d0.6 k1.0 | +21,528 | -3,287 | -340,342 | -3,476 (7.0%) | 6.6% | 1.13 / 3.22 | 5.8 |
| matched d0.8 k1.0 | +21,054 | -2,556 | -292,552 | -3,476 (7.0%) | 3.0% | 1.08 / 3.22 | 5.0 |
| matched d0.5 k1.5 | +19,697 | -3,218 | -312,566 | -3,476 (7.0%) | 4.2% | 1.11 / 3.22 | 5.1 |
| matched d0.6 k1.5 | +18,363 | -2,858 | -260,883 | -3,476 (7.0%) | 1.8% | 1.06 / 3.22 | 4.6 |
| matched ATM d0.6 k1.5 | +17,481 | -2,890 | -262,530 | -3,525 (7.1%) | 3.5% | 1.08 / 1.55 | 4.3 |
| **C1** matched d0.6 k1.2 buf 0.15 | **+20,494** | -2,332 | -277,709 | -3,077 (6.15%) | 0.26% (2) | – / 2.8 | 4.8 |
| C2 matched d0.5 k1.5 buf 0.15 | +20,315 | -2,420 | -274,593 | -3,032 (6.06%) | 0.13% (1) | – / 2.8 | 4.7 |
| matched d0.6 k1.5 buf 0.15 | +18,578 | -1,913 | -230,437 | -3,032 (6.06%) | 0.13% (1) | – / 2.8 | 4.2 |
| **C3** matched d0.6 k1.5 buf 0.10 min_prem 20 | +16,381 | -2,858 | -220,486 | -2,991 (5.98%) | **0%** | – / 1.04 | 3.9 |

"Matched" means the option safety stop is moved to the planned loss:
`sl_pct = min(option_sl_pct, 100 x sl_k x index_risk x delta_est / premium)` (floor 5%). Lots are then sized
from that stop: `per_lot = premium x sl_pct x (1 + loss_buf) x lot_size`. The resting option SL becomes
the hard bound on the loss, and the index stop usually fires first.

Max drawdown on hist mostly tracks the strategy's negative edge. With flow_proxy the strategy loses on
2024-10..2025-06 at every sizing: about -Rs 105 to -123 per lot per trade, 742 trades, win rate 26%. So
hist Rs and max DD mainly show how much size each variant takes; the max DD values are in the jsonl.
On a per-lot basis the matched stop does **not** cost edge: base -113, C1 -106, C3 -109 Rs per lot-trade.
Extra check, 2024-01..09 without flow (60 trades): baseline ratio max 2.93, C1 1.00, C3 1.02; none over the cap.

Strike selection: ATM or ITM1 with a higher premium cap did **not** help on its own (14% over cap in hist),
because the problem is the loss model, not gamma. ATM with the matched stop is no better than OTM with the
matched stop. Keep OTM1 stepping out under the 150 cap. Add a 20 premium floor to drop the costly
lottery-ticket expiry-day strikes; in hist it also improved the per-trade return.

## Recommendation
C3 keeps the cap strictly (0 of 693 hist trades over 6%, worst 5.98%, worst 2d trade -2,858). The 2d
total is about the same as baseline (+16.4k vs +16.5k) on fewer lots. C1 keeps more of the 2-day upside
(+20.5k) at a 0.26% over-cap rate (worst 6.15%). I suggest C3. Pick C1 if a few tenths of a percent
over 6% is acceptable.

Minimal code change in `src/algo/strategies/index_plan.py::_buy_with_plan` (replace the `per_lot_risk` line):
```python
        if p.get("min_premium") and ltp < p["min_premium"]:
            self.skips["too_cheap"] += 1
            return False
        sl_pct = p["option_sl_pct"]
        if p.get("sl_k"):        # option safety stop placed at the planned loss; size from it
            sl_pct = max(p.get("min_sl_pct", 5.0), min(sl_pct, 100 * p["sl_k"] * risk * p["delta_est"] / ltp))
            unit = ltp * sl_pct / 100
        else:
            unit = min(risk * p["delta_est"], ltp * sl_pct / 100)
        per_lot_risk = unit * (1 + p.get("loss_buf", 0.0)) * opt.lot_size
        ...
        order = ctx.buy(opt, lots=lots, sl_pct=sl_pct, ...)      # was sl_pct=p["option_sl_pct"]
```
Changes to the `fvg_flow` defaults: `delta_est: 0.6, sl_k: 1.5, loss_buf: 0.10, min_premium: 20.0` (C1:
`sl_k: 1.2, loss_buf: 0.15`, no min_premium). Other IndexPlanMixin strategies keep their behaviour, since
`sl_k` defaults to None for them.

## Untested
- Live and paper fills. Paper uses SimBroker brackets on ~1 s Fyers snapshots. The live -7,910 stop at
  100k was not replayed, and real gap-through on a resting SL can still exceed the cap.
- The holdout (2025-07+), which is locked. Hist used the flow proxy, not footprint.
- Interaction with the parallel entry, exit and session changes. A tighter option stop adds "sl" exits
  (44 -> 50 for C3, 131 for C1).
- Compounding path effects and the max_lots=10 cap. Hist sized off a fixed 50k.
