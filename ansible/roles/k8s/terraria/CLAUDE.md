# terraria (k8s) — Vanilla Terraria server

Moved from Docker 2026-08-10 (backup consolidation BL1 — its worlds were the only
irreplaceable data kopia still uniquely protected; the Docker role is in
`roles/containers/archive/terraria`). See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `beardedio/terraria:vanilla-latest@sha256:901bc117…` — **digest-pinned**
  (2026-08-13). Upstream publishes no pinned vanilla versions, so `vanilla-latest` is the only tag
  available; the digest is what stops it floating. A redeploy therefore does NOT pick up a new
  upstream build — updates arrive as a Renovate digest-bump PR plus a deliberate redeploy. That
  matters here because the volume holds irreplaceable world data.
- **Host:** daniel-box · **Port:** public 7777 via `Service type: LoadBalancer`,
  `externalTrafficPolicy: Local`, pinned to the host IP — the router's forward points here
- **Storage:** worlds + config on a BACKED-UP Longhorn PVC (the point of the move),
  seeded from the Docker copy at cutover
- **Probes:** a `/proc/net/tcp` listen-check on :7777 (`1E61`), NOT a connect probe — a
  TCP connect spams the game console with join/leave noise
- **Caps:** `DAC_OVERRIDE` only (the image's world-save backup rotation touches
  root-owned files); `stdin`/`tty` kept so the server console stays attachable
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`
  (`name: terraria`, no port/hostname — not Traefik-routed) and `defaults/main.yml`

## Notable
- **Logs ship via the loki-homelab promtail DaemonSet** (`{container="terraria"}`,
  `job="k8s"`) — that stream is what feeds terraria-stats (still a Docker sidecar on
  daniel-server, reading the cluster Loki since Phase D.2).
- cloudflare-ddns publishes `terraria.<domain>` (direct/unproxied — game traffic can't
  ride Cloudflare's HTTP proxy).
- Seeding used a sudo-staged copy — the world files **on the pre-migration Docker host**
  were root-600, which the unprivileged seed pipeline can't read in place. See the BL1 record
  in `docs/archive/k3s-migration/backup-consolidation-longhorn.md`. This describes the SOURCE,
  not the live volume: the seed landed everything uid-1000, and `/config` measured 1000:1000
  drwxr-xr-x on 2026-08-31. Read as a claim about live state it contradicts the DAC_OVERRIDE
  comment in `templates/deployment.yaml.j2`, which is why the epoch is spelled out here.

## Editing
- Manifests: `templates/*.yaml.j2` · Defaults: `defaults/main.yml`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "terraria" -e target=daniel-box`
