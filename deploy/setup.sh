#!/usr/bin/env bash
# =============================================================================
# Kalshi Trading Bot - DigitalOcean Droplet Setup Script
# Target OS: Ubuntu 24.04 LTS
#
# This script configures a fresh droplet to run the Kalshi trading bot with:
#   - PostgreSQL database
#   - Python 3 virtual environment
#   - systemd service (auto-restart, boot start)
#   - nginx reverse proxy
#   - UFW firewall
#   - Auto-deploy via cron (checks GitHub every minute)
#
# Usage: sudo bash setup.sh
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Require root
# ---------------------------------------------------------------------------
if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: This script must be run as root (use sudo)."
    exit 1
fi

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REPO_URL="https://github.com/usawrapco-spec/kalshi-trading-bot"
INSTALL_DIR="/opt/kalshi-trading-bot"
BOT_USER="kalshi"
DB_NAME="kalshi"
DB_USER="kalshi"
DB_PASS=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 32)
PORT=8080

echo "============================================="
echo " Kalshi Trading Bot - Server Setup"
echo "============================================="
echo ""

# ---------------------------------------------------------------------------
# 1. Update system packages
# ---------------------------------------------------------------------------
echo "[1/11] Updating system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
echo "       Done."

# ---------------------------------------------------------------------------
# 2. Install required packages
# ---------------------------------------------------------------------------
echo "[2/11] Installing PostgreSQL, Python 3, pip, nginx, certbot..."
apt-get install -y -qq \
    postgresql \
    postgresql-contrib \
    python3 \
    python3-pip \
    python3-venv \
    nginx \
    certbot \
    python3-certbot-nginx \
    git \
    ufw \
    curl \
    jq
echo "       Done."

# ---------------------------------------------------------------------------
# 3. Create PostgreSQL database and user
# ---------------------------------------------------------------------------
echo "[3/11] Setting up PostgreSQL database..."

# Ensure PostgreSQL is running
systemctl enable --now postgresql

# Create user and database (idempotent: skip if they already exist)
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" \
    | grep -q 1 \
    || sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';"

sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" \
    | grep -q 1 \
    || sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"

sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};"

DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}"
echo "       Database '${DB_NAME}' ready."
echo "       DATABASE_URL=${DATABASE_URL}"

# ---------------------------------------------------------------------------
# 4. Create system user for the bot
# ---------------------------------------------------------------------------
echo "[4/11] Creating system user '${BOT_USER}'..."
if id "${BOT_USER}" &>/dev/null; then
    echo "       User '${BOT_USER}' already exists, skipping."
else
    useradd --system --shell /usr/sbin/nologin --home-dir "${INSTALL_DIR}" "${BOT_USER}"
    echo "       User '${BOT_USER}' created."
fi

# ---------------------------------------------------------------------------
# 5. Clone the repository
# ---------------------------------------------------------------------------
echo "[5/11] Cloning repository to ${INSTALL_DIR}..."
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    echo "       Repository already exists, pulling latest..."
    git -C "${INSTALL_DIR}" pull --ff-only
else
    rm -rf "${INSTALL_DIR}"
    git clone "${REPO_URL}" "${INSTALL_DIR}"
fi
chown -R "${BOT_USER}:${BOT_USER}" "${INSTALL_DIR}"
echo "       Done."

# ---------------------------------------------------------------------------
# 6. Create Python virtual environment and install requirements
# ---------------------------------------------------------------------------
echo "[6/11] Setting up Python virtual environment..."
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

# Also install psycopg2 for PostgreSQL support
"${INSTALL_DIR}/venv/bin/pip" install psycopg2-binary -q

chown -R "${BOT_USER}:${BOT_USER}" "${INSTALL_DIR}"
echo "       Done."

# ---------------------------------------------------------------------------
# 7. Create systemd service file
# ---------------------------------------------------------------------------
echo "[7/11] Creating systemd service..."

cat > /etc/systemd/system/kalshi-bot.service <<UNIT
[Unit]
Description=Kalshi Trading Bot
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=${BOT_USER}
Group=${BOT_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/bot.py

# ---- Environment ----
Environment=DATABASE_URL=${DATABASE_URL}
Environment=PORT=${PORT}

# Placeholder: set these via 'systemctl edit kalshi-bot' or an env file
Environment=KALSHI_API_KEY_ID=REPLACE_ME
Environment=KALSHI_PRIVATE_KEY=REPLACE_ME
Environment=ENABLE_TRADING=false

# ---- Restart policy ----
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5

# ---- Security hardening ----
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}
PrivateTmp=true

# ---- Logging ----
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kalshi-bot

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable kalshi-bot
echo "       Service 'kalshi-bot' created and enabled."
echo "       NOTE: Edit KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY, and"
echo "             ENABLE_TRADING before starting the service."
echo "             Use: sudo systemctl edit kalshi-bot"

# ---------------------------------------------------------------------------
# 8. Set up nginx reverse proxy
# ---------------------------------------------------------------------------
echo "[8/11] Configuring nginx reverse proxy..."

cat > /etc/nginx/sites-available/kalshi-bot <<'NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    # --- Security headers ---
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';" always;

    # --- Proxy to the bot ---
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Websocket support (if needed)
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Timeouts
        proxy_connect_timeout 60s;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    # --- Health check endpoint (bypass proxy if needed) ---
    location /health {
        proxy_pass http://127.0.0.1:8080/health;
    }
}
NGINX

# Enable the site, disable default
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/kalshi-bot /etc/nginx/sites-enabled/kalshi-bot

nginx -t
systemctl enable --now nginx
systemctl reload nginx
echo "       nginx configured and running."

