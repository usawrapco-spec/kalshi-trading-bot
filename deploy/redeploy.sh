#!/usr/bin/env bash
# =============================================================================
# Kalshi Scraper Bot - Redeploy Script
# Pulls the latest code from GitHub and restarts the bot.
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

# === CLEANUP: remove old hedger service ===
if systemctl is-active kalshi-hedger &>/dev/null || [[ -f /etc/systemd/system/kalshi-hedger.service ]]; then
    echo "[redeploy] Removing hedger service..."
    systemctl stop kalshi-hedger 2>/dev/null || true
    systemctl disable kalshi-hedger 2>/dev/null || true
    rm -f /etc/systemd/system/kalshi-hedger.service
    systemctl daemon-reload
    echo "[redeploy] Hedger service removed."
fi

echo "[redeploy] Restarting scraper bot..."
systemctl restart kalshi-bot

echo "[redeploy] Done. Service status:"
systemctl --no-pager status kalshi-bot
