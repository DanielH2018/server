# home-assistant-engineer — persistent memory

Index of cross-session learnings for the HA engineer subagent (scope: `project`, so this is
version-controlled). The agent reads the first ~200 lines of this file at the start of every run and
curates it over time. Keep THIS file a concise index; move detail into sibling topic files.

Record only what you *learned the hard way* — device quirks, entity-naming traps, validate/deploy
gotchas, fixes that didn't stick. Do NOT duplicate the role's `CLAUDE.md`/`SETUP.md` (those are the
encyclopedia); link to them instead.

## Learnings
<!-- e.g. - FP300 running-fan presence false-hold → set ai_interference_source_selfidentification ON -->
- [2026-06-27 review findings](ha-review-2026-06-27-findings.md) — wake-ramp end over-dims (contaminated lux); window advisor disabled by purifier keeping indoor PM low (op>ip guard). Both unfixed (review-only).
- [2026-07-01 review findings](ha-review-2026-07-01-findings.md) — winddown_start stale guard (FIXED 4ab99299); fallback-wake vs color-track (FIXED 2026-07-04, see below).
- [2026-07-04 fixes](ha-fixes-2026-07-04.md) — bedtime-prompt two-part suppression escape (winddown `>now()` + prompt `<now()`, complementary); fallback-wake rewritten STATELESS (time_pattern /1, restart-safe, own START-tick setup, no_real_wake guards every branch); winddown comment; color-track fallback suppression. Deployed + load-verified. Fallback can't live-fire-verify on weekend/armed-alarm.
- [2026-07-02 review findings](ha-review-2026-07-02-findings.md) — wake_start 2h grace (36b46123) verified correct+live; rule: an availability guard anchoring an in-flight window must outlive the WINDOW, not the anchor; al_startup_suppress Med FIXED same day (128015a6, verified); grace shifts fallback_wake's date-compare defer (Low, likely accept); AL manual_control non-empty is NORMAL post-apply.
- [2026-07-04 review findings](ha-review-2026-07-04-findings.md) — Med: bedroom_notify away-recovery orphans the home-created bare-`tag` visibility note (dismisses only `hold_tag`); affects airquality/sensor_offline/ups/zigbee. Low: SETUP.md 31→33 drift. Suite otherwise clean.
- [clear_notification pattern](ha-clear-notification-pattern.md) — DELIVERED Android push ≠ persistent_notification; clear via `message: clear_notification` + `data.tag` (repo's first, use notify.mobile_app_pixel_9_pro + continue_on_error). bedroom_away arrive-home push strand FIXED 2026-07-04.
