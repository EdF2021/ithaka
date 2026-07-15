#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/ithaka-ui.service"

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: ithaka-ui.service not found in $SCRIPT_DIR"
  exit 1
fi

echo "Installing Ithaka UI service..."
echo "Make sure you've edited ithaka-ui.service with your username and paths first!"
echo ""

sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ithaka-ui
sudo systemctl start ithaka-ui
sudo systemctl status ithaka-ui
