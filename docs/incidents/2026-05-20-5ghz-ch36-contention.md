# 2026-05-20 — 5 GHz channel 36 contention causing widespread ping incidents

## TL;DR

- **Symptom**: ~400 ping incidents in 3 h on the morning of 2026-05-20 across **all** monitored targets (gateway, eap_mesh, 5 DNS resolvers), peaking 11:10-11:44 local with worst single spikes of 960-988 ms.
- **Misleading appearance**: looked like a DNS / WAN / ISP issue because DNS resolvers dominated the incident counts (~370 of 400).
- **Actual root cause**: 5 GHz channel 36 air-time contention. The Pi's WiFi link (and the mesh backhaul, and 5-9 external neighbour APs) were all sharing one congested channel. When channel utilisation peaked, Pi's transmits failed/retried at high rate → all pings appeared degraded simultaneously.
- **Fix**: moved planta-baja 5 GHz radio to channel 44 (much cleaner neighbours). After a sticky-2.4-GHz fallback issue, also enabled 2.4 GHz Rate Control + CCK rate disable + Management Rate Control on planta-baja.
- **Outcome**: incidents dropped to 0/min by 13:05 local, Pi tx_failed delta near zero, Pi back on 5 GHz at 433 Mbit/s.

## Diagnostic methodology (the reusable bit)

The key technique that unlocked this: **compare Pi-side ping latency against the router-side back-probes** (`router_ping` measurement). When the Pi reports an incident, the router probes 1.1.1.1 and 8.8.8.8 right after via the same path.

- If router-side probes are **clean** while Pi-side is bad → problem is between Pi and router (local WiFi)
- If router-side probes are **also bad** → problem is past the router (WAN / ISP)

In this incident, during the 11:45 storm peak (50+ Pi incidents in 5 min), only 1 of 14 router-side probes was elevated. That immediately ruled out WAN as the dominant cause and pointed at local WiFi.

A second key signal: **the Pi's `wifi_station.tx_failed` counter delta over the storm**. It jumped +182 in 5 minutes during the peak, with tx_bitrate occasionally falling to 24 Mbit/s (MCS fallback). Compared to a baseline of near-zero growth, this proves the WiFi link itself was failing — not just observation latency.

## Timeline (local local time)

| Time | Event |
|---|---|
| 09:00-11:50 | Storm window observed: ~400 incidents across all targets |
| 11:10-11:44 | Storm peak with worst spikes (960-988 ms on DNS targets, 287 ms on gateway) |
| 12:22 | Changed planta-baja 5 GHz radio: ch 36 → ch 44 in Omada controller |
| 12:23 | Pi briefly disconnected during AP radio reload, fell back to **2.4 GHz ch 6** at 6.5 Mbit/s (sticky-client behaviour) |
| 12:25 | Second storm triggered by Pi being on degraded 2.4 ch 6 (117 incidents in one 5-min bucket) |
| 12:42-12:52 | Pi briefly returned to 5 GHz ch 44 then bounced back to 2.4 — ping-ponging |
| 12:49 | Applied Omada `802.11 Rate Control` to 2.4 GHz: min 12 Mbps + Disable CCK Rates + Management Rate Control + Beacons-at-1-Mbps OFF |
| 12:58 | Pi forced off 2.4 GHz by rate control, settled on 5 GHz ch 44 at -56 dBm / 433 Mbit/s |
| 12:58-13:00 | Transient storm from AP radio reload after rate control config push |
| 13:05 | Incidents at 0/min — baseline achieved |

## Investigation findings

### Pi WiFi was on 5 GHz ch 36 — the same channel as the mesh backhaul

Initial state:
- Pi `wlan0` → planta-baja BSSID `aa:aa:aa:11:11:1b`, 5180 MHz (ch 36), 80 MHz width
- Mesh backhaul (`apclix0`) between planta-1 ↔ planta-baja: also ch 36
- External neighbours on ch 36: **5-9 BSSIDs, strongest at -41 dBm** (very loud)

Pi-AP signal was -56 dBm. Neighbours at -41 dBm are 15 dB **stronger** than the AP it's listening to — every transmission required waiting through neighbour traffic via CSMA-CA.

### Storm signature was uniform across all targets

At 11:45 (09:45 UTC), in one 5-min bucket:
- dns_quad9: 20 incidents, dns_level3: 16, dns_cloudflare2: 15, dns_google: 14, dns_cloudflare: 12
- gateway: 11, eap_mesh: 7
- **Every target hit at the same time** — diagnostic for local rather than WAN issue

