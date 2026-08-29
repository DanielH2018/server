# dozzle — Real-time Docker log viewer (RETIRED 2026-08-29)

> **Archived, not deployed.** No `containers_list` entry references this role, so nothing
> renders or deploys it. It is kept for the Compose plumbing, the way `archive/kopia` and
> `archive/portainer-agent` are.
>
> **Why it went.** promtail ships daniel-pi's container logs to loki-homelab, so Grafana
> answers what dozzle answered and keeps 31 days of it, which dozzle never did. Against that,
> it cost ~15 MiB of a 456 MB board — 9.3 MiB RSS plus its ~6 MiB containerd-shim — on a host
> measured at 8% sustained full memory stall, where the per-container shim tax is the only
> remaining lever. This contradicts the marker in `archive/portainer-agent/CLAUDE.md`
> ("dozzle stays … dozzle is 6 MB, a nicer merged live-tail"): it had grown to 9.3 MiB, and
> the log pipeline it was kept ahead of has since been built.
>
> **Its host artifacts were removed imperatively** at retire time — the container, the
> `containers/dozzle/` Compose project directory, and the `amir20/dozzle:latest` image.
> Nothing in Ansible recreates them. Its Kuma monitor ("Daniel Pi Dozzle") was deleted from
> `roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2` in the same change.
>
> **To revive it**, restore the `containers_list` entry in `daniel-pi.yml` and move this role
> back out of `archive/` — both, or `test_containers_list_roles_exist.py` fails.

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
