# bazarr — subtitle manager for the *arr stack

Bazarr pulls subtitles for the media Sonarr/Radarr manage. See repo-root `CLAUDE.md` for
shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/bazarr` (`bazarr_k8s_image`)
- **Deploy tag:** `--tags "bazarr"`. Route: `bazarr.<domain>` (Authelia), port 6767.
- **Storage:** `bazarr-config` PVC (`longhorn`, 1Gi) holds the database and subtitle paths.
  Also mounts the shared `media-data` claim (read/write, via subPath) for the files
  themselves.
- **Auto-deploy:** eligible. `k8s_autodeploy: true` since slice 7b (2026-08-21) — the
  `bazarr-config` claim is snapshotted pre-apply by `k8s/volume-snapshot` and reverted on a
  failed deploy, which is what makes the Recreate + RWO migrating-state risk safe to
  auto-promote. `media-data` itself is **not** reverted (a shared claim other roles also
  write); a revert can forget a subtitle file bazarr already wrote there, which is
  self-healing because bazarr checks existence before re-downloading.

## Notable
- `-lsNN` linuxserver tagging hides a breaking bump as a routine patch bump — the reason the
  auto-deploy reasoning in `defaults/main.yml` calls this out explicitly even after
  promotion.
- `templates/networkpolicy-bazarr.yaml.j2` admits monitor-bridge to bazarr's API port on top
  of the netpol-baseline default set (traefik, prometheus, the two cni0 gateways) — without
  it monitor-bridge's health check gets refused, which happened on the check's first three
  cycles (2026-08-29) before the policy existed.

## Editing
- Manifests: `templates/deployment.yaml.j2`, `templates/ingressroute.yaml.j2`,
  `templates/networkpolicy-bazarr.yaml.j2`, `templates/service.yaml.j2`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "bazarr"`.
