# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project context

NetMon is a single-host network monitor for a coworking space, running on a Raspberry Pi (Debian/aarch64). It is **deployed in place** at `/opt/netmon/` — there is no separate checkout or build step. Edits to scripts/config take effect on the next service restart. The directory IS a git repo (init'd 2026-05-20); changes should be committed but the working tree IS production — there is no staging/deploy step, edits are live the moment systemd restarts the affected unit. No remote is configured yet. README and comments are in Spanish; code/identifiers are English.

## Architecture

Data flows in one direction: Python collectors → InfluxDB → Grafana.

- **Python collectors** (`scripts/*.py`) run as systemd units. Each script is a self-contained collector for one signal (ping, WiFi, speedtest, router SSH, syslog, SNMP, iperf3, public-IP check). They write InfluxDB line protocol over HTTP via `common.influx_write()`.
- **`scripts/common.py`** is the only shared module. It handles: YAML config loading with mtime caching, env-based InfluxDB params, line-protocol writes with retry/backoff, tag/field escaping, and stderr logging (captured by journald). Scripts depend only on stdlib + `pyyaml`; **do not add pip dependencies** — system packages are installed via `apt` in `install.sh`.
- **Daemon vs. timer services**: long-running collectors (ping, ipcheck, wifi-station, wifi-scanner, syslog-parser, router) are `Type=simple` daemons with their own internal `while True: ... sleep(interval)` loops driven by `netmon.yml`. Periodic jobs (speedtest, iperf3, snmp) are oneshot services triggered by systemd `.timer` units — **do not add a sleep loop to a timer-driven script**.
- **Docker stack** (`docker-compose.yml`): InfluxDB 2.7 on `127.0.0.1:8086` (loopback only), Grafana 11 on `0.0.0.0:3000` (reachable from anyone on the `eito` WiFi at `http://192.168.0.171:3000`). Grafana is configured with anonymous Viewer role so coworking staff can open dashboards without logging in; admin login still required to edit. Remote access for tooling is via Tailscale + SSH tunnel. Grafana dashboards and datasource are provisioned read-only from `config/grafana/`.
- **Two InfluxDB buckets**: `netmon` (30d retention) for high-frequency data, `netmon_speedtest` (90d) for speedtest/iperf3. Pass `bucket=` to `influx_write()` to target the speedtest bucket.

## Hardware inventory (building network)

The coworking building has two coexisting network stacks. NetMon can monitor either via the [profile system](#network-profiles).

**Omada stack** (the "eito" network — managed via Omada Cloud):
- **TP-Link Omada ER706W** — gateway/router with built-in WiFi 7 tri-band. 192.168.0.0/24 subnet. SSH on port 2222.
- **2× TP-Link EAP610** APs — wireless mesh backhaul to the gateway (single 5 GHz radio per AP shared between client + mesh). `planta-baja` at 192.168.0.100, `planta-1` at 192.168.0.101. **Powered by standalone wall-plugged PoE injectors** — they are NOT connected to the TL-SG2210MP switch below.

**Ubiquiti stack** (the "Eito_plus" network — UniFi, added by technician 2026-05-20):
- **Ubiquiti U6-Pro** — single WiFi 6 AP, 2×2 MIMO @ 2.4 GHz + 4×4 MIMO @ 5 GHz, broadcasting `Eito_plus` on 2.4 GHz ch 11 and 5 GHz ch 149.
- **Ubiquiti UCK G2 SSD Mini** — local UniFi controller server. Manages the U6-Pro.
- **TP-Link TL-SG2210MP** — 8-port PoE+ switch (150 W budget, 2× SFP), brought in by the technician with the Ubiquiti gear. Powers the U6-Pro (and possibly the UCK) over PoE. The fact that it's an Omada-managed product is incidental — it's being used as a plain PoE+ switch here; it isn't talking to the Omada controller.
- Wired uplink from the switch into the **Movistar/Telefónica HGU** (the ISP-provided fibre router, 192.168.1.0/24 subnet). Bypasses the Omada gateway entirely.

**The Pi** (NetMon host):
- `wlan0`: onboard Broadcom (brcmfmac) — managed-mode only (no monitor-mode support). Currently used as the `eito` client on the Omada side.
- `wlan1`: USB Realtek RTW8822BU — dual-band 2×2 MIMO 802.11ac, supports monitor mode AND managed. Currently used as the `Eito_plus` client (better RF reception than wlan0).

When updating BSSID → location mapping (`ap_labels` in `netmon.yml`), follow the existing convention: EAP610s expose BSSID base + base+1 for 2.4/5; the U6-Pro exposes several BSSIDs per band (primary + hidden mgmt + guest SSID variants), all of which are labeled `ubiquiti u6pro`.

## Network profiles

The building has multiple WiFi networks with very different topologies (Omada mesh on `eito` vs single wired Ubiquiti on `Eito_plus`). NetMon keeps **named profiles** in `config/netmon.yml` so switching the Pi between networks is one command:

```bash
scripts/switch_profile.sh --list                 # see what's defined
scripts/switch_profile.sh eito_plus              # switch active profile
sudo nmcli connection up Eito_plus               # switch Pi's WiFi (separate step)
```

The profile controls:
- `ping_monitor`'s gateway + `eap_mesh` ping targets (DNS targets stay the same — network-agnostic)
- Whether `router_monitor`, `eap_monitor`, `syslog_parser`, and ping-incident-triggered `wan_ping` back-probes do anything (gated on `profiles.<active>.omada.enabled`). When `false`, those daemons idle (`time.sleep(3600)`) so systemd doesn't restart-loop, and dashboards naturally show "no data" for Omada-specific panels.
- The list of Omada APs scraped by `eap_monitor` (`profiles.<active>.omada.eap_hosts`).
- Whether the airspace channel scanner (`wifi_scanner`) runs (`profiles.<active>.monitor.enabled`), which interface it uses (`monitor.interface`), and how often (`monitor.interval` — seconds between sweeps). Default state is `enabled: false` — the RF baseline is documented in `docs/wifi-environment-baseline.md` so wlan1 stays free for other use. Re-enable with `enabled: true` + restart `netmon-wifi-scanner` for fresh data when investigating a new incident or after a major environmental change.

Adding a new profile: add a block under `profiles:` in `netmon.yml` with at minimum `gateway:` and `omada.enabled:`. The switch script validates the name exists.

Network-agnostic collectors (`wifi_station`, `wifi_scanner`, `speedtest`, `iperf3`, `ipcheck`) run on any profile — they read Pi-local state or talk to public hosts, so they don't need a profile.

`scripts/switch_profile.sh` logs a `netmon_event` annotation before the change so dashboards mark the boundary. It does NOT switch the Pi's WiFi SSID — that's `nmcli` and is intentionally separate (you might want to test the profile change before disrupting the network connection).

## Config and secrets

- **`config/netmon.yml`** — single source of truth for intervals, targets, thresholds, interfaces. Scripts re-read it on each cycle (mtime-cached), so config changes apply without restart for most fields, but interval changes only take effect after the current sleep.
- **`config/secrets.env`** — generated by `install.sh` (mode 0600). Contains `INFLUX_TOKEN`, Grafana admin password, optional `ROUTER_SSH`/`ROUTER_SSH_PASSWORD`. Loaded by systemd via `EnvironmentFile=` and by `docker compose --env-file`. **Never commit or print this file.** When adding a new env var, update both `install.sh`'s heredoc and the relevant systemd unit.
- The Grafana container reads `INFLUX_TOKEN` from env to authenticate to InfluxDB; the datasource provisioning file references it as `${INFLUX_TOKEN}`.

## Common commands

```bash
# Install / re-run (idempotent)
sudo bash /opt/netmon/install.sh

# Service control
sudo systemctl restart 'netmon-*'                 # restart everything
sudo systemctl restart netmon-ping                # one daemon
sudo systemctl start netmon-speedtest.service     # force a timer-job to run now
systemctl list-timers 'netmon-*'                  # show next-fire times
journalctl -u netmon-ping -f                      # tail one service

# Docker
cd /opt/netmon && docker compose --env-file config/secrets.env up -d
cd /opt/netmon && docker compose logs -f grafana
cd /opt/netmon && docker compose ps

# Run a collector directly for debugging (uses env from secrets.env)
set -a; source /opt/netmon/config/secrets.env; set +a
python3 /opt/netmon/scripts/ping_monitor.py
```

There is no test suite, linter config, or build step. Verify changes by running the script directly with secrets sourced, or by `systemctl restart` + `journalctl -f`.

## Verifying changes

`verify.sh` is the standard sanity checker. It's modular — run only the sections relevant to what you changed. Always run the matching section(s) after an edit before declaring the task done.

```bash
bash /opt/netmon/verify.sh                            # all sections
bash /opt/netmon/verify.sh config scripts dashboards  # static-only (no service deps)
bash /opt/netmon/verify.sh services influx ap_labels  # live (needs services running)
```

| You edited                          | Run after editing                                                |
| ----------------------------------- | ---------------------------------------------------------------- |
| `config/netmon.yml`                 | `verify.sh config` (then restart any affected services)          |
| `scripts/*.py`                      | `verify.sh scripts services influx` (after `systemctl restart`)  |
| `config/grafana/dashboards/*.json`  | `verify.sh dashboards`                                           |
| `config/netmon.yml` `ap_labels:`    | `verify.sh ap_labels` (after restarting wifi-station/scanner)    |
| Adding/removing a systemd unit      | `verify.sh services` (after running `install.sh`)                |
| Anything significant                | `verify.sh` (all sections)                                       |

`install.sh` itself calls `verify.sh config scripts dashboards` pre-restart (fail-fast) and `verify.sh services influx ap_labels` post-restart (smoke test), so a clean `install.sh` run implies a clean verify.

Exit code 0 means all checks passed; `[WARN]` lines don't fail (low-frequency measurements may legitimately be stale). `[FAIL]` is a real problem — fix it before moving on.

## Conventions when editing collectors

- Use `common.influx_write()` — don't open a new InfluxDB client. It already retries 3× with exponential backoff and handles 204 vs. error responses.
- Always `escape_tag()` values used in InfluxDB tag positions; string field values need `escape_field_str()` and surrounding quotes.
- Timestamps in line protocol are seconds (the write uses `precision=s`). Use `ts_now()`.
- Catch exceptions inside the main loop so one bad cycle doesn't crash the service — systemd will restart it, but you lose continuity. Pattern: `try: ... except Exception as e: logging.error(..., exc_info=True)` then `time.sleep(interval)` regardless.
- Log to stderr (default via `setup_logging`); journald captures it.

## Incident log / past investigations

Past network and infrastructure investigations are written up in `docs/incidents/`, one file per incident named `YYYY-MM-DD-short-slug.md`. Each entry captures symptoms, the diagnostic technique that worked, root cause, what was tried (including options considered and rejected), the resulting changes, and follow-ups.

When troubleshooting weird network behaviour, **check `docs/incidents/` first** — many "huh, that's strange" patterns have prior diagnoses and a record of mitigations that worked. Specifically, anything involving widespread ping incidents across multiple targets, 5 GHz contention, mesh behaviour, sticky clients, or DNS-looks-broken-but-isn't almost certainly has precedent there.

Write a new entry when investigating a non-trivial issue, especially ones that required config or topology changes. Keep entries reusable: lead with the diagnostic methodology (the part future-you actually needs), not just the timeline.

`docs/` also holds longer-form reference material (e.g., `docs/omada-cli.md`) that is too long to fit in `CLAUDE.md` or a memory.

## When adding a new collector

1. Create `scripts/<name>.py` following the pattern in `ping_monitor.py` (load config, loop, write to InfluxDB).
2. Add a corresponding `systemd/netmon-<name>.service` (and `.timer` if periodic).
3. Add it to the `DAEMON_SERVICES` or `TIMER_SERVICES` array in `install.sh` so `sudo bash install.sh` enables it.
4. Add a section to `config/netmon.yml` for its intervals/targets.
5. If it writes a new measurement, add a Grafana panel in `config/grafana/dashboards/*.json`.
