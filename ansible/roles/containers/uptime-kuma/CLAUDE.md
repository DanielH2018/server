# uptime-kuma — offsite monitoring (remote node)

## At a glance
- **Image:** `louislam/uptime-kuma:2.5.3` (tag-only, matching the cluster role; arm64 verified 2026-08-30)
- **Host:** `daniel-cloud` (Oracle A1, arm64) — a **second** Kuma, alongside the cluster's
- **Route:** `uptime-kuma.<domain>` · **Port:** 3001 · **Networks:** `proxy` · **Depends on:** traefik
- **State:** `containers/uptime-kuma/data/` — monitor set and heartbeat history

## Notable
- **This instance watches the house from outside it.** The cluster's Kuma cannot be its own
  dead-man switch: an outage silences both the push and the monitor waiting for it. That is
  the same argument already written into the heartbeat scripts.
- **No AutoKuma sidecar here, deliberately.** AutoKuma creates monitors from the Docker
  labels of services beside it; this instance's monitors are *push* monitors fed by the
  home crons, which no local label can describe. The `kuma.*` labels in the templates are
  inert without it and are kept only so the templates stay uniform.
- It **needs its own notify path and its own liveness check**, or it is a watcher nobody
  watches. A free-tier VM subject to reclamation is a worse outermost detector than
  Healthchecks.io, so the SaaS stays outermost — this is a complement, never a replacement.
- `NET_RAW` is granted on purpose: ping monitors need raw ICMP sockets, and under
  `cap_drop: ALL` the ping binary fails with "operation not permitted" and the monitor can
  never come up.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Monitors are created in the UI, not in this repo.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "uptime-kuma" -e target=daniel-cloud`