# ---------------------------------------------------------------------------
# 9. Set up UFW firewall
# ---------------------------------------------------------------------------
echo "[9/11] Configuring UFW firewall..."

ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   comment "SSH"
ufw allow 80/tcp   comment "HTTP"
ufw allow 443/tcp  comment "HTTPS"
ufw --force enable
echo "       Firewall active: SSH(22), HTTP(80), HTTPS(443) allowed."

# ---------------------------------------------------------------------------
# 10. Create redeploy script
# ---------------------------------------------------------------------------
echo "[10/11] Creating redeploy script..."

mkdir -p "${INSTALL_DIR}/deploy"

cat > "${INSTALL_DIR}/deploy/redeploy.sh" <<'REDEPLOY'
#!/usr/bin/env bash
# =============================================================================
# Kalshi Trading Bot - Redeploy Script
# Pulls the latest code from GitHub and restarts the service.
#
# Usage: sudo bash /opt/kalshi-trading-bot/deploy/redeploy.sh
# =============================================================================

set -euo pipefail

INSTALL_DIR="/opt/kalshi-trading-bot"
SERVICE_NAME="kalshi-bot"

echo "[redeploy] Pulling latest code..."
cd "${INSTALL_DIR}"
git fetch origin
git reset --hard origin/main

echo "[redeploy] Installing/updating dependencies..."
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

echo "[redeploy] Fixing file ownership..."
chown -R kalshi:kalshi "${INSTALL_DIR}"

echo "[redeploy] Restarting services..."
systemctl restart "${SERVICE_NAME}"
# Restart hedger if it exists
systemctl restart kalshi-hedger 2>/dev/null || true

echo "[redeploy] Done. Service status:"
systemctl --no-pager status "${SERVICE_NAME}"
systemctl --no-pager status kalshi-hedger 2>/dev/null || true
REDEPLOY

chmod +x "${INSTALL_DIR}/deploy/redeploy.sh"
echo "       Redeploy script created at ${INSTALL_DIR}/deploy/redeploy.sh"

# ---------------------------------------------------------------------------
# 11. Set up cron-based auto-deploy (checks for new commits every minute)
# ---------------------------------------------------------------------------
echo "[11/11] Setting up auto-deploy cron..."

cat > /usr/local/bin/kalshi-auto-deploy.sh <<'CRON_SCRIPT'
#!/usr/bin/env bash
# =============================================================================
# Kalshi Auto-Deploy
# Runs every minute via cron. Checks if the remote main branch has new
# commits. If so, pulls and restarts the service.
# =============================================================================

set -euo pipefail

INSTALL_DIR="/opt/kalshi-trading-bot"
LOCK_FILE="/tmp/kalshi-deploy.lock"
LOG_FILE="/var/log/kalshi-auto-deploy.log"

# Prevent overlapping runs
if [[ -f "${LOCK_FILE}" ]]; then
    exit 0
fi
trap 'rm -f "${LOCK_FILE}"' EXIT
touch "${LOCK_FILE}"

cd "${INSTALL_DIR}"

# Fetch remote without changing working tree
git fetch origin main --quiet 2>/dev/null || exit 0

LOCAL_SHA=$(git rev-parse HEAD)
REMOTE_SHA=$(git rev-parse origin/main)

if [[ "${LOCAL_SHA}" == "${REMOTE_SHA}" ]]; then
    # Nothing to deploy
    exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') - New commits detected (${LOCAL_SHA:0:8} -> ${REMOTE_SHA:0:8}). Deploying..." >> "${LOG_FILE}"

# Run the full redeploy
bash "${INSTALL_DIR}/deploy/redeploy.sh" >> "${LOG_FILE}" 2>&1

echo "$(date '+%Y-%m-%d %H:%M:%S') - Deploy complete." >> "${LOG_FILE}"
CRON_SCRIPT

chmod +x /usr/local/bin/kalshi-auto-deploy.sh

# Install cron job (runs every minute as root)
CRON_LINE="* * * * * /usr/local/bin/kalshi-auto-deploy.sh"
( crontab -l 2>/dev/null | grep -v "kalshi-auto-deploy" ; echo "${CRON_LINE}" ) | crontab -
echo "       Auto-deploy cron installed (checks every minute)."
echo "       Logs at /var/log/kalshi-auto-deploy.log"

# ---------------------------------------------------------------------------
# Save credentials to a file only root can read
# ---------------------------------------------------------------------------
CREDS_FILE="/root/.kalshi-credentials"
cat > "${CREDS_FILE}" <<CREDS
# Kalshi Trading Bot - Generated Credentials
# Created: $(date -Iseconds)
#
DATABASE_URL=${DATABASE_URL}
DB_PASSWORD=${DB_PASS}
CREDS

chmod 600 "${CREDS_FILE}"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================="
echo " Setup Complete!"
echo "============================================="
echo ""
echo " Database URL: ${DATABASE_URL}"
echo " DB password saved to: ${CREDS_FILE}"
echo ""
echo " NEXT STEPS:"
echo "  1. Set your Kalshi API credentials:"
echo "       sudo systemctl edit kalshi-bot"
echo "     Add under [Service]:"
echo "       Environment=KALSHI_API_KEY_ID=your_key_id"
echo "       Environment=KALSHI_PRIVATE_KEY=your_private_key"
echo "       Environment=ENABLE_TRADING=true"
echo ""
echo "  2. Start the bot:"
echo "       sudo systemctl start kalshi-bot"
echo ""
echo "  3. Check status:"
echo "       sudo systemctl status kalshi-bot"
echo "       sudo journalctl -u kalshi-bot -f"
echo ""
echo "  4. (Optional) Set up HTTPS with certbot:"
echo "       sudo certbot --nginx -d yourdomain.com"
echo ""
echo "  5. Auto-deploy is active. Push to main and it"
echo "     will deploy within 60 seconds."
echo ""
echo "============================================="
