# WiFi RF Environment — Baseline (2026-05-20)

Snapshot of channel occupation in the local environment, captured from passive monitor-mode scanning on `wlan1` (Realtek RTW8822BU) over **~36 hours** between 2026-05-19 18:00Z and 2026-05-20 ~14:00Z. **2,387 channel-summary samples** across a Tuesday evening + Wednesday workday.

The purpose is to bake this baseline once so we can free `wlan1` for client use (e.g. monitoring `<own-ssid-secondary>` directly). The `wifi_scanner` daemon will go optional — re-enable it if a future incident suggests RF environment has shifted, but for routine operations the baseline below is sufficient.

## Data caveats

- **Window is short** (~1.5 days). Patterns from a longer window (1-2 weeks) could reveal weekly cycles (e.g. weekends vs weekdays) we haven't captured.
- **wlan1 in monitor mode only scans intermittently** — channel-hopping passive scans, not continuous. So we see a subset of frames per channel per minute, but the trend is meaningful.
- **Our own APs are part of the observed environment.** When planta-baja moved from 5 GHz ch 36 → ch 44 at ~10:22 UTC today, ch 36 strongest-signal dropped from ~-42 dBm to ~-84 dBm (we stopped contributing to it ourselves). The ch 36 numbers below average across both states.
- **Signal level (`strongest_dbm`)** is the strongest single AP heard on that channel during a scan, not aggregate. A channel with 5 APs at -80 dBm is much cleaner than one with 1 AP at -40 dBm despite the count.

## 2.4 GHz — channel ranking

3 non-overlapping channels in EU. Lower count + weaker neighbours = cleaner.

| Channel | Mean AP count | Mean strongest signal | Verdict |
|---|---|---|---|
| **1** | **1.1** | -53 dBm | **Cleanest 2.4 channel by far** |
| 6 | 2.9 | **-39 dBm** | LOUD — strongest neighbours of any channel in the building |
| 11 | 4.1 | -47 dBm | Most APs but moderately quiet signals |

**Recommendation**: 2.4 GHz APs in this environment should be on **ch 1**, not 6. Planta-baja Omada is currently on ch 6 (the worst). The third-party `<own-ssid-secondary>` is on ch 11 (acceptable).

If we ever move planta-baja 2.4 → ch 1, the change-in-place would noticeably improve 2.4 conditions for any client that lands there.

## 5 GHz — channel ranking

EU has many more 5 GHz channels. Several DFS channels (52-144) are nearly empty in this environment. Non-DFS = 36/40/44/48 (UNII-1) and 149/153/157/161 (UNII-3 — but UNII-3 needs verification per country).

Sorted by mean ap_count (lowest = least crowded):

