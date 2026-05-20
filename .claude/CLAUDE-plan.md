Plan: Sistema de Monitorización de Red para Coworking

     Context

     Coworking en Madrid con microcortes de 2-3s en videollamadas. Dual WAN (Movistar+Orange) con failover LTE, 2 APs Omada en mesh. Necesitamos datos objetivos para
     diagnosticar si el problema es jitter/loss en una WAN, saltos entre WANs, interferencias WiFi, o saturación.

     Entorno RPi: Debian 13 trixie, aarch64, 7.6GB RAM, 100GB disco libre, Docker instalado, Python 3.13, wlan0 (conectado), wlan1 (TP-Link Archer T4U con monitor
     mode), Tailscale activo.

     Architecture

     ┌─────────────────────────────────────────────────────┐
     │                  Raspberry Pi                        │
     │                                                      │
     │  ┌──────────────┐  ┌──────────────┐                 │
     │  │ ping_monitor │  │ ip_checker   │  ← systemd      │
     │  │ (daemon)     │  │ (daemon)     │    services      │
     │  └──────┬───────┘  └──────┬───────┘                 │
     │         │                  │                         │
     │  ┌──────┴───────┐  ┌──────┴───────┐                 │
     │  │wifi_station  │  │wifi_scanner  │                  │
     │  │(daemon,wlan0)│  │(daemon,wlan1)│                  │
     │  └──────┬───────┘  └──────┬───────┘                 │
     │         │                  │                         │
     │  ┌──────┴───────┐  ┌──────┴───────┐                 │
     │  │speedtest     │  │iperf3_sim    │  ← systemd      │
     │  │(timer,1h)    │  │(timer,noche) │    timers        │
     │  └──────┬───────┘  └──────┬───────┘                 │
     │         │                  │                         │
     │  ┌──────┴───────┐  ┌──────┴───────┐                 │
     │  │syslog_parser │  │snmp_poller   │                  │
     │  │(daemon)      │  │(timer,1min)  │                  │
     │  └──────┬───────┘  └──────┬───────┘                 │
     │         │                  │                         │
     │         ▼ HTTP POST (line protocol)                  │
     │  ┌─────────────────────────┐                        │
     │  │   InfluxDB 2.7 (Docker) │ :8086                  │
     │  └────────────┬────────────┘                        │
     │               │ Flux queries                         │
     │  ┌────────────▼────────────┐                        │
     │  │   Grafana 11.x (Docker) │ :3000 (Tailscale only)│
     │  │   5 dashboards + alerts │                        │
     │  └─────────────────────────┘                        │
     │                                                      │
     │  rsyslog ← Router syslog UDP:514                    │
     └─────────────────────────────────────────────────────┘

     Technology Choices

     ┌───────────────┬──────────────────────────────────┬─────────────────────────────────────────────────────────────────────────────────────────────────────────────
     ────┐
     │   Decision    │              Choice              │                                                    Rationale
         │
     ├───────────────┼──────────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────────────────
     ────┤
     │ TSDB          │ InfluxDB 2.7 OSS                 │ Push model (scripts write directly via HTTP), Flux query language, built-in retention policies, good ARM64
         │
     │               │                                  │ Docker image
         │
     ├───────────────┼──────────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────────────────
     ────┤
     │ Visualization │ Grafana 11.x                     │ Standard, file-based dashboard provisioning, native InfluxDB/Flux support
         │
     ├───────────────┼──────────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────────────────
     ────┤
     │ Containers    │ Docker Compose                   │ InfluxDB + Grafana in containers; monitoring scripts native (need WiFi/network access)
         │
     ├───────────────┼──────────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────────────────
     ────┤
     │ Ping tool     │ fping                            │ Multi-target parallel ping, parseable output, lightweight
         │
     ├───────────────┼──────────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────────────────
     ────┤
     │ Scripts       │ Python 3.13                      │ Already installed, good subprocess/parsing support
         │
     ├───────────────┼──────────────────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────────────────
     ────┤
     │ Data format   │ InfluxDB line protocol via HTTP  │ Simple, no client library needed, just HTTP POST with requests
         │
     │               │ API                              │
         │
     └───────────────┴──────────────────────────────────┴─────────────────────────────────────────────────────────────────────────────────────────────────────────────
     ────┘

     Project Structure

     /opt/netmon/
     ├── install.sh                    # Main installer
     ├── docker-compose.yml            # InfluxDB + Grafana
     ├── config/
     │   ├── netmon.yml                # Central config (targets, intervals, etc.)
     │   ├── secrets.env               # Tokens/passwords (generated, mode 0600)
     │   ├── grafana/
     │   │   ├── grafana.ini           # Grafana config overrides
     │   │   ├── provisioning/
     │   │   │   ├── datasources/
     │   │   │   │   └── influxdb.yml
     │   │   │   └── dashboards/
     │   │   │       └── dashboards.yml
     │   │   └── dashboards/
     │   │       ├── 01-connectivity.json
     │   │       ├── 02-speedtest.json
     │   │       ├── 03-wifi.json
     │   │       ├── 04-router.json
     │   │       └── 05-system.json
     │   └── rsyslog/
     │       └── 60-netmon.conf
     ├── scripts/
     │   ├── common.py                 # Shared: config loader, InfluxDB writer, logging
     │   ├── ping_monitor.py           # Continuous fping to multiple targets
     │   ├── ip_checker.py             # Public IP change detection
     │   ├── wifi_station.py           # WiFi client metrics (wlan0) + system metrics
     │   ├── wifi_scanner.py           # Channel scan (wlan1, monitor mode)
     │   ├── speedtest_runner.py       # Hourly speedtest
     │   ├── syslog_parser.py          # Parse rsyslog from Omada
     │   ├── snmp_poller.py            # SNMP polling (optional)
     │   └── iperf3_simulator.py       # Nocturnal load simulation
     ├── systemd/
     │   ├── netmon-ping.service
     │   ├── netmon-ipcheck.service
     │   ├── netmon-wifi-station.service
     │   ├── netmon-wifi-scanner.service
     │   ├── netmon-syslog-parser.service
     │   ├── netmon-speedtest.service
     │   ├── netmon-speedtest.timer
     │   ├── netmon-snmp.service
     │   ├── netmon-snmp.timer
     │   ├── netmon-iperf3.service
     │   └── netmon-iperf3.timer
     └── README.md

     Implementation Plan

     Phase 1: Infrastructure (~files: install.sh, docker-compose.yml, config/)

     1. Create /opt/netmon/ directory structure
     2. Write docker-compose.yml:
       - InfluxDB 2.7 (arm64): port 8086, volume influxdb-data, mem_limit 512m, auto-setup with env vars (org=netmon, bucket=netmon, retention=30d)
       - Grafana 11.x (arm64): port 3000 bound to 127.0.0.1 + Tailscale, volume for grafana-data, provisioning mounts
       - Shared Docker network netmon
     3. Write config/netmon.yml with all tunable parameters
     4. Write config/secrets.env generation logic
     5. Write Grafana provisioning files (datasource + dashboard provider)
     6. Write config/rsyslog/60-netmon.conf for receiving router syslog
     7. Write install.sh orchestrating everything

     Phase 2: Common module + Ping monitor

     8. scripts/common.py: Config loader (YAML), InfluxDB line protocol writer (HTTP POST to /api/v2/write), structured logging (JSON to journald)
     9. scripts/ping_monitor.py: Run fping every 20s against all targets (gateway, 8.8.8.8, 1.1.1.1, meet.google.com, zoom.us), parse output, compute jitter (rolling
     stddev of latency), write to InfluxDB
     10. systemd/netmon-ping.service: Daemon, Restart=always, RestartSec=10

     Phase 3: IP checker + WiFi station monitor

     11. scripts/ip_checker.py: Check public IP every 60s via HTTP to ifconfig.me/ip, detect changes, write measurement public_ip with fields ip (string) and changed
     (0/1)
     12. scripts/wifi_station.py: Every 30s run iw dev wlan0 station dump + iw dev wlan0 link, parse signal/noise/tx_rate/rx_rate, also collect system metrics
     (CPU/mem/disk/temp via /proc and /sys)
     13. Corresponding systemd services

     Phase 4: Speedtest + iperf3 simulator

     14. scripts/speedtest_runner.py: Run speedtest-cli --json, parse download/upload/latency, write to netmon bucket
     15. scripts/iperf3_simulator.py: Run multiple iperf3 UDP streams simulating video calls, parse JSON output, write results. Requires an iperf3 server (can be
     localhost for baseline or a remote Tailscale peer)
     16. Corresponding systemd services + timers (speedtest hourly, iperf3 Mon/Wed/Fri 02:00)

     Phase 5: WiFi scanner

     17. scripts/wifi_scanner.py: Every 5min, put wlan1 in monitor mode, run iw dev wlan1 scan (or use iwlist scan), parse results for each channel (AP count,
     strongest signal, our APs), write to InfluxDB

     Phase 6: Syslog + SNMP

     18. scripts/syslog_parser.py: Tail /var/log/netmon/router.log (populated by rsyslog), classify events (WAN/DHCP/WiFi/firewall), write to InfluxDB as syslog_event
      measurements
     19. scripts/snmp_poller.py: Optional, disabled by default. Uses snmpget to poll router interface counters every 60s

     Phase 7: Grafana Dashboards (5 JSON files)

     20. 01-connectivity.json: Stat panels (latency/loss/IP/uptime), time series (latency/loss/jitter per target), IP change timeline
     21. 02-speedtest.json: Download/upload trends, iperf3 load test results, speedtest table
     22. 03-wifi.json: Signal strength, TX/RX rates, channel congestion bars, nearby APs table
     23. 04-router.json: Syslog event timeline, event counts by type, SNMP traffic, event log table
     24. 05-system.json: RPi CPU/RAM/disk/temp, service health

     Phase 8: Alert Rules

     25. Configure Grafana alert rules (provisioned via JSON or API):
     - Packet loss > 1% for 5min → Warning
     - Packet loss > 5% for 3min → Critical
     - Latency > 100ms for 5min → Warning
     - Jitter > 30ms for 5min → Warning
     - Public IP changed → Warning (instant)
     - Speedtest < 50% expected → Warning
     - WiFi signal < -75dBm for 5min → Warning
     - RPi temp > 70C → Warning
     - Data staleness (no ping data 5min) → Critical

     Phase 9: Documentation + Final installer

     26. Write README.md with installation, usage, troubleshooting, annotation guide
     27. Finalize install.sh with full automation

     InfluxDB Buckets & Retention

     ┌──────────────────┬───────────┬──────────────────────────────────────────────────────────────────────┐
     │      Bucket      │ Retention │                               Contents                               │
     ├──────────────────┼───────────┼──────────────────────────────────────────────────────────────────────┤
     │ netmon           │ 30 days   │ ping, public_ip, wifi_station, wifi_scan, syslog_event, snmp, system │
     ├──────────────────┼───────────┼──────────────────────────────────────────────────────────────────────┤
     │ netmon_speedtest │ 90 days   │ speedtest, iperf3 results                                            │
     └──────────────────┴───────────┴──────────────────────────────────────────────────────────────────────┘

     Resource Estimates

     ┌────────────────────┬──────────────┬─────────┬──────────┐
     │     Component      │     RAM      │ CPU avg │ Disk/day │
     ├────────────────────┼──────────────┼─────────┼──────────┤
     │ InfluxDB 2.7       │ ~200-400MB   │ 2-5%    │ ~15MB    │
     ├────────────────────┼──────────────┼─────────┼──────────┤
     │ Grafana 11.x       │ ~100-200MB   │ 1-3%    │ ~1MB     │
     ├────────────────────┼──────────────┼─────────┼──────────┤
     │ All Python scripts │ ~100MB total │ <3%     │ -        │
     ├────────────────────┼──────────────┼─────────┼──────────┤
     │ rsyslog            │ ~10MB        │ <1%     │ ~5MB     │
     ├────────────────────┼──────────────┼─────────┼──────────┤
     │ Total              │ ~500-800MB   │ ~5-10%  │ ~20MB    │
     └────────────────────┴──────────────┴─────────┴──────────┘

     Fits comfortably in 7.6GB RAM. At 20MB/day, 64GB SD has years of capacity (but retention policies limit to 30-90 days anyway).

     Verification Plan

     1. After install: docker compose ps shows InfluxDB + Grafana healthy
     2. After Phase 2: Open Grafana → Connectivity dashboard shows live ping data
     3. Check each systemd service: systemctl status netmon-*
     4. Trigger IP change detection: manually verify with curl ifconfig.me
     5. Force speedtest: systemctl start netmon-speedtest.service
     6. Verify WiFi scan data appears in WiFi dashboard
     7. Test syslog: logger -n 127.0.0.1 -P 514 "test message" → appears in Router dashboard
     8. Reboot RPi → verify all services auto-start
     9. Access only via Tailscale: verify port 3000 not accessible from LAN