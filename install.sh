#!/usr/bin/env bash
# =============================================================================
# NetMon - Coworking Network Monitor - Installer
# =============================================================================
# Run as root: sudo bash /opt/netmon/install.sh
# Idempotent: safe to run multiple times.
set -euo pipefail

NETMON_DIR="/opt/netmon"
LOG_DIR="/var/log/netmon"
SECRETS_FILE="$NETMON_DIR/config/secrets.env"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# Check root
if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (sudo bash install.sh)"
    exit 1
fi

echo ""
echo "=========================================="
echo "  NetMon - Network Monitor Installer"
echo "=========================================="
echo ""

# -------------------------------------------------------------------------
# Phase 1: System packages
# -------------------------------------------------------------------------
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq fping iw wireless-tools speedtest-cli iperf3 snmp jq python3-yaml python3-requests python3-pexpect 2>/dev/null
ok "System packages installed"

# Set capabilities for fping (allows non-root ping)
if command -v fping &>/dev/null; then
    setcap cap_net_raw+ep "$(which fping)" 2>/dev/null || true
fi

# -------------------------------------------------------------------------
# Phase 2: Directory structure
# -------------------------------------------------------------------------
info "Creating directory structure..."
mkdir -p "$NETMON_DIR"/{config/grafana/provisioning/{datasources,dashboards,alerting},config/grafana/dashboards,config/rsyslog,scripts,systemd,data/{influxdb,influxdb-config,grafana}}
mkdir -p "$LOG_DIR"
ok "Directories created"

# -------------------------------------------------------------------------
# Phase 3: Generate secrets
# -------------------------------------------------------------------------
if [[ ! -f "$SECRETS_FILE" ]]; then
    info "Generating credentials..."
    INFLUX_TOKEN=$(openssl rand -hex 32)
    INFLUX_PASSWORD=$(openssl rand -base64 16 | tr -d '=/+' | head -c 20)
    GRAFANA_PASSWORD=$(openssl rand -base64 12 | tr -d '=/+' | head -c 16)

    cat > "$SECRETS_FILE" <<SECRETS
# NetMon secrets - generated $(date -Iseconds)
# DO NOT commit this file to version control

# InfluxDB
INFLUX_USER=admin
INFLUX_PASSWORD=${INFLUX_PASSWORD}
INFLUX_ORG=netmon
INFLUX_BUCKET=netmon
INFLUX_RETENTION=720h
INFLUX_TOKEN=${INFLUX_TOKEN}

# Grafana
GRAFANA_USER=admin
GRAFANA_PASSWORD=${GRAFANA_PASSWORD}

# InfluxDB connection for scripts
INFLUX_URL=http://127.0.0.1:8086

# Omada Router SSH (uncomment and configure)
#ROUTER_SSH=user@192.168.0.1
#ROUTER_SSH_PASSWORD=your_password_here

# Omada EAP SSH (mesh AP polling). Site-wide SSH must be enabled in the
# Omada controller (Settings -> Site -> Services -> Device Account & SSH).
# If left unset, eap_monitor.py reuses ROUTER_SSH's username with EAP host
# from netmon.yml, and ROUTER_SSH_PASSWORD as the device account password.
#EAP_SSH=admin@192.168.0.100
#EAP_SSH_PASSWORD=your_password_here
SECRETS
    chmod 600 "$SECRETS_FILE"

    echo ""
    echo "============================================"
    echo -e "  ${GREEN}Grafana admin password: ${GRAFANA_PASSWORD}${NC}"
    echo -e "  ${GREEN}InfluxDB admin password: ${INFLUX_PASSWORD}${NC}"
    echo "  Save these! They won't be shown again."
    echo "============================================"
    echo ""
else
    ok "Secrets file already exists, skipping generation"
fi

