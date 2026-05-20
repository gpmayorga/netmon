#!/usr/bin/env python3
"""
NetMon - Omada Router Monitor via SSH.
Polls the TP-Link Omada router for WAN port status, system info,
and can run router-side pings for incident diagnosis.

Based on patterns from /opt/internet-monitoring/monitor.py.

Requires:
- ROUTER_SSH env var (e.g. "user@192.168.0.1")
- ROUTER_SSH_PASSWORD env var (optional, for password auth)
- SSH key-based auth configured (if no password)
"""

import logging
import os
import re
import time

from common import load_config, influx_write, setup_logging, escape_tag, escape_field_str, ts_now
from ssh_helper import run_commands

LOG_NAME = "router_monitor"


def _port_status_from_text(text):
    """Pick UP/DOWN out of a single 'show interface switchport N' output."""
    for line in text.splitlines():
        if "Routing Interface Status" in line:
            return "UP" if line.rstrip().endswith("UP") else "DOWN"
    return None


def _split_kv(line):
    """Split a 'key SEP value' line. Tries ' - ' then ':' as separators.
    Newer Omada firmware (ER706W) uses ' - '; older ones use ':'."""
    m = re.match(r"^(.*?)\s+-\s+(.*)$", line)
    if m:
        return m.group(1), m.group(2)
    if ":" in line:
        k, _, v = line.partition(":")
        return k, v
    return None, None


def parse_system_info(output):
    """Parse 'show system-info' output for uptime, CPU, memory, firmware, temp.
    Field availability varies by model: ER706W exposes firmware + running time only."""
    result = {}
    for line in output.splitlines():
        key, val = _split_kv(line)
        if key is None:
            continue
        key_lower = key.strip().lower()
        val = val.strip()

        if "uptime" in key_lower or "running time" in key_lower:
            result["uptime_str"] = val
            # Try "1 days, 02:03:04"
            m = re.search(r"(\d+)\s*days?[,\s]+(\d+):(\d+):(\d+)", val, re.I)
            if m:
                d, h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                result["uptime_seconds"] = d * 86400 + h * 3600 + mi * 60 + s
            else:
                # Try "18 day - 4 hour - 32 min - 29 sec" (ER706W format)
                d = re.search(r"(\d+)\s*day", val, re.I)
                h = re.search(r"(\d+)\s*hour", val, re.I)
                mi = re.search(r"(\d+)\s*min", val, re.I)
                s = re.search(r"(\d+)\s*sec", val, re.I)
                if any([d, h, mi, s]):
                    result["uptime_seconds"] = (
                        (int(d.group(1)) if d else 0) * 86400
                        + (int(h.group(1)) if h else 0) * 3600
                        + (int(mi.group(1)) if mi else 0) * 60
                        + (int(s.group(1)) if s else 0)
                    )
        elif "cpu" in key_lower and "usage" in key_lower:
            try:
                result["cpu_percent"] = float(re.sub(r"[^\d.]", "", val) or "0")
            except ValueError:
                pass
        elif "memory" in key_lower and "usage" in key_lower:
            try:
                result["memory_percent"] = float(re.sub(r"[^\d.]", "", val) or "0")
            except ValueError:
                pass
        elif "firmware" in key_lower or "software version" in key_lower:
            result["firmware"] = val
        elif "temperature" in key_lower or key_lower == "temp":
            try:
                result["temp_c"] = float(re.sub(r"[^\d.]", "", val) or "0")
            except ValueError:
                pass
    return result


def get_wan_status(ssh_target, password, port=None):
    """Get WAN port status (UP/DOWN) for ports 1-3."""
    commands = [f"show interface switchport {p}" for p in (1, 2, 3)]
    outputs = run_commands(ssh_target, password, commands, timeout_sec=15, port=port)
    if not outputs:
        return None
    result = {}
    for port_num, text in zip((1, 2, 3), outputs):
        result[f"port{port_num}"] = _port_status_from_text(text)
    return result if any(v is not None for v in result.values()) else None


