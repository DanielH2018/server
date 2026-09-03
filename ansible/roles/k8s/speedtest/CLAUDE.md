# speedtest — speedtest-tracker

`linuxserver/speedtest-tracker`, a periodic self-hosted speed test with history. See
repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Deploy tag:** `--tags "speedtest"`.
- **Route:** `speedtest.<domain>`, behind Authelia.
- **Claim:** `speedtest-config`, `longhorn-nobackup`, 1Gi. `/config` holds a real
  `database.sqlite`, but its Laravel `APP_KEY` lives in SOPS so a rebuilt instance still
  works — losing the volume loses history, not function.
- **`k8s_autodeploy: true`**, promoted in slice 7b: `Recreate` + an RWO PVC seeded
  through `k8s/volume-claim` is now protected by a pre-apply Longhorn snapshot and
  revert (`k8s_autodeploy_snapshot_pvcs: [speedtest-config]`).

## Notable
- Was the original slice-1 auto-deploy pilot, paused for the same `Recreate` + RWO PVC
  shape every other \*arr-adjacent role was held back for; the snapshot/revert machinery
  added in slice 7b is what let it re-enable.
- The image is pinned `tag@sha256` rather than a bare digest, deliberately: Renovate's
  k8s-defaults manager tracks the tag to raise digest-bump PRs, and a bare `@sha256`
  pin would freeze with no update signal.
