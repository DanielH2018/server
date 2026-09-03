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
- **Prometheus scrape (#996) needs a one-time manual UI toggle.** monitor-bridge's
  `speedtest` check reads the app's REST API and pushes a verdict to Kuma, which keeps a
  tile, not a series — a degradation (the 2026-09-03 78.8 Mbps DOWN) had no history. The
  image natively exposes `/prometheus` (`speedtest_tracker_download_bits`,
  `_upload_bits`, `_ping_ms`, etc., labeled by server/ISP — see
  `PrometheusMetricsService.php` upstream), scraped by claude-otel's `prometheus.yaml.j2`
  (`job_name: speedtest`). But the app's `prometheus_enabled` setting defaults **off** and
  is a Spatie DB-backed setting (Filament Settings -> Data Integration), with no env var
  or artisan flag this role can set declaratively — unlike `SPEEDTEST_SCHEDULE` and the
  other `SPEEDTEST_*` env vars above. Until someone logs in and flips it once, the scrape
  target 404s and reads `up == 0` (loud, not a silent empty series). `prometheus_allowed_ips`
  can stay empty — blank means "allow any caller", so it needs no IP entered for the
  in-cluster scrape to work once the toggle is on.
