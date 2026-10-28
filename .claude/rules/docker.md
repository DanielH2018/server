---
paths:
  - "hosts/components/pi-compose/**"
---

# Docker Compose Rules

These services deploy to `daniel-pi` only; neither cluster node has Docker. The `homelab-daniel-pi`
package runs them as one Compose project, `homelab`. The `/new-container` skill carries the
service skeleton.

## The rendered project is not tracked

The package installs the rendered project at `/usr/lib/homelab/compose/` on the Pi. It is
read-only: the next package version overwrites an edit, so always modify
`hosts/components/pi-compose/services/<svc>/` instead. The directories under
`/home/<user>/server/containers/<svc>/` that some services bind-mount are created by the
component's `tmpfiles.d/homelab-compose.conf`. H5.12 deleted the Ansible Compose path that
used to render them (`ansible/roles/containers/`).

## Conventions

- All containers use Traefik labels for reverse proxy routing
- Docker network: `proxy`
- PUID/PGID: `1000`/`1000`, user: `ubuntu`
- Timezone: `America/Chicago`
- Containers should have healthchecks defined where possible
- Set `restart: unless-stopped` on every service.
- Persistent storage = bind mounts under a well-known `/data` path; **no anonymous volumes** — bind
  mounts under `containers/` keep state inspectable and portable. (Kopia retired 2026-08-10;
  the backup plane is Longhorn-on-k8s, so Docker-tier state is deliberately unbacked-up —
  the services still on Docker hold regenerable or migrating state only.)
  - **Documented exception — named volumes** are the deliberate pattern for bulky, regenerable state:
    a log shipper's read cursor (regenerable — the Pi's Alloy keeps its positions under
    `./data`). Don't flag it; justify any new named volume with a comment.
- Pin image tags or use a stable channel. `latest` is acceptable for the homelab tier, but note when
  a specific version is preferred.
- **`read_only: true` + `tmpfs:` — the `noexec` residual is an ACCEPTED trade-off, do not re-flag.**
  Services with an immutable rootfs still get writable `tmpfs:` scratch mounts (`/tmp`, `/run`,
  `.next/cache`, `/var/cache/nginx`, `/app/config`, …), and Compose's `tmpfs` long-form only exposes
  `size`/`mode` — there is **no Compose-native way to set `noexec`**. So those mounts are technically
  writable-and-executable inside an otherwise-immutable container. This is defense-in-depth only and
  the `suid` half is already neutered fleet-wide by `no-new-privileges:true` + `cap_drop:[ALL]`;
  exploiting the residual `exec` bit requires prior in-container RCE. Closing it would need a
  daemon-level `default-mount-opts`/AppArmor change, out of scope for the compose layer — reviewed
  2026-07-05 and consciously accepted. Don't propose per-service `noexec` (Compose can't express it).
