#!/usr/bin/env bash
# One-command setup for a Linux VPS (Ubuntu/Debian): installs everything and keeps the bot running 24/7.
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg nodejs
python3 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip uninstall -y pyrogram >/dev/null 2>&1 || true
./venv/bin/pip install -r requirements.txt "yt-dlp[default]"

sudo tee /etc/systemd/system/musicbot.service >/dev/null <<EOF
[Unit]
Description=Music bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python bot.py
Restart=always
RestartSec=5
User=$(whoami)

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now musicbot
echo "Done. Bot is running. Watch logs with:  journalctl -u musicbot -f"
