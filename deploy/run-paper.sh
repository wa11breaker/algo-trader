#!/usr/bin/env bash
# Daily job (systemd timer): update code, log in to Fyers headlessly, paper trade until 15:30,
# then write the report. Logs: logs/paper-<date>.log
set -uo pipefail
cd "$(dirname "$0")/.."
export TZ=Asia/Kolkata
mkdir -p logs
LOG="logs/paper-$(date +%F).log"
exec >>"$LOG" 2>&1
echo "=== $(date) start"

STRATEGY="${STRATEGY:-fvg_flow}"
CAPITAL="${CAPITAL:-100000}"

export PATH="$HOME/.local/bin:$PATH"
git pull --ff-only || echo "warn: git pull failed, using current code"
uv pip install -q -p .venv/bin/python -e ".[live]" || echo "warn: dependency update failed"

for i in 1 2 3 4 5; do
  .venv/bin/algo fyers-login --totp && break
  echo "login attempt $i failed; retrying in 60 s"; sleep 60
done

.venv/bin/algo paper -s "$STRATEGY" --ul NIFTY --capital "$CAPITAL"
echo "=== $(date) session ended (exit $?)"
.venv/bin/algo paper-report -s "$STRATEGY" --ul NIFTY || true
