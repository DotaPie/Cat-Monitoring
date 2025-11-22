#!/usr/bin/env bash
########################################################################
# install-cat-monitoring.sh
# ---------------------------------------------------------------------
# Installs Cat-Monitoring to /opt/CatMonitoring, copies runtime files,
# sets up a venv, installs deps from a chosen requirements file located
# beside the installer, registers a systemd service that runs as the
# invoking (non-root) user, and configures mDNS with hostname 'catmonitoring'.
########################################################################
set -euo pipefail

# 1. Variables you might tweak
RUN_USER="${SUDO_USER:-$USER}"                       # non-root account
INSTALL_DIR="/opt/CatMonitoring"
SERVICE_FILE="/etc/systemd/system/cat-monitoring.service"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"          # dir of this script
CONFIG_JSON="${SCRIPT_DIR}/src/config.json"          # config file to read
HOSTNAME_TARGET="catmonitoring"                      # desired hostname (no .local)

# Ensure the config file is present before we go any further
if [[ ! -f "$CONFIG_JSON" ]]; then
    echo "ERROR: $CONFIG_JSON not found. Aborting." >&2
    exit 1
fi

# 2. Install system packages (add avahi-daemon for mDNS)
echo " > Updating APT index and installing python3-pip, libgl1, jq, avahi-daemon ..."
apt update
apt install -y python3-pip libgl1 jq avahi-daemon

# --- Hostname & /etc/hosts (for mDNS name catmonitoring.local) ---
echo " > Setting hostname to '${HOSTNAME_TARGET}' and updating /etc/hosts ..."
# Backup once
[[ -f /etc/hostname && ! -f /etc/hostname.bak ]] && cp /etc/hostname /etc/hostname.bak
[[ -f /etc/hosts    && ! -f /etc/hosts.bak    ]] && cp /etc/hosts    /etc/hosts.bak

# Set the static hostname
if command -v hostnamectl >/dev/null 2>&1; then
  CURRENT_HOST="$(hostnamectl --static | tr -d '[:space:]')"
else
  CURRENT_HOST="$(tr -d '[:space:]' </etc/hostname)"
fi
if [[ "$CURRENT_HOST" != "$HOSTNAME_TARGET" ]]; then
  if command -v hostnamectl >/dev/null 2>&1; then
    hostnamectl set-hostname "$HOSTNAME_TARGET" --static
  else
    echo "$HOSTNAME_TARGET" > /etc/hostname
  fi
fi

# Ensure Debian/RPi convention line: 127.0.1.1 <hostname>
# (127.0.0.1 stays for localhost; we map the host on 127.0.1.1)
if grep -qE '^127\.0\.1\.1\s' /etc/hosts; then
  sed -i -E "s/^127\.0\.1\.1\s+.*/127.0.1.1   ${HOSTNAME_TARGET}/" /etc/hosts
else
  echo "127.0.1.1   ${HOSTNAME_TARGET}" >> /etc/hosts
fi

# 3. Resolve log / video paths from config.json and create them
LOGGING_PATH=$(jq -r '.LOGGING_PATH' "$CONFIG_JSON")
VIDEO_PATH=$(jq -r '.VIDEO_PATH'   "$CONFIG_JSON")

echo " > Creating paths from config.json ..."
mkdir -p "$LOGGING_PATH" "$VIDEO_PATH"
chown -R "${RUN_USER}:${RUN_USER}" "$LOGGING_PATH" "$VIDEO_PATH"
chmod 750 "$LOGGING_PATH" "$VIDEO_PATH"

# 4. Copy runtime files (no requirements files)
echo " > Copying runtime files to ${INSTALL_DIR}/"
mkdir -p "$INSTALL_DIR"
cp "${SCRIPT_DIR}"/src/{config.json,logging_setup.py,main.py,hud.py,view.py} "$INSTALL_DIR/"

# 5. Virtual environment + dependency install
echo " > Creating Python virtual environment ..."
python3 -m venv "${INSTALL_DIR}/venv"
# shellcheck disable=SC1091
source "${INSTALL_DIR}/venv/bin/activate"
pip install --upgrade pip

echo " > Checking requirements file ..."
REQ_FILE="requirements.txt"

REQ_PATH="${SCRIPT_DIR}/${REQ_FILE}"
if [[ ! -f "$REQ_PATH" ]]; then
    echo "ERROR: ${REQ_FILE} not found next to installer. Aborting." >&2
    exit 1
fi

echo " > Installing Python dependencies from ${REQ_PATH} ..."
pip install -r "$REQ_PATH"
deactivate

# 6. Create systemd unit
echo " > Writing systemd unit to ${SERVICE_FILE} ..."
cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=Cat Monitoring
After=network.target

[Service]
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/main.py

AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

Restart=on-failure
RestartSec=5

Environment="PYTHONUNBUFFERED=1"
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

# 7. Enable & start the app service
echo " > Enabling and starting cat-monitoring.service ..."
systemctl daemon-reload
systemctl enable cat-monitoring
systemctl start  cat-monitoring

# 8. Enable mDNS responder (Avahi) so http://catmonitoring.local/ works
echo " > Enabling mDNS (avahi-daemon) ..."
systemctl enable avahi-daemon
systemctl restart avahi-daemon

echo -e "\nAll done!"
echo "Local access (port 80) via mDNS:"
echo "    http://catmonitoring.local/"
echo
echo "Check the service status with:"
echo "    sudo systemctl status cat-monitoring"
