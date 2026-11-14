# zigbee2mqtt — Zigbee coordinator bridge (SLZB-06M → MQTT)

Zigbee2MQTT 2.x bridging the network-attached SLZB-06M coordinator into MQTT/Home Assistant.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=zigbee2mqtt` gates the rollout
- **Images:** `ghcr.io/koenkk/zigbee2mqtt` (`zigbee2mqtt_k8s_image`), `alpine`
  (`zigbee2mqtt_k8s_init_image`)
- **Route:** `zigbee2mqtt.<domain>` · `zigbee2mqtt.local.<domain>`, Authelia one_factor
- **Claim:** `zigbee2mqtt-data` (daily -> R2)
<!-- /generated_from -->

- **Host: daniel-box (k8s), since 2026-08-09 — slice 5, B2.** The Docker role this config came
  from is gone; this role's Secret renders `templates/config/configuration.yaml.j2`. Edit Z2M
  config HERE, then merge; `promote` applies it.
- **Port:** 8080 · reaches the coordinator at `tcp://{{ slzb_ip }}:6638`, broker at the
  in-cluster `mosquitto` Service name
- **Flux applies it.** Its `containers_list` entry
  is in `cluster/services.yml`, so nothing applies it by hand, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. mosquitto
  is Flux-owned too, so the
  Kustomization depends on it. The Deployment is `Recreate` and the SLZB-06M coordinator
  accepts one client, so a change that rolls the pod drops every Zigbee device from MQTT until
  the new pod connects.
- **The `zigbee2mqtt-snapshot` Job snapshots `zigbee2mqtt-data` before each revision applies**
  (D5.2a). The zigbee2mqtt Kustomization depends on `zigbee2mqtt-snapshot`, so a revision
  applies only after the snapshot is ready; `docs/flux-deploy-plane.md` lists the Job's checks.
  A failed revision that changes zigbee2mqtt writes the stateful hold, and `promote` does not
  reset `prod`. A volume revert is not the default answer to that hold here: the coordinator's
  NVRAM holds the network key and frame counters, so a reverted volume can silence paired
  devices, which is why `k8s_autodeploy` is false. Weigh a forward fix before the revert runbook in
  `docs/volume-snapshot-drills.md`. `k8s_autodeploy: false` still stops Renovate automerging
  the image bump.
- **The network key and MQTT credentials substitute from `homelab-cluster-vars`.**
  `zigbee_network_key`, `mqtt_username` and `mqtt_password` are `${<key>}` placeholders in the
  published render. Flux substitutes the values from the `homelab-cluster-vars` Secret. A
  rotation reaches the cluster only through `cluster_vars.py write`.
- **Its snapshot and rollout.** The `zigbee2mqtt-snapshot` Job takes the pre-apply snapshot,
  and the Kustomization's `wait` holds the rollout.

## Notable
- **Network coordinator, not USB.** The SLZB-06M is reached as `serial.port: tcp://<ip>:6638`,
  `adapter: ember` (Silabs EFR32 EmberZNet). No `network_mode: host`, no `devices:`.
- **`configuration.yaml` is templated** (`data/configuration.yaml`) and is the source
  of truth — overwritten on deploy. The **Zigbee network identity is pinned**
  (`network_key` from SOPS `zigbee_network_key`, `pan_id`/`ext_pan_id` from host_vars) so a
  redeploy can NEVER regenerate it and un-pair every device. Do not switch these to GENERATE.
- **Device/pairing state is Z2M-owned, NOT templated:** `data/database.db`,
  `coordinator_backup.json`, `devices.yaml`, `groups.yaml`. All on the `zigbee2mqtt-data`
  Longhorn PVC, backed up daily to R2 (the tier is in the generated block above; Kopia
  stopped covering this at the cutover). Losing the PVC = re-pair everything. **Friendly names** (renamed 2026-06-18:
  Lamp / Left Light / Right Light / Tap Dial / Aqara FP300) live here too — set via the Z2M UI or
  the `zigbee2mqtt/bridge/request/device/rename` MQTT request `{"from":"<ieee>","to":"<name>"}`.
  **Per-device settings** (exposed as HA number/select entities) are also Z2M-owned device state —
  e.g. the FP300 presence tuning (`presence_detection_options`/`motion_sensitivity`/`absence_delay_timer`,
  see the home-assistant role CLAUDE.md). Re-apply via `zigbee2mqtt/<name>/set` after a re-pair.
- **Renaming a device keeps its HA entity_ids (sticky, IEEE-based unique_id) but MOVES its raw MQTT
  topic** to `zigbee2mqtt/<new name>`. So a rename is zero-cascade for entities
  referenced by `entity_id` (e.g. the bulbs in the `light.bedroom_lights` group, the Tap Dial's
  `sensor.0x…_battery`) — only consumers of the *raw topic* break. The Tap Dial automation
  (`bedroom_tap_dial_control`) triggers on `zigbee2mqtt/Tap Dial` (the raw topic) and was the one
  edit needed. Entity_ids stay IEEE-based until separately renamed in the HA UI (display names follow
  the Z2M name regardless).
