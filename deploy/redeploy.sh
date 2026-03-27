#!/usr/bin/env bash
# =============================================================================
# Kalshi Trading Bot - Redeploy Script
# Pulls the latest code from GitHub and restarts all services.
#
# Usage: sudo bash /opt/kalshi-trading-bot/deploy/redeploy.sh
# =============================================================================

set -euo pipefail

INSTALL_DIR="/opt/kalshi-trading-bot"

echo "[redeploy] Pulling latest code..."
cd "${INSTALL_DIR}"
git fetch origin
git reset --hard origin/main

echo "[redeploy] Installing/updating dependencies..."
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

echo "[redeploy] Fixing file ownership..."
chown -R kalshi:kalshi "${INSTALL_DIR}"

# Install hedger service if it doesn't exist
if [[ ! -f /etc/systemd/system/kalshi-hedger.service ]]; then
    echo "[redeploy] Installing kalshi-hedger service..."
    cp "${INSTALL_DIR}/deploy/kalshi-hedger.service" /etc/systemd/system/kalshi-hedger.service
    systemctl daemon-reload
    systemctl enable kalshi-hedger
fi

# One-time reset: clear hedger data for fresh $20 start
if [[ -f "${INSTALL_DIR}/.hedger-reset-pending" ]]; then
    echo "[redeploy] Resetting hedger database tables..."
    sudo -u postgres psql -d kalshi -c "TRUNCATE hedger_trades, hedger_rounds RESTART IDENTITY;"
    rm -f "${INSTALL_DIR}/.hedger-reset-pending"
fi

echo "[redeploy] Restarting services..."
systemctl restart kalshi-bot
systemctl restart kalshi-hedger

echo "[redeploy] Done. Service status:"
systemctl --no-pager status kalshi-bot
systemctl --no-pager status kalshi-hedger
