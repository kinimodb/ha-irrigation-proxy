# CONTEXT.md – Technischer Kontext

## Hardware-Setup

- 5-6x Sonoff SWV Zigbee Smart Water Valves
- Zigbee-Stack: Zigbee2MQTT (Migration von ZHA geplant/durchgeführt)
- Sonoff SWV exponiert via Z2M:
  - `switch.sonoff_swv_*` (on/off)
  - `sensor.sonoff_swv_*_flow_rate` (Durchfluss m³/h)
  - `sensor.sonoff_swv_*_battery` (Batterie %)
  - `binary_sensor.sonoff_swv_*_water_leak` (Leck)
  - `binary_sensor.sonoff_swv_*_water_shortage` (Wassermangel)
  - Cyclic irrigation features via MQTT (nur Z2M, nicht ZHA!)

## Warum Zigbee2MQTT statt ZHA

ZHA exponiert beim SWV nur basic on/off.
Z2M exponiert den vollen Cluster inkl. cyclic_timed_irrigation,
cyclic_quantitative_irrigation, auto_close_when_water_shortage und
Flow-Rate. Für diese Integration ist Z2M zwingend erforderlich.

## Standort

Reilingen, Baden-Württemberg, Deutschland
- Latitude: ~49.30
- Longitude: ~8.56
- Klimazone: Cfb (gemäßigt ozeanisch)
- Open-Meteo liefert ET₀ direkt für diese Koordinaten

## Bewässerungssaison

- April bis Oktober (konfigurierbar)
- Hauptsaison: Mai bis September
- Morgendliche Bewässerung bevorzugt (4:00-7:00)
- Neuer Rollrasen erfordert intensive Anfangsbewässerung

## Bekannte SWV-Eigenheiten

1. SWV ist EndDevice (kein Router) – braucht gutes Zigbee-Mesh
2. Batteriebetrieben – sparsam mit Commands umgehen
3. Firmware 1.0.04+ nötig für auto_close Feature
4. Flow-Messung ~1L Offset (bekannt, akzeptabel)
5. Vertikale Installation erforderlich für korrekte Flow-Messung
