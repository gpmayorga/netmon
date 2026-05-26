# Omada CLI reference (ER706W-4G, cloud-managed)

This document is the consolidated reference for what the **TP-Link Omada ER706W-4G** gateway CLI actually exposes when the device is **adopted by an Omada Cloud-Based Controller (CBC)**. Written 2026-05-20 against firmware `2.1.10 Build 20260109 Rel.59943`.

If TP-Link releases newer firmware that adds capabilities, re-probe and update this doc. Each command below has been individually tested against the live device — entries are not aspirational.

## TL;DR — what's usable

| Capability | Available? | Where |
|---|---|---|
| WAN port UP/DOWN | ✅ | `show interface switchport <N>` |
| Per-port IP/gateway/DNS/MTU | ✅ | `show interface switchport <N>` |
| LAN configuration (vlan1) | ✅ | `show interface vlan 1` |
| Firmware version | ✅ | `show system-info` |
| Uptime | ✅ | `show system-info` |
| ARP table (active devices) | ✅ | `show arp` |
| Basic ICMP ping | ✅ (no flags) | `ping <ip>` |
| Per-WAN ping / `-I` | ❌ | CLI rejects all flags |
| CPU / Memory / Temperature | ❌ | Not in `show system-info` on this model |
| SNMP | ❌ | Off, cannot enable (CLI stub) |
| Syslog redirect | ❌ | Not exposed in CBC UI |
| Per-WAN traffic counters | ❌ | No `show interface counters` |
| Routing table | ❌ | `show ip route` "not registered" |
| NAT sessions | ❌ | `show nat` "not registered" |
| Full Linux shell | ❌ | Restricted CLI only |
| Open API (REST) | ❌ for CBC | CBC doesn't expose it; SW Controller v5.12+ does |

If a column shows ❌, no amount of typing in this CLI will get it. Stop trying.

## Access

- **Host**: `192.168.0.1`
- **Port**: `2222` (site-wide override; default 22 was changed in Omada Cloud → `Settings → Services → Device Account & SSH`)
- **User**: the Omada site's "Device Account" username (e.g. `device-admin`)
- **Auth**: password only (no key auth on this firmware)
- **SSH options required** (legacy algorithms):
  ```
  -o HostKeyAlgorithms=+ssh-rsa
  -o PubkeyAcceptedAlgorithms=+ssh-rsa
  -o KexAlgorithms=+diffie-hellman-group1-sha1,diffie-hellman-group14-sha1
  -o Ciphers=+aes128-cbc,aes256-cbc,3des-cbc
  ```
- See `scripts/ssh_helper.py` for the canonical client used by all NetMon collectors.

**Critical**: the **"Remote Assistance"** toggle in Omada Cloud's gateway settings controls SSH for the gateway entirely. Turning it off kills SSH on port 22/2222 (you'll get `Connection refused`). The site-wide SSH toggle alone is **not sufficient** for the gateway. EAP SSH is *not* tied to Remote Assistance — that's gateway-only.

## Session model

After password auth you land at the user-mode prompt:

```
T2600G-28TS>
```

Privilege levels:

1. **User mode** (`>`) — read-only, very limited. `show system-info` is denied.
2. **Privileged mode** (`#`) — enter with `enable`. No enable password on this firmware. `show system-info` works here.
3. **Configure mode** (`(config)#`) — entered with `configure`. **Stub mode**: only `help`, `exit`, `show`, `clear` are registered. **You cannot configure anything from the CLI.** All config lives in the controller.

`ssh_helper.run_commands()` automatically promotes user→privileged after login.

### Top-level command tree

From `?` at user mode:

```
help         Show available commands
exit         Exit from current mode
enable       Turn on privileged commands
disable      Turn off privileged commands
configure    Enter configuration mode    ← stub mode; see above
show         Display module information
ping         Ping ip address
tracert      traceroute ip address
clear        Clear statistic
reboot       Reboot device               ← do not run from monitoring
reset        Reset device                ← absolutely do not run
roll-back    Roll back to the previous software version
```

