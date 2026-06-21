# Bedroom Lighting Review — Design

**Date:** 2026-06-21
**Status:** Approved (pending spec review)
**Scope:** A focused review of the bedroom lighting automations to fix the wake-up ramp and
smooth four day-long rough edges. All changes live in the `home-assistant` role
(`ansible/roles/containers/home-assistant/`), follow the copy-not-template + tested-macro
conventions, and ship via `ha-deploy` + `ha-verify-state`.

## Goals

1. **Wake-up:** fix the "comes on way too bright to start" bug and replace it with a deliberate,
   gentle-then-steep sunrise centered on the alarm.
2. **Presence flap:** stop the FP300 false-absence on/off churn.
3. **Evening wind-down:** make the bedtime fade actually gradual and make its prompt track the
   real alarm instead of a fixed clock time.
4. **Manual stickiness:** when a light level is set by hand, it must persist — no harsh auto-overrides.
5. **Context-aware presence auto-on:** when presence turns the lights on, choose brightness from the
   current ambient light + time of day, and color from the time of day — no flat slam to ~100%.
6. **Slow color tracking (item 5b):** while the lights are in "auto" mode, let the color quietly drift
   to follow Adaptive Lighting's sun curve over time, without disturbing the one-shot ambient brightness.

## Out of scope (explicitly excluded by the operator)

- The lux-gate **threshold value** (75 lux) and its feedback-loop circularity — not retuned here.
  (Items 2, 4, and 5 all work *around* the gate without changing it.)
- AL `max_brightness: 100` stays as configured — item 5 overrides brightness on the auto path rather
  than retuning AL's own curve, so the constant is left untouched.

---

## Item 1 — Wake-up sunrise

### Root cause (confirmed)

`script.bedroom_set_natural_brightness` (scripts.yaml) runs, in order:
1. `adaptive_lighting.apply` with `turn_on_lights: true, adapt_brightness: false` → turns the OFF
   bulbs **on at their last level** (AL drives them to 100% by day, confirmed live), so they snap bright.
2. `light.turn_on brightness_pct: <wake_brightness> transition: 900`.

The wake exception in `bedroom_apply_natural` is also **sampled once** at the alarm trigger
(`elapsed ≈ 0 → 1%`). Net result: a bright pop at `alarm−15`, then a 15-min fade *down* to ~1% — the
opposite of a sunrise, and "too bright to start." The documented 1%→50% rise never executes.

### New behavior

- **Window:** 30 minutes **centered on the alarm** — `alarm−15` → `alarm+15`. `sensor.bedroom_wake_start`
  stays `alarm−15` (the ramp start + the `bedroom_morning_reset` trigger); the window *length* becomes 30.
- **Curve (gentle-then-steep):** `1%` at `alarm−15` → `~12%` at the alarm → `40%` at `alarm+15`.
  - Pre-alarm half (elapsed 0–15): `1 → 12` linear.
  - Post-alarm half (elapsed 15–30): `12 → 40` linear (the "get up" push).
- **Sleep-aware:** short night (`0 < sleep_min < 360`) scales the targets down ~0.6× →
  `~7%` at the alarm, `~24%` peak. Unknown/0/long night → normal (12/40).
