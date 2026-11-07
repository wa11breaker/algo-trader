#!/usr/bin/env bash
# One-time server setup (Ubuntu/Debian/Oracle Linux, systemd). Run from the cloned repo:
#   git clone https://github.com/wa11breaker/algo-trader && cd algo-trader && bash deploy/setup-server.sh
# Then put the secrets in .env (see .env.example, incl. FYERS_CLIENT_ID / FYERS_PIN / FYERS_TOTP_SECRET).
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

# Python >= 3.11 via uv (works on any distro, no system Python changes)
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python -e ".[live]"

[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "Created .env: fill in the secrets."; }
mkdir -p logs

# systemd: run deploy/run-paper.sh every weekday at 08:45 IST
sed -e "s|@REPO@|$REPO|g" -e "s|@USER@|$USER|g" deploy/algo-paper.service | sudo tee /etc/systemd/system/algo-paper.service >/dev/null
sudo cp deploy/algo-paper.timer /etc/systemd/system/algo-paper.timer
sudo systemctl daemon-reload
sudo systemctl enable --now algo-paper.timer
sudo systemctl enable algo-paper.service     # start at boot too (resumes a session after a reboot)
systemctl list-timers algo-paper.timer --no-pager
echo "Done. Test the login now:  TZ=Asia/Kolkata .venv/bin/algo fyers-login --totp"
