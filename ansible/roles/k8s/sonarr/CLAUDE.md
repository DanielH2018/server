# sonarr — TV library management (\*arr)

Sonarr with an exportarr metrics sidecar and a striptracks mod. Shares the `media-data`
RWX volume with the rest of the \*arr stack. See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
- **Deploy tag:** `--tags "sonarr"`.
- **Route:** `sonarr.<domain>`, behind Authelia.
- **Claims:** `sonarr-config` (2Gi, `longhorn`, backed up — holds the library DB and
  absolute root-folder paths) and the shared `media-data` (mounted, not owned).
- **`k8s_autodeploy: true`**, promoted in slice 7b: `Recreate` + an RWO config PVC is
  now protected by a pre-apply Longhorn snapshot and revert (`k8s_autodeploy_snapshot_pvcs:
  [sonarr-config]`). `media-data` is mounted but explicitly **not** reverted
  (`k8s_autodeploy_unreverted_claims`) — a revert can desync import/rename history from
  files left in place, recoverable by a library rescan.

## Notable
- `sonarr_k8s_log_level: info` / `rotate: "10"` overrides upstream's debug-level default
  on purpose: Longhorn backs up allocated blocks, and a directory that rewrites 50MB of
  debug logs in a rolling window ships that churn forever.
- The exportarr sidecar's image tag is pinned in this role's own `defaults/main.yml`
  (not `group_vars`, so Renovate's k8s-images manager can see it) and kept in lockstep
  with radarr and prowlarr's copies by `test_exportarr_pins_in_lockstep.py`.
- `tasks/verify.yml` reads the Sonarr API through the Service ClusterIP (not the
  ingress, which Authelia would intercept) to check the library actually loaded — a
  running pod alone proves nothing about a broken import.
