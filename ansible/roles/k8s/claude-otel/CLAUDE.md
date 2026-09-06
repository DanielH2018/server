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

## Grafana logs in through Authelia (OIDC), and the admin form stays on

Grafana is an OIDC client of the Authelia portal — client `grafana` in
`roles/k8s/authelia/templates/config-secret.yaml.j2`, `GF_AUTH_GENERIC_OAUTH_*` in
`templates/grafana.yaml.j2`. Forward-auth is unchanged and still runs in front of the route;
what OIDC removes is the SECOND login Grafana asked for after Authelia had already
authenticated the request. The Authelia `admins` group maps to Grafana Admin, every other
authenticated user to Viewer.

**`GF_SERVER_ROOT_URL` names the LAN host, and that is the whole design constraint.** Grafana
builds its OAuth callback from `root_url` alone and does not vary it by request Host, so one
of the two routes can carry the OIDC login. The LAN name wins because it is what the `-m ui`
suite drives and what a browser on this network should reach without a round trip through
Cloudflare. The consequence, stated plainly: **`grafana.<domain>` cannot complete an OAuth
login.** That is why `GF_AUTH_DISABLE_LOGIN_FORM` and auto-login are deliberately absent —
the admin form is the intended public path in, and the break-glass path when Authelia is down.
Moving `root_url` to the public name flips which route works; the Authelia client already
registers both callbacks, so nothing else has to change.

**UNVERIFIED since `root_url` was set (2026-09-06): whether an admin-form login on
`grafana.<domain>` still lands somewhere reachable.** Grafana derives its post-login redirect
from `root_url`, which now names the LAN host, so a public login may bounce the user to a name
that does not resolve outside the network. Checking it costs a TOTP — the `*.<domain>`
access_control rule is `two_factor` — and the `homelab-ui` browser pins only
`*.local.<domain>`, so nobody has. Test it before relying on the public route.

**Granting the `groups` scope does not deliver the `groups` claim.** Measured on 2026-09-06,
both times through a real login: with the scope alone a user in `admins` got
`[{"orgId":1,"name":"Main Org.","role":"Viewer"}]` from `/api/user/orgs`; with the `with_groups`
`claims_policy` added to the Authelia client, the same user got `Admin`. Nothing else changed.
The Viewer version shipped behind a green pod, a green health gate and a passing `-m ui` suite —
none of which can see a role, which is why `/api/user/orgs` is the check.

The working hypothesis for *why*, not itself measured: Authelia keeps `groups` out of the ID
token by default and serves it from userinfo, Grafana evaluates `role_attribute_path` against
the ID token first, and this expression always yields a value there because `|| 'Viewer'` fires
when the claim is absent — so userinfo is never consulted. Confirming that needs
`GF_LOG_FILTERS=oauth.generic_oauth:debug` and a deploy. **Do not drop the claims policy on the
strength of this paragraph**; the two measurements above are what the config rests on.

Three settings are written twice and fail silently when they drift — `require_pkce` against
`GF_AUTH_GENERIC_OAUTH_USE_PKCE`, the client's `groups` scope against `ROLE_ATTRIBUTE_PATH`,
and that `claims_policy` against the same role path.
`ansible/tests/services/test_grafana_authelia_oidc.py` holds both roles to all three. The client secret is one credential in two SOPS keys:
`grafana_oidc_client_secret` (plaintext, into the `grafana-admin` Secret) and
`grafana_oidc_client_secret_hash` (the pbkdf2 digest Authelia's config holds). Rotate them
together.

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

**A board in a folder `claude_otel_dashboard_folders` does not list is provisioned nowhere.**
`tasks/dashboards.yml` bakes one ConfigMap per listed folder and the Deployment mounts each by
explicit volume name, so the deploy stays green and the board simply never appears. ENFORCED by
`ansible/tests/services/test_dashboard_folders_are_mounted.py`.

**Two panels on `Apps/exportarr-arr-stack.json` guard against an ABSENT series, and that is
what a naive expression gets wrong here.** `Open health issues` reads `sum(...) or vector(0)`
because a `*_system_health_issues` series is absent when an app is clean, and an absent series
renders an empty tile rather than a zero. `Download queue depth` reads
`max(<app>_queue_total) or 0 * max(<app>_system_status)` for the same reason plus one more:
exportarr's queue collector emits NOTHING when the queue is empty, and when it does emit, it
sends a single sample whose value is the whole queue depth but whose `status`/`download_status`/
`download_state` labels describe only the last record it read. `max()` drops those labels, so a
changing tail record does not fork the line.

**`--enable-additional-metrics` does not gate the queue metrics** — a plausible reading that
issue #1380 was filed on and that a live census at an idle moment appears to confirm. exportarr
v2.3.0 registers `NewQueueCollector` unconditionally for sonarr and radarr
(`internal/commands/arr.go`), never for prowlarr; the flag gates per-series `episodefile` and
`episode` calls that feed `sonarr_episode_monitored_total`, `_unmonitored_total` and
`_quality_total`, at two extra app API calls per series per scrape. Issue #1404 held that
separate trade-off and shipped it for sonarr alone: sonarr's sidecar carries the flag, radarr's
and prowlarr's do not, and `test_only_sonarr_enables_the_additional_metrics_collector` asserts
both halves. Measured after the change, sonarr's `scrape_duration_seconds` moved from ~18 ms to
335-403 ms while radarr's and prowlarr's stayed at ~12-15 ms.

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
`scripts/diagnostics/probe_lib/b2_api.py` already holds tested B2 listing (`b2_longhorn_lines`)
and `scripts/diagnostics/probe_lib/b2_ledger.py` a spend ledger, so the wrapper would be thin. The trap is the number: `b2_list_files`
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

**Deploy tag:** `--tags "claude-otel"`.
