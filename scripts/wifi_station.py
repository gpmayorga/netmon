#!/usr/bin/env python3
"""
NetMon - WiFi client monitor + system metrics.
Monitors wlan0 connection quality (signal, bitrate, noise).
Also collects RPi system metrics (CPU, memory, disk, temperature).
"""

import logging
import os
import re
import subprocess
import time

from common import load_config, influx_write, setup_logging, escape_tag, escape_field_str, ts_now

LOG_NAME = "wifi_station"


def parse_iw_link(output):
    """Parse output of 'iw dev <iface> link'."""
    data = {}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("signal:"):
            m = re.search(r"(-?\d+)\s*dBm", line)
            if m:
                data["signal_dbm"] = int(m.group(1))
        elif line.startswith("freq:"):
            m = re.search(r"(\d+)", line)
            if m:
                data["frequency"] = int(m.group(1))
        elif line.startswith("tx bitrate:"):
            m = re.search(r"([\d.]+)\s*MBit/s", line)
            if m:
                data["tx_bitrate"] = float(m.group(1))
        elif line.startswith("rx bitrate:"):
            m = re.search(r"([\d.]+)\s*MBit/s", line)
            if m:
                data["rx_bitrate"] = float(m.group(1))
        elif line.startswith("SSID:"):
            data["ssid"] = line.split(":", 1)[1].strip()
        elif line.startswith("Connected to"):
            m = re.search(r"([0-9a-f:]{17})", line, re.I)
            if m:
                data["bssid"] = m.group(1)
    return data


def parse_station_dump(output):
    """Parse output of 'iw dev <iface> station dump' for extra metrics."""
    data = {}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("signal avg:"):
            m = re.search(r"(-?\d+)\s*dBm", line)
            if m:
                data["signal_avg_dbm"] = int(m.group(1))
        elif line.startswith("noise:"):
            m = re.search(r"(-?\d+)\s*dBm", line)
            if m:
                data["noise_dbm"] = int(m.group(1))
        elif line.startswith("expected throughput:"):
            m = re.search(r"([\d.]+)\s*Mbps", line)
            if m:
                data["expected_throughput"] = float(m.group(1))
        elif line.startswith("rx bytes:"):
            m = re.search(r"(\d+)", line)
            if m:
                data["rx_bytes"] = int(m.group(1))
        elif line.startswith("tx bytes:"):
            m = re.search(r"(\d+)", line)
            if m:
                data["tx_bytes"] = int(m.group(1))
        elif line.startswith("rx packets:"):
            m = re.search(r"(\d+)", line)
            if m:
                data["rx_packets"] = int(m.group(1))
        elif line.startswith("tx packets:"):
            m = re.search(r"(\d+)", line)
            if m:
                data["tx_packets"] = int(m.group(1))
        elif line.startswith("tx retries:"):
            m = re.search(r"(\d+)", line)
            if m:
                data["tx_retries"] = int(m.group(1))
        elif line.startswith("tx failed:"):
            m = re.search(r"(\d+)", line)
            if m:
                data["tx_failed"] = int(m.group(1))
        elif line.startswith("tx bitrate:"):
            # MCS index appears here on capable drivers; bitrate is already parsed by parse_iw_link.
            m = re.search(r"MCS\s+(\d+)", line)
            if m:
                data["tx_mcs"] = int(m.group(1))
        elif line.startswith("rx bitrate:"):
            m = re.search(r"MCS\s+(\d+)", line)
            if m:
                data["rx_mcs"] = int(m.group(1))
    return data


def parse_dev_info(output):
    """Parse output of 'iw dev <iface> info' for channel/width."""
    data = {}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("channel"):
            m = re.search(r"channel\s+(\d+)\s+\((\d+)\s*MHz\),\s*width:\s*(\d+)", line)
            if m:
                data["channel"] = int(m.group(1))
                data["freq_mhz"] = int(m.group(2))
                data["channel_width"] = int(m.group(3))
        elif line.startswith("txpower"):
            m = re.search(r"([\d.]+)\s*dBm", line)
            if m:
                data["txpower_dbm"] = float(m.group(1))
    return data