- **Color:** fixed warm `2200K` (clamps to the bulbs' ~2237K floor) for the whole ramp. No AL apply
  on the wake path → no flash. (A warm→cool color ramp is a possible later enhancement, not now.)

### Implementation

- **Macros (`custom_templates/lighting.jinja`, unit-tested):**
  - Rewrite `wake_brightness(elapsed_min, sleep_min)` → the piecewise two-slope curve above, with
    named constants (`WAKE_START_PCT=1`, `WAKE_MID_PCT=12`, `WAKE_PEAK_PCT=40`, short-night factor `0.6`).
  - Update `in_wake_window(elapsed_min)` upper bound `15 → 30`. Centralize the window length as a single
    constant used by both macros.
  - Remove `wake_transition` (the per-minute tick uses a fixed transition; see below) and its tests.
- **New `script.bedroom_apply_wake` (scripts.yaml):** reads `now()`, `sensor.bedroom_wake_start`,
  `sensor.pixel_9_pro_sleep_duration`; computes the target via `wake_brightness`; issues one
  `light.turn_on brightness_pct: <target>, color_temp_kelvin: 2200, transition: 60`. No-op if
  `bedroom_wake_start` is unavailable. This is the single source of the wake "frame."
- **New automation `bedroom_wake_ramp` (automations.yaml):**
  - Trigger: `time_pattern minutes: "/1"` (there is precedent — `ha_heartbeat`).
  - Condition: `person.daniel == home` and `input_boolean.bedroom_manual_off == off`.
  - Action `choose`: if `in_wake_window(elapsed)` → `script.bedroom_apply_wake`; else if the window just
    ended (elapsed in `[30, 31)`) → `script.bedroom_apply_natural` (default branch releases AL and hands
    the lights back to Adaptive Lighting for the day); else no-op.
- **`bedroom_apply_natural` wake exception → `script.bedroom_apply_wake`** so a mid-ramp resume
  (presence re-trigger, Tap-Dial B4) recomputes the correct current frame. The `and not in_window`
  guard on the nightlight exception is unchanged (still mutually exclusive at the boundary).
- **`bedroom_morning_reset`:** structurally unchanged — at `wake_start` it still clears the overnight
  overrides, calls `apply_natural` (now → `apply_wake`, the instant first frame), and sends the
  sleep-quality note. The per-minute climb is the new `bedroom_wake_ramp`.
- **`bedroom_set_natural_brightness`:** fix the flash (set brightness + AL's natural color in one
  `light.turn_on` instead of `adaptive_lighting.apply turn_on_lights: true`) so the documented
  extension helper is safe for any future exception, even though the wake path no longer uses it.

### Rejected alternative

Two chained Zigbee transitions with a `delay` (1→12 over 15 min, then 12→40 over 15 min): simpler but
**not restart-safe** (a restart mid-window drops the second segment) and awkward to resume. Per-minute
re-evaluation is stateless — a restart just resumes at the next tick — and makes resume free.

---

## Item 2 — Presence flap / on-off churn

### Root cause (confirmed live)

While `person.daniel = home`, FP300 `presence` cycles on/off every 2–4 min (false-absence). Each drop:
`bedroom_absence_off` (`for: 1m`) turns the lights off → the FP300 illuminance (dominated by the bulbs)
collapses ~547 → ~48, **below** the 75-lux gate → `bedroom_presence_on` re-lights on re-acquire →
repeat. Observed midday (07:00 local) as a repeating `absence off → call_service` cycle.

### Fix

Lengthen `bedroom_absence_off` `for: 00:01:00 → 00:05:00`. A brief radar drop never reaches lights-off,
so the cycle never starts. Git-managed, one-line, reversible. **Lux gate untouched.**

- **Trade-off:** the lights linger ~5 min after you genuinely leave the bedroom while still home; the
  geofence `bedroom_away` sweep still handles leaving the house.
- **Optional follow-up (not required):** bump the FP300 `absence_delay_timer` Z2M device setting
  (60s → 120s) via the `z2m-device-setting` skill — holds presence through short drops at the source.
  Noted for a later tuning pass; not part of this change.

---

## Item 3 — Evening wind-down

### 3a. Abruptness (same bug class as the morning)

In `script.bedroom_bedtime`, AL sleep mode is enabled **before** the nightlight fade, so AL's forced
~45s apply crashes brightness to `sleep_brightness: 1` first; the subsequent 15-min scene fade is then a
near-invisible 1%→3%. Perceived result: a fast 45s dim, not a gradual wind-down.

**Fix:** reorder so the fade owns the lights:
1. `input_boolean.bedroom_sleep_mode` on (fan cap — unchanged).
2. `adaptive_lighting.set_manual_control: true` (mark the group AL-hands-off) **before** sleep mode.
3. `scene.bedroom_nightlight` with `transition: 900` (the only brightness change: current → amber 3%).
4. `switch.adaptive_lighting_..._sleep_mode_bedroom` on (now harmless — AL is already hands-off, so it
   can't pre-dim; it just sets AL's target for after the morning reset releases control).
5. Re-apply the fan (unchanged).

The bulb's internal 900s transition still survives an HA/Z2M restart mid-fade (single Zigbee command).

### 3b. Rigid timing → alarm-anchored prompt

The routine already auto-starts on watch Bedtime mode; only the fixed 22:00 *prompt* is rigid.

- **New `sensor.bedroom_winddown_start` (templates.yaml):** `state = (next_alarm − 8h).isoformat()`,
  `availability` gated to a real **morning** alarm (local hour 3–11, mirroring `bedroom_wake_start`).
  For a 6:00am alarm this is 10:00pm; for 7:30am, 11:30pm. Target-sleep default **8h** (a constant in the
  template, tunable).
- **`bedroom_bedtime_prompt`:** triggers become `at: sensor.bedroom_winddown_start` (id `dynamic`) +
  `at: "22:30:00"` (id `fallback`). Keep the existing conditions (present + not sleep_mode + home) and add:
  the `fallback` only fires when `sensor.bedroom_winddown_start` is `unavailable` (i.e. no morning alarm
  set), so an alarm night never double-prompts. Action (the `Start now` actionable notify) is unchanged.

---

## Item 4 — Manual changes stick (no harsh overrides)

### Root cause (hypothesis, to confirm via logbook in implementation)

A manual dim drops the FP300 illuminance below 75; `bedroom_presence_on`'s `numeric_state below: 75 for: 30s`
trigger fires `apply_natural`, whose **default branch calls `set_manual_control: false` and slams the group
to AL's sun-curve target** (~100% midday). So ~30–90s after a manual change it snaps *brighter* and releases
control. Adaptive Lighting's `take_over_control` already backs off on HA-issued dial/slider/scene changes —
the clobber comes from `presence_on` releasing it, not from AL itself.

### Fix

`bedroom_presence_on` should only ever turn lights **off→on**, never re-stomp lights already on:

- Add condition `light.bedroom_lights` state `off` to `bedroom_presence_on`. This preserves the
  "light a dark room as dusk falls" purpose (lights are off in that case) while ending the manual-dim
  clobber. The dusk-lux trigger then only matters when the room is genuinely dark *and unlit*.
- Add the same `light.bedroom_lights == off` guard to `bedroom_arrive_home`'s light re-check (a GPS
  jitter home→away→home shouldn't relight/clobber an already-on manual level).

### Verification gate

During implementation, confirm from the logbook that the snap-back context is `presence_on` /
`call_service` (not `al:` Adaptive Lighting). **If AL itself is releasing/re-applying** over a manual
change, add an explicit `input_boolean.bedroom_light_manual` override (mirroring the proven
`bedroom_fan_manual` pattern: set on a detected manual change, gate the auto-light paths on it, cleared
by Tap-Dial B1 hold / morning reset / away) as the fallback. Prefer the single `presence_on` guard if it
proves sufficient (YAGNI).

---

## Item 5 — Context-aware presence auto-on

### Goal

Today, walking in during the day runs `apply_natural`'s `default:` branch = **full Adaptive Lighting**,
which drives brightness from the sun curve → ~100% midday. Replace that with a brightness chosen from
**current ambient light + time of day**, and color from the **time of day** (Adaptive Lighting). Applies
to every "natural" turn-on (presence, Tap-Dial B1 press, arrive-home re-check) — i.e. the auto path, not
the explicit scene buttons (B2 relax/bright stay as-is).

### Model — "ambient-fill" (operator's choice)

- **Time-of-day base** (the level if the room were pitch dark): morning (05–09) **55%**, daytime
  (09–17) **45%**, evening (17–24) **35%**. (00–05 is the nightlight exception — not this path.)
- **Ambient dim:** multiply the base by a factor falling linearly **1.0 → ~0.2 across 0 → 75 lux**
  (the gate ceiling), with an output floor of ~5% so it's always visibly on. Brighter room → dimmer
  output, within the only band where auto-on ever happens.
- **Color:** Adaptive Lighting's computed time-of-day color, read from the AL switch's
  `color_temp_kelvin` attribute (warm AM/PM, cooler midday) — no `adaptive_lighting.apply` flash.

This reproduces the approved table (morning 55/35/12, daytime 45/28/10, evening 35/18/8 at ~5/40/70 lux);
exact numbers are tunable constants in the macro.

### Implementation

- **New tested macro `natural_brightness(hour, illuminance)` (`lighting.jinja`):** numbers in → target %,
  encoding the time bands + the linear ambient factor + the floors. Unit-tested in `test_lighting_macros.py`.
- **`bedroom_apply_natural` `default:` branch:** replace "full AL (color + brightness)" with
  `script.bedroom_set_natural_brightness(brightness_pct = natural_brightness(now().hour,
  sensor.aqara_fp300_illuminance), transition = 2)`. Illuminance is read at turn-on while the lights are
  **off**, so it's true ambient (no feedback contamination); default `0` (→ base brightness) if unavailable.
- **`bedroom_set_natural_brightness`** (the flash-fixed helper from item 1) becomes the universal
  "AL-color + chosen-brightness" applier: a single `light.turn_on` with `brightness_pct` +
  `color_temp_kelvin` (read from the AL switch) + `transition`. Used by item 5's default, future
  exceptions, and (color aside) sharing the no-flash approach with item 1's `bedroom_apply_wake`.

### Behavior change to note

Setting an explicit brightness marks the group manually-controlled (AL `take_over_control`), so AL stops
acting as a continuous governor for the bedroom group. **Brightness** therefore stays the one-shot ambient
value (re-picked on each fresh turn-on) — by design, to avoid the lux feedback loop. **Color** does *not*
go stale: item 5b re-adds slow, continuous color tracking on top (see below), so the lights still follow
the sun's color over the day while you stay in the room. AL still computes the color (we read it) and owns
bedtime sleep mode.

### Interaction with the gate

`presence_on` only fires below 75 lux (gate unchanged), so the 0–75 band is exactly where this operates;
near the gate it yields a gentle ~8–12% "just enough" light as dusk falls.

---

## Item 5b — Slow color tracking

### Goal

After a context-aware turn-on (item 5), AL is taken over and would otherwise hold the color fixed at the
turn-on value. Restore continuous **color** drift — quietly follow AL's sun-curve color over time —
**without** re-deriving brightness (which would oscillate via the lux feedback loop). Brightness stays the
one-shot ambient value; only color tracks.

### Why color is safe but brightness isn't

AL keeps *computing* its sun-curve `color_temp_kelvin` even while taken over (take-over suppresses
*applying*, not *calculating*), so `switch.bedroom_adaptive_lighting_bedroom`'s attribute is always live
and readable. Nudging *color* at fixed brightness barely moves the FP300 lux, so a color loop can't
oscillate; re-deriving *brightness* from that bulb-dominated sensor would — hence color-only.

### Implementation

- **New helper `input_number.bedroom_light_expected_color_temp`** (Kelvin; `configuration.yaml.j2`
  `input_number:`, beside `bedroom_fan_expected_level`) — the color we last *auto*-set. Mirrors the proven
  `bedroom_fan_expected_level` "expected value" pattern, the self-contained way to tell our own color from
  a user's without fragile context-sniffing.
- **New automation `bedroom_color_track`:** `time_pattern minutes: "/5"`. Conditions: lights on; person
  home; `manual_off` off; **not** in the wake window (the ramp owns fixed warm); `sleep_mode` off and not
  00:00–05:00 (night warmth); `color_mode == color_temp` (skip RGB picks); and **current color within
  ~150K of `bedroom_light_expected_color_temp`** (i.e. still "auto" — not a user color/scene). Action: read
  AL's `color_temp_kelvin`, `light.turn_on color_temp_kelvin: <al> transition: 290` (color only — no
  brightness, so the ambient level holds), then write that value to the expected-color helper.
