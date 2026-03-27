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

echo "[redeploy] Restarting services..."
systemctl restart kalshi-bot
systemctl restart kalshi-hedger 2>/dev/null || true

echo "[redeploy] Done. Service status:"
systemctl --no-pager status kalshi-bot
systemctl --no-pager status kalshi-hedger 2>/dev/null || true