def parse_survey_dump(output):
    """Parse 'iw dev <iface> survey dump' to compute channel busy ratio (CCA proxy).
    Only the survey entry marked 'in use' is current channel utilization.
    Many drivers (notably Broadcom on RPi) return no data here; that's fine."""
    data = {}
    in_use_block = False
    block = {}
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("Survey data from"):
            if in_use_block and block:
                data.update(block)
                return data
            in_use_block = False
            block = {}
        elif line == "in use:":
            in_use_block = True
        elif in_use_block:
            if line.startswith("channel active time:"):
                m = re.search(r"(\d+)", line)
                if m:
                    block["survey_active_ms"] = int(m.group(1))
            elif line.startswith("channel busy time:"):
                m = re.search(r"(\d+)", line)
                if m:
                    block["survey_busy_ms"] = int(m.group(1))
            elif line.startswith("channel receive time:"):
                m = re.search(r"(\d+)", line)
                if m:
                    block["survey_rx_ms"] = int(m.group(1))
            elif line.startswith("channel transmit time:"):
                m = re.search(r"(\d+)", line)
                if m:
                    block["survey_tx_ms"] = int(m.group(1))
            elif line.startswith("noise:"):
                m = re.search(r"(-?\d+)\s*dBm", line)
                if m:
                    block["noise_dbm"] = int(m.group(1))
    if in_use_block and block:
        data.update(block)
    return data


def get_wifi_metrics(interface):
    """Collect WiFi metrics using iw commands."""
    metrics = {"connected": False}

    # iw dev <iface> link
    try:
        result = subprocess.run(
            ["iw", "dev", interface, "link"],
            capture_output=True, text=True, timeout=10
        )
        if "Not connected" in result.stdout:
            return metrics
        link_data = parse_iw_link(result.stdout)
        metrics.update(link_data)
        metrics["connected"] = True
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logging.warning("iw link failed: %s", e)
        return metrics

    # iw dev <iface> station dump
    try:
        result = subprocess.run(
            ["iw", "dev", interface, "station", "dump"],
            capture_output=True, text=True, timeout=10
        )
        station_data = parse_station_dump(result.stdout)
        metrics.update(station_data)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logging.warning("iw station dump failed: %s", e)

    # iw dev <iface> info -> channel/width/txpower
    try:
        result = subprocess.run(
            ["iw", "dev", interface, "info"],
            capture_output=True, text=True, timeout=5
        )
        metrics.update(parse_dev_info(result.stdout))
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # iw dev <iface> survey dump -> CCA (busy/active ratio). May be empty on RPi.
    try:
        result = subprocess.run(
            ["iw", "dev", interface, "survey", "dump"],
            capture_output=True, text=True, timeout=5
        )
        survey = parse_survey_dump(result.stdout)
        metrics.update(survey)
        if "survey_busy_ms" in survey and "survey_active_ms" in survey:
            active = survey["survey_active_ms"]
            if active > 0:
                metrics["channel_busy_pct"] = round(survey["survey_busy_ms"] * 100.0 / active, 2)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Compute SNR if both signal and noise are available
    if "signal_dbm" in metrics and "noise_dbm" in metrics:
        metrics["snr"] = metrics["signal_dbm"] - metrics["noise_dbm"]

    return metrics


def format_wifi_line(metrics, interface, timestamp, ap_labels=None):
    """Format WiFi metrics as InfluxDB line protocol."""
    if not metrics.get("connected"):
        return f'wifi_station,interface={escape_tag(interface)} connected=false {timestamp}'

    tags = f"interface={escape_tag(interface)}"
    if "ssid" in metrics:
        tags += f",ssid={escape_tag(metrics['ssid'])}"
    if "bssid" in metrics:
        tags += f",bssid={escape_tag(metrics['bssid'])}"
        location = (ap_labels or {}).get(metrics["bssid"].lower(), "unknown")
        tags += f",ap_location={escape_tag(location)}"

    fields = ["connected=true"]
    int_fields = [
        "signal_dbm", "signal_avg_dbm", "noise_dbm", "snr", "frequency",
        "channel", "freq_mhz", "channel_width",
        "tx_mcs", "rx_mcs",
        "survey_active_ms", "survey_busy_ms", "survey_rx_ms", "survey_tx_ms",
    ]
    float_fields = [
        "tx_bitrate", "rx_bitrate", "expected_throughput",
        "txpower_dbm", "channel_busy_pct",
    ]
    int64_fields = [
        "rx_bytes", "tx_bytes", "rx_packets", "tx_packets",
        "tx_retries", "tx_failed",
    ]
    for key in int_fields:
        if key in metrics:
            fields.append(f"{key}={metrics[key]}i")
    for key in float_fields:
        if key in metrics:
            fields.append(f"{key}={metrics[key]}")
    for key in int64_fields:
        if key in metrics:
            fields.append(f"{key}={metrics[key]}i")

    return f"wifi_station,{tags} {','.join(fields)} {timestamp}"


