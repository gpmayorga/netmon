# NetMon - Coworking Network Monitor

Sistema de monitorización de red para el coworking. Detecta microcortes, mide calidad de conexión, y genera datos objetivos para diagnosticar problemas de red.

## Quick Start

```bash
sudo bash /opt/netmon/install.sh
```

## Architecture

```
RPi (Debian/aarch64)
├── Python scripts (systemd services)
│   ├── ping_monitor.py      → Pings continuos (20s) a gateway, DNS, Zoom, Meet
│   ├── ip_checker.py         → Detección de cambio de IP pública (failover WAN)
│   ├── wifi_station.py       → Señal WiFi + métricas del sistema (CPU/mem/temp)
│   ├── wifi_scanner.py       → Escaneo de canales WiFi (wlan1 monitor mode)
│   ├── speedtest_runner.py   → Test de velocidad (cada hora)
│   ├── iperf3_simulator.py   → Simulación de carga (noches L/M/V)
│   ├── syslog_parser.py      → Receptor de syslog del router Omada
│   ├── snmp_poller.py        → SNMP polling (opcional)
│   └── router_monitor.py     → Estado WAN ports via SSH al router
├── Docker
│   ├── InfluxDB 2.7          → Base de datos de series temporales (:8086)
│   └── Grafana 11.x          → Dashboards y alertas (:3000)
└── Tailscale                  → Acceso remoto seguro
```

## Acceso a Grafana

**Via SSH tunnel** (recomendado):
```bash
ssh -L 3000:127.0.0.1:3000 pi@<TAILSCALE_IP>
# Abrir http://localhost:3000
```

**Via Tailscale directo** (requiere cambiar port binding en docker-compose.yml):
```yaml
# Cambiar la línea de Grafana de:
- "127.0.0.1:3000:3000"
# A:
- "<TAILSCALE_IP>:3000:3000"
```

Credenciales: Ver `/opt/netmon/config/secrets.env`

## Dashboards

| Dashboard | Descripción | Refresh |
|-----------|------------|---------|
| **Network Connectivity** | Estado general: latencia, packet loss, jitter, IP pública | 30s |
| **Bandwidth & Speedtest** | Velocidad download/upload, tests de carga iperf3 | 5m |
| **WiFi Health** | Señal, SNR, bitrate, canales congestionados, APs vecinas | 1m |
| **Router & Syslog** | Estado WAN ports, eventos syslog, tráfico SNMP | 1m |
| **System Health** | CPU, memoria, disco, temperatura del RPi | 30s |

## Configuración

### Archivo principal: `/opt/netmon/config/netmon.yml`

Contiene todos los parámetros configurables:
- Targets de ping y sus intervalos
- Servicios para detección de IP pública
- Interfaces WiFi
- Configuración de syslog
- Configuración de SNMP (deshabilitado por defecto)
- Programación de tests de carga
- Umbrales de alerta

### Secretos: `/opt/netmon/config/secrets.env`

Contiene tokens de InfluxDB, contraseñas de Grafana, y credenciales SSH del router.

### Monitorización del router Omada via SSH

1. Editar `/opt/netmon/config/secrets.env`:
```bash
ROUTER_SSH=user@192.168.0.1
ROUTER_SSH_PASSWORD=tu_contraseña
```

2. Reiniciar el servicio:
```bash
sudo systemctl restart netmon-router
```

3. Para auth por clave SSH (sin contraseña):
```bash
ssh-copy-id -o HostKeyAlgorithms=+ssh-rsa user@192.168.0.1
# Dejar ROUTER_SSH_PASSWORD vacío o comentado
```

### Configurar syslog desde Omada

En la interfaz de gestión del router Omada:
1. Ir a Settings > System > Syslog
2. Configurar Remote Syslog Server: `<IP del RPi>` Puerto: `5514`
3. Los logs aparecerán en el dashboard "Router & Syslog"

## Comandos útiles

