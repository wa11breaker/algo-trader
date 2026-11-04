#!/usr/bin/env bash
# Step 1: tune every round-2 strategy in index mode on NIFTY, Jan 2022 - Sep 2024 (no test period here:
# the shortlist is chosen by research/round2/shortlist.py with rules fixed in advance).
set -euo pipefail
cd "$(dirname "$0")/../.."
for s in trend_pullback squeeze_breakout pdhl_retest vwap_structure orb_momentum; do
  .venv/bin/algo sweep -s "$s" --ul NIFTY --grid "research/round2/$s.yaml" \
    --train 2022-01-01:2024-09-30 --mode index --workers 7 > "research/round2/train_$s.log" 2>&1
  echo "done $s: $(tail -1 research/round2/train_$s.log)"
done
