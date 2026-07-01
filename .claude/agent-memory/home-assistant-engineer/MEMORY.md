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
- [2026-07-01 review findings](ha-review-2026-07-01-findings.md) — winddown_start missing wake_start's stale-past-alarm `>now()` guard (bedtime prompt silently suppressed on stale Wear OS alarm); 06:00 fallback wake not protected from color-track. Both unfixed (review-only).
