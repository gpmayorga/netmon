# Prompt: Sistema de Monitorización de Red para Coworking

## Contexto

Necesito desarrollar un sistema de monitorización de red completo para un coworking en Madrid con problemas intermitentes de conectividad. Los usuarios reportan microcortes de 2-3 segundos en videollamadas (Zoom, Google Meet) que no se pueden reproducir bajo demanda.

### Hardware de red existente
- **Router/Gateway**: TP-Link Omada ER706W-4G
- **APs**: 2x Omada EAPs en mesh (backhaul 5GHz compartido con clientes)
- **WANs**: 
  - Movistar fibra (primary, peso 4)
  - Orange fibra (secondary, peso 2)  
  - LTE 4G (backup solo si fallan ambas)
- **Espacio**: ~200m² en 2 plantas
- **Usuarios**: 10-30 simultáneos

### Problema a diagnosticar
- Microcortes de 2-3 segundos en videollamadas
- Intermitente, no reproducible
- Posibles causas: jitter/packet loss en una WAN, salto entre WANs, interferencias WiFi, saturación, mesh backhaul inestable

---

## Hardware disponible para monitorización

- Raspberry Pi 4 (4GB RAM) o Raspberry Pi 5
- Tarjeta SD 64GB+
- Alimentación oficial
- Cable Ethernet (conexión al router)
- 2x Adaptadores WiFi USB:
  - Uno para conectarse como cliente a la red
  - Otro con soporte monitor mode para escaneo de canales (chipset compatible, ej: Alfa AWUS036ACH o similar con RTL8812AU)
- Acceso remoto via Tailscale (ya configurado en la red)

---

## Requisitos funcionales

### 1. Monitorización de conectividad WAN/Internet

**Ping continuo (cada 10-30 segundos):**
- Gateway local (192.168.0.1)
- DNS públicos: 8.8.8.8, 1.1.1.1
- Servidores de videollamada:
  - meet.google.com
  - zoom.us
  - Servidores STUN/TURN comunes

**Métricas a registrar:**
- Latencia (min, avg, max, p95, p99)
- Jitter (variación de latencia)
- Packet loss (%)
- Timestamps exactos de cualquier pérdida o latencia >100ms

**Test de velocidad (cada hora):**
- Download/Upload speed
- Registrar con timestamp
- Usar speedtest-cli o similar

**Detección de cambio de IP pública (cada minuto):**
- Registrar IP pública actual
- Alertar/loguear cuando cambie (indica posible salto de WAN o failover)

### 2. Monitorización WiFi

**Como cliente conectado (antena 1):**
- Conectarse a la misma SSID que usan los usuarios
- Medir señal (RSSI), ruido, link speed
- Detectar desconexiones o roaming entre APs

**Escaneo de canales (antena 2, modo monitor):**
- Escaneo periódico (cada 5-10 minutos)
- Detectar:
  - Canales más congestionados
  - Número de redes vecinas por canal
  - Interferencias (mismo canal que nuestros APs)
- Registrar histórico para ver patrones

### 3. Integración con Omada (solo métodos estables)

**Syslog:**
- Configurar router Omada para enviar logs a la Raspberry (rsyslog/syslog-ng)
- Parsear y almacenar eventos relevantes:
  - Failover WAN
  - Cliente conectado/desconectado
  - Errores de autenticación
  - Cambios de estado de APs

**SNMP (si está disponible):**
- Polling de métricas básicas del router
- Tráfico por interfaz WAN
- Estado de interfaces
- Clientes conectados

**NO usar:**
- APIs no documentadas
- Scraping de la web UI
- Cualquier cosa experimental

### 4. Simulación de carga de videollamadas

**Objetivo:** Reproducir condiciones de uso real para detectar problemas.

**Especificaciones:**
- Simular tráfico UDP tipo WebRTC/RTP
- Equivalente a 30+ dispositivos con 3-5 videollamadas simultáneas
- Streams bidireccionales (upload + download)
- Bitrates realistas:
  - Video HD: ~2-4 Mbps por stream
  - Audio: ~50-100 kbps por stream
- Medir calidad durante la simulación:
  - Packet loss
  - Jitter
  - Latencia

**Programación:**
- Ejecutar automáticamente de noche (ej: 02:00-04:00)
- 2-3 veces por semana
- Generar informe de cada sesión

**Herramientas sugeridas:**
- iperf3 (UDP mode)
- Custom scripts con ffmpeg/gstreamer
- O cualquier herramienta que simule tráfico RTP/WebRTC realista

### 5. Dashboard y visualización

**Stack sugerido:**
- Prometheus + Grafana, o
- InfluxDB + Grafana, o
- VictoriaMetrics + Grafana (más ligero)

**Acceso:**
- Solo via Tailscale (sin autenticación adicional)
- Puerto estándar de Grafana (3000)

**Dashboards requeridos:**

**Dashboard 1: Overview en tiempo real**
- Estado actual: ✅ OK / ⚠️ Degradado / ❌ Caído
- Latencia actual a cada destino
- Packet loss últimas 24h
- IP pública actual
- Velocidad último test

**Dashboard 2: Histórico de calidad WAN**
- Gráfico de latencia últimos 7/30 días
- Gráfico de packet loss
- Gráfico de jitter
- Eventos de cambio de IP pública marcados
- Correlación con timestamps de quejas (anotaciones manuales)

**Dashboard 3: WiFi**
- Señal del cliente de prueba
- Mapa de canales congestionados
- Histórico de interferencias

