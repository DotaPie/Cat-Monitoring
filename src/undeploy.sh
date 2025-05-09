#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

echo "===> Stopping and disabling cat-monitoring.service ..."
systemctl stop    cat-monitoring.service || true
systemctl disable cat-monitoring.service || true

echo "===> Removing systemd unit file ..."
rm /etc/systemd/system/cat-monitoring.service
systemctl daemon-reload          

echo "===> Deleting /opt/CatMonitoring/..."
rm -r /opt/CatMonitoring/

echo "===> Undeploy complete - rebooting in 5 seconds ..."
sleep 5
reboot now