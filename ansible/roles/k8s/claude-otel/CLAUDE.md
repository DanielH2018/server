# claude-otel — Claude Code telemetry stack (k3s, daniel-box)

Loki + Prometheus + Tempo + OTel Collector + Grafana in the `observability` namespace.
Claude Code runs on the **host**, not in a container, and exports to `127.0.0.1:4317` —
a `hostPort` bound to loopback, so the collector never becomes LAN-reachable. See
`defaults/main.yml` for why that IP is not a MetalLB VIP.

Loki, Prometheus and Tempo carry the same treatment on 3100/9090/3200 (added
2026-08-05) so `otelq` — also a host process, and one that hardcodes 127.0.0.1 — can
read them. Their Services stay ClusterIP; the `hostIP` pin is what keeps the node-side
listener off the LAN. Tempo's second port (otlp-grpc 4317) has no `hostPort` on purpose:
the collector owns 4317, and two hostPorts on one number wedge a pod in `Pending`.

## Dashboards

`files/dashboards/**/*.json` is the source of truth for every provisioned board, in six
folders (`claude_otel_dashboard_folders`). Five of them moved here on 2026-08-14 from the
retired Docker grafana role, which had kept them only so this role could read them across
the tree; `AI` has been role-owned since D7. `tasks/dashboards.yml` stages them and bakes a
ConfigMap per folder.

To change a board: edit the JSON here (or edit in the Grafana UI and round-trip with
`scripts/grafana/export_grafana_dashboards.py`, which execs into the observability/grafana pod via
`sudo k3s kubectl`, so expect a sudo prompt), then deploy **claude-otel**.
`scripts/grafana/fetch_grafana_dashboards.py` refreshes the two community boards (1860, 14282).

Every dashboard's datasource ref must resolve to a uid declared in this role's
`templates/grafana.yaml.j2` — the `validate-grafana-dashboards` prek hook parses that file
as the uid registry and fails on an unresolved reference, which is how a hand-imported
board's stale uid gets caught before it renders "No data".

**A live datasource with a dead metric is the gap that hook cannot see, and it has already
cost a board.** `Apps/backups-b2-usage.json` queried `kopia_b2_billable_bytes`, a gauge the
kopia role's `b2-usage.sh` wrote into node-exporter's textfile directory. Kopia retired
2026-08-13 and nothing took the writer over, so all three panels returned no data behind a
healthy Grafana pod for two weeks — the datasource resolved perfectly the whole time. Removed
2026-08-27 rather than left rendering nothing; `probe.py metric kopia_b2_billable_bytes`
returns `no data`, and `/var/lib/node-exporter-textfile` has been empty since 2026-08-14.

Alerting did not go with it: monitor-bridge's `check_b2_storage` still sizes the bucket every
cycle and pushes the **B2 Storage Usage** Kuma monitor. What was lost is the human-facing
runway curve, because that check yields a pass/fail verdict rather than a series.

**Restoring the curve needs a writer, and two things decide whether it is honest.** The
textfile collector is still live (`node-exporter` DaemonSet, `--collector.textfile.directory`),
so the socket exists — but monitor-bridge cannot fill it: it runs `runAsNonRoot` with every
capability dropped, and the directory is `root:root 0755`. That leaves a root host cron, and
`scripts/diagnostics/probe_longhorn.py` already holds tested B2 listing (`b2_longhorn_lines`)
and `scripts/diagnostics/probe_b2_ledger.py` a spend ledger, so the wrapper would be thin. The trap is the number: `b2_list_files`
sums CURRENT objects, while B2 bills stored bytes including hidden versions until lifecycle
clears them after 7 days. `check_b2_storage` uses `b2_list_versions` for exactly that reason.
A gauge built on the cheaper call and labelled "billable" would under-report the thing the
10 GB cap is measured against — a false-GREEN worse than the missing board.

## The trap: an idle stack is indistinguishable from a broken one

Verified 2026-08-05. The stack ran 47h with every pod Ready, zero export failures, and the
Kuma heartbeat green — while carrying **one metric name, no prompt/tool logs, and zero spans**.
Nothing was wrong. Two behaviours combine to produce that appearance:

