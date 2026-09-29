# claude-otel: the dashboard record

Where the provisioned Grafana boards came from, how the two community boards are seeded, and the
panel-query traps a 2026-09-10 audit measured. The editing rules — where the JSON lives, which
folders are provisioned, which datasource `uid` values resolve, what to run after a change — are in
`ansible/roles/k8s/claude-otel/CLAUDE.md`, the file a session loads on every touch of the role.
This page is where the evidence and the history live, kept out of that file so the rules stay
short (#2925, the same split `docs/monitor-bridge-checks.md` makes for monitor-bridge and
`docs/volume-snapshot-drills.md` for volume-snapshot).

Nothing tests this page. `ansible/roles/k8s/claude-otel/tasks/dashboards.yml` and the JSON under
`ansible/roles/k8s/claude-otel/files/` are the authority on what is provisioned; where a
paragraph here disagrees with them, the tree is right.

## Where the boards came from

Five of the six folders moved into this role on 2026-08-14 from the retired Docker `grafana` role,
which had kept them only so this role could read them across the tree; `AI` has been role-owned
since D7.

## Seeding and round-tripping a board

`scripts/grafana/export_grafana_dashboards.py` round-trips a board edited in the Grafana UI back
to the JSON in the role. It execs into the `observability/grafana` pod via `sudo k3s kubectl`, so
expect a sudo prompt.

`scripts/grafana/fetch_grafana_dashboards.py` seeds the two community boards, each pinned to a
`grafana.com` REVISION in `scripts/grafana/fetch_grafana_dashboards.py:DASHBOARDS` (1860 at
revision 45, 14282 at revision 1). Until 2026-09-28 it fetched `revisions/latest`, so an
unrelated re-run could rewrite all 13,746 lines of `node-exporter-full.json`. Bump a revision in
its own commit and read the diff.

