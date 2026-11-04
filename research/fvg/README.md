# fvg_flow rework: research protocol

Goal: make `fvg_flow` (src/algo/strategies/fvg_flow.py) profitable after costs by reworking the FVG
definition, the order-flow confirmation and the stop/target, without overfitting.

## Tools
- `eval.py`: index-only replay of fvg_flow for a grid of params (see its docstring). One config over
  2022-01..2025-06 takes about 15-20 s CPU. Use `--workers 2`; three agents share 8 CPUs.
- `proxy_check.py`: the close-location delta proxy matches recorded tick delta in sign 68% of bars
  (76% on bigger bars), corr 0.50, on the one recorded day (2026-11-04).

## Periods
| name | range | use |
|---|---|---|
| train | 2022-01 .. 2023-12 | fit |
| val | 2024-01 .. 2025-06 | check what train picked |
| dev | 2022-01 .. 2025-06 | robustness / stability |
| flowdev | 2024-10 .. 2025-06 | anything with `flow_proxy` (futures start 2024-09) |
| holdout | 2025-07 .. 2026-10 | LOCKED. Final phase only. Never run it. |

## Rules
- Metric: `netR` (R per trade after 2 index points of cost). Also look at `t`, trades/day, `netR_c3`
  (cost sensitivity), long vs short, and per-year results.
- Prefer a plateau, where neighbouring values are also good, over a single best point. Results must
  hold on both train and val.
- Fewer than about 150 trades in a period is too few to trust.
- Don't edit src/. If you need a feature that doesn't exist, describe it in your results file.
- Baselines (val): current defaults netR -0.130 (15 trades/day); gap 6 / buffer 10 / 3R / no
  breakeven netR +0.005 (4.9/day).

## Params (defaults = original behaviour)
FVG: min_gap_pts 3, min_gap_atr None, disp_atr None, retest_depth "edge"|"mid", trend "none"|"vwap"|"ema",
max_age_min 30, entry_start "09:30", entry_end "15:00".
Stop/target: sl_mode "gap"|"atr"|"gap_atr", sl_buffer_pts 2, sl_atr 1.0, tp_mode "r"|"atr"|"liquidity",
target_r 2, tp_atr 2, tp_min_r 1, tp_max_r 4, breakeven_at_r 1.0 (null = off), trail_atr None, trail_start_r None.
Flow: flow_proxy False, flow_bars 1, min_delta 0, min_delta_ratio None (|delta|/volume), delta_z None,
require_cum_delta False.
