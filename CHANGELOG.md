# Changelog

All notable changes to the Irrigation Proxy integration are documented in
this file. See the Release Process section in `CLAUDE.md` for the rules
that govern every entry.

## v0.7.2 — 2026-04-19

### Changed
- Disable `Inter-Zone Delay`, `Depressurize Delay` and `Max Runtime`
  number entities by default. These are install-time / safety values
  that rarely need daily tuning, so they no longer clutter the
  auto-generated dashboard. Edit them through the options flow, or
  re-enable the entity in the registry if you want a dashboard knob.
- Per-zone `Zone Duration` number entities remain enabled by default
  as the one number intended for regular tweaking.

## v0.7.1 — 2026-04-19

### Fixed
- Remove leftover `sensor.<zone>_duration` entities that stayed as
  `unavailable` in the entity registry after upgrading to v0.7.0. The
  integration now purges these stale IDs on setup; adjust per-zone
  runtime via the `number.<zone>_duration` entity instead.

## v0.7.0 — 2026-04-19

### Added
- New **Master Valve** switch entity that lets the user manually open and
  close the configured master/pump valve from the dashboard for
  maintenance, line-bleeding or testing. The switch is only registered
  when a master valve is configured. Manual opens arm an independent
  master deadman timer (same budget as the per-zone deadman) so a
  forgotten test cannot leave the line under pressure.
- New **Depressurize Delay** number entity – live-tunable from the
  dashboard, persists to the config entry. Mirrors the value previously
  only reachable through the options flow.
- New sensor **Depressurize Remaining** (seconds left in the current
  master-valve drain phase, `unknown` outside of it).
- New sensor **Pause Remaining** (seconds left in the current inter-zone
  pause, `unknown` outside of it).
- `Program Status` sensor now distinguishes the four phases
  `idle`, `running`, `depressurizing`, `pausing` so dashboards and
  automations can react to the exact phase instead of guessing from
  remaining-second sensors.

### Changed
- All number entities now use HA's `BOX` input mode (precise number
  entry) instead of the slider. Easier to dial in exact values like
  `17` minutes.
- Number entity changes (zone duration, inter-zone delay, max runtime,
  depressurize delay) now **persist** to the config entry, so
  dashboard tweaks survive a Home Assistant restart. The persistence
  path skips the usual options-flow reload so it does not abort a
  running program.
- Zone countdown (`Zone Time Remaining`) now starts exactly when the
  zone's water-flow sleep starts, instead of when we issue the
  `turn_on` service call. Previously the Zigbee verify window (up to
  5 s for the zone valve plus up to 5 s for the master valve) was
  silently subtracted from the displayed countdown, so a configured
  3 min zone could appear to drop to 2:50 before water actually
  flowed. The countdown now matches the actual irrigation time and
  returns to `unknown` during depressurize and pause phases.
- Options flow `Advanced` step description clarifies that the values
  are live-tunable as Number entities and that the depressurize delay
  is *added* to the program's total runtime, never deducted from a
  zone's configured duration.

### Removed
- **BREAKING:** `sensor.<program>_zone_duration` (read-only mirror of
  the per-zone duration) is gone. The same value is already exposed –
  and editable – as the `Zone Duration` Number entity, so the sensor
  was redundant. Dashboards or automations that referenced the sensor
  must switch to `number.<program>_<zone>_duration` (its `state` field
  carries the same minute value).

  **Migration:** delete the stale sensor reference from any Lovelace
  card / automation and replace it with the matching Number entity.

### Safety
- Master valve manual open arms a dedicated deadman timer; cancelled
  on manual close. The same `Max Runtime` budget applies as for zones
  so users have one safety knob.
- Manual master-valve toggling is refused while the program sequencer
  is running (raises a `HomeAssistantError` with a clear message). The
  sequencer owns the valve during a run.

## v0.6.14 — 2026-04-19

