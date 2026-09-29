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

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "claude-otel"`
- **Images:** `otel/opentelemetry-collector-contrib` (`claude_otel_collector_image`),
  `grafana/loki` (`claude_otel_loki_image`), `prom/prometheus`
  (`claude_otel_prometheus_image`), `grafana/grafana` (`claude_otel_grafana_image`),
  `registry.k8s.io/kube-state-metrics/kube-state-metrics`
  (`claude_otel_kube_state_metrics_image`), `grafana/tempo` (`claude_otel_tempo_image`)
- **Route:** `grafana.<domain>` · `grafana.local.<domain>`, Authelia one_factor
- **Claims:** `grafana-data` (no backup (StorageClass longhorn-nobackup)), `loki-data` (no
  backup (StorageClass longhorn-nobackup)), `prometheus-data` (no backup (StorageClass
  longhorn-nobackup)), `tempo-data` (no backup (StorageClass longhorn-nobackup))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — observability — Claude Code's own
  telemetry stack, six sub-images across several Deployments. ALSO each stateful component
  (loki/grafana/prometheus/tempo) is Recreate + its own PVC — compounding, not
  single-component. COUPLING NOTE for a future promotion: UI-edited Grafana dashboards live
  only in the PVC until an export script round-trips them to git; a revert discards unsaved
  edits
<!-- /generated_from -->

- **`probe.py health claude-otel` gates all six workloads in `observability`**, none of them
  named claude-otel.
- **OIDC login on the LAN route** — see *Grafana OIDC* below.
- **Each stateful sub-service is `Recreate` on its own PVC**; `grafana-data` is
  `longhorn-nobackup`.

## Eviction tiers

DECIDED: claude-otel tiers — Prometheus is `homelab-critical`; the other five pod templates
are `homelab-best-effort`. Every pod template here names a class since 2026-09-17 (#1851).
Before that none did, which placed the whole stack at priority 0, below `homelab-best-effort`
(1000) — `roles/setup/k3s/templates/priorityclass.yaml.j2` explains why that class exists
as a real value rather than as the absence of one.

Prometheus earns tier 1 because tier 1 names alerting in its own description ("edge, auth,
DNS, WAF, registry, alerting") and monitor-bridge, itself tier 1, reads it every cycle:
`PROMETHEUS_URL` in `monitor-bridge/templates/env-secret.yaml.j2` points at this Prometheus,
and the disk, memory, OOM, restart, PVC-fullness and scrape-target verdicts all read from it. Evicting Prometheus under pressure would degrade the
bridge's verdicts at the moment pressure makes them matter. The exporters it scrapes
(kube-state-metrics here, node-exporter and gpu-exporter in their own roles) stay tier 4: a
lost exporter surfaces loudly as `up == 0` through the scrape-target check, so the exporter
does not need to outlive the thing that reports it missing. Grafana, Loki, Tempo and the
collector are dashboards and Claude Code telemetry, which is tier 4 by the class's own
description. `ansible/tests/k8s/test_pod_template_hygiene.py` enforces that every
long-running pod template names one of the four classes.

## One config change, one restart

This role sets `manifests_rollout: ''`, so `k8s/manifests` neither waits nor restarts for it
and the role does both itself. Until 2026-09-28 its restart task looped over all six workloads
gated on `manifests_render is changed`. That is a per-ROLE signal: it fires when ANY of the
role's eight manifests changed, and `templates/prometheus.yaml.j2` alone changed in 49 commits
in 90 days — each of which restarted Loki, Tempo, kube-state-metrics, the collector and Grafana
for an edit none of them reads (#2858).

Each workload now carries its own trigger, and there are two kinds:

- **A ConfigMap edit rolls its own pod.** Each template captures its ConfigMap body into a
  Jinja variable and hashes it into that workload's pod template as `checksum/config`
  (`ansible/templates/checksum-annotation.yml.j2`). The apply then changes one pod template,
  rolls one pod, and reports it through `manifests_rolled_by_apply`. kube-state-metrics renders
  no ConfigMap and needs no annotation — every input it has is already in its pod template.
- **A Secret edit still needs the restart task**, for prometheus and grafana only. Hashing a
  Secret's bytes into a world-readable annotation would open a read path over it, so those two
  declare `restart_on: [secret]` in
  `ansible/roles/k8s/claude-otel/defaults/main.yml:claude_otel_stabilise_workloads` and the
  other four declare `restart_on: []`. `manifests_secret_render` is one register over all three
  secret manifests, so rotating Grafana's admin password also restarts Prometheus.

`restart_on` is read twice, and both readers must agree or the deploy fails loudly:
the restart task here, and the release record's `rollouts[].restart`
(`manifests_restart_triggers_default` in `ansible/roles/k8s/manifests/defaults/main.yml`).
`probe.py health claude-otel` reads a `restart: true` with no newer `restartedAt` as NOT ROLLED,
which is what turns a wrong `restart_on` into a red deploy rather than a silent one.

**Widen the capture when you add a `data:` key.** Four of the five templates capture ONE key's
body — grafana's captures the whole `data:` block — so a second key added to `prometheus.yaml.j2`
(recording rules, an alerts file) would land outside the hash. The ConfigMap would change, the
pod template would not, and the annotation would be present throughout: the original bug with
the gate blinded.

The three ways this goes QUIET are guarded by
`ansible/tests/services/test_claude_otel_config_rolls_one_workload.py::test_a_workload_mounting_a_role_configmap_carries_the_checksum_annotation`,
`ansible/tests/services/test_claude_otel_config_rolls_one_workload.py::test_every_workload_reading_a_role_secret_declares_restart_on_secret`
and
`ansible/tests/services/test_claude_otel_config_rolls_one_workload.py::test_no_role_configmap_grew_a_key_the_annotation_does_not_hash`.

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

`files/dashboards/**/*.json` is the source of truth for every provisioned board, in six folders
(`claude_otel_dashboard_folders`). `tasks/dashboards.yml` stages them and bakes a ConfigMap per
folder. To change a board: edit the JSON here, or edit it in the Grafana UI and round-trip it with
`scripts/grafana/export_grafana_dashboards.py`, then deploy **claude-otel**. The two community
boards are seeded by `scripts/grafana/fetch_grafana_dashboards.py` at a pinned revision.

Three editing rules, each of which otherwise leaves a green deploy and a blank board:

- **A board in a folder `claude_otel_dashboard_folders` does not list is provisioned nowhere.**
  `tasks/dashboards.yml` bakes one ConfigMap per listed folder and the Deployment mounts each by
  explicit volume name, so the deploy stays green and the board simply never appears. ENFORCED by
  `ansible/tests/services/test_dashboard_folders_are_mounted.py`.
- **A hand-edited board must stay in the writers' form** — keys sorted at every depth, 2-space
  indent, non-ASCII literal — or the next export rewrites it and the drift read (`git diff
  --stat` after an export) shows a change that is not one. ENFORCED by
  `ansible/tests/services/test_committed_dashboards_are_a_fixed_point_of_the_exporter.py`, whose
  oracle is `scripts/grafana/export_grafana_dashboards.py:dump`.
- **Every dashboard's datasource ref must resolve to a uid declared in this role's
  `templates/grafana.yaml.j2`.** The `validate-grafana-dashboards` prek hook parses that file as
  the uid registry and fails on an unresolved reference, which is how a hand-imported board's
  stale uid gets caught before it renders "No data".

`docs/claude-otel-dashboards.md` carries the rest, and is what to read before debugging a panel:
where the boards came from, how a fetch is revision-pinned and why it refuses to overwrite, the
two exportarr panels written around an absent series, the three ways a panel reads "No data"
behind a datasource that resolves, and the B2 usage board that rendered nothing for two weeks.

## The trap: an idle stack is indistinguishable from a broken one

Measured once: the stack ran 47h with every pod Ready, zero export failures, and the
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
