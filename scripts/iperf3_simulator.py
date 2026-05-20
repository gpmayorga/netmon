#!/usr/bin/env python3
"""
NetMon - iperf3 load simulator.
Simulates video call traffic using iperf3 UDP mode.
Designed to run as a oneshot systemd service triggered by a timer (nightly).
"""

import json
import logging
import subprocess
import time

from common import load_config, influx_write, setup_logging, escape_tag, ts_now

LOG_NAME = "iperf3_sim"


def start_iperf3_server(port=5201):
    """Start a local iperf3 server (daemon mode, one-off)."""
    try:
        subprocess.run(
            ["iperf3", "-s", "-D", "-1", "-p", str(port)],
            timeout=5,
            capture_output=True,
        )
        time.sleep(1)  # Let server start
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logging.warning("Failed to start iperf3 server: %s", e)
        return False


def run_iperf3_test(server, port, duration, bandwidth, parallel):
    """Run iperf3 client test, return parsed JSON."""
    cmd = [
        "iperf3",
        "-c", server,
        "-p", str(port),
        "-u",                    # UDP mode (simulates RTP/video)
        "-b", bandwidth,         # target bandwidth per stream
        "-t", str(duration),     # duration in seconds
        "-P", str(parallel),     # parallel streams
        "--json",                # JSON output
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=duration + 30,
        )
        data = json.loads(result.stdout)

        # Check for iperf3 error
        if "error" in data:
            logging.error("iperf3 error: %s", data["error"])
            return None
        return data

    except subprocess.TimeoutExpired:
        logging.error("iperf3 test timed out after %ds", duration + 30)
        return None
    except json.JSONDecodeError as e:
        logging.error("Failed to parse iperf3 output: %s", e)
        return None
    except FileNotFoundError:
        logging.error("iperf3 not found - install with: apt install iperf3")
        return None


def parse_iperf3_results(data):
    """Extract relevant metrics from iperf3 JSON output."""
    results = {}

    end = data.get("end", {})

    # UDP send summary
    sum_sent = end.get("sum_sent", end.get("sum", {}))
    if sum_sent:
        results["send_bps"] = sum_sent.get("bits_per_second", 0)
        results["send_mbps"] = round(results["send_bps"] / 1_000_000, 2)
        results["send_bytes"] = sum_sent.get("bytes", 0)
        results["send_packets"] = sum_sent.get("packets", 0)

    # UDP receive summary (has jitter and loss)
    sum_recv = end.get("sum_received", end.get("sum", {}))
    if sum_recv:
        results["recv_bps"] = sum_recv.get("bits_per_second", 0)
        results["recv_mbps"] = round(results["recv_bps"] / 1_000_000, 2)
        results["jitter_ms"] = round(sum_recv.get("jitter_ms", 0), 3)
        results["lost_packets"] = sum_recv.get("lost_packets", 0)
        results["total_packets"] = sum_recv.get("packets", 0)
        results["loss_pct"] = round(sum_recv.get("lost_percent", 0), 3)

    # CPU utilization
    cpu = end.get("cpu_utilization_percent", {})
    if cpu:
        results["cpu_host"] = round(cpu.get("host_total", 0), 1)
        results["cpu_remote"] = round(cpu.get("remote_total", 0), 1)

    return results


def format_iperf3_line(results, server, duration, parallel, timestamp):
    """Format iperf3 results as InfluxDB line protocol."""
    tags = f"server={escape_tag(server)},mode=udp,streams={parallel}i"
    fields = []

    for key in ["send_mbps", "recv_mbps", "jitter_ms", "loss_pct", "cpu_host"]:
        if key in results:
            fields.append(f"{key}={results[key]}")
    for key in ["lost_packets", "total_packets", "send_bytes"]:
        if key in results:
            fields.append(f"{key}={results[key]}i")
    fields.append(f"duration_sec={duration}i")

    if not fields:
        return None
    return f"iperf3,{tags} {','.join(fields)} {timestamp}"


def main():
    setup_logging(LOG_NAME)
    logging.info("Starting iperf3 load simulation")

    config = load_config()
    iperf_cfg = config.get("iperf3", {})
    server = iperf_cfg.get("server", "127.0.0.1")
    port = iperf_cfg.get("port", 5201)
    duration = iperf_cfg.get("duration", 60)
    bandwidth = iperf_cfg.get("bandwidth", "4M")
    parallel = iperf_cfg.get("parallel", 5)
    bucket = config.get("influxdb", {}).get("bucket_speedtest", "netmon_speedtest")

    # If testing against localhost, start a local server
    if server in ("127.0.0.1", "localhost", "::1"):
        logging.info("Starting local iperf3 server on port %d", port)
        if not start_iperf3_server(port):
            logging.error("Cannot start local iperf3 server")
            return

    logging.info("Running iperf3: server=%s, port=%d, duration=%ds, "
                 "bandwidth=%s, parallel=%d",
                 server, port, duration, bandwidth, parallel)

    timestamp = ts_now()
    data = run_iperf3_test(server, port, duration, bandwidth, parallel)

    if data:
        results = parse_iperf3_results(data)
        line = format_iperf3_line(results, server, duration, parallel, timestamp)

        if line:
            success = influx_write([line], bucket=bucket)
            if success:
                logging.info("iperf3 complete: send=%.1f Mbps, recv=%.1f Mbps, "
                             "jitter=%.2f ms, loss=%.2f%%",
                             results.get("send_mbps", 0),
                             results.get("recv_mbps", 0),
                             results.get("jitter_ms", 0),
                             results.get("loss_pct", 0))
            else:
                logging.error("Failed to write iperf3 results")
    else:
        error_line = f'iperf3,server={escape_tag(server)} error=true {timestamp}'
        influx_write([error_line], bucket=bucket)
        logging.error("iperf3 test failed")


if __name__ == "__main__":
    main()
