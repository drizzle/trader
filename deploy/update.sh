#!/usr/bin/env bash
# Pull latest code and restart the service. Run on the droplet:
#   sudo bash /opt/trader/deploy/update.sh

set -euo pipefail

INSTALL_DIR="/opt/trader"
SERVICE_USER="trader"

echo "==> Pulling latest (as root — uses /root/.ssh/ deploy key)"
# Repo is chowned to trader:trader; tell root's git that's safe.
git config --global --add safe.directory "${INSTALL_DIR}" 2>/dev/null || true
git -C "${INSTALL_DIR}" pull --ff-only

echo "==> Restoring ${SERVICE_USER} ownership after the pull"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "==> Updating dependencies"
"${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}"

echo "==> Reloading systemd unit files (in case .service files changed)"
install -m 644 "${INSTALL_DIR}/deploy/trader.service"           /etc/systemd/system/trader.service
install -m 644 "${INSTALL_DIR}/deploy/trader@.service"          /etc/systemd/system/trader@.service
install -m 644 "${INSTALL_DIR}/deploy/trader-dashboard.service" /etc/systemd/system/trader-dashboard.service
install -m 644 "${INSTALL_DIR}"/deploy/trader-manager-*.service /etc/systemd/system/
install -m 644 "${INSTALL_DIR}"/deploy/trader-manager-*.timer   /etc/systemd/system/
systemctl daemon-reload

echo "==> Installing sudoers entry (lets dashboard restart trader service)"
install -m 440 "${INSTALL_DIR}/deploy/sudoers.d/trader-restart" /etc/sudoers.d/trader-restart
if ! visudo -cf /etc/sudoers.d/trader-restart >/dev/null; then
  echo "  !! sudoers validation failed — removing file"
  rm -f /etc/sudoers.d/trader-restart
  exit 1
fi

echo "==> Restarting services"
systemctl restart trader 2>/dev/null || echo "  (legacy trader service not enabled)"
for unit in $(systemctl list-units --type=service --state=active 'trader@*.service' --no-legend | awk '{print $1}'); do
  systemctl restart "$unit"
done
systemctl restart trader-dashboard 2>/dev/null || echo "  (dashboard not yet enabled — run: sudo systemctl enable --now trader-dashboard)"

echo "==> Status"
systemctl --no-pager status trader | head -n 10
echo ""
systemctl --no-pager status trader-dashboard 2>/dev/null | head -n 10 || true

echo ""
echo "Tail bot logs:        sudo journalctl -u trader -f"
echo "Tail dashboard logs:  sudo journalctl -u trader-dashboard -f"
