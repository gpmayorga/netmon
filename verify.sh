#!/bin/bash
# NetMon - sanity checks across config, scripts, services, InfluxDB, dashboards, and the
# ap_location tag flow. Modular: pass one or more section names to limit the scope.
#
# Usage:
#   bash /opt/netmon/verify.sh                  # run all sections
#   bash /opt/netmon/verify.sh dashboards       # only validate dashboard JSON
#   bash /opt/netmon/verify.sh config scripts   # multiple sections
#
# Sections: config scripts services influx dashboards ap_labels
#
# Exit code: 0 if all checks pass, 1 if any [FAIL] was emitted. [WARN] does not fail.

set -u
cd "$(dirname "$0")"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; BLUE='\033[0;34m'; NC='\033[0m'
FAIL=0
pass()    { echo -e "  ${GREEN}[OK]${NC}    $*"; }
fail()    { echo -e "  ${RED}[FAIL]${NC}  $*"; FAIL=1; }
warn()    { echo -e "  ${YELLOW}[WARN]${NC}  $*"; }
section() { echo; echo -e "${BLUE}=== $* ===${NC}"; }

# Load secrets so INFLUX_TOKEN is available to the influx/ap_labels checks.
if [ -f config/secrets.env ]; then
    set -a; source config/secrets.env; set +a
fi

DAEMON_SERVICES=(netmon-ping netmon-ipcheck netmon-wifi-station netmon-wifi-scanner
                 netmon-syslog-parser netmon-router netmon-eap)
TIMER_SERVICES=(netmon-speedtest netmon-snmp netmon-iperf3)

# Run a Flux query and echo the raw CSV. Empty on transport failure.
flux_query() {
    local q="$1"
    curl -s --max-time 10 -X POST "http://127.0.0.1:8086/api/v2/query?org=netmon" \
        -H "Authorization: Token ${INFLUX_TOKEN:-}" \
        -H "Content-Type: application/vnd.flux" \
        -d "$q" 2>/dev/null
}

check_config() {
    section "config/netmon.yml"
    if ! python3 -c "import yaml; yaml.safe_load(open('config/netmon.yml'))" 2>/dev/null; then
        fail "YAML parse error"
        return
    fi
    pass "YAML parses"

    python3 - <<'PY' || FAIL=1
import re, sys, yaml
cfg = yaml.safe_load(open("config/netmon.yml"))
required = ["influxdb", "ping", "wifi_station", "wifi_scanner"]
missing = [k for k in required if k not in cfg]
if missing:
    print(f"  [FAIL]  missing required sections: {missing}")
    sys.exit(1)
for k in required:
    print(f"  [OK]    section present: {k}")

labels = cfg.get("ap_labels") or {}
if not labels:
    print("  [WARN]  no ap_labels configured (panels will all show 'unknown')")
else:
    bad = [k for k in labels if not re.match(r"^[0-9a-f:]{17}$", k.lower())]
    if bad:
        print(f"  [FAIL]  malformed MAC(s) in ap_labels: {bad}")
        sys.exit(1)
    print(f"  [OK]    {len(labels)} ap_labels entries (all valid MACs)")
PY
}

