---
name: review-2026-08-16c-findings
description: "HA-domain review ledger (PR #218) — 10 findings fixed and deployed, 1 refuted, 1 deliberately excluded; what the next /ha-review must not re-flag"
metadata:
  node_type: memory
  type: project
  modified: 2026-08-16T17:15:58.387Z
  originSessionId: 485854be-b613-4836-b831-3e74605e737e
---

`/ha-review` of `roles/k8s/home-assistant/`, 2026-08-16. Reviewer on opus, one sonnet
skeptic per High/Medium. 11 findings: 3 High + 1 Medium confirmed, 1 Medium refuted,
6 Low. Fixed in PR #218 (merged `c7d9472f`), deployed 17:06 UTC — pod 1/1, limit 2Gi live,
33/33 automations loaded, error log clean.

**Fixed — do not re-flag.** Phone DND repointed to
`sensor.pixel_watch_3_do_not_disturb_sensor` (on-body, better "asleep" proxy).
Sleep-duration paths **deleted, not repaired** — no source exists on any device, so
`wake_brightness` lost its `sleep_min` arg (arity now pinned by a test), the latch writes,
the `input_number` helper and the "you slept N h" note are all gone. Rebuilding short-night
softening means a locally-derived signal (`binary_sensor.pixel_watch_3_bedtime_mode`'s
last_changed), never another cloud sensor. `update_available_digest` fixed — the
`selectattr` now runs inside each expression. `probe.py ha verify-automations` and
`ha_state_model.py refresh` both repointed. Memory limit 1536Mi → 2Gi. `custom_templates/`
now cleared before reinstall. Test-scenario picker re-arms to `off`/`fast` at end of run.
`trusted_proxies` trimmed to `10.42.0.0/16`. Corrected: fan-curve constants, Tap Dial
button numbers (B2=Sleep, B4=Fan since the 2026-07-18 remap), dashboard header, and the
`bridge_hostname` claim — that key now exists **only in comments** repo-wide.

**Refuted — do not resurrect.** "The colour tracker re-pauses Adaptive Lighting, undoing
the wake-ramp hand-back." The mechanism is real (`take_over_control: true`, no
`autoreset_manual_control`, bare `light.turn_on` every 5 min) but the claimed *loss* is
not: `docs/lighting-and-presence.md:96-99` says brightness comes from the
`natural_brightness` ambient macro and AL supplies colour only, and AL keeps computing
colour while taken over. AL's own brightness is applied once, at release. Nothing is lost.

**Still open, deliberately.** Nest Hub (`media_player.bedroom_display` unavailable since
2026-08-03, watched by nothing) — Daniel excluded it; his call whether to add it to
`bedroom_sensor_offline_alert` or retire the two display automations.

**Unproven by design.** The digest fix cannot be observed until a Sunday 10:00 trigger;
`ha_runtime_error_alert` is the detector if it regresses (it is how the bug surfaced).

Two structural lessons came out of this and have their own entries — both were confirmed
by a second independent occurrence, not one run's say-so:
`ansible/roles/k8s/home-assistant/CLAUDE.md`, [[argparse-only-test-hid-a-dead-path]].