```bash
# Ver estado de todos los servicios
systemctl status 'netmon-*'

# Ver logs en tiempo real
journalctl -u netmon-ping -f           # Pings
journalctl -u netmon-ipcheck -f        # IP checker
journalctl -u netmon-wifi-station -f   # WiFi
journalctl -u netmon-router -f         # Router SSH

# Forzar un speedtest ahora
sudo systemctl start netmon-speedtest.service

# Forzar un test de carga iperf3
sudo systemctl start netmon-iperf3.service

# Reiniciar todo
sudo systemctl restart 'netmon-*'

# Ver timers programados
systemctl list-timers 'netmon-*'

# Docker containers
cd /opt/netmon && docker compose ps
cd /opt/netmon && docker compose logs -f grafana
cd /opt/netmon && docker compose logs -f influxdb
```

## Anotaciones manuales en Grafana

Para marcar "hubo una queja a las 12:34":

1. Abrir cualquier dashboard en Grafana
2. Click en el punto de tiempo en el gráfico
3. Seleccionar "Add annotation"
4. Escribir la descripción (ej: "Usuario reportó corte en videollamada")
5. La anotación aparecerá como una línea vertical en todos los dashboards

## Alertas

Las alertas se muestran solo en Grafana (sin notificaciones externas):

| Alerta | Condición | Severidad |
|--------|-----------|-----------|
| Packet loss alto | >1% durante 5 min | Warning |
| Packet loss crítico | >5% durante 3 min | Critical |
| Latencia alta | >100ms durante 5 min | Warning |
| Jitter alto | >30ms durante 5 min | Warning |
| Cambio de IP pública | IP cambió | Warning |
| Speedtest bajo | <50 Mbps download | Warning |
| WiFi señal débil | <-75 dBm durante 5 min | Warning |
| RPi temperatura alta | >70°C | Warning |
| Servicio caído | Sin datos 5 min | Critical |

## Retención de datos

| Datos | Retención | Bucket |
|-------|-----------|--------|
| Pings, WiFi, syslog, sistema | 30 días | netmon |
| Speedtest, iperf3 | 90 días | netmon_speedtest |

## Troubleshooting

### InfluxDB no arranca
```bash
cd /opt/netmon && docker compose logs influxdb
# Verificar permisos del directorio data
ls -la /opt/netmon/data/influxdb/
```

### Grafana muestra "No data"
1. Verificar que InfluxDB tiene datos: `curl -s http://127.0.0.1:8086/health`
2. Verificar logs del script: `journalctl -u netmon-ping --since "5 min ago"`
3. Comprobar la datasource en Grafana > Settings > Data Sources > InfluxDB > Test

### WiFi scanner no funciona
```bash
# Verificar que wlan1 existe
iw dev
# Verificar que soporta monitor mode
iw phy phy2 info | grep monitor
# Probar manualmente
sudo iw dev wlan1 scan
```

### Router monitor sin datos
```bash
# Test manual SSH al router
ssh -o HostKeyAlgorithms=+ssh-rsa user@192.168.0.1 "show system-info"
# Ver logs
journalctl -u netmon-router -f
```

### Servicio falla repetidamente
```bash
# Ver por qué falla
journalctl -u netmon-ping --since "1 hour ago" --no-pager
# Reiniciar con más info
sudo systemctl restart netmon-ping
journalctl -u netmon-ping -f
```

## Estructura de archivos

```
/opt/netmon/
├── install.sh                    # Instalador
├── docker-compose.yml            # InfluxDB + Grafana
├── config/
│   ├── netmon.yml                # Configuración central
│   ├── secrets.env               # Tokens y contraseñas (0600)
│   ├── grafana/
│   │   ├── grafana.ini
│   │   ├── provisioning/
│   │   │   ├── datasources/influxdb.yml
│   │   │   └── dashboards/dashboards.yml
│   │   └── dashboards/
│   │       ├── 01-connectivity.json
│   │       ├── 02-speedtest.json
│   │       ├── 03-wifi.json
│   │       ├── 04-router.json
│   │       └── 05-system.json
│   └── rsyslog/60-netmon.conf
├── scripts/
│   ├── common.py                 # Utilidades compartidas
│   ├── ping_monitor.py
│   ├── ip_checker.py
│   ├── wifi_station.py
│   ├── wifi_scanner.py
│   ├── speedtest_runner.py
│   ├── iperf3_simulator.py
│   ├── syslog_parser.py
│   ├── snmp_poller.py
│   └── router_monitor.py
├── systemd/                      # Unit files
└── data/                         # Volúmenes Docker (InfluxDB, Grafana)
```
