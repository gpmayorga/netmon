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


# Import urllib.parse for URL encoding
import urllib.parse
