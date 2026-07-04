---
name: ha-review-2026-07-04-findings
description: 2026-07-04 review-only findings — bedroom_notify away-recovery orphans the home-created visibility-backstop persistent note (Medium); SETUP.md 31→33 count drift (Low). Suite otherwise clean.
metadata:
  type: project
---

Read-only audit 2026-07-04 (after 138ab9c5 recorder churn cut + f4df6039 notify-rename fallback).
Baseline clean: `ha verify-automations` = all 33 loaded; validate_ha_config OK; macro tests pass;
error_log only benign WARNINGs (dreo no-ack, sqlite unclean-shutdown, untested custom integrations).
All dashboard/customize/automation entity refs resolve live. All 10 config files OR'd into
common_config_changed (no deploy-wiring gap). Z2M availability on (offline-alert dep satisfied).

**NEW Medium — away-recovery orphans the visibility-backstop persistent note.**
`script.bedroom_notify` (scripts.yaml ~612-627). The away-hold recovery branch dismisses only
`hold_{tag}` then `stop`s — it never dismisses the bare `{tag}` note that the visibility-fallback
(scripts.yaml ~668-680) creates for watch/pierce/critical_away tiers. So the sequence: elevated
alert fires while HOME (bare `{tag}` note created) → you leave → condition recovers while AWAY →
recovery routes to the held path → dismisses `hold_{tag}` (absent, no-op) and stops → bare `{tag}`
note is left stranded in the HA UI showing a "bad" state that already cleared. Self-heals only on a
later at-home same-tag recovery. Affected (watch-tier, non-critical_away, recovery via notify):
airquality moderate (indoor+outdoor), sensor_offline, ups_power (outage+low-batt, shared tag),
zigbee_bridge. NOT temperature (critical_away recovery bypasses the held path and DOES dismiss `tag`)
and NOT severe-AQ (threshold automation's recovery else-branch dismisses its note directly). The
maintainer already special-cased the two analogous cases (severe-AQ else-dismiss; bedroom_away's note
via arrive_home) — this residual class is the un-covered one. Fix: add
`persistent_notification.dismiss(notification_id: "{{ tag }}")` next to the `hold_{{ tag }}` dismiss in
the away-hold recovery branch (dismiss of absent id no-ops, so safe). UNFIXED (review-only).

**Low — SETUP.md automation count drift.** SETUP.md:144 still says "the suite has 31 in total";
actual = 33 (SETUP.md:76 was corrected by 9eb6c2cc, :144 missed). Carry-over from 07-02; partial fix.

Carry-over Lows (cite only, all still open): bedtime prompt skipped when alarm set AFTER its own
winddown time (future alarm, alarm−8h already past → winddown available-but-past → dynamic `at:`
unfireable + 22:30 fallback suppressed; automations.yaml ~1436); fallback-wake while-loop dies on HA
restart (~632); wake_fallback branch doesn't arm expected_color_temp (scripts.yaml ~93-100).

Non-findings confirmed this run (don't re-flag): AL-stuck-manual on manual-off-during-wake (known,
self-heals via away-sweep); recorder automation/script domain excludes don't break restore_state or
last_triggered (both live/independent of history); display show/dismiss pair has a clean 50–60 lux
deadband and no re-cast fight; bedroom_display cast view_path `bedroom` matches ui-lovelace.
