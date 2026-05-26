#!/usr/bin/env python3
"""
NetMon - Syslog receiver and parser.
Listens on UDP for syslog messages from the Omada router,
parses and classifies events, writes to InfluxDB.
"""

import logging
import re
import socket
import time

from common import load_config, get_active_profile, influx_write, setup_logging, escape_tag, escape_field_str, ts_now

LOG_NAME = "syslog_parser"

# RFC 3164 syslog format
SYSLOG_RE = re.compile(
    r"<(\d{1,3})>"                     # PRI
    r"(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"  # timestamp
    r"(\S+)\s+"                        # hostname
    r"(.+)"                            # message (rest of line)
)

SEVERITY_MAP = {
    0: "emerg", 1: "alert", 2: "crit", 3: "err",
    4: "warning", 5: "notice", 6: "info", 7: "debug"
}

# Event classification patterns
EVENT_PATTERNS = [
    (re.compile(r"DHCP|dhcp|lease", re.I), "dhcp"),
    (re.compile(r"link\s+(up|down)|carrier|interface.*(up|down)", re.I), "link"),
    (re.compile(r"(associated|disassociated|deauth|client.*connect)", re.I), "client_wifi"),
    (re.compile(r"failover|failback|WAN|wan.*switch", re.I), "wan_event"),
    (re.compile(r"firewall|drop|reject|block|iptables", re.I), "firewall"),
    (re.compile(r"auth|login|password|radius", re.I), "auth"),
    (re.compile(r"firmware|upgrade|reboot|restart", re.I), "system"),
]


def parse_syslog_message(data):
    """Parse a raw syslog UDP datagram."""
    try:
        text = data.decode("utf-8", errors="replace").strip()
    except Exception:
        return None

    m = SYSLOG_RE.match(text)
    if m:
        pri = int(m.group(1))
        severity_num = pri & 0x07
        facility_num = pri >> 3
        return {
            "timestamp": m.group(2),
            "hostname": m.group(3),
            "message": m.group(4),
            "severity": SEVERITY_MAP.get(severity_num, str(severity_num)),
            "severity_num": severity_num,
            "facility_num": facility_num,
            "raw": text,
        }
    else:
        # Try to parse as plain text
        return {
            "timestamp": "",
            "hostname": "unknown",
            "message": text,
            "severity": "info",
            "severity_num": 6,
            "facility_num": 0,
            "raw": text,
        }


def classify_event(parsed):
    """Classify a syslog message into an event type."""
    msg = parsed.get("message", "")
    for pattern, event_type in EVENT_PATTERNS:
        if pattern.search(msg):
            return event_type
    return "generic"


def format_syslog_line(parsed, event_type, source_ip, timestamp):
    """Format a syslog event as InfluxDB line protocol."""
    hostname = escape_tag(parsed.get("hostname", "unknown"))
    severity = escape_tag(parsed["severity"])
    msg = escape_field_str(parsed["message"][:500])  # Truncate long messages

    tags = (f"hostname={hostname},"
            f"severity={severity},"
            f"event_type={escape_tag(event_type)},"
            f"source_ip={escape_tag(source_ip)}")
    fields = (f'message="{msg}",'
              f'severity_num={parsed["severity_num"]}i')
    return f"syslog_event,{tags} {fields} {timestamp}"


def main():
    setup_logging(LOG_NAME)
    logging.info("Starting syslog receiver")

    profile = get_active_profile()
    if not profile["omada"]["enabled"]:
        logging.info("Active profile has omada.enabled=false — syslog parser is idle "
                     "(no Omada router to receive syslog from).")
        while True:
            time.sleep(3600)

    config = load_config()
    syslog_cfg = config.get("syslog", {})
    bind_address = syslog_cfg.get("bind_address", "0.0.0.0")
    bind_port = syslog_cfg.get("bind_port", 5514)

    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_address, bind_port))
    sock.settimeout(5.0)

    logging.info("Listening on %s:%d/udp", bind_address, bind_port)

    # Buffer for batching writes
    buffer = []
    last_flush = time.time()
    FLUSH_INTERVAL = 5    # seconds
    FLUSH_SIZE = 50       # max lines before flush

    while True:
        try:
            try:
                data, addr = sock.recvfrom(8192)
                source_ip = addr[0]

                parsed = parse_syslog_message(data)
                if parsed is None:
                    continue

                event_type = classify_event(parsed)
                timestamp = ts_now()
                line = format_syslog_line(parsed, event_type, source_ip, timestamp)
                buffer.append(line)

                # Log notable events
                if parsed["severity_num"] <= 3:
                    logging.warning("Router %s [%s]: %s",
                                    parsed["severity"], event_type,
                                    parsed["message"][:200])

            except socket.timeout:
                pass  # No message, just check if we need to flush

            # Flush buffer periodically or when full
            now = time.time()
            if buffer and (len(buffer) >= FLUSH_SIZE or now - last_flush >= FLUSH_INTERVAL):
                influx_write(buffer)
                logging.debug("Flushed %d syslog events", len(buffer))
                buffer.clear()
                last_flush = now

        except Exception as e:
            logging.error("Syslog receiver error: %s", e, exc_info=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
