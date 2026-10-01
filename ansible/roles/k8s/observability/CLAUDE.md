# observability — the cluster metrics plane plus the Claude Code telemetry stack (k3s, daniel-box)

Six workloads in the `observability` namespace, this role their only tenant. Claude telemetry
is the collector, Tempo and the Loki here. The cluster's metrics plane is Prometheus — what
monitor-bridge reads through `PROMETHEUS_URL` — plus kube-state-metrics and Grafana. Cluster
LOGS are the exception: `loki-homelab` holds those, separate by decision KL1. Named
`claude-otel` until 2026-10-01 (#2911).

Claude Code runs on the **host**, not in a container, and exports to `127.0.0.1:4317` — a
`hostPort` bound to loopback, so the collector never becomes LAN-reachable. Loki, Prometheus
and Tempo carry the same `hostIP` pin on 3100/9090/3200 for `otelq`, another host process, and
Tempo's otlp-grpc 4317 has none because two hostPorts on one number wedge a pod in `Pending`.

The working-out lives in `docs/observability-dashboards.md` (the boards) and
`docs/observability-oidc-and-idle-diagnosis.md` (OIDC measurements, idle versus broken, the
verification commands).

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "observability"`
- **Images:** `otel/opentelemetry-collector-contrib` (`observability_collector_image`),
  `grafana/loki` (`observability_loki_image`), `prom/prometheus`
  (`observability_prometheus_image`), `grafana/grafana` (`observability_grafana_image`),
  `registry.k8s.io/kube-state-metrics/kube-state-metrics`
  (`observability_kube_state_metrics_image`), `grafana/tempo` (`observability_tempo_image`)
- **Route:** `grafana.<domain>` · `grafana.local.<domain>`, Authelia one_factor
- **Claims:** `grafana-data` (no backup (StorageClass longhorn-nobackup)), `loki-data` (no
  backup (StorageClass longhorn-nobackup)), `prometheus-data` (no backup (StorageClass
  longhorn-nobackup)), `tempo-data` (no backup (StorageClass longhorn-nobackup))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — observability — the cluster metrics
  plane and Claude Code's own telemetry stack, six sub-images across several Deployments. ALSO
  each stateful component (loki/grafana/prometheus/tempo) is Recreate + its own PVC —
  compounding, not single-component. COUPLING NOTE for a future promotion: UI-edited Grafana
  dashboards live only in the PVC until an export script round-trips them to git; a revert
  discards unsaved edits
<!-- /generated_from -->

**`probe.py health observability` gates all six workloads in `observability`**, none of them
named observability.

## Eviction tiers

DECIDED: observability tiers — Prometheus is `homelab-critical`; the other five pod templates
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
`ansible/roles/k8s/observability/defaults/main.yml:observability_stabilise_workloads` and the other
four `restart_on: []`. `manifests_secret_render` is one register over all three secret
manifests, so rotating Grafana's admin password also restarts Prometheus.

`restart_on` is read twice and both readers must agree — the restart task here, and the release
record's `rollouts[].restart`. A wrong value lands as a red deploy, because `probe.py health
observability` reads a `restart: true` with no newer `restartedAt` as NOT ROLLED.

**Widen the capture when you add a `data:` key.** Four of the five templates capture ONE key's
body, so a second key in `prometheus.yaml.j2` lands outside the hash and the pod never rolls.
The three ways this goes QUIET are guarded by
`ansible/tests/services/test_observability_config_rolls_one_workload.py`.

The snapshot that hands these six to the play gate fires on the render AND the apply both
changing, so a comment-only edit buys no soak (#3133).

## Grafana logs in through Authelia (OIDC), and the admin form stays on

Grafana is an OIDC client of the Authelia portal — client `grafana` in
`roles/k8s/authelia/templates/config-secret.yaml.j2`, `GF_AUTH_GENERIC_OAUTH_*` in
`templates/grafana.yaml.j2`. `admins` maps to Grafana Admin, everyone else to Viewer.

Three rules hold this together, each stated in full under *The three OIDC rules* on
`docs/observability-oidc-and-idle-diagnosis.md`: `GF_SERVER_ROOT_URL` decides which single
route can complete an OIDC login, so the admin form stays on as the public and break-glass
path; the Authelia client keeps its `with_groups` claims policy, without which an `admins` user
logs in as Viewer behind a green health gate; and the client secret is one credential in two
SOPS keys (`grafana_oidc_client_secret`, `grafana_oidc_client_secret_hash`), rotated together.
`ansible/tests/services/test_grafana_authelia_oidc.py` holds both roles to the three settings
that are written twice.

## Dashboards

`files/dashboards/**/*.json` is the source of truth for every provisioned board, in the folders
`observability_dashboard_folders` lists. To change one: edit the JSON here, or edit it in the UI
and round-trip it with `scripts/grafana/export_grafana_dashboards.py`, then deploy
**observability**. Three editing rules each otherwise leave a green deploy behind a blank board:
a board in an unlisted folder is provisioned nowhere; a hand-edited board must stay in the
writers' form; every datasource ref must resolve to a uid `templates/grafana.yaml.j2` declares.
Two tests and the `validate-grafana-dashboards` prek hook enforce them. Read the dashboards page
before debugging a panel.

## The trap: an idle stack is indistinguishable from a broken one

The stack has run 47h with every pod Ready, zero export failures and a green heartbeat while
carrying one metric name, no logs and zero spans — with nothing wrong. **Run `claude -p` on the
quiet host before investigating its collector**, and read the logs before the metrics rather
than diagnosing from counters alone. The four behaviours behind that appearance are measured on
the docs page. `telemetry-health.sh` checks the OTLP door's reachability deliberately, and
monitor-bridge's `with_export_failures` arm the collector's export failures — neither can detect
"nobody is using it".

## Content logging is on

`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`, `OTEL_LOG_TOOL_DETAILS` and
`OTEL_LOG_TOOL_CONTENT` are all `1`, so prompts, responses and tool output land in Loki
**verbatim**. That is why the OTLP port is bound to loopback. Treat Loki's retention window as
holding the same sensitivity as the transcripts.
