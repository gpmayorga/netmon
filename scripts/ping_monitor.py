#!/usr/bin/env python3
"""
NetMon - Continuous ping monitor using fping.

Pings multiple targets at 1Hz, computes per-window stats (avg/max/p95/jitter/loss),
writes them to InfluxDB, and emits a ping_incident measurement when a single RTT
exceeds `rtt_threshold_ms` or a packet is lost. Incidents also fire an
asynchronous per-WAN ping probe (wan_ping.py) so we can attribute the spike
to a specific fiber.
"""

import logging
import os
import statistics
import subprocess
import time

from common import load_config, get_active_profile, influx_write, setup_logging, escape_tag, ts_now

LOG_NAME = "ping_monitor"
WAN_PING_SCRIPT = "/opt/netmon/scripts/wan_ping.py"
EVENT_MARKER = "/run/netmon/last_event_ts"
TEST_MARKER = "/run/netmon/test_running"


def compute_stats(rtts, total):
    """Return dict of stats over a list of RTTs (ms). total = samples sent (incl. lost)."""
    lost = total - len(rtts)
    loss_pct = (lost / total) * 100 if total > 0 else 100.0

    if not rtts:
        return {
            "avg": 0.0, "min": 0.0, "max": 0.0, "p95": 0.0, "p99": 0.0,
            "jitter": 0.0, "loss_pct": round(loss_pct, 1),
            "samples": total, "lost": lost,
        }

    avg_rtt = statistics.mean(rtts)
    # Jitter = stddev of RTTs (sample stddev). Falls back to 0 with <2 samples.
    jitter = statistics.stdev(rtts) if len(rtts) > 1 else 0.0

    p95 = p99 = avg_rtt
    if len(rtts) >= 5:
        sorted_rtts = sorted(rtts)
        p95 = sorted_rtts[min(int(len(sorted_rtts) * 0.95), len(sorted_rtts) - 1)]
        p99 = sorted_rtts[min(int(len(sorted_rtts) * 0.99), len(sorted_rtts) - 1)]

    return {
        "avg": round(avg_rtt, 2),
        "min": round(min(rtts), 2),
        "max": round(max(rtts), 2),
        "p95": round(p95, 2),
        "p99": round(p99, 2),
        "jitter": round(jitter, 2),
        "loss_pct": round(loss_pct, 1),
        "samples": total,
        "lost": lost,
    }


def parse_fping_output(stderr_text, target_names):
    """Parse fping -C output from stderr. Each line: 'target : rtt1 rtt2 ...' ('-' = lost)."""
    results = []
    for line in stderr_text.strip().split("\n"):
        if ":" not in line:
            continue
        target_part, _, times_part = line.partition(":")
        target = target_part.strip()
        samples = times_part.strip().split()
        if not samples:
            continue

        rtts = []
        for s in samples:
            if s != "-":
                try:
                    rtts.append(float(s))
                except ValueError:
                    pass

        stats = compute_stats(rtts, len(samples))
        stats["target"] = target
        stats["target_name"] = target_names.get(target, target)
        stats["rtts"] = rtts  # keep raw for incident detection
        results.append(stats)

    return results


def format_ping_line(r, timestamp):
    """Format a parsed result as a ping line-protocol point."""
    tags = f"target={escape_tag(r['target'])},target_name={escape_tag(r['target_name'])}"
    fields = (
        f"rtt_avg={r['avg']},"
        f"rtt_min={r['min']},"
        f"rtt_max={r['max']},"
        f"rtt_p95={r['p95']},"
        f"rtt_p99={r['p99']},"
        f"jitter={r['jitter']},"
        f"loss_pct={r['loss_pct']},"
        f"samples={r['samples']}i,"
        f"lost={r['lost']}i"
    )
    return f"ping,{tags} {fields} {timestamp}"


def classify_incident(r, threshold_ms, loss_min):
    """Return None or a string label describing why this window is an incident.

    A 'loss' incident requires >= loss_min lost packets in the batch (default 2);
    a single dropped packet is treated as WiFi background noise, not an incident.
    """
    has_spike = r["max"] >= threshold_ms
    has_loss = r["lost"] >= loss_min
    if has_spike and has_loss:
        return "spike+loss"
    if has_spike:
        return "rtt_spike"
    if has_loss:
        return "loss"
    return None


def read_marker_ts(path):
    """Return the epoch seconds written in `path`, or None if missing/invalid.

    Marker files are written by log_event.sh and the test runners. Format is a
    single line: "<unix_ts>" or "<unix_ts> <metadata>". We only need the ts.
    """
    try:
        with open(path, "r") as f:
            return int(f.readline().split()[0])
    except (IOError, ValueError, IndexError):
        return None


def synthetic_reason(now, event_settle_s, test_max_age_s):
    """Return a short string explaining why this cycle is 'synthetic', or None.

    Reasons (precedence order):
      - 'test'           — a speedtest/iperf3 marker is fresh
      - 'config_settle'  — a config/hw event happened within the settle window
    """
    test_ts = read_marker_ts(TEST_MARKER)
    if test_ts is not None and (now - test_ts) <= test_max_age_s:
        return "test"
    event_ts = read_marker_ts(EVENT_MARKER)
    if event_ts is not None and (now - event_ts) <= event_settle_s:
        return "config_settle"
    return None


