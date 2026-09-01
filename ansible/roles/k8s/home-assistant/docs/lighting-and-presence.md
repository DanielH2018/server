# home-assistant — lighting and presence

Split out of the role's `CLAUDE.md` on 2026-08-15; the content is unchanged.
Presence detection, the adaptive-lighting stack, the single-guarded-writer mediator,
and the bedtime/wake routines that drive them.

## Presence, adaptive lighting and the mediator
- **Too-bright arrival blip (since 2026-06-19).** `automation.bedroom_presence_blip_too_bright` is a
  sibling of `bedroom_presence_on`: same arrival edge (`binary_sensor.aqara_fp300_presence` -> on),
  but the lux gate is **inverted** (`binary_sensor.bedroom_auto_light_allowed` == off) plus
  `manual_off` off, `person home`, and lights currently off. When you walk in but it's too bright to
  auto-light, it calls `script.bedroom_blip` (off -> 15% warm 2700K ~1s -> off) so you get an
  acknowledgement instead of silence. `bedroom_blip` is the inverse of `bedroom_alert_pulse` — it
  needs NO `scene.create` snapshot because it only runs with the lights already off, so a plain
  `turn_off` restores the known state. No feedback loop: it fires only at T1 illuminance >= 90 (bright
  ambient), and a ~1s blip can't satisfy `presence_on`'s `below: 90 for: 30s`. No cooldown initially;
  add a trigger `for:` debounce if presence flapping makes it chatty.
  **Tap Dial button-1 HOLD blips too (since 2026-06-20).** The "reset to auto" branch in
  `bedroom_tap_dial_control` calls the SAME `script.bedroom_blip` when its lux-gated apply
  (`script.bedroom_apply_natural_gated`) leaves the lights off — i.e. `bedroom_auto_light_allowed` ==
  off (too bright) AND the sun is up AND the lights were already off before the reset (a `was_off` snapshot
  taken before the apply, so a reset that turns an already-on light off doesn't double-up the visible
  feedback with a blip). Same too-bright acknowledgement as the arrival blip, but button-driven (not
  lux-driven), so there is no feedback-loop concern at all.
  **Sun-aware HOLD reset (since 2026-06-21).** `script.bedroom_apply_natural_gated` now lights when the
  lux gate allows **OR `sun.sun` is `below_horizon`** (no longer lux-only). Root cause it fixes: the FP300
  illuminance LAGS ~100 s after the bulbs switch off (sleepy `light_sampling: low` sensor — see the
  feedback-loop caveat above), so right after a manual/voice off the gate read "bright" and the HOLD reset
  just blipped; you had to HOLD a SECOND time once the sensor caught up (confirmed live via history:
  illuminance held 553 for ~100 s, lights came on only once it dropped < 50). At night a stale-bright
  reading is physically impossible, so sun-below-horizon makes one HOLD light reliably; by day live lux
  still rules (bright room stays off). Scoped to the HOLD path ONLY — the automatic `presence_on` gate is
  unchanged — and the HOLD blip condition above carries the EXACT complement (`... and sun above_horizon`)
  so the reset can't both light and blip. A `light_sampling: high` write to shrink the lag was attempted
  but did NOT land (sleepy-device downlink not accepted — the FP300 kept reporting `low`); the sun term is
  the deterministic, git-managed fix and doesn't depend on it.
  **`script.bedroom_blip` is the SINGLE source of truth for the blip flash** (off -> 15% warm 2700K ->
  off). Both the arrival automation AND this Button 1 HOLD branch reference that one script — never
  re-roll an inline flash — so the acknowledgement is identical by construction and can't drift. Any
  future "the lights stayed off on purpose, acknowledge it" feedback should call `script.bedroom_blip`
  too (it requires the lights already off — no snapshot; each caller must enforce that precondition).
  **Verification gotcha:** an automation's `entity_id` derives from its `alias` (slugified) at
  first creation, NOT its `id` — so `bedroom_fan_temperature` (id) is
  `automation.bedroom_fan_temperature_control` (alias) in the state machine / recorder DB. Query by
  the alias-slug, not the id, when checking whether an automation loaded.
  **FP300 presence tuning (2026-06-18, "lights off while sitting at the desk" fix):** the FP300 was
  dropping `presence` ~2 min while the operator sat still (187 flips/24h, 16 false-absences 1–5 min
  that crossed `bedroom_absence_off`'s 1-min timeout). Fixed via Z2M **device settings** (NOT
  templated — set with `mosquitto_pub -t 'zigbee2mqtt/Aqara FP300/set' -m '{...}'`; re-apply after a
  re-pair): `presence_detection_options: mmwave` (radar-only — holds a stationary person; PIR sees
  only motion), `motion_sensitivity: high`, `absence_delay_timer: 60` (sec; was 10, range 10–300 —
  the hold-vs-prompt knob). `bedroom_absence_off` was bumped to **5 min** (`for: 00:05:00`) for
  FP300 false-absence de-flap (was 1 min; the Z2M device-tuning alone wasn't enough to eliminate
  brief drops on a stationary person).
  **FP300 fan false-HOLD (2026-06-18, the mirror-image fix):** the above hold-harder tuning
  over-corrected — the high-sensitivity mmwave radar read the **running tower fan's** moving air as a
  permanent occupant, so `presence` stuck `true` in an empty room (15+ min observed) and
  `bedroom_absence_off` never fired → lights stayed on. Confirmed by experiment: fan OFF → `presence`
  cleared to `false` 72s later (= the 60s `absence_delay_timer`). Fixed via another Z2M **device
  setting** (same `mosquitto_pub .../Aqara FP300/set`, NOT templated, re-apply after a re-pair):
  `ai_interference_source_selfidentification: ON` — Aqara's purpose-built interference rejection;
  keeps `motion_sensitivity: high` so the desk-sitting fix survives. The dog is NOT a factor (an
  mmwave radar does see a pet as presence, but the room was confirmed pet-free during the incident).
