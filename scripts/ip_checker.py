#!/usr/bin/env python3
"""
NetMon - Public IP change detector.
Checks public IP every minute, detects WAN failover events.
"""

import logging
import time
import urllib.request
import urllib.error

from common import load_config, influx_write, setup_logging, escape_field_str, ts_now

LOG_NAME = "ip_checker"


def get_public_ip(services):
    """Try each service until one returns a valid IP."""
    for url in services:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "netmon/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ip = resp.read().decode("utf-8").strip()
                # Basic validation: should look like an IP
                parts = ip.split(".")
                if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                    return ip
                logging.warning("Invalid IP response from %s: %s", url, ip[:50])
        except (urllib.error.URLError, OSError, ValueError) as e:
            logging.warning("IP check via %s failed: %s", url, e)
    return None


def main():
    setup_logging(LOG_NAME)
    logging.info("Starting IP checker")

    config = load_config()
    ip_cfg = config.get("ip_check", {})
    interval = ip_cfg.get("interval", 60)
    services = ip_cfg.get("services", ["https://ifconfig.me/ip", "https://api.ipify.org"])

    last_ip = None
    consecutive_failures = 0

    logging.info("Check interval: %ds, services: %s", interval, services)

    while True:
        try:
            current_ip = get_public_ip(services)

            if current_ip is None:
                consecutive_failures += 1
                logging.warning("Failed to get public IP (attempt %d)", consecutive_failures)
                if consecutive_failures >= 3:
                    lines = [f'public_ip error=true,message="Failed to get public IP" {ts_now()}']
                    influx_write(lines)
                time.sleep(interval)
                continue

            consecutive_failures = 0
            timestamp = ts_now()
            changed = last_ip is not None and current_ip != last_ip

            lines = [
                f'public_ip ip="{escape_field_str(current_ip)}",'
                f'changed={str(changed).lower()} {timestamp}'
            ]

            if changed:
                logging.warning("WAN IP changed: %s -> %s", last_ip, current_ip)
                lines.append(
                    f'wan_event,type=ip_change '
                    f'previous_ip="{escape_field_str(last_ip)}",'
                    f'new_ip="{escape_field_str(current_ip)}",'
                    f'message="WAN IP changed: {last_ip} -> {current_ip}" '
                    f'{timestamp}'
                )
            elif last_ip is None:
                logging.info("Initial public IP: %s", current_ip)

            influx_write(lines)
            last_ip = current_ip

        except Exception as e:
            logging.error("IP check cycle error: %s", e, exc_info=True)

        time.sleep(interval)


if __name__ == "__main__":
    main()
