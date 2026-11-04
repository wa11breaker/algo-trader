#!/usr/bin/env bash
# Serve the paper dashboard publicly on port 8770 (read-only, NO password) and keep it running
# after market hours. Also open 8770 in the Oracle Cloud security list (VCN > Security Lists >
# Ingress: 0.0.0.0/0 TCP 8770) - that part is in the Oracle Cloud console.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"
sed -e "s|@REPO@|$REPO|g" -e "s|@USER@|$USER|g" deploy/algo-ui.service | sudo tee /etc/systemd/system/algo-ui.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now algo-ui.service
# Oracle's Ubuntu images reject inbound traffic in iptables except SSH: allow 8770 and persist it
if ! sudo iptables -C INPUT -p tcp --dport 8770 -j ACCEPT 2>/dev/null; then
  n=$(sudo iptables -L INPUT --line-numbers | awk '/REJECT/ {print $1; exit}')   # before the catch-all REJECT
  sudo iptables -I INPUT "${n:-1}" -p tcp --dport 8770 -m state --state NEW -j ACCEPT
fi
command -v netfilter-persistent >/dev/null && sudo netfilter-persistent save >/dev/null || true
systemctl is-active algo-ui.service
echo "Dashboard: http://$(curl -s -4 ifconfig.me || hostname -I | cut -d' ' -f1):8770"
