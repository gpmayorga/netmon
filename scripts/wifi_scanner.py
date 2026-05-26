#!/usr/bin/env python3
"""
NetMon - WiFi channel scanner using wlan1 in monitor mode.
Scans for nearby APs, measures channel congestion.
Runs as root (monitor mode requires it).
"""

import logging
import re
import subprocess
import time

from common import load_config, get_active_profile, influx_write, setup_logging, escape_tag, escape_field_str, ts_now

LOG_NAME = "wifi_scanner"


def setup_monitor_mode(interface):
    """Put interface into monitor mode. Returns True on success."""
    try:
        # Check current mode
        result = subprocess.run(
            ["iw", "dev", interface, "info"],
            capture_output=True, text=True, timeout=5
        )
        if "type monitor" in result.stdout:
            logging.info("%s already in monitor mode", interface)
            return True

        # Set monitor mode
        subprocess.run(["ip", "link", "set", interface, "down"],
                       check=True, timeout=5, capture_output=True)
        subprocess.run(["iw", "dev", interface, "set", "type", "monitor"],
                       check=True, timeout=5, capture_output=True)
        subprocess.run(["ip", "link", "set", interface, "up"],
                       check=True, timeout=5, capture_output=True)
        logging.info("Set %s to monitor mode", interface)
        return True
    except subprocess.CalledProcessError as e:
        logging.error("Failed to set monitor mode on %s: %s", interface, e)
        return False
    except subprocess.TimeoutExpired:
        logging.error("Timeout setting monitor mode on %s", interface)
        return False


def setup_managed_mode(interface):
    """Temporarily switch to managed mode for scanning, then back."""
    try:
        subprocess.run(["ip", "link", "set", interface, "down"],
                       check=True, timeout=5, capture_output=True)
        subprocess.run(["iw", "dev", interface, "set", "type", "managed"],
                       check=True, timeout=5, capture_output=True)
        subprocess.run(["ip", "link", "set", interface, "up"],
                       check=True, timeout=5, capture_output=True)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logging.error("Failed to set managed mode: %s", e)
        return False