## `show` subcommands

`show ?` lists 15 subcommands. **5 of them actually return data on this firmware.** The rest are either stubs ("not registered yet") or vestigial.

| `show <X>` | Returns data? | Notes |
|---|---|---|
| `system-info` | ✅ | Only firmware + uptime on ER706W (no CPU/mem/temp) |
| `interface switchport <N>` | ✅ | Port N=1..3. Status, IP, MAC, MTU, gateway, DNS |
| `interface vlan <N>` | ✅ | vlan IDs in use: 0 (WAN2), 1 (LAN), 4093 (WAN3) |
| `arp` | ✅ | Active L2 neighbors per vlan |
| `snmp-server` | ✅ (state only) | Shows `off/off`; cannot toggle from CLI |
| `history` | ✅ | Your own command history — not useful for monitoring |
| `ip rip` / `ip ospf` | ✅ (mostly empty) | Routing protocol state; nothing useful unless you run those |
| `ip route` | ❌ | "Command is not registered yet,please check." |
| `ip http` | ❌ | "not registered" |
| `nat` | ❌ | "not registered" — no NAT session table available |
| `all` | ❌ | "not registered" |
| `network` | ❌ | "OSPF is not enabled" |
| `ssh` | ❌ | "not registered" (despite SSH being on) |
| `urlfilter` | ❌ | "not registered" |
| `crypto` / `ikev1` / `ikev2` / `transform-set` | ❌ | "not registered" — IPsec submenus stubbed |

**Trap**: the help system lists commands TP-Link planned but didn't implement. Don't trust `?` alone — actually test the command. Above table is the source of truth.

## Per-command output formats

### `show system-info`

Privileged mode (`#`) required.

```
Hardware version - ER706W-4G v1.0
Software version - 2.1.10 Build 20260109 Rel.59943
Mac address      - AA-AA-AA-11-11-63
Running time     - 18 day - 4 hour - 32 min - 29 sec
```

**Fields available on ER706W**: hardware version, software (firmware) version, MAC, running time.

**Fields ABSENT on ER706W** (but present on other Omada gateways): CPU usage, memory usage, temperature. The dashboard panels for these are intentionally empty / removed.

Parser: `scripts/router_monitor.py:parse_system_info` — uses both `:` and ` - ` as key/value separators; running time is parsed via two regex variants to handle multiple firmware time formats.

### `show interface switchport <N>` (N = 1, 2, 3)

```
show interface switchport 2
     Port name..................WAN2
     Belonged vlan..............0
     Pvid.......................0
 Vlan0 config
     Vlan type..................wan
     Routing Interface Status...UP
     Primary IP Address.........192.168.1.217/255.255.255.0
     MAC Address................AA-AA-AA-11-11-66
     Proto......................dhcp
     Mtu........................1500
     Bandwidth..................1000kbps
     Uplink.....................1000000kbps
     Downlink...................1000000kbps
     Default Gateway............192.168.1.1
     Primary DNS................80.58.61.250
     Secondary DNS..............0.0.0.0
```

**Key field for monitoring**: `Routing Interface Status` (UP/DOWN). Everything else is mostly static after boot.

**No traffic counters**. No bytes/packets/errors. This is the biggest single gap — most Omada gateways expose counters, this firmware does not.

`Bandwidth` line is the configured rate cap, not a live measurement.

`show interface switchport all` is rejected (`Invalid parameter all`). You must query each port individually.

### `show interface vlan <N>` (N ∈ {0, 1, 4093} on this device)

```
show interface vlan 1
     vlan1 ports:1 4 5 6
     Vlan type..................lan
     Routing Interface Status...UP
     Proto......................static
     Dhcp mode..................dhcp_server
     Dhcp enable................on
     Primary IP address:........192.168.0.1/255.255.255.0
     Dhcp start.................192.168.0.100
     Dhcp end...................192.168.0.199
     Dhcp release time..........120min
     Dhcp relay enable..........off
     Macaddr....................AA-AA-AA-11-11-63
     Mtu........................1500
```

