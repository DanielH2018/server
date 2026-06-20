---
name: z2m-device-setting
description: Persist a Zigbee2MQTT device setting (Aqara/Hue tuning like FP300 motion_sensitivity, absence_delay_timer, presence_detection_options) via an authenticated MQTT publish. Use when tuning a Zigbee device's behavior. These settings are NOT git-managed and must be re-applied after a re-pair — this skill also reminds you to record them.
allowed-tools: Bash
---

Set a Zigbee2MQTT **device** setting at runtime. Z2M (and Mosquitto) run on `daniel-server`;
the broker requires auth (`allow_anonymous false`). Run from `/home/ubuntu/server`.

**Important:** these settings live on the device / in Z2M's runtime state, **not** in git. A
**re-pair resets them**, so every setting you apply must be recorded (see step 4) to be
reproducible. (Contrast: automations/scenes/scripts ARE git-managed — use `ha-edit-automation`.)

## 1. Publish the setting

Creds are in SOPS (`mqtt_username` / `mqtt_password`). Decrypt them into shell vars so they
never sit in the command literal or history, then publish to `zigbee2mqtt/<Friendly Name>/set`:

```bash
u=$(sops -d --extract '["mqtt_username"]' ansible/vars/secrets.yml)
p=$(sops -d --extract '["mqtt_password"]' ansible/vars/secrets.yml)
docker exec mosquitto mosquitto_pub -h localhost -u "$u" -P "$p" \
  -t 'zigbee2mqtt/Aqara FP300/set' -m '{"motion_sensitivity": "high"}'
```

- The friendly name is the device's Z2M name (e.g. `Aqara FP300`, `Tap Dial`) — exact, spaces
  and all, single-quoted.
- One JSON object; multiple keys are fine: `-m '{"motion_sensitivity":"high","absence_delay_timer":60}'`.
- `$(...)` forces a permission prompt (and this is a write) — expected. Minor caveat: `-P "$p"`
  is visible in `ps` on the host for the instant the exec runs (single-user box; accepted, and
  it's the repo's documented recipe).

## 2. Verify it applied

Z2M republishes the device's state after a `/set`. Capture one message to confirm the new value
is reflected:

```bash
u=$(sops -d --extract '["mqtt_username"]' ansible/vars/secrets.yml)
p=$(sops -d --extract '["mqtt_password"]' ansible/vars/secrets.yml)
docker exec mosquitto mosquitto_sub -h localhost -u "$u" -P "$p" \
  -t 'zigbee2mqtt/Aqara FP300' -C 1
```

If the value isn't in the payload, check Z2M logs for a rejected/unknown option:
`uv run python scripts/probe.py loki-query '{container="zigbee2mqtt"}'` or
`docker logs --tail 40 zigbee2mqtt`. (Battery devices may need a wake/poll before the change
takes — some Aqara settings apply on the next check-in.)

## 3. Confirm the HA-visible effect (if relevant)

If the setting changes what HA sees (e.g. presence hold behavior), verify downstream with
`ha-verify-state` — e.g. `probe.py ha state binary_sensor.aqara_fp300_presence`.

## 4. Record it (the part people forget)

Document the applied setting + value + rationale in
`ansible/roles/containers/home-assistant/CLAUDE.md` (the FP300 tuning is already noted there),
so it survives a re-pair and the next person knows it's intentional runtime state, not drift.

## Related
- **Rename a device:** different topic — `zigbee2mqtt/bridge/request/device/rename` with
  `{"from":"<old>","to":"<new>"}`. Renames ARE persisted by Z2M (in its `configuration.yaml`).
- **Network key** is PINNED in the Z2M config — never regenerate it (see the zigbee2mqtt role).
