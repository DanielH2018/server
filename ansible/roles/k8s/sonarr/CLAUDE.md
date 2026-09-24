# sonarr — TV library management (\*arr)

Sonarr with an exportarr metrics sidecar and a striptracks mod. Shares the `media-data`
RWX volume with the rest of the \*arr stack. See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "sonarr"`
- **Images:** `lscr.io/linuxserver/sonarr` (`sonarr_k8s_image`), `ghcr.io/onedr0p/exportarr`
  (`sonarr_exportarr_image`)
- **Route:** `sonarr.<domain>` · `sonarr.local.<domain>`, Authelia one_factor
- **Claims:** `sonarr-config` (weekly -> B2 (default target)), `media-data` (not Longhorn
  (media-local))
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **`sonarr-config`** (2Gi, `longhorn`, backed up) holds the library DB and absolute
  root-folder paths; `media-data` is the shared claim (mounted, not owned).
- **Auto-deploy since slice 7b:** `Recreate` + an RWO config PVC is now protected by a pre-apply Longhorn snapshot and revert (`k8s_autodeploy_snapshot_pvcs:
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
- Sonarr is the only *arr whose sidecar gets `--enable-additional-metrics`
  (`additional_metrics=true` on the shared `exportarr` macro). The flag turns on the episode
  collector that publishes `sonarr_episode_monitored_total`, `_unmonitored_total` and
  `_quality_total`. It costs two extra Sonarr API calls per series per scrape, so the
  parameter exists to keep it off radarr and prowlarr, whose collectors have no block behind
  it. `test_only_sonarr_enables_the_additional_metrics_collector` asserts both halves.
- `tasks/verify.yml` reads the Sonarr API through the Service ClusterIP (not the
  ingress, which Authelia would intercept) to check the library actually loaded — a
  running pod alone proves nothing about a broken import.
- **`verify.yml` opens with its own `rollout status` gate**, because `k8s/manifests` queues
  the rollout for `k8s/rollout-drain` at the end of the batch rather than waiting. Until
  2026-09-22 the rootfolder read covered that rollout with a 12-sample `until:` poll, whose
  120s budget was a fifth of the rollout's own — so a slow first boot failed a rollout that
  would have succeeded (#2235). `sonarr_k8s_rollout_timeout` is the one place that budget is
  written; the gate, `manifests_rollout_timeout` and the template's `progressDeadlineSeconds`
  all read it. The deadline has to move with the budget, or `rollout status` fails at the 600s
  Kubernetes default whatever `--timeout` says (#2370).
