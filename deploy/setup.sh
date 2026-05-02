#!/usr/bin/env bash
# First-time droplet setup. Run as root on a fresh Ubuntu 22.04 / 24.04 droplet.
#
# Usage (on the droplet, as root):
#   curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/deploy/setup.sh | bash
# OR after cloning:
#   sudo bash deploy/setup.sh

set -euo pipefail

REPO_URL="${REPO_URL:-}"           # e.g. https://github.com/you/trader.git  (set as env var)
INSTALL_DIR="/opt/trader"
SERVICE_USER="trader"

echo "==> Updating apt and installing prerequisites"
apt-get update -y
apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3-pip \
    git curl ca-certificates tzdata

echo "==> Creating ${SERVICE_USER} user"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "==> Cloning / pulling repo into ${INSTALL_DIR}"
if [[ ! -d "${INSTALL_DIR}/.git" ]]; then
    if [[ -z "${REPO_URL}" ]]; then
        echo "ERROR: REPO_URL not set and ${INSTALL_DIR} is not a git repo." >&2
        echo "Either run:  REPO_URL=https://github.com/you/trader.git sudo -E bash deploy/setup.sh"
        echo "Or clone manually first:  git clone <url> ${INSTALL_DIR} && sudo bash ${INSTALL_DIR}/deploy/setup.sh"
        exit 1
    fi
    git clone "${REPO_URL}" "${INSTALL_DIR}"
else
    git -C "${INSTALL_DIR}" pull --ff-only
fi

echo "==> Setting up Python virtualenv"
python3.11 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}"

echo "==> Creating data directory"
mkdir -p "${INSTALL_DIR}/data"

echo "==> Setting ownership"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "==> Installing systemd unit"
install -m 644 "${INSTALL_DIR}/deploy/trader.service" /etc/systemd/system/trader.service
systemctl daemon-reload

echo ""
echo "================================================================"
echo "Setup complete. Next steps:"
echo ""
echo "  1. Create your .env file:"
echo "       sudo cp ${INSTALL_DIR}/.env.example ${INSTALL_DIR}/.env"
echo "       sudo nano ${INSTALL_DIR}/.env       # paste your Alpaca keys"
echo "       sudo chown ${SERVICE_USER}:${SERVICE_USER} ${INSTALL_DIR}/.env"
echo "       sudo chmod 600 ${INSTALL_DIR}/.env"
echo ""
echo "  2. Start the service:"
echo "       sudo systemctl enable --now trader"
echo ""
echo "  3. Watch logs:"
echo "       sudo journalctl -u trader -f"
echo ""
echo "  4. To stop trading without uninstalling, engage the kill switch:"
echo "       sudo -u ${SERVICE_USER} touch ${INSTALL_DIR}/data/STOP"
echo "================================================================"
