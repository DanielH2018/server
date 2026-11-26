---
paths:
  - "hosts/components/pi-compose/**"
  - "hosts/daniel-pi/vars.yml"
---

# Docker Compose Rules

These services deploy to `daniel-pi` only; neither cluster node has Docker. `alloy`, `autoheal`,
`docker-proxy` and `wg-easy` are `compose_services` entries in
`hosts/daniel-pi/vars.yml`, rendered from
`hosts/components/pi-compose/services/<name>/` into the `homelab-daniel-pi` package. The package
runs them as one Compose project, `homelab`. The `/new-container` skill carries the service
skeleton.

## The rendered project is not tracked

The package installs the rendered project at `/usr/lib/homelab/compose/` on the Pi
(`scripts/renderer/renderer_lib/host_outputs.py:COMPOSE_ROOT`). It is read-only: the next
package version overwrites an edit, so always modify `hosts/components/pi-compose/services/<svc>/`
instead. The directories under
`/home/<user>/server/containers/<svc>/` that some services bind-mount are created by the
component's `tmpfiles.d/homelab-compose.conf`
(`hosts/lib/tests/pi_compose/test_pi_compose_census.py::test_a_relative_bind_source_with_no_tmpfiles_line_is_flagged`).

## Conventions

- The Pi is LAN-only, so no service carries Traefik labels. A UI that needs reaching binds the
  Pi's LAN IP on the `port` its `compose_services` entry names, as wg-easy does
  (`hosts/lib/tests/pi_compose/test_pi_compose_wg_easy.py::test_the_admin_ui_is_bound_to_the_lan_address_only`).
- A service joins only the networks its `compose_services` entry lists. Most join `proxy`, which
  reaches the read-only docker-proxy; autoheal joins only `lifecycle`, which reaches the
  restart-only docker-proxy-lifecycle. The docker-proxy file names the sub-proxies' networks
  itself, since they have no entry
  (`hosts/lib/tests/pi_compose/test_pi_compose_docker_proxy.py::test_the_proxies_join_the_networks_the_ansible_era_containers_use`).
- PUID/PGID: `1000`/`1000`, user: `ubuntu`
- Timezone: `America/Chicago`
- Containers should have healthchecks defined where possible
- Set `restart: unless-stopped` on every service.
- Persistent storage = long-syntax bind mounts under the service's `containers/` directory;
  **no named or anonymous volumes**: the bind census refuses any volume that is not a bind
  (`hosts/lib/tests/_compose_tree.py:bind_problems`). Bind mounts under `containers/` keep state
  inspectable and portable. Nothing backs up the Pi's Docker state except wg-easy's `./config`,
  which the `pi-peer-backup` CronJob pulls nightly (`k8s/roles/pi-peer-backup/`); every other
  service's state is regenerable.
- Pin image tags or use a stable channel. `latest` is acceptable for the homelab tier, but note when
  a specific version is preferred. Every image carries a digest either way
  (`hosts/lib/tests/pi_compose/test_pi_compose_census.py::test_an_image_without_a_digest_is_flagged`).
- **`read_only: true` + `tmpfs:` — the `noexec` residual is an ACCEPTED trade-off, do not re-flag.**
  Services with an immutable rootfs still get writable `tmpfs:` scratch mounts (`/run` on
  docker-proxy and docker-proxy-lifecycle, `/tmp` on alloy), and Compose's `tmpfs` long-form only exposes
  `size`/`mode` — there is **no Compose-native way to set `noexec`**. So those mounts are technically
  writable-and-executable inside an otherwise-immutable container. This is defense-in-depth only and
  the `suid` half is already neutered fleet-wide by `no-new-privileges:true` + `cap_drop:[ALL]`;
  exploiting the residual `exec` bit requires prior in-container RCE. Closing it would need a
  daemon-level `default-mount-opts`/AppArmor change, out of scope for the compose layer — reviewed
  2026-07-05 and consciously accepted. Don't propose per-service `noexec` (Compose can't express it).
