# k8s/longhorn-api — resolve the node-local Longhorn API address

This role deploys nothing. It reads the current node's own `longhorn-manager` pod IP and sets
`longhorn_api` (`http://<ip>:9500`), because the Longhorn HTTP API is reachable from a node's
host netns **only** via that node's own manager pod — the `longhorn-backend` ClusterIP
load-balances cross-node and the manager's NetworkPolicy refuses host-originated traffic
(measured 2026-08-21: 2 of 8 GETs against the ClusterIP succeeded).

**No standalone deploy tag.** Callers reach it via `include_role: { name: k8s/longhorn-api }`,
not `--tags longhorn-api`, and MUST pass `tasks_from: resolve.yml` — a bare include runs only
`tasks/main.yml`, which fails loudly by design rather than silently leaving `longhorn_api`
undefined. `k8s/volume-revert` and `k8s/volume-snapshot` are the current callers.

## At a glance
- **`longhorn_api_required`** (default `true`): whether a missing manager pod on this node is
  fatal. `volume-revert` leaves it at the default (a rollback with no API has no fallback).
  `volume-snapshot`'s detached-volume path sets it `false` and reads `longhorn_api_resolved`
  instead of aborting the play.
- **`k8s_autodeploy: false`** — deploys no workload, renders no manifest, pins no image; declared
  rather than auto-deploy-eligible for the same reason `cronjob-gate` and `volume-snapshot` are.

## Notable
- `ignore_errors` on the include does **not** make a missing manager non-fatal — it suppresses
  only a failure of the include statement, not the `fail()` this role raises. The
  `longhorn_api_required` flag is the only way to make that soft.
- Duplicate podIPs from a mid-eviction pod are resolved by taking the first, not by
  concatenating — see `resolve.yml`'s comment on the DaemonSet's `maxSurge: 0` rollout strategy.
