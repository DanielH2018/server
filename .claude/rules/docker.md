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
    traefik+crowdsec). Don't flag these; justify any new named volume with a comment.
- Pin image tags or use a stable channel. `latest` is acceptable for the homelab tier, but note when
  a specific version is preferred.
