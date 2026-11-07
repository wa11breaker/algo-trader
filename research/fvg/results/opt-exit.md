# fvg_flow exit-side optimisation (opt_exit), 2026-11-05

Scripts: research/fvg/opt_exit_grid1.json, opt_exit_grid2.json (grids), research/fvg/opt_exit_rs.py (rupees via run2d, 50k).
Raw: results/opt_exit-*.csv/.log. Holdout never touched. Grid2 (sl_buffer 8/10, x tp_atr 5/6, + trail/BE combos)
history run was still in progress at hand-off: see results/opt_exit-g2-{train,val}.log when finished.

## Baseline (current tuned defaults)
| period | n | per_day | netR | t |
|---|---|---|---|---|
| train 2022-23 | 5563 | 11.3 | -0.101 | -6.2 |
| val 2024-25H1 | 3779 | 10.2 | -0.104 | -5.2 |
| Nov 4 (in-sample) | 7 | - | sum +10.8 | |
| Nov 5 (OOS) | 5 | - | sum +2.86 | |
Rupees (run2d, 50k): Nov4 +13,134, Nov5 +3,390, total +16,524.

## Grid1 (one change at a time from defaults)
| change | train netR | val netR | Nov4 sumR | Nov5 sumR | Rs total (N4/N5) |
|---|---|---|---|---|---|
| baseline | -0.101 | -0.104 | 10.8 | 2.86 | 16,524 (13,134/3,390) |
| sl_buffer_pts 4 | -0.089 | -0.094 | 8.94 | 2.10 | 18,880 (14,387/4,494) |
| sl_buffer_pts 6 | **-0.062** | **-0.083** | 7.57 | 1.55 | 18,444 (14,867/3,577) |
| sl gap_atr 0.25 | -0.105 | -0.107 | 9.81 | 2.88 | 18,880 |
| sl gap_atr 0.5 | -0.093 | -0.093 | 7.63 | 2.13 | 18,152 |
| sl atr 1.5 | -0.108 | -0.093 | 7.79 | 2.64 | 15,680 |
| tp_atr 3 | -0.103 | -0.101 | 7.19 | 1.37 | 2,866 |
| tp_atr 5 | -0.079 | -0.096 | 4.84 | 4.25 | 14,767 (7,320/7,447) |
| tp_atr 6 | -0.073 | -0.089 | 0.09 | 5.64 | 4,512 |
| tp r 2 / r 3 | -0.106 / -0.083 | -0.103 / -0.101 | 6.84 / 6.00 | 1.20 / 3.20 | -616 / 9,018 |
| tp liquidity 1-4R | -0.112 | -0.118 | 5.69 | 3.38 | 11,039 |
| breakeven off | -0.099 | -0.120 | 9.80 | 1.86 | 13,739 |
| breakeven 1.0 | -0.110 | -0.113 | 10.8 | 1.86 | 14,350 |
| breakeven 1.5 | -0.091 | -0.121 | 10.8 | 1.86 | 14,350 |
| trail 1.0 ATR from 1R | -0.069 | **-0.074** | 4.16 | 1.79 | -3,667 |
| trail 2.0 ATR from 1R | -0.089 | -0.107 | 3.78 | 2.86 | -1,059 |
| exit_time 14:30 | -0.111 | -0.116 | 10.82 | 2.86 | 17,147 |

## Grid2 two-day results (history pending)
sl_buffer 8: Nov4 6.51 / Nov5 1.12 R, Rs 17,513. sl_buffer 10: 5.67 / -0.21, Rs 16,336.
sl6+tp5: 3.24 / 2.57, Rs 16,687. sl6+tp6: -0.13 / 3.59, Rs 6,431. sl6+trail1.0: 2.61/1.47, Rs 401.
sl6 + BE off or 1.0: 7.6 / 0.55, Rs ~15.4k.

## Findings
- Breakeven 0.5R: no alternative (off/1.0/1.5) beats it on both train and val; keep it. It does not cut winners on
  history in a way that pays: removing it loses on val and on Nov 5.
- Wider gap stop (sl_buffer 4-6) is the only change better on train AND val with neighbours OK (4, 6, gap_atr 0.5
  all improve). Mechanism: cost (2 pts) is a smaller fraction of a wider risk; mean gross R actually falls.
  It lowers index sum_netR on both days (Nov5 2.86 -> 1.55), but rupees are equal or better (+Rs 1.9k over the two
  days at 50k, smaller worst loss -4,990 vs -6,288) because size scales down with risk.
- Bigger ATR target (5-6) and a 1-ATR trail improve history but clearly hurt Nov 4 and rupees (trail turns +16.5k
  into -3.7k). Rejected under the protocol.
- liquidity targets, R-multiple targets, exit_time 14:30, atr stop: no improvement.
- Every variant still loses after costs on history (best val -0.074). Exit tweaks shave ~0.02-0.04R; they do not
  make the strategy profitable.

## Index R vs option rupees
Index R and rupees disagree on stop width: sizing divides by risk, so a wider stop changes R sums but not much
rupees. Index replay also exits AT the stop level; the engine exits at the bar price. Nov 5 13:55: stop 24540.8,
the 13:55 bar fell to 24528.8 and closed at the low, so the option lost 15 premium pts on a 15-pt planned risk
(planned ~6 at delta_est 0.4) -> fast-bar slip-through plus real delta ~0.5 for the <=Rs150 strike.
delta_est 0.4 understates risk for these strikes (sizing lane: suggest 0.5 or size off realised delta).

## Recommendation
Tentative: sl_buffer_pts 2 -> 4 (conservative; 6 is the stronger history result). Train -0.101 -> -0.089,
val -0.104 -> -0.094, Rs two days 16.5k -> 18.9k, but Nov 5 index sum_netR drops 2.86 -> 2.10, so strictly it fails
the "does not hurt Nov 5" rule in R terms while passing in rupees. Otherwise keep defaults.
Untested: grid2 history (pending), tick-level stop checks (would cut slip-through), option-premium-based stops,
time-stop (exit if no progress after N min), partial exits, per-direction exits.

## Grid2 train (arrived after hand-off; val pending in results/opt_exit-g2-val.log)
netR train: sl10+tp5 -0.031, sl8+tp6 -0.033, sl6+tp5+trail1.5 -0.037, sl6+trail1.0 -0.039, sl10 -0.040,
sl6+tp6 -0.042, sl6+BE1.0 -0.046, sl8+tp5 -0.046, sl8 -0.053, sl6+tp5 -0.053, sl6+BE off -0.063 (baseline -0.101).
Wider stops keep improving on train (monotonic 2->10). Most of these hurt Nov 4/5 (see grid2 two-day table);
sl8 alone is the most day-neutral (Rs 17.5k vs 16.5k). Confirm on val before adopting anything wider than 4-6.
