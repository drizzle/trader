#!/usr/bin/env bash
# Pull latest code and restart the service. Run on the droplet:
#   sudo bash /opt/trader/deploy/update.sh

set -euo pipefail

INSTALL_DIR="/opt/trader"
SERVICE_USER="trader"

echo "==> Pulling latest"
sudo -u "${SERVICE_USER}" git -C "${INSTALL_DIR}" pull --ff-only

echo "==> Updating dependencies"
"${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}"

echo "==> Reloading systemd unit files (in case .service files changed)"
install -m 644 "${INSTALL_DIR}/deploy/trader.service"           /etc/systemd/system/trader.service
install -m 644 "${INSTALL_DIR}/deploy/trader-dashboard.service" /etc/systemd/system/trader-dashboard.service
systemctl daemon-reload

echo "==> Restarting services"
systemctl restart trader
systemctl restart trader-dashboard 2>/dev/null || echo "  (dashboard not yet enabled — run: sudo systemctl enable --now trader-dashboard)"

echo "==> Status"
systemctl --no-pager status trader | head -n 10
echo ""
systemctl --no-pager status trader-dashboard 2>/dev/null | head -n 10 || true

echo ""
echo "Tail bot logs:        sudo journalctl -u trader -f"
echo "Tail dashboard logs:  sudo journalctl -u trader-dashboard -f"