- **HA discovery on** (`homeassistant.enabled: true`) — paired devices auto-appear in HA via
  the MQTT integration; no per-device HA config.
- **Availability tracking on (since 2026-06-18).** `availability.enabled: true` (off by default
  in Z2M 2.x) publishes `online`/`offline` to `zigbee2mqtt/<device>/availability` and adds
  `availability_topic` to the HA discovery configs, so a dropped device's HA entities go
  `unavailable` — consumed by HA's `bedroom_sensor_offline_alert`. `active.timeout` (10 min) =
  mains/routed devices (the Hue bulbs), actively pinged; `passive.timeout` (60 min) = battery
  end-devices (FP300, Tap Dial), judged on last-seen only since pinging a sleeping radio drains it.
  60 min is a STARTING POINT (Z2M default is 1500 min/25h): the Tap Dial self-reports every
  ~7-18 min and the FP300 is very chatty, so 60 min catches a real dropout within the hour without
  false offline alerts. Tune per observed cadence. Verify after deploy (from daniel-box):
  `kubectl -n homelab logs deploy/zigbee2mqtt | grep "/availability'"` should show `online` publishes.
- **Coordinator monitored at the infra level (since 2026-06-18).** A blackbox_exporter
  `tcp_connect` probe of `{{ slzb_ip }}:6638`, the `zigbee2mqtt` target of the `blackbox-extra`
  job in `k8s/roles/observability/templates/prometheus.yaml.j2`, which the
  `ExtraProbeFailed` rule pages. It pages even if
  HA/MQTT are down, catching an `EHOSTUNREACH` like the 2026-06-18 accidental unplug, which a
  pod-level health check misses (Z2M stays running while it crash-loops against an unreachable
  radio). HA layers a second,
  software-side alert on `binary_sensor.zigbee2mqtt_bridge_connection_state` (the home-assistant role's
  `zigbee_bridge_offline` automation, fed by Z2M's MQTT Last-Will).
- **Pairing is closed by default** (no `permit_join` in 2.x). Enable join from the Z2M UI
  (`zigbee2mqtt.<domain>`) when adding devices, then disable.

## Replacing the coordinator

A dead coordinator does not have to mean re-pairing every device. Zigbee2MQTT can form the
same network on a replacement radio from `coordinator_backup.json`. Nobody has run this
procedure yet, so treat the restore behaviour below as read from the source, not observed.

### The coordinator's recorded firmware

The SLZB's own API reported this on 2026-11-14, from `http://<slzb_ip>/ha_info`:

| Field | Value |
|---|---|
| `model` | `SLZB-06MU` |
| `sw_version` (SMLIGHT core firmware) | `v3.3.1`, `fw_channel` release |
| `zb_hw` (radio chip) | EFR32MG21 |
| `zb_version` (Zigbee firmware build) | `20250220` |
| `zb_type` | `0`, coordinator |

The device reports itself as an SLZB-06MU, although this doc calls it the SLZB-06M. The
`zb_version` field is a build date, not an EmberZNet version. The build's EZSP protocol version
was not read, but its range is known. The ember adapter refuses a radio whose EZSP version is
outside 13 to 19, the `EZSP_MIN_PROTOCOL_VERSION` and `EZSP_PROTOCOL_VERSION` constants in
zigbee-herdsman's [`ezsp/consts.ts`][herdsman-consts]. This coordinator runs under Zigbee2MQTT,
so build `20250220` is inside that range. Zigbee2MQTT logs the exact version at startup, and
the frontend's About page shows it; add it to the table the first time someone reads it.
Re-read the API after any firmware update and change this table in the same edit.

[herdsman-consts]: https://github.com/Koenkk/zigbee-herdsman/blob/master/src/adapter/ember/ezsp/consts.ts

### What the restore carries, and what it does not

The zigbee-herdsman ember adapter writes the backup (`EmberAdapter.backup`). It holds the PAN
ID, extended PAN ID, channel, network key, the network key's frame counter, the hashed trust
centre link key and the old coordinator's IEEE address. It does not carry these:

- **The coordinator's IEEE address on the new radio.** The restore stores the old address but
  never writes it. The new radio keeps its own address, and Zigbee2MQTT logs
  `Coordinator address changed`. Zigbee2MQTT's FAQ says to copy the old adapter's IEEE address
  into the new one when moving to another adapter. Whether the paired devices keep working
  without that copy has not been tested here.
- **A frame counter increment.** The restore passes the backup's `network_key.frame_counter`
  to `ezspSetNWKFrameCounter` unchanged, in `formNetwork` in zigbee-herdsman's
  [`emberAdapter.ts`][herdsman-ember]. A device drops a frame whose counter is not above the
  last one it saw from the coordinator. Zigbee2MQTT writes the backup at startup only when the
  file is missing, daily, and on a clean stop. A dead coordinator gets no clean stop, so the
  backup can be up to a day behind.

  **The backup's age sets no safe threshold; the frames the coordinator sent after it do.**
  Without an offset, a backup of any age leaves the new coordinator below the counter that
  devices last saw. The devices then ignore it until it has sent that many frames again,
  which can take as long as the backup is old. With an offset larger than those frames, age
  does not matter. Procedure step 2 therefore raises the counter before the restore. The
  counter is 32-bit, and the adapter warns only above `0xfeeeeeee` (about 4.0 billion), so
  a generous offset costs nothing.

- **Device tables.** The backup holds no child or routing table, and its per-device link keys
  are empty with Zigbee2MQTT's defaults. Devices re-announce and routes rebuild on their own.
  A battery device that stays silent may need a button press or a power cycle.
- **The SLZB's own settings.** Its IP address, coordinator mode and port 6638 live on the
  device, not in the backup.

The adapter restores only when every one of these holds; otherwise it forms a new network
from config and logs `Config does not match backup`:

- The new radio is on no network, or on one whose PAN ID, extended PAN ID or key differs from
  config, which the adapter then leaves.
- The backup's PAN ID, extended PAN ID and network key match config. They do here because
  config pins all three (`network_key`, `pan_id` and `ext_pan_id` in
  `templates/config/configuration.yaml.j2`).
- The backup's channel is in config's channel list. Config sets none, so Zigbee2MQTT's default
  list applies.
- The backup file records EZSP version 12 or later (`BACKUP_OLDEST_SUPPORTED_EZSP_VERSION`).
  The adapter renames an older file to `.old` and forms fresh. This floor applies to the
  backup file. The radio itself must still run EZSP 13 to 19, as the firmware section says.

A fresh network means every device has to rejoin, and the new radio is then on that network.
If that happens, stop and read the log before trying again.

[herdsman-ember]: https://github.com/Koenkk/zigbee-herdsman/blob/master/src/adapter/ember/adapter/emberAdapter.ts

### Procedure (operator-only)

1. **Keep the backup.** Copy `coordinator_backup.json` and `database.db` off the
   `zigbee2mqtt-data` volume before anything starts the pod against the new radio. If the
   volume is also gone, restore it from its R2 backup first (`docs/longhorn-disaster-recovery.md`).
2. **Raise the frame counter in the copy you restore from.** Add an offset to
   `network_key.frame_counter` in `coordinator_backup.json`, then put the file back on the
   volume. To size the offset, compare that field with the same field in an older backup of
   the volume, such as the previous day's R2 backup. Use at least ten times the daily
   growth, and never less than 100000.
3. **Power off the old coordinator** if it still runs. The radio accepts one client, and two
   radios on the same PAN conflict.
4. **Flash the replacement** to the firmware in the table above: the same SMLIGHT core
   firmware and the same Zigbee build `20250220`, in coordinator mode. That build is known to
   be in the EZSP 13 to 19 range the adapter accepts. If that build is unavailable, use a
   build whose release notes state an EZSP version in that range. A build outside the range
   stops Zigbee2MQTT at startup with `Adapter EZSP protocol version (...) is not supported by
   Host`.
5. **Copy the old IEEE address onto the new radio**, following Zigbee2MQTT's
   "copy the IEEE address" guide. The address is `coordinatorIeeeAddress` in the backup.
6. **Give the replacement the old address and port.** Either reserve `10.0.0.127` for it on
   the LAN and set its Zigbee TCP port to 6638, or change `slzb_ip` in
   `inventory/group_vars/all.yml`. That variable feeds both the Zigbee2MQTT config and the
   blackbox probe in `k8s/roles/observability/templates/prometheus.yaml.j2`, so changing it
   is a merged change.
7. **Let the pod reconnect.** Zigbee2MQTT exits when it cannot reach the coordinator, and the
   kubelet restarts it. Once the new radio answers on port 6638, the next start connects with
   no manual restart. The log should read `Forming from backup` and then `restored`.
8. **Check the devices.** Each one should report within its availability timeout. Toggle a
   bulb and press the Tap Dial. A device that stays offline may need a power cycle, and
   re-pairing it is the last resort.

Nobody has run this procedure onto a spare adapter. The first run should record its date
here, together with how many devices needed attention.

## Editing
- Z2M cfg: `templates/config/configuration.yaml.j2` (rendered into the k8s Secret by `k8s/roles/zigbee2mqtt/`)
