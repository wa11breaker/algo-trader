# fvg_flow: session and trade management (opt-session)

Scope: time of day, day-level stops, max trades/day, cooldown, no-reversal, weekday/expiry, gap days.
Strategy = current tuned defaults. Holdout not touched.

## Method
- `opt_session_plans.py` dumps the strategy's `plan_log` + 1-min spot for train, val, flowdev
  (with `flow_proxy=True`, which is closer to the live flow-filtered stream) and Nov 4 / Nov 5 (real footprint flow).
- `opt_session_rules.py` re-implements `index_replay.replay` (same stop-first / target / breakeven logic)
  with a rule hook `allow(plan, now, today's closed trades)`. Skipping a trade frees the slot for the
  next plan, so it is exact and uses only past information. Baseline reproduces eval.py/today.py (Nov 4 +10.8R, 7 trades).
- `opt_session_analyse.py`: baseline breakdowns. `opt_session_grid.py`: 58 rules -> `results/opt-session-grid.csv`.
- Metric netR (cost 2 pts). Note: the baseline is negative (-0.10), so ANY rule that trades less cuts
  total R losses. Judge by netR per trade, not by sumR.
- Multi-year runs have no flow filter (no_flow_data); ~11 trades/day vs 5-7 live. flowdev_proxy: 6.6/day.

## Baseline
| period | trades | /day | netR | sumR |
|---|---|---|---|---|
| Nov 4 (in-sample) | 7 | 7 | +1.54 | +10.8 |
| Nov 5 (OOS) | 5 | 5 | +0.57 | +2.9 |
| train 22-23 | 5563 | 11.3 | -0.101 | -562 |
| val 24-25H1 | 3779 | 10.1 | -0.104 | -394 |
| flowdev proxy | 1248 | 6.6 | -0.035 | -43 |

## Breakdowns (train / val netR)
- By half hour: no bad slot that holds in both periods. Lunch 11:30-13:00 is bad in val (12:00 bucket -0.26) but
  12:00 is the best slot in train (+0.045). Last hour about average in both.
- By trade number of the day: flat. The first trade isn't better (train -0.149, val -0.014), and there's no decay with k.
- Weekday: no pattern that holds in both. Thursday (weekly expiry for all of train/val, holiday shifts ignored) is
  -0.098 train / -0.176 val.
- After a loss: a reversal is worse than continuing in the same direction (train -0.151 vs +0.004, val -0.229 vs -0.109). On flowdev_proxy it isn't worse (-0.060 vs -0.090).
- Cumulative day R before the trade: no monotonic pattern, so day stops and profit locks have nothing to exploit.
- Opening gap |open/prev close|: >0.6% days are better in both (train -0.045, val +0.008) vs <0.25% (-0.100, -0.117).

## Rules tried (netR; full table in opt-session-grid.csv)
| rule | train | val | flowdev | Nov4 / Nov5 sumR | verdict |
|---|---|---|---|---|---|
| baseline | -0.101 | -0.104 | -0.035 | 10.8 / 2.9 | |
| no reversal within 10/15/30/60 m | -0.103/-0.093/-0.096/-0.103 | -0.094/-0.091/-0.082/-0.078 | -0.036/-0.028/-0.038/-0.047 | 15m: 10.8/2.9; 30m: 8.9/4.0 | small +0.005..0.01, noise-level |
| no reversal after loss 3..999 m | -0.099..-0.109 | -0.083..-0.103 | 15m -0.038 | 15m: unchanged | no |
| cooldown after loss 2..30 m | -0.107..-0.125 (worse) | -0.081..-0.101 | 10m -0.033 | unchanged | fails train |
| stop after k losses (k=1..5) | -0.100..-0.143 | -0.085..-0.113 | k=2 -0.022 | k=1 hurts both days | no |
| stop after k non-wins | -0.104..-0.151 | -0.041..-0.116 | | | no |
| day stop -1/-2/-3/-4/-6 R | -0.094..-0.114 | -0.103..-0.115 | -2R -0.016 | -1R: -1.1 / -1.2 | no |
| lock profit +1..+6 R | -0.102..-0.116 | -0.099..-0.110 | +3R -0.040 | +1R: 4.2 / 1.9 | no |
| max 2/3/4/5/6/8 trades | -0.100..-0.115 | -0.047..-0.130 | 3: +0.007 | 3: 3.1 / 1.9 | fails train |
| skip 11:30-13:00 | -0.102 | -0.075 | +0.011 | 4.9 / 2.9 | val/flowdev only; train flat, lunch better in 3/4 train halves |
| skip 12:00-13:00 / 11:00-13:30 | -0.105 / -0.111 | -0.087 / -0.062 | +0.003 / +0.001 | | fails train |
| skip 09:30-10:00 / 10:30-11:00 / 14:00-15:00 / 14:30-15:00 | -0.108 / -0.089 / -0.103 / -0.102 | -0.110 / -0.105 / -0.106 / -0.111 | | | no |
| skip Mon..Fri (one weekday) | -0.093..-0.104 | -0.090..-0.114 | | | no (Thu: val only) |
| only days with abs. gap > 0.25 / 0.4 / 0.6 % | -0.101 / -0.075 / -0.045 | -0.090 / -0.017 / +0.008 | -0.018 / +0.034 / +0.079 | 0.4%: 0 / 2.9 (Nov 4 gap -0.29%, Nov 5 +1.74%) | monotonic in all 3 periods, but noisy by half-year (worse than small-gap days in 2022H1/H2) and leaves 1-2 trades/day |
| no-rev-after-loss 10m + cooldown 5m | -0.116 | -0.092 | | | no |

## Candidates (none convincing)
1. **No reversal within 15 min of the previous exit.** +0.008 train, +0.013 val, +0.007 flowdev. Neighbours at 10 m and 30 m
   are mixed (each within ±0.01 of baseline in some period). Nov 4 and Nov 5 are unchanged. It's harmless, but the gain is within noise.
2. **Gap-day filter (abs. opening gap >= 0.4-0.6%).** This is the only effect that is monotonic in train, val and flowdev, but
   train stays negative, the trade count falls about 75%, and it removes Nov 4 entirely. It's a regime observation for the
   entry/regime agent, not a session rule to ship.
3. **Skip lunch 11:30-13:00.** It helps val and flowdev (+0.03 to +0.05), but train is flat (-0.102), so it fails the both-periods rule.

## Recommendation
Keep as is: no session rule. The losses per trade don't depend on the time of day, the trade number, the
day's running P&L or a cooldown in any way that holds in both periods. Day stops, profit locks and max trades
only cut volume on a strategy that is negative per trade in the backtest. So any "improvement" in total R
is just trading less, which is the opposite of the user's request to trade all day.

## Untested
- Rupees with real option prices (run2d) for the candidates. On Nov 4/5, no-rev-15m doesn't change the trades, so the rupees are identical.
- True expiry calendar (holiday shifts; Tuesday expiry from Sep 2025 is in the holdout only).
- Session rules on the live flow-filtered stream beyond the two recorded days (flowdev uses the close-location proxy, which matches recorded delta in sign 68% of the time).
- Interaction with the other agents' entry/exit/sizing changes: rerun opt_session_grid.py on the new plan dumps.
