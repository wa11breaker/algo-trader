# fvg_flow on 2026-11-04: dynamic stop loss / take profit (agent B)

One day with real tick delta. FVG and flow settings stay at their defaults; only the exit params change.
Metric: sum_netR = Σ(R − 2 pts / risk). **This is a single trending day with about 10–12 trades. Everything
below is fitted to this day and needs to be checked on more recorded days before it goes live.**

Baseline (defaults: gap stop with a 2-pt buffer, 2R target, breakeven at 1R): 12 trades, 5 wins,
**sum_netR +2.40**, −2.9 index pts gross, max DD 2.57R.

## Iterations
| # | file | what | configs |
|---|---|---|---|
| 1 | today-b-1.csv | broad: sl_mode × (buffer 1–15 / sl_atr 0.5–3) × tp_mode r / atr / liquidity × breakeven {null, .5, 1, 1.5} | 1520 |
| 2 | today-b-2.csv, today-b-2t.csv | refine: ATR stop 0.3–1.3 × tp_atr 2.5–5; gap buffer 0–6 × tp_atr / target_r; gap_atr 0.25–1.75 × r / liquidity; trailing 0.5–3 ATR × start 0.5–2 on six bases | 1182 + 1152 |
| 3 | today-b-3.csv (today-b-stab.py) | stability: ±1–2 steps on each param, every breakeven value, cost 3 / 4 pts, entry 1 pt worse, sum without the best trade | ~80 |

## The structure of the day (it explains every result)
Four trades carry the day: **10:22 short, 11:40 short, 12:34 long, 13:12 long**. All of them ran 4+ ATR
without much pullback. Five trades lose a full −1R in *every* exit config tested: 09:33 long, 10:00 short,
10:09 long, 11:04 short and 12:20 short. No stop placement rescues them, because they are entry
problems. So exit tuning can only (a) let the four runners go further and (b) make the losers cheaper.

A far target also changes **which** trades are taken, because the replay holds one position at a time.
When the 10:22 short is still open at 10:28, the default's 10:28 short is skipped, and that trade was a
−1R loser (39.5-pt risk). Likewise, the 11:49 short (0R in the default) is skipped while the 11:40 short is
still running.

## Top 3

### 1. Tight ATR stop + 4 ATR target: highest score, fragile mechanism
`{"sl_mode":"atr","sl_atr":0.5,"tp_mode":"atr","tp_atr":4,"breakeven_at_r":1}`

| n | wins | sum_netR | netR | pts | cost 3 | cost 4 | entry +1pt slip | without best trade | max DD |
|---|---|---|---|---|---|---|---|---|---|
| 10 | 4 | **23.36** | 2.34 | +142 | 21.5 | 19.7 | 18.2 | 15.7 | 2.62 |

How the exits change:
- Risk falls from a median of 15.5 pts to **5.9 pts** (3.5–9.2), and the target is about 8R.
- The four runners each make **+8R** instead of +2R. 10:22 runs to 23817 (default exit 23853), and 12:34 is the same trade with a 6-pt stop instead of 23.5.
- The losers lose the same −1R but far fewer points: 09:33 loses 9 pts instead of 31, and 11:04 loses 7 instead of 23.
- 10:28 and 11:49 are skipped (still in the 10:22 / 11:40 trades).
- 14:47 gives up the default's +2R. Its 4 ATR target (23938) is never reached, so it exits at breakeven.

Plateau: every cell of sl_atr 0.3–0.8 × tp_atr 3.5–5 scores ≥ +8.9 (today-b-2.csv). Neighbours:
sl_atr 0.4 / 0.6 give 29.5 / 17.6, and tp_atr 3.75 / 4.5 give 20.4 / 17.9. breakeven 1 and 1.5 are identical.
**Caveat:** the score rises steadily as the stop tightens (0.3 ATR, about 3.5 pts, scores 41) with no
interior peak. That is the signature of R inflation on a day where the winners never retraced 0.5 ATR,
not of a real optimum. A 4–6-pt index stop sits inside one 1-min bar's noise and option spread. On a
choppy day it will be hit far more often. A safer member of the same family is **sl_atr 1.0** (9.18;
neighbours 0.8–1.2 score 5.8–11.7, risk about 12 pts).

### 2. Gap stop (2-pt buffer) + 4 ATR target + breakeven at 0.5R: best broad plateau
`{"sl_mode":"gap","sl_buffer_pts":2,"tp_mode":"atr","tp_atr":4,"breakeven_at_r":0.5}`

| n | wins | sum_netR | netR | pts | cost 3 | cost 4 | slip 1 | without best | max DD |
|---|---|---|---|---|---|---|---|---|---|
| 10 | 4 | **8.37** | 0.84 | +88 | 7.7 | 7.0 | 6.1 | 3.1 | 2.24 |

