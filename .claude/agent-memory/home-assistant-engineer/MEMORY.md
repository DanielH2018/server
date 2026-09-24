# home-assistant-engineer — persistent memory

Index of cross-session learnings for the HA engineer subagent (scope: `project`, so this is
version-controlled). The agent reads the first ~200 lines of this file at the start of every run and
curates it over time. Keep THIS file a concise index; move detail into sibling topic files.

Record only what you *learned the hard way* — device quirks, entity-naming traps, validate/deploy
gotchas, fixes that didn't stick. Do NOT duplicate the role's `CLAUDE.md`/`SETUP.md` (those are the
encyclopedia); link to them instead.

## Learnings
<!-- e.g. - FP300 running-fan presence false-hold → set ai_interference_source_selfidentification ON -->
- [clear_notification pattern](ha-clear-notification-pattern.md) — DELIVERED Android push ≠ persistent_notification; clear via `message: clear_notification` + `data.tag` (repo's first, use notify.mobile_app_pixel_9_pro + continue_on_error). bedroom_away arrive-home push strand FIXED 2026-07-04.
- [UPS Replace-Battery (RB) coverage](ha-ups-replace-battery.md) — M2 done 2026-07-14: RB branch in ups_power_event + new binary_sensor.apc_ups_replace_battery; sensor MUST stay strictly on/off (Prometheus/monitor-bridge contract — don't make it emit unknown).