### Single-radio mesh constraint discovered

The EAP610 has **one 5 GHz radio per AP**, used simultaneously for client service AND mesh backhaul. The wireless mesh requires both endpoints on the same channel, so:

- Both APs must run on the same 5 GHz channel as long as backhaul is wireless
- **Cannot** split client load across different 5 GHz channels per AP without wired backhaul
- See [eap610_telemetry](memory:eap610_telemetry.md) and [eap610_single_radio_mesh](memory:eap610_single_radio_mesh.md) memories

## Changes applied

### 1. Planta-baja 5 GHz: channel 36 → channel 44

Chose ch 44 over DFS alternatives (100, 108, 116) because:
- Non-DFS = no Channel Availability Check delay
- No radar-eviction risk (relevant in regions with nearby airports)
- Scan showed only 4-5 neighbours on ch 44 and all at -84 dBm (effectively invisible)

Could have gone to ch 100 (cleanest DFS) or ch 108 (1 neighbour at -80 dBm) — kept those as fallbacks if ch 44 fills up.

### 2. Omada 2.4 GHz Rate Control on planta-baja

Settings:
- Enable Minimum Rate Control: **ON**
- Slider: **12 Mbps**
- Enable Management Rate Control: **ON**
- Disable CCK Rates (1/2/5.5/11 Mbps): **ON**
- Require Clients to Use Rates at or Above the Minimum Rate: **ON**
- Send Beacons at 1 Mbps: **OFF**

Purpose: make 2.4 GHz unattractive to weak/sticky clients. Beacons jump from 1 Mbps to 12 Mbps (~6× air-time savings), and clients that can't sustain 12 Mbps get evicted to 5 GHz.

This was triggered by the **post-channel-change sticky-2.4 fallout**: the Pi (and presumably other dual-band clients) dropped to 2.4 GHz ch 6 during the brief 5 GHz reload window, then refused to roam back — got stuck on 2.4 ch 6 at -66 dBm / 6.5 Mbit/s, causing a worse storm than the original.

## Outcome

| Metric | Before | After |
|---|---|---|
| Pi WiFi band | 5 GHz ch 36 (later 2.4 ch 6) | 5 GHz ch 44 |
| Pi signal | -56 dBm | -56 dBm |
| Pi tx_bitrate | 433 Mbit/s (dipping to 24 under contention) | 433 Mbit/s stable |
| Pi tx_failed delta during peak | +182 / 5 min | +12 / 8 min |
| Ping incidents | 117 / 5 min (peak) | 0 / min sustained |
| Mesh signal | -52 to -57 dBm | -54 to -58 dBm (unchanged, healthy) |

## Considered but did not do

- **Move to DFS channel (100 / 108)** — would have been cleaner from neighbours but added CAC delay and radar-eviction risk. Kept as fallback.
- **Split APs onto different 5 GHz channels** — physically impossible with single-radio EAP610 mesh; would break backhaul.
- **Disable 2.4 GHz entirely on planta-baja** — considered but kept as relief valve and for any IoT that needs it. Re-evaluate if internal 5 GHz contention becomes a problem.
- **Min RSSI threshold on 2.4** — Rate Control was sufficient on its own; min RSSI is the secondary lever to enable if stickiness returns.
- **Force-deauth all 2.4 clients from controller** — would have worked but Rate Control is a permanent fix vs a one-off kick.

## Follow-ups

1. **Watch through busy afternoon hours** — concentrated 5 GHz load on the single shared channel (clients + mesh) could re-create contention from *internal* load instead of external neighbours. Signal would be `tx_failed` deltas climbing + incidents returning even though external scan stays clean.
2. **Wire planta-1 to the router via ethernet** — the real long-term fix. Eliminates wireless mesh, frees planta-1's 5 GHz radio to be on a different channel from planta-baja, doubles total 5 GHz air-time budget. Solves the single-shared-channel limitation properly.
3. **Verify the 12 Mbps 2.4 minimum doesn't break any IoT** — check over the next week for any 2.4-only device that stopped working (smart plugs, etc.). If something breaks, bump down to 9 Mbps or whitelist it.
4. **Reconsider eap_radio PER metric** — planta-1 2.4 GHz still reports 22.4% PER but planta-1 2.4 SSID is now disabled, so this is residual from before. Worth a config-time reset of the counter or just understanding it's stale.

