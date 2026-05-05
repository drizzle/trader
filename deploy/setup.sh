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
    python3 python3-venv python3-pip \
    git curl ca-certificates tzdata

# Verify Python is >= 3.10 (we use modern type hints + pydantic v2)
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 || ( "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10 ) ]]; then
    echo "ERROR: Python 3.10+ required, found $PY_VERSION" >&2
    exit 1
fi
echo "==> Using Python $PY_VERSION"

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
python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}"

echo "==> Creating data + backtests directories"
mkdir -p "${INSTALL_DIR}/data/backtests"

echo "==> Setting ownership"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "==> Installing systemd units"
install -m 644 "${INSTALL_DIR}/deploy/trader.service"           /etc/systemd/system/trader.service
install -m 644 "${INSTALL_DIR}/deploy/trader@.service"          /etc/systemd/system/trader@.service
install -m 644 "${INSTALL_DIR}/deploy/trader-dashboard.service" /etc/systemd/system/trader-dashboard.service
install -m 644 "${INSTALL_DIR}"/deploy/trader-manager-*.service /etc/systemd/system/
install -m 644 "${INSTALL_DIR}"/deploy/trader-manager-*.timer   /etc/systemd/system/
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
echo "  2. Start dashboard and whichever account services you want active:"
echo "       sudo cp ${INSTALL_DIR}/deploy/dashboard.env.example ${INSTALL_DIR}/dashboard.env"
echo "       sudo nano ${INSTALL_DIR}/dashboard.env  # set DASHBOARD_PASSWORD; set DASHBOARD_READ_ONLY=false only when needed"
echo "       sudo chown ${SERVICE_USER}:${SERVICE_USER} ${INSTALL_DIR}/dashboard.env"
echo "       sudo chmod 600 ${INSTALL_DIR}/dashboard.env"
echo "       sudo systemctl enable --now trader@paper_account"
echo "       # optional:"
echo "       sudo systemctl enable --now trader@live_account"
echo "       sudo systemctl enable --now trader@Roth_IRA"
echo "       sudo systemctl enable --now trader-dashboard"
echo "       # optional manager reports:"
echo "       sudo systemctl enable --now trader-manager-daily@paper_account.timer"
echo "       sudo systemctl enable --now trader-manager-weekly@paper_account.timer"
echo "       sudo systemctl enable --now trader-manager-monthly@paper_account.timer"
echo ""
echo "  3. Watch logs:"
echo "       sudo journalctl -u trader -f"
echo "       sudo journalctl -u trader-dashboard -f"
echo ""
echo "  4. Open the dashboard from your laptop (NOT directly from the droplet):"
echo "       ssh -L 8000:localhost:8000 root@<droplet-ip>"
echo "       open http://localhost:8000   # in your laptop's browser"
echo ""
echo "  5. Run a backtest from the droplet:"
echo "       cd ${INSTALL_DIR}"
echo "       sudo -u ${SERVICE_USER} .venv/bin/python -m trader backtest --strategy yypt_tqqq_rsi --start 2018-01-01"
echo ""
echo "  6. To stop trading without uninstalling, engage the kill switch:"
echo "       sudo -u ${SERVICE_USER} touch ${INSTALL_DIR}/data/STOP"
echo "================================================================"
