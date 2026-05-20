#!/usr/bin/env python3
"""
NetMon - Speedtest runner (schedule-aware).

Designed as a oneshot systemd service ticked every 5 min by
netmon-speedtest.timer. Each tick this script decides whether to actually
run a speedtest, based on:

  - Which schedule window the current local time falls into
    (work / evening / night, configured in netmon.yml).
  - Whether enough minutes have passed since the last successful run for
    that window's cadence.

Server selection: random pick from the pinned `speedtest.servers` list in
netmon.yml. The pinned list comes from scripts/speedtest_benchmark.py — see
that file for rationale. If the list is empty, fall back to auto-pick.

State (last_run epoch) is persisted at /opt/netmon/data/speedtest_state.json
so cadence survives reboots and timer-fire jitter.
"""

import datetime
import json
import logging
import os
import random
import subprocess
import sys
import time

from common import load_config, influx_write, setup_logging, escape_tag, ts_now

LOG_NAME = "speedtest"
STATE_FILE = "/opt/netmon/data/speedtest_state.json"


def parse_hhmm(s):
    """Parse 'HH:MM' to a (hour, minute) tuple."""
    h, m = s.split(":")
    return int(h), int(m)


def in_window(now, start, end):
    """Is `now` (a datetime.time) inside [start, end)? Window may cross midnight."""
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def pick_window(schedules, now_time):
    """Return (name, every_min) for the schedule containing now_time, or (None, None)."""
    for name, spec in schedules.items():
        win = spec.get("window", "")
        if "-" not in win:
            continue
        start_s, end_s = win.split("-", 1)
        start = datetime.time(*parse_hhmm(start_s.strip()))
        end = datetime.time(*parse_hhmm(end_s.strip()))
        if in_window(now_time, start, end):
            return name, int(spec.get("every_min", 60))
    return None, None


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except IOError as e:
        logging.warning("Failed to save speedtest state: %s", e)


def pick_server(servers):
    """Return a server id string from the pinned list, or '' for auto-pick."""
    if not servers:
        return ""
    return str(random.choice(servers))


def run_speedtest(server_id, timeout=120):
    """Run speedtest-cli (optionally pinned to server_id) and return parsed JSON."""
    cmd = ["speedtest-cli", "--json", "--secure"]
    if server_id:
        cmd.extend(["--server", server_id])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logging.error("speedtest-cli failed: %s", result.stderr.strip()[:200])
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logging.error("speedtest-cli timed out after %ds", timeout)
        return None
    except json.JSONDecodeError as e:
        logging.error("Failed to parse speedtest output: %s", e)
        return None
    except FileNotFoundError:
        logging.error("speedtest-cli not found - install with: apt install speedtest-cli")
        return None


def format_speedtest_line(data, window_name, timestamp):
    """Format speedtest results as InfluxDB line protocol."""
    download_mbps = round(data["download"] / 1_000_000, 2)
    upload_mbps = round(data["upload"] / 1_000_000, 2)
    latency_ms = round(data["ping"], 2)

    server = data.get("server", {})
    server_name = server.get("name", "unknown")
    server_id = str(server.get("id", "0"))

    tags = (
        f"server_name={escape_tag(server_name)},"
        f"server_id={escape_tag(server_id)},"
        f"window={escape_tag(window_name)}"
    )
    fields = (
        f"download_mbps={download_mbps},"
        f"upload_mbps={upload_mbps},"
        f"latency_ms={latency_ms},"
        f"bytes_received={data.get('bytes_received', 0)}i,"
        f"bytes_sent={data.get('bytes_sent', 0)}i"
    )
    return f"speedtest,{tags} {fields} {timestamp}"


def main():
    setup_logging(LOG_NAME)

    config = load_config()
    st_cfg = config.get("speedtest", {})
    timeout = st_cfg.get("timeout", 120)
    schedules = st_cfg.get("schedules", {})
    servers = st_cfg.get("servers", []) or []
    bucket = config.get("influxdb", {}).get("bucket_speedtest", "netmon_speedtest")

    if not schedules:
        logging.error("No speedtest.schedules configured")
        return 1

    now = datetime.datetime.now()
    window_name, every_min = pick_window(schedules, now.time())
    if window_name is None:
        logging.info("Current time %s falls outside any schedule window; skipping",
                     now.strftime("%H:%M"))
        return 0

    state = load_state()
    last_run = state.get("last_run", 0)
    elapsed = time.time() - last_run

    if elapsed < every_min * 60:
        remaining = int((every_min * 60 - elapsed) / 60)
        logging.info(
            "Window '%s' (every %dmin) — %dmin since last run, %dmin to go; skipping",
            window_name, every_min, int(elapsed / 60), remaining,
        )
        return 0

    server_id = pick_server(servers)
    logging.info(
        "Window '%s' — running speedtest against server %s",
        window_name, server_id if server_id else "(auto-pick)",
    )

    timestamp = ts_now()
    data = run_speedtest(server_id, timeout=timeout)

    if data:
        line = format_speedtest_line(data, window_name, timestamp)
        success = influx_write([line], bucket=bucket)
        if success:
            logging.info(
                "Speedtest complete (server %s): %.1f Mbps down, %.1f Mbps up, %.1f ms",
                data.get("server", {}).get("id", "?"),
                data["download"] / 1_000_000,
                data["upload"] / 1_000_000,
                data["ping"],
            )
            state["last_run"] = time.time()
            state["last_window"] = window_name
            save_state(state)
        else:
            logging.error("Failed to write speedtest results to InfluxDB")
            return 2
    else:
        # Write error marker so the dashboard shows the attempt and the gap is visible
        error_line = (
            f'speedtest,window={escape_tag(window_name)} '
            f'error=true,message="Speedtest failed" {timestamp}'
        )
        influx_write([error_line], bucket=bucket)
        logging.error("Speedtest failed")
        # Update last_run anyway so we don't hammer a broken endpoint at every tick
        state["last_run"] = time.time()
        state["last_window"] = window_name
        save_state(state)
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