## Annotations in InfluxDB

These events were logged via `scripts/log_event.sh` and appear as green vertical lines on the Grafana dashboards:

- `2026-05-20 10:49:08Z` — "Omada: 802.11 Rate Control enabled on 2.4 GHz, min 12 Mbps"
- (Channel change at ~10:22 UTC was not logged — should have been; lesson: log *before* the change too)

---

# Afternoon follow-up (same day, 14:00-16:00 local)

The morning's diagnosis (mesh contention on ch 36) was correct, and the fixes (ch 36→44 + Rate Control) materially improved the network. But the afternoon's stress tests proved the **mesh contention problem is not fully eliminated** — just deferred until higher load. A colleague separately involved a technician who installed a third-party AP, which complicated the topology further without solving the underlying issue.

## New topology context (as of afternoon)

A technician brought in and installed a **third-party AP** wired directly to the ISP-A/ISP-A router, broadcasting SSID `<own-ssid-secondary>`. Important details:

- **BSSID**: `bb:bb:bb:22:22:7a`
- **Band**: 2.4 GHz only (channel 11) — third-party's 5 GHz radio is NOT broadcasting `<own-ssid-secondary>`. Either disabled in the technician's config, or the model is 2.4-only.
- **Physical placement**: literally next to the ISP-A router (downstairs near the gateway closet). This means it duplicates planta-baja's downstairs coverage rather than serving the upstairs gap.
- **Network**: clients on `<own-ssid-secondary>` get `192.168.1.x` (ISP-A subnet) instead of `192.168.0.x` (Omada subnet).

**Why this AP isn't helping much:**
1. It's downstairs where planta-baja already covers (no spatial diversity)
2. It's 2.4 GHz only — slow, congested band
3. Different IP subnet from the Omada APs, so roaming between APs forces a DHCP renegotiation (brief reconnect)
4. Not Omada-managed (separate vendor) — band steering and Min-RSSI can't be coordinated with the Omada APs

**The proper relocation would be**: take the third-party upstairs to replace planta-1's role and run a cable up there. Currently impossible per a colleague ("que yo sepa no podemos tirar cable hacia arriba"). Until then the third-party is essentially decorative.

A **better alternative** a colleague identified during discussion: instead of relying on the third-party, cable the Omada planta-baja AP directly to the ER706W gateway and use the ER706W's built-in WiFi 7 tri-band radio as a second client AP (on a different 5 GHz channel, or on 6 GHz). This keeps everything in one subnet/one Omada controller. Requires a cable run between planta-baja AP and ER706W (typically much shorter than running cable upstairs). Not done yet.

## Planta-1 toggle: zombie-client problem

A coverage-vs-contention trade-off emerged. Test sequence:

1. **Planta-1 turned OFF** (around 14:00 local) to remove the wireless mesh from ch 44 → upstairs clients lost coverage; the Omada controller showed iPhones at **-94 dBm** still associated to planta-baja, retransmitting constantly and dragging the channel down. Incident rate climbed from ~6/min → 10-24/min over ~90 minutes.
2. **Planta-1 turned back ON** (~14:55 local) — clients still didn't migrate at first because the AP came up but its SSID was not broadcasting (separate config item: enabling AP ≠ enabling SSIDs on it). Once we enabled the <own-ssid> SSID on planta-1's 5 GHz radio explicitly, planta-1 started broadcasting BSSID `aa:aa:aa:11:22:69` and the iPhones reassociated with much better signal.
3. **Net result**: incident rate dropped back to ~5/min after re-enablement, and iperf3 jitter went from 4.5 ms (planta-1 off, zombies present) → 0.94 ms (planta-1 on, clean state).

**Lesson**: zombie clients (devices at -94 dBm hammering the closest AP with failed retries) are a worse air-time cost than mesh contention, in this setup. With only one AP serving the building, fringe clients drag the channel down via retries.

## Min-RSSI not yet applied (open follow-up)

Recommended config to apply in Omada controller (not yet done):
- **5 GHz radio of BOTH planta-baja AND planta-1**: Minimum RSSI threshold = **-72 dBm**
- This force-kicks weak clients so they roam to the closer AP instead of clinging to a distant one. Pairs with the band-steering / Rate Control already on 2.4.

## Stress test results — the critical finding

