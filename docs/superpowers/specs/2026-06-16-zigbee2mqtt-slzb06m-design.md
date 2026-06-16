# SLZB-06M → Zigbee2MQTT → Mosquitto → Home Assistant

**Date:** 2026-06-16
**Status:** Approved (design) — pending implementation plan
**Host:** daniel-server

## Goal

Connect a SMLIGHT **SLZB-06M** network Zigbee coordinator to Home Assistant via a
**Zigbee2MQTT + Mosquitto** stack, deployed as two new Ansible container roles that follow
existing homelab conventions. HA consumes Zigbee devices over MQTT using HA MQTT discovery.

## Key facts driving the design

- The SLZB-06M is a **network coordinator**: it exposes the Zigbee radio as serial-over-TCP
  (default port **6638**) over Ethernet. HA/Z2M reach it as `tcp://<ip>:6638` — **no
  `network_mode: host`, no USB `devices:` passthrough.** The bridge-networking caveat in
  `home-assistant/CLAUDE.md` (which concerns *inbound* discovery / USB dongles) does not apply.
- Docker bridge networks NAT outbound through the host, so a Z2M container on `apps` can dial
  the coordinator's LAN IP directly.
- The device is **already prepared** by the operator: on the LAN with a reserved IP, latest
  Zigbee coordinator firmware flashed, Zigbee mode + port 6638, not bonded to any other stack.

## Architecture

```text
SLZB-06M (10.0.0.127:6638)         ← physical Zigbee coordinator
      │  serial-over-TCP
      ▼
zigbee2mqtt  ──MQTT──►  mosquitto  ◄──MQTT──►  home-assistant
 (apps + mqtt)          (mqtt only)            (apps + ups + mqtt)
 UI: zigbee.<domain>                           MQTT integration (HA UI)
     Traefik + Authelia
```

Z2M runs with HA MQTT discovery enabled, so paired Zigbee devices auto-appear in HA with no
per-device HA configuration.

## Components

### 1. `mosquitto` role (new) — internal MQTT broker

- **Image:** `eclipse-mosquitto:2` (pinned major).
- **Networks:** `mqtt` **only**. No Traefik label, no Authelia, port 1883 **not** host-published
  — reachable solely by Z2M and HA on the `mqtt` net. Not registered with a `port:` in
  `containers_list` (non-web-facing); `use_authelia` is irrelevant.
- **Volumes (bind mounts, Kopia-backed):**
  - `./config` → templated `mosquitto.conf` + the hashed `passwordfile`.
  - `./data` → retained-message / persistence DB (regenerable; low concern).
- **Auth:** `listener 1883`, `allow_anonymous false`, `password_file /mosquitto/config/passwordfile`.
- **Password file:** generated **hash-in-SOPS** (see Secrets). The role templates the password
  file directly from a pre-hashed `mosquitto_passwd` line stored in SOPS — fully declarative
  and idempotent.
- **Healthcheck:** `mosquitto_sub` self-check against `$$SYS/broker/uptime` with `-C 1` using
  the broker credentials. **NOTE:** the `$SYS` topic MUST be written `$$SYS` in the template —
  Compose interpolates a lone `$SYS` at parse time and the validate-compose hook does not catch
  it (ref: `compose-healthcheck-dollar-escaping`).
- **Macros:** autokuma `kuma()` monitor, `resources()` caps, `healthcheck()`.
- Config tasks `register:`-ed and passed via `common_config_changed` so edits recreate the
  container.

### 2. `zigbee2mqtt` role (new) — Zigbee stack + admin UI

- **Image:** `ghcr.io/koenkk/zigbee2mqtt` (pinned; Renovate-managed like other pins).
- **Networks:** `apps` (networks[0] — Traefik binds here) + `mqtt` (reach mosquitto).
- **Web:** `port: 8080`, `use_authelia: true`, `hostname: zigbee` → `zigbee.<domain>`
  (covered by the existing `*.<domain>` wildcard cert — no DNS/cert work).
