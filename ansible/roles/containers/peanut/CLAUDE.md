# peanut — UPS monitor (PeaNUT + NUT)

Web dashboard for the APC UPS, backed by a Network UPS Tools (NUT) sidecar.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Images:** `brandawg93/peanut:latest` (UI) + built `nut` sidecar (`files/Dockerfile`)
- **Host:** daniel-server · **Port:** 8080 · **URL:** `peanut.<domain>` (Authelia: yes)
- **Networks:** peanut on apps + internal; `nut` sidecar on `internal` + `nut_host` + `ups`
  (`ups` is a shared isolation net so Home Assistant's NUT integration can reach upsd:3493)
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
- **Host shutdown is two-tier (fixed 2026-06-10):** the container upsmon (primary) only
  *raises FSD* — its old `nsenter -t 1 … poweroff` SHUTDOWNCMD silently failed (needs
  pid:host + SYS_ADMIN we don't grant), so battery exhaustion meant a hard power-cut.
  The role now installs **nut-client on the host**: a `secondary`-mode upsmon watches
  the containerized upsd via the `127.0.0.1:3493` publish (over the dedicated `nut_host`
  bridge — `internal: true` nets can't publish ports) and runs the real
  `systemctl poweroff` when FSD propagates. Sequence: 120 s on battery (upssched) or
  LOWBATT → container `upsmon -c fsd` → host secondary powers off.
- **Manual shutdown drill** (actually powers the host off — have console access):
  `docker exec nut upsmon -c fsd` → the host should begin `systemctl poweroff` within
  ~15 s (HOSTSYNC). The benign `nut-common-tmpfiles.conf` warning from nut-monitor is
  a Debian packaging nit.

## Editing
- Compose: `templates/docker-compose.yml.j2` · NUT cfg: `templates/*.j2`, `files/`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "peanut"`
