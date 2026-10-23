# observability — the cluster metrics plane plus the Claude Code telemetry stack (k3s, daniel-box)

The workloads in the `observability` namespace all belong to this role. Claude telemetry
is the collector, Tempo and the Loki here. The cluster's metrics plane is Prometheus — what
monitor-bridge reads through `PROMETHEUS_URL` — plus kube-state-metrics and Grafana. Cluster
LOGS are the exception: `loki-homelab` holds those, separate by decision KL1.

Claude Code runs on the **host**, not in a container, and exports to `127.0.0.1:4317` — a
`hostPort` bound to loopback, so the collector never becomes LAN-reachable. Loki, Prometheus
and Tempo carry the same `hostIP` pin on 3100/9090/3200 for `otelq`, another host process.
Tempo's otlp-grpc 4317 skips the pin: two hostPorts on one number wedge a pod in `Pending`.

Flux applies the role (`owner: flux`), and no Ansible path runs it. `tasks/main.yml` holds the
include of `k8s/manifests` alone, which the renderer reads for the role's vars. To give the role
back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of
the flag leaves two writers on the objects.

The `observability-verify` Job reads the pin back from the live DaemonSet and fails without it.
It lives in `templates/verify/` and runs `files/observability_verify.py` with `get` on the
`otel-collector` DaemonSet and no other grant.
`ansible/roles/k8s/observability/tests/test_observability_verify.py` renders it through the
Job census and fails it on the collector render with its `hostIP` removed.

The working-out lives in `docs/observability-dashboards.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=observability` with exit 5
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