def run_scan(interface):
    """Run iw scan and return raw output. Needs managed mode."""
    try:
        result = subprocess.run(
            ["iw", "dev", interface, "scan"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            # scan may fail if busy, retry once
            time.sleep(2)
            result = subprocess.run(
                ["iw", "dev", interface, "scan"],
                capture_output=True, text=True, timeout=30
            )
        return result.stdout
    except subprocess.TimeoutExpired:
        logging.warning("iw scan timed out")
        return ""
    except FileNotFoundError:
        logging.error("iw not found")
        return ""


def run_survey(interface):
    """Run iw survey dump for per-channel noise/utilization."""
    try:
        result = subprocess.run(
            ["iw", "dev", interface, "survey", "dump"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def parse_scan_output(output):
    """Parse iw scan output into a list of AP dicts."""
    aps = []
    current = None

    for line in output.splitlines():
        line_stripped = line.strip()

        # New BSS entry
        m = re.match(r"BSS\s+([0-9a-f:]{17})", line)
        if m:
            if current:
                aps.append(current)
            current = {"bssid": m.group(1)}
            continue

        if current is None:
            continue

        if line_stripped.startswith("freq:"):
            m = re.search(r"(\d+)", line_stripped)
            if m:
                current["frequency"] = int(m.group(1))
                current["channel"] = freq_to_channel(int(m.group(1)))
        elif line_stripped.startswith("signal:"):
            m = re.search(r"(-?[\d.]+)\s*dBm", line_stripped)
            if m:
                current["signal_dbm"] = float(m.group(1))
        elif line_stripped.startswith("SSID:"):
            ssid = line_stripped.split(":", 1)[1].strip()
            current["ssid"] = ssid if ssid else "(hidden)"
        elif "HT operation:" in line_stripped or "primary channel:" in line_stripped:
            m = re.search(r"primary channel:\s*(\d+)", line_stripped)
            if m:
                current["primary_channel"] = int(m.group(1))

    if current:
        aps.append(current)

    return aps


def parse_survey_output(output):
    """Parse iw survey dump into per-channel utilization."""
    channels = []
    current = None

    for line in output.splitlines():
        line_stripped = line.strip()

        if line_stripped.startswith("frequency:"):
            if current and "frequency" in current:
                channels.append(current)
            current = {}
            m = re.search(r"(\d+)", line_stripped)
            if m:
                freq = int(m.group(1))
                current["frequency"] = freq
                current["channel"] = freq_to_channel(freq)
        elif current is not None:
            if line_stripped.startswith("noise:"):
                m = re.search(r"(-?\d+)", line_stripped)
                if m:
                    current["noise_dbm"] = int(m.group(1))
            elif "channel active time:" in line_stripped:
                m = re.search(r"(\d+)", line_stripped)
                if m:
                    current["active_ms"] = int(m.group(1))
            elif "channel busy time:" in line_stripped:
                m = re.search(r"(\d+)", line_stripped)
                if m:
                    current["busy_ms"] = int(m.group(1))
            elif "channel receive time:" in line_stripped:
                m = re.search(r"(\d+)", line_stripped)
                if m:
                    current["rx_ms"] = int(m.group(1))
            elif "channel transmit time:" in line_stripped:
                m = re.search(r"(\d+)", line_stripped)
                if m:
                    current["tx_ms"] = int(m.group(1))

    if current and "frequency" in current:
        channels.append(current)

    # Compute busy percentage
    for ch in channels:
        if "active_ms" in ch and ch["active_ms"] > 0 and "busy_ms" in ch:
            ch["busy_pct"] = round((ch["busy_ms"] / ch["active_ms"]) * 100, 1)

    return channels


def freq_to_channel(freq):
    """Convert WiFi frequency (MHz) to channel number."""
    if 2412 <= freq <= 2484:
        if freq == 2484:
            return 14
        return (freq - 2407) // 5
    elif 5180 <= freq <= 5825:
        return (freq - 5000) // 5
    elif 5955 <= freq <= 7115:  # 6 GHz
        return (freq - 5950) // 5
    return 0


def format_ap_lines(aps, timestamp, ap_labels=None):
    """Format AP scan results as InfluxDB lines."""
    lines = []
    for ap in aps:
        ssid = ap.get("ssid", "(hidden)")
        bssid = ap.get("bssid", "unknown")
        channel = ap.get("channel", 0)
        freq = ap.get("frequency", 0)
        band = "2.4GHz" if freq < 5000 else "5GHz" if freq < 6000 else "6GHz"
        location = (ap_labels or {}).get(bssid.lower(), "unknown")

        tags = (f"ssid={escape_tag(ssid)},"
                f"bssid={escape_tag(bssid)},"
                f"ap_location={escape_tag(location)},"
                f"channel={channel},"
                f"band={band}")
        fields = []
        if "signal_dbm" in ap:
            fields.append(f"signal_dbm={ap['signal_dbm']}")
        fields.append(f"frequency={freq}i")

        if fields:
            lines.append(f"wifi_scan_ap,{tags} {','.join(fields)} {timestamp}")
    return lines


def format_channel_summary(aps, timestamp):
    """Create per-channel summary from AP scan."""
    channels = {}
    for ap in aps:
        ch = ap.get("channel", 0)
        if ch == 0:
            continue
        if ch not in channels:
            channels[ch] = {"count": 0, "strongest": -100, "frequency": ap.get("frequency", 0)}
        channels[ch]["count"] += 1
        sig = ap.get("signal_dbm", -100)
        if sig > channels[ch]["strongest"]:
            channels[ch]["strongest"] = sig

    lines = []
    for ch, data in channels.items():
        freq = data["frequency"]
        band = "2.4GHz" if freq < 5000 else "5GHz" if freq < 6000 else "6GHz"
        tags = f"channel={ch},band={band}"
        fields = f"ap_count={data['count']}i,strongest_dbm={data['strongest']}"
        lines.append(f"wifi_channel_summary,{tags} {fields} {timestamp}")
    return lines


def format_survey_lines(surveys, timestamp):
    """Format channel survey data as InfluxDB lines."""
    lines = []
    for ch in surveys:
        if "frequency" not in ch:
            continue
        freq = ch["frequency"]
        channel = ch.get("channel", 0)
        band = "2.4GHz" if freq < 5000 else "5GHz" if freq < 6000 else "6GHz"

        tags = f"channel={channel},band={band},frequency={freq}"
        fields = []
        for key in ["noise_dbm", "active_ms", "busy_ms", "rx_ms", "tx_ms"]:
            if key in ch:
                fields.append(f"{key}={ch[key]}i")
        if "busy_pct" in ch:
            fields.append(f"busy_pct={ch['busy_pct']}")

        if fields:
            lines.append(f"wifi_channel,{tags} {','.join(fields)} {timestamp}")
    return lines


def probe_survey_support(interface):
    """One-shot probe: does the driver populate nl80211 survey counters?

    Many drivers (Realtek rtw_*, Broadcom brcmfmac) accept the survey-dump
    command but emit nothing. ath9k_htc and a few others return per-channel
    'channel active/busy time' counters that drive the busy_pct field. If this
    returns False, the wifi_channel measurement (and its dashboard panel) will
    never populate on this hardware — no config or env var needed; swap the
    adapter and busy_pct shows up automatically. Tries managed mode first
    (some drivers only expose counters there), falls back to current mode.
    """
    try:
        setup_managed_mode(interface)
        output = run_survey(interface)
        return ("channel active time" in output) and ("channel busy time" in output)
    except Exception:
        return False


def main():
    setup_logging(LOG_NAME)
    logging.info("Starting WiFi scanner")

    # Profile gates whether airspace monitoring is even active. If disabled,
    # the daemon sleeps so systemd doesn't restart-loop. Re-enable by setting
    # `monitor.enabled: true` on the active profile in netmon.yml and running
    # `scripts/switch_profile.sh <profile>` (or just restart this service).
    profile = get_active_profile()
    if not profile["monitor"]["enabled"]:
        logging.info("Active profile has monitor.enabled=false — wifi scanner is idle. "
                     "Baseline is captured in docs/wifi-environment-baseline.md.")
        while True:
            time.sleep(3600)

    config = load_config()
    scan_cfg = config.get("wifi_scanner", {})
    # Profile.monitor takes precedence over the legacy wifi_scanner section for
    # both interface (which radio) and interval (how often). Falling back to
    # wifi_scanner.* keeps old configs working.
    interface = profile["monitor"]["interface"] or scan_cfg.get("interface", "wlan1")
    interval = profile["monitor"]["interval"] or scan_cfg.get("interval", 300)

    logging.info("Interface: %s, interval: %ds", interface, interval)

    if probe_survey_support(interface):
        logging.info("nl80211 survey supported on %s — wifi_channel.busy_pct will be written", interface)
    else:
        logging.info("nl80211 survey NOT supported on %s — wifi_channel.busy_pct will stay empty (driver limitation; swap to ath9k_htc adapter to enable)", interface)

    while True:
        try:
            timestamp = ts_now()
            lines = []

            # Re-read config each cycle so ap_labels edits apply without a restart.
            cfg = load_config()
            ap_labels = {k.lower(): v for k, v in (cfg.get("ap_labels") or {}).items()}

            # iw scan requires managed mode
            if not setup_managed_mode(interface):
                logging.error("Cannot set managed mode for scanning")
                time.sleep(interval)
                continue

            # Run AP scan
            scan_output = run_scan(interface)
            if scan_output:
                aps = parse_scan_output(scan_output)
                logging.info("Found %d APs", len(aps))
                lines.extend(format_ap_lines(aps, timestamp, ap_labels))
                lines.extend(format_channel_summary(aps, timestamp))

            # Run channel survey
            survey_output = run_survey(interface)
            if survey_output:
                surveys = parse_survey_output(survey_output)
                lines.extend(format_survey_lines(surveys, timestamp))

            # Switch back to monitor mode for passive listening
            setup_monitor_mode(interface)

            if lines:
                influx_write(lines)
                logging.debug("Wrote %d WiFi scan lines", len(lines))

        except Exception as e:
            logging.error("WiFi scan cycle error: %s", e, exc_info=True)
            # Try to restore monitor mode
            try:
                setup_monitor_mode(interface)
            except Exception:
                pass

        time.sleep(interval)


if __name__ == "__main__":
    main()