### Fixed
- `irrigation_proxy_program_aborted` (reason: "stopped") is now fired
  **after** the master valve and the active zone valve are confirmed
  closed and the sequencer is back in IDLE. Previously it fired at the
  very start of `stop()`, so automations reacting to the event would
  still see open valves and a running sequencer.
  The corrected sequence is: cancel task → close master → close zone →
  cancel deadman → reset to IDLE → fire ABORTED.

## v0.6.13 — 2026-04-19

### Changed
- `start_program` and `stop_program` services now accept an optional
  `entry_id` field. When supplied, the service targets only the matching
  config entry instead of broadcasting to every configured program.
  This is the correct behaviour when multiple irrigation programs are
  set up in one HA instance.
- Without `entry_id` the legacy broadcast behaviour is preserved for
  backwards compatibility, but now emits a `WARNING`-level log when
  more than one entry exists. The broadcast fallback will become a
  hard error in v0.7 — users with multiple programs should add
  `entry_id` to their automations/scripts now.
- Updated `services.yaml` with field description and UI selector for
  `entry_id` on both services.

## v0.6.12 — 2026-04-19

### Fixed
- `next_fire_time()` now uses `datetime.combine(date, t).astimezone()` instead
  of `datetime.combine(date, t, tzinfo=now.tzinfo)`. The old form attached the
  UTC offset of the *current* moment to every candidate date, producing a
  wrong wall-clock time for dates that cross a DST boundary. The actual
  scheduler trigger (via `async_track_time_change`) was unaffected; only
  the display value shown in the sensor/UI was wrong.

## v0.6.11 — 2026-04-19

### Safety
- When `_close_master()` exhausts all retry attempts without the master
  valve reporting closed, raise a persistent HA notification
  ("Irrigation Proxy: master valve stuck open") and fire a
  `irrigation_proxy_master_close_failed` bus event carrying
  `master_entity_id`, `attempts`, and `last_state`. Previously the
  failure was only logged at ERROR level, leaving water potentially
  flowing with no user-visible signal.
- `_close_master()` now returns `bool` (`True` = confirmed closed or no
  master configured, `False` = retries exhausted). Callers currently
  treat this as fire-and-forget; the event + notification serve as the
  user-facing signal.

## v0.6.10 — 2026-04-19

### Fixed
- `irrigation_proxy_program_completed` now reports the number of zones
  that actually ran the full open→wait→close cycle. Previously
  `zones_completed` was always equal to `total_zones` even when zones
  were skipped (valve verify failure, master valve open failure), so
  automations relying on that field were misled.
- The event payload also gains a `zones_skipped` field for symmetry, and
  `irrigation_proxy_zone_completed` continues to fire only for zones
  that genuinely completed.

### Changed
- When a program ends with **zero** zones actually completed (e.g. every
  zone was skipped), the sequencer now fires
  `irrigation_proxy_program_aborted` with `reason: "all_zones_skipped"`
  instead of `irrigation_proxy_program_completed`. The aborted event
  carries `zones_completed`, `zones_skipped` and `total_zones` so
  notifications can summarise what happened.

## v0.6.9 — 2026-04-19

### Safety
- Arm each zone's deadman timer **before** issuing the open service call
  instead of after state verification. Zigbee end-devices can acknowledge
  an open several seconds after the 5 s verify window times out; the
  pre-armed deadman now guarantees a safety net covers that race.
- When `Zone.turn_on()` reports a verify mismatch, the sequencer now
  defensively calls `Zone.force_close()` (which has its own retry + poll
  loop) before cancelling the deadman. This closes any valve that opens
  late due to Zigbee latency, instead of relying on the 30 s coordinator
  poll to spot the orphan.

### Fixed
- Eliminate the orphan-open-valve window between `turn_on` returning
  `False` and the next coordinator poll cycle.

## v0.6.8 — 2026-04-19

### Changed
- Restructure the options menu so the leak-sensor settings live in a
  dedicated "Safety" submenu instead of a single terminal form. The
  main menu entry is now just "Safety" (not "Safety (leak sensors)"),
  and the submenu currently exposes one entry — "Leak sensors" — so
  future safety features (e.g. flow-rate limits, emergency stop switch)
  can be added without reshuffling the top-level menu.
