# k3s Slice 5 — Smart Home

**Status:** B3 prereqs done + role built-and-held, 2026-08-09 ~02:55 UTC — upsd published on
daniel-server's LAN IP behind a DOCKER-USER lock (daniel-box + local bridges only; a bare
dport DROP would have severed HA's current container-to-container path), HA's NUT integration
repointed via reconfigure flow and reading OL on the new path, `trusted_proxies` carries the
cluster chain, AirGradient entry is `source: user` (manual host — final check at cutover).
`roles/k8s/home-assistant` is committed but held by `home_assistant_k8s_enabled: false` (the
B4a pattern). The cutover itself (flip the flag + bridge_hostname + daniel-server entry
removal + stop/seed/start + the re-plumb inventory) waits for a daytime window.
B2 executed 2026-08-09 ~02:15 UTC — z2m runs in the cluster: PVC seeded from the
stopped Docker copy (checksum-verified), ember/SLZB reconnected to the same network, all
devices publishing with their tuned settings intact, FP300 live in HA, zero re-interviews.
The mesh gap ran ~17 min, not the target 5: the seed's ssh calls plus operator ssh tripped
daniel-server's 6/30s rate limiter twice (space them next time), and `slzb_ip`/PAN vars had
to move host_vars → group_vars mid-cutover (z2m renders on daniel-box now). Docker z2m
container + rendered compose removed; Kopia sentinel + restore-drill rotation dropped z2m
(guard tests enforced both); Kuma re-plumbed (VIP port probe, z2m -k8s route, SLZB monitor
moved). Rollback = re-add the daniel-server entry + deploy (data dir still on disk).
B1 executed through the client repoints, 2026-08-09 ~01:25 UTC — cluster broker
deployed and gated (VIP round-trip from daniel-server), HA repointed via the reconfigure flow
(`reconfigure_successful`, connection validated before commit), z2m repointed and reconnected
(SLZB/ember link up, bridge online). Docker mosquitto still runs, clientless, pending the soak
+ retirement in step 3. One finding for the record: mosquitto 2.1's password-file plugin
refuses the pwfile straight off a Secret mount — an init container copies it into a pod-owned
emptyDir (see the deployment template's comment). B2/B3 not started. Plan written 2026-08-08
from measured state.

Design doc §8 gives this slice one line: *"Smart home: mosquitto, zigbee2mqtt, home-assistant —
Zigbee mesh intact (PAN identity preserved); HA automations firing."*

The blocking question — where is the Zigbee radio — turns out to already be answered in this
repo's favor: **the coordinator is an SLZB network device** (`tcp://10.0.0.127:6638`, ember), not
a USB stick. Nothing in this slice touches hardware, so the whole move is software and it can run
before daniel-server joins the cluster. The three services move as a chain, not a unit: HA and
zigbee2mqtt are both *clients* of mosquitto, and zigbee2mqtt's HA integration flows through MQTT
discovery — so the broker is the seam, and it moves first.

---

## Baseline — measured 2026-08-08

| What | Measured |
|---|---|
| home-assistant `/config` | **140 M** total, 71 M of it the SQLite recorder DB |
| zigbee2mqtt data | **63 M** (`database.db`, `devices.yaml`, `coordinator_backup.json`, config) |
| mosquitto config+data | **516 K** |
| Coordinator | SLZB at `10.0.0.127:6638` (`slzb_ip`), ember adapter, TCP — no USB |
| Zigbee PAN identity | `network_key` / `pan_id` / `ext_pan_id` **pinned in SOPS** and templated into z2m's config — survives any data move by construction |
| MQTT clients | Exactly three: z2m, HA, and mosquitto's own healthcheck. Broker publishes **no host port** (Docker `mqtt` isolation net only) |
| HA integrations with host coupling | MQTT (broker host in `.storage`, UI-configured), NUT (`nut` sidecar via `ups` net; upsd published on `127.0.0.1:3493` only), AirGradient (LAN device), dreo (cloud), companion app (Cloudflare route) |
| MetalLB pool | `10.0.0.241-250`; `.241` = jellyfin-lan. Pinned-VIP LoadBalancer pattern exists (`service-lan.yaml.j2`) |

**What makes this slice different from 4:** the data is tiny and the paths are simple — but two of
the three services are *single-client* or *single-writer* against something external. Only one
z2m may hold the SLZB's TCP session; only one HA may own the recorder DB and answer MQTT
discovery. There is no shadow-running the stateful pair — each cutover is stop-A, delta-sync,
start-B. And the blast radius is the bedroom: lights, fan, wake ramp, alerts.

---

## Decisions

### D1 — Broker first, then z2m, then HA

mosquitto is stateless-enough (516 K, one SOPS-managed user) and *can* shadow-run: a second
broker on a new endpoint serves nobody until a client is repointed. Standing it up in the
cluster first gives the other two moves a stable endpoint to converge on, and each client
repoint is an independently reversible one-line change. Moving z2m or HA first would instead
require publishing Docker mosquitto's 1883 onto the LAN as a temporary bridge — throwaway work.

### D2 — MQTT crosses hosts on a pinned MetalLB VIP, not Traefik

MQTT is raw TCP with its own auth; the HTTP ingress machinery (Authelia, CrowdSec, the `-k8s`
hostnames) has nothing to offer it. The cluster mosquitto gets a **pinned LoadBalancer VIP**
(`service-lan.yaml.j2` pattern, next free pool address) serving 1883, reachable identically from
cluster pods, daniel-server containers, and the operator's shell — one endpoint for every phase
of the migration and after it. Same SOPS credentials; `allow_anonymous false` stays.

### D3 — HA's config plane stays in `roles/containers/home-assistant`

That role's `files/` + `templates/` are the anchor for the validate-ha-config hook, the Jinja
macro tests, `sanctioned_writers.yml`, the HA skills, and the derived state model. Moving them
breaks dozens of paths for zero gain. The new `roles/k8s/home-assistant` consumes the existing
role's files (the `k8s/configarr` ← `containers/configarr` precedent) and owns only the k8s
plane: Deployment, PVC, seeding, route. The containers role keeps the config-authoring job even
after its compose half retires.

### D4 — NUT stays put; upsd opens to the cluster

Design decision 3 pins the UPS to daniel-server permanently. HA-on-k8s therefore reaches upsd
over the LAN: the peanut role's `127.0.0.1:3493:3493` binding widens to the LAN IP, restricted
to daniel-box by firewall rule (the portainer-agent precedent, in reverse). Repoint HA's NUT
integration to `daniel-server-ip:3493` **while HA is still on Docker** — it de-risks the move and
is trivially revertible.

### D5 — No concurrent instances, enforced structurally

z2m and HA deployments use `strategy: Recreate`, and each cutover is: stop the Docker container
→ delta-rsync the data → start the pod. Rollback is the exact inverse; the Docker copy stays on
disk, config intact, until the slice's exit criteria pass. Two z2m against one coordinator or
two HA against one MQTT/recorder is the failure mode this slice must never enter.

### D6 — Storage class waits on tonight's B2 verdict

Three new Longhorn PVCs (~205 M total). Whether they join the backup set or `longhorn-nobackup`
follows the B2 transaction-cap experiment reading at 03:30 UTC — green means config-class PVCs
back up daily; capped means the nobackup class plus the staggered-weekly design. Don't guess;
read the verdict first. Kopia's sentinels for `home-assistant/config/.storage/core.device_registry`
and `zigbee2mqtt/data/coordinator_backup.json` retire at each cutover (the frozen-copy
false-assurance trap is already documented in `check.py`) and coverage asserts cluster-side via
`longhorn-backup-health.sh` instead.

---

## Steps

### B1 — mosquitto to the cluster (shadow-safe)

New `roles/k8s/mosquitto`: Deployment (Recreate, though it hardly matters here), config from a
Secret, data on a small Longhorn PVC, pinned VIP service on 1883. Deploy alongside the Docker
broker — nothing points at it yet.

**Gate:** `mosquitto_sub`/`pub` round-trip through the VIP with the SOPS credentials, from
daniel-server. Then repoint, one client at a time, each verified before the next:

1. z2m: `mqtt.server` in the templated config → the VIP. Redeploy, watch it reconnect, confirm
   device availability topics republish and HA entities stay live.
2. HA: MQTT integration broker host (`.storage`, UI or WS API) → the VIP. Confirm discovery
   entities survive and FP300 events flow.
3. Stop + remove Docker mosquitto; drop the `mqtt` isolation net from both client entries;
   delete the rendered compose (`containers/mosquitto/docker-compose.yml`) — the
   gitops phantom-gate lesson from today.

**Monitoring re-plumb:** the AutoKuma label dies with the container; the cluster copy gets the
uptime-kuma k8s-monitor treatment the slice-3/4 services got. `z2m-device-setting`'s
`mosquitto_pub` invocation repoints at the VIP.

**Rollback:** repoint the clients back; the Docker broker's config never left.

### B2 — zigbee2mqtt to the cluster

New `roles/k8s/zigbee2mqtt`: Deployment (Recreate, replicas 1), PVC for `/app/data` seeded from
the Docker copy, the same `configuration.yaml.j2` templated in (PAN identity from SOPS —
identical by construction), frontend behind the `-k8s` route with Authelia
(`use_authelia: true` today).

**Cutover (target < 5 min, outside the wake window):** stop Docker z2m → delta-rsync `data/` →
start the pod. The SLZB only ever sees one client; the stop releases its TCP session.

**Gates:**
- z2m log shows the ember coordinator connected and the **same network** joined (no
  "coordinator changed" / re-form warnings).
- Device count in the frontend matches `devices.yaml` pre-move; zero re-interviews.
- FP300 presence/illuminance publishing; Tap Dial button event arrives; HA entities recover
  from `unavailable` within the availability timeout.
- `coordinator_backup.json` updates post-move (it must not stay frozen at the pre-move copy).

**Monitoring re-plumb:** the Kopia sentinel for `coordinator_backup.json` retires; Longhorn
covers the PVC. Kuma monitor re-plumbed like mosquitto's. The `bedroom_sensor_offline_alert`
5-minute `for:` rides out a clean cutover; a benign offline/recovery pair is acceptable if it
runs long.

**Rollback:** stop the pod, start the Docker container — its data dir is still on disk and the
PAN is pinned, so the mesh doesn't notice.

### B3 — home-assistant to the cluster

Prereqs, all while HA still runs on Docker: NUT repoint (D4) verified; MQTT already on the VIP
(B1); `trusted_proxies` in `configuration.yaml.j2` gains the cluster CIDR (verify the actual pod
CIDR — the bridge chain daniel-server-Traefik → cluster-Traefik → HA must keep
`X-Forwarded-For` honest or `ip_ban` starts banning proxies); confirm the AirGradient
integration addresses the device by IP/hostname, not mDNS discovery that only worked on the
Docker bridge.

New `roles/k8s/home-assistant`: Deployment (Recreate), `/config` PVC seeded from the Docker
copy, config files shipped from the containers role per D3, LSIO image with the same
`DOCKER_MODS` (verify HACS mod applies under containerd), pod securityContext carrying the
compose `cap_add` set, resources from the compose caps. Route: unsuffixed
`home-assistant.<domain>` forwards via `bridge_hostname` (the B4c pattern) so the
Cloudflare-proxied companion-app path never changes; no Authelia, same as today.

**Cutover (stop → rsync 140 M → start; daytime, nobody mid-wake-ramp):**

**Gates:**
- `probe.py ha verify-automations` exits 0 (every automation loaded).
- One real automation trace (`probe.py ha why …`) — automations *firing*, not just loaded.
- MQTT, NUT, dreo, AirGradient integrations all up; recorder writing (a state change appears in
  history post-move).
- Companion app reaches HA through the public route; a push notification delivers.
- The known stale-override-restore trap: after the unclean stop, check
  `bedroom_fan_manual`/`sleep_mode` restored sanely (the startup reconcile handles the fan).

**Re-plumb inventory (the B7a lesson — this list is the work):** `probe.py ha` container-IP
resolution → cluster route; the `ha-deploy`/`ha-verify-state`/`ha-edit-automation` skills' deploy
targets; the `home-assistant-engineer` agent brief; homepage's HA tile (`server: my-docker`
pair — today's widget lesson); Kuma monitor; Kopia sentinel for `.storage/core.device_registry`;
the `ups` net retires from peanut's entry; rendered compose deleted on daniel-server.

**Rollback:** stop the pod, start the Docker container against the still-on-disk config. Note
the recorder delta since cutover is lost on rollback — acceptable for history data.

---

## Exit criteria

1. Zigbee mesh intact: device count unchanged, zero re-interviews, PAN identity verified
   (coordinator log + a battery device — the FP300 — still reporting without a re-pair).
2. HA automations firing: `verify-automations` green plus a live trace of a bedroom automation
   actually executing post-move.
3. All three Docker copies stopped, removed from `containers_list`, rendered composes deleted,
   `mqtt`/`ups` isolation nets gone.
4. Backup plane moved: three PVCs visible in Longhorn's backup accounting per the D6 verdict;
   both Kopia sentinels retired; `longhorn-backup-health.sh` counting them.
5. Monitoring equivalent: Kuma monitors green against the cluster copies; `probe.py ha` and the
   HA skills working against the new home.

## Unverified — resolve during execution, not by assuming

- **SLZB takeover behavior:** does a new ember client connect cleanly after the old one's TCP
  session dies, or does the SLZB need its socket to time out first? Measure during B2's cutover;
  it bounds the real gap.
- **DOCKER_MODS / HACS under k8s** — LSIO mod layers download at container start; verify the HACS
  mod applies in the pod (first boot needs egress + a writable layer).
- **HA `.storage` MQTT broker host** — editable via the WS API like the entity registry, or
  UI-only? Affects whether B1 step 2 is scriptable.
- **Cluster pod CIDR** for `trusted_proxies` (read it from the live cluster, don't assume
  `10.42.0.0/16`).
- **Whether anything else on the LAN speaks to Docker mosquitto** that the isolation-net survey
  missed (the Pi's crons don't; verify with the broker's connection log before B1 step 3).
- **B2 verdict** (03:30 UTC) → D6 storage class.
