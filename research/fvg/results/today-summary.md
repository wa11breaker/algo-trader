# fvg_flow tuned on 2026-11-04 (real order flow): summary

Harness: research/fvg/today.py (Fyers spot candles + recorded tick delta). With the defaults it
reproduces all 9 live paper entries, plus 3 signals the live bot missed (09:33 before it started,
10:09 blocked by the daily loss limit, 11:40 just after a restart).

| config | trades | wins | sum netR today | val 2024-01..2025-06 netR/trade |
|---|---|---|---|---|
| defaults | 12 | 5 | +2.4 | -0.130 (t -7.8) |
| A: min_gap_atr 0.45 + trend ema | 9 | 5 | +5.8 | -0.124 |
| B: tp_mode atr, tp_atr 4, breakeven_at_r 0.5 | 10 | 4 | +8.4 | -0.100 |
| C: min_delta_ratio 0.1 | 11 | 5 | +3.5 | n/a (needs real flow) |
| A+B (+C) | 7 | 4 | +10.8 | -0.104; -0.032 with flow_proxy |

- All 324 neighbouring configs of A+B+C were positive today (min +6.0, median +9.4).
- Once the gap filter is on, C changes nothing today.
- On history, every variant still loses money after costs. Today's gain comes from dropping
  small gaps and letting two shorts run to about 4 ATR.

Agent reports: today-a.md (FVG), today-b.md (SL/TP), today-c.md (order flow).
