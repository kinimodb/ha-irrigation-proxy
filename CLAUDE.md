# CLAUDE.md – Anweisungen für Claude Code

## Projekt

ha-irrigation-proxy ist eine Home Assistant Custom Integration (HACS) für
intelligente Bewässerungssteuerung mit Sonoff SWV Zigbee-Ventilen.

## Architektur-Prinzipien

1. **Zuverlässigkeit über Features** – Lieber 5 Dinge die immer funktionieren
   als 50 die manchmal buggy sind.
2. **State-Verification** – Nach JEDEM Ventil-Schaltvorgang den tatsächlichen
   State prüfen. Das ist der #1 Bug in bestehenden Integrationen.
3. **Fail-Safe** – Deadman-Timer auf JEDEM Ventil. Bei Zweifel: Ventil ZU.
4. **Separation of Concerns** – Sequencer, Weather, Safety sind unabhängige
   Module. Der Coordinator orchestriert.
5. **Keine YAML-Config** – Alles über Config Flow / Options Flow.

## Code-Stil

- Python 3.12+, Type Hints überall
- async/await konsequent (HA ist async)
- Logging: _LOGGER = logging.getLogger(__name__)
- Deutsch in Kommentaren ist OK, Code/Variablen englisch
- Kein `hass.states.get()` in Loops – immer über Coordinator-Cache

## Referenz-Repo

ha-tadox-proxy (gleicher Entwickler) als Vorlage für:
- Config Flow Pattern
- Entity-Registration
- Coordinator-Pattern
- Options Flow
- Translations-Struktur

## Kritische Regeln

1. NIEMALS ein Ventil öffnen ohne Deadman-Timer
2. IMMER State-Verification nach switch.turn_on / turn_off
3. Bei HA-Restart: ALLE Ventile sofort schließen (async_will_remove_from_hass)
4. Open-Meteo API: max 1 Request pro 30 Minuten
5. Sequencer: EINE Zone gleichzeitig (default), konfigurierbar
6. Alle Zeitangaben in Minuten (User-facing) / Sekunden (intern)

## Test-Strategie

- Unit-Tests für Sequencer, Weather, Safety (ohne HA)
- Integration-Tests mit pytest-homeassistant-custom-component
- Mock: Ventil-Entities als einfache State-Switches
- Mock: Open-Meteo API Response

## Dateien die du NICHT anfassen sollst

- .gitignore (ist gesetzt)
- LICENSE (MIT, ist gesetzt)
- hacs.json (ist gesetzt)
