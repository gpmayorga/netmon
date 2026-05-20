1. Contexto del negocio
Eito Coworking

Ubicación: Chamberí, Madrid
Espacio: ~200m² en 2 plantas
Usuarios: 10-30 personas simultáneas
Uso principal: Trabajo remoto con muchas videollamadas (Zoom, Meet, Teams)

Precios del coworking

Day pass: €22
Half day: €16
x5 pass (1 mes): €90
x10 pass (1 mes): €170
x15 pass (45 días): €200
Ilimitado mensual: €240

Relación G ↔ Gonzalo

No hay contrato formal
Acuerdo informal: G tiene coworking gratis a cambio de ayuda con la red
G ha ido 10-12 días, va de media 4-6 veces al mes
Valor recibido: ~€250
Valor del trabajo realizado: ~€1.000-1.500 (20-25h a precio de mercado)


2. Problema original
Situación inicial (principios 2026)

Router de Telefónica básico, sin posibilidad de configuración
Una sola línea de internet (Movistar), sin backup
Cobertura WiFi insuficiente, especialmente en phonebooths y planta superior
Quejas frecuentes: "no hay internet", "va muy lento", "no conecta en los booths"

Qué se pidió a G

Mejorar la cobertura WiFi
Tener más control sobre la red
Que "funcionara bien"


3. Infraestructura instalada
Hardware actual
EquipoFunciónCoste aprox.TP-Link Omada ER706W-4GRouter/gateway con multi-WAN y gestión cloud~€2002× Omada EAP (Access Points)Cobertura WiFi planta baja (booths) y superior~€150-200SIM 4GBackup de emergenciaVariable
Inversión total en hardware: ~€350-400 (pagado por Gonzalo)
Conexiones WAN

WAN1: Movistar fibra (prioridad 4, ~67% del tráfico)
WAN2: Orange fibra (prioridad 2, ~33% del tráfico)
WAN3: LTE 4G (backup, solo si fallan ambas fibras)

Configuración implementada

Load balancing entre Movistar y Orange (ratio 4:2)
LTE como backup puro (failover cuando fallan ambas primarias)
Application Optimized Routing habilitado (sesiones sticky)
Límites de ancho de banda por usuario: 200 Mbps down / 100 Mbps up
Detección de línea mala: intervalo conservador de 6 minutos
Gestión remota via Omada Cloud
Mesh WiFi entre APs (backhaul 5GHz compartido con clientes)


4. Cronología del troubleshooting
Enero 2026 — Setup inicial

G investiga y recomienda equipamiento Omada
Problemas iniciales accediendo al router (192.168.0.1 no respondía)
Finalmente se configura router en standalone mode
Se migra a Omada Cloud para gestión remota (resetea config standalone)
Se configura todo de nuevo en el controller

Febrero-Abril 2026 — Problemas de cobertura

Usuarios se quejan de mala señal en booths y planta superior
Se prueban PLCs Devolo Magic 2 LAN → descartados por inestabilidad
Se instalan 2 APs Omada para cobertura completa
Cobertura mejora significativamente (señales de -58 a -68 dBm)

Mayo 2026 — Quejas de microcortes

Nuevas quejas: "videollamadas se cortan 2-3 segundos"
Problema intermitente, no reproducible bajo demanda
G ajusta configuración WAN: prioriza Movistar (4:2)
Se prueban varias teorías sin resultado concluyente

13-14 Mayo 2026

Gonzalo reporta quejas en phonebooths
G sugiere desconectar Orange como prueba
Orange se desconecta brevemente, "parece mejorar" pero no concluyente
G configura Movistar como primary (90%), Orange backup (10%), LTE emergencia
Ajusta detección de línea mala a 6 minutos

18 Mayo 2026

Gonzalo reporta "va muy lento todo"
G pone límite de 200/100 Mbps por usuario
Las quejas continúan

19 Mayo 2026 (hoy)

G envía mensaje explicando la situación
Plantea que no todas las quejas son culpa de la red
Propone sistema de monitorización o registro manual de quejas
Gonzalo responde "justo se acaban de quejar en la sala de reuniones"
G va a ir a verlo en persona


5. Estado técnico actual
Lo que funciona bien

✅ Cobertura WiFi completa en ambas plantas
✅ Redundancia triple (Movistar + Orange + 4G)
✅ Failover automático configurado
✅ Límites por usuario para evitar saturación
✅ Gestión remota via Omada Cloud
✅ Señales WiFi buenas (-58 a -68 dBm en la mayoría de clientes)

El problema actual

Quejas intermitentes de "videollamadas se cortan 2-3 segundos"
No reproducible bajo demanda
Ocurre en diferentes zonas (booths, planta arriba, "todos lados")
Sin patrón claro

Hipótesis pendientes de validar

Una de las WANs tiene jitter/packet loss — Movistar u Orange con problemas intermitentes
Mesh backhaul fluctúa — La misma radio 5GHz sirve clientes y backhaul
Interferencias WiFi — Canales congestionados, vecinos, etc.
Problemas externos — El otro lado de la llamada, servidores de Zoom/Meet
Dispositivos de usuarios — WiFi de portátiles mal configurados