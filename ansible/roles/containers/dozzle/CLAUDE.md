# dozzle — Real-time Docker log viewer

Read-only web UI that live-tails `docker logs` across containers. Added to the Pi as the
ad-hoc logging tool in place of Portainer. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `amir20/dozzle:latest`
- **Host:** daniel-pi (role is host-agnostic; only listed in the Pi's `containers_list`)
- **Port:** 8080 · **URL:** `http://<pi-lan-ip>:8080` (LAN-bound, no Authelia)
- **Networks:** proxy
- **Depends on:** docker-proxy
- **Config in:** `ansible/inventory/host_vars/daniel-pi.yml` → `containers_list`

## Notable
- **No raw socket:** reads the Docker API through the read-only `docker-proxy`
  (`DOCKER_HOST=tcp://docker-proxy:2375`), so it never mounts `/var/run/docker.sock`.
- **Stateless:** no bind mounts / DB. `DOZZLE_NO_ANALYTICS=true` disables phone-home.
- **Exposure is host-driven** via `expose.yml.j2` + `expose_mode` — Traefik+Authelia where
  `expose_mode: traefik`, LAN-bound where `expose_mode: lan`.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy (driven from the server): `uv run ansible-playbook ansible/deploy.yml --tags "dozzle" -e target=daniel-pi`
  (`-e target=`, not `--limit` — `--limit daniel-pi` from the server matches zero hosts)
