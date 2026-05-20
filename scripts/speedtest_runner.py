#!/usr/bin/env python3
"""
NetMon - Speedtest runner.
Runs speedtest-cli periodically, records download/upload/latency.
Designed to run as a oneshot systemd service triggered by a timer.
"""

import json
import logging
import subprocess
import time

from common import load_config, influx_write, setup_logging, escape_tag, escape_field_str, ts_now

LOG_NAME = "speedtest"


def run_speedtest(timeout=120):
    """Run speedtest-cli and return parsed JSON results."""
    try:
        result = subprocess.run(
            ["speedtest-cli", "--json", "--secure"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logging.error("speedtest-cli failed: %s", result.stderr[:200])
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


def format_speedtest_line(data, timestamp):
    """Format speedtest results as InfluxDB line protocol."""
    download_mbps = round(data["download"] / 1_000_000, 2)
    upload_mbps = round(data["upload"] / 1_000_000, 2)
    latency_ms = round(data["ping"], 2)

    server = data.get("server", {})
    server_name = server.get("name", "unknown")
    server_id = str(server.get("id", "0"))

    tags = f"server_name={escape_tag(server_name)},server_id={escape_tag(server_id)}"
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
    logging.info("Running speedtest")

    config = load_config()
    st_cfg = config.get("speedtest", {})
    timeout = st_cfg.get("timeout", 120)
    bucket = config.get("influxdb", {}).get("bucket_speedtest", "netmon_speedtest")

    timestamp = ts_now()
    data = run_speedtest(timeout)

    if data:
        line = format_speedtest_line(data, timestamp)
        success = influx_write([line], bucket=bucket)
        if success:
            download = round(data["download"] / 1_000_000, 1)
            upload = round(data["upload"] / 1_000_000, 1)
            logging.info("Speedtest complete: %.1f Mbps down, %.1f Mbps up, %.1f ms",
                         download, upload, data["ping"])
        else:
            logging.error("Failed to write speedtest results to InfluxDB")
    else:
        # Write error marker so dashboard shows the gap
        error_line = f'speedtest error=true,message="Speedtest failed" {timestamp}'
        influx_write([error_line], bucket=bucket)
        logging.error("Speedtest failed")


if __name__ == "__main__":
    main()