1. **Telemetry config is read once, at session start.** Editing the `env` block in
   `settings.json` does nothing to a session already running. This stack's keys landed
   2026-08-03 12:38; a session whose process started 2026-08-02 18:41 exported nothing for
   its entire life. `/proc/<pid>/environ` is not a reliable check either — Claude Code
   configures its own SDK rather than exporting that block, so a correctly-configured
   session showed just one OTEL var there
   (`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta`, itself contradicting the
   `cumulative` in settings). Compare process start time against the settings mtime
   instead — and against the collector's own start time
   (`kubectl -n observability get pod -l app=otel-collector -o jsonpath='{.items[0].status.startTime}'`),
   since a session that began after the config landed but before the collector was up
   also exports nothing for its whole life. The 08-02 18:41 session above predated both.
2. **An idle session emits only `claude_code.session.count`.** It is a cumulative counter
   re-sent every `OTEL_METRIC_EXPORT_INTERVAL`, so `otelcol_receiver_accepted_metric_points`
   climbs steadily and the pipeline looks busy while carrying nothing of substance.

3. **A restarted session's events arrive under a new `session_id`.** `claude --resume`
   mints a fresh id even without `--fork-session`, so a query pinned to the old id reads
   as dead while the session is exporting normally.

4. **A host that exports nothing may simply be unable to log in.** Measured on daniel-server
   2026-08-22: telemetry enabled, `OTEL_EXPORTER_OTLP_ENDPOINT` correct, its collector pod
   Running and scraped — and zero `claude_code_*` series and zero Loki lines from it, ever.
   `claude -p` there exits `Failed to authenticate: OAuth session expired and could not be
   refreshed`, so no session starts and nothing is exported. Every pipeline-side check reads
   green because the pipeline is fine. **Run `claude -p` on the quiet host before
   investigating its collector** — an auth failure and an idle host look identical from the
   cluster side, and only one of them is fixed by anything in this role. Re-auth is
   interactive and cannot be done from another host.

   Resolved the same day, which confirms the diagnosis rather than closing it: after re-auth,
   one `claude -p` probe put 77 log lines and 7 `claude_code_*` series carrying
   `node=daniel-server` into the stack within minutes, against seven days of exactly zero.
   Note the ordering when you re-run this — logs appear first (`OTEL_LOGS_EXPORT_INTERVAL`
   5s) and metrics lag (`OTEL_METRIC_EXPORT_INTERVAL` 10s, plus the Prometheus scrape), so a
   metrics query run straight after a short probe reads as failure while the logs already
   prove it worked. Check the logs first.

So: **do not diagnose this stack from counters alone.** Rising metric points prove the
transport works, not that anything useful is flowing. `telemetry-health.sh` deliberately
checks reachability and export *failures* — it cannot detect "nobody is using it", and a
no-data alarm would fire every time daniel-box sits idle.

## Verifying the pipeline in one command

A one-shot headless session exercises every signal end to end:

```bash
claude -p "Run the bash command: echo otel-probe-marker. Then reply with only the word: done" \
  --allowedTools Bash --model claude-haiku-4-5-20251001
```

Then read the three backends. They are published on the node's loopback, so `otelq`
reaches them with no plumbing:

```bash
otelq ready                       # expect 200 from loki, prometheus and tempo
otelq labels --name service_name  # expect claude-code
otelq logs '{service_name="claude-code"}' --stream --since 1h --limit 5
otelq metric 'group by (__name__) ({__name__=~"claude.*"})' --rows
```

`otelq` ships with the workstation dotfiles, not with this role, so on a host without it
fall back to curling the ClusterIPs — resolve them first, they change on recreate:

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
`cost_usage_USD`, `active_time_seconds`; the five content event types above, with
`user_prompt` carrying the prompt verbatim (`OTEL_LOG_USER_PROMPTS=1`); and one
`claude_code.interaction` trace.

## Content logging is on

`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`, `OTEL_LOG_TOOL_DETAILS`, and
`OTEL_LOG_TOOL_CONTENT` are all `1` — prompts, responses, and tool output land in Loki
**verbatim**. That is the intended configuration, and it is the reason the OTLP port is
bound to loopback rather than published. Treat Loki's retention window as holding the same
sensitivity as the transcripts themselves.
