#!/usr/bin/env python3
"""
NetMon - Common utilities for all monitoring scripts.
No external dependencies - stdlib only.
"""

import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
import yaml

CONFIG_PATH = os.environ.get("NETMON_CONFIG", "/opt/netmon/config/netmon.yml")

_config_cache = None
_config_mtime = 0


def load_config():
    """Load YAML config, with simple file-mtime caching."""
    global _config_cache, _config_mtime
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
        if _config_cache is not None and mtime == _config_mtime:
            return _config_cache
        with open(CONFIG_PATH, "r") as f:
            _config_cache = yaml.safe_load(f)
            _config_mtime = mtime
            return _config_cache
    except Exception as e:
        logging.error("Failed to load config from %s: %s", CONFIG_PATH, e)
        if _config_cache is not None:
            return _config_cache
        raise


def get_active_profile():
    """Return the dict for the currently-active network profile.

    Reads `active_profile` (a name) and `profiles.<name>` (the config dict)
    from netmon.yml. Always returns a dict; missing values default to a
    permissive shape so scripts can rely on:
        prof["gateway"]            -> str or None
        prof["eap_mesh"]           -> str or None
        prof["omada"]["enabled"]   -> bool
        prof["omada"]["eap_hosts"] -> list (possibly empty)

    Logs a warning and returns an empty/safe profile if config is missing
    or malformed — scripts should be tolerant of this (idle-skip rather
    than crash) to keep systemd from restart-looping during a bad edit.
    """
    config = load_config()
    name = config.get("active_profile")
    profiles = config.get("profiles", {})

    if not name:
        logging.warning("No active_profile set in netmon.yml; using empty profile")
        return {"gateway": None, "eap_mesh": None, "omada": {"enabled": False, "eap_hosts": []}}
    if name not in profiles:
        logging.warning("active_profile '%s' not found in profiles; using empty profile", name)
        return {"gateway": None, "eap_mesh": None, "omada": {"enabled": False, "eap_hosts": []}}

    prof = dict(profiles[name])  # shallow copy so callers don't mutate config cache
    omada = dict(prof.get("omada") or {})
    omada.setdefault("enabled", False)
    omada.setdefault("eap_hosts", [])
    prof["omada"] = omada
    monitor = dict(prof.get("monitor") or {})
    monitor.setdefault("enabled", False)
    monitor.setdefault("interface", None)
    monitor.setdefault("interval", None)   # None = fall back to wifi_scanner.interval
    prof["monitor"] = monitor
    prof.setdefault("gateway", None)
    prof.setdefault("eap_mesh", None)
    prof.setdefault("client_interface", None)
    return prof


def get_influx_params():
    """Read InfluxDB connection params from environment and config."""
    config = load_config()
    influx_cfg = config.get("influxdb", {})
    return {
        "url": os.environ.get("INFLUX_URL", influx_cfg.get("url", "http://127.0.0.1:8086")),
        "token": os.environ["INFLUX_TOKEN"],
        "org": os.environ.get("INFLUX_ORG", influx_cfg.get("org", "netmon")),
        "bucket": os.environ.get("INFLUX_BUCKET", influx_cfg.get("bucket", "netmon")),
    }


def influx_write(lines, bucket=None):
    """
    Write InfluxDB line protocol data via HTTP POST.
    lines: string or list of strings (each a line protocol line).
    Returns True on success, False on failure.
    """
    params = get_influx_params()
    if bucket is None:
        bucket = params["bucket"]
    if isinstance(lines, list):
        lines = "\n".join(l for l in lines if l)
    if not lines.strip():
        return True

    url = (f"{params['url']}/api/v2/write"
           f"?org={urllib.parse.quote(params['org'])}"
           f"&bucket={urllib.parse.quote(bucket)}"
           f"&precision=s")
    headers = {
        "Authorization": f"Token {params['token']}",
        "Content-Type": "text/plain; charset=utf-8",
    }
    data = lines.encode("utf-8")

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 204:
                    return True
                logging.warning("influx_write: unexpected status %d", resp.status)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200]
            logging.warning("influx_write attempt %d: HTTP %d: %s", attempt + 1, e.code, body)
        except (urllib.error.URLError, OSError) as e:
            logging.warning("influx_write attempt %d: %s", attempt + 1, e)
        if attempt < 2:
            time.sleep(2 ** attempt)

    logging.error("influx_write failed after 3 attempts")
    return False


def setup_logging(name, level=logging.INFO):
    """Configure structured logging to stderr (captured by journald)."""
    logging.basicConfig(
        level=level,
        format=f"%(asctime)s [%(levelname)s] {name}: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )


def escape_tag(s):
    """Escape special characters in InfluxDB tag values."""
    return str(s).replace(",", r"\,").replace("=", r"\=").replace(" ", r"\ ")


def escape_field_str(s):
    """Escape a string field value for InfluxDB line protocol."""
    return str(s).replace('"', r'\"')


def ts_now():
    """Return current Unix timestamp as integer (seconds)."""
    return int(time.time())


TEST_MARKER_PATH = "/run/netmon/test_running"


class TestMarker:
    """Context manager that tells ping_monitor a self-induced test is running.

    ping_monitor stats /run/netmon/test_running and tags any incidents observed
    while the marker is fresh as synthetic=true, so speedtest/iperf3-induced
    contention doesn't pollute the headline incident count.

    Best-effort: all errors swallowed. The marker is a hint, not a lock.
    """

    def __init__(self, kind):
        self.kind = kind

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(TEST_MARKER_PATH), exist_ok=True)
            with open(TEST_MARKER_PATH, "w") as f:
                f.write(f"{int(time.time())} {self.kind}\n")
        except OSError as e:
            logging.debug("TestMarker create failed: %s", e)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            os.remove(TEST_MARKER_PATH)
        except OSError:
            pass
        return False


# Import urllib.parse for URL encoding
import urllib.parse
