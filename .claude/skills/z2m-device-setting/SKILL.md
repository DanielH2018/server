---
name: z2m-device-setting
description: Persist a Zigbee2MQTT device setting (Aqara/Hue tuning like FP300 motion_sensitivity, absence_delay_timer, presence_detection_options) via an authenticated MQTT publish. Use when tuning a Zigbee device's behavior. These settings are NOT git-managed and must be re-applied after a re-pair — this skill also reminds you to record them.
allowed-tools: Bash
---

Set a Zigbee2MQTT **device** setting at runtime. Z2M and Mosquitto run in the k3s cluster on
daniel-box; the broker is reachable LAN-wide at the pinned VIP `mqtt_k8s_vip`
(`group_vars/all.yml`) and requires auth. The MQTT clients the script runs come from the
`mosquitto-clients` host package (install it if missing). Run from the repo root on either
host.

**Important:** these settings live on the device / in Z2M's runtime state, **not** in git. A
**re-pair resets them**, so every setting you apply must be recorded (see step 3) to be
reproducible. (Contrast: automations/scenes/scripts ARE git-managed — use `ha-edit-automation`.)

## 1. Choose the setting

The judgement is here: which device (its Z2M friendly name — exact, spaces and all, e.g.
`Aqara FP300`, `Tap Dial`), which option, and which value. The option names are the ones Z2M
exposes for the device (HA shows them as `number`/`select` entities; the Z2M UI lists them
under the device's *Exposes* tab). Every FP300 value applied so far and why is in
`ansible/roles/k8s/home-assistant/CLAUDE.md`.

## 2. Apply and confirm

One script does the four steps — read the broker creds from SOPS, subscribe to the device's
state topic, publish `{"<key>": <value>}` to `zigbee2mqtt/<device>/set`, compare the state Z2M
republishes against what was asked:

```bash
scripts/z2m/set_device_option.sh 'Aqara FP300' motion_sensitivity high
```

A value that parses as JSON is sent as JSON (`60`, `true`), a bare word as a string; to force
a string that looks like a number, quote it yourself (`'"60"'`). The script is a write, so
expect a permission prompt.

Read the exit code, not just the output:

- `0` — the republished state carries the new value. Done.
- `1` — Z2M republished a DIFFERENT value: rejected or coerced. The reason is in
  `kubectl -n homelab logs deploy/zigbee2mqtt --tail=40` (from daniel-box).
- `2` — no state message came back within the window. Unconfirmed, not rejected: a battery
  device (FP300, Tap Dial) applies on its next check-in. Wake it, then re-run the same
  command; the second run's read-back is the confirmation.

If the setting changes what HA sees (e.g. presence hold behaviour), verify downstream with
`ha-verify-state` — e.g. `probe.py ha state binary_sensor.aqara_fp300_presence`.

## 3. Record it (the part people forget)

Document the applied setting + value + rationale in
`ansible/roles/k8s/home-assistant/CLAUDE.md` (the FP300 tuning is already noted there),
so it survives a re-pair and the next person knows it's intentional runtime state, not drift.

## Related
- **Rename a device:** different topic — `zigbee2mqtt/bridge/request/device/rename` with
  `{"from":"<old>","to":"<new>"}`. Renames ARE persisted by Z2M (in its `configuration.yaml`).
- **Network key** is PINNED in the Z2M config — never regenerate it (see the zigbee2mqtt role).