def get_system_info(ssh_target, password, port=None):
    """Get router system info (CPU, memory, uptime, firmware, temp)."""
    outputs = run_commands(ssh_target, password, ["show system-info"], timeout_sec=15, port=port)
    if not outputs:
        return None
    return parse_system_info(outputs[0]) or None


def format_wan_line(wan_status, timestamp):
    """Format WAN status as InfluxDB line protocol."""
    fields = []
    for port_key in ("port1", "port2", "port3"):
        status = wan_status.get(port_key)
        if status is not None:
            is_up = 1 if status == "UP" else 0
            fields.append(f'{port_key}_up={is_up}i')
            fields.append(f'{port_key}_status="{escape_field_str(status)}"')
    if not fields:
        return None
    return f"router_wan {','.join(fields)} {timestamp}"


def format_sysinfo_line(info, timestamp):
    """Format router system info as InfluxDB line protocol."""
    fields = []
    for key in ("uptime_seconds", "cpu_percent", "memory_percent", "temp_c"):
        if key in info:
            val = info[key]
            if isinstance(val, int):
                fields.append(f"{key}={val}i")
            else:
                fields.append(f"{key}={val}")
    if "firmware" in info:
        fields.append(f'firmware="{escape_field_str(info["firmware"])}"')
    if not fields:
        return None
    return f"router_info {','.join(fields)} {timestamp}"


def main():
    setup_logging(LOG_NAME)
    logging.info("Starting router monitor")

    ssh_target = os.environ.get("ROUTER_SSH", "").strip()
    password = os.environ.get("ROUTER_SSH_PASSWORD", "").strip()

    if not ssh_target:
        logging.warning("ROUTER_SSH not set - router monitoring disabled. "
                        "Set ROUTER_SSH=user@192.168.0.1 in secrets.env")
        # Sleep forever so systemd doesn't restart-loop
        while True:
            time.sleep(3600)

    logging.info("Router SSH target: %s (auth: %s)",
                 ssh_target, "password" if password else "key-based")

    prev_wan = None
    cycle_count = 0

    while True:
        try:
            config = load_config()
            router_cfg = config.get("router", {})
            interval = int(router_cfg.get("interval", 60))
            sysinfo_interval = int(router_cfg.get("sysinfo_interval", 1))
            ssh_port = router_cfg.get("ssh_port")

            timestamp = ts_now()
            lines = []

            # WAN port status
            wan = get_wan_status(ssh_target, password, port=ssh_port)
            if wan:
                wan_line = format_wan_line(wan, timestamp)
                if wan_line:
                    lines.append(wan_line)

                if prev_wan is not None:
                    for port in ("port1", "port2", "port3"):
                        old = prev_wan.get(port)
                        new = wan.get(port)
                        if old and new and old != new:
                            logging.warning("WAN %s changed: %s -> %s", port, old, new)
                            event_line = (
                                f'wan_event,type=port_change,port={port} '
                                f'previous="{old}",current="{new}",'
                                f'message="WAN {port} changed: {old} -> {new}" '
                                f'{timestamp}'
                            )
                            lines.append(event_line)
                prev_wan = wan
            else:
                logging.debug("Failed to get WAN status")

            # System info — cadence governed by sysinfo_interval (cycles).
            if cycle_count % max(sysinfo_interval, 1) == 0:
                info = get_system_info(ssh_target, password, port=ssh_port)
                if info:
                    info_line = format_sysinfo_line(info, timestamp)
                    if info_line:
                        lines.append(info_line)
                    logging.debug("Router: CPU=%s%%, Mem=%s%%, Uptime=%s",
                                  info.get("cpu_percent", "?"),
                                  info.get("memory_percent", "?"),
                                  info.get("uptime_str", "unknown"))
            cycle_count += 1

            if lines:
                influx_write(lines)

        except Exception as e:
            logging.error("Router monitor cycle error: %s", e, exc_info=True)

        time.sleep(interval)


if __name__ == "__main__":
    main()
