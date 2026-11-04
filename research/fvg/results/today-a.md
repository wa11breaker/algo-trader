# Agent A: FVG definition and entry on 2026-11-04 only

Tool: `research/fvg/today.py`, which uses real spot bars and real tick delta. Stop, target and flow settings stay at their defaults.
Cost: 2 index points per trade. `c3` is the same sum at 3 points.
**This tunes one day and about 12 trades. Treat every number as a hypothesis to test on the multi-year eval, not as evidence.**

## Baseline: the 12 trades and what separates them

The gap size and ATR columns come from the 3-bar gap each trade retested. "body/ATR" is the impulse bar's body divided by ATR.

| # | time | dir | gap pts | gap/ATR | body/ATR | EMA50 slope | netR |
|---|---|---|---|---|---|---|---|
| 0 | 09:33 | L | 15.7 | 0.82 | 1.39 | +25 | -1.07 |
| 1 | 10:00 | S | 4.5 | **0.33** | 1.14 | **+5.2 (counter)** | -1.11 |
| 2 | 10:09 | L | 4.2 | **0.32** | 0.93 | +7 | -1.13 |
| 3 | 10:22 | S | 7.8 | 0.57 | 1.69 | -5 | +1.80 |
| 4 | 10:28 | S | 13.0 | 0.82 | 1.99 | -9 | -1.05 |
| 5 | 11:04 | S | 16.1 | 1.17 | 1.34 | -11 | -1.09 |
| 6 | 11:40 | S | 6.8 | 0.60 | 1.68 | -5 | +1.81 |
| 7 | 11:49 | S | 9.5 | 0.86 | 1.32 | -4.5 | -0.13 (BE) |
| 8 | 12:20 | S | 3.4 | **0.36** | 0.60 | -5 | -1.20 |
| 9 | 12:34 | L | 13.5 | 1.22 | 1.39 | +3 | +1.91 |
| 10 | 13:12 | L | 6.4 | 0.82 | 0.96 | +5 | +1.86 |
| 11 | 14:47 | L | 6.4 | 0.90 | 1.28 | +3.5 | +1.78 |

The one real structural signal is gap size. All 3 gaps under 0.4 ATR (under 5 points) lost: trades 1, 2 and 8. The 9 gaps at 0.57 ATR or more won 6 of 9.

## Iterations

1. **Broad grid** (`today-a-1.csv`, 2940 configs; `today-a-1b.csv`, 128 configs):
   - min_gap_pts 2–15
   - min_gap_atr and disp_atr: None or 0.3–2
   - depth: edge or mid
   - trend: none, vwap or ema
   - age, plus entry start and end

   One large plateau: depth edge, gap 4.5–6 points (or min_gap_atr 0.4–0.55), trend none or ema.
2. **Refinement** (`today-a-2.csv`, 7168 configs) around that plateau, adding entry_start and disp_atr. 1744 configs land on the same result: 8 trades, +6.90, dropping trades {0,1,2,8}.
3. **Stability** (`today-a-3.csv`): one parameter at a time around each finalist, with cost at 2 and at 3 points.

## Top 3

### 1. `{"min_gap_atr": 0.45, "trend": "ema", "entry_start": "09:45"}`
- **Result:** 8 trades, 5 wins, sum_netR **+6.90** (c3 +6.35), 0.86 R per trade.
- **Drops:** trades 0, 1, 2 and 8. Keeps every winner.
- **Why the drops are plausible:**
  - 1, 2 and 8 are the three sub-0.4-ATR gaps.
  - 1 is also a short against a rising EMA.
  - 0 is the opening-range trade: the gap formed at 09:28 and the trade carried a 30.7-point risk.
- **Plateau:**
  - min_gap_atr 0.4–0.55 gives the same result. 0.35 gives 5.71 and 0.6 gives 5.10.
  - min_gap_pts 3–6 gives the same result; it is redundant with the ATR filter.
  - entry_start anywhere from 09:34 to 10:22 gives the same result. 10:30 gives 6.15.
  - max_age 8–90 gives the same result.
  - disp_atr None to 0.75 gives the same result.
  - trend none gives 5.83.
- **Caveat:** the entry_start gain comes from exactly one trade (#0). That is the "drop one loser" pattern. Keep it only if a 15-minute opening skip also holds on the multi-year eval.

### 2. `{"min_gap_atr": 0.45, "trend": "ema"}`
- **Result:** 9 trades, 5 wins, **+5.84** (c3 +5.25), max drawdown 2.14 R.
- **Drops:** trades 1, 2 and 8, the three small gaps. Trade 1 is also counter to the EMA.
- **Plateau:**
  - min_gap_atr 0.4–0.55 and min_gap_pts 3–6 (or min_gap_pts 4.5–6 with no ATR filter) give the same result. 632 configs in iteration 1 sit on it.
  - max_age 8–90 gives the same result.
  - disp_atr ≤0.75 gives the same result.
- **Why I prefer it:** no time-window fitting, and the drops come from one coherent rule ("gap ≥ ~0.45 ATR").

### 3. `{"min_gap_pts": 5}`, with trend none
- **Result:** 10 trades, 5 wins, **+4.76** (c3 +4.14).
- **Drops:** only trades 2 and 8, the small gaps.
- **Note on 10:00:** at 10:00 it trades a different, larger gap (a 27.6-point risk instead of 18.5), which still loses.
- **Why it's here:** the most conservative option. It changes one parameter, keeps the most trades, and holds a plateau over min_gap_pts 4.5–6.
- **Adding filters:** adding trend=ema gives #2, and adding min_gap_atr 0.55 also gives #2's trades.

## Rejected spike
`{"min_gap_pts": 2, "disp_atr": 0.75, "trend": "ema", "entry_start": "10:15"}` scores **+8.57** with 9 trades. It is the top result, but only 16 configs reach it.
- It depends on one new 11:33 short taken from a 2–3-point gap with a 6-point risk (+1.67 R).
- At min_gap_pts 3 that trade disappears.
- It contradicts the finding that small gaps lose.

## What clearly hurts
- **retest_depth "mid":** 3–5 trades, sum −1 to −5 in every combination. It misses almost all the winners, because their retests only tagged the near edge.
- **trend "vwap":** it drops the winners at 12:34 and 13:12, which were longs below VWAP on the afternoon reversal. That costs 2.5–4 R compared with ema or none.
- **min_gap_pts ≥ 6.5 or min_gap_atr ≥ 0.6:** it starts dropping the 6.4 to 7.8-point winners (trades 3, 6, 10 and 11). This is a cliff right next to the plateau. The plateau's upper edge (6 points or 0.55 ATR) is only about 0.5 points from that cliff, so prefer the middle of the plateau (5 points or 0.45 ATR).
- **disp_atr ≥ 1.0:** it drops winner #10, whose impulse body is 0.96 ATR.
- **max_age_min 5:** it kills trades 7, 9 and 10 (gaps 6–8 minutes old). The sum drops to about 0–2. Ages from 8 to 90 make no difference on this day.
- **entry_end ≤ 14:30:** it loses the 14:47 winner.
- **min_gap_atr ≥ 1:** almost no trades.

## Not explored
I did not explore stop, target or flow settings; they are outside my area. All three finalists keep the same 5 winners as the baseline. The improvement comes only from removing losers, and with 3–4 removed trades that is a weak basis. The test that matters is whether "gap ≥ 0.45 ATR" (and EMA alignment) improves netR across the multi-year `eval.py` periods.
