# observability — the cluster metrics plane plus the Claude Code telemetry stack (k3s, daniel-box)

The workloads in the `observability` namespace all belong to this role. Claude telemetry
is the collector, Tempo and the Loki here. The cluster's metrics plane is Prometheus — what
monitor-bridge reads through `PROMETHEUS_URL` — plus kube-state-metrics and Grafana. Cluster
LOGS are the exception: `loki-homelab` holds those, separate by decision KL1.

Claude Code runs on the **host**, not in a container, and exports to `127.0.0.1:4317` — a
`hostPort` bound to loopback, so the collector never becomes LAN-reachable. Loki, Prometheus
and Tempo carry the same `hostIP` pin on 3100/9090/3200 for `otelq`, another host process.
Tempo's otlp-grpc 4317 skips the pin: two hostPorts on one number wedge a pod in `Pending`.

The deploy reads the pin back from the live DaemonSet and fails without it. That readback has
a Flux form in `templates/verify-inert/`, which nothing renders yet: the flip PR renames the
directory to `templates/verify/`, the renderer then emits `observability-verify`, and the PR
deletes the two readback tasks. The Job runs `files/observability_verify.py` with `get` on
the `otel-collector` DaemonSet and no other grant.
`ansible/roles/k8s/observability/tests/test_observability_verify.py` renders the renamed copy
through the Job census and fails it on the collector render with its `hostIP` removed.

The working-out lives in `docs/observability-dashboards.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "observability"`
- **Images:** `otel/opentelemetry-collector-contrib` (`observability_k8s_collector_image`),
  `grafana/loki` (`observability_k8s_loki_image`), `prom/prometheus`
  (`observability_k8s_prometheus_image`), `prom/alertmanager`
  (`observability_k8s_alertmanager_image`), `grafana/grafana`
  (`observability_k8s_grafana_image`), `registry.k8s.io/kube-state-metrics/kube-state-metrics`
  (`observability_k8s_kube_state_metrics_image`), `grafana/tempo`
  (`observability_k8s_tempo_image`)
- **Route:** `grafana.<domain>` · `grafana.local.<domain>`, Authelia one_factor
- **Claims:** `alertmanager-data` (no backup (StorageClass longhorn-nobackup)), `grafana-data`
  (no backup (StorageClass longhorn-nobackup)), `loki-data` (no backup (StorageClass
  longhorn-nobackup)), `prometheus-data` (no backup (StorageClass longhorn-nobackup)),
  `tempo-data` (no backup (StorageClass longhorn-nobackup))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — observability — the cluster metrics
  plane and Claude Code's own telemetry stack, one sub-image per workload across several
  Deployments. ALSO each stateful component (loki/grafana/prometheus/tempo/alertmanager) is
  Recreate + its own PVC — compounding, not single-component. COUPLING NOTE for a future
  promotion: UI-edited Grafana dashboards live only in the PVC until an export script
  round-trips them to git; a revert discards unsaved edits
<!-- /generated_from -->

**`probe.py health observability` gates every workload in the role**, none of them named observability.

## Eviction tiers

