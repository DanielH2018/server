# Connect the APC UPS to Home Assistant via NUT

**Date:** 2026-06-15
**Status:** Approved — pending implementation plan
**Host:** daniel-server

## Goal

Expose the existing APC BR1500MS2 UPS to Home Assistant as sensors (battery
charge, load, runtime, input voltage, status) using Home Assistant's native
**Network UPS Tools (NUT)** integration.

## Current state

A `peanut` role already runs a full NUT stack on `daniel-server`:

- A built `nut` sidecar owns the USB link to the UPS (`usbhid-ups`, via
  `/dev/bus/usb` + a `99-nut-apc.rules` udev rule) and runs `upsd`.
- `upsd` listens on `0.0.0.0:3493` inside the container and is published to the
  host loopback at `127.0.0.1:3493`.
- The `nut` container is attached only to two project-scoped networks:
  `internal` (`internal: true`) and `nut_host` (a plain bridge that exists solely
  to back the loopback publish). It is **not** on any shared/external network.
- UPS name: `apc-ups` (`nut_ups_name` default).
- Existing NUT users (`upsd.users`): only `upsmon` (role `upsmon primary`,
  password `nut_monitor_password`) — a master/primary login with shutdown rights.
- Consumers today: the host-side `upsmon` (secondary, performs the real
  `systemctl poweroff` on FSD) and the PeaNUT web dashboard.

Home Assistant runs on `daniel-server`, port 8123, on the `apps` network only,
with its own auth (no Authelia).

## The problem

NUT is a client/server protocol: `upsd` owns the USB connection and serves UPS
state over TCP/3493 to any number of clients. Home Assistant becomes just another
NUT *client* — the UPS is never plugged into HA directly. Two gaps prevent HA
from connecting today:

1. **No network path.** HA is on `apps`; `nut` is on `internal` + `nut_host`
   (both project-scoped to the `peanut` compose project, real names
   `peanut_internal` / `peanut_nut_host`). HA cannot reach `nut:3493`. The
   `127.0.0.1:3493` host publish is useless to a container — that is the
   container's own loopback, not the host's.
2. **No appropriate credential.** The only NUT user (`upsmon`) is a
   master/primary login with shutdown rights — too privileged to hand to HA.

HA's NUT integration is **UI config-flow only** (not YAML-configurable), so
`configuration.yaml.j2` cannot set it up. Ansible's job is limited to making
`upsd` reachable from HA and providing a read-only credential; the final wiring
is a one-time click-through in the HA UI.

## Approach

**Chosen: dedicated `ups` isolation network.** Create a new shared external
network `ups`, joined by *only* `nut` and `home-assistant`. This mirrors the
existing `kopia` ("isolation net: Kopia <-> Traefik only") and `lifecycle`
isolation networks, and keeps the `upsd` power-control surface off the busy
`apps` network — preserving the `peanut` role's deliberate isolation.

**Rejected alternative: put `nut` on `apps`.** One line, no new network, no
deploy-ordering care, but exposes `upsd` to every container on `apps` (~30
services). Rejected for weakening isolation against the role's intent.

## Changes

### 1. New isolation network
`ansible/roles/setup/docker_install/tasks/main.yml` — add `ups` to the
`Create Docker networks` loop, with an isolation comment:
`# isolation net: NUT <-> Home Assistant only`.

### 2. `nut` joins `ups`
`ansible/roles/containers/peanut/templates/docker-compose.yml.j2` — add `- ups`
to the `nut` service's `networks:` list and declare `ups: { external: true }` in
the top-level networks stanza. `nut` keeps `internal` + `nut_host`; this only
adds a third attachment. (`peanut` and the top-level inline networks are
unchanged.)

### 3. Home Assistant joins `ups`
`ansible/inventory/host_vars/daniel-server.yml` — change `home-assistant`'s
`networks: [apps]` to `networks: [apps, ups]`. The `service_networks()` /
`external_networks()` macros generate the compose output. `apps` MUST stay first
in the list — the Traefik label uses `networks[0]`, so HA's reverse-proxy routing
must continue to bind to `apps`.

### 4. Dedicated read-only NUT user
`ansible/roles/containers/peanut/templates/upsd.users.j2` — append:

```
[homeassistant]
  password = {{ nut_ha_password }}
```

No `upsmon` role line and no `instcmds`/`actions` → read-only. HA can read every
UPS variable but cannot issue FSD/shutdown or instant commands. Editing this file
flows through the existing `peanut_cfg_nut is changed` → `common_config_changed`
wiring, so the `nut` container auto-recreates on deploy.

### 5. New secret
`nut_ha_password` added to `ansible/vars/secrets.yml` via `sops`, then
`uv run python scripts/secret_rotation.py sync` and commit the registry update.

## Deploy sequence

Order matters — the external network must exist before `peanut` / `home-assistant`
are recreated:

1. Create the network: `uv run ansible-playbook ansible/initial_setup.yml --tags docker-networks`
   (or, once, `docker network create ups`).
2. `uv run ansible-playbook ansible/deploy.yml --tags peanut` — adds the
   `homeassistant` user and the `ups` attachment, recreates `nut`.
3. `uv run ansible-playbook ansible/deploy.yml --tags home-assistant` — joins
   `ups`.

## Manual Home Assistant step (one-time, not automatable)

HA → Settings → Devices & Services → **Add Integration** → *Network UPS Tools
(NUT)* → Host `nut`, Port `3493`, Username `homeassistant`, Password
`<nut_ha_password>` → select the `apc-ups` device. HA then exposes battery %,
load, runtime, input voltage, and status as sensors.

## Testing / verification

- `uv run python scripts/validate_compose_templates.py` and `prek run --all-files`
  for template/whitespace/lint correctness (peanut + home-assistant render
  cleanly; macro whitespace is correct).
- Post-deploy reachability: `docker exec home-assistant nc -z nut 3493`
  (or confirm via HA's integration setup / logs).
- Regression: existing clients unaffected — `docker exec nut upsc apc-ups@localhost`
  still returns vars; host-side `upsmon` (nut-monitor.service) and PeaNUT still
  read `apc-ups`. We only *added* a user and a network; nothing existing changed.

## Risks & rollback

- **Risk:** deploying `peanut`/`home-assistant` before the `ups` network exists
  fails the recreate. Mitigation: the deploy-sequence step 1.
- **Risk:** reordering HA's `networks` list (ups before apps) would repoint the
  Traefik label. Mitigation: keep `apps` first (called out in change 3).
- **Rollback:** revert the four files + remove the `nut_ha_password` secret; the
  `ups` network can be left in place (harmless if unused) or removed with
  `docker network rm ups` after the containers detach. No existing UPS behaviour
  (host shutdown chain, PeaNUT) is touched, so rollback is low-risk.
