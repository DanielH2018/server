---
paths:
  - "containers/**"
  - "ansible/roles/containers/**/*.j2"
---

# Docker Compose Rules

The core conventions (`containers/` is read-only/Ansible-generated, `proxy` network, PUID/PGID
1000/1000, TZ America/Chicago, Traefik labels, healthchecks) live in CLAUDE.md. This file only adds
the path-specific detail not spelled out there:

- Set `restart: unless-stopped` on every service.
- Persistent storage = bind mounts under a well-known `/data` path; **no anonymous volumes** — bind
  mounts under `containers/` are what Kopia backs up.
  - **Documented exception — named volumes** are the deliberate pattern for bulky, regenerable state
    that should stay OUT of Kopia's scope: `prometheus_data` (TSDB), `loki` (logs), `feed_cache`
    (freshrss nginx), `karakeep_meili` (rebuildable search index), `crowdsec-db` (shared
    traefik+crowdsec), `promtail_positions` (promtail's log-read cursor, regenerable). Don't flag
    these; justify any new named volume with a comment.
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
