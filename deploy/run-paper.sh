#!/usr/bin/env bash
# Daily job (systemd timer, and at boot): update code, log in to Fyers headlessly, paper trade every
# strategy in STRATEGIES (one process, independent accounts) until 15:30, then write the reports.
# Logs: logs/paper-<date>.log
set -uo pipefail
cd "$(dirname "$0")/.."
export TZ=Asia/Kolkata
mkdir -p logs
LOG="logs/paper-$(date +%F).log"
exec >>"$LOG" 2>&1
echo "=== $(date) start"

STRATEGIES="${STRATEGIES:-fvg_flow fvg_supertrend}"
CAPITAL="${CAPITAL:-50000}"

export PATH="$HOME/.local/bin:$PATH"
git pull --ff-only || echo "warn: git pull failed, using current code"
uv pip install -q -p .venv/bin/python -e ".[live]" || echo "warn: dependency update failed"

for i in 1 2 3 4 5; do
  .venv/bin/algo fyers-login --totp && break
  echo "login attempt $i failed; retrying in 60 s"; sleep 60
done

args=(); for s in $STRATEGIES; do args+=(-s "$s"); done
.venv/bin/algo paper "${args[@]}" --ul NIFTY --capital "$CAPITAL"
rc=$?
echo "=== $(date) session ended (exit $rc)"
if [ "$rc" -eq 0 ] && [ "$(date +%H%M)" -ge 1530 ]; then
  for s in $STRATEGIES; do .venv/bin/algo paper-report -s "$s" --ul NIFTY || true; done
  .venv/bin/python research/fvg/whatif.py "${args[@]}" || true   # paper/<s>_NIFTY/whatif-<day>.html
fi
exit "$rc"   # non-zero -> systemd restarts the session (Restart=on-failure)