- Rename the advanced menu label from "Advanced (delays, safety)" to
  "Advanced (delays, limits)" now that the safety bits have their own
  top-level entry.
- Rewrite the leak-sensor form copy: add an explicit description of
  what the sensors do, how a detection propagates (stop program,
  force-close all valves, persistent notification, bus event), and a
  hint for the typical Sonoff SWV entity-id patterns. The field label
  is "Leak and water-shortage sensors", not the raw `leak_sensors` key.

### Safety
- No valve-path changes in this release. The emergency-shutdown
  behaviour introduced in v0.6.7 is unchanged; only the options-flow
  layout and translations are updated.

## v0.6.7 — 2026-04-19

### Added
- Add a `Safety – leak sensors` step to the options flow. Pick one or
  more `binary_sensor` entities (typically the Sonoff SWV
  `*_water_leak` and `*_water_shortage` sensors) that should trigger an
  immediate emergency shutdown.
- Fire `irrigation_proxy_leak_detected` on the HA bus whenever a leak
  emergency runs, with the triggering sensor's entity id and state.

### Safety
- Subscribe to the configured leak / water-shortage sensors at setup.
  When any of them transitions to `on` (including a sensor that is
  already `on` at integration startup), the coordinator stops the
  running program, force-closes every zone valve and explicitly closes
  the master valve, and raises a persistent notification. Previously a
  leak detected by the valve hardware was ignored and the program kept
  running.
- The handler is idempotent: concurrent state-change events for the
  same or other leak sensors are coalesced into a single shutdown run
  so retries never race against the close path.

## v0.6.6 — 2026-04-19

### Fixed
- Program Total Remaining now also accounts for the post-zone depressurize
  wait (when a master valve is configured). Previously the countdown
  dropped at the boundary between zone-end and depressurize and then
  dropped again at the start of the inter-zone pause, because neither
  phase was reflected in the total. The remaining-seconds estimate now
  includes every interval whose duration is known (zone runtime,
  depressurize, inter-zone pause); only zigbee command latencies remain
  unaccounted for.

### Added
- Expose `depressurize_remaining_seconds` and extend the `phase` field
  with a `depressurizing` value on the sequencer progress snapshot for
  diagnostics.

### Safety
- No valve-path changes in this release. The fix is limited to the
  countdown math and does not alter when or how zones / master valves
  open or close.

## v0.6.5 — 2026-04-19

### Fixed
- Program Total Remaining now counts down during the inter-zone delay
  instead of freezing and under-reporting the remaining runtime. The
  currently running pause is tracked explicitly and included in the
  remaining-seconds estimate.

### Added
- Expose `pause_remaining_seconds` and a `phase` field (`idle` / `running` /
  `pausing`) on the sequencer progress snapshot for diagnostics.

### Safety
- No valve-path changes in this release. The fix is limited to the
  countdown math and does not alter when or how zones / master valves open
  or close.

## v0.6.4 — 2026-04-19

### Changed
- Replace the blind 5 s `asyncio.sleep` after every valve switch with an
  event-loop-friendly state poll (0.2 s tick, 5 s cap). On every zone cycle
  the sequencer previously burned ~25 s of fixed waits (zone-open verify,
  master-open verify, master-close verify, depressurize, zone-close verify);
  it now returns as soon as the valve reports the new state, typically under
  1 s per switch. A 2-zone × 1 min program with a 15 s pause now finishes
  close to the expected ~135 s instead of ~185 s.

### Safety
- No change to the verification contract: every valve switch is still
  confirmed against the actual entity state, still retries on close, and
  still times out at 5 s per switch. Only the wait strategy changed from
  fixed sleep to polling.

## v0.6.3 — 2026-04-19

### Fixed
- Fix zone valve entity picker in the options flow: change the valve field from
  `vol.Required` to `vol.Optional` with `description={"suggested_value": ...}`,
  matching the pattern used by HA's frontend for entity selectors. Previously
  the entity picker rendered but submitted an empty value, triggering the
  "valve_required" error on every attempt. The fix aligns both the
  "Add zone" and "Edit zone" steps with the working master-valve field pattern.
