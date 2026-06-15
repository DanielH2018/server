# home-assistant — Home automation platform

LinuxServer.io Home Assistant. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/homeassistant:latest` (LSIO is x86-64-maintained;
  only the 32-bit ARM variant was deprecated — fine for daniel-server)
- **Host:** daniel-server · **Port:** 8123 · **Networks:** apps + ups · **Authelia:** no
  (`ups` = isolation net to the `nut` sidecar's upsd:3493 for the NUT integration;
  `apps` stays networks[0] so the Traefik label binds to it)
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Auth: HA's own login, NOT Authelia.** `use_authelia: false` is deliberate —
  Authelia forward-auth breaks the HA companion mobile app, webhooks, and long-lived
  API tokens (none can complete the portal login flow). The route still gets Traefik
  TLS + CrowdSec + per-router rate-limiting; harden the gate inside HA (strong
  password + TOTP). If you ever want Authelia on the *web UI only*, you'd need
  per-path bypass rules for `/api/`, `/auth/`, and the webhook paths.
- **HACS preinstalled** via `DOCKER_MODS=linuxserver/mods:homeassistant-hacs`
  (LSIO Docker mod that drops the Home Assistant Community Store into `/config`).
- **`configuration.yaml` is templated** from `configuration.yaml.j2` to `./config`.
  It sets `use_x_forwarded_for: true` + `trusted_proxies: 172.16.0.0/12` so HA honors
  Traefik's `X-Forwarded-For` (without it HA rejects the proxied request with
  "400 Bad Request"). The template task is wired to `common_config_changed`, so editing
  it recreates the container on the next deploy. **Note:** HA may rewrite parts of its
  own config via the UI, but this file is the Ansible source of truth and is
  overwritten on deploy — keep UI-managed config (integrations, etc.) in the areas HA
  stores separately (`.storage/`, automations.yaml…), which are NOT templated.
- **All persistent state is `./config` → `/config`** (Kopia-backed): the SQLite
  recorder DB, `.storage/`, secrets, automations, and the templated `configuration.yaml`.
- **Bridge networking, not host.** Cloud/API-based integrations work fine. **Local
  device discovery** (mDNS/SSDP, Bluetooth, Zigbee/Z-Wave USB dongles) generally needs
  `network_mode: host` and/or `devices:` passthrough — which is incompatible with the
  Traefik-label + bridge-network setup here. Switching to host mode is a separate,
  larger change; revisit only if you add local hardware.

## Editing
- Compose: `templates/docker-compose.yml.j2` · HA cfg: `templates/configuration.yaml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"`
