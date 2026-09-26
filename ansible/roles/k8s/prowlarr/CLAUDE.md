# prowlarr — indexer manager for the *arr stack

Prowlarr, with a `flaresolverr` sidecar that solves Cloudflare challenges for the indexers by
rendering attacker-supplied pages in a headless browser.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "prowlarr"`
- **Images:** `lscr.io/linuxserver/prowlarr` (`prowlarr_k8s_image`),
  `ghcr.io/flaresolverr/flaresolverr` (`prowlarr_k8s_flaresolverr_image`), `alpine`
  (`prowlarr_k8s_probe_image`), `ghcr.io/onedr0p/exportarr` (`prowlarr_exportarr_image`)
- **Route:** `prowlarr.<domain>` · `prowlarr.local.<domain>`, Authelia one_factor
- **Claim:** `prowlarr-config` (weekly -> B2 (default target))
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **The `-lsNN` linuxserver tag scheme** means a breaking bump can hide as a routine patch
  bump. `prowlarr_k8s_flaresolverr_image` and `prowlarr_exportarr_image` are sidecars.
- **Port:** 9696
- **Persists:** `prowlarr-config` PVC (`longhorn`, ~84Mi) — indexer definitions and their API
  keys — plus a separate `prowlarr-flaresolverr-config` for the disposable browser profile.
- **Secrets:** `prowlarr_api_key` (also reused as the exportarr sidecar's credential —
  Prowlarr has no scoped read-only key to mint instead).
- **`Recreate` strategy, auto-deployed anyway** — protected by a pre-apply Longhorn snapshot (`k8s_autodeploy_snapshot_pvcs: [prowlarr-config]`) that
  `k8s/manifests` reverts to on a failed deploy.

## Notable
- **A revert desyncs indexer IDs.** Unlike sonarr/radarr/bazarr/jellyfin, which self-heal a
  volume revert (rescan, re-download, existence check), Prowlarr pushes indexer config to the
  other *arr apps and they store Prowlarr-assigned IDs back. A rollback needs a manual re-sync.
- **flaresolverr is fenced to ingress from prowlarr alone**, via
  `templates/networkpolicy-flaresolverr.yaml.j2` — a deliberate narrowing from the Compose-era
  isolated network, since egress policies on this cluster don't hold but ingress does.
- **Both Deployments wait 780s, above the shared default.** A cold pull on daniel-server took 8m54s for the
  292 MB flaresolverr image and 3m41s for prowlarr's own (#2369). `prowlarr_k8s_rollout_timeout`
  sets the drain's wait and each template's `progressDeadlineSeconds`. The deadline has to move
  with the wait, or `rollout status` fails at the 600s default anyway.
- **Log churn, not log size, drives PVC growth.** `prowlarr_k8s_log_level`/`_log_rotate`
  override upstream's noisier defaults; see `roles/k8s/sonarr/defaults/main.yml` for the full
  rationale, shared across the *arr roles.

## Editing
- Manifest: `templates/deployment.yaml.j2`, `templates/deployment-flaresolverr.yaml.j2`
- Deploy: `./scripts/deploy.sh --tags "prowlarr"`
