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

echo "==> Restarting service"
systemctl restart trader

echo "==> Status"
systemctl --no-pager status trader | head -n 15

echo ""
echo "Tail logs with:  sudo journalctl -u trader -f"