check_scripts() {
    section "Python scripts (syntax)"
    local ok=1
    for f in scripts/*.py; do
        if python3 -m py_compile "$f" 2>/dev/null; then
            pass "compiles: $(basename "$f")"
        else
            fail "syntax error: $f"
            ok=0
        fi
    done
}

check_services() {
    section "systemd services"
    if ! command -v systemctl >/dev/null; then
        warn "systemctl not available (skipping)"
        return
    fi
    for svc in "${DAEMON_SERVICES[@]}"; do
        if ! systemctl list-unit-files | grep -q "^${svc}.service"; then
            warn "not installed: $svc"
            continue
        fi
        if systemctl is-active --quiet "$svc.service"; then
            pass "active: $svc"
        else
            local state
            state=$(systemctl is-active "$svc.service" 2>/dev/null || true)
            fail "$svc is $state"
        fi
    done
    for tmr in "${TIMER_SERVICES[@]}"; do
        if ! systemctl list-unit-files | grep -q "^${tmr}.timer"; then
            warn "not installed: $tmr.timer"
            continue
        fi
        if systemctl is-active --quiet "$tmr.timer"; then
            local next
            next=$(systemctl show -p NextElapseUSecRealtime --value "$tmr.timer")
            pass "timer active: $tmr (next: $next)"
        else
            fail "timer inactive: $tmr"
        fi
    done
}

check_influx() {
    section "InfluxDB"
    if [ -z "${INFLUX_TOKEN:-}" ]; then
        fail "INFLUX_TOKEN not set (source config/secrets.env)"
        return
    fi
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:8086/health" 2>/dev/null || echo "000")
    if [ "$code" = "200" ]; then
        pass "InfluxDB /health -> 200"
    else
        fail "InfluxDB /health -> $code (is the container up?)"
        return
    fi

    # Per-measurement freshness. Each entry: "measurement|window|severity-if-stale"
    # severity: fail (must be fresh) or warn (low-frequency, may legitimately be old)
    local checks=(
        "wifi_station|2m|fail"
        "ping|2m|fail"
        "system|2m|fail"
        "wifi_scan_ap|15m|warn"
        "wifi_channel_summary|15m|warn"
        "router_wan|5m|warn"
        "public_ip|10m|warn"
    )
    for entry in "${checks[@]}"; do
        IFS='|' read -r m window sev <<<"$entry"
        local out
        out=$(flux_query "from(bucket:\"netmon\") |> range(start: -$window) |> filter(fn: (r) => r._measurement == \"$m\") |> limit(n:1)")
        if echo "$out" | grep -q "_result"; then
            pass "$m has data in last $window"
        else
            if [ "$sev" = "fail" ]; then fail "$m has no data in last $window"
            else warn "$m has no data in last $window"; fi
        fi
    done
}

check_dashboards() {
    section "Grafana dashboards"
    local count=0
    for f in config/grafana/dashboards/*.json; do
        if python3 -c "import json; json.load(open('$f'))" 2>/dev/null; then
            pass "valid JSON: $(basename "$f")"
            count=$((count+1))
        else
            fail "invalid JSON: $f"
        fi
    done
    [ $count -gt 0 ] || warn "no dashboard files found"

    if command -v docker >/dev/null; then
        for c in netmon-grafana netmon-influxdb; do
            if docker inspect "$c" >/dev/null 2>&1; then
                local status
                status=$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null)
                if [ "$status" = "running" ]; then pass "$c container running"
                else fail "$c container state: $status"; fi
            else
                warn "$c container not found"
            fi
        done
    else
        warn "docker not available (skipping container checks)"
    fi
}

check_ap_labels() {
    section "ap_location tag flow"
    if [ -z "${INFLUX_TOKEN:-}" ]; then
        fail "INFLUX_TOKEN not set"
        return
    fi

    local out
    out=$(flux_query 'from(bucket:"netmon") |> range(start: -5m) |> filter(fn: (r) => r._measurement == "wifi_station" and r._field == "signal_dbm" and exists r.ap_location) |> last() |> keep(columns: ["_value","bssid","ap_location"])')
    if echo "$out" | grep -q "ap_location"; then
        pass "wifi_station has ap_location in last 5m"
        echo "$out" | tail -n +2 | grep -v '^$' | sed 's/^/      /'
    else
        fail "wifi_station has no ap_location in last 5m (did wifi-station restart after the change?)"
    fi

    out=$(flux_query 'from(bucket:"netmon") |> range(start: -15m) |> filter(fn: (r) => r._measurement == "wifi_scan_ap" and exists r.ap_location) |> keep(columns: ["ap_location"]) |> group() |> distinct(column: "ap_location")')
    if echo "$out" | grep -q "ap_location"; then
        local locs
        locs=$(echo "$out" | awk -F, '/^,_result/ {print $NF}' | sort -u | tr '\n' ' ')
        pass "wifi_scan_ap has ap_location in last 15m (locations: $locs)"
    else
        warn "wifi_scan_ap has no ap_location yet (scanner runs every 5m — check again later)"
    fi

    # Spot-check: every label value in ap_labels should match what scripts emit.
    python3 - <<'PY' || true
import yaml
cfg = yaml.safe_load(open("config/netmon.yml"))
labels = cfg.get("ap_labels") or {}
unique = sorted(set(labels.values()))
print(f"      configured locations: {unique}")
PY
}

ALL=(config scripts services influx dashboards ap_labels)
if [ $# -eq 0 ]; then
    sections=("${ALL[@]}")
else
    sections=("$@")
fi

echo "NetMon verify ($(date -Iseconds))"
for s in "${sections[@]}"; do
    case "$s" in
        config)     check_config ;;
        scripts)    check_scripts ;;
        services)   check_services ;;
        influx)     check_influx ;;
        dashboards) check_dashboards ;;
        ap_labels)  check_ap_labels ;;
        all)        for x in "${ALL[@]}"; do "check_$x"; done ;;
        *) echo "Unknown section: $s (valid: ${ALL[*]})"; exit 2 ;;
    esac
done

echo
if [ "$FAIL" -eq 0 ]; then
    echo -e "${GREEN}All checks passed.${NC}"
    exit 0
else
    echo -e "${RED}Some checks failed.${NC}"
    exit 1
fi
