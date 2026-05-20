#!/usr/bin/env python3
"""
NetMon - SNMP poller for router metrics.
Optional - disabled by default.
Polls interface counters, uptime, etc. from the Omada router via SNMP.
Designed as a oneshot systemd service triggered by a timer.
"""

import json
import logging
import os
import re
import subprocess

from common import load_config, influx_write, setup_logging, escape_tag, ts_now

LOG_NAME = "snmp_poller"
STATE_FILE = "/opt/netmon/data/snmp_state.json"


def snmp_get(host, community, oid, timeout=5):
    """Run snmpget and return the value as a string."""
    try:
        result = subprocess.run(
            ["snmpget", "-v2c", "-c", community, "-Oqv", host, oid],
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            logging.warning("snmpget %s failed: %s", oid, result.stderr.strip())
            return None
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logging.warning("snmpget %s timed out", oid)
        return None
    except FileNotFoundError:
        logging.error("snmpget not found - install with: apt install snmp")
        return None


def parse_snmp_value(raw):
    """Parse an SNMP value string to a number."""
    if raw is None:
        return None
    # Remove quotes and type prefixes
    raw = raw.strip().strip('"')
    # Counter32/Counter64/Gauge32 values
    m = re.search(r"(\d+)", raw)
    if m:
        return int(m.group(1))
    return None


def load_state():
    """Load previous counter values from state file."""
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}


def save_state(state):
    """Save current counter values to state file."""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except IOError as e:
        logging.warning("Failed to save SNMP state: %s", e)


def main():
    setup_logging(LOG_NAME)

    config = load_config()
    snmp_cfg = config.get("snmp", {})

    if not snmp_cfg.get("enabled", False):
        logging.debug("SNMP polling is disabled")
        return

    host = snmp_cfg.get("host", "192.168.0.1")
    community = snmp_cfg.get("community", "public")
    oids = snmp_cfg.get("oids", {})

    if not oids:
        logging.warning("No SNMP OIDs configured")
        return

    logging.info("Polling SNMP on %s", host)

    state = load_state()
    new_state = {}
    timestamp = ts_now()
    lines = []

    for name, oid in oids.items():
        raw = snmp_get(host, community, oid)
        value = parse_snmp_value(raw)
        if value is None:
            continue

        new_state[name] = value

        # Compute delta for counter OIDs
        delta = None
        if name in state and "octets" in name:
            prev = state[name]
            if value >= prev:
                delta = value - prev
            else:
                # Counter wrap (32-bit: 2^32, 64-bit: 2^64)
                delta = (2**32 - prev) + value

        tags = f"host={escape_tag(host)},oid_name={escape_tag(name)}"
        fields = f"value={value}i"
        if delta is not None:
            fields += f",delta={delta}i"
        lines.append(f"snmp,{tags} {fields} {timestamp}")

    if lines:
        influx_write(lines)
        logging.info("Polled %d SNMP OIDs", len(lines))

    save_state(new_state)


if __name__ == "__main__":
    main()
