---
name: ha-review-2026-07-01-findings
description: 2026-07-01 review-only findings — winddown_start missing the stale-past-alarm guard (bedtime prompt suppressed), and the 06:00 fallback wake isn't protected from color-track
metadata:
  type: project
---

Read-only review of the bedroom HA suite on 2026-07-01 (after commits through 8382920b). Suite is
mature: 33 automations (32 + the just-added bedroom_fallback_wake), validate_ha_config clean, all
macro tests pass, ventilation macro is absolute-PM-only. Two genuine findings (UNFIXED — review-only):

**1. (Medium) `sensor.bedroom_winddown_start` never got the stale-past-alarm `> now()` guard that
`bedroom_wake_start` got in cde01b72.** templates.yaml:41 availability lacks `and as_datetime(na) >
now()` (wake_start:27 has it). A Wear OS watch that holds an already-fired morning alarm all day
(the exact quirk that motivated cde01b72, observed 2026-07-01) leaves winddown_start = stale_alarm −
8h = a PAST timestamp, still `available`. Then bedroom_bedtime_prompt's dynamic `at:` trigger can't
fire (past time) AND its 22:30 fallback is suppressed by its own condition
(`winddown in ['unknown','unavailable']` is false because the value is stale-but-present) →
NO bedtime prompt at all that night. Fix: mirror wake_start — add `and as_datetime(na) > now()` to
winddown_start's availability so a stale alarm makes it unavailable and the 22:30 fallback fires.

**2. (Low) The 06:00 fallback wake (`bedroom_fallback_wake`) is not protected from
`bedroom_color_track` the way the real ramp is.** color_track's "not during wake ramp" guard
(automations.yaml:696-700) keys off `sensor.bedroom_wake_start`, which is `unavailable` during the
fallback (that's the whole trigger for the fallback), so `in_wake_window(-1)` = False → the guard
doesn't block. Also the `wake_fallback` branch of bedroom_lights_set (scripts.yaml:93-100) sets
2200K but never arms `input_number.bedroom_light_expected_color_temp`. So whether color_track drifts
the fixed-warm fallback ramp toward AL's cool morning color depends nondeterministically on the last
expected_color_temp value (color_track's "still auto" ±150K gate). Fix: arm expected_color_temp in
the wake_fallback branch, or gate color_track additionally on `input_datetime.bedroom_last_wake`
freshness. Low impact (fallback is a rare path; often self-mitigated by the ±150K gate).

Minor/accepted: the fallback wake releases AL + sends "Good morning, wake ramp ran" UNCONDITIONALLY
after the while-loop, even if you cancelled it via manual_off mid-ramp (cosmetic). AL-stranded-in-
manual-control if manual_off is engaged during the REAL wake window is the known/self-healing edge
(see [[ha-review-2026-06-27-findings]]).
