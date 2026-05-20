#!/usr/bin/env python3
"""
NetMon - Router-side ping for incident attribution.

Invoked on demand (typically by ping_monitor when an RTT-spike or loss incident
fires). Opens an SSH session to the Omada router and runs `ping <target>` —
the router CLI accepts ONLY the destination (no -I, no -c, no -W). The router
sends 4 pings via whatever WAN the load balancer picks.

The value: when Pi-side RTT spikes but router-side RTT to the same target is
normal, the problem is between Pi and router (WiFi/mesh/LAN). When both spike,
the problem is upstream of the router. This attribution still works even
though we can't isolate Movistar-vs-Orange.

Per-WAN isolation would need reverse-engineering the Omada local web API
(see TODO note in classify_incident).

Usage:
  python3 wan_ping.py --reason <incident_id>
"""

import argparse
import logging
import os
import re
import sys
import time

from common import load_config, influx_write, setup_logging, escape_tag, ts_now
from ssh_helper import run_commands

LOG_NAME = "wan_ping"

# Track last run timestamp in a file so concurrent ping_monitor cycles
# don't fire overlapping SSH sessions.
LAST_RUN_FILE = "/tmp/netmon-wan-ping.last"


def _rate_limited(min_interval):
    """Return True if we ran within min_interval seconds."""
    try:
        with open(LAST_RUN_FILE, "r") as f:
            last = float(f.read().strip())
        if time.time() - last < min_interval:
            return True
    except (IOError, ValueError):
        pass
    return False


def _mark_run():
    try:
        with open(LAST_RUN_FILE, "w") as f:
            f.write(str(time.time()))
    except IOError:
        pass


PING_STATS_RE = re.compile(
    r"(\d+)\s*packets?\s*transmitted.*?(\d+)\s*(?:packets?\s*)?received",
    re.IGNORECASE | re.DOTALL,
)
PING_LOSS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:packet\s*)?loss", re.IGNORECASE)
PING_RTT_RE = re.compile(
    r"(?:rtt|round-trip).*?=\s*([\d.]+)\s*/\s*([\d.]+)\s*/\s*([\d.]+)(?:\s*/\s*([\d.]+))?",
    re.IGNORECASE,
)


def parse_ping_output(text):
    """Parse standard `ping` summary lines. Returns dict or None."""
    if not text:
        return None
    result = {}
    m = PING_STATS_RE.search(text)
    if m:
        result["tx"] = int(m.group(1))
        result["rx"] = int(m.group(2))
    m = PING_LOSS_RE.search(text)
    if m:
        result["loss_pct"] = float(m.group(1))
    m = PING_RTT_RE.search(text)
    if m:
        result["rtt_min"] = float(m.group(1))
        result["rtt_avg"] = float(m.group(2))
        result["rtt_max"] = float(m.group(3))
        if m.group(4):
            result["rtt_mdev"] = float(m.group(4))
    if not result:
        return None
    return result


def format_line(reason, target, parsed, timestamp):
    """Format router_ping measurement line."""
    tags = (
        f"source=router,"
        f"target={escape_tag(target)},"
        f"reason={escape_tag(reason)}"
    )
    fields = []
    for k, ftype in (("tx", "i"), ("rx", "i")):
        if k in parsed:
            fields.append(f"{k}={parsed[k]}{ftype}")
    for k in ("loss_pct", "rtt_min", "rtt_avg", "rtt_max", "rtt_mdev"):
        if k in parsed:
            fields.append(f"{k}={parsed[k]}")
    if not fields:
        return None
    return f"router_ping,{tags} {','.join(fields)} {timestamp}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reason", default="manual", help="Incident id or trigger label")
    parser.add_argument("--force", action="store_true", help="Skip rate-limit check")
    args = parser.parse_args()

    setup_logging(LOG_NAME)

    config = load_config()
    cfg = config.get("wan_ping", {})
    if not cfg.get("enabled", True):
        logging.info("wan_ping disabled in config, skipping")
        return 0

    min_interval = int(cfg.get("min_interval", 30))
    if not args.force and _rate_limited(min_interval):
        logging.debug("wan_ping rate-limited (last run within %ds)", min_interval)
        return 0

    ssh_target = os.environ.get("ROUTER_SSH", "").strip()
    password = os.environ.get("ROUTER_SSH_PASSWORD", "").strip()
    if not ssh_target:
        logging.warning("ROUTER_SSH not set; cannot run router-side ping")
        return 1

    target = cfg.get("target", "8.8.8.8")
    # Omada CLI ping accepts ONLY the destination address — no flags. It sends
    # 4 packets by default. To probe multiple targets, send multiple commands.
    targets = cfg.get("targets") or [target]
    commands = [f"ping {t}" for t in targets]
    logging.info("Running router-side pings (reason=%s, targets=%s)", args.reason, targets)
    outputs = run_commands(ssh_target, password, commands, timeout_sec=30)
    _mark_run()
    if outputs is None:
        logging.error("SSH session failed")
        return 1

    timestamp = ts_now()
    lines = []
    for tgt, out in zip(targets, outputs):
        parsed = parse_ping_output(out)
        if not parsed:
            logging.warning("Could not parse router ping output for %s; raw=%r",
                            tgt, (out or "")[:200])
            continue
        line = format_line(args.reason, tgt, parsed, timestamp)
        if line:
            lines.append(line)
        logging.info("Router-side %s: loss=%s%% avg=%sms max=%sms",
                     tgt, parsed.get("loss_pct", "?"),
                     parsed.get("rtt_avg", "?"),
                     parsed.get("rtt_max", "?"))

    if lines:
        influx_write(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