def format_incident_line(r, threshold_ms, kind, incident_id, timestamp, synthetic=None):
    """Format a ping_incident line-protocol point.

    When `synthetic` is truthy, an extra tag synthetic="true" is added so the
    dashboard can filter out self-induced or post-config-change spikes.
    """
    tags = (
        f"target={escape_tag(r['target'])},"
        f"target_name={escape_tag(r['target_name'])},"
        f"type={escape_tag(kind)}"
    )
    if synthetic:
        tags += f",synthetic=true,synthetic_cause={escape_tag(synthetic)}"
    fields = (
        f"rtt_max={r['max']},"
        f"rtt_avg={r['avg']},"
        f"rtt_p95={r['p95']},"
        f"jitter={r['jitter']},"
        f"loss_pct={r['loss_pct']},"
        f"samples={r['samples']}i,"
        f"lost={r['lost']}i,"
        f"threshold_ms={threshold_ms}i,"
        f"incident_id=\"{incident_id}\""
    )
    return f"ping_incident,{tags} {fields} {timestamp}"


def trigger_wan_ping(incident_id):
    """Fire-and-forget invocation of wan_ping.py. The script self-rate-limits."""
    if not os.path.exists(WAN_PING_SCRIPT):
        return
    try:
        subprocess.Popen(
            ["python3", WAN_PING_SCRIPT, "--reason", incident_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:
        logging.warning("Failed to spawn wan_ping: %s", e)


def run_fping(targets, count, period_ms, inter_target_ms):
    """Run fping against targets, return stderr output (parseable rtt lines)."""
    cmd = [
        "fping",
        "-C", str(count),
        "-q",
        "-B", "1",
        "-r", "0",
        "-i", str(inter_target_ms),
        "-p", str(period_ms),
    ] + list(targets)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=(count * period_ms / 1000) + 30,
        )
        return result.stderr
    except subprocess.TimeoutExpired:
        logging.error("fping timed out")
        return ""
    except FileNotFoundError:
        logging.error("fping not found - install with: apt install fping")
        raise


def main():
    setup_logging(LOG_NAME)
    logging.info("Starting ping monitor")

    while True:
        try:
            config = load_config()
            ping_cfg = config.get("ping", {})
            interval = ping_cfg.get("interval", 0)
            count = ping_cfg.get("count", 5)
            period_ms = ping_cfg.get("period_ms", 1000)
            inter_target_ms = ping_cfg.get("inter_target_ms", 50)
            threshold_ms = ping_cfg.get("rtt_threshold_ms", 200)
            loss_min = ping_cfg.get("loss_min_packets", 2)
            event_settle_s = ping_cfg.get("event_settle_seconds", 90)
            test_max_age_s = ping_cfg.get("test_marker_max_age_seconds", 180)
            # Merge profile-specific targets (gateway, eap_mesh) with the
            # network-agnostic ones (DNS) from `ping.targets`. Null entries on
            # the profile are skipped — e.g. eito_plus has no eap_mesh.
            targets_dict = dict(ping_cfg.get("targets", {}))
            profile = get_active_profile()
            if profile.get("gateway"):
                targets_dict["gateway"] = profile["gateway"]
            if profile.get("eap_mesh"):
                targets_dict["eap_mesh"] = profile["eap_mesh"]

            if not targets_dict:
                logging.error("No ping targets configured")
                time.sleep(30)
                continue

            target_names = {v: k for k, v in targets_dict.items()}
            target_list = list(targets_dict.values())

            stderr = run_fping(target_list, count, period_ms, inter_target_ms)
            if not stderr:
                time.sleep(max(interval, 1))
                continue

            timestamp = ts_now()
            results = parse_fping_output(stderr, target_names)

            lines = [format_ping_line(r, timestamp) for r in results]

            # Synthetic detection: are we inside a self-induced test or a
            # post-config-change settle window? If so, incidents still get
            # written (for forensic analysis) but with synthetic=true so the
            # dashboard can drop them from headline counts.
            synthetic = synthetic_reason(timestamp, event_settle_s, test_max_age_s)

            # Incident detection — per-target, but trigger per-WAN ping only once per cycle.
            incident_triggered = False
            incident_id = None
            for r in results:
                kind = classify_incident(r, threshold_ms, loss_min)
                if not kind:
                    continue
                if incident_id is None:
                    incident_id = f"{timestamp}-{r['target_name']}"
                lines.append(format_incident_line(
                    r, threshold_ms, kind, incident_id, timestamp, synthetic=synthetic,
                ))
                logging.warning(
                    "INCIDENT (%s%s) %s (%s): max=%sms avg=%sms p95=%sms loss=%s%% jitter=%sms",
                    kind, f" synthetic={synthetic}" if synthetic else "",
                    r["target_name"], r["target"],
                    r["max"], r["avg"], r["p95"], r["loss_pct"], r["jitter"],
                )
                # Per-WAN ping only for genuine internet-side spikes — skip when
                # synthetic, otherwise iperf3/speedtest would trigger WAN pings
                # against themselves. Also skipped on non-Omada profiles since
                # the router-side back-probe SSHes the Omada gateway.
                if (not incident_triggered
                        and r["target_name"] != "gateway"
                        and not synthetic
                        and profile["omada"]["enabled"]):
                    trigger_wan_ping(incident_id)
                    incident_triggered = True

            if lines:
                influx_write(lines)
                logging.debug("Wrote %d points (%d targets, incident=%s)",
                              len(lines), len(results), bool(incident_id))

        except Exception as e:
            logging.error("Ping cycle error: %s", e, exc_info=True)

        if interval > 0:
            time.sleep(interval)


if __name__ == "__main__":
    main()