DECIDED: observability tiers — Prometheus and Alertmanager are `homelab-critical`; the other
pod templates are `homelab-best-effort` (#1851). Prometheus earns tier 1 because tier-1
monitor-bridge reads it every cycle via `PROMETHEUS_URL`. Alertmanager earns it because it is
the paging path, which the operator accepted on 2026-10-12 (#4693, the M1 plan's open question
1). The exporters stay tier 4 — a lost one surfaces loudly as `up == 0`.
`ansible/tests/k8s/test_pod_template_hygiene.py` holds every long-running pod template to one
of the four classes.

## One config change, one restart

This role sets `manifests_rollout: ''`: `k8s/manifests` neither waits nor restarts for it, and
each workload carries its own trigger (#2858). A ConfigMap edit rolls its own pod: the
template hashes the ConfigMap body into that pod template as `checksum/config`
(`ansible/templates/checksum-annotation.yml.j2`). A Secret edit goes through the role's own
restart task instead, since hashing one into an annotation would expose it. Each Secret reader
names the rendered files its pod reads in `restart_secrets`, in
`ansible/roles/k8s/observability/defaults/main.yml:observability_stabilise_workloads`:
`scrape-credentials-secret.yaml` for prometheus, `grafana-secret.yaml` for grafana and
`alertmanager-secret.yaml` for alertmanager. The role pairs each result of the shared
`manifests_secret_render` loop with its file as `observability_changed_secret_files`. One Secret
edit then restarts its readers alone and soaks them alone in the play gate. An Alertmanager
config edit restarts and soaks Alertmanager alone.

**An initContainer writes Grafana's admin password.** `GF_SECURITY_ADMIN_PASSWORD` seeds only a
new database, so the `admin-password` initContainer in `grafana.yaml.j2` runs `grafana cli
admin reset-admin-password` from the `grafana-admin` Secret before every Grafana start. A
rotated `grafana_admin_password` changes `grafana-secret.yaml`, the restart rolls grafana, and
the initContainer writes the new password. grafana is `Recreate`, so no server holds the
SQLite file while the CLI runs. Once the tag is flux-owned, the renderer's restart annotation
rolls grafana instead (D5.1e).

The rule files are the one ConfigMap no annotation hashes. Prometheus mounts `prometheus-rules`
as a directory and runs with `--config.auto-reload`, so a rule edit reloads in place and never
rolls the `Recreate` pod
(`ansible/roles/k8s/observability/templates/prometheus.yaml.j2:DECIDED: rules reload in place`).

Every entry declares `restart_on: []`, so the shared release record stamps `restart: false`
for each workload. After its rollout wait, the role raises that to `restart: true` through
`ansible/roles/k8s/manifests/tasks/rollout_amend.yml` for each workload its restart rolled. A
wrong raise lands as a red deploy, because `probe.py health observability` reads a
`restart: true` with no newer `restartedAt` as NOT ROLLED. The entries carry no
`rolled_by_role`, because the shared role drops a marked entry from the pod-template
fingerprints, and the restart's skip of a workload the apply already rolled reads them (#3115).
`ansible/roles/k8s/observability/defaults/main.yml:DECIDED: no rolled_by_role` has the
ruling.

**Widen the capture when you add a `data:` key.** Every capture but Grafana's holds ONE key's
body, so a second key in `prometheus.yaml.j2` lands outside the hash and the pod never rolls.
`ansible/tests/services/test_observability_config_rolls_one_workload.py` guards the three ways
this goes QUIET.

The snapshot handing these workloads to the play gate fires for a ConfigMap edit only when both
render and apply change, so a comment-only edit buys no soak (#3133).

## Alert plane

Prometheus sends alerts to the role's Alertmanager and scrapes it as job `alertmanager`, which
carries no `origin` label, so Kuma's Cluster Scrape Targets tile pages a failed scrape.
Its config, credentials included, is the `alertmanager-config` Secret, and every Go template
expression in it sits in a Jinja raw block. Each rule group is a plain-YAML file in
`files/rules/` with a promtool test in `tests/rules/`, and ships only once
`observability_rule_groups` names it. The routing tree, the receivers and the rule conventions
are in `docs/target-architecture/monitoring/m1-alertmanager.md`.

**The weekly reboot mutes every paging route, never the Watchdog.** The `weekly-reboot` time
interval derives from the reboot cron's `weekly_reboot_*` values and the mute's lead and
recovery allowance in `group_vars/all.yml`, which Kuma's maintenance window also reads. A
schedule written here as a literal would stop following the reboot.
`ansible/tests/services/test_alertmanager_reboot_mute.py::test_moving_the_reboot_moves_the_mute_with_kumas_window`
holds the two windows equal, and
`ansible/tests/services/test_alertmanager_routing.py::test_every_route_a_paging_alert_reaches_is_muted`
walks the routing tree to prove the alerts reach the mute.

**A host-plane rule names its host as a literal, so a new host is a rule edit.** The
`host-checks` group carries one `HostReconcileFailed` per host in `hosts.ini`, and one
`HostUpdateStale` per entry of `observability_expected_hosts`, each from the shape in
`tests/rules/fixtures/host-update-stale.yaml`, which `host-checks.test.yaml` fires.
`ansible/tests/services/test_host_plane_rule_literals.py` holds both sets to their sources and
prints the rule a new entry needs. `docs/host-packages.md` is the runbook for both pages. The
group also carries one `WiredLinkDown` warning per node whose host_vars set
`k3s_node_static_link`, reading that link's carrier and operstate (#5124). The same test
module holds that set to host_vars, so a node that pins a new link needs a rule edit too.

**The `blackbox-ingress` job probes every ingress tile through blackbox-exporter.** It is
hand-written in `prometheus.yaml.j2` and renders one target per tile the
`kuma_ingress_monitors` filter yields, so Kuma's tiles and the probes read one list. Each
target sets `service`, `module` and `page_tier`, and the `ingress-probes` rules take `service`
from the target rather than from a literal. A new routed entry therefore changes
`prometheus.yml` and rolls Prometheus. `ansible/tests/services/test_blackbox_ingress_targets.py`
pins the targets to the filter's tiles, and
`docs/target-architecture/monitoring/m4-blackbox-ingress.md` has the design.

**A per-workload alert takes its `service` label from a recording rule.**
`ansible/roles/k8s/observability/files/rules/workload-service.yaml` records one series per
Deployment, DaemonSet and pod, each carrying the deploy tag as `service`. kube-state-metrics
exports the `homelab/role` and `kustomize.toolkit.fluxcd.io/name` labels through its
`--metric-labels-allowlist`. It collects ReplicaSets only for `kube_replicaset_owner`, the hop
from a pod to its Deployment, and the `kube-state-metrics` scrape job drops the other
`kube_replicaset_*` families. A workload with no label resolves through a literal override
table in the rule, then to `k3s` in the add-on namespaces, then to `unmapped`.
`ansible/tests/k8s/test_workload_service_overrides.py` derives the table from the render. The
pod mapping leaves out Job-owned and ownerless pods outside the add-on namespaces. That holds
only while every rendered Job runs under `restartPolicy: Never`, which the
`job-restart-policy-is-never` row of `ansible/tests/k8s/test_rendered_properties.py` enforces.

## Flux in the metrics plane

The `flux` scrape job reads the three Flux controllers' `http-prom` port and drops every
`_bucket` series. kube-state-metrics exports `gotk_resource_info` for Kustomizations and
OCIRepositories from the config in
`ansible/roles/k8s/observability/defaults/main.yml:observability_ksm_flux_resource_state`.
That config travels as the `--custom-resource-state-config` argument, not a ConfigMap, so a
change to it rolls kube-state-metrics through the apply and adds no key for the rolls-one-workload
guard to track. A kind added to the config needs `list` and `watch` in the ClusterRole beside it;
`ansible/tests/services/test_ksm_flux_resource_state.py` fails a kind with no grant.
`docs/flux-deploy-plane.md` has the labels, and why `suspended` is absent rather than `"false"`.
A Kustomization's `content_digest` label is the tag's own rendered-content digest. It is what
`probe.py health` matches a flux-owned tag's reference time on, because `revision` moves on
every master commit (#5338).

## Grafana logs in through Authelia (OIDC), and the admin form stays on

Grafana is an OIDC client of the Authelia portal: client `grafana` in
`config-secret.yaml.j2`, `GF_AUTH_GENERIC_OAUTH_*` in `grafana.yaml.j2`. `admins` maps to
Grafana Admin, everyone else to Viewer.

Three rules hold this together: `GF_SERVER_ROOT_URL` decides the one route that completes an
OIDC login, keeping the admin form on as the break-glass path; the Authelia client keeps its
`with_groups` claims policy, or an `admins` user logs in as Viewer behind a green health gate;
and the client secret is one credential in two SOPS keys, rotated together.
`ansible/tests/services/test_grafana_authelia_oidc.py` holds both roles to the three settings
that are written twice. `docs/observability-dashboards.md` has each rule in full.

## Dashboards

`files/dashboards/**/*.json` is the source of truth for every board, in the folders
`observability_dashboard_folders` lists. To change one: edit the JSON, or edit it in the UI and
round-trip with `scripts/grafana/export_grafana_dashboards.py`, then deploy
**observability**. Three editing rules each otherwise leave a green deploy behind a blank board:
a board in an unlisted folder is provisioned nowhere; a hand-edited board must stay in the
writers' form; every datasource ref must resolve to a uid `templates/grafana.yaml.j2` declares.
Two tests and the `validate-grafana-dashboards` prek hook enforce them. Read the dashboards
page first.

## The trap: an idle stack is indistinguishable from a broken one

The stack can run for days fully green — every pod Ready, zero export failures, a green
heartbeat — while carrying no real signal. **Run `claude -p` on the quiet host before
investigating its collector**, and read the logs before the metrics, not from counters alone.
The four behaviours behind it are on the docs page.
`telemetry-health.sh` checks OTLP reachability, and monitor-bridge's `with_export_failures`
arms export failures — neither can detect "nobody is using it". The heartbeat is a daniel-box
cron that `roles/setup/claude_code` installs (H2.7), not this role. `with_tempo_poll_errors` pages
when Tempo's blocklist poll fails: stored traces go unsearchable behind a Ready pod.

## Content logging is on

`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`, `OTEL_LOG_TOOL_DETAILS` and
`OTEL_LOG_TOOL_CONTENT` are all `1`, so prompts, responses and tool output land in Loki
**verbatim**. That is why the OTLP port is bound to loopback. Treat Loki's retention window as
sensitive as the transcripts themselves.