- **`bedroom_set_natural_brightness` arms the tracker:** after applying AL color + the ambient brightness
  it writes the same color to `bedroom_light_expected_color_temp`, so tracking is "armed" after every auto
  turn-on. Scenes (B2 Relax/Bright) and the color-temp slider deliberately do **not** arm it → their color
  fails the tolerance check → tracking pauses and respects the manual choice until a reset / fresh turn-on.

### "Manual override" semantics

The pause is on a manual **color** change (scene, color-temp slider, RGB pick) — not a brightness-only
change. Dialing brightness leaves the color matching the expected value, so color keeps tracking the sun
while your manual brightness sticks (item 4). This is the intended split: brightness and color overrides
are independent.

---

## Files touched

| File | Change |
|------|--------|
| `files/custom_templates/lighting.jinja` | Rewrite `wake_brightness` (piecewise + sleep-aware), `in_wake_window` 15→30, window constant, remove `wake_transition`; **new `natural_brightness(hour, illuminance)` (item 5)**. |
| `tests/test_lighting_macros.py` | New curve endpoints/midpoints, 30-min window boundaries, sleep-aware scaling; drop `wake_transition` tests; **`natural_brightness` band + ambient-factor + floor tests (item 5)**. |
| `files/scripts.yaml` | New `bedroom_apply_wake`; `apply_natural` wake exception → `apply_wake`; **`apply_natural` `default:` → `set_natural_brightness(natural_brightness(...), 2)` (item 5)**; fix `set_natural_brightness` flash + read AL color attr + **arm the color tracker (item 5b)**; reorder `bedroom_bedtime`. |
| `files/automations.yaml` | New `bedroom_wake_ramp`; **new `bedroom_color_track` (item 5b)**; `absence_off` 1m→5m; `presence_on` + `arrive_home` add `light == off` guard; `bedtime_prompt` alarm-anchored. |
| `templates/configuration.yaml.j2` | **New `input_number.bedroom_light_expected_color_temp` helper (item 5b)**, beside `bedroom_fan_expected_level`. |
| `files/templates.yaml` | New `sensor.bedroom_winddown_start`. (`bedroom_wake_start`, `bedroom_auto_light_allowed` unchanged — the latter inherits the 30-min window via the macro.) |
| `home-assistant/CLAUDE.md` | Update the wake/bedtime/presence prose to match the new behavior (doc-accuracy convention). |

