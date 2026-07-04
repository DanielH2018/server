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
  `clear_notification` is a no-op when nothing's showing (safe to fire unconditionally). Always add
  `continue_on_error: true` — a re-paired phone renames the `notify.mobile_app_*` slug → ServiceNotFound
  would abort the rest of the automation (same rationale as `bedroom_notify`'s pushes, scripts.yaml ~636-640).

**Repo state (2026-07-04):** this was the repo's FIRST `clear_notification` — grep found none before.
`notify.mobile_app_pixel_9_pro` is the established phone notify service (used by `bedroom_notify`
scripts.yaml:641 + flush_held automations.yaml:1362). `bedroom_notify` is NOT the right tool for a
clear (it always sends a title/message push + runs away-hold logic) — call the notify service directly.

**bedroom_away recovery, now closed:** the away "🏠 Left on" push (tag `bedroom_away`, `critical_away:
true` so it bypasses the hold and lands as a real push) only cleared on a "Turn back on" tap. A genuine
away (you don't tap) stranded it. `automation.bedroom_arrive_home` already dismissed the *persistent
note* (added earlier by the maintainer); the remaining strand was the *phone push*. Added the
`clear_notification` above right after the dismiss in `bedroom_arrive_home` (before its fan/light resume,
which is untouched). Deployed + all 33 automations loaded; can't live-fire without a real person.daniel→home
GPS transition. See [[ha-review-2026-07-04-findings]] for the sibling (still-open) orphan class: the
`bedroom_notify` away-hold recovery branch orphans bare-`tag` notes for watch-tier alerts (different bug).
