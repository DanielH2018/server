# media-volume — the shared media library, one filesystem for seven workloads

Declares `media-data`, the volume seven media-stack roles mount at `/data`. They must all see
**one** filesystem or sonarr/radarr imports silently become copies instead of hardlinks. No
route — infra role, no Deployment of its own.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "media-volume"`
- **Image:** `alpine` (`media_volume_k8s_probe_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `media-data` (not Longhorn (media-local))
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

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
- Every deploy renders `changed=2`, not `changed=0`, on purpose: a hardlink probe Job
  (`hardlink-probe-job.yaml.j2`) re-runs on every apply to catch cluster-state regressions a
  quiet deploy could otherwise hide (a split volume, a claim rebound elsewhere). It also proves
  the PVC actually binds — with `WaitForFirstConsumer`, a `Pending` timeout here means the
  PV/nodeAffinity pair is wrong, not the hardlinks.
- The probe's manifest is rendered into its own directory
  (`/etc/rancher/k3s/manifests/media-volume-probe`), never beside the PV manifests — a Job's pod
  template is immutable, so a re-applied Job there is a no-op or an outright error.
- Directory `mode:` is deliberately unset on the host paths: the library keeps the modes it
  came with from daniel-server, and pinning one would flip them on every apply.
- **`templates/verify-inert/` is the hardlink probe's Flux form, and nothing renders it yet.**
  Neither the manifests role nor the renderer's `verify_files` reads that directory.
  media-volume's flip PR renames it to `templates/verify/`, which makes the renderer emit
  `media-volume-verify`, and deletes the probe tasks. The Job runs
  `files/media_volume_verify.py`, which makes the probe's check with `os.link`. Its name embeds
  a digest of the rendered PV and StorageClass, so Flux re-runs it only when those change, not
  on every apply as Ansible does; `job.yaml.j2`'s `DECIDED:` note has why.
  `tests/test_media_volume_verify.py` renders the renamed copy and runs the Job census over it.
