#!/usr/bin/env bash
# Run on your laptop: copy the server's paper accounts, reports, footprint ticks and logs into
# ./server-data/ (kept apart from your local paper/ folder).
#   SERVER=ubuntu@1.2.3.4 SSH_KEY=~/.ssh/oracle_vpn.key bash deploy/pull.sh
set -euo pipefail
: "${SERVER:?set SERVER=user@host}"
DIR="${REMOTE_DIR:-algo-trader}"
SSH="ssh ${SSH_KEY:+-i $SSH_KEY}"
cd "$(dirname "$0")/.."
mkdir -p server-data
for p in paper reports/paper data/footprint logs; do
  mkdir -p "server-data/$p"
  rsync -az -e "$SSH" "$SERVER:$DIR/$p/" "server-data/$p/" || echo "skip $p (not on server yet)"
done
echo "Pulled into server-data/. Latest report: $(ls -td server-data/reports/paper/*/ 2>/dev/null | head -1)"
