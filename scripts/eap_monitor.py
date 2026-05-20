#!/usr/bin/env python3
"""
NetMon - Omada EAP610 monitor via SSH.

The EAP610 (MediaTek MT7915 chipset) exposes a BusyBox shell when SSH is
enabled site-wide in the Omada controller. There is no `iw` — only the
classic wireless-tools (`iwconfig`, `iwpriv`) and /proc/net/wireless.

We poll three sources per cycle:
  * `iwconfig apclix0`  -> 5 GHz mesh-backhaul link quality (most critical
                           for diagnosing video-call cuts in a mesh setup)
  * `iwpriv raN stat`   -> per-radio TX/RX counters, packet error rate,
                           current MCS, chipset temperature
  * `/proc/net/wireless` -> per-VAP retry/discard counters (8 SSIDs × 2 bands)

SSH must be enabled in the Omada controller
(Settings -> Site -> Services -> Device Account & SSH). Credentials default
to ROUTER_SSH_* (same Omada site account works on every device); override
with EAP_SSH_* in secrets.env if the EAP password differs.
"""

import logging
import os
import re
import time

from common import load_config, influx_write, setup_logging, escape_tag, ts_now
from ssh_helper import run_commands

LOG_NAME = "eap_monitor"

# Mesh-backhaul interfaces. apclix0 = 5 GHz mesh client, apcli0 = 2.4 GHz.
# The EAP connects "up" to another mesh node through these. Down to clients
# it serves via ra* (2.4G) and rax* (5G) VAPs.
MESH_BACKHAUL_IFACES = ["apclix0", "apcli0"]
# Primary radios (VAP index 0). Index 0 is the main SSID; higher indices are
# guest/secondary SSIDs sharing the same radio.
RADIO_PRIMARIES = {"ra0": "2g", "rax0": "5g"}


# ---- /proc/net/wireless ----
# Format: " face | status | qual link level noise | nwid crypt frag retry misc | beacon"
PROC_WIRELESS_RE = re.compile(
    r"^\s*(\S+):\s+"
    r"(\S+)\s+"             # status
    r"(\S+)\s+(\S+)\s+(\S+)\s+"  # link level noise
    r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+"  # discarded: nwid crypt frag retry misc
    r"(\d+)"                # missed beacon
)


def parse_proc_wireless(text):
    """Yield dict per interface from /proc/net/wireless."""
    rows = []
    for line in text.splitlines():
        m = PROC_WIRELESS_RE.match(line)
        if not m:
            continue
        iface, status, link, level, noise, nwid, crypt, frag, retry, misc, missed = m.groups()
        rows.append({
            "iface": iface,
            "link": _safe_float(link),
            "level_dbm": _safe_int(level),
            "noise_dbm": _safe_int(noise),
            "disc_retry": int(retry),
            "disc_misc": int(misc),
            "missed_beacon": int(missed),
        })
    return rows


def _safe_int(s):
    """Parse '-54' -> -54, '10.' -> None, '-256' -> -256."""
    s = (s or "").rstrip(".")
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _safe_float(s):
    s = (s or "").rstrip(".")
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