- **Adaptive Lighting is a HACS dependency (since 2026-06-18).** `configuration.yaml` declares
  `adaptive_lighting:` for the bedroom group; the integration code installs via HACS into
  `custom_components/adaptive_lighting/` (on the config PVC, not templated — like `dreo`). Install it
  via HACS BEFORE deploying, or HA logs "integration not found" and skips the block. The deploy's
  full restart loads a newly added custom component (a YAML "Quick Reload" does not).
- **Light/fan mediator — single guarded writer (Phase 2, since 2026-06-22).** AUTO/programmatic
  writes of `light.bedroom_lights` go through `script.bedroom_lights_set(reason)` and of
  `fan.tower_fan` through `script.bedroom_fan_set(reason)`. The light gate is the tested
  `light_decision(reason, …)` macro in `lighting.jinja`: `presence` is GATED (manual_off/sleep/home/
  presence/lux/light-off — the conditions that used to live on `bedroom_presence_on`); `natural`/
  `wake`/`off` are pass-through (the caller pre-gates). The mediator DELEGATES to the existing
  primitives (`apply_natural`/`apply_wake`/`light.turn_off`) — it does not reimplement them.
  `bedroom_fan_set` is `auto`→`apply_fan` / `boost`→max+override (arms `expected_level=9`) /
  `off`→`fan.turn_off`. **The manual Tap Dial is a DECLARED EXEMPTION** (writes directly, by design —
  intentional/ungated, and its brightness dial is a latency-sensitive relative step), as are
  `apply_natural_gated`, `bedroom_blip`, `bedroom_alert_pulse`, `bedroom_color_tracking`, and
  `bedroom_fan_startup_reconcile`. The allowed writer set is enforced **HARD** by the
  `validate-ha-config` hook via `state/sanctioned_writers.yml` (module ∪ exemptions): a new automation
  that writes an actuator directly fails CI. **Add a writer = route it through the mediator, or
  declare it in `sanctioned_writers.yml`.** `reason: "off"` MUST stay quoted (unquoted `off` is YAML
  `false` → silent no-op). Design: `docs/superpowers/specs/2026-06-21-ha-state-model-phase2-mediator-design.md`.
  The mediator's `reason` is contract-checked by `validate-ha-config` (`mediator_reason_errors`
  in `ha_state_model.py`): every `bedroom_lights_set`/`bedroom_fan_set` call must pass a quoted
  `reason` from the declared vocabulary (`MEDIATOR_REASONS`) — a missing/typo'd reason or the
  unquoted-`off`→YAML-`false` no-op fails CI. Add a new reason to `MEDIATOR_REASONS` when you add
  one to the mediator.
