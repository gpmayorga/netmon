#!/usr/bin/env python3
"""
NetMon - Speedtest server benchmark.

One-off helper to evaluate the ~N closest Ookla servers and pick which IDs to
pin in netmon.yml. Some Ookla servers are bandwidth-capped or oversubscribed
and produce misleadingly low numbers (e.g. server_id=1680 'Madrid' returning
~10 Mbps when the link can do 100 Mbps). Letting speedtest-cli auto-pick a
server hides this — the dashboard line drops without warning when the picker
lands on a bad server.

This script:
  1. Lists the N closest servers via `speedtest-cli --list`.
  2. Runs M speedtests against each.
  3. Prints a ranked table sorted by download.
  4. Optionally writes results to InfluxDB under `speedtest_benchmark`
     measurement (separate from `speedtest` so the dashboard stays clean).

Usage:
  python3 speedtest_benchmark.py                # 10 servers, 1 run each, write to influx
  python3 speedtest_benchmark.py --count 5      # only the 5 closest
  python3 speedtest_benchmark.py --runs 3       # 3 runs per server (variance check)
  python3 speedtest_benchmark.py --no-influx    # print only, don't write
  python3 speedtest_benchmark.py --list-only    # just dump the server list, no tests
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import time

from common import influx_write, setup_logging, escape_tag, ts_now

LOG_NAME = "speedtest_benchmark"
BUCKET = "netmon_speedtest"


def list_servers(count):
    """Return list of (id, sponsor, name, country, distance_km) for the N closest servers."""
    try:
        result = subprocess.run(
            ["speedtest-cli", "--secure", "--list"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        logging.error("speedtest-cli --list timed out")
        return []
    except FileNotFoundError:
        logging.error("speedtest-cli not found")
        return []

    if result.returncode != 0:
        logging.error("speedtest-cli --list failed: %s", result.stderr[:200])
        return []

    # Lines look like:
    #   6903) Aire Networks (Madrid, Spain) [ 42.31 km]
    pattern = re.compile(
        r"^\s*(\d+)\)\s+(.+?)\s+\((.+?),\s*(.+?)\)\s+\[\s*([\d.]+)\s*km\]"
    )
    servers = []
    for line in result.stdout.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        sid, sponsor, city, country, dist = m.groups()
        servers.append({
            "id": sid,
            "sponsor": sponsor.strip(),
            "city": city.strip(),
            "country": country.strip(),
            "distance_km": float(dist),
        })
        if len(servers) >= count:
            break
    return servers


def run_one(server_id, timeout=120):
    """Run one speedtest against a specific server. Returns dict or None."""
    try:
        result = subprocess.run(
            ["speedtest-cli", "--server", str(server_id), "--json", "--secure"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logging.warning("server %s: timed out", server_id)
        return None

    if result.returncode != 0:
        logging.warning("server %s: %s", server_id, result.stderr.strip()[:160])
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logging.warning("server %s: bad JSON: %s", server_id, e)
        return None

    return {
        "download_mbps": round(data["download"] / 1_000_000, 2),
        "upload_mbps": round(data["upload"] / 1_000_000, 2),
        "latency_ms": round(data["ping"], 2),
        "server_name": data.get("server", {}).get("name", "unknown"),
        "server_sponsor": data.get("server", {}).get("sponsor", "unknown"),
        "bytes_received": data.get("bytes_received", 0),
        "bytes_sent": data.get("bytes_sent", 0),
    }


def format_line(server_id, sponsor, city, country, distance_km, result, run_index, timestamp):
    """Format one benchmark result as InfluxDB line protocol."""
    tags = (
        f"server_id={escape_tag(str(server_id))},"
        f"server_name={escape_tag(result['server_name'])},"
        f"sponsor={escape_tag(sponsor)},"
        f"city={escape_tag(city)},"
        f"country={escape_tag(country)},"
        f"run_index={run_index}"
    )
    fields = (
        f"download_mbps={result['download_mbps']},"
        f"upload_mbps={result['upload_mbps']},"
        f"latency_ms={result['latency_ms']},"
        f"distance_km={distance_km},"
        f"bytes_received={result['bytes_received']}i,"
        f"bytes_sent={result['bytes_sent']}i"
    )
    return f"speedtest_benchmark,{tags} {fields} {timestamp}"


def print_table(rows):
    """Print a fixed-width ranked table sorted by download speed."""
    rows = sorted(rows, key=lambda r: -r["best_down"])
    header = f"{'rank':<4} {'id':<8} {'down':>8} {'up':>8} {'lat':>6}  {'dist':>6}  {'city':<18} {'sponsor'}"
    print()
    print(header)
    print("-" * len(header))
    for i, r in enumerate(rows, 1):
        print(
            f"{i:<4} {r['id']:<8} "
            f"{r['best_down']:>7.1f}M {r['best_up']:>7.1f}M {r['best_lat']:>5.1f}  "
            f"{r['distance_km']:>5.1f}  {r['city']:<18} {r['sponsor']}"
        )
    print()


def main():
    p = argparse.ArgumentParser(description="Benchmark Ookla speedtest servers.")
    p.add_argument("--count", type=int, default=10, help="how many closest servers to test")
    p.add_argument("--also", type=str, default="",
                   help="comma-separated extra server IDs to include (e.g. ones not in the closest-N list)")
    p.add_argument("--runs", type=int, default=1, help="runs per server")
    p.add_argument("--timeout", type=int, default=120, help="per-test timeout (s)")
    p.add_argument("--no-influx", action="store_true", help="print only, don't write to InfluxDB")
    p.add_argument("--list-only", action="store_true", help="dump server list and exit")
    args = p.parse_args()

    setup_logging(LOG_NAME)

    servers = list_servers(args.count)
    if not servers:
        logging.error("No servers found — speedtest-cli --list returned nothing parseable")
        return 1

    extra_ids = [s.strip() for s in args.also.split(",") if s.strip()]
    if extra_ids:
        known_ids = {s["id"] for s in servers}
        for extra in extra_ids:
            if extra in known_ids:
                continue
            servers.append({
                "id": extra,
                "sponsor": "extra",
                "city": "unknown",
                "country": "unknown",
                "distance_km": 0.0,
            })

    if args.list_only:
        for s in servers:
            print(f"  {s['id']:<8} {s['sponsor']:<35} {s['city']}, {s['country']}  ({s['distance_km']} km)")
        return 0

    logging.info(
        "Benchmarking %d servers, %d run(s) each (≈ %ds total)",
        len(servers), args.runs, len(servers) * args.runs * 35,
    )

    summary = []
    lines = []
    for s in servers:
        all_down, all_up, all_lat = [], [], []
        last_result = None
        for run in range(1, args.runs + 1):
            logging.info("Testing %s (%s, %s) run %d/%d ...",
                         s["id"], s["sponsor"], s["city"], run, args.runs)
            r = run_one(s["id"], timeout=args.timeout)
            if not r:
                continue
            last_result = r
            all_down.append(r["download_mbps"])
            all_up.append(r["upload_mbps"])
            all_lat.append(r["latency_ms"])
            ts = ts_now()
            lines.append(format_line(
                s["id"], s["sponsor"], s["city"], s["country"], s["distance_km"],
                r, run, ts,
            ))
            logging.info(
                "  -> %.1f Mbps down / %.1f Mbps up / %.1f ms",
                r["download_mbps"], r["upload_mbps"], r["latency_ms"],
            )
            # tiny pause between runs to be polite to the server
            if args.runs > 1 and run < args.runs:
                time.sleep(2)

        if all_down:
            summary.append({
                "id": s["id"],
                "sponsor": s["sponsor"],
                "city": s["city"],
                "country": s["country"],
                "distance_km": s["distance_km"],
                "best_down": max(all_down),
                "best_up": max(all_up),
                "best_lat": min(all_lat),
                "runs": len(all_down),
            })

    if not summary:
        logging.error("All speedtests failed")
        return 2

    print_table(summary)

    if not args.no_influx and lines:
        ok = influx_write(lines, bucket=BUCKET)
        if ok:
            logging.info("Wrote %d benchmark points to '%s'", len(lines), BUCKET)
        else:
            logging.error("InfluxDB write failed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
