# media-volume — the shared media library, one filesystem for nine workloads

Declares `media-data`, the volume nine media-stack roles mount at `/data`. They must all see
**one** filesystem or sonarr/radarr imports silently become copies instead of hardlinks. No
route — infra role, no Deployment of its own.

## At a glance
- **Deploy tag:** `--tags "media-volume"`. Must run **before** any role that mounts `media-data`
  (`sonarr` in `containers_list` is ordered right after it, with a comment saying so).
- **Storage:** a **static local PV** (`media-local` StorageClass) at `media_volume_host_path`
  (`/srv/media`, 400Gi advisory only — no quota behind it), deliberately not a dynamic
  `local-path` PVC — see `defaults/main.yml` for the three reasons (naming, reclaim policy,
  `WaitForFirstConsumer` chicken-and-egg).
- **Seeding is opt-in** (`media_volume_sync: false` by default) — a one-time rsync from
  `daniel-server:/home/ubuntu/server/containers/data`, never automatic; see `tasks/sync.yml`'s
  warning before setting it true.

## Notable
- Every deploy renders `changed=2`, not `changed=0`, on purpose: a hardlink probe Job
  (`hardlink-probe-job.yaml.j2`) re-runs on every apply to catch cluster-state regressions a
  quiet deploy could otherwise hide (a split volume, a claim rebound elsewhere). It also proves
  the PVC actually binds — with `WaitForFirstConsumer`, a `Pending` timeout here means the
  PV/nodeAffinity pair is wrong, not the hardlinks.
- The probe's manifest is rendered into its own directory
  (`/etc/rancher/k3s/manifests/media-volume-probe`), never beside the PV manifests — a Job's pod
  template is immutable, so a re-applied Job there is a no-op or an outright error.
- Directory `mode:` is deliberately unset on the host paths: rsync's `-p` imposes
  daniel-server's modes, and pinning one here would fight it every deploy.
