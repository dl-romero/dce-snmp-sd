#!/usr/bin/env bash
# install.sh — installs snmp-http-sd as a systemd service
#
# Usage: sudo bash install.sh [options]
#
# Options:
#   --lookup PATH    Path to module_lookup.json (default: /opt/ddf-to-snmp-exporter/output/module_lookup.json)
#   --port PORT      Port to listen on (default: 8000)
#   --host HOST      Bind address (default: 0.0.0.0)
#   --help

set -euo pipefail

INSTALL_DIR="/opt/snmp-http-sd"
DATA_DIR="/var/lib/snmp-http-sd"
SERVICE_USER="snmp-http-sd"
SYSTEMD_DIR="/etc/systemd/system"
LOOKUP_PATH="/opt/ddf-to-snmp-exporter/output/module_lookup.json"
PORT=8000
HOST="0.0.0.0"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --lookup) LOOKUP_PATH="$2"; shift 2 ;;
        --port)   PORT="$2";        shift 2 ;;
        --host)   HOST="$2";        shift 2 ;;
        --help|-h) grep '^#' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

[[ "$EUID" -ne 0 ]] && { echo "ERROR: run as root (sudo)"; exit 1; }

log() { echo "  [install] $*"; }
ok()  { echo "  ✓ $*"; }

echo ""
echo "=== snmp-http-sd installer ==="
echo ""

# ── Service user ──────────────────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "Created user: $SERVICE_USER"
else
    ok "User exists: $SERVICE_USER"
fi

# ── Directories ───────────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
ok "Directories: $INSTALL_DIR  $DATA_DIR"

# ── Copy app ──────────────────────────────────────────────────────────────────
log "Copying application files..."
cp -r "${REPO_DIR}/app" "$INSTALL_DIR/"
cp "${REPO_DIR}/requirements.txt" "$INSTALL_DIR/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
ok "App files copied to $INSTALL_DIR"

# ── Python venv ───────────────────────────────────────────────────────────────
log "Creating virtual environment..."
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"
ok "Virtual environment ready"

# ── Systemd unit ──────────────────────────────────────────────────────────────
log "Installing systemd unit..."
cp "${REPO_DIR}/systemd/snmp-http-sd.service" "${SYSTEMD_DIR}/"

# Patch in configured values
sed -i "s|MODULE_LOOKUP_PATH=.*|MODULE_LOOKUP_PATH=${LOOKUP_PATH}|" "${SYSTEMD_DIR}/snmp-http-sd.service"
sed -i "s|DATA_FILE=.*|DATA_FILE=${DATA_DIR}/devices.json|" "${SYSTEMD_DIR}/snmp-http-sd.service"
sed -i "s|--host 0.0.0.0 --port 8000|--host ${HOST} --port ${PORT}|" "${SYSTEMD_DIR}/snmp-http-sd.service"
sed -i "s|^User=.*|User=${SERVICE_USER}|" "${SYSTEMD_DIR}/snmp-http-sd.service"
sed -i "s|^Group=.*|Group=${SERVICE_USER}|" "${SYSTEMD_DIR}/snmp-http-sd.service"

systemctl daemon-reload
systemctl enable --now snmp-http-sd.service
ok "Service enabled and started"

echo ""
echo "=== Installation complete ==="
echo ""
echo "  App:      $INSTALL_DIR"
echo "  Data:     $DATA_DIR/devices.json"
echo "  Lookup:   $LOOKUP_PATH"
echo "  API:      http://${HOST}:${PORT}"
echo "  Targets:  http://${HOST}:${PORT}/targets"
echo ""
echo "  Commands:"
echo "    sudo systemctl status snmp-http-sd"
echo "    sudo journalctl -u snmp-http-sd -f"
echo ""