Useful once per cycle if you want to detect DHCP scope changes, default gateway moves, etc. Static-ish data.

### `show arp` ← the workhorse

```
---------------------------------------------------------------------------------------------------
Interface     IP Address      MACAdress              Type      AGE
---------------------------------------------------------------------------------------------------
vlan1         192.168.0.100   AA-AA-AA-11-11-1A      Dynamic   N/A
---------------------------------------------------------------------------------------------------
vlan1         192.168.0.171   DD-DD-DD-99-99-CC      Dynamic   N/A
---------------------------------------------------------------------------------------------------
vlan0         192.168.1.1     CC-CC-CC-05-05-05      Dynamic   N/A
---------------------------------------------------------------------------------------------------
vlan4093      192.168.2.1     CC-CC-CC-07-07-44      Dynamic   N/A
```

Why this is the most useful command we have:

1. **vlan1 row count = active LAN devices.** Subtract infrastructure (2 EAPs + the Pi = 3) to get the human-client count. Polled once per cycle into `router_lan.total_devices` (see `parse_arp` / `format_lan_line` in `router_monitor.py`).
2. **vlan0 / vlan4093 rows = L2 reachability of the ISP upstream gateways.** A WAN port can be UP but the ISP gateway unreachable; ARP-presence is a stronger uptime signal. Polled into `router_wan_l2,vlan=<vlanN> gateway_up`.
3. The MAC column is the device MAC (Ethernet), not BSSID. EAPs appear as their base MAC; the BSSIDs in `ap_labels` match.

Parser: `scripts/router_monitor.py:parse_arp`. Regex: `^(vlan\d+)\s+(IPv4)\s+([0-9A-Fa-f-]{17})`.

Note: ARP "AGE" is always `N/A` on this firmware — useless. Don't try to compute "last seen" from it; rely on whether the row reappears each cycle.

### `show snmp-server`

```
SNMPv1-v2c:          off
SNMPv3:              off
```

State-only. Cannot toggle from CLI (`(config)#` mode is a stub — `snmp-server enable` returns "Invalid command"). Cannot toggle from CBC UI either (not exposed). Stuck at off. The OIDs in `netmon.yml`'s `snmp` section are vestigial; the `snmp_poller.py` collector will only ever return empty data while the device is cloud-managed.

### `ping <ip>`

```
ping 8.8.8.8
PING 8.8.8.8 (8.8.8.8): 56 data bytes
64 bytes from 8.8.8.8: seq=0 ttl=113 time=3.335 ms
64 bytes from 8.8.8.8: seq=1 ttl=113 time=4.435 ms
64 bytes from 8.8.8.8: seq=2 ttl=113 time=4.603 ms
64 bytes from 8.8.8.8: seq=3 ttl=113 time=4.596 ms

--- 8.8.8.8 ping statistics ---
4 packets transmitted, 4 packets received, 0% packet loss
round-trip min/avg/max = 3.335/4.242/4.603 ms
```

**Accepts ONLY the destination address.** Every flag is rejected:

- `ping 8.8.8.8 -c 3` → `Error: Too many paramerters.` (sic — firmware typo)
- `ping 8.8.8.8 -I wan2` → same error
- `ping 8.8.8.8 -W 2` → same

Hard-coded count (4) and timeout. **There is no way to attribute a ping to a specific WAN from this CLI** — this is a structural limitation, not a permissions issue. The Omada Cloud web UI's per-WAN ping diagnostic uses a controller-side endpoint that we cannot reach from the CLI.

`wan_ping.py` therefore does the next-best thing: SSH in during incidents, run `ping <target>` (whichever WAN the load balancer picks), and compare the resulting RTT to the Pi's measurement. If router-side RTT is normal while Pi-side spiked, the issue is between Pi and router (WiFi/mesh). If both spiked, upstream of router. We lose per-WAN isolation.

### `tracert <ip>`