- **Volume (bind mount, Kopia-backed — CRITICAL):** `./data` holds the device database,
  `coordinator_backup.json`, and the Zigbee **network key**. Losing it means re-pairing every
  device. Must be inside Kopia scope; ensure no kopiaignore pattern excludes it.
- **Templated `configuration.yaml`** (wired to `common_config_changed`):
  - `serial: { port: "tcp://10.0.0.127:6638", adapter: ember }`
  - `mqtt: { server: "mqtt://mosquitto:1883", user/password from SOPS }`
  - `homeassistant: true` (HA discovery)
  - `frontend` enabled on 8080
  - `permit_join: false` (pairing is performed deliberately via the UI)
  - The SLZB IP (`10.0.0.127`) is a templated var (host_vars), not hardcoded in the template.
- **`adapter` value:** `ember` is correct for the SLZB-06M's Silabs EFR32 EmberZNet firmware;
  confirm against the flashed firmware during implementation.
- **Healthcheck:** HTTP GET against the frontend on 8080 (use the tool the image ships —
  `wget`/`curl`; verify during implementation). autokuma + resource caps.

### 3. New `mqtt` Docker network

- Add `mqtt` to the `Create Docker networks` loop in
  `ansible/roles/setup/docker_install/tasks/main.yml` (alongside `ups`, `kopia`), with the same
  isolation-net intent comment.
- **Must be created before first deploy** — deploys only *attach* to networks, they do not
  create them. Run `initial_setup.yml --tags docker-networks` first, else both containers fail
  with "network mqtt not found."

### 4. Home Assistant change (minimal)

- Add `mqtt` to `home-assistant`'s `networks:` list in `host_vars/daniel-server.yml`
  (currently `apps` + `ups`). Redeploy HA.
- The MQTT **integration** is added once in the HA UI (server `mosquitto`, port 1883, SOPS
  creds). This lives in HA's `.storage/` and is intentionally **not** templated (per
  `home-assistant/CLAUDE.md`).

### 5. Secrets (SOPS)

- `mqtt_username` (plaintext — used by Z2M and HA clients).
- `mqtt_password` (plaintext — used by Z2M and HA clients).
- `mqtt_password_hash` (the `mosquitto_passwd`-generated line — used to template the broker's
  password file). Generated once during implementation by running `mosquitto_passwd` against
  the chosen username/password.
- Add via `/add-secret`; afterward `uv run python scripts/secret_rotation.py sync` and register
  in `ansible/secret_rotation.yml`.

### 6. host_vars registration

- Add `mosquitto` and `zigbee2mqtt` entries to `containers_list` in
  `ansible/inventory/host_vars/daniel-server.yml`.
- Add a templated `slzb_ip: 10.0.0.127` (or similar) var for the coordinator's LAN IP.

## Deploy order

1. `/add-secret` the three MQTT secrets → `secret_rotation.py sync`.
2. `initial_setup.yml --tags docker-networks` (create `mqtt` net).
3. Deploy `mosquitto`, then `zigbee2mqtt`.
4. Redeploy `home-assistant` (picks up `mqtt` net).
5. HA UI: add the MQTT integration (mosquitto:1883 + creds).
6. Z2M UI (`zigbee.<domain>`): confirm it connected to the coordinator, then permit-join and
   pair devices. Devices auto-appear in HA via discovery.

## Validation

- `scripts/validate_compose_templates.py` (PostToolUse hook re-renders on template edit).
- `uv run python scripts/probe.py health mosquitto` and `health zigbee2mqtt` (post-deploy gate;
  exits 0 only when running + healthy).
- Z2M UI shows the coordinator online (adapter connected, on `tcp://10.0.0.127:6638`).
- HA MQTT integration connected; a paired device appears as an HA entity.

## Out of scope (YAGNI)

- Thread/Matter multiprotocol on the SLZB-06M.
- MQTT TLS / websockets (broker is on an isolated internal-only net).
- Any HA→Z2M coupling beyond standard MQTT discovery.
- Templating HA's MQTT integration (UI/`.storage`-managed by design).