def get_system_metrics():
    """Collect RPi system metrics from /proc and /sys."""
    metrics = {}

    # CPU usage from /proc/stat (simple 1-second sample)
    try:
        with open("/proc/loadavg", "r") as f:
            parts = f.read().split()
            metrics["load_1m"] = float(parts[0])
            metrics["load_5m"] = float(parts[1])
            metrics["load_15m"] = float(parts[2])
    except (IOError, IndexError, ValueError):
        pass

    # Memory from /proc/meminfo
    try:
        meminfo = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]
                    meminfo[key] = int(val)  # kB

        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        if total > 0:
            metrics["mem_total_mb"] = round(total / 1024, 1)
            metrics["mem_available_mb"] = round(available / 1024, 1)
            metrics["mem_used_pct"] = round((1 - available / total) * 100, 1)
    except (IOError, KeyError, ValueError):
        pass

    # Disk usage
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used_pct = (1 - free / total) * 100 if total > 0 else 0
        metrics["disk_total_gb"] = round(total / (1024 ** 3), 1)
        metrics["disk_free_gb"] = round(free / (1024 ** 3), 1)
        metrics["disk_used_pct"] = round(used_pct, 1)
    except OSError:
        pass

    # Temperature
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp_mc = int(f.read().strip())
            metrics["temp_c"] = round(temp_mc / 1000, 1)
    except (IOError, ValueError):
        pass

    return metrics


def format_system_line(metrics, timestamp):
    """Format system metrics as InfluxDB line protocol."""
    fields = []
    for key, val in metrics.items():
        if isinstance(val, float):
            fields.append(f"{key}={val}")
        elif isinstance(val, int):
            fields.append(f"{key}={val}i")
    if not fields:
        return None
    return f"system,host=raspberrypi {','.join(fields)} {timestamp}"


def main():
    setup_logging(LOG_NAME)
    logging.info("Starting WiFi station monitor + system metrics")

    config = load_config()
    wifi_cfg = config.get("wifi_station", {})
    interval = wifi_cfg.get("interval", 30)
    interface = wifi_cfg.get("interface", "wlan0")

    logging.info("Interface: %s, interval: %ds", interface, interval)

    while True:
        try:
            timestamp = ts_now()
            lines = []

            # Re-read config each cycle so ap_labels edits apply without a restart.
            cfg = load_config()
            ap_labels = {k.lower(): v for k, v in (cfg.get("ap_labels") or {}).items()}

            # WiFi metrics
            wifi = get_wifi_metrics(interface)
            wifi_line = format_wifi_line(wifi, interface, timestamp, ap_labels)
            lines.append(wifi_line)

            if not wifi.get("connected"):
                logging.warning("WiFi not connected on %s", interface)

            # System metrics
            sys_metrics = get_system_metrics()
            sys_line = format_system_line(sys_metrics, timestamp)
            if sys_line:
                lines.append(sys_line)

            influx_write(lines)
            logging.debug("WiFi: signal=%s, sys: temp=%s, mem=%s%%",
                          wifi.get("signal_dbm", "N/A"),
                          sys_metrics.get("temp_c", "N/A"),
                          sys_metrics.get("mem_used_pct", "N/A"))

        except Exception as e:
            logging.error("WiFi station cycle error: %s", e, exc_info=True)

        time.sleep(interval)


if __name__ == "__main__":
    main()
