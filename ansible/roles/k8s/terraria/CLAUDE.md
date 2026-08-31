# terraria (k8s) — Vanilla Terraria server

Moved from Docker 2026-08-10 (backup consolidation BL1 — its worlds were the only
irreplaceable data kopia still uniquely protected; the Docker role is in
`roles/containers/archive/terraria`). See repo-root `CLAUDE.md`.

## At a glance
- **Image:** built in-cluster (`<registry>/terraria:latest`) from `templates/Dockerfile.j2`,
  since 2026-08-31 — a single `chown` layer whose only purpose is to let the server run as a
  non-root uid. The **digest pin did not go away**: it is the `FROM` line of that Dockerfile,
  still `beardedio/terraria:vanilla-latest@sha256:901bc117…` (2026-08-13). Upstream publishes no
  pinned vanilla versions, so `vanilla-latest` is the only tag available and the digest is what
  stops it floating.
  What changed is how an update lands. Renovate's built-in dockerfile manager still raises a
  digest-bump PR, but built images carry `automerge: false`, so the bump is realised by an
  operator rebuild (`./scripts/deploy.sh --tags terraria`) rather than by a pull. A redeploy
  rebuilds only when the rendered context changed; `-e image_builder_force=true` overrides that
  for a base-image CVE. All of this matters because the volume holds irreplaceable world data.
- **Host:** daniel-box · **Port:** public 7777 via `Service type: LoadBalancer`,
  `externalTrafficPolicy: Local`, pinned to the host IP — the router's forward points here
- **Storage:** worlds + config on a BACKED-UP Longhorn PVC (the point of the move),
  seeded from the Docker copy at cutover
- **Probes:** a `/proc/net/tcp` listen-check on :7777 (`1E61`), NOT a connect probe — a
  TCP connect spams the game console with join/leave noise
- **Caps:** none — `drop: ALL` with nothing added back, since 2026-08-31. The server runs as
  uid 1000 with `fsGroup` 1000, so it owns the data it writes instead of overriding the
  permission check to reach it. This needed an IMAGE change, not just a manifest one: the
  upstream entrypoint hardcodes `/root/.local/share/Terraria` under `set -euo pipefail` and
  Debian's `/root` is 0700, so the stock image CrashLoopBackOffs under `runAsUser: 1000`. The
  image is now built in-cluster from `templates/Dockerfile.j2`, a single `chown` layer over the
  pinned upstream digest. `stdin`/`tty` kept so the server console stays attachable
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
  drwxr-xr-x on 2026-08-31. The epoch is spelled out because reading it as live state is what
  kept a `DAC_OVERRIDE` grant looking necessary for three weeks after it had stopped being so.

## Editing
- Manifests: `templates/*.yaml.j2` · Defaults: `defaults/main.yml`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "terraria" -e target=daniel-box`
