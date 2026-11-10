#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
SERVICE_NAME="formd-scout"
USER_NAME="$(whoami)"

echo "=== Form D & SEDAR+ Scout 24/7 VM Installer ==="
echo "App directory: $APP_DIR"

# 1. Install Python 3 venv & pip if not already installed
if ! python3 -m venv --help >/dev/null 2>&1; then
    echo "[1/4] Installing python3-venv..."
    sudo apt-get update -y -qq
    sudo apt-get install -y -qq python3-venv python3-pip
fi

# 2. Create isolated Linux virtual environment and install requirements (no cache to save VM disk)
echo "[2/4] Setting up isolated Python virtual environment ($APP_DIR/.venv)..."
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --no-cache-dir --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install --no-cache-dir -r "$APP_DIR/requirements.txt" -q

# 3. Secure .env and SQLite DB file permissions
echo "[3/4] Securing .env and database permissions (chmod 600)..."
if [ -f "$APP_DIR/.env" ]; then
    chmod 600 "$APP_DIR/.env"
fi
if [ -f "$APP_DIR/formd_state.db" ]; then
    chmod 600 "$APP_DIR/formd_state.db"
fi

# 4. Create and enable isolated 24/7 systemd service
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
echo "[4/4] Creating and starting systemd service at $SERVICE_FILE..."

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Form D & SEDAR+ Scout Discord Bot (24/7)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/bot.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

# Security Hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"
sleep 2
sudo systemctl status "${SERVICE_NAME}" --no-pager -l -n 8

echo ""
echo "============================================================"
echo "✅ Form D Scout Bot is now running 24/7 on your Google Cloud VM!"
echo "Check live status:  sudo systemctl status ${SERVICE_NAME}"
echo "View live logs:     sudo journalctl -u ${SERVICE_NAME} -f"
echo "============================================================"
