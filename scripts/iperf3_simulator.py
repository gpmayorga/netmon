#!/usr/bin/env python3
"""
NetMon - iperf3 load simulator.
Simulates video call traffic using iperf3 UDP mode.
Designed to run as a oneshot systemd service triggered by a timer (nightly).
"""

import json
import logging
import random
import subprocess

from common import load_config, influx_write, setup_logging, escape_tag, ts_now, TestMarker

LOG_NAME = "iperf3_sim"


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

        # iperf3 returns a structured error (e.g. "the server is busy running a test")
        # when a public server's slot is locked. Caller treats this as retryable.
        if "error" in data:
            logging.warning("iperf3 rejected by %s:%d — %s", server, port, data["error"])
            return None
        return data

    except subprocess.TimeoutExpired:
        logging.error("iperf3 test timed out after %ds (server=%s:%d)", duration + 30, server, port)
        return None
    except json.JSONDecodeError as e:
        logging.error("Failed to parse iperf3 output from %s:%d: %s", server, port, e)
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


def classify_incident(results, jitter_threshold, loss_threshold):
    """Return None or a string describing why this run breached thresholds.

    Mirrors ping_monitor.classify_incident so the dashboard can render iperf3
    incidents alongside ping incidents using the same vocabulary.
    """
    jitter = results.get("jitter_ms", 0)
    loss = results.get("loss_pct", 0)
    has_jitter = jitter >= jitter_threshold
    has_loss = loss >= loss_threshold
    if has_jitter and has_loss:
        return "jitter+loss"
    if has_jitter:
        return "jitter_spike"
    if has_loss:
        return "loss"
    return None


def format_iperf3_incident_line(results, server, kind, jitter_threshold,
                                loss_threshold, incident_id, timestamp):
    """Format an iperf3_incident line — analogous to ping_incident."""
    tags = f"server={escape_tag(server)},type={escape_tag(kind)}"
    fields = (
        f"jitter_ms={results.get('jitter_ms', 0)},"
        f"loss_pct={results.get('loss_pct', 0)},"
        f"recv_mbps={results.get('recv_mbps', 0)},"
        f"lost_packets={results.get('lost_packets', 0)}i,"
        f"total_packets={results.get('total_packets', 0)}i,"
        f"jitter_threshold={jitter_threshold},"
        f"loss_threshold={loss_threshold},"
        f"incident_id=\"{incident_id}\""
    )
    return f"iperf3_incident,{tags} {fields} {timestamp}"


def main():
    setup_logging(LOG_NAME)
    logging.info("Starting iperf3 load simulation")

    config = load_config()
    iperf_cfg = config.get("iperf3", {})
    servers = iperf_cfg.get("servers", [])
    duration = iperf_cfg.get("duration", 60)
    bandwidth = iperf_cfg.get("bandwidth", "4M")
    parallel = iperf_cfg.get("parallel", 5)
    bucket = config.get("influxdb", {}).get("bucket_speedtest", "netmon_speedtest")
    thresholds = config.get("thresholds", {})
    jitter_threshold = thresholds.get("jitter_warn_ms", 30)
    loss_threshold = thresholds.get("loss_warn_pct", 1)

    if not servers:
        logging.error("iperf3.servers is empty; nothing to do")
        return

    # Shuffle so each run picks a random primary, but fall through the rest if
    # the chosen public server is slot-locked ("server is busy running a test").
    attempts = list(servers)
    random.shuffle(attempts)

    timestamp = ts_now()
    data = None
    server_used = None
    port_used = None
    with TestMarker("iperf3"):
        for s in attempts:
            host = s["host"]
            port = s["port"]
            logging.info("Trying iperf3 %s:%d (duration=%ds, %dx%s)",
                         host, port, duration, parallel, bandwidth)
            data = run_iperf3_test(host, port, duration, bandwidth, parallel)
            if data:
                server_used, port_used = host, port
                break

    if data:
        results = parse_iperf3_results(data)
        line = format_iperf3_line(results, server_used, duration, parallel, timestamp)
        lines = [line] if line else []

        kind = classify_incident(results, jitter_threshold, loss_threshold)
        if kind:
            incident_id = f"{timestamp}-{server_used}"
            lines.append(format_iperf3_incident_line(
                results, server_used, kind,
                jitter_threshold, loss_threshold,
                incident_id, timestamp,
            ))
            logging.warning(
                "INCIDENT (%s) iperf3 → %s:%d: jitter=%.2f ms (threshold %s), "
                "loss=%.3f%% (threshold %s)",
                kind, server_used, port_used,
                results.get("jitter_ms", 0), jitter_threshold,
                results.get("loss_pct", 0), loss_threshold,
            )

        if lines:
            success = influx_write(lines, bucket=bucket)
            if success:
                logging.info("iperf3 complete via %s:%d: send=%.1f Mbps, "
                             "recv=%.1f Mbps, jitter=%.2f ms, loss=%.2f%%%s",
                             server_used, port_used,
                             results.get("send_mbps", 0),
                             results.get("recv_mbps", 0),
                             results.get("jitter_ms", 0),
                             results.get("loss_pct", 0),
                             f" [INCIDENT: {kind}]" if kind else "")
            else:
                logging.error("Failed to write iperf3 results")
    else:
        tried = ",".join(f"{s['host']}:{s['port']}" for s in attempts)
        error_line = f'iperf3,server=none error=true {timestamp}'
        influx_write([error_line], bucket=bucket)
        logging.error("iperf3 test failed against all %d servers (%s)",
                      len(attempts), tried)


if __name__ == "__main__":
    main()
