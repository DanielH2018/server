---
name: ha-review-2026-07-02-findings
description: 2026-07-02 review of the hot wake_start 2h-grace fix (36b46123) + fallback-wake home gate (f87f8e60) — grace is correct/live; residual edges in al_startup_suppress and the bedtime-prompt fallback
metadata:
  type: project
---

Review-only audit 2026-07-02 (after 36b46123, committed minutes before the run). Both prior 07-01
findings resolved or carried: #1 winddown stale guard FIXED (4ab99299); #2 (wake_fallback doesn't arm
expected_color_temp) STILL OPEN, Low.

**The 36b46123 grace fix is CORRECT and was LIVE at review time** (deployed copy byte-matched git;
container recreated 13:07Z, commit 13:12Z — operator deployed then committed). Verified the math:
guard alive until alarm+120 min ⇒ elapsed ≤ 135, covering the [45,90) release window with 45 min
margin. All other wake_start consumers are window-bounded (in_wake_window) or date-compared, so the
2h-stale-but-available state is benign for them. Key learning: **an availability guard on a sensor
that anchors an in-flight window must outlive the WINDOW, not the anchor event** — bare `> now()`
killed the ramp at elapsed=15 the first real morning (2026-07-02). End-to-end proof deferred to the
next real alarm morning (check AL manual_control empties within [45,90) after the alarm).

Findings (UNFIXED — review-only):
1. (Med) `bedroom_al_startup_suppress` gates on `wake_start in [unknown,unavailable]` — an
   availability proxy for "wake context". The grace widens the skip-span to alarm+2h (was already
   over-broad pre-alarm: skipped from alarm-set evening onward). A deploy <2h post-alarm with the
   room empty now leaves AL's startup self-on burning with NO off-path (absence_off needs a fresh
   presence off-edge). Fix: compose `in_wake_window(elapsed)` like bedroom_auto_light_allowed.
2. (Low) Bedtime prompt: an alarm set AFTER its own winddown time (alarm−8h already past, e.g.
   tomorrow-06:00 alarm created at 22:10) → dynamic `at:` unfireable AND the 22:30 fallback
   suppressed (sensor available) → no prompt that night. Fix direction: fallback also fires when
   winddown is PAST, guarded on bedroom_bedtime_prompt.last_triggered not today-evening.
3. (Low) `bedroom_fallback_wake`'s while-loop dies on HA restart (unlike the real ramp's stateless
   ticks) — mid-window deploy on a fallback day strands lights at the last frame + AL paused with no
   release path that day (wake_start unavailable). Self-heals on leave+return.
4. (Low, cosmetic) f87f8e60 gated the closing AL-release+notify on person-home but not on the loop
   actually completing — a manual_off cancel still gets "wake ramp ran".

templates.yaml:41-44 comment drift: winddown still says "Same ... guard as bedroom_wake_start
(`> now()`)" — no longer true; the asymmetry is CORRECT (winddown is consumed 8h before the alarm,
needs no grace) but undocumented, inviting a future "fix" in either direction.

Non-finding worth remembering: **AL manual_control being non-empty is the NORMAL post-apply state**
(set_natural_brightness's explicit brightness marks take-over) — it only signals a stranded hand-off
if it persists past the release window on a wake morning. Don't flag it raw.
