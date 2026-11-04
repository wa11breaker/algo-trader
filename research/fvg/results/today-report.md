# fvg_flow replay report: 2026-11-04 (real spot candles + recorded order flow)

Harness: `research/fvg/today.py`, index-only. R = result in units of the planned risk; net R charges 2 index
points of cost per trade. With the defaults it reproduces all 9 live paper entries, plus 3 the live bot
missed (09:33 before start, 10:09 daily-loss block, 11:40 just after a restart).

| Settings | Trades | Wins | Net R (day) | Index pts | Longs R | Shorts R | Max drawdown R |
|---|---|---|---|---|---|---|---|
| Defaults | 12 | 5 | +2.40 | -2.9 | +3.36 | -0.96 | 2.57 |
| Order flow: min_delta_ratio 0.1 | 11 | 5 | +3.53 | +12.7 | +4.49 | -0.96 | 2.14 |
| Gaps: min_gap_atr 0.45 + trend ema | 9 | 5 | +5.84 | +41.5 | +4.49 | +1.35 | 2.14 |
| Best on history: gap 6, buffer 10, 3R, no breakeven | 6 | 4 | +6.62 | +188.3 | +1.85 | +4.77 | 1.06 |
| Exits: tp_atr 4 + breakeven 0.5R | 10 | 4 | +8.37 | +88.1 | +1.46 | +6.90 | 2.24 |
| **Combined (gaps + exits + flow)** | **7** | **4** | **+10.80** | **+122.2** | +2.59 | +8.21 | 1.09 |

Combined, per trade: 09:33 long -1R (stop) | 10:22 short +5.5R | 11:04 short -1R | 11:40 short +4.2R |
12:34 long +2.0R | 13:12 long +2.1R | 14:47 long 0R (breakeven).
Dropped versus the defaults: 10:00, 10:09 and 12:20 (small gaps / weak delta, all losers), plus 10:28 and
11:49, which the combined setting skips because it is still in the 10:22 and 11:40 trades.

Caveats: one day only, a trending one. On 2024-01..2025-06 history the combined setting still loses
(-0.10R/trade; -0.03R with estimated order flow). Rupee P&L is not modelled: option premium, theta
and the Rs150 OTM rule are not in the replay.
