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
- [2026-07-01 review findings](ha-review-2026-07-01-findings.md) — winddown_start stale guard (FIXED 4ab99299); fallback-wake vs color-track (still open, Low).
- [2026-07-02 review findings](ha-review-2026-07-02-findings.md) — wake_start 2h grace (36b46123) verified correct+live; rule: an availability guard anchoring an in-flight window must outlive the WINDOW, not the anchor; al_startup_suppress Med FIXED same day (128015a6, verified); grace shifts fallback_wake's date-compare defer (Low, likely accept); AL manual_control non-empty is NORMAL post-apply.