| Channel | Type | Mean AP count | Mean strongest | Notes |
|---|---|---|---|---|
| **108** | DFS | 1 | -81 dBm | Effectively empty |
| **120** | DFS | 1 | -80 dBm | Effectively empty |
| **56** | DFS | 1 | -77 dBm | Effectively empty |
| **64** | DFS | 1.7 | **-86 dBm** | Empty + extremely weak signals |
| 60 | DFS | 1.9 | **-89 dBm** | Empty + extremely weak signals |
| **149** | UNII-3 | 1.9 | -63 dBm | Where `<own-ssid-secondary>` lives — moderate signals |
| 40 | UNII-1 | 2.2 | -72 dBm | OK |
| 116 | DFS | 2.4 | -77 dBm | OK |
| 48 | UNII-1 | 3.5 | -76 dBm | OK |
| 52 | DFS | 3.5 | -62 dBm | Moderate signals |
| **44** | UNII-1 | 4.6 | -76 dBm | **CURRENT — chosen 2026-05-20.** Many APs but all weak |
| 100 | DFS | 6.9 | -69 dBm | Crowded but moderate signal |
| 36 | UNII-1 | 5.3 | **-48 dBm** | **WORST: many APs AND loud** (this morning's storm source) |

**Recommendation**: Channel 44 (our current choice) is a reasonable middle: non-DFS, moderate AP count, weak neighbours. **Cleaner alternatives exist** if you accept DFS (CAC delay + radar eviction risk):

- **ch 60 or 64** — practically empty (1.7-1.9 APs at <-86 dBm)
- **ch 108** — 1 AP at -81 dBm
- **ch 56 / 120** — also nearly empty

For mesh stability we kept ch 44 (non-DFS), but if mesh contention recurs after future changes, **try ch 60 or 108 next** before considering hardware changes.

## Loudest individual neighbours (top 10 by mean signal)

Strongest external APs in the local environment's RF environment. These are the actual physical noise sources:

| BSSID | SSID | Band | Channel | Mean signal | Note |
|---|---|---|---|---|---|
| cc:cc:cc:01:01:01 | Neighbour-Hotspot-A | 2.4 | 1 | -41 dBm | Strongest single neighbour. Phone/MiFi hotspot from a nearby unit |
| cc:cc:cc:02:02:02 | Neighbour-Personal-Hotspot | 2.4 | 6 | -46 dBm | Phone hotspot (personal device, intermittent) |
| cc:cc:cc:03:03:03 | Neighbour-Printer-WiFi-Direct | 2.4 | 6 | -51 dBm | Printer WiFi Direct (always-on, ch 6 noise source) |
| bb:bb:bb:22:22:2a | (own) third-party hidden / BBBBBB22222A | 2.4 | 1 | -52 dBm | **OWN** U6-Pro's hidden secondary SSID |
| cc:cc:cc:04:04:04 | Unknown-Random-SSID | 2.4 | 11 | -52 dBm | Long-random-string SSID — probably IoT/smartphone |
| cc:cc:cc:05:05:05 | Neighbour-ISP-A | 2.4 | 6 | -56 dBm | Neighbour tenant's ISP-A router |
| cc:cc:cc:06:06:24 | Neighbour-Router-C | 2.4 | 11 | -56 dBm | third-party router (despite "-5G" in name, this BSSID is 2.4) |
| cc:cc:cc:07:07:46 | Neighbour-ISP-B | 2.4 | 6 | -58 dBm | Neighbour's ISP-B router |
| bb:bb:bb:22:22:2b | (own) third-party hidden | 5G | **149** | -59 dBm | **OWN** U6-Pro's 5 GHz hidden SSID |
| aa:aa:aa:11:33:1b | (own) Omada hidden | 5G | 36 | -41 dBm | **OWN** planta-baja Omada hidden mgmt SSID |

Observations:
- **A printer (a nearby WiFi-Direct printer) is constantly broadcasting on 2.4 ch 6 at -51 dBm.** That's a meaningful contributor to ch 6 being the loudest 2.4 channel.
- **Multiple neighbour ISP routers** (ISP-A, Orange) on ch 6 confirm 2.4 GHz ch 6 is the local "everybody picks it" default.
- **Our own third-party broadcasts on 2.4 ch 1, 2.4 ch 11, AND 5 GHz ch 149** with multiple BSSIDs each (visible + hidden mgmt). This is normal multi-SSID/mesh behaviour.

## Always-present neighbours (by frequency seen)

These BSSIDs were observed in nearly every scan, i.e. they're 24/7 fixtures:

| BSSID | SSID | Observation count | Inference |
|---|---|---|---|
| cc:cc:cc:04:04:04 | Unknown-Random-SSID | 245 | A neighbour's static device |
| cc:cc:cc:03:03:03 | Neighbour-Printer-WiFi-Direct | 245 | Printer (ours? a neighbour's?) |
| cc:cc:cc:06:06:24 | Neighbour-Router-C | 238 | third-party router (neighbour) |
| aa:aa:aa:11:11:1a | <own-ssid> | 202 | **Own** Omada planta-baja 2.4 |
| cc:cc:cc:06:06:25 | Neighbour-Router-C | 193 | Same third-party router 5 GHz |
| cc:cc:cc:05:05:05 | Neighbour-ISP-A | 192 | Neighbour ISP-A |

**Note**: If the Epson printer at -51 dBm on ch 6 is OURS (worth checking), moving it to wired or disabling its WiFi Direct would directly clean up ch 6.

## Time-of-day patterns

Mean AP count fluctuates only mildly across 24h (range 2.4-3.7), suggesting most neighbour APs are 24/7 fixtures rather than work-hour-only devices:

| UTC hour | Local hour | Mean APs visible |
|---|---|---|
| 09:00 | 11:00 | 2.4 (lowest) |
| 11:00 | 13:00 | 2.7 |
| 13:00 | 15:00 | 3.3 |
| 19:00 | 21:00 | 3.7 (highest — evening peak when people are home) |

The slight dip during work hours (UTC 09-15) is the inverse of expectation — possibly because devices get carried away from the building during the day (people taking phones to lunch/meetings) and return in the evening.

For our specific worst channel (2.4 ch 6, strongest signal):
- Overnight & morning: -32 to -42 dBm (the printer + always-on neighbours)
- Evening (UTC 18-22): -46 to -52 dBm (slightly quieter)

So **ch 6 is consistently loud all day**, not just during work hours. The printer is the most likely culprit.

## Practical recommendations

1. **Keep planta-baja 5 GHz on ch 44** (our current choice) for non-DFS simplicity. It's a reasonable middle: many APs but all weak.
2. **If 5 GHz contention recurs**, try **ch 60 or 108** before any hardware change — they're nearly empty (DFS but very low radar exposure in this band range).
3. **Move planta-baja 2.4 GHz from ch 6 → ch 1** if any client still needs 2.4 GHz on the Omada side. Ch 1 has ~3× fewer neighbours and ~14 dB weaker signal levels.
4. **Investigate the a nearby WiFi-Direct printer printer's WiFi Direct**. It's constantly broadcasting on ch 6 at -51 dBm and is one of the louder always-on contributors. If it's our printer, disable WiFi Direct or move it to wired.
5. **<own-ssid-secondary> on ch 149** is on a moderate channel — not the cleanest but acceptable. Cleaner DFS alternatives exist if third-party needs them later.

## When to re-enable continuous airspace monitoring

This baseline is good for ~3-6 months unless:

- **New tenant moves in** to a neighbouring unit (could add new APs nearby)
- **A specific incident** shows the morning-storm signature (gateway + mesh + all DNS simultaneously) — that suggests a new RF environmental factor and warrants fresh scanning
- **You change AP placement or channels** and want to verify the choice was good
- **Once a quarter** as a sanity check

Re-enable via the `monitor.enabled: true` flag on the active network profile in `netmon.yml`. Run for ~24-48 hours, regenerate this doc with new findings, then turn it off again.
