#!/bin/bash
# NetMon - log an event annotation to InfluxDB.
# Shows up as a vertical mark on dashboards so we can correlate dashboard
# behavior with config/code changes, hardware tweaks, incidents, etc.
#
# Usage:
#   log_event.sh "description"                          # default category: config, ts=now
#   log_event.sh "description" category                 # custom category
#   log_event.sh "description" config "claude"          # custom source (default: $USER)
#   log_event.sh --at <unix_ts> "description" ...       # backfill at a specific time
#   log_event.sh --at "2026-05-20 02:08:00" "..."       # any GNU-date-parseable string
#
# Categories drive the annotation color in dashboards:
#   config = green   hw = blue   ops = orange   note = grey
set -euo pipefail

CUSTOM_TS=""
if [ "${1:-}" = "--at" ]; then
    [ $# -ge 3 ] || { echo "Usage: $0 --at <ts> \"description\" [category] [source]" >&2; exit 1; }
    if [[ "$2" =~ ^[0-9]+$ ]]; then
        CUSTOM_TS="$2"
    else
        CUSTOM_TS=$(date -d "$2" +%s 2>/dev/null) || {
            echo "log_event: could not parse timestamp '$2'" >&2; exit 1; }
    fi
    shift 2
fi

if [ $# -lt 1 ] || [ -z "$1" ]; then
    echo "Usage: $0 [--at <ts>] \"description\" [category] [source]" >&2
    exit 1
fi

DESC="$1"
CATEGORY="${2:-config}"
SOURCE="${3:-${USER:-unknown}}"

SECRETS="/opt/netmon/config/secrets.env"
if [ -f "$SECRETS" ]; then
    # shellcheck disable=SC1090
    set -a; source "$SECRETS"; set +a
fi

: "${INFLUX_TOKEN:?INFLUX_TOKEN not set}"
: "${INFLUX_URL:=http://127.0.0.1:8086}"
: "${INFLUX_ORG:=netmon}"
: "${INFLUX_BUCKET:=netmon}"

TS=${CUSTOM_TS:-$(date +%s)}

# Escape line-protocol special chars in tag values: space, comma, equals
esc_tag() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/ /\\ /g' -e 's/,/\\,/g' -e 's/=/\\=/g'; }
esc_field() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

CAT_T=$(esc_tag "$CATEGORY")
SRC_T=$(esc_tag "$SOURCE")
DESC_F=$(esc_field "$DESC")

LINE="netmon_event,category=${CAT_T},source=${SRC_T} description=\"${DESC_F}\" ${TS}"

curl -sf -X POST \
    "${INFLUX_URL}/api/v2/write?org=${INFLUX_ORG}&bucket=${INFLUX_BUCKET}&precision=s" \
    -H "Authorization: Token ${INFLUX_TOKEN}" \
    -H "Content-Type: text/plain; charset=utf-8" \
    --data-binary "$LINE" > /dev/null

# Marker for ping_monitor's settle window: only for live events (no --at) and
# only for categories that disrupt the network. ping_monitor tags incidents in
# the next event_settle_seconds as synthetic so a roaming/re-assoc storm right
# after a config change doesn't pollute the headline incident count.
if [ -z "$CUSTOM_TS" ] && { [ "$CATEGORY" = "config" ] || [ "$CATEGORY" = "hw" ]; }; then
    mkdir -p /run/netmon 2>/dev/null || true
    printf '%s %s\n' "$TS" "$CATEGORY" > /run/netmon/last_event_ts 2>/dev/null || true
fi

echo "[event] ${CATEGORY}: ${DESC}"
