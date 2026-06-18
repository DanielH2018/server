# HA smooth temperature → fan curve

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant bedroom (`ansible/roles/containers/home-assistant`, `script.bedroom_apply_fan`)

## Problem

The temp→fan map uses 4 coarse bands → DREO levels 0/2/4/6 (off/Low/Med/High), so the fan jumps
2 levels at a time — effectively 3 speeds. Smooth it to use the DREO's full 9 levels (~1 level/°F).

## Goals / decisions

- **Continuous mapping, ~1 level/°F:** off below ~72°F, then `level = round(t − 71)` clamped 1–9
  (72→L1 … 80→L9). Start offset (71) and implicit slope are one-line tunables.
- **Caps unchanged in effect, re-expressed as LEVEL caps:** sleep mode → max L2 (~Low), 22:00–06:00
  night → max L4 (~Medium), else L9. (Match today's band-index caps 1/2 → levels 2/4.)
- **Hysteresis as a ~0.7-level deadband** (replaces the band-edge 0.5°F deadband): when the fan is
  on, only change level when temp "wants" ≥0.7 level away from the current one; turning on jumps
  straight to the ideal level. So sensor noise near a step doesn't flap the fan.
- **Unchanged mechanics:** the `(L−0.5)/9·100%` send trick (hit an exact level past the DREO
  integration's `ceil`), the "only command when the level changes" gate, the
  `input_number.bedroom_fan_expected_level` echo-suppression write, the `t >= 0`
  sensor-unavailable guard, and the temp/22:00/06:00 triggers + caller override gates.

## Architecture

All within `script.bedroom_apply_fan`'s `variables:` (replacing the band/`cb`/`abs_band`/`band_raw`
/`levels` math) plus one `choose` condition:

```
t        = airgradient temp °F (-1 if unavailable)
cur_pct  = fan % (0 if off);  cur_level = round(cur_pct*9/100)
ideal    = (t - 71) if t >= 0 else 0          # continuous level temp wants
is_night = 22:00–06:00 ;  sleep = bedroom_sleep_mode on
cap      = 2 if sleep else (4 if night else 9)
want     = 0 if (t < 0 or ideal < 0.3)
           else round(ideal) if (cur_level == 0 or ideal > cur_level+0.7 or ideal < cur_level-0.7)
           else cur_level                      # hysteresis: stay within the deadband
target_level = min(want, cap)                  # 0..9
send_pct = round((target_level-0.5)*100/9) if target_level>0 else 0
```

`choose`: `target_level == 0` → turn the fan off (if on); else → turn on + `set_percentage`
`send_pct` only when `cur_level != target_level`.

## Behavior vs. today

- Smooth ramp: e.g. 73→L2, 74→L3, 75→L4, 76→L5, 77→L6 … 80→L9 (vs old Low/Med/High at 2/4/6).
- At 76°F slightly gentler (≈L5 vs old L6) but more headroom above. Night caps at L4, sleep at L2.
- Turning on jumps to the right level; small temp wiggles (<0.7 level) hold the current level.

## Error handling / edge cases

- **Sensor unavailable (t < 0):** the existing `t >= 0` condition aborts before commanding.
- **Flapping:** the 0.7-level deadband (~0.7°F either side of a step) holds the level through noise.
- **Cap transitions:** the 22:00 / 06:00 triggers re-run the script so the cap engages/releases even
  at steady temp (unchanged); sleep cap engages/releases via bedtime / morning reset re-applies.
- **Big temp jump:** `want = round(ideal)` jumps straight there (deadband only gates small moves).

## Testing (manual + simulated)

- Simulate the level math in Python across every °F (68–82) and a rising/falling sequence to prove
  the curve + hysteresis (no flapping, monotonic) before deploy.
- Before deploy: HA Developer Tools → YAML → Check Configuration.
- After deploy: confirm `script.bedroom_apply_fan` loads; the fan tracks temperature in finer steps.

## Files touched

- `ansible/roles/containers/home-assistant/files/scripts.yaml` — `bedroom_apply_fan` curve
- `ansible/roles/containers/home-assistant/CLAUDE.md` — update the fan-control description
- `ansible/PLANS.md` — mark the note done

## Future / out of scope

- A configurable comfort setpoint / per-time-of-day curves.
