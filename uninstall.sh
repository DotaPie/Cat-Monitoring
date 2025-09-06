#!/usr/bin/env bash
########################################################################
# uninstall-cat-monitoring.sh
# ---------------------------------------------------------------------
# Stops & removes Cat-Monitoring, deletes its files, restores hostname
# and /etc/hosts from backups (if present), and reboots.
########################################################################
set -euo pipefail

# Must run as root
if (( EUID != 0 )); then
    echo "ERROR: Please run this script with sudo or as root." >&2
    exit 1
fi

SERVICE_NAME="cat-monitoring"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
INSTALL_DIR="/opt/CatMonitoring"
CONFIG_JSON="${INSTALL_DIR}/config.json"

echo " > Stopping and disabling ${SERVICE_NAME}.service ..."
systemctl stop    "${SERVICE_NAME}"  || true
systemctl disable "${SERVICE_NAME}"  || true

echo " > Removing systemd unit file ..."
rm -f "${SERVICE_FILE}"
systemctl daemon-reload

# Remove log / video directories mentioned in config.json (if any)
LOGGING_PATH=""
VIDEO_PATH=""

if [[ -f "${CONFIG_JSON}" ]]; then
    echo " > Reading paths from ${CONFIG_JSON} ..."
    # Ensure jq is available; install silently if missing
    if ! command -v jq &>/dev/null; then
        echo "       (jq not found – installing)"
        apt update -qq
        apt install -y -qq jq
    fi
    LOGGING_PATH=$(jq -r '.LOGGING_PATH // empty' "$CONFIG_JSON")
    VIDEO_PATH=$(jq -r '.VIDEO_PATH   // empty' "$CONFIG_JSON")
fi

# Delete installation directory
echo " > Deleting ${INSTALL_DIR} ..."
rm -rf "${INSTALL_DIR}"

# Delete any external log/video dirs (protect against rm -rf / mistakes)
for P in "$LOGGING_PATH" "$VIDEO_PATH"; do
    if [[ -n "$P" && -d "$P" && "$P" != "/" && "$P" != "${INSTALL_DIR}"* ]]; then
        echo " > Deleting ${P} ..."
        rm -rf "$P"
    fi
done

# Restore hostname and /etc/hosts if backups exist
echo " > Restoring hostname and /etc/hosts (if backups are present) ..."
RESTORED_HOST=""
if [[ -f /etc/hostname.bak ]]; then
    RESTORED_HOST="$(tr -d '[:space:]' </etc/hostname.bak || true)"
    cp -f /etc/hostname.bak /etc/hostname
    if command -v hostnamectl >/dev/null 2>&1; then
        hostnamectl set-hostname "$RESTORED_HOST" --static || true
    else
        # Fallback: write and apply to current session
        echo "$RESTORED_HOST" > /etc/hostname
        hostname "$RESTORED_HOST" || true
    fi
    echo "   - Restored hostname: ${RESTORED_HOST}"
else
    echo "   - No /etc/hostname.bak found; leaving hostname as-is."
fi

if [[ -f /etc/hosts.bak ]]; then
    cp -f /etc/hosts.bak /etc/hosts
    echo "   - Restored /etc/hosts from backup."
else
    echo "   - No /etc/hosts.bak found; leaving /etc/hosts as-is."
fi

# Restart avahi so mDNS reflects the (possibly) restored hostname
if systemctl list-unit-files | grep -q '^avahi-daemon.service'; then
    echo " > Restarting avahi-daemon ..."
    systemctl restart avahi-daemon || true
fi

echo " > Uninstallation finished."
echo "System will reboot in 5 seconds (Ctrl+C to cancel)."
sleep 5
reboot
