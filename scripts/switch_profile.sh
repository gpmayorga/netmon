#!/bin/bash
# NetMon — switch the active network profile (eito / eito_plus / etc).
#
# Usage:
#   scripts/switch_profile.sh <profile-name>
#   scripts/switch_profile.sh --list
#
# What it does:
#   1. Validates the named profile exists in netmon.yml
#   2. Updates `active_profile` in netmon.yml
#   3. Logs a netmon_event annotation (so dashboards mark the change)
#   4. Restarts the affected daemon services so the new profile takes effect
#
# What it does NOT do:
#   - Switch the Pi's WiFi SSID (use `nmcli connection up <profile>` for that)
#   - Edit secrets.env (e.g. ROUTER_SSH still points wherever you set it)
#   - Reconfigure DHCP / static IPs
#
# Profiles are described in netmon.yml under `profiles:`. The script
# auto-restarts services that read profile state per cycle, so config
# changes take effect within ~60s without service restarts — but we
# restart anyway to drop sockets cleanly (e.g. syslog UDP listener).

set -euo pipefail

CONFIG="/opt/netmon/config/netmon.yml"
LOG_EVENT="/opt/netmon/scripts/log_event.sh"

err() { echo "[switch_profile] ERROR: $*" >&2; exit 1; }
info() { echo "[switch_profile] $*"; }

# Daemon services that change behavior based on profile.
# Re-listed here (vs reading from systemd) so this script is self-contained.
PROFILE_AFFECTED_DAEMONS=(
    netmon-ping
    netmon-router
    netmon-eap
    netmon-syslog-parser
    netmon-wifi-station
    netmon-wifi-scanner
)

[ $# -lt 1 ] && err "Usage: $0 <profile-name> | --list"

if [ "$1" = "--list" ] || [ "$1" = "-l" ]; then
    info "Available profiles:"
    python3 -c "
import yaml, sys
with open('$CONFIG') as f:
    c = yaml.safe_load(f)
active = c.get('active_profile', '<unset>')
profiles = c.get('profiles', {}) or {}
for name, p in profiles.items():
    marker = ' (active)' if name == active else ''
    desc = (p or {}).get('description', '')
    print(f'  {name}{marker}: {desc}')
"
    exit 0
fi

NEW_PROFILE="$1"

# Validate profile exists.
EXISTS=$(python3 -c "
import yaml
with open('$CONFIG') as f:
    c = yaml.safe_load(f)
print('1' if '$NEW_PROFILE' in (c.get('profiles') or {}) else '0')
")
[ "$EXISTS" = "1" ] || err "Profile '$NEW_PROFILE' not found in $CONFIG (run with --list to see names)"

CURRENT_PROFILE=$(python3 -c "
import yaml
with open('$CONFIG') as f:
    print(yaml.safe_load(f).get('active_profile', ''))
")

if [ "$CURRENT_PROFILE" = "$NEW_PROFILE" ]; then
    info "Already on profile '$NEW_PROFILE' — nothing to do"
    exit 0
fi

info "Switching active_profile: '$CURRENT_PROFILE' -> '$NEW_PROFILE'"

# Log BEFORE the change so the dashboard annotation marks the start of any
# settle storm caused by services restarting.
if [ -x "$LOG_EVENT" ]; then
    "$LOG_EVENT" "netmon profile: ${CURRENT_PROFILE} -> ${NEW_PROFILE}" config "switch_profile.sh" || true
fi

# Update active_profile line in-place. We do this with sed on the exact key
# at column 0 to avoid touching profile blocks' `description:` etc.
# Tolerates quoted ("eito") or unquoted (eito) values.
sed -i.bak -E "s/^active_profile:\s*\"?[^\"]*\"?\s*$/active_profile: \"${NEW_PROFILE}\"/" "$CONFIG"

# Verify the edit landed.
ACTUAL=$(python3 -c "
import yaml
with open('$CONFIG') as f:
    print(yaml.safe_load(f).get('active_profile', ''))
")
if [ "$ACTUAL" != "$NEW_PROFILE" ]; then
    info "sed edit failed — restoring backup"
    mv "${CONFIG}.bak" "$CONFIG"
    err "Could not update active_profile in $CONFIG"
fi
rm -f "${CONFIG}.bak"

# Restart affected daemons so they re-evaluate the profile.
info "Restarting affected services..."
for svc in "${PROFILE_AFFECTED_DAEMONS[@]}"; do
    if systemctl is-active --quiet "$svc"; then
        info "  - $svc"
        sudo systemctl restart "$svc" || info "    (restart failed, continuing)"
    else
        info "  - $svc (not active, skipping)"
    fi
done

info "Done. Now on profile: $NEW_PROFILE"
info ""
info "Next steps if also switching WiFi network:"
info "  sudo nmcli connection up <SSID>     # e.g. Eito_plus"
info ""
info "Verify with:"
info "  journalctl -u netmon-ping -n 5 --no-pager"
info "  journalctl -u netmon-eap -n 3 --no-pager     # should show 'idle' on non-omada profiles"
