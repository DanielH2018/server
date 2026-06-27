---
name: ha-review-2026-06-27-findings
description: Two real cross-feature HA findings from the 2026-06-27 review — wake-end over-dim and window-advisor disabled by the purifier
metadata:
  type: project
---

Read-only review of the bedroom HA suite on 2026-06-27. The suite is very mature; config validates
clean, all macro tests pass, every automation loads. Two genuine cross-feature issues found (NOT yet
fixed — review-only task):

**1. Wake-ramp end over-dims the room (Medium).** `automation.bedroom_wake_ramp`'s window-end
branch (elapsed 30..31) calls `script.bedroom_lights_set reason: "natural"` → `bedroom_apply_natural`
default → `natural_brightness(hour, illuminance)`. At that instant the lights are still ON (~38% from
the ramp), so `sensor.aqara_fp300_illuminance` is contaminated by the bulbs (the documented
lights-dominate-illuminance loop) → ambient factor floors at 0.2 → lights drop to ~11% at alarm+15
(worst in winter, no daylight). `natural_brightness`'s own docstring says "read illuminance while the
lights are OFF" — the wake-end caller violates that precondition. Also it leaves AL in take-over
(manual brightness), so it never actually hands back to AL for the day; self-heals only when you next
leave+return (presence_on runs with lights off). Fix direction: window-end should release AL
(`adaptive_lighting.set_manual_control:false` + `adaptive_lighting.apply`) instead of the ambient-fill
path, OR `bedroom_apply_natural` default should skip ambient-fill when lights are already on.

**2. Window advisor is effectively disabled by the air purifier (Medium).** `ventilation.jinja`
`ventilation_advice` smoke guard `op > ip` (outdoor PM2.5 > indoor PM2.5 → 'none'). The HEPA purifier
(`switch.air_purifier`, `automation.bedroom_air_purifier_presence`) keeps indoor PM2.5 very low
(verified live: indoor 4.24 vs outdoor 11.9, both far under pm_safe=25), so `op > ip` is almost always
true → `bedroom_window_advisor` never advises opening for stale CO2/VOC or free cooling, even when both
PM values are perfectly safe. The purifier (added recently) regressed the advisor (added 2026-06-20).
Fix direction: only apply the dirtier-than-indoors guard above an absolute floor, or use a margin
(`op > ip + N`) — the absolute `op > pm_safe`/`op10 > pm10_safe` caps already cover genuinely bad air.

Lesser notes: the 75-lux gate is duplicated as a literal in `bedroom_presence_on`'s numeric_state
trigger AND `auto_light_allowed` (inherent — a trigger can't call a macro; keep in sync). A sudden
severe air-quality spike fires BOTH the moderate (watch) and severe (pierce) alerts at once.
`switch.air_purifier` is an automated actuator with no single-writer entry in `sanctioned_writers.yml`
(only lights+fan are governed) — a future second writer wouldn't fail CI.