- Fix `voluptuous.Optional` and `voluptuous.Invalid` missing from the test
  mock in `conftest.py`; all 119 unit tests now pass.

### Safety
- No valve-path changes in this release.

## v0.6.2 — 2026-04-16

### Fixed
- Fix HACS download failure ("No manifest.json file found
  'custom_components/None/manifest.json'"): add the required `codeowners` and
  `requirements` fields to `manifest.json`. HACS v2 requires both fields during
  repository indexing; without them HACS leaves `domain = None` internally and
  every subsequent install attempt fails with the above error.
- Lower `homeassistant` minimum version in `hacs.json` from `2024.1.0` to
  `2022.9.0`. The previous setting blocked installation for users on HA < 2024.1
  before the manifest was ever read, also leaving `domain = None`.

## v0.6.1 — 2026-04-16

### Fixed
- Fix zone valve selection in the options flow: the `EntitySelector` previously
  used `domain=["switch", "valve"]` (a list), which HA versions before 2022.9
  do not support. The domain restriction is now removed entirely so the picker
  accepts any entity type (`switch.*`, `valve.*`, or anything else) on all HA
  versions. Sonoff SWV via Zigbee2MQTT (`switch.sonoff_swv_*`) is unaffected.
- Fix zone name field rendered as `vol.Required` with an empty default; it is
  now `vol.Optional`, reflecting that the zone name is truly optional and
  preventing edge-case form-validation issues in certain HA versions.
- Add `DEBUG` log lines to `zone_add` and `zone_edit` step handlers so that
  the exact `user_input` arriving at the back-end is visible in HA logs when
  `custom_components.irrigation_proxy: debug` logging is enabled.

### Safety
- No valve-path changes in this release.

## v0.6.0 — 2026-04-16

### Added
- Add `Zone Duration`, `Inter-Zone Delay`, and `Max Runtime` as adjustable
  **number entities** on the dashboard (in-memory; config entry remains the
  persistent source).
- Fire HA bus events for the full program/zone lifecycle:
  `irrigation_proxy_program_started`, `_program_completed`, `_program_aborted`,
  `_zone_started`, `_zone_completed`, `_zone_error`.

### Fixed
- Fix zone valve selection in the options flow: the entity picker now includes
  both `switch` and `valve` domains, so Sonoff SWV and other modern Zigbee
  valves that register as `valve` entities can be selected.
- Fix all valve service calls to use the correct HA service domain:
  `switch.turn_on/turn_off` for switch entities,
  `valve.open_valve/close_valve` for valve entities.
- Fix state detection for valve entities: the "open" state is now recognised as
  "on" everywhere (coordinator, safety, zone model, switch entity).

## v0.5.1 — 2026-04-16

### Fixed
- Fix crash on startup when loading config entries created under v0.4.x.
  Old zone format (list of entity-ID strings) is now automatically
  migrated to the v0.5.0 dict format via HA config entry migration.
- Fix silent data corruption in the options flow when zones were still in
  the old string format (`dict()` on a string produced a char-indexed dict).

## v0.5.0 — 2026-04-15

### Added
- Add optional **master / pump valve** on the main supply line. Per zone
  the sequencer now opens the zone valve first, then the master, waits
  the configured duration, closes the master, waits a short
  depressurize delay and finally closes the zone – so no pressure rests
  on the lines between zones.
- Add configurable **depressurize delay** (seconds after closing the
  master before the zone valve is closed).
- Add a menu-based **options flow** (Irrigation v5 style): a single menu
  with Basics / Zones / Advanced / Save & Close, zones can be added,
  edited and removed individually.

### Changed
- Zones are now stored as an ordered list of `{id, name, valve_entity_id,
  duration_minutes}` dicts. Each zone owns its name + runtime; the
  previous `CONF_ZONE_DURATIONS` override map is gone.
- The config flow only asks for a program name; everything else is set
  up via the options menu afterwards.
- Scheduler simplified to schedule-enabled + start times + weekdays. No
  rain handling, no duration scaling.

### Removed
- Remove the Open-Meteo weather provider, the `Evapotranspiration`,
  `Water Need Factor` and `Rain Skip` entities, and the `rain_adjust_mode`
  option. These will come back in a future weather-aware release.
- Remove the `duration_multiplier` argument from `Sequencer.start()`.

### Safety
- Master valve closes BEFORE the zone valve on both normal completion
  and `stop()`/error paths, so zone hoses are drained before their valve
  shuts. Emergency shutdown on startup / HA stop also force-closes the
  master valve in addition to all zone valves.

**BREAKING:** The config-entry schema changed. Existing entries from
v0.4.x lose their weather/rain settings and their per-zone duration
overrides; zones need to be re-added via the new Zones menu. Delete and
re-add the integration if migration issues appear.

## v0.4.0 — 2026-04-14

### Added
- Add schedule editor to the config and options flows – configure multiple
  daily start times, active weekdays, inter-zone delay and an optional
  rain-based adjustment mode per program.
- Add per-zone duration configuration. Every zone can now have its own
  runtime in minutes; a new options step renders one slider per selected
  zone.
- Add `Zone Duration` sensor for every configured zone (duration in
  seconds) so the planned runtime is always visible, even when the program
  is idle.
- Add `Program Total Remaining` sensor reflecting the full program runtime
  while idle and the live remaining time across all pending zones while
  running.
- Add `Next Scheduled Start` timestamp sensor surfacing the upcoming
  automatic run.
- Expose the new `rain_adjust_mode` option (`off` / `hard` / `scale`) so
  rain can optionally skip or shorten scheduled runs without changing the
  semantics of the existing `Rain Skip` binary sensor.

### Changed
- Refresh timer sensors every second while the program is running instead
  of every 30 seconds. The coordinator keeps its 30-second poll cycle for
  external IO (Home Assistant states, Open-Meteo) and runs a dedicated
  in-memory 1 Hz ticker only while the sequencer is active.
- Zone switch entities now read their state directly from the underlying
  valve and are pushed by a state-change listener, so they flip within
  ~1 s of the real valve changing instead of waiting for the next poll.
- Program switch exposes richer attributes: total program remaining
  seconds, duration multiplier, inter-zone delay, per-zone planned
  durations and the next scheduled start.
- Inter-zone delay is now user-configurable (previously hardcoded to
  30 s).

### Fixed
- `Zone Time Remaining` sensor no longer reports `unknown` while idle.
  When no program is running it falls back to the configured duration of
  the first zone so the UI always shows a sensible value.

### Safety
- Sequencer still opens at most one zone at a time and arms a deadman
  timer per zone; the new duration multiplier never exceeds 1.0, so
  rain-aware runs can only shorten (never extend) the configured time.

## v0.3.0 — 2026-04-12

### Added
- Weather-aware smart irrigation. Open-Meteo provides ET₀, recent and
  forecast precipitation; a `Water Need Factor` sensor and a `Rain Skip`
  binary sensor are published and the combined threshold is configurable.
- New `start_program` and `stop_program` services so automations can
  trigger the sequencer without a UI click.

### Safety
- Open-Meteo calls are rate-limited to at most one request every 30 min.

## v0.2.0 — 2026-04-08

### Added
- Sequential zone irrigation. A new Sequencer runs zones one after the
  other with a configurable pause between them and reports progress via
  `Program Status`, `Current Zone` and `Zone Time Remaining` sensors.
- Program switch entity plus service registration for starting/stopping
  the sequencer.

### Safety
- Deadman timer now wraps every zone opened by the sequencer, and
  sequencer `stop()` closes the active zone before resetting state.

## v0.1.0 — 2026-04-05

### Added
- Initial HACS-compatible integration skeleton for Sonoff SWV valves:
  config flow, coordinator, per-zone switches with state verification,
  and unit tests for the safety-critical paths.

### Safety
- Safety manager with deadman timers, emergency-shutdown on setup and
  HA stop, and force-close retries on state mismatch.