- **`files/scripts/lighting.yaml` — the "natural lighting state" dispatcher (shipped verbatim, like
  automations/scenes; wired via `script: !include_dir_merge_named scripts/`; an edit rolls the pod).**
  `script.bedroom_apply_natural` sets the bedroom group to what it would be with no manual
  intervention RIGHT NOW: an ordered `choose:` of time-based **exceptions** (brightness overrides
  on AL's natural color) with **AL color + ambient-fill brightness (`natural_brightness(hour,
  illuminance)` macro) as `default:`**. AL is a color source at turn-on; the per-minute
  `automation.bedroom_color_tracking` (id `bedroom_color_track`) slow-drifts color toward AL every
  5 min while in auto so brightness sticks but color follows the sun (see item 5b in the brief).
  The FIRST exception is the night-time dim nightlight (`scene.bedroom_nightlight`) when
  `bedroom_sleep_mode` is on OR it's 00:00–05:00 — so a presence re-trigger doesn't blast
  you (it wins over the wake ramp; at wake time sleep_mode is cleared and hour≥5, so it's false).
  **Note (since 2026-06-20):** while `sleep_mode` is on, `bedroom_presence_on` is GATED OFF entirely
  (see its conditions), so there is NO automatic "got up overnight" nightlight — getting up leaves
  the room dark and a B2 tap brings the nightlight back. The sleep-mode arm of this exception is thus
  reached via `presence_on` only in the 00:00–05:00 *no-sleep-mode* case (up late, not yet in bed);
  it still protects the B2-tap nightlight and any other direct caller of the dispatcher.
  The morning wake ramp is the next exception. It spans a **45-min window** (`alarm−15` →
  `alarm+30`, so the alarm sits 1/3 in — NOT centered), with a gentle-then-steep three-segment curve:
  1% at window start → ~8% at the alarm → 20% at `alarm+20` (the knee) → **100% at `alarm+30`**.
  The curve deliberately STAYS DIM through the alarm and the ~20 min after (a soft, non-jarring
  wake), then does the steep climb in the final 10 min (reshaped 2026-07-05 after "too bright at the
  alarm" — the old curve hit ~12%/40% earlier and reached 40% by `alarm+15`). Ramping all the way to
  100% by window end is still deliberate (since 2026-06-29): the old pre-100% curve stopped at 40%
  then `bedroom_wake_ramp`'s hand-off to Adaptive Lighting popped the room to its full daytime
  target all at once. Now AL takes over at 100%, so the hand-off has no upward jump — it just keeps
  getting gradually brighter. `sensor.bedroom_wake_start` = alarm−15 min (the window open edge;
  dynamic — see the dynamic-wake bullet below). Delegated entirely to `script.bedroom_apply_wake`
  (fixed warm 2200K, no `adaptive_lighting.apply` turn-on flash), driven per-minute by
  `automation.bedroom_wake_ramp`. Short night (<6h) scales the mid/knee down to ~5/14% but still
  reaches 100% by `alarm+30` (else the hand-off pop returns on short nights — see the sleep-quality
  bullet). Pressing button 4 mid-window resumes the ramp from the current point (`bedroom_apply_wake`
  recomputes the right frame for now()). When no morning alarm is set the sensor is `unavailable` →
  this exception is false and the default applies. `wake_transition` macro is gone — transition is
  now always 60 s (one ramp step). **`bedroom_morning_reset` also calls this dispatcher** (single
  source of truth — no duplicated ramp math). Color temp ALWAYS comes from AL; exceptions override
  brightness only (except the wake exception, which uses fixed 2200K).
  Helper `script.bedroom_set_natural_brightness(brightness_pct, transition)` holds the AL
  release + color-apply boilerplate so a new exception is just a `(condition, brightness,
  transition)` triple dropped above `default:` — see the worked example comment in the file. It
  also arms `input_number.bedroom_light_expected_color_temp` (the color tracker's "auto" baseline)
  so `automation.bedroom_color_tracking` (id `bedroom_color_track`) knows what color it last set
  and can drift from there without treating it as a manual override.

- **Aqara T1 ambient light sensor (added 2026-08-31), `sensor.aqara_t1_illuminance`.** A dedicated
  illuminance sensor mounted near the desk, clear of the bedroom bulbs' cones, to give the room a
  reading the lights cannot contaminate — the defect the FP300 feedback-loop caveat above describes.
  **The lux gate runs on it as of 2026-09-01, at `< 90` lux** — see *Picking the 90-lux threshold*
  below. This entry records the hardware and the install-day measurements.
  Measured live on install day with the sensor stationary: switching `light.bedroom_lights` moved the
  T1 by 17-30 lx (49 dark ↔ 79 lit) while it moved the FP300 by ~550 lx (9 dark ↔ 561 lit) — a 23:1
  reduction in bulb bleed. True ambient with both the bulbs and the desk lamp off read 57 lx at 08:11
  local. The presence-triggered desk lamp beside it was measured and is NOT a contaminant: switching it
  off did not lower the reading (49 → 57 over six minutes, morning daylight rising). That mattered
  because the lamp fires on presence, so a contribution there would have biased the gate at exactly the
  moment it is evaluated — the FP300's failure mode with occupancy as the trigger instead of the bulbs.
  Daylight drift over the same window bounds the magnitude rather than pinning it, so the claim is "not
  a dominant source", not "exactly zero".
- **Picking the 90-lux threshold (2026-09-01).** The gate moved from `sensor.aqara_fp300_illuminance < 75`
  to `sensor.aqara_t1_illuminance < 90`, in `auto_light_allowed` (lighting.jinja), the
  `bedroom_auto_light_allowed` template sensor, and `bedroom_presence_on`'s dusk trigger.
  **The method matters more than the number, because the obvious method is wrong.** Scoring candidate
  thresholds by how well they agree with the OLD gate reproduces the defect: `FP300 >= 75` is mostly
  "the lights are on", not "the room is bright", so agreement with it rewards bulb contamination. Scored
  that way, agreement peaked at 40 lux and fell monotonically upward — an artifact of the contaminated
  reference, not a calibration.
  The uncontaminated method is to pair the T1 series against `light.bedroom_lights` history and keep only
  the samples taken while the lights were OFF. Over 2026-08-31 13:01 UTC to 2026-09-01 12:09 UTC that
  left **43 of 278** samples. On that subset, evening and night ambient read 1-33 lux (hours 15, 18, 01
  local) while midday read 134-264 (hours 10-11), leaving a wide empty gap. 90 sits in that gap, and
  clears the 49-79 bulb-bleed band the entry above requires.
  **The number is a first cut and the sample is thin** — 23 h, one weather pattern, and only one
  lights-off night sample. Expect to retune it once the sensor has a few days of recorder history;
  re-run the same lights-off filter rather than eyeballing the raw distribution.
- **Two FP300 consumers were deliberately left alone**, because they are on the FP300's scale and
  resyncing them to 90 would be wrong. `natural_brightness` (lighting.jinja) dims the auto-on brightness
  across `lux / 75` — moving it to the T1 needs its 0.8 slope recalculated, a separate change. The Hub
  cast/dismiss automations (`bedroom_display_cast` / `bedroom_display_dismiss`) trigger at FP300 60 and
  50 — a different feature with its own thresholds.
- **`auto_light_allowed` takes a `dark_fallback` argument (added with the swap).** The T1 reports only on
  change, so it parks at `unknown` after an HA restart or a Z2M rename. The macro's old
  `| float(9999)` treated that as "bright" and shut the gate, which on the FP300 was harmless (it stays
  alive on temp/humidity/PIR traffic) but on the T1 would silently disable auto-lighting for a whole
  night in a stable dark room. The caller now passes `is_state('sun.sun', 'below_horizon')`, the same
  deterministic fallback `script.bedroom_apply_natural_gated` already uses for a stale read.
- **The T1 shrinks the circularity, it does not remove it.** The FP300 swings 13× across a light switch
  (48 ↔ 640); the T1 swings 1.6× (49 ↔ 79). So a gate threshold moved onto the T1 must sit clearly
  ABOVE 79 or clearly BELOW 49, never between — a value inside that band reintroduces the same
  self-referential gate in miniature. Real daylight runs into the hundreds, so a threshold picked from
  the sensor's own recorder history lands well above 79 — the 90 in force does. **Pick it from data, not
  by scaling the old 75** — that number was chosen against a lights-contaminated scale and means nothing
  on this sensor. The two constraints are independent: clearing the band is necessary but not
  sufficient, and *Picking the 90-lux threshold* above covers the part the band rule does not.
- **Z2M runtime state on the T1 (NOT in git — a re-pair wipes it).** `detection_period: 5` s, the
  factory default, left as-is: it is already the fast end and reports landed 2-9 s after a light
  switch. It is also the aggressive end for the CR2450, so revisit once the battery sensor reports
  (this model can take up to 24 h to publish battery for the first time). Re-apply after any re-pair
  with `zigbee2mqtt/Aqara T1/set` — see the `z2m-device-setting` skill.
- **The T1's HA entity parks at `unknown` after a Z2M rename or an HA restart**, because a rename
  republishes the MQTT-discovery configs and clears the state while the sensor only reports on change.
  Observed for 3.5 min on install day, cleared by the next brightness change. Anything that consumes
  this sensor must tolerate `unknown` — in a stable room that is a normal steady state, not a fault.
  `probe.py ha verify-entities` is unaffected either way: it compares entity_ids against
  `/api/states` and never reads state values, so an `unavailable` or `unknown` entity still counts as
  present.

## Manual override, bedtime and wake
- **Manual light detect (since 2026-06-21).** `automation.bedroom_manual_light_detect` makes a
  hand/voice/dashboard turn-off of `light.bedroom_lights` engage `input_boolean.bedroom_manual_off`
  (and a manual turn-on clear it) — the SAME override the Tap Dial's power button sets. Without it, an
  external "off" was re-lit ~30s later by `bedroom_presence_on`'s dusk-lux trigger: the bulbs going
  dark drop the FP300 illuminance below the gate (the documented lights-dominate-illuminance feedback
  loop), and only the Tap Dial — not Google Assistant / the dashboard tile / the app — had been
  engaging manual-off. Confirmed live via the logbook (voice `google_assistant_command` OnOff-off →
  `presence_on` numeric-state relight 36s later). Discriminates a genuine external action from our own
  automations' parented service calls via **`trigger.to_state.context.parent_id is none`** (same trick
  as `bedroom_fan_manual_detect`), so absence_off / away / bedtime / apply_natural / the Tap Dial never
  reach its action; a `from_state` unavailable/unknown guard stops an HA-restart group recompute from
  clearing the override. Symmetric with button 1 (off→engage, on→clear).
- **Bedtime / sleep routine (since 2026-06-18).** `script.bedroom_bedtime` (the shared "going to
  sleep" action) engages `input_boolean.bedroom_sleep_mode` (a quiet fan cap), then — **critically
  reordered** — calls `adaptive_lighting.set_manual_control: true` BEFORE flipping AL into sleep
  mode, so AL can't fire its own ~45s pre-dim before the fade begins; then fades to
  `scene.bedroom_nightlight` (amber 3%) over 30 min; then enables AL sleep mode (warm/dim target for
  after morning reset). The fade is a per-call `transition: 1800` on `scene.turn_on` (NOT baked into
  the scene), so only bedtime ramps — the B2-press and overnight "got up" nightlight stay instant.
  (Lengthened 900 -> 1800 on 2026-06-23 — the 15-min descent felt too fast.)
  This reorder is what makes the 30-min nightlight fade genuinely gradual: without it, enabling AL
  sleep mode FIRST caused AL to immediately pre-dim to its sleep_brightness before the fade started.
  `take_over_control: true` + `detect_non_ha_changes: false` keep AL from re-stomping the group
  mid-fade. The bulb does the
  brightness+color ramp internally (single Zigbee command, ZCL caps ~6553s), so an HA/Z2M restart
  mid-fade doesn't abort it — only a bulb power-cycle would. Triggered by `automation.bedroom_bedtime`
  off `binary_sensor.pixel_watch_3_bedtime_mode` → on (gated `person.daniel == home`), with **Tap
  Dial button-2 (Sleep) HOLD** as the manual fallback (`bedroom_tap_dial_control`). **Charging is deliberately
  NOT a trigger** (operator charges in-room). **Fan stays temperature-responsive, just bounded to a
  seasonal band:** when `bedroom_sleep_mode` is on, `bedroom_apply_fan`/`fan_target_level` apply an
  **outdoor-temp seasonal FLOOR** (L2 winter `< 45°F` / L3 shoulder `45–68°F` / L4 summer `≥ 68°F`,
  from `weather.forecast_home`) up to a fixed **L5 ceiling** — the floor guarantees white noise even in
  a cold room (curve wants 0 → floor holds), the indoor-temp curve modulates within `[floor, 5]` on a
  hot night, and a missing outdoor reading falls back to the winter band (quiet L2, the old behavior).
  Replaced the old flat L2 sleep cap 2026-07-13; TUNE the bands/floors/ceiling in `fan.jinja`. It does
  NOT freeze the fan. `bedroom_morning_reset` unwinds both sleep_mode + AL sleep mode before its fan/
  light re-applies (later moves to the watch-alarm wake). Phone bedtime/sleep sensors (DND,
  sleep_confidence, next_alarm) are now enabled in the companion app; the watch exposes
  `sensor.pixel_watch_3_next_alarm` (the real wake alarm) + `notify.mobile_app_pixel_watch_3`.
- **Dynamic morning wake (since 2026-06-18).** The wake ramp is driven by the real alarm, not a
  hardcoded time. `sensor.bedroom_wake_start` (a `device_class: timestamp` template sensor in
  `files/templates.yaml`) = `sensor.pixel_watch_3_next_alarm − 15 min`, `availability:` gated to
  MORNING alarms only (local hour 03:00–11:00) so a nap/evening alarm never arms it. It's the SINGLE
  source of truth for the wake window `[wake_start, alarm)`: `bedroom_morning_reset` time-triggers
  `at: sensor.bedroom_wake_start` (id `alarm`), and both `bedroom_apply_natural`'s morning exception
  and `bedroom_presence_on`'s window read it (the old triplicated 06:00/07:00 formula + weekday/weekend
  split are GONE). `bedroom_morning_reset` also has a `09:00` `fallback` trigger that clears the
  overnight overrides (sleep mode, AL sleep, manual-off, fan-manual) on no-alarm days WITHOUT forcing
  lights; only the `alarm` trigger runs the ramp. **The wake ramp is gated on the GEOFENCE
  (`person.daniel == home`), NOT the FP300 room sensor** (changed 2026-06-19). The room presence
  sensor was the gate originally, but with `motion_sensitivity` reverted to `high` (no setting
  separates the running fan from a person — see the FP300 fan false-HOLD note) the radar drops a
  motionless sleeper, so `presence` can read `off`/`unknown` at the exact moment you need waking
  (e.g. right after an HA restart the battery Zigbee radio hasn't reported yet). `person home` is the
  reliable "you're here to be woken" signal and still won't ramp an empty bedroom while away (an FP300
  dog/false-positive can't trigger the wake either). **Uses the WATCH alarm** (`pixel_watch_3`), not the
  phone's (unreliable). Watch caveat moot now — set alarms anywhere; only morning ones wake.
- **Sleep-quality-aware morning (since 2026-06-18).** The wake ramp adapts to how you slept: the
  `wake_brightness` macro (`custom_templates/lighting.jinja`, the tested source of the ramp —
  `bedroom_apply_natural` now delegates the morning exception to `bedroom_apply_wake`) scales the
  mid/knee down on a short night — knee **14%** (mid 5%) at `alarm+20` when
  `sensor.pixel_9_pro_sleep_duration` is `0 < x < 360` min (under 6h), else knee **20%** (mid 8%);
  unknown/0 falls back to the normal curve. The final segment STILL climbs to **100% by `alarm+30`**
  on a short night — only the early (most-asleep) part softens; ending below 100% would reintroduce
  the AL hand-off pop the ramp-to-100 design removes. `bedroom_morning_reset`'s alarm+present block
  also sends a routine "you slept N h" note (😴 short night / ☀️ good morning), skipped if
  sleep_duration is 0/unknown. **Caveat:** the Google Sleep API finalizes `sleep_duration` around
  wake, so at alarm−15min it can be stale — best-effort (graceful fallback to a normal wake). Only
  the early mid/knee change with sleep; the window/transition/final-100% and `presence_on` are
  untouched.
