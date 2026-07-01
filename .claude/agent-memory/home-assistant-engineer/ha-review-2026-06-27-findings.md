---
name: ha-review-2026-06-27-findings
description: Two real cross-feature HA findings from the 2026-06-27 review — wake-end over-dim and window-advisor disabled by the purifier
metadata:
  type: project
---

UPDATE 2026-07-01 (2nd re-review, review-only): BOTH original findings now CLOSED — do NOT re-flag.
Finding #1 (wake-end over-dim) RESOLVED by ramp-to-100 + bounded AL-release. Finding #2 (advisor
disabled by purifier) RESOLVED by commit 3dd603c6 dropping the relative PM veto ENTIRELY (the
`pm_relative_floor` half-fix below was superseded the same day) — macro gates on absolute
pm_safe/pm10_safe only. This was the 3rd/final fix; NEVER re-propose any relative/margin/floor term.
2nd review found no new High/Medium; suite validates clean, macro tests pass. AL-stuck-manual edge
(leave mid-wake-window) self-heals (AL clears manual_control on light-off via the away-sweep). Only
nit: `bedroom_window_advisor` 'cool' message computes delta from rounded temps → can read ±1° off the
raw 5°F trigger (cosmetic).

SUPERSEDED — UPDATE 2026-07-01 (1st re-review): Finding #2 is now PARTIALLY fixed (commit d25641fb): `ventilation.jinja`
gained `pm_relative_floor=15`, so the relative `op > ip + margin` veto only bites once outdoor PM is
itself > 15. Residual GAP: floor 15 < pm_safe 25, so on safe-but-moderate outdoor days (PM 15–25) a
purifier-scrubbed indoor still vetoes CO2/cooling advice. Cleanest per prior fix direction: raise
`pm_relative_floor` to 25 (== pm_safe → relative term never independently bites) or drop it entirely
(the absolute pm_safe/pm10_safe caps already cover genuinely bad air). Wake-ramp handoff single-tick
watch-item is RESOLVED (bounded [45,90) catch-up window `in_wake_release_window` + `al_still_manual`).

UPDATE 2026-06-29 (re-review): **Finding #1 (wake-ramp end over-dim) is now RESOLVED** — the
2026-06-29 ramp-to-100 redesign changed `bedroom_wake_ramp`'s window-end branch (elapsed 45..46) to
release AL (`adaptive_lighting.set_manual_control:false` + `apply turn_on_lights:false`) instead of
`reason:"natural"`; the ramp now climbs to 100% so AL takes over at full brightness. **Finding #2
(window advisor disabled by purifier) is STILL LIVE** — the pm_dirty_margin=10 fix does NOT durably
hold: confirmed 2026-06-29 indoor PM2.5=1.66 vs outdoor=12.1, so `op > ip + 10` (12.1 > 11.66) = true
→ verdict 'none'. A HEPA purifier drives indoor near-zero, so any normal/safe outdoor air (10-15)
exceeds ip+10. Fix: gate the relative guard behind an absolute floor (e.g. only block on op>ip+margin
when op also > ~20), or drop the relative guard entirely since pm_safe=25/pm10_safe=50 already cover
bad air. New low-confidence watch-item: the window-end AL handoff is a SINGLE-shot tick (one tick in
elapsed∈[45,46)); a missed tick (HA restart / scheduler hiccup spanning that minute, or a watch
next_alarm rollover at fire-time pushing elapsed negative mid-window) strands AL in manual_control for
the day (self-heals only on next leave+return). The in-window frames are idempotent; the handoff isn't.

---

Read-only review of the bedroom HA suite on 2026-06-27. The suite is very mature; config validates
clean, all macro tests pass, every automation loads. Two genuine cross-feature issues found (NOT yet
fixed — review-only task):

**1. Wake-ramp end over-dims the room (Medium). [RESOLVED 2026-06-29 — see update above.]** `automation.bedroom_wake_ramp`'s window-end
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