**Dashboard 4: Eventos/Logs**
- Timeline de eventos Omada (syslog)
- Filtrable por tipo de evento
- Búsqueda por rango de tiempo

**Dashboard 5: Tests de carga**
- Resultados de simulaciones nocturnas
- Comparativa entre sesiones
- Detección de degradación

### 6. Sistema de alertas (en dashboard)

**NO enviar notificaciones externas** (no Telegram, no email).

**Mostrar en dashboard:**
- Panel de alertas activas
- Histórico de alertas
- Severidad: Info / Warning / Critical

**Condiciones de alerta:**
- Packet loss > 1% durante 5 minutos
- Latencia > 100ms durante 5 minutos
- Jitter > 30ms durante 5 minutos
- Cambio de IP pública
- Test de velocidad < 50% del esperado
- Pérdida de conectividad a gateway
- WiFi: señal < -75dBm
- WiFi: desconexión del cliente de prueba

### 7. Almacenamiento y retención

- **Métricas de alta frecuencia (pings):** 30 días con resolución completa
- **Métricas horarias (speedtest):** 90 días
- **Logs Omada:** 30 días
- **Datos de simulación:** 90 días

**Espacio estimado:** Asegurar que cabe en SD de 64GB con margen, o recomendar SSD USB si hace falta.

---

## Entregables esperados

### 1. Script de instalación automatizado
```bash
curl -sSL https://... | bash
# o
git clone ... && cd ... && ./install.sh
```

Debe:
- Instalar todas las dependencias
- Configurar servicios systemd
- Configurar Prometheus/InfluxDB/Grafana
- Importar dashboards predefinidos
- Configurar rsyslog para recibir logs
- Crear usuario y permisos adecuados

### 2. Archivos de configuración
- prometheus.yml o equivalente
- Configuración de Grafana (datasources, dashboards JSON)
- Scripts de monitorización
- Configuración de rsyslog
- Crontabs para tareas periódicas

### 3. Scripts de monitorización
- ping_monitor.py/sh — pings continuos
- speedtest_monitor.py/sh — tests de velocidad
- wifi_scanner.py/sh — escaneo de canales
- ip_checker.py/sh — detección de cambio IP
- load_simulator.py/sh — simulación de videollamadas

### 4. Dashboards Grafana
- JSON exportables
- Documentados con explicación de cada panel

### 5. Documentación
- README con instrucciones de instalación
- Guía de uso del dashboard
- Troubleshooting común
- Cómo añadir anotaciones manuales (para marcar "queja a las 12:34")
- Cómo interpretar los datos

---

## Configuración inicial requerida

El sistema debe pedir/configurar al inicio:

1. **Red WiFi del coworking:**
   - SSID
   - Password
   
2. **IPs de la red:**
   - Gateway (default: 192.168.0.1)
   - Rango DHCP esperado
   
3. **Credenciales SNMP (si se usa):**
   - Community string
   
4. **Configuración Omada syslog:**
   - Instrucciones para configurar en Omada que envíe logs a la IP de la Raspberry

5. **Horario de tests de carga:**
   - Hora de inicio (default: 02:00)
   - Días de la semana (default: Lunes, Miércoles, Viernes)

---

## Restricciones técnicas

- **Debe funcionar 24/7** sin intervención manual
- **Bajo consumo de recursos** — dejar margen para la RPi
- **Recuperación automática** — si un servicio falla, systemd debe reiniciarlo
- **Sin dependencias de cloud** — todo local excepto los pings externos
- **Seguridad:** 
  - No exponer puertos a internet (solo Tailscale)
  - No almacenar credenciales en texto plano (usar variables de entorno o secrets)

---

## Información adicional

### Contexto del problema actual

Los usuarios del coworking reportan:
- Cortes de 2-3 segundos en Google Meet y Zoom
- "Imagen congelada", "voz entrecortada"
- Ocurre en phonebooths y planta de arriba
- Intermitente, no reproducible bajo demanda
- Ha habido reseñas negativas por este problema

### Lo que ya se ha probado
- Instalación de 2 APs Omada (mejoró cobertura)
- Priorizar Movistar sobre Orange (4:2) — no resolvió
- Límite de ancho de banda por usuario (200/100 Mbps)
- Ajustar detección de failover a 6 minutos

### Hipótesis pendientes de validar
1. Una de las WANs (Movistar u Orange) tiene jitter/packet loss intermitente
2. El mesh backhaul WiFi entre APs fluctúa
3. Interferencias WiFi en canales congestionados
4. Saturación puntual de upload por algún usuario

**El objetivo de este sistema es tener datos objetivos para confirmar o descartar estas hipótesis.**

---

## Output esperado

Cuando el sistema esté funcionando, debería poder responder preguntas como:

- "¿A las 11:47 hubo algún problema de red?" → Mirar dashboard, ver si hay spike de latencia/packet loss
- "¿Es Orange o Movistar el problema?" → Comparar métricas cuando se detecta cambio de IP
- "¿El WiFi está congestionado?" → Ver escaneo de canales
- "¿Aguanta la red 30 usuarios en videollamada?" → Ver resultados de simulación nocturna
- "¿Cuándo fue el último incidente?" → Timeline de alertas

---

## Notas para el desarrollo

- Preferir soluciones simples y mantenibles sobre complejas
- Comentar el código
- Usar Python 3.9+ para scripts
- Seguir mejores prácticas de systemd para servicios
- Logs estructurados (JSON preferible) para facilitar parsing
- Testear en RPi antes de entregar (no solo en x86)
