# Tap Dial Button 4 — dial-controls-fan mode (2026-06-20)

## Problem / goal

Today the Hue Tap Dial's rotary dial always controls **light brightness** (±12 %/step), and
**Button 4 HOLD boosts the fan to 100 %**. We want a way to adjust the **fan level** with the dial:

- **Button 4 HOLD** toggles a temporary "fan-dial" mode where the **dial changes the fan level**
  instead of the lights.
- **Hold again** toggles it back off (dial returns to controlling lights).
- The mode **auto-reverts to lights after 5 minutes**, on a **sliding window** — each dial turn
  restarts the 5-minute clock, so it stays in fan mode while you're actively adjusting and reverts
  5 minutes after your *last* turn.
- **No on-screen / light cue** when it toggles (silent — the Tap Dial has no display).

This **replaces** the current Button-4-hold boost behavior.

## Key design decision: the timer *is* the mode

Rather than a separate `input_boolean` flag plus a timer, use a single HA `timer` helper whose
`active` state **is** "fan-dial mode":

- **Enter** = `timer.start` · **Exit** = `timer.cancel` · **Auto-revert** = the timer expires
- The dial-rotate handlers read `is_state('timer.bedroom_fan_dial', 'active')` to choose fan vs light
- **Restart-safe for free:** HA timers default to `idle` after a restart (no `restore`), so a deploy
  in the middle of a session can't strand the dial in fan mode. This deliberately sidesteps the known
  "stale `input_boolean` override restored on unclean shutdown / deploy" trap.
- **No `timer.finished` automation needed** — revert is implicit (the timer simply stops being
  `active`, so the next dial turn falls through to the light branch).

## Accumulator: reuse `input_number.bedroom_fan_expected_level`

The DREO is a `cloud_push` integration whose reported `percentage` lags our commands. If each dial
step read the fan's *actual* level, rapid spins would mis-count (read-after-write staleness against
the laggy cloud echo).

Instead, drive the nudge off **`input_number.bedroom_fan_expected_level`** as an instant, server-side
accumulator. This helper already means "the level we intend the fan to be at" (it's what
`bedroom_apply_fan` writes before commanding, and what `bedroom_fan_manual_detect` reads to suppress
self-echoes), so it stays coherent:

- On **enter** (Button-4-hold start) seed it from the fan's *actual* current level (via `pct_to_level`),
  so the first dial turn is relative to reality even if a prior remote change left it stale.
- Each **dial turn** reads the accumulator, adds ±1 (clamped 0–9), writes it back, and commands the
  fan to that level. Because we write `expected_level == commanded level`, `bedroom_fan_manual_detect`
  sees our own echo and does not double-flag; we set the manual override explicitly instead (below).

No new `input_number` is introduced.

## Manual-override + cap semantics

- Turning the dial in fan mode is a **manual** fan change, so the nudge sets
  `input_boolean.bedroom_fan_manual` **on**. This stops the temperature automation
  (`bedroom_fan_temperature` → `bedroom_apply_fan`, gated on the override) from fighting the user.
- The level you set **persists** after the 5-minute revert (the revert only changes what the *dial*
  controls; it does not touch the fan or the override). Return the fan to automatic the normal way:
  **Button 4 TAP** (clears the override + applies the temperature band) or the morning reset.
- The manual dial **ignores the night (L4) / sleep (L2) caps** that `bedroom_apply_fan` applies —
  you're explicitly in control, so the full 0–9 range is available (the old hold-to-boost-100 also
  ignored the caps). The caps still constrain the *automatic* curve only.
- **Entering** fan mode without ever turning the dial does **not** set the manual override (the fan is
  untouched); the override is only engaged on an actual nudge.

## Components

### 1. `configuration.yaml.j2` — new `timer` helper
```yaml
timer:
  bedroom_fan_dial:
    name: Bedroom fan-dial mode
    duration: "00:05:00"
    icon: mdi:fan-chevron-up
```
(No `restore:` — idle on restart is the desired behavior. No new `input_boolean`.)

### 2. `custom_templates/fan.jinja` — new `fan_nudge_level` macro
Pure clamp math (numbers in → number out), tested like the other fan macros:
```jinja
{# Current level + delta -> new level, clamped to 0..FAN_LEVELS (0 = off). #}
{%- macro fan_nudge_level(cur_level, delta) -%}
{{ [[ (cur_level | int(0)) + (delta | int(0)), 0 ] | max, FAN_LEVELS] | min | int }}
{%- endmacro -%}
```

### 3. `scripts.yaml` — new `script.bedroom_fan_nudge`
`fields: delta` (+1 / −1). Mode `queued` (max 10) so rapid turns serialize and each reads the freshly
written accumulator. Steps:
1. `new` = `fan_nudge_level(states('input_number.bedroom_fan_expected_level') | int(0), delta)`
2. `input_boolean.turn_on` → `bedroom_fan_manual` (manual override)
3. `input_number.set_value` → `bedroom_fan_expected_level = new` (accumulator + suppress self-detect)
4. command the fan: if `new == 0` → `fan.turn_off` (only if on); else `fan.turn_on` +
   `fan.set_percentage` to `level_to_pct(new)` (the midpoint % that the integration's `ceil` lands on `new`)

### 4. `automations.yaml` — `bedroom_tap_dial_control`
- **Button 4 HOLD** (`button_4_hold_release`) — replace the boost sequence with the toggle:
  ```
  if timer active → timer.cancel
  else            → timer.start ; seed bedroom_fan_expected_level from the actual fan level
  ```
- **Button 4 TAP** (`button_4_press_release`) — unchanged fan→auto behavior, **plus** `timer.cancel`
  (tapping "fan back to auto" logically exits manual fan-dialing).
- **Dial rotate right / left** — branch on the timer:
  ```
  if timer active → script.bedroom_fan_nudge(+1 / -1) ; timer.start   (sliding-window reset)
  else            → light.turn_on brightness_step_pct ±12             (unchanged)
  ```

### 5. `tests/test_fan_macros.py` — cover `fan_nudge_level`
Grid over `cur_level` 0–9 × `delta` ∈ {−1, +1}; assert clamping at both bounds (0 and 9) and +1/−1
in the interior. Follows the existing macro-test pattern via `jinja_harness`.

## Behavior left unchanged

- Buttons 1 (Power), 2 (Brightness), 3 (Sleep) and their hold actions.
- The dial in the **default** (no fan mode) state: ±12 % light brightness, `transition: 0.2`.
- Max fan is still reachable (dial to level 9); the notification "Boost fan" action
  (`BEDROOM_BOOST_FAN`) is untouched, so no capability is lost despite dropping hold-to-boost.

## Testing & deploy

- `uv run pytest ansible/roles/containers/home-assistant/tests` — new `fan_nudge_level` test green,
  existing fan/lighting macro tests still pass (TDD: write the macro test first).
- `prek` hooks: `validate-ha-config` (assembles `/config`, checks YAML + `!include` + inline Jinja
  syntax incl. the new macro) and `validate-compose-templates`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"` (recreates HA ~120 s;
  `configuration.yaml`, `automations.yaml`, `scripts.yaml`, and `custom_templates/` all feed
  `common_config_changed`).
- Live check: hold B4 → dial changes fan, lights unaffected; hold again → dial back to lights; idle
  5 min → reverts; rapid spin accumulates correctly; `timer.bedroom_fan_dial` is `idle` after a deploy.

## Out of scope (YAGNI)

- Dashboard countdown card for the timer (explicitly declined).
- Any cue/notification on toggle (silent by choice).
- A dedicated new accumulator `input_number` (reuse `bedroom_fan_expected_level`).
