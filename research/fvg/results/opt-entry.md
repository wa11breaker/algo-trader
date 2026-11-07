# fvg_flow entry-side optimisation (2026-11-05) — PARTIAL

Defaults = tuned set (min_gap_atr 0.45, trend ema, tp atr 4, BE 0.5R, min_delta_ratio 0.1). One-at-a-time changes from defaults.
Grids: opt_entry_grid1.json (FVG/trend/age/window), opt_entry_grid2.json (flow), opt_entry_grid3.json (absolute gap).
CSVs: opt_entry-day-*.csv, opt_entry-dayflow-*.csv, opt_entry-daygap-*.csv, opt_entry-train1.csv;
val (opt_entry-val1.csv) and flowdev+flow_proxy (opt_entry-flowdev2.csv) were still running at hand-off.

## Baseline (defaults)
| period | n | per_day | netR | t |
|---|---|---|---|---|
| Nov 4 (in-sample) | 7 | - | sum +10.80 | |
| Nov 5 (OOS) | 5 | - | sum +2.86 | |
| train | 5563 | 11.3 | -0.101 | -6.2 |
| val | 3779 | 10.2 | -0.104 | -5.2 |

## One-at-a-time results (Nov4 sum / Nov5 sum / train netR)
| change | Nov4 | Nov5 | train |
|---|---|---|---|
| defaults | 10.80 | 2.86 | -0.101 |
| min_gap_atr 0.30 / 0.35 / 0.40 | 10.6 / 10.6 / 10.8 | 2.86 all | -0.101 / -0.097 / -0.094 |
| min_gap_atr 0.50 / 0.55 / 0.65 / 0.80 | 10.8 / 10.8 / 1.3 / 1.3 | 0.81 / 0.81 / 1.94 / 1.70 | -0.100 / -0.104 / -0.103 / -0.107 |
| min_gap_pts 5 / 6 / 7 / 8 / 10 | 10.8 / 10.8 / 4.9 / -0.5 / -0.4 | 0.81 / 1.94 / 1.60 / 1.60 / -0.19 | -0.077 / - / - / -0.040 / - |
| disp_atr 0.5 / 1.0 | 10.8 / 8.8 | 0.81 / 1.94 | -0.103 / -0.115 |
| retest_depth mid | -2.6 | -1.1 | -0.056 |
| trend none | 9.7 | -1.71 | -0.116 |
| trend vwap | 6.7 | 2.86 | -0.087 |
| max_age 15 / 20 / 45 / 60 | 10.8 all | 2.86 all | -0.104 / -0.104 / -0.099 / -0.099 |
| entry_start 09:45 | 11.9 | 2.86 | -0.101 |
| entry_end 14:30 / 14:00 | 11.0 / 11.0 | 2.86 | -0.102 / -0.103 |

Flow (real delta, days only; history pending): min_delta_ratio null/0.05/0.15 = defaults on Nov4 (10.8), but null/0.05 give
Nov5 +7.69 (adds a winner); 0.2 -> 8.6 / 2.86; 0.3 -> 4.3 / 4.1. flow_bars 2 -> 7.8 / 8.8; flow_bars 3 -> 5.3 / 12.4
(Nov5 was a one-way up day). delta_z 0.5/1.0 -> ~-0.5 / 4.0, 0.8 (bad). require_cum_delta -> 7.1 / 4.0.

## Findings
- EMA trend filter is pulling its weight: removing it adds 4 losers on Nov 5 (-1.71 vs +2.86) and is the worst train row.
- Absolute gap size (min_gap_pts 8) and retest_depth mid are the best on train but cut good trades on both real-flow days
  (fail criterion a). min_gap_pts 5-6 is a possible mild plateau on train, but costs Nov 5 0.9-2 R.
- min_gap_atr 0.40-0.50 is a flat plateau on the days; 0.40 is marginally better on train (-0.094) — noise-level.
- Window and max_age: no effect beyond noise.
- Flow: min_delta_ratio 0.1 vs off/0.05 costs one Nov 5 winner; flow_bars 2 trades Nov4 for Nov5 — mixed, needs flowdev.

## Recommendation (provisional, val + flowdev not yet read)
Keep defaults. No entry change passes all of: no harm on Nov 5, small harm on Nov 4, better on train AND val.
Untested / pending: val for grid1, flowdev(flow_proxy) for flow params, combos, per-year splits.

## Addendum: val results arrived after hand-off (opt_entry-val1.csv)
val netR: defaults -0.104 | min_gap_atr 0.30 -0.095, 0.35 -0.090, 0.40 -0.090, 0.50 -0.116, 0.55 -0.122 |
min_gap_pts 5 -0.091, 8 -0.053 | retest mid -0.045 | trend vwap -0.076, none -0.109 | disp 0.5 -0.101, 1.0 -0.113 |
max_age 15/20/45/60 -0.112/-0.111/-0.102/-0.102 | entry_start 09:45 -0.106 | entry_end 14:30/14:00 -0.111/-0.106.

Update: min_gap_atr 0.40 is the one change that meets every criterion so far — identical trades on Nov 4 and Nov 5,
better on train (-0.094 vs -0.101) and val (-0.090 vs -0.104), neighbours 0.30-0.35 also better on both. Effect is small
(+0.01 R/trade, ~0.3 extra trades/day) and history still loses after costs. Weak candidate: min_gap_atr 0.40.
mid retest / min_gap_pts 8 / vwap win on train+val but fail on the real-flow days (see above). flowdev still pending.