Three iperf3 stress tests proved that the morning's mesh-contention issue is **bounded but not eliminated**. The current setup handles light/moderate load fine; sustained heavy load still reproduces the original storm signature.

### Test 1: 50 Mbps UDP for 30s, network otherwise calm
```
Loss:     0/129,490 = 0%
Jitter:   0.256 ms
Pi tx_failed: +30 over 30s (~1/sec, negligible)
```
**Verdict**: clean. The setup easily handles ~10 HD videocalls' worth of synthetic load.

### Test 2: 50 Mbps UDP for 30s during a real videocall (a colleague on a call)
```
Loss:     0%
Jitter:   0.300 ms
Pi tx_failed: +394 over 30s (~13/sec, 13× more than Test 1)
Brief DNS storm at start: dns_cloudflare spike to 844 ms
```
**Verdict**: the network still carried it (zero loss), but the Pi was working materially harder. DNS-path briefly stressed for ~14 seconds.

### Test 3: 50 Mbps UDP for 3 MINUTES during the same videocall
```
Loss:     0/776,964 = 0%
Jitter:   0.215 ms
Pi tx_failed: +234 over 180s (1.3/sec — paradoxically lower per-second rate than Test 2)
A COLLEAGUE REPORTED 2 PERCEIVED VIDEOCALL CUTS during this test.
```

The critical event at **15:59:20 local (13:59:20 UTC)** — a storm with the EXACT MORNING SIGNATURE:
```
gateway:     291 ms     ← LAN-internal, smoking gun
eap_mesh:    342 ms     ← LAN-internal
dns_cloudflare:  371 ms
dns_cloudflare2: 321 ms
dns_google:      271 ms
dns_quad9:       242 ms
ALL TARGETS rtt_spike SIMULTANEOUSLY at 13:59:20
```

This is the same fingerprint as the morning storm: gateway + mesh + all DNS all elevated together = local WiFi+mesh contention. After ~10-20 seconds the channel recovered (gateway back to 5.8 ms by 13:59:50). A colleague perceived this as cuts in his real call.

**Conclusion from stress tests**: today's channel/rate-control changes meaningfully reduced contention but did NOT eliminate the structural mesh+client shared-channel limitation. Under sustained ≥50 Mbps load (real coworking peak with multiple concurrent calls + a download), the mesh saturates the shared 5 GHz channel and clients perceive cuts. The wired-backhaul option (cable planta-baja to ER706W, then use ER706W's WiFi for a second channel) is the only structural fix.

## Grafana exposed for LAN access

To let a colleague monitor in real time, Grafana port binding changed from `127.0.0.1:3000` to `0.0.0.0:3000` and anonymous Viewer role enabled. Accessible at `http://192.168.0.171:3000/` from any device on `<own-ssid>`. Updated CLAUDE.md. Pi's DHCP IP `192.168.0.171` should be DHCP-reserved in Omada to keep the URL stable across reboots (not yet done).

## Updated follow-ups (priority order)

1. **Wire planta-baja Omada AP to ER706W** — the real fix. Eliminates the mesh on planta-baja entirely. Enables ER706W's WiFi 7 tri-band (incl. 6 GHz) to serve as a second AP on a different channel for ~2-3× total capacity. Single subnet, single controller, all Omada-managed.
2. **Apply Min-RSSI -72 dBm on 5 GHz both APs** — enforces clean client distribution, prevents zombie-client retries. ~5 min config change.
3. **Repurpose or remove the third-party** — currently adds 2.4 GHz noise on ch 11 without serving anyone useful. Either move upstairs (requires cable) or turn off.
4. **DHCP reservation for the Pi** in Omada controller — keeps `192.168.0.171` stable so the Grafana URL doesn't break on Pi reboot.
5. **Re-test under realistic concurrent-call load** once #1 is done — confirms the wired-backhaul fix actually eliminates the storm signature under stress.

## Updated TL;DR (afternoon)

Morning's diagnosis was correct; morning's fixes were the right immediate moves. **But "the issue is solved" requires a qualifier**: it's solved under normal coworking load, and it's NOT solved under heavy sustained load (50 Mbps+ for minutes). A colleague perceived 2 cuts during a 3-minute synthetic stress test that exactly reproduced the morning signature. The structural fix (wired backhaul to free the 5 GHz radio from sharing with mesh) remains the only way to give the network real headroom for peak coworking activity.
