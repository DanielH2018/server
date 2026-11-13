---
name: ha-clear-notification-pattern
description: How to clear a DELIVERED Android push in this repo (message clear_notification + data.tag), why persistent_notification.dismiss isn't enough, and the bedroom_away arrive-home fix.
metadata:
  type: project
---

Clearing a bedroom alert has TWO surfaces, and they need TWO different calls:
- **HA UI persistent_notification** (the `critical_away`/pierce/watch visibility fallback, id = `tag`):
  cleared by `persistent_notification.dismiss` with `notification_id: <tag>`.
- **Delivered Android phone push** (companion app, carried `data.tag: <tag>`): NOT touched by
  `persistent_notification.dismiss`. Clear it by re-sending the SAME notify service a magic message:
  ```yaml
  - service: notify.mobile_app_pixel_9_pro
    continue_on_error: true
    data:
      message: clear_notification
      data:
        tag: <tag>
  ```
  `clear_notification` is a no-op when nothing's showing (safe to fire unconditionally). Put it LAST in
  its sequence. `continue_on_error: true` covers a failed delivery only: a re-paired phone renames the
  `notify.mobile_app_*` slug, and HA re-raises the resulting ServiceNotFound even under
  `continue_on_error`, so it aborts every step after the notify (#8203).
  `tests/test_notify_runs_after_state_changes.py` fails on a state change placed after any notify.

**Repo state (2026-07-04):** this was the repo's FIRST `clear_notification` — grep found none before.
`notify.mobile_app_pixel_9_pro` is the established phone notify service (used by `bedroom_notify`
files/scripts/alerts.yaml + `bedroom_flush_held_notifications` in files/automations/alerts.yaml). `bedroom_notify` is NOT the right tool for a
clear (it always sends a title/message push + runs away-hold logic) — call the notify service directly.

**bedroom_away recovery, now closed:** the away "🏠 Left on" push (tag `bedroom_away`, `critical_away:
true` so it bypasses the hold and lands as a real push) only cleared on a "Turn back on" tap. A genuine
away (you don't tap) stranded it. `automation.bedroom_arrive_home` already dismissed the *persistent
note* (added earlier by the maintainer); the remaining strand was the *phone push*. Added the
`clear_notification` above to `bedroom_arrive_home`; since #8203 it runs after the fan/light resume. Deployed + all 33 automations loaded; can't live-fire without a real person.daniel→home
GPS transition. The sibling orphan class — `bedroom_notify`'s away-hold recovery branch leaving
bare-`tag` visibility notes stranded for watch-tier alerts (a different bug from this one) — was
fixed the same day: it now dismisses both `hold_{{ tag }}` and `{{ tag }}`
(`k8s/roles/home-assistant/files/scripts/alerts.yaml`).
