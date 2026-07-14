---
name: ha-ups-replace-battery
description: UPS Replace-Battery (RB flag) coverage — new binary_sensor + ups_power_event branch; the strictly-on/off invariant is a cross-role contract with monitor-bridge
metadata:
  type: project
---

The APC UPS's periodic self-test "Replace Battery" verdict (NUT sets `RB` in `ups.status` →
`sensor.apc_ups_status_data`, a space-separated flag list) is covered on BOTH alert channels
(homelab-review finding M2, done 2026-07-14).

- **HA-mobile-push channel:** `automation.ups_power_event` (files/automations.yaml) gained `was_rb`/
  `is_rb` vars and two `choose:` branches mirroring the OB/LB pattern — RB newly present → watch buzz
  (NOT pierce; it's maintenance, not an imminent cut), RB cleared → routine `recovery: true`. Same
  `ups_power` coalescing tag.
- **Kuma→Discord channel:** monitor-bridge `check_ups` reads a NEW template
  `binary_sensor.apc_ups_replace_battery` (files/templates.yaml, device_class problem) via its
  Prometheus scrape (`UPS_REPLACE_QUERY` / `hass_binary_sensor_state`).

**Load-bearing invariant (don't "fix" it):** the binary_sensor's state expression
`{{ 'RB' in (states('sensor.apc_ups_status_data') or '').split() }}` is deliberately STRICTLY on/off
— it falls back to `off` when the source is unavailable/unknown, never emits `unknown`. Reason:
HA's Prometheus exporter only emits `hass_binary_sensor_state` for a strictly-binary sensor, so its
ABSENCE from the scrape means the whole HA scrape is down (monitor-bridge's check_ups treats that as
a defer, not a silent single-arm RB drop). Making it return `unknown` on source-unavailable would
drop it from the scrape whenever NUT blips and defeat that contract. Verified live 2026-07-14:
sensor `off` with source `OL`, both `last_changed` post-deploy (genuine live eval, not stale).
