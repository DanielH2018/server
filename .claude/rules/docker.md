---
paths:
  - "ansible/roles/containers/**"
---

# Docker Compose Rules

These roles deploy to `daniel-pi` only; neither cluster node has Docker. The `/new-container`
skill carries the compose skeleton and the shared macros in `ansible/templates/`.

## `containers/` is rendered, not tracked

`containers/` is not a directory in this repo — it is untracked and rendered by Ansible onto
the *target host* at `/home/<user>/server/containers/<svc>/docker-compose.yml`. Post-migration
it exists only on `daniel-pi`; neither cluster node has one. It is still read-only: edits are
overwritten on the next deploy, so always modify `ansible/roles/containers/*/templates/`
instead. (The `block-protected-edits` hook enforces this.)

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
