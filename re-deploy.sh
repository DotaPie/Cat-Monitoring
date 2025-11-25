#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/CatMonitoring"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "  > Stopping cat-monitoring.service ..."
systemctl stop    cat-monitoring.service || true

echo "  > Copying src files ..."
cp "${SCRIPT_DIR}"/src/{config.json,logging_setup.py,main.py,hud.py,view.py,upload.py,cam.py} "${INSTALL_DIR}/"

echo "  > Starting cat-monitoring.service ..."
systemctl start cat-monitoring.service || true

echo "  > Re-deploy complete"