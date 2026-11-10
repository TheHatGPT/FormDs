#!/usr/bin/env bash
# Idempotent 24/7 Google Cloud VM installer for FormDScoutBot (Ubuntu/Debian Linux)
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="formd-scout"
RUN_USER="$(whoami)"

echo "[1/5] Installing Python 3 venv & system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip

echo "[2/5] Creating virtual environment and installing requirements.txt..."
cd "$APP_DIR"
python3 -m venv .venv
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r requirements.txt -q

echo "[3/5] Installing headless Chromium for SEDAR+ fallback..."
"$APP_DIR/.venv/bin/playwright" install --with-deps chromium

echo "[4/5] Locking down file permissions on .env and SQLite state..."
if [ -f "$APP_DIR/.env" ]; then
    chmod 600 "$APP_DIR/.env"
else
    echo "WARNING: $APP_DIR/.env not found yet! Copy your .env file to $APP_DIR/.env and run chmod 600 $APP_DIR/.env"
fi

echo "[5/5] Configuring 24/7 systemd service ($SERVICE_NAME.service)..."
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=Form D & SEDAR+ Scout Discord Bot (24/7)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
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
sudo systemctl enable "${SERVICE_NAME}.service"
sudo systemctl restart "${SERVICE_NAME}.service"

echo "================================================================="
echo "✅ FormDScoutBot is installed and running 24/7 via systemd!"
echo "• Check status:  sudo systemctl status ${SERVICE_NAME}"
echo "• Live logs:     sudo journalctl -u ${SERVICE_NAME} -f"
echo "• Stop service:  sudo systemctl stop ${SERVICE_NAME}"
echo "================================================================="