How the exits change (same stops as the live bot, only the target and breakeven differ):
- 10:22 short: +5.5R (target 23817) instead of +2R. Being still in this trade skips the 10:28 loser (+1R saved).
- 11:40 short: +4.2R instead of +2R. Being still in this trade skips the 11:49 trade (0R in the default).
- **12:20 short: breakeven at 0.5R moves the stop to entry. It exits at 0R instead of −1R.**
- 12:34 / 13:12 longs: about +2R either way (4 ATR ≈ 2R on their wider gap stops).
- 14:47 long: 0R (breakeven) instead of +2R. This is the price of the far target.

Plateau: with breakeven 0.5, **every** cell of buffer 1–4 × tp_atr 2.5–4.5 is positive (2.5–9.5), and
buffer 0–6 × tp_atr 3.5–4.5 scores 3.0–9.5. Neighbours: buffer 1 / 3 / 4 give 9.5 / 6.4 / 5.6, and
tp_atr 3.5 / 3.75 / 4.25 / 4.5 give 6.6 / 7.5 / 6.9 / 7.8. Breakeven null / 1 / 1.5 give 6.4 / 7.4 / 7.4.
Positive under all stresses, including removing the best trade. Weak spot: tp_atr ≥ 4.75 with breakeven 0.5 drops to about 2.

### 3. Gap stop (3-pt buffer) + 4R target + breakeven at 0.5R: same idea with a plain R target
`{"sl_mode":"gap","sl_buffer_pts":3,"tp_mode":"r","target_r":4,"breakeven_at_r":0.5}`

| n | wins | sum_netR | netR | pts | cost 3 | cost 4 | slip 1 | without best | max DD |
|---|---|---|---|---|---|---|---|---|---|
| 10 | 3 | **5.77** | 0.58 | +86 | 5.2 | 4.5 | 4.9 | 1.9 | 2.22 |

How the exits change:
- 10:22 / 11:40 / 12:34: +4R each instead of +2R. 12:34 rides to 23899, so the 13:12 long is skipped (−2R vs the default).
- The 10:22 target (23828) is hit before 10:28, so the 10:28 short *is* taken. Breakeven at 0.5R saves it (0R instead of −1R).
- 14:47 long: 0R (breakeven) instead of +2R.

Plateau: with breakeven 0.5, buffer 1–6 × target_r 3.5–4.75 scores 4.3–8.9 everywhere. Neighbours:
buffer 1 / 2 / 4 / 5 give 6.6 / 6.7 / 5.9 / 6.0, and target_r 3.75 / 4.25 / 4.5 give 5.0 / 6.5 / 7.3. Breakeven null / 1 give 4.9 / 4.8.
Below 3R the scores collapse (target_r 2.25–3 with buffer ≥ 3 is negative), because the runners get cut.

Rejected finalists (fail the neighbour test): gap_atr 1.0 + liquidity (tp_min_r 1, tp_max_r 4) scores 5.75
but falls to 2.2 / 2.7 at tp_min_r 1.5 / 0.5 and to 1.6 with breakeven 0.5. gap_atr 0.75 + 3R scores 5.4 but
falls to −1.5 / 3.2 at sl_atr 0.55 / 0.65 and to 0.3 at 2.5R.

## Cheapest single change
Defaults with only **breakeven_at_r 0.5** score **+4.40** (vs +2.40). The 12:20 short exits at 0R instead
of −1R, and 10:28 / 11:49 are scratched at breakeven. It is robust to cost (3.6 at 3 pts) and to slippage (2.4).

## What clearly hurts
- **Near targets**: tp_atr 1 (median −0.7), target_r 1 and liquidity with tp_min_r 0.5. They cut the four runners at 1R or less.
- **Wide stops**: sl_mode atr with sl_atr 2.0 (median −0.7) and gap_atr with sl_atr ≥ 2.5. The losers cost more points, and R targets move out of reach.
- **Trailing stops never beat a fixed far target.** On the best base (atr 0.5 / tp_atr 4), trail_atr 2.5–3 scores 17–19 vs 22.4 without a trail, and trail_atr 1–1.5 scores 1.8–4.6. On gap bases, trail 0.5 ATR is about neutral and 1–2 ATR is worse. trail_start_r rarely matters, because the trail distance dominates.
- **Breakeven 0.5 with tight ATR stops or liquidity targets**: atr 0.5 / tp_atr 4 falls from 22.4 to 14.6, and gap_atr / liquidity falls from 5.75 to 1.6. The runners get scratched at entry before they leave.
- tp_atr 3.0 is a consistent dip in every stop family (one runner just misses its target), so avoid targets of exactly 3 ATR.

## Caveats
- Everything here comes from 4 winning trades on one trending day. The "without best trade" column shows how much depends on any single trade. Configs 2 and 3 stay positive; config 1 stays highly positive only because all four runners pay 8R.
- Before adopting a tight ATR stop (config 1), test it on the multi-year index data (eval.py, dev/val). I expect it to fail on choppy days.
- Configs 2 and 3 keep the live bot's stops and only widen the target and add an earlier breakeven. That is the least-fitted change.
