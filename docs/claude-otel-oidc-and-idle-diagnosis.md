# claude-otel — the OIDC measurements and the idle-versus-broken diagnosis

Working-out moved off `ansible/roles/k8s/claude-otel/CLAUDE.md` (#2993), which a session reads on
every touch of the telemetry stack. The role doc keeps the rules; this page keeps the two Grafana
OIDC measurements, the four behaviours that make an idle stack look broken, and the commands that
exercise the pipeline end to end. `docs/claude-otel-dashboards.md` is the sibling page for the
boards.

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
works, not that anything useful is flowing. `telemetry-health.sh` deliberately checks reachability
and export *failures* — it cannot detect "nobody is using it," and a no-data alarm would fire every
time daniel-box sits idle.

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

## Why the restart triggers are per-workload

Until 2026-09-28 the role's restart task looped over all six workloads gated on
`manifests_render is changed`. That is a per-ROLE signal: it fires when ANY of the role's eight
manifests changed, and `templates/prometheus.yaml.j2` alone changed in 49 commits in 90 days — each
of which restarted Loki, Tempo, kube-state-metrics, the collector and Grafana for an edit none of
them reads (#2858). The per-workload `checksum/config` annotation and the `restart_on` declarations
in the role doc are what replaced it.
