# prowlarr — indexer manager for the *arr stack

Prowlarr, with a `flaresolverr` sidecar that solves Cloudflare challenges for the indexers by
rendering attacker-supplied pages in a headless browser.

## At a glance
- **Images:** `prowlarr_k8s_image` (`-lsNN` linuxserver scheme — a breaking bump can hide as a
  routine patch bump), sidecar `prowlarr_k8s_flaresolverr_image` plus an `exportarr` metrics
  sidecar (`prowlarr_exportarr_image`).
- **Route:** `prowlarr.<domain>` · Authelia · port 9696
- **Persists:** `prowlarr-config` PVC (`longhorn`, ~84Mi) — indexer definitions and their API
  keys — plus a separate `prowlarr-flaresolverr-config` for the disposable browser profile.
- **Secrets:** `prowlarr_api_key` (also reused as the exportarr sidecar's credential —
  Prowlarr has no scoped read-only key to mint instead).
- **Deploy tag:** `--tags "prowlarr"`. `k8s_autodeploy: true`, `Recreate` strategy — protected
  by a pre-apply Longhorn snapshot (`k8s_autodeploy_snapshot_pvcs: [prowlarr-config]`) that
  `k8s/manifests` reverts to on a failed deploy.

## Notable
- **A revert desyncs indexer IDs.** Unlike sonarr/radarr/bazarr/jellyfin, which self-heal a
  volume revert (rescan, re-download, existence check), Prowlarr pushes indexer config to the
  other *arr apps and they store Prowlarr-assigned IDs back. A rollback needs a manual re-sync.
- **flaresolverr is fenced to ingress from prowlarr alone**, via
  `templates/networkpolicy-flaresolverr.yaml.j2` — a deliberate narrowing from the Compose-era
  isolated network, since egress policies on this cluster don't hold but ingress does.
- **Log churn, not log size, drives PVC growth.** `prowlarr_k8s_log_level`/`_log_rotate`
  override upstream's noisier defaults; see `roles/k8s/sonarr/defaults/main.yml` for the full
  rationale, shared across the *arr roles.

## Editing
- Manifest: `templates/deployment.yaml.j2`, `templates/deployment-flaresolverr.yaml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "prowlarr"`
