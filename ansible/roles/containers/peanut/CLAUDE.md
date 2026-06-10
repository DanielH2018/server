# peanut — UPS monitor (PeaNUT + NUT)

Web dashboard for the APC UPS, backed by a Network UPS Tools (NUT) sidecar.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Images:** `brandawg93/peanut:latest` (UI) + built `nut` sidecar (`files/Dockerfile`)
- **Host:** daniel-server · **Port:** 8080 · **URL:** `peanut.<domain>` (Authelia: yes)
- **Networks:** apps + internal (NUT only on `internal`)
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- `nut` talks to the UPS over USB via `/dev/bus/usb` + a `99-nut-apc.rules` udev rule
  (MODE=0666) installed by the role — no privileged mode needed.
- NUT config is fully templated: `ups.conf`, `upsd.conf`, `upsd.users`, `upsmon.conf`,
  `upssched.conf`, `nut.conf`.
- PeaNUT web creds (`peanut_username`/`peanut_password`) come from secrets.
- The built `nut` sidecar rides a rolling base (`debian:bookworm-slim`) — a weekly
  Sunday rebuild cron (06:15, via `common/redeploy_cron.yml`) delivers base updates;
  Watchtower can't.

## Editing
- Compose: `templates/docker-compose.yml.j2` · NUT cfg: `templates/*.j2`, `files/`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "peanut"`
