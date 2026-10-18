# media-volume — the shared media library, one filesystem for seven workloads

Declares `media-data`, the volume seven media-stack roles mount at `/data`. They must all see
**one** filesystem or sonarr/radarr imports silently become copies instead of hardlinks. No
route — infra role, no Deployment of its own.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=media-volume` with exit 5
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `media-data` (not Longhorn (media-local))
<!-- /generated_from -->

- **Flux applies it, not Ansible** (#5652, Wave D). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it. A merged change
  reaches the cluster when `promote` publishes and verifies the next bundle. To give it back
  to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects. The claim carries
  `kustomize.toolkit.fluxcd.io/prune: disabled`, so neither a revert nor a removal from the
  bundle can delete it.
- **Must run before any role that mounts `media-data`** — the seven mounters (`sonarr`,
  `qbittorrent`, `radarr`, `bazarr`, `jellyfin`, `tdarr`, `janitorr`) each carry
  `depends_on: [media-volume]` on their `containers_list` entry, and every pair is pinned by
  `ansible/tests/deploy/test_k8s_toposort.py::test_documented_pairwise_ordering_survives_an_adversarial_list`.
- **Storage:** a **static local PV** (`media-local` StorageClass) at `media_volume_host_path`
  (`/srv/media`, 400Gi advisory only — no quota behind it), deliberately not a dynamic
  `local-path` PVC — see `defaults/main.yml` for the three reasons (naming, reclaim policy,
  `WaitForFirstConsumer` chicken-and-egg).
- **The host directories belong to the setup plane.** `/srv/media` and its `media/` and
  `torrents/` subdirectories are created by `setup/k8s_host_files` (`tasks/media-volume.yml`
  there), which the GitOps tick applies through `initial_setup.yml`. That task holds a copy of
  `media_volume_host_path`, and `ansible/tests/setup/test_k8s_host_files_pairs.py` holds the
  copy equal to this role's default.
- **HISTORY —** the library was seeded once, at the 2026-08 migration, by an opt-in rsync from
  daniel-server's `containers/data`. That directory no longer exists, so D5 deleted the seeding
  tasks (`tasks/sync.yml`) rather than move them. Nothing backs the library up.

## Notable
- **The hardlink check is the Job in `media-volume-verify`.** The renderer emits
  `templates/verify/` into that Kustomization, which Flux applies once `media-volume` reads
  Ready. The Job runs `files/media_volume_verify.py`, which links a file in `torrents/` into
  `media/` with `os.link` and fails unless the link count reads 2. It also proves the claim
  binds: with `WaitForFirstConsumer`, a pod that stays `Pending` until its 300s deadline means
  the PV's node pin is wrong, not the hardlinks. Its name embeds a digest of the rendered PV
  and StorageClass, so Flux re-runs it only when those change, where the Ansible probe it
  replaced re-ran on every deploy; `job.yaml.j2`'s `DECIDED:` note has why.
  `tests/test_media_volume_verify.py` renders the role and runs the Job census over it.
- **HISTORY —** until the flip, a hardlink probe Job rendered into
  `/etc/rancher/k3s/manifests/media-volume-probe` on daniel-box and ran on every deploy. The
  flip deleted its tasks and template. The staged file stays on the node with no reader; k3s
  auto-applies only its own server manifests directory, so it changes nothing.
- Directory `mode:` is deliberately unset on the host paths: the library keeps the modes it
  came with from daniel-server, and pinning one would flip them on every apply.