# -------------------------------------------------------------------------
# Phase 4: Set file permissions
# -------------------------------------------------------------------------
info "Setting permissions..."
chmod 755 "$NETMON_DIR"/scripts/*.py 2>/dev/null || true
chmod 600 "$SECRETS_FILE"
ok "Permissions set"

# -------------------------------------------------------------------------
# Phase 5: Configure logrotate
# -------------------------------------------------------------------------
info "Configuring logrotate..."
cat > /etc/logrotate.d/netmon <<'LOGROTATE'
/var/log/netmon/*.log {
    daily
    rotate 30
    compress
    missingok
    notifempty
    create 0640 root root
}
LOGROTATE
ok "Logrotate configured"

# -------------------------------------------------------------------------
# Phase 6: Docker stack
# -------------------------------------------------------------------------
info "Starting Docker stack (InfluxDB + Grafana)..."

# Ensure data dirs have correct ownership for Docker containers
chown -R 472:472 "$NETMON_DIR/data/grafana" 2>/dev/null || true  # grafana UID

cd "$NETMON_DIR"
docker compose --env-file "$SECRETS_FILE" pull -q 2>/dev/null || docker compose --env-file "$SECRETS_FILE" pull
docker compose --env-file "$SECRETS_FILE" up -d

# Wait for InfluxDB health
info "Waiting for InfluxDB to be ready..."
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8086/health >/dev/null 2>&1; then
        ok "InfluxDB is healthy"
        break
    fi
    if [[ $i -eq 30 ]]; then
        warn "InfluxDB health check timed out (may still be initializing)"
    fi
    sleep 2
done

# Create speedtest bucket with 90-day retention
info "Creating additional InfluxDB buckets..."
source "$SECRETS_FILE"
# Get org ID
ORG_ID=$(curl -sf "http://127.0.0.1:8086/api/v2/orgs" \
    -H "Authorization: Token ${INFLUX_TOKEN}" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['orgs'][0]['id'])" 2>/dev/null || echo "")

if [[ -n "$ORG_ID" ]]; then
    # Create netmon_speedtest bucket (90 days)
    curl -sf -X POST "http://127.0.0.1:8086/api/v2/buckets" \
        -H "Authorization: Token ${INFLUX_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"name\":\"netmon_speedtest\",\"orgID\":\"${ORG_ID}\",\"retentionRules\":[{\"type\":\"expire\",\"everySeconds\":7776000}]}" \
        >/dev/null 2>&1 || true
    ok "Buckets configured"
else
    warn "Could not get InfluxDB org ID (buckets may need manual creation)"
fi

# Wait for Grafana
info "Waiting for Grafana to be ready..."
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:3000/api/health >/dev/null 2>&1; then
        ok "Grafana is healthy"
        break
    fi
    if [[ $i -eq 30 ]]; then
        warn "Grafana health check timed out"
    fi
    sleep 2
done

# -------------------------------------------------------------------------
# Phase 6.5: Pre-restart sanity check
# -------------------------------------------------------------------------
# Fail fast on malformed config / dashboards / Python before we tear down
# any currently-running collectors. Live checks (services, influx) run in
# Phase 8 after the restart.
info "Running pre-restart static checks..."
if ! bash "$NETMON_DIR/verify.sh" config scripts dashboards; then
    err "Static checks failed — aborting before service restart."
    err "Existing services are still running with the previous config."
    exit 1
fi
ok "Static checks passed"

# -------------------------------------------------------------------------
# Phase 7: Install systemd services
# -------------------------------------------------------------------------
info "Installing systemd services..."
cp "$NETMON_DIR"/systemd/*.service /etc/systemd/system/ 2>/dev/null || true
cp "$NETMON_DIR"/systemd/*.timer /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload

# Enable and start daemon services
DAEMON_SERVICES=(
    netmon-ping
    netmon-ipcheck
    netmon-wifi-station
    netmon-wifi-scanner
    netmon-syslog-parser
    netmon-router
    netmon-eap
)

for svc in "${DAEMON_SERVICES[@]}"; do
    if systemctl enable "$svc.service" 2>/dev/null; then
        systemctl restart "$svc.service" 2>/dev/null || true
        ok "  $svc enabled and started"
    fi
done

# Enable timer-based services
TIMER_SERVICES=(
    netmon-speedtest
    netmon-snmp
    netmon-iperf3
)

for timer in "${TIMER_SERVICES[@]}"; do
    if systemctl enable "$timer.timer" 2>/dev/null; then
        systemctl start "$timer.timer" 2>/dev/null || true
        ok "  $timer timer enabled"
    fi
done

# -------------------------------------------------------------------------
# Phase 8: Verification
# -------------------------------------------------------------------------
echo ""
info "Verifying services..."
echo ""

echo "--- Docker Containers ---"
docker compose ps --format "table {{.Name}}\t{{.Status}}"
echo ""

echo "--- Monitoring Services ---"
for svc in "${DAEMON_SERVICES[@]}"; do
    status=$(systemctl is-active "$svc.service" 2>/dev/null || echo "inactive")
    if [[ "$status" == "active" ]]; then
        echo -e "  ${GREEN}●${NC} $svc: $status"
    else
        echo -e "  ${RED}●${NC} $svc: $status"
    fi
done
echo ""

echo "--- Timers ---"
for timer in "${TIMER_SERVICES[@]}"; do
    status=$(systemctl is-active "$timer.timer" 2>/dev/null || echo "inactive")
    if [[ "$status" == "active" ]]; then
        echo -e "  ${GREEN}●${NC} $timer: $status"
    else
        echo -e "  ${RED}●${NC} $timer: $status"
    fi
done

# Post-restart live verification. Non-fatal: timer-driven measurements may
# legitimately have no data yet on a fresh install.
echo ""
info "Running post-restart live checks..."
sleep 5
if ! bash "$NETMON_DIR/verify.sh" services influx ap_labels; then
    warn "Post-install verify reported issues (see above) — review before relying on the dashboard."
fi

# -------------------------------------------------------------------------
# Phase 9: Get Tailscale IP for access instructions
# -------------------------------------------------------------------------
TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "100.x.x.x")

echo ""
echo "=========================================="
echo -e "  ${GREEN}Installation Complete!${NC}"
echo "=========================================="
echo ""
echo "  Access Grafana:"
echo "    http://${TAILSCALE_IP}:3000  (via Tailscale - requires port binding update)"
echo "    ssh -L 3000:127.0.0.1:3000 pi@${TAILSCALE_IP}  (SSH tunnel)"
echo "    Then open: http://localhost:3000"
echo ""
echo "  View logs:"
echo "    journalctl -u netmon-ping -f"
echo "    journalctl -u netmon-ipcheck -f"
echo ""
echo "  Force a speedtest now:"
echo "    sudo systemctl start netmon-speedtest.service"
echo ""
echo "  Configure router SSH monitoring:"
echo "    Edit $SECRETS_FILE"
echo "    Uncomment ROUTER_SSH and ROUTER_SSH_PASSWORD"
echo "    sudo systemctl restart netmon-router"
echo ""
echo "  Configure syslog from Omada router:"
echo "    Set remote syslog target to: $(hostname -I | awk '{print $1}'):5514"
echo ""