This role sets `manifests_rollout: ''`, and each workload carries its own trigger (#2858). A
ConfigMap edit rolls its own pod: the template hashes the ConfigMap body into that pod template
as `checksum/config` (`ansible/templates/checksum-annotation.yml.j2`). A Secret edit rolls
through the renderer's restart annotation, which hashes the Secrets a pod template
names without putting their bytes in the annotation. The role passes its workloads to the
include as `manifests_self_rollouts`, from
`ansible/roles/k8s/observability/defaults/main.yml:observability_stabilise_workloads`. Each
Secret reader names the rendered files its pod reads in `restart_secrets`:
`scrape-credentials-secret.yaml` for prometheus, `grafana-secret.yaml` for grafana and
`alertmanager-secret.yaml` for alertmanager.
`ansible/tests/services/test_observability_config_rolls_one_workload.py` holds that
declaration equal to the Secrets each pod reads. One Secret edit then rolls its readers alone.
An Alertmanager config edit rolls Alertmanager alone.

**An initContainer writes Grafana's admin password.** `GF_SECURITY_ADMIN_PASSWORD` seeds only a
new database, so the `admin-password` initContainer in `grafana.yaml.j2` runs `grafana cli
admin reset-admin-password` from the `grafana-admin` Secret before every Grafana start. A
rotated `grafana_admin_password` changes `grafana-secret.yaml`, the annotation rolls grafana,
and the initContainer writes the new password. grafana is `Recreate`, so no server holds the
SQLite file while the CLI runs. The renderer's restart annotation rolls grafana when the
Secret changes (D5.1e).

Two kinds of ConfigMap carry no hash. Prometheus mounts `prometheus-rules`
as a directory and runs with `--config.auto-reload`, so a rule edit reloads in place and never
rolls the `Recreate` pod
(`ansible/roles/k8s/observability/templates/prometheus.yaml.j2:DECIDED: rules reload in place`).
Grafana's file provisioner re-reads the dashboard ConfigMaps from their mounts every 30
seconds, so a board edit rolls nothing either. Both hold after the flip too. The renderer's
restart annotation hashes the ConfigMaps a pod mounts, but it skips every name in
`scripts/renderer/renderer_lib/restart.py:RELOADED_IN_PLACE`. Without that list, a board edit
would restart the `Recreate` grafana pod once the dashboards join the bundle, and a rule edit
would restart Prometheus.
`scripts/renderer/tests/test_renderer_self_rollout_restart.py::test_a_board_or_rule_edit_after_the_flip_moves_no_restart_value`
renders the flipped role and holds both values still across such an edit.

Every entry declares `restart_on: []`, which the renderer ignores for a self-rollout entry.
The entries carry no `rolled_by_role`, because the shared role drops a marked entry from the
pod-template fingerprints (#3115). The ruling is
`ansible/roles/k8s/observability/defaults/main.yml:DECIDED: no rolled_by_role`.

**Widen the capture when you add a `data:` key.** Every capture but Grafana's holds ONE key's
body, so a second key in `prometheus.yaml.j2` lands outside the hash and the pod never rolls.
`ansible/tests/services/test_observability_config_rolls_one_workload.py` guards the three ways
this goes QUIET.

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
It also carries `FanoutSlotsIdleWithFreeWork`, in shadow until #5658 is ruled: free issues
for an hour with no fan-out batch live on either node, read from the server lander's
`findings_issues` gauge and the per-batch cgroup series.

**`ScrapeTargetDown` is one rule per owning role, and a new scrape job needs a line in one.**
`files/rules/scrape-targets.yaml` gives each rule a literal `service`, because a static scrape
job has no other source for it. The last rule catches a job that none of the others names, with
`service="unmapped"`. `ansible/tests/services/test_scrape_target_rule_jobs.py` fails until every
rendered job is in exactly one named rule. These rules and `files/rules/log-pipeline.yaml` carry
`shadow` until M6's batch A3 cuts Kuma's scrape and log-shipper tiles over.
`files/rules/kuma-cutover.yaml` is temporary: its `CheckWithoutPager` pages a tile that is gone
while its rule is still shadow, and the last M6 Phase A cutover deletes it.

**`files/rules/hosts.yaml` holds the host rules, one copy per host.** Its disk, memory,
temperature, UPS and Pi pressure rules replace monitor-bridge's `disk`, `memory`, `host_temp`,
`ups` and `pi_pressure` checks and carry `shadow` until M6's batch A1 cuts them over (the Pi's
until B8). A rule's `service` is a literal, so a host gets its own copy, and a fourth host needs
rules added. Every rule keeps `job` and `instance`, because the inhibit that replaces
`EXPORTER_DEPENDENT` matches `ScrapeTargetDown` on them. A missing-data arm starts from
`up == 1`, so it fires only while the scrape succeeds and the series is absent. A dead exporter
pages once, through `ScrapeTargetDown`. The two coverage-floor rules count hosts, so they carry
neither label, and a dead exporter also pages through them.

**The `blackbox-ingress` job probes every ingress tile through blackbox-exporter.** It is
hand-written in `prometheus.yaml.j2` and renders one target per tile the
`kuma_ingress_monitors` filter yields. Kuma's own ingress tiles came from that filter until
M4's PR 4 (#5625) removed them, so these probes are the only ones on the ingress names. Each
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
`kube_replicaset_*` families. It collects Jobs for `homelab:gate_job_duration_seconds`, the
duration of each soak, snapshot and verify Job a landing waits on (#5675), and the scrape job
keeps five `kube_job_*` families of the seventeen. A workload with no label resolves through a literal override
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

`files/dashboards/**/*.json` is the source of truth for every board, in the folders and board
lists `observability_dashboard_folders` holds. To change one: edit the JSON, or edit it in the
UI and round-trip with `scripts/grafana/export_grafana_dashboards.py`, then deploy
**observability**. A new board file also needs its name under its folder in that map. Three
editing rules each otherwise leave a green deploy behind a blank board: a board in an unlisted
folder is provisioned nowhere; a hand-edited board must stay in the writers' form; every
datasource ref must resolve to a uid `templates/grafana.yaml.j2` declares. Three tests and the
`validate-grafana-dashboards` prek hook enforce them. Read the dashboards page first.

**The dashboard ConfigMaps are a render.** `templates/grafana-dashboards.yaml.j2` renders one
per folder and passes each board through the `grafana_deploy_annotated` filter, which adds the
two deploy-annotation queries. The file joins the `observability` bundle, and Flux applies it
server-side, which the 262144-byte annotation cap on a client-side apply of the Infrastructure
folder needs. `ansible/tests/services/test_dashboard_configmaps.py` renders the bundle and holds
every ConfigMap under the 1 MiB object cap.

**Flux applies every key of a dashboard ConfigMap.** The Ansible task that deleted a key no
listed board justified went with `tasks/dashboards.yml`. A board dropped from
`observability_dashboard_folders` leaves the ConfigMap at the next Flux apply, because Flux
applies the whole object.

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
