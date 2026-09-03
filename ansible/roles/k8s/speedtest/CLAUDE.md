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
- **REQUIRED one-time post-deploy step (#996):** log in at `speedtest.<domain>`, open
  **Settings -> Data Integration**, and turn on **Prometheus**. Without this the
  `speedtest` scrape job in claude-otel's `prometheus.yaml.j2` returns 404 forever — see
  below for why this cannot be set any other way.

## Notable
- Was the original slice-1 auto-deploy pilot, paused for the same `Recreate` + RWO PVC
  shape every other \*arr-adjacent role was held back for; the snapshot/revert machinery
  added in slice 7b is what let it re-enable.
- The image is pinned `tag@sha256` rather than a bare digest, deliberately: Renovate's
  k8s-defaults manager tracks the tag to raise digest-bump PRs, and a bare `@sha256`
  pin would freeze with no update signal.
- **Prometheus scrape (#996) needs the manual UI toggle above — there is no declarative
  alternative, checked at the exact pinned build.** monitor-bridge's `speedtest` check
  reads the app's REST API and pushes a verdict to Kuma, which keeps a tile, not a
  series — a degradation (the 2026-09-03 78.8 Mbps DOWN, itself below the worst-ever
  119 Mbps this pinned server had returned) had no history. The image natively exposes
  `/prometheus` (`speedtest_tracker_download_bits`, `_upload_bits`, `_ping_ms`, etc.,
  labeled by server/ISP), scraped by claude-otel's `prometheus.yaml.j2`
  (`job_name: speedtest`).

  The pin (`defaults/main.yml`) resolves to upstream `alexjustesen/speedtest-tracker`
  tag `v1.14.7`, LSIO build `ls166` (confirmed via the image's
  `org.opencontainers.image.version` label, not inferred from the tag string). At that
  exact tag: `config/settings.php` sets `default_repository => database` with no env
  override wired in; `app/Settings/DataIntegrationSettings.php` has no `env()` calls;
  the `prometheus_enabled` migration hardcodes its `false` default rather than reading
  one; `MetricsController::__invoke()` gates on `$this->settings->prometheus_enabled`
  with no query-param or header bypass; and no artisan command in the app touches
  `prometheus_enabled` (a repo-wide code search for the string turns up only the UI
  page, the controller, the settings class and its migration/tests). The LSIO wrapper's
  `init-speedtest-tracker-config` script (same file, byte-identical, at both `main` and
  the pinned commit) seeds `DB_CONNECTION`, `APP_KEY` and migrations from env — nothing
  Prometheus-related. This role has no precedent for seeding an app's DB-backed setting
  either (`tasks/main.yml` only creates the PVC and applies manifests) — unlike
  `SPEEDTEST_SCHEDULE` and the other `SPEEDTEST_*` env vars above, which the app *does*
  read directly.

  Until the toggle is flipped, the scrape target 404s and reads `up == 0` — loud, not a
  silently-missing series. `prometheus_allowed_ips` can stay empty: blank means "allow
  any caller", so the in-cluster scrape needs no IP entered once the toggle is on.
