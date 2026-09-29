# claude-otel — Claude Code telemetry stack (k3s, daniel-box)

Loki + Prometheus + Tempo + OTel Collector + Grafana in the `observability` namespace.
Claude Code runs on the **host**, not in a container, and exports to `127.0.0.1:4317` — a
`hostPort` bound to loopback, so the collector never becomes LAN-reachable. Loki, Prometheus
and Tempo carry the same `hostIP` pin on 3100/9090/3200 for `otelq`, another host process, and
Tempo's otlp-grpc 4317 has none because two hostPorts on one number wedge a pod in `Pending`.

Two docs/ pages carry the working-out: `docs/claude-otel-dashboards.md` for the boards, and
`docs/claude-otel-oidc-and-idle-diagnosis.md` for the OIDC measurements, the idle-versus-broken
diagnosis and the verification commands.

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

**`probe.py health claude-otel` gates all six workloads in `observability`**, none of them
named claude-otel.

## Eviction tiers

DECIDED: claude-otel tiers — Prometheus is `homelab-critical`; the other five pod templates
are `homelab-best-effort` (#1851). Prometheus earns tier 1 because monitor-bridge, itself tier
1, reads it every cycle through `PROMETHEUS_URL`; the exporters it scrapes stay tier 4, because
a lost exporter surfaces loudly as `up == 0`. `ansible/tests/k8s/test_pod_template_hygiene.py`
holds every long-running pod template to one of the four classes.

## One config change, one restart

This role sets `manifests_rollout: ''`, so `k8s/manifests` neither waits nor restarts for it
and each workload carries its own trigger (#2858). A ConfigMap edit rolls its own pod: the
template hashes the ConfigMap body into that pod template as `checksum/config`
(`ansible/templates/checksum-annotation.yml.j2`). A Secret edit goes through the restart task
instead, because hashing a Secret into a world-readable annotation would open a read path over
it — prometheus and grafana declare `restart_on: [secret]` in
`ansible/roles/k8s/claude-otel/defaults/main.yml:claude_otel_stabilise_workloads` and the other
four `restart_on: []`. `manifests_secret_render` is one register over all three secret
manifests, so rotating Grafana's admin password also restarts Prometheus.

`restart_on` is read twice and both readers must agree — the restart task here, and the release
record's `rollouts[].restart`. A wrong value lands as a red deploy, because `probe.py health
claude-otel` reads a `restart: true` with no newer `restartedAt` as NOT ROLLED.

**Widen the capture when you add a `data:` key.** Four of the five templates capture ONE key's
body, so a second key in `prometheus.yaml.j2` lands outside the hash and the pod never rolls.
The three ways this goes QUIET are guarded by
`ansible/tests/services/test_claude_otel_config_rolls_one_workload.py`.

## Grafana logs in through Authelia (OIDC), and the admin form stays on

Grafana is an OIDC client of the Authelia portal — client `grafana` in
`roles/k8s/authelia/templates/config-secret.yaml.j2`, `GF_AUTH_GENERIC_OAUTH_*` in
`templates/grafana.yaml.j2`. `admins` maps to Grafana Admin, everyone else to Viewer.

**`GF_SERVER_ROOT_URL` names the LAN host, and that is the whole design constraint.** Grafana
builds its OAuth callback from `root_url` alone, so only one route carries the OIDC login and
**`grafana.<domain>` cannot complete one**. That is why `GF_AUTH_DISABLE_LOGIN_FORM` and
auto-login are deliberately absent: the admin form is the public path in, and the break-glass
path when Authelia is down. Moving `root_url` flips which route works; both callbacks are
already registered.

**Keep the `with_groups` claims policy on the Authelia client** — the `groups` scope alone
leaves an `admins` user logged in as Viewer behind a green pod and a green health gate, and
`/api/user/orgs` is the only check that sees it. Three settings are written twice and fail
silently when they drift (`require_pkce`, the `groups` scope, that `claims_policy`);
`ansible/tests/services/test_grafana_authelia_oidc.py` holds both roles to all three. The
client secret is one credential in two SOPS keys — `grafana_oidc_client_secret` and
`grafana_oidc_client_secret_hash` — so rotate them together.

## Dashboards

`files/dashboards/**/*.json` is the source of truth for every provisioned board, in the folders
`claude_otel_dashboard_folders` lists. To change one: edit the JSON here, or edit it in the UI
and round-trip it with `scripts/grafana/export_grafana_dashboards.py`, then deploy
**claude-otel**. Three editing rules each otherwise leave a green deploy behind a blank board: a
board in an unlisted folder is provisioned nowhere; a hand-edited board must stay in the
writers' form; and every datasource ref must resolve to a uid `templates/grafana.yaml.j2`
declares. The dashboards page states each in full, and two tests plus the
`validate-grafana-dashboards` prek hook enforce them. Read that page before debugging a panel.

## The trap: an idle stack is indistinguishable from a broken one

The stack has run 47h with every pod Ready, zero export failures and a green heartbeat while
carrying one metric name, no logs and zero spans — with nothing wrong. **Run `claude -p` on the
quiet host before investigating its collector**, read the logs before the metrics, and **do not
diagnose this stack from counters alone**. The four behaviours behind that appearance are
measured on the docs page. `telemetry-health.sh` checks reachability and export *failures*
deliberately — it cannot detect "nobody is using it".

## Content logging is on

`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`, `OTEL_LOG_TOOL_DETAILS` and
`OTEL_LOG_TOOL_CONTENT` are all `1`, so prompts, responses and tool output land in Loki
**verbatim**. That is why the OTLP port is bound to loopback rather than published. Treat
Loki's retention window as holding the same sensitivity as the transcripts.
