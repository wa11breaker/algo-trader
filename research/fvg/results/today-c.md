# fvg_flow on 2026-11-04: order-flow confirmation with the real tick delta (agent C)

Scope: one day, real footprint delta (NIFTY26NOVFUT ticks, recording from 09:32:52), `today.py` only.
FVG, stop and target are at their defaults. `flow_proxy` was not used. Net R = R − 2 pts / risk.
**Warning:** this is one day and 12 trades. Every number below is fitted to this day.

Files: `today-c-1a.csv` (flow_bars × min_delta_ratio × min_delta × cum, 256 configs),
`today-c-1b.csv` (flow_bars × delta_z × ratio × cum, 224), `today-c-2.csv` (ratio × min_delta
refinement, 165), `today-c-3.csv` (stability neighbourhood incl. negative z, 192).

Baseline (defaults): 12 trades, 5 wins, sum_netR **+2.40**.

## 1. Per-trade flow table (signal bar = flow bar at the signal minute)

Delta is in contracts (multiples of the 65 lot). The ratio is signed: + means delta is on the trade's
side. z = (bar delta − mean of the previous 20 bars) / sd, signed to the trade. cum is the day's
cumulative delta. body is the signal candle's body in the trade direction, in points.

| time | dir | result | delta | ratio | 2-bar Δ | 5-bar Δ | z | cum Δ | bar vol | body |
|---|---|---|---|---|---|---|---|---|---|---|
| 09:33 | L | loss | +3120 | 0.22 | +3315 | +3315 | n/a (no history) | +3k | 14170 | 13.2 |
| 10:00 | S | loss | −14365 | 0.68 | −19500 | −46995 | 2.16 | −69k | 21125 | 8.1 |
| 10:09 | L | loss | **+260** | **0.03** | −5460 | −3835 | 0.84 | −90k | 7670 | 5.0 |
| 10:22 | S | WIN | −2795 | 0.21 | −5980 | −39780 | −0.28 | −174k | 13195 | 2.7 |
| 10:28 | S | loss | −11895 | 0.69 | −9945 | −42705 | 0.52 | −242k | 17225 | 23.3 |
| 11:04 | S | loss | −7735 | 0.61 | −9620 | −63375 | 0.48 | −384k | 12675 | 10.1 |
| 11:40 | S | WIN | −6760 | 0.93 | −13975 | −27885 | 0.44 | −521k | 7280 | 0.6 |
| 11:49 | S | BE | −11180 | 0.88 | −15210 | −26130 | 1.85 | −562k | 12740 | 8.6 |
| 12:20 | S | loss | −7865 | 0.55 | −10985 | −11700 | 0.51 | −733k | 14235 | 8.6 |
| 12:34 | L | WIN | +1235 | 0.44 | −2405 | −11700 | 0.38 | −729k | 2795 | 8.8 |
| 13:12 | L | WIN | +1430 | 0.17 | +3120 | +7800 | −0.04 | −680k | 8190 | 3.2 |
| 14:47 | L | WIN | +6110 | 0.68 | +6045 | +9360 | 2.14 | −587k | 8970 | 0.6 |

Means: winners have ratio 0.49 and |Δ| 3.7k. Losers have ratio 0.46 and |Δ| 7.5k.

## 2. Does flow separate winners from losers? Mostly no.

- **Ratio and z do not separate them.** Winners and losers overlap fully: ratio 0.17–0.93 against
  0.03–0.69, and z −0.28–2.14 against 0.48–2.16. Raising min_delta_ratio or delta_z mostly removes
  winners. Ratio ≥0.22 gives −0.32 and z ≥0.5 gives −1.9 to −3.0.
- **Bigger delta lost more often.** Four of the five shorts with |Δ| > 7k lost, and the fifth broke
  even. Those were aggressive sells late in an established down move, which fits exhaustion or
  absorption: heavy selling that didn't move price further. min_delta cannot express this, because
  it is a floor, not a cap.
- **require_cum_delta is clearly harmful** (−2.0 to −4.5 everywhere). Cumulative delta was negative
  from 09:50 to the close, and all four afternoon longs won against it. Those longs came as cum delta
  climbed back from −733k to −587k. The level was negative, but the slope was up.
- **flow_bars > 1 is worse**: 2 bars +2.23, 3 bars −0.52, 5 bars −2.78. Multi-bar sums keep the
  morning chase-shorts and add more losers through path changes.
- **The one real effect is a "no flow" veto.** At 10:09 a long fired on a bar with +260 delta
  (ratio 0.03): price rose 5 pts with no net buying, and the 2-bar delta was −5.5k. Rejecting
  near-zero delta drops only this loser, without any replacement trade.

## 3. Top 3 configs

The baseline uses flow_bars 1 with every other flow param at its default. Iterations: broad grid
(1a, 1b), then refinement (2), then stability (3).