A fresh fetch still differs from the committed boards: both carry hand edits the script does not
reproduce, and query-variable defaults resolve against the live Prometheus. So it refuses to
overwrite a differing board and writes nothing (#2912); `--overwrite` takes the upstream form,
after which re-apply the hand edits.

## Two panels guard against an absent series

Two panels on `Apps/exportarr-arr-stack.json` guard against an ABSENT series, and that is what a
naive expression gets wrong here. `Open health issues` reads `sum(...) or vector(0)` because a
`*_system_health_issues` series is absent when an app is clean, and an absent series renders an
empty tile rather than a zero. `Download queue depth` reads
`max(<app>_queue_total) or 0 * max(<app>_system_status)` for the same reason plus one more:
exportarr's queue collector emits NOTHING when the queue is empty, and when it does emit, it
sends a single sample whose value is the whole queue depth but whose `status`/`download_status`/
`download_state` labels describe only the last record it read. `max()` drops those labels, so a
changing tail record does not fork the line.

## `--enable-additional-metrics` does not gate the queue metrics

That is a plausible reading that issue #1380 was filed on and that a live census at an idle
moment appears to confirm. exportarr v2.3.0 registers `NewQueueCollector` unconditionally for
sonarr and radarr (`internal/commands/arr.go`), never for prowlarr; the flag gates per-series
`episodefile` and `episode` calls that feed `sonarr_episode_monitored_total`,
`_unmonitored_total` and `_quality_total`, at two extra app API calls per series per scrape.
Issue #1404 held that separate trade-off and shipped it for sonarr alone: sonarr's sidecar
carries the flag, radarr's and prowlarr's do not, and
`ansible/tests/services/test_exportarr_sidecars.py::test_only_sonarr_enables_the_additional_metrics_collector`
asserts both halves. Measured after the change, sonarr's `scrape_duration_seconds` moved from
~18 ms to 335-403 ms while radarr's and prowlarr's stayed at ~12-15 ms.

## Three ways a panel reads "No data" behind a resolving datasource

A 2026-09-10 audit ran every panel's query against the live backends: 98 of 597 targets returned
nothing. All three causes below produce a healthy pod, a passing `-m ui` suite and a blank panel,
and all three were repaired in that pass.

- **A ported board's label names are not this cluster's.** The four CrowdSec boards filtered on
  `machine`; this cluster's CrowdSec exports `node`. A `label_values(up, machine)` variable
  returning nothing interpolates EMPTY into every panel, so the board cannot even build a
  query — worse than one bad panel. `crowdsec-insight` and `lapi-metrics` were 100% dead.
  Not every rename is mechanical: **the LAPI target (`job="crowdsec"`) carries no `node` label at
  all**, so `lapi-metrics` keys on `instance` instead, and `cs_alerts` /
  `cs_bucket_pour_seconds_bucket` come only from the engine — a per-node board cannot filter them
  by node.
- **`[1m]` against a 1-minute scrape returns nothing.** Every application job here sets
  `scrape_interval: 1m` (`ansible/roles/k8s/claude-otel/templates/prometheus.yaml.j2`), so a
  `rate()`/`increase()` over a literal `[1m]` — or over `$__interval`, which is SHORTER than 1m
  on a typical range — sees one sample and yields no result. Use `$__rate_interval`, which
  Grafana derives from the datasource's `timeInterval`. This killed panels on `traefik-custom`
  and all three CrowdSec boards.
- **The right data in the wrong Loki.** Both Claude Code boards queried uid `bf4q19tuivta8e`
  (`loki-homelab`), which has no `service_name="claude-code"` stream — the collector exports to
  `http://loki:3100`, uid `loki`. The deploy annotation on those boards reads `{job="syslog"}`
  and correctly stays on `loki-homelab`, so the two `uid` values coexist in one file on purpose.

**A panel that is empty because nothing happened is not a defect.** CrowdSec emits `cs_buckets`,
`cs_bucket_created_total` and `cs_bucket_overflowed_total` only once a bucket exists, so those
panels stay blank until a scenario fires and come alive during the incident you want them for.
Distinguish that from a dead selector by asking whether the metric is absent over a RANGE, not at
an instant: `prowlarr_indexer_queries_total` returns nothing instantaneously and 14 series over
`[1h]`.

## A live datasource with a dead metric: the B2 usage board

**A live datasource with a dead metric is the gap the `validate-grafana-dashboards` hook cannot
see, and it has already cost a board.** `Apps/backups-b2-usage.json` queried
`kopia_b2_billable_bytes`, a gauge the kopia role's `b2-usage.sh` wrote into node-exporter's
textfile directory. Kopia retired 2026-08-13 and nothing took the writer over, so all three
panels returned no data behind a healthy Grafana pod for two weeks — the datasource resolved
perfectly the whole time. Removed 2026-08-27 rather than left rendering nothing;
`probe.py metric kopia_b2_billable_bytes` returns `no data`, and `/var/lib/node-exporter-textfile`
has been empty since 2026-08-14.

Alerting did not go with it: monitor-bridge's `check_b2_storage` still sizes the bucket every
cycle and pushes the **B2 Storage Usage** Kuma monitor. What was lost is the human-facing runway
curve, because that check yields a pass/fail verdict rather than a series.

**Restoring the curve needs a writer, and two things decide whether it is honest.** The textfile
collector is still live (`node-exporter` DaemonSet, `--collector.textfile.directory`), so the
socket exists — but monitor-bridge cannot fill it: it runs `runAsNonRoot` with every capability
dropped, and the directory is `root:root 0755`. That leaves a root host cron, and
`scripts/diagnostics/probe_lib/b2_api.py` already holds tested B2 listing (`b2_longhorn_lines`)
and `scripts/diagnostics/probe_lib/b2_ledger.py` a spend ledger, so the wrapper would be thin.
The trap is the number: `b2_list_files` sums CURRENT objects, while B2 bills stored bytes
including hidden versions until lifecycle clears them after 7 days. `check_b2_storage` uses
`b2_list_versions` for exactly that reason. A gauge built on the cheaper call and labelled
"billable" would under-report the thing the 10 GB cap is measured against — a false-GREEN worse
than the missing board.
