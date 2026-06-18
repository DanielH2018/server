# HA update-available digest

**Date:** 2026-06-18
**Status:** Approved — implementing
**Area:** Home Assistant (`ansible/roles/containers/home-assistant`)

## Problem

Surface pending device/integration updates that Renovate/IaC doesn't cover — Zigbee/sensor firmware
(Tap Dial, Hue bulbs, FP300, AirGradient) and HACS integrations (Adaptive Lighting, DREO, HACS).
A low-noise weekly digest, **notify-only — never auto-flash** firmware.

## Goals / decisions

- **Generic over all `update.*` entities** (not a hardcoded list) — a newly paired Zigbee device's
  update entity joins automatically. (Note: LSIO container HA has no `update.home_assistant_*`
  entity — the HA image is updated via the repo's Renovate/Watchtower path, not in-HA.)
- **Weekly digest:** Sunday 10:00, one notification listing every `update.*` that is `on`, **only if
  any exist**. Routine severity (informational) via `script.bedroom_notify`.
- **Notify-only:** no `update.install` — Zigbee firmware is flashed manually/deliberately.

## Architecture

### Single `automation: update_available_digest` (`automations.yaml`)

- Trigger: `time` at `10:00:00`; conditions: `now().weekday() == 6` (Sunday) **and** at least one
  `update.*` is `on`.
- Action: build a multi-line `message` from the pending update entities
  (`states.update | selectattr('state','eq','on')`) — each line `• {{ name }} ({{ installed }} →
  {{ latest }})` — then `script.bedroom_notify` (title `📦 N update(s) available`, `tag:
  update_digest`, routine — no watch/pierce/actions).

Named without the `bedroom_` prefix (it's homelab-wide, not bedroom-specific) but routes through the
same `bedroom_notify` layer.

## Edge cases

- **No updates pending:** the count condition gates the whole automation — no empty digest.
- **Opaque Zigbee versions / IEEE friendly names** (e.g. `0x001788…  Update`): shown as-is until the
  device is renamed in Z2M (separate backlog note) — the digest is still informative (a version
  delta means a real update).
- **Routine + quiet:** delivered silently if you happen to be in DND at 10:00 Sunday — fine.

## Testing (manual)

- Before deploy: HA Developer Tools → YAML → Check Configuration.
- After deploy: confirm `automation.update_available_digest` loads. Functional: it can be run
  manually (Developer Tools → run the automation) — with the Tap Dial update currently `on`, expect
  a digest listing it. (The real schedule is Sunday 10:00.)

## Files touched

- `ansible/roles/containers/home-assistant/files/automations.yaml` — the digest automation
- `ansible/roles/containers/home-assistant/CLAUDE.md` — document it
- `ansible/PLANS.md` — move the item to done

## Future / out of scope

- Per-update actionable "Install" buttons (deliberately omitted — notify-only for firmware).
- Splitting firmware vs HACS into separate digests.