# ---- iwconfig <iface> ----
def parse_iwconfig(text):
    """Pick signal/noise/bitrate/AP/ESSID from a single iwconfig stanza."""
    out = {}
    m = re.search(r'ESSID:"([^"]*)"', text)
    if m:
        out["essid"] = m.group(1)
    m = re.search(r"Access Point:\s*([0-9A-Fa-f:]{17})", text)
    if m:
        out["bssid"] = m.group(1).lower()
    m = re.search(r"Bit Rate[=:]\s*([\d.]+)\s*(\w*b/s)", text)
    if m:
        rate = float(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("g"):
            rate *= 1000.0
        elif unit.startswith("k"):
            rate /= 1000.0
        out["bitrate_mbps"] = round(rate, 1)
    m = re.search(r"Signal level[:=]\s*(-?\d+)\s*dBm", text)
    if m:
        out["signal_dbm"] = int(m.group(1))
    m = re.search(r"Noise level[:=]\s*(-?\d+)\s*dBm", text)
    if m:
        out["noise_dbm"] = int(m.group(1))
    m = re.search(r"Channel[=:]?\s*(\d+)", text)
    if m:
        out["channel"] = int(m.group(1))
    if "signal_dbm" in out and "noise_dbm" in out:
        out["snr"] = out["signal_dbm"] - out["noise_dbm"]
    return out


# ---- iwpriv raN stat ----
def parse_iwpriv_stat(text):
    """Pull counters out of MediaTek `iwpriv raN stat` output."""
    out = {}
    m = re.search(r"CurrentTemperature\s*=\s*(\d+)", text)
    if m:
        out["temp_c"] = int(m.group(1))

    m = re.search(r"Tx success\s*=\s*(\d+)", text)
    if m:
        out["tx_success"] = int(m.group(1))
    m = re.search(r"Tx fail count\s*=\s*(\d+),\s*PER=([\d.]+)%", text)
    if m:
        out["tx_fail"] = int(m.group(1))
        out["tx_per_pct"] = float(m.group(2))

    m = re.search(r"Rx success\s*=\s*(\d+)", text)
    if m:
        out["rx_success"] = int(m.group(1))
    m = re.search(r"Rx with CRC\s*=\s*(\d+),\s*PER=([\d.]+)%", text)
    if m:
        out["rx_crc_err"] = int(m.group(1))
        out["rx_per_pct"] = float(m.group(2))
    m = re.search(r"Rx drop due to out of resource\s*=\s*(\d+)", text)
    if m:
        out["rx_drop_resource"] = int(m.group(1))

    m = re.search(r"Last TX Rate\s*=\s*MCS(\d+),\s*BW(\d+)", text, re.I)
    if m:
        out["last_tx_mcs"] = int(m.group(1))
        out["last_tx_bw"] = int(m.group(2))
    return out


# ---- line formatting ----
def format_mesh_line(host, iface, parsed, timestamp):
    tags = f"host={escape_tag(host)},iface={escape_tag(iface)}"
    # Skip empty BSSID/ESSID — happens when the mesh interface isn't connected.
    # InfluxDB rejects empty tag values; an absent tag is the right signal anyway.
    if parsed.get("bssid"):
        tags += f",bssid={escape_tag(parsed['bssid'])}"
    if parsed.get("essid"):
        tags += f",essid={escape_tag(parsed['essid'])}"
    fields = []
    for k in ("signal_dbm", "noise_dbm", "snr", "channel"):
        if k in parsed:
            fields.append(f"{k}={parsed[k]}i")
    if "bitrate_mbps" in parsed:
        fields.append(f"bitrate_mbps={parsed['bitrate_mbps']}")
    if not fields:
        return None
    return f"eap_mesh,{tags} {','.join(fields)} {timestamp}"


def format_radio_line(host, iface, band, parsed, timestamp):
    tags = f"host={escape_tag(host)},iface={escape_tag(iface)},band={escape_tag(band)}"
    fields = []
    int_keys = [
        "temp_c", "tx_success", "tx_fail", "rx_success", "rx_crc_err",
        "rx_drop_resource", "last_tx_mcs", "last_tx_bw",
    ]
    float_keys = ["tx_per_pct", "rx_per_pct"]
    for k in int_keys:
        if k in parsed:
            fields.append(f"{k}={parsed[k]}i")
    for k in float_keys:
        if k in parsed:
            fields.append(f"{k}={parsed[k]}")
    if not fields:
        return None
    return f"eap_radio,{tags} {','.join(fields)} {timestamp}"


def format_vap_line(host, row, timestamp):
    """Per-VAP line from /proc/net/wireless (retry/discard counters)."""
    tags = f"host={escape_tag(host)},iface={escape_tag(row['iface'])}"
    fields = [
        f"disc_retry={row['disc_retry']}i",
        f"disc_misc={row['disc_misc']}i",
        f"missed_beacon={row['missed_beacon']}i",
    ]
    if row.get("level_dbm") is not None and row["level_dbm"] > -200:
        fields.append(f"level_dbm={row['level_dbm']}i")
    if row.get("noise_dbm") is not None:
        fields.append(f"noise_dbm={row['noise_dbm']}i")
    if row.get("link") is not None:
        fields.append(f"link={row['link']}")
    return f"eap_vap,{tags} {','.join(fields)} {timestamp}"


def main():
    setup_logging(LOG_NAME)
    logging.info("Starting EAP monitor")

    ssh_user_host = (os.environ.get("EAP_SSH") or "").strip()
    password = (os.environ.get("EAP_SSH_PASSWORD")
                or os.environ.get("ROUTER_SSH_PASSWORD") or "").strip()

    config = load_config()
    eap_cfg = config.get("eap", {})
    host = eap_cfg.get("host", "192.168.0.100")
    interval = int(eap_cfg.get("interval", 60))
    ssh_port = eap_cfg.get("ssh_port")

    if not ssh_user_host:
        router_target = (os.environ.get("ROUTER_SSH") or "").strip()
        if "@" in router_target:
            user = router_target.split("@", 1)[0]
            ssh_user_host = f"{user}@{host}"
        else:
            logging.error("EAP_SSH not set and ROUTER_SSH missing user@host; cannot proceed")
            while True:
                time.sleep(3600)

    logging.info("EAP target: %s (auth: %s)", ssh_user_host,
                 "password" if password else "key-based")

    # Build the command batch once. We run everything in a single SSH session.
    commands = ["cat /proc/net/wireless"]
    commands += [f"iwconfig {iface}" for iface in MESH_BACKHAUL_IFACES]
    commands += [f"iwpriv {iface} stat" for iface in RADIO_PRIMARIES]

    while True:
        try:
            outputs = run_commands(ssh_user_host, password, commands, timeout_sec=25, port=ssh_port)
            if not outputs:
                logging.warning("SSH session to %s failed", host)
                time.sleep(interval)
                continue

            timestamp = ts_now()
            lines = []
            idx = 0

            # 1. /proc/net/wireless -> one eap_vap line per interface
            vap_rows = parse_proc_wireless(outputs[idx]); idx += 1
            for row in vap_rows:
                line = format_vap_line(host, row, timestamp)
                if line:
                    lines.append(line)

            # 2. Mesh backhaul interfaces
            mesh_summary = []
            for iface in MESH_BACKHAUL_IFACES:
                parsed = parse_iwconfig(outputs[idx]); idx += 1
                if not parsed:
                    continue
                line = format_mesh_line(host, iface, parsed, timestamp)
                if line:
                    lines.append(line)
                    mesh_summary.append(
                        f"{iface} signal={parsed.get('signal_dbm','?')}dBm "
                        f"snr={parsed.get('snr','?')} bssid={parsed.get('bssid','?')}"
                    )

            # 3. Per-radio iwpriv stat
            radio_summary = []
            for iface, band in RADIO_PRIMARIES.items():
                parsed = parse_iwpriv_stat(outputs[idx]); idx += 1
                if not parsed:
                    continue
                line = format_radio_line(host, iface, band, parsed, timestamp)
                if line:
                    lines.append(line)
                    radio_summary.append(
                        f"{iface}({band}) PER={parsed.get('tx_per_pct','?')}% "
                        f"temp={parsed.get('temp_c','?')}C MCS={parsed.get('last_tx_mcs','?')}"
                    )

            if lines:
                influx_write(lines)
            logging.info("EAP %s: %d points | mesh: %s | radios: %s",
                         host, len(lines),
                         "; ".join(mesh_summary) or "n/a",
                         "; ".join(radio_summary) or "n/a")
        except Exception as e:
            logging.error("EAP monitor cycle error: %s", e, exc_info=True)

        time.sleep(interval)


if __name__ == "__main__":
    main()