Same restriction as `ping` — destination only, no flags. Not currently used by NetMon (round-trip is enough; we don't need a full path trace).

### `clear ?`

Documented as "Clear statistic" — what statistics, you ask? Unknown. `clear ?` doesn't enumerate subcommands. Not investigated further because (a) we don't have counters to clear, (b) running it from monitoring code would be destructive in unknown ways.

## `configure` mode: it's a trap

Enter with `configure`. Prompt becomes `(config)#`. Looks like Cisco-style config mode. **It isn't.**

`?` in config mode returns only:

```
help    Show available commands
exit    Exit from current mode
show    Display module information.
clear   Clear statistic.
```

Trying to configure anything fails:

- `snmp-server enable` → `Error: Invalid command "snmp-server"`
- `interface ...` → invalid
- `ip route ...` → invalid

**Conclusion**: config mode is a stub. Real configuration is impossible from the CLI; everything routes through the CBC UI.

## The Omada Cloud / API situation

The router is adopted by an Omada **Cloud-Based Controller (CBC)** at `99.80.193.119` (TP-Link AWS). This is the controller TP-Link hosts on their own infrastructure for free-tier Omada users.

**Critical: CBC does not expose an Open REST API** (confirmed via TP-Link community KB, status as of Dec 2023, unchanged through 2026-05). Only:

- **Omada Software Controller (self-hosted Docker)** — Open API since v5.12
- **Omada Hardware Controller (OC200 / OC300)** — Open API since v5.12
- **Omada Pro line** (enterprise) — all versions

The implication: if you ever decide you need per-WAN traffic counters, per-AP client lists, syslog, SNMP, or per-WAN ping, the route is **migrate the site from CBC to a self-hosted Software Controller** running on the same Pi (Docker image `mbentley/omada-controller` or the official TP-Link build). It's a half-day project including config migration without downtime.

The local web UI at `https://192.168.0.1/` returns `403 Permission Denied: Controller Software is running` when the device is adopted. The only escape hatches: "Forget" the device from the controller (wipes config) or migrate to self-hosted.

## Operational rules for collectors using this CLI

1. **Always open SSH on port 2222** via `ssh_helper.run_commands(host, password, commands, port=2222)`. Hardcoding `22` will break the day after someone touches the port setting.
2. **Always go through `ssh_helper`**, never spawn `ssh` directly — the helper handles pexpect prompts (`$`, `>`, `#`, `(config)#`), legacy algorithms, and auto-`enable`.
3. **Never run commands that contain `>` or `$` or `#` characters** (e.g. shell redirection) — the prompt regex in `ssh_helper` will match prematurely.
4. **Read-only only.** Don't run `reboot`, `reset`, `roll-back`, or `clear` from monitoring code, ever.
5. **Don't chase commands the help advertises.** If it returns "not registered yet", it never will; move on.
6. **One SSH session per cycle if possible.** Each connection has ~1-2 s setup overhead. Bundle commands via `run_commands([...])`.

## Future probes worth re-running on firmware updates

If the firmware bumps a major version (e.g. 2.2.x, 3.x):

```
show ip route               # would unlock route table → multi-WAN routing visibility
show nat                    # would unlock NAT session table → per-flow telemetry
show interface counters     # if it ever appears → per-WAN traffic metrics
ping <ip> -c N -I <wan>     # check if flag parser was fixed
configure → snmp-server     # check if (config)# mode became real
```

Update this doc if any starts working.

## Related references

- Memory: `[[omada-cli-capabilities]]`, `[[omada-ssh-access]]`, `[[omada-cloud-locked-features]]`
- Collectors using this CLI: `scripts/router_monitor.py`, `scripts/wan_ping.py`
- Shared SSH machinery: `scripts/ssh_helper.py`
- EAP-side telemetry (separate firmware, full BusyBox): `[[eap610-telemetry]]`
- Dashboard surfaced from this CLI: `config/grafana/dashboards/04-router.json` ("Router & WAN")