## Testing & rollout

- **TDD first** on the `lighting.jinja` macros (`uv run pytest ansible/roles/containers/home-assistant/tests`).
- `validate-ha-config` prek hook (syntax + `!include` + inline-template checks) before deploy.
- `ha-deploy` (gate on container health, confirm the new/changed automations + sensors loaded).
- `ha-verify-state`: watch one wake window (or fast-forward by setting a near-term test alarm) to confirm
  the rising ramp and the AL hand-off; confirm `sensor.bedroom_winddown_start` resolves; manually dim and
  confirm it sticks past ~2 min; confirm a brief FP300 drop no longer kills the lights; confirm a
  presence turn-on in a dim vs. a brighter room yields different brightness (item 5) with time-of-day color;
  confirm the color slowly tracks AL over time (item 5b) and that picking a scene/color-temp pauses it.

## Defaults chosen (easy to change)

- Absence-off persistence: **5 min**.
- Target sleep for the wind-down prompt: **8h**; no-alarm fallback **22:30**.
- Wake curve: **1% → 12% (alarm) → 40% (+15)**, short-night ~0.6×; wake color **2200K**.
- Auto-on ambient-fill (item 5): time-of-day base **55% / 45% / 35%** (morning / day / evening); ambient
  factor **1.0 → 0.2 across 0–75 lux**; output floor **5%**; color from Adaptive Lighting.
- Color tracking (item 5b): nudge **every 5 min**, **~5-min glide** (transition 290s), match tolerance
  **±150K**.