### #1 `{"min_delta_ratio": 0.1}`: 11 trades, 5 wins, sum_netR +3.53 (netR 0.321), max DD 2.14R
- Keeps all 5 winners and drops only 10:09 (L, −1R).
- Plateau: every ratio from 0.04 to 0.17 gives the identical +3.53, a ×4 range (today-c-2). The
  cliff at 0.18 drops the 13:12 winner (+1.48), and 0.22 drops the 10:22 winner too.
- Stability: at a cost of 3 pts it gives +2.80 and at 4 pts +2.06 (baseline: +1.61 and +0.81). The
  leave-one-out minimum is +1.62 (baseline +0.49). flow_bars 2 with the same ratio collapses to
  +0.50, so keep flow_bars 1.
- Rationale: it doesn't depend on the day's volume level, and it is the centre of the widest plateau.

### #2 `{"min_delta": 700}`: the same 11 trades and stats as #1
- Plateau: min_delta from 300 to 1200 gives +3.53. The cliff at 1300 drops the 12:34 winner
  (+1235). This is narrower than #1 and sits right next to winner deltas of 1235 and 1430.
- An absolute contract count drifts with volume regime and contract roll, so #1 is preferred. #2 is
  listed as a cross-check that the same single trade is the one being removed.

### #3 `{"min_delta_ratio": 0.1, "delta_z": -0.5}`: 10 trades, 5 wins, sum_netR +4.60 (netR 0.46)
- Drops 10:09 and 09:33 (L, −1R), and keeps all winners.
- Plateau: delta_z from −2 to −0.5 behaves the same (18 configs at +4.60 in today-c-3, 3 with
  delta_z alone at +3.47).
- **Caveat: this is an artefact, not a flow signal.** A negative z threshold passes almost every bar
  but rejects when sd = 0 or fewer than 5 history bars exist. Because recording started at 09:32:52,
  only the 09:33 trade is affected. In effect this is "skip until 5 minutes of flow are recorded",
  which is really an entry_start or warm-up rule. Do not adopt it as a flow filter. Use #1, and
  consider a flow warm-up rule separately.

## 4. Stability summary (iteration 3)

| config | n | cost 2 | cost 3 | cost 4 | leave-one-out min | AM (<12:00) | PM |
|---|---|---|---|---|---|---|---|
| baseline | 12 | +2.40 | +1.61 | +0.81 | +0.49 | −1.96 | +4.36 |
| ratio 0.05 / 0.1 / 0.15, min_delta 700 | 11 | +3.53 | +2.80 | +2.06 | +1.62 | −0.83 | +4.36 |
| ratio 0.2 | 11 | +1.48 | +0.72 | −0.04 | −0.43 | −0.83 | +2.31 |
| ratio 0.1, flow_bars 2 | 11 | +0.50 | −0.25 | −1.00 | −1.36 | −1.94 | +2.44 |
| require_cum_delta | 8 | −2.02 | −2.53 | −3.05 | −3.83 | −0.83 | −1.20 |

The gain from #1 over the baseline is **one trade (+1.13R)**. It holds up as a principled veto ("don't
confirm on zero flow"), but one day cannot show that it is an edge. The morning stays negative under
every flow setting. The day's P&L comes from the afternoon longs, and flow can't select for them.

## 5. Feature ideas that need code changes (not tested; src/ untouched)

1. **Absorption / exhaustion cap**: reject when |Δ| is large but price progress per unit of delta is
   small, for example `body_pts / (|Δ|/1000)` below a threshold, or a max_delta_ratio or
   max_delta_z cap. On this day the big-delta shorts (10:00, 10:28, 11:04, 12:20) lost. This needs
   the signal bar's OHLC next to the flow in `_flow_ok`.
2. **Cum-delta slope instead of level**: `require_cum_delta` uses the sign of the day's level, which
   was negative all afternoon while the longs won. A slope over N bars, such as
   cum_delta[-1] − cum_delta[-30], would have been positive for the afternoon longs.
3. **Delta divergence**: price makes a new low on the retest while cumulative delta makes a higher
   low, or the reverse. This is the classic reversal confirmation, and the afternoon reversal looks
   like one.
4. **Flow warm-up rule**: require ≥ N recorded flow bars before trading (what #3 does by accident),
   with the option to skip the trade or fall back to no-flow when history is short.
5. **Delta at the gap level**: sum the delta only from ticks traded inside the FVG zone during the
   retest (footprint by price), rather than the whole minute. This needs price-level footprint
   aggregation.
6. **Volume-normalised z**: z of delta/volume rather than raw delta, so that the high-volume opening
   bars don't dominate the 20-bar sd.

Side note outside my area: losers tend to have large signal-candle bodies (chasing). Winners' bodies
are 0.6–8.8 pts; losers' are 5–23 pts.
