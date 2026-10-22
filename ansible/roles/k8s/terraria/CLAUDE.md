# terraria (k8s) — Vanilla Terraria server

Moved from Docker 2026-08-10 (backup consolidation BL1 — its worlds were the only
irreplaceable data kopia still uniquely protected; the Docker role is in git history at
`git show 2460d0675fd748e70fcbcde87185371ffd62402b:ansible/roles/containers/archive/terraria/`). See repo-root `CLAUDE.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=terraria` with exit 5
- **Image:** `ghcr.io/danielh2018/terraria` (`terraria_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `terraria-config` (weekly -> B2 (default target))
<!-- /generated_from -->

- **Built by CI** from `templates/Dockerfile.j2`: in-cluster from 2026-08-31, and by the
  promote workflow's `images` job since D5.15. It is a single `chown` layer whose only purpose
  is to let the server run as a non-root uid. The **digest pin did not go away**: it is the
  `FROM` line of that Dockerfile, `beardedio/terraria:vanilla-latest` pinned by a digest that
  Renovate bumps. Upstream publishes no pinned vanilla versions, so `vanilla-latest` is the
  only tag available and the digest is what stops it floating.
  What changed is how an update lands. Renovate's built-in dockerfile manager still raises a
  digest-bump PR, and built images carry `automerge: false`, so a person merges it. The merge
  changes the context, so the content tag moves, the `images` job pushes the new tag, and
  `promote` rolls the pod. `image_builder_force` has no Flux-era use: a package refresh that no
  pin moves is a hand PR that bumps a `rebuild_stamp` data key in the context
  (`ansible/roles/k8s/image-builder/CLAUDE.md`). All of this matters because the volume holds
  irreplaceable world data.
- **Flux applies it, not Ansible** (D5.15, Wave C). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a merged
  change reaches the cluster when `promote` publishes and verifies the next bundle. To give it
  back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of
  the flag leaves two writers on the objects. `k8s_autodeploy: false` still stops Renovate's
  automerge of the digest bump. The Deployment is `Recreate`, so a change that rolls it takes
  the server down until the new pod passes its startup probe.
- **Parked at `replicas: 0`.** `ansible/inventory/group_vars/all.yml:terraria_k8s_replicas` is
  0 (#822), so the render holds `replicas: 0` and a roll starts no pod. Flux's drift
  correction now resets a hand scale as well. The value has three readers on two planes: this
  role's Deployment, which `promote` applies, and game-stats' `terraria-stats` Deployment and
  observability's `terraria-stats` scrape job, which Ansible deploys. So unparking is a merge
  that `promote` applies for the server plus Ansible deploys of game-stats and observability.
  The restore checklist on #822 has the rest. The comment above the value in `group_vars`
  still names `--tags terraria`, which `deploy.sh` refuses with exit 5.
- **Where the tasks went.** `tasks/main.yml` holds two includes, `k8s/image-builder` and
  `k8s/manifests`, and no Flux path runs either. They stay for the handback. The build became
  the `images` job (D5.4), which pushes `ghcr.io/danielh2018/terraria:<content tag>`. The
  defaults name `<k8s_registry_pull_host>/terraria`; the renderer rewrites that ref to the GHCR
  one with `imagePullPolicy: IfNotPresent`
  (`scripts/renderer/renderer_lib/images.py:ghcr_refs`). The Kustomization's `wait` replaces
  the rollout wait.
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
  image is now built from `templates/Dockerfile.j2`, a single `chown` layer over the
  pinned upstream digest. `stdin`/`tty` kept so the server console stays attachable
- **Not Traefik-routed**: the `containers_list` entry carries no port or hostname.

## Notable
- **Logs ship via the loki-homelab promtail DaemonSet** (`{container="terraria"}`,
  `job="k8s"`). That stream feeds `k8s/terraria-stats`, which reads it from the cluster
  Loki.
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
