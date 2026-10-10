# observability record

Evidence and history behind `ansible/roles/k8s/observability/CLAUDE.md`, which keeps the rules a session loads on every touch of the role (#2925, #2993). The first half covers the Grafana boards: how the two community boards are seeded, the panel-query traps a 2026-09-10 audit measured, and the B2 usage board that was removed. The second half covers the Grafana OIDC measurements, the four behaviours that make an idle telemetry stack look broken, and the commands that exercise the pipeline end to end.

Nothing tests the board half. `ansible/roles/k8s/observability/tasks/dashboards.yml` and the JSON under `ansible/roles/k8s/observability/files/` are the authority on what is provisioned; where a paragraph here disagrees with them, the tree is right.

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

## The container board reads kubelet's cAdvisor, not Docker's

`Infrastructure/docker-and-system-monitoring.json` (titled *Containers and system monitoring*)
came from the Docker era and keyed every per-container panel on cAdvisor's `name` label. Under
k3s, kubelet's cAdvisor (`job="kubernetes-cadvisor"`) still sets `name`, but to the containerd ID
hash, so the panels returned data with unreadable legends. They also counted the 102 pod sandbox
series beside the 118 real containers (measured 2026-09-30).

On 2026-09-30 (#2806) the panels were re-keyed rather than deleted, because every one of them
returns data. CPU, memory and swap select `container!=""` and group by
`namespace, pod, container`. Network selects `container="", pod!=""` and groups by
`namespace, pod`, because the network namespace belongs to the pod sandbox and no
`container!=""` series carries a network counter. The dead `containergroup` variable, which read
a Docker label no series has, was dropped.

The board stays a board of its own rather than merging into `node-exporter-full.json`. That one
is a pinned community board the fetch script re-seeds, so panels merged into it would conflict
with every re-seed. This board is the only compact view of host totals beside per-container
usage.

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
  `scrape_interval: 1m` (`ansible/roles/k8s/observability/templates/prometheus.yaml.j2`), so a
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
`scripts/lib/b2.py` already holds a tested B2 session that lists versions (`B2Session.list_files`)
and `scripts/diagnostics/probe_lib/b2_ledger.py` a spend ledger, so the wrapper would be thin.
The trap is the number: `b2_list_files` sums CURRENT objects, while B2 bills stored bytes
including hidden versions until lifecycle clears them after 7 days. `check_b2_storage` uses
`b2_list_versions` for exactly that reason. A gauge built on the cheaper call and labelled
"billable" would under-report the thing the 10 GB cap is measured against — a false-GREEN worse
than the missing board.

## Granting the `groups` scope does not deliver the `groups` claim

Measured on 2026-09-06, both times through a real login. With the scope alone, a user in `admins`
got `[{"orgId":1,"name":"Main Org.","role":"Viewer"}]` from `/api/user/orgs`. With the `with_groups`
`claims_policy` added to the Authelia client, the same user got `Admin`. Nothing else changed.

The Viewer version shipped behind a green pod, a green health gate and a passing `-m ui` suite —
none of which can see a role, which is why `/api/user/orgs` is the check.

The working hypothesis for *why*, not itself measured: Authelia keeps `groups` out of the ID token
by default and serves it from `userinfo`, Grafana evaluates `role_attribute_path` against the ID token
first, and this expression always yields a value there because `|| 'Viewer'` fires when the claim is
absent — so `userinfo` is never consulted. Confirming that needs
`GF_LOG_FILTERS=oauth.generic_oauth:debug` and a deploy. **Do not drop the claims policy on the
strength of this paragraph**; the two measurements above are what the config rests on.

## The three OIDC rules

**`GF_SERVER_ROOT_URL` names the LAN host, and that is the whole design constraint.** Grafana
builds its OAuth callback from `root_url` alone, so only one route carries the OIDC login and
**`grafana.<domain>` cannot complete one**. That is why `GF_AUTH_DISABLE_LOGIN_FORM` and
auto-login are deliberately absent: the admin form is the public path in, and the break-glass
path when Authelia is down. Moving `root_url` flips which route works, because both callbacks are
registered.

**The `with_groups` claims policy and the client secret are the other two rules.** The policy is
measured above. Three settings are written twice and drift silently (`require_pkce`, the `groups`
scope, that `claims_policy`), and `ansible/tests/services/test_grafana_authelia_oidc.py` holds both
the `observability` and `authelia` roles to them. The client secret is one credential in two SOPS
keys, `grafana_oidc_client_secret` and `grafana_oidc_client_secret_hash`, so rotate them together.

## UNVERIFIED: the admin form on the public route

Since `root_url` was set (2026-09-06), nobody has checked whether an admin-form login on
`grafana.<domain>` still lands somewhere reachable. Grafana derives its post-login redirect from
`root_url`, which names the LAN host, so a public login may bounce the user to a name that does not
resolve outside the network. Checking it costs a TOTP — the `*.<domain>` `access_control` rule is
`two_factor` — and the `homelab-ui` browser pins only `*.local.<domain>`. Test it before relying on
the public route.

## Why an idle stack is indistinguishable from a broken one

Measured once: the stack ran 47h with every pod Ready, zero export failures and the Kuma heartbeat
green — while carrying **one metric name, no prompt or tool logs, and zero spans**. Nothing was
wrong. Four behaviours combine to produce that appearance.

1. **Telemetry config is read once, at session start.** Editing the `env` block in `settings.json`
   does nothing to a session already running. This stack's keys landed 2026-08-03 12:38; a session
   whose process started 2026-08-02 18:41 exported nothing for its entire life. `/proc/<pid>/environ`
   is not a reliable check either — Claude Code configures its own SDK rather than exporting that
   block, so a session configured correctly showed just one OTEL var there
   (`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta`, itself contradicting the `cumulative`
   in settings). Compare process start time against the settings mtime instead, and against the
   collector's own start time:

   ```bash
   kubectl -n observability get pod -l app=otel-collector -o jsonpath='{.items[0].status.startTime}'
   ```

   A session that began after the config landed but before the collector was up also exports nothing
   for its whole life. The 08-02 18:41 session above predated both.

2. **An idle session emits only `claude_code.session.count`.** It is a cumulative counter re-sent
   every `OTEL_METRIC_EXPORT_INTERVAL`, so `otelcol_receiver_accepted_metric_points` climbs steadily
   and the pipeline looks busy while carrying nothing of substance.

3. **A restarted session's events arrive under a new `session_id`.** `claude --resume` mints a fresh
   id even without `--fork-session`, so a query pinned to the old id reads as dead while the session
   is exporting normally.

4. **A host that exports nothing may simply be unable to log in.** Measured on daniel-server
   2026-08-22: telemetry enabled, `OTEL_EXPORTER_OTLP_ENDPOINT` correct, its collector pod Running
   and scraped — and zero `claude_code_*` series and zero Loki lines from it, ever. `claude -p` there
   exits `Failed to authenticate: OAuth session expired and could not be refreshed`, so no session
   starts and nothing is exported. Every pipeline-side check reads green because the pipeline is
   fine. Run `claude -p` on the quiet host before investigating its collector; re-auth is interactive
   and cannot be done from another host.

   Resolved the same day, which confirms the diagnosis rather than closing it: after re-auth, one
   `claude -p` probe put 77 log lines and 7 `claude_code_*` series carrying `node=daniel-server` into
   the stack within minutes, against seven days of exactly zero. Note the ordering when you re-run
   this — logs appear first (`OTEL_LOGS_EXPORT_INTERVAL` 5s) and metrics lag
   (`OTEL_METRIC_EXPORT_INTERVAL` 10s, plus the Prometheus scrape), so a metrics query run straight
   after a short probe reads as failure while the logs already prove it worked. Check the logs first.

So: **do not diagnose this stack from counters alone.** Rising metric points prove the transport
works, not that anything useful is flowing. The two watchers deliberately check reachability and
export *failures* — `telemetry-health.sh` the OTLP door, monitor-bridge's `with_export_failures`
the collector's send-failed counters. Neither can detect "nobody is using it," and a no-data alarm
would fire every time daniel-box sits idle.

## Verifying the pipeline in one command

A one-shot headless session exercises every signal end to end:

```bash
claude -p "Run the bash command: echo otel-probe-marker. Then reply with only the word: done" \
  --allowedTools Bash --model claude-haiku-4-5-20251001
```

Then read the three backends. They are published on the node's loopback, so `otelq` reaches them
with no plumbing:

```bash
otelq ready                       # expect 200 from loki, prometheus and tempo
otelq labels --name service_name  # expect claude-code
otelq logs '{service_name="claude-code"}' --stream --since 1h --limit 5
otelq metric 'group by (__name__) ({__name__=~"claude.*"})' --rows
```

`otelq` ships with the workstation dotfiles, not with this role, so on a host without it fall back
to curling the ClusterIPs — resolve them first, they change on recreate:

```bash
kubectl -n observability get svc -o custom-columns=NAME:.metadata.name,IP:.spec.clusterIP
```

```bash
# Metrics — expect 4 names, not 1
curl -s http://<otel-collector>:8889/metrics | grep -v '^#' | sed 's/{.*//' | sort -u

# Logs — expect user_prompt / assistant_response / tool_decision / tool_result / api_request.
# The `query=` key and an explicit `start` are both required; omitting either returns empty.
curl -s -G http://<loki>:3100/loki/api/v1/query_range \
  --data-urlencode 'query={service_name="claude-code"}' \
  --data-urlencode 'limit=300' --data-urlencode 'start=<unix-nanoseconds>' \
  | grep -o '"event_name":"[a-z_]*"' | sort | uniq -c

# Traces — expect a claude_code.interaction root span
curl -s -G http://<tempo>:3200/api/search --data-urlencode 'tags=' --data-urlencode 'limit=5'
```

Healthy output after one probe session: metric names `session_count`, `token_usage_tokens`,
`cost_usage_USD`, `active_time_seconds`; the five content event types above, with `user_prompt`
carrying the prompt verbatim (`OTEL_LOG_USER_PROMPTS=1`); and one `claude_code.interaction` trace.
