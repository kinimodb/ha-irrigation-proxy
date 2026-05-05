# Irrigation Proxy

![Version](https://img.shields.io/badge/version-0.9.4-blue)
![HA](https://img.shields.io/badge/Home%20Assistant-2024.6%2B-41BDF5)
![License](https://img.shields.io/badge/license-MIT-green)

A Home Assistant custom component (HACS) for sequential irrigation
control. Drives any `switch.*` or `valve.*` entity, ships a built-in
scheduler, and enforces a per-zone deadman timer plus state verification
on every command so a stuck valve or a network glitch can never leave
the water running.

Tested daily with **Sonoff SWV** valves via Zigbee2MQTT and ZHA, but the
core watering loop has no vendor-specific code and works with any valve
exposed as a standard Home Assistant `switch` or `valve` entity.

---

## The Problem

Most DIY irrigation setups in Home Assistant are a stack of
`automation:` blocks that assume the happy path: turn the valve on, wait
N minutes, turn it off. They break in three predictable ways:

1. The valve doesn't actually open or close — `switch.turn_on`
   returns before the hardware confirms, and nobody verifies the result.
2. Home Assistant restarts mid-run and the automation forgets the valve
   is still on.
3. A leak sensor goes off and nothing in the watering chain reacts.

Irrigation Proxy is built around three rules that make those failures
impossible:

- **Every open valve has a deadman timer.** If anything goes wrong, the
  valve force-closes after the configured maximum runtime.
- **Every command is verified.** State is read back after every
  `turn_on` / `turn_off`; mismatches retry, then abort.
- **On any doubt, valves close.** Restart, integration unload, leak,
  sensor wobble — the default action is always to close everything.

---

## Prerequisites

- Home Assistant **2024.6** or newer
- [HACS](https://hacs.xyz) installed
- At least one valve as a `switch.*` or `valve.*` entity in HA
- Optional: one or more `binary_sensor.*` entities for leak / water-shortage detection
- Optional: one `sensor.*` returning a numeric weather factor (0.0–2.0) for runtime adjustment

---

## Installation

1. Open HACS → **Integrations** → menu (three dots, top right) → **Custom repositories**
2. Enter URL: `https://github.com/kinimodb/ha-irrigation-proxy`
3. Category: **Integration** → **Add**
4. Search for **Irrigation Proxy** in HACS and install
5. Restart Home Assistant
6. **Settings** → **Devices & Services** → **Add Integration** → *Irrigation Proxy*

---

## Configuration

Initial setup asks for one field:

| Field | Description |
|-------|-------------|
| Program name | Display name for this irrigation program (used as device name and entity prefix) |

Everything else is configured under **Settings → Devices & Services →
Irrigation Proxy → Configure**, which opens a menu:

| Menu entry | Covers |
|------------|--------|
| **Basics** | Schedule (start times, weekdays), optional master/pump valve |
| **Zones** | Add, edit and remove zones (the watering sequence) |
| **Advanced** | Inter-zone delay, depressurize delay, max runtime, weather factor sensor |
| **Safety** | Leak / water-shortage sensors |

Add the integration once per program (e.g. one entry for the lawn,
another for the flower beds). Each entry is fully independent.

### Zones

| Field | Description |
|-------|-------------|
| Zone name | Free-form (`Lawn`, `Hedge`, `Drip line south`) |
| Valve entity | Any `switch.*` or `valve.*` (e.g. `switch.sonoff_swv_xxxxxx`) |
| Duration (minutes) | Default runtime; also exposed as a `number.*` entity for live tuning |

Zones run in the order they are added. To re-order, edit a zone, tick
**Delete this zone**, save, then re-add it in the right place.

### Schedule

| Field | Description |
|-------|-------------|
| Enable schedule | Master toggle. Mirrored as `switch.<program>_schedule_enabled` |
| Start times | Comma-separated `HH:MM` list, e.g. `06:00, 20:30` |
| Active weekdays | Checkbox list (Mon–Sun) |

The program starts once at every `start time × weekday` combination.
`sensor.<program>_next_scheduled_start` always shows the upcoming start.

### Master / pump valve (optional)

| Field | Description |
|-------|-------------|
| Master / pump valve | Any `switch.*` or `valve.*` on the supply line |
| Depressurize delay | Seconds the master stays closed *before* the zone valve closes, so the line drains under pressure (default: 5s) |

When configured, the master opens *after* the zone valve and closes
*before* it.

### Advanced

| Parameter | Default | Description |
|-----------|---------|-------------|
| Delay between zones | 30s | Pause after a zone finishes before the next zone starts |
| Depressurize delay | 5s | See master valve above |
| Maximum runtime per zone | 60 min | **Deadman limit.** Force-closes the valve if it stays open longer. Don't disable. |
| Weather factor sensor | (none) | Optional `sensor.*` with a 0.0–2.0 factor multiplied into every zone duration at program start |

---

## Features

### Sequential Zone Runs

Zones run one at a time, with the configured inter-zone pause between
them. Within one program no two zones are ever open simultaneously —
this matches what most home water supplies can handle without losing
pressure.

### Built-in Scheduler

Configure start times and weekdays in the options menu. No
`automation:` YAML required. The **Automatic Schedule** switch
(`switch.<program>_schedule_enabled`) toggles the scheduler live without
interrupting a running program.

### Weather-Based Runtime Adjustment

Point **Weather factor sensor** at any `sensor.*` whose state is a
number between 0.0 and 2.0 (e.g. an ET₀-based template, a custom
rain-corrected helper). At program start the sequencer multiplies every
zone duration by that factor:

- `1.0` — run as configured
- `0.0` — skip the zone entirely (no valve open, no deadman)
- `2.0` — double the runtime (still hard-capped by the deadman)
- `unknown` / `unavailable` / not set — fall back to `1.0`

The factor is captured **once at program start** and stays constant for
the run. The **Ignore Weather Adjustment** switch
(`switch.<program>_ignore_weather_adjustment`) bypasses the factor when
on, useful for vacation prep or line flushes. State is persisted across
restarts.

### Leak / Water-Shortage Shutdown

In **Configure → Safety → Leak sensors**, pick one or more
`binary_sensor.*` entities. If any of them transitions to `on` while a
program is running:

1. The program is stopped immediately.
2. Every configured valve (zones + master) is force-closed.
3. A persistent notification is raised.
4. The `irrigation_proxy_leak_detected` event is fired with the
   triggering `entity_id` for your own automations.

For Sonoff SWV via Zigbee2MQTT, `binary_sensor.sonoff_swv_*_water_leak`
and `binary_sensor.sonoff_swv_*_water_shortage` are the right inputs.

### Manual Overrides Are Adopted, Not Fought

If a configured zone valve is opened directly — toggling the underlying
`switch.*`, pressing the physical button, a separate automation — the
proxy detects the orphan-open within at most 30s (and immediately on
the state-change event), arms a normal deadman timer on it, and lets
it run. The valve closes at the deadman (or whenever it is turned off).

This makes manual flow tests safe by default.

### State Verification

After every `switch.turn_on` / `turn_off` the proxy reads the actual
state back (200 ms poll, 5s timeout). Mismatches retry up to 3 times.
Persistent failures abort the program and fire
`irrigation_proxy_zone_error` (or
`irrigation_proxy_master_close_failed` for a stuck master valve).

### Restart Safety

On unload, the integration force-closes every valve it owns. After a
restart the program comes back up `Idle` — even if the underlying
valve was on. Combined with the deadman, this means a half-finished
program after a crash cannot leave a valve open longer than the
configured maximum.

---

## Entities

Each program creates one Home Assistant device with these entities
(prefix `<program_slug>` = the slugified program name).

### Switches

| Entity | Description |
|--------|-------------|
| `switch.<program>_program` | Manual start/stop for the whole sequence |
| `switch.<program>_schedule_enabled` | Automatic schedule on/off |
| `switch.<program>_ignore_weather_adjustment` | Bypass the weather factor |
| `switch.<program>_master_valve` | Manual control of the master valve (only with master configured) |
| `switch.<program>_<zone_slug>` | One per zone — manual control with proxy supervision |

### Sensors

| Entity | Description |
|--------|-------------|
| `sensor.<program>_program_status` | `idle`, `running`, `depressurizing`, or `pausing` |
| `sensor.<program>_current_zone` | Name of the active zone |
| `sensor.<program>_zone_time_remaining` | Active zone countdown, formatted `MM:SS` (`seconds_remaining` attr) |
| `sensor.<program>_program_total_remaining` | Whole-program countdown, formatted `MM:SS` (decomposed in attrs) |
| `sensor.<program>_pauses_total_remaining` | Total inter-zone pause time still ahead |
| `sensor.<program>_depressurize_total_remaining` | Total depressurize time still ahead |
| `sensor.<program>_next_scheduled_start` | Next planned start as a `datetime` |
| `sensor.<program>_weather_factor` | Currently cached factor + source attributes |

### Numbers

All `number.*` entities edit the same value as the options flow, but
live and from the dashboard. Per-zone duration is enabled by default;
the others (inter-zone delay, max runtime, depressurize delay) are
disabled by default and can be re-enabled in the entity registry.

| Entity | Description |
|--------|-------------|
| `number.<program>_<zone_slug>_duration` | One per zone (minutes) |
| `number.<program>_inter_zone_delay` | Seconds |
| `number.<program>_max_runtime` | Minutes |
| `number.<program>_depressurize_delay` | Seconds |

---

## Services

### `irrigation_proxy.start_program`

Starts the configured program. Runs all zones sequentially with their
configured durations. No-op if already running.

| Field | Type | Description |
|-------|------|-------------|
| `entry_id` | string (optional) | Config entry ID of the program to start. Find it under **Settings → Devices & Services → Irrigation Proxy → ⋮ → Entry ID**. Omitting it starts every program (deprecated). |

### `irrigation_proxy.stop_program`

Stops the running program. Closes the active valve and cancels all
remaining zones.

| Field | Type | Description |
|-------|------|-------------|
| `entry_id` | string (optional) | Same as above. |

---

## Events

All events are fired on the Home Assistant event bus and carry the
originating `entry_id`.

| Event | Payload extras |
|-------|----------------|
| `irrigation_proxy_program_started` | `weather_factor`, `weather_ignored` |
| `irrigation_proxy_program_completed` | — |
| `irrigation_proxy_program_aborted` | `reason` |
| `irrigation_proxy_zone_started` | `zone_name`, `base_duration_seconds`, `weather_factor`, `skipped` |
| `irrigation_proxy_zone_completed` | `zone_name` |
| `irrigation_proxy_zone_error` | failing `entity_id` |
| `irrigation_proxy_leak_detected` | `entity_id` |
| `irrigation_proxy_master_close_failed` | — |

---

## Known Limitations

- One zone at a time per program. Two programs can overlap.
- No built-in weather data; bring your own factor sensor.
- No drag-handle re-ordering of zones in the options UI yet.
- The integration card in **Settings → Devices & Services** uses the
  generic puzzle-piece icon. Brand assets for custom integrations live
  in the [home-assistant/brands](https://github.com/home-assistant/brands)
  repository under `custom_integrations/irrigation_proxy/`; until that
  PR is opened and merged, no custom logo is shown there. The
  `logo.png` in this repo is used by HACS and the README only.

---

## License

[MIT](LICENSE) © kinimodb
