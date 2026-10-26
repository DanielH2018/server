# M2 — One bridge family to rules

Status: IN PROGRESS

PR 1, the workload-service mapping: [#4777](https://github.com/DanielH2018/server/pull/4777).
PR 2, the k8s-workloads rules in shadow: [#4779](https://github.com/DanielH2018/server/pull/4779). Its soak starts at its deploy, and PR 4a waits 7 days from that time.
PR 3, the weekly-reboot mute: [#4793](https://github.com/DanielH2018/server/pull/4793). PR 4a waits until it records the three Alertmanager series after its deploy.
Soak reads, [#4786](https://github.com/DanielH2018/server/issues/4786): day 3 passed on 2026-10-16 ([the read](https://github.com/DanielH2018/server/issues/4786#issuecomment-6205316129)). Over 2026-10-13T00:15:51Z to 2026-10-16T00:15:51Z, queries 1, 2 and 3 returned no minutes, and the attribution step listed no bridge DOWN episode. Day 7, read on 2026-10-21 over 2026-10-13T00:15:51Z to 2026-10-20T00:15:51Z, returned 58 minutes for query 1 and 55 for query 2 ([the read](https://github.com/DanielH2018/server/issues/4786#issuecomment-6326523507)). Query 3 returned nothing. Attribution ([comment](https://github.com/DanielH2018/server/issues/4786#issuecomment-6326681270)) explains 51 of the 58 minutes under the plan's kinds: the log-pattern arm, and an episode shorter than the rule's `for:`. It leaves 7 minutes (home-assistant, 2026-10-17 06:43 to 06:49), where a 2-minute dip in `kube_deployment_spec_replicas` reset `K8sDeploymentZeroAvailable`'s `for:` while the bridge kept paging. Query 2's 55 minutes fall in two episodes. The janitorr crash loop on 2026-10-18 07:47 to 08:05 fell inside Kuma's weekly-reboot maintenance window. The cluster-wide outage on 2026-10-18 17:44 to 18:19 took Kuma itself down, so the tile stored no sample while the rules and the bridge both fired. The tuning is `keep_firing_for: 5m` on that rule ([#5811](https://github.com/DanielH2018/server/pull/5811), live in the `prometheus-rules` ConfigMap), so no unexplained minute remains and PR 4a is unblocked. That count is a projection: replayed against the attribution's minute-by-minute data, the tuned rule keeps firing through 06:51, and the 8-minute margin covers Kuma DOWN through 06:49. Prometheus does not re-evaluate stored `ALERTS` under a changed rule, so a re-run of query 1 over the same window still returns 58.
Soak note, 2026-10-17: D5's Wave A moved eight roles to Flux inside this soak's window. Six of the takeovers rolled their workload once:
- gpu-exporter, nut-exporter and pihole-exporter from 15:52:58Z (D5.5, #5417);
- littlelink after 17:01Z (D5.6, #5420);
- homepage and node-exporter from 17:47:09Z (D5.7, #5423). node-exporter's pods restarted at 17:47:10Z on daniel-server and 17:47:22Z on daniel-box.

longhorn-ui and deploy-ui did not roll. Under M1's convention 6, a Query-1 episode at those times is explained, not a reset.

Depends on: M1

Unblocks: M6 (with M3, M4 and M5), and D5

This slice moves the Kubernetes arms of monitor-bridge's workload check, with its DaemonSet
floor, out of the bridge and into Prometheus rules. It also builds the two pieces every later
family reuses: the recording rules that give each alert the C7 `service` label, and the
weekly-reboot mute. The check's log-pattern arm reads Loki, not Prometheus, so it stays on the
bridge on a narrowed row. This slice does not port that arm. Open question 6 gates the
narrowing on another slice's text adopting the port to the loki-homelab ruler. M6 adopts it
as its batch A7. The slice follows the conventions in
[M1](m1-alertmanager.md#conventions-every-monitoring-slice-follows), which this page calls
"M1 convention N".

## Goal and exit criterion

**Goal.** The `k8s_workloads` row of monitor-bridge is gone. Its Kubernetes arms are `for:`
rules that page through Alertmanager. Each alert is labelled with the deploy tag of the
workload it names, and each input family has a zero-series test. The row's log-pattern arm
runs alone on a `workload_log_errors` row, which keeps the old row's push token and Kuma id. D5
can join its own soak rule to the same workload-service recording rules.

**Exit criterion.** All four hold:

1. `CHECKS` in `ansible/roles/k8s/monitor-bridge/files/check_table.py` has no `k8s_workloads`
   row. The `workload_log_errors` row that replaces it calls only
   `checks/logs.py:with_log_errors`.
   `probe.py metric 'monitor_status{monitor_name="k3s Workload Health"}'` returns nothing, and
   the same query for `Workload Log Errors` returns one series.
2. `probe.py metric 'ALERTS{alertname=~"K8s.*|WorkloadService.*"}'` returns only series that
   carry `service` and `severity` and no `shadow` label. `promtool test rules` passes on
   `workload-service.test.yaml` and `k8s-workloads.test.yaml`. The second file holds one
   zero-series case per input family named under *The rules*.
3. The 7-day shadow soak recorded 0 unexplained minutes in soak query 1 (M1 convention 6), and
   soak query 3 returned nothing. *Verification* defines both queries and which minutes count
   as explained.
4. The three coverage queries in *Verification* return nothing. Each one lists the workloads
   of one kind that have no mapping series, or whose only mapping series is
   `service="unmapped"`.

## Baseline at `70591c307`

- **The row.** `PushCheck(name="k8s_workloads", ...)` in `check_table.py` has
  `fn=cluster.check_k8s_workloads`, `token="monitor_bridge_k8s_workloads_push_token"`,
  `kuma_id="monitor-bridge-k8s-workloads"`, `display="k3s Workload Health"` and
  `gate="prometheus"`. It is not `critical`, so Kuma pages Discord only.
- **The Kubernetes arms**, from
  `ansible/roles/k8s/monitor-bridge/files/checks/cluster.py:check_k8s_workloads` and its
  helpers:
    - Unavailable replicas: `kube_deployment_status_replicas_unavailable > 0`, held by
      `K8S_WORKLOADS_CONSECUTIVE` (3).
    - Zero available: `kube_deployment_status_replicas_available == 0 and on(namespace,
      deployment) kube_deployment_spec_replicas > 0`
      (`checks/cluster_zero.py:zero_available_offenders`), held by
      `K8S_ZERO_AVAILABLE_CONSECUTIVE` (2).
    - Stalled rollout: `kube_deployment_status_replicas_updated < on(namespace, deployment)
      kube_deployment_spec_replicas` (`checks/cluster_rollout.py:stalled_rollout_offenders`),
      held by `K8S_ROLLOUT_STALL_CONSECUTIVE` (3, the code default in
      `bridge/config_cluster.py`).
    - Crash loop: `increase(kube_pod_container_status_restarts_total[1h]) > 3 and
      increase(...[30m]) > 0` (`K8S_RESTART_WINDOW`, `K8S_RESTART_MAX`,
      `K8S_RESTART_RECENT_WINDOW`), with no hold. The `DECIDED 30m` marker in
      `bridge/config_cluster.py` records why the recency window is 30m.
    - DaemonSet unavailable: `kube_daemonset_status_number_unavailable > 0`, with no hold.
    - Census floors: `count(kube_deployment_status_replicas_unavailable)` against
      `K8S_MIN_WORKLOADS` (5) and `count(kube_daemonset_status_number_unavailable)` against
      `K8S_MIN_DAEMONSETS` (11). This is the "DaemonSet floor" that keeps the arm from reading
      an empty vector as healthy (`verdicts/cluster.py:k8s_workloads_verdict`).
    - Extended resource: `devic.es/dri` advertised by some node, read as
      `kube_node_status_allocatable{resource="devic_es_dri"} > 0`
      (`verdicts/cluster.py:extended_resource_verdict`, with the name sanitised by
      `verdicts/cluster.py:ksm_resource_label`). It reports INERT, not DOWN, when no
      `kube_node_status_allocatable` series exist at all.
- **The log-pattern arm can turn the tile DOWN.** `check_k8s_workloads` ends by calling
  `checks/logs.py:with_log_errors`, whose docstring reads "Fold the log-pattern arm into the
  workload verdict". When `verdicts/cluster.py:log_error_verdict` fails, it returns
  `False, "%s | %s" % (log_msg, msg)`, so the tile goes DOWN with the log message first. The
  inputs are in `templates/env-secret.yaml.j2`:
    - `LOG_ERROR_SELECTOR` covers the cluster pod streams and daniel-pi's container streams.
    - `LOG_ERROR_PATTERN` is `(?i)(panic:|fatal|traceback|out of memory)`.
    - `LOG_ERROR_WINDOW` is `1h`, and `LOG_ERROR_MAX` is 20.
    - The `DECIDED stays at 20` marker at `LOG_ERROR_MAX` records two home-assistant bursts
      that "held k3s Workload Health red for 46 and 43 minutes".

  The arm reads `LOKI_URL`, which is loki-homelab. It fails open on a Loki error, and its
  docstring keeps it out of `LOKI_DEPENDENT` only because that gate would also blind the
  Kubernetes arms. The `checks/logs.py` module docstring names `check_k8s_workloads` as its
  only caller, and `git grep with_log_errors` finds no other outside the tests.
- **The gate sets derive from the `gate` column.** `gates.py:_members` returns the rows whose
  `gate` equals its argument, and `LOKI_DEPENDENT` and `STARTUP_GRACE` are two of its calls.
  Three rows name `loki_reachable`: `loki_ingestion`, `swallowed_verdicts` and
  `kuma_notify_failures`, the rows M5 deletes. `STARTUP_GRACE` holds a check `up` through
  its first `GRACE_CYCLES` − 1 consecutive down cycles (`bridge/streaks.py:apply_startup_grace`),
  and `templates/env-secret.yaml.j2` sets `GRACE_CYCLES` to 2. `test_check_table.py`'s
  `_GATE_VALUES` accepts `startup_grace`.
  `test_check_streaks.py:test_startup_grace_covers_every_ungated_reach_out_check` reads each
  check body's own source for `_get_json(` or `_post_json(`.
- **The bridge cycles every 300s** (`INTERVAL`). It pages on the Nth consecutive bad sample,
  (N − 1) × 300s after the first, and `bridge/config_cluster.py` reads each hold as N × 300s
  ("3 cycles = 15 min at INTERVAL=300"). Its stall evidence reads "`updated < spec` occurred
  for 1-2 five-minute samples at a time (ordinary rollouts) and NEVER for three consecutive
  ones". Its zero-available evidence reads "At 1 the arm pages every Recreate swap of a
  slow-starting workload".
- **The unavailable-replica streak is per arm, not per workload.**
  `checks/cluster_rollout.py:held_replica_offenders` keeps one counter,
  `k8s_workload_replicas`, for any non-empty offender set. Different workloads in successive
  cycles therefore add up to one page.
- **The code this family alone uses.** `checks/cluster_rollout.py` (100 lines) and
  `checks/cluster_zero.py` (72 lines). No other check imports either. Four test files total
  586 lines: `test_check_k8s_workload_gates.py`, `test_check_k8s_workload_replicas.py`,
  `test_check_k8s_rollout_stall.py` and `test_check_k8s_zero_available.py`, under
  `ansible/roles/k8s/monitor-bridge/tests/`. `verdicts/cluster.py:ksm_resource_label` has no
  production caller outside `check_k8s_workloads`.
- **Other tests read the family.**
    - `ansible/tests/services/test_dri_resource_name_agrees.py` reads the
      `K8S_EXTENDED_RESOURCES` default out of `bridge/config.py` by regex.
    - `test_config_reads_the_rendered_env.py:test_no_rendered_key_carries_a_second_default_in_python`
      names `("config.py", "K8S_EXTENDED_RESOURCES", True)` as a member it must find.
    - `ansible/tests/services/test_kuma_push_resend_beats.py:test_no_push_monitor_can_resend_faster_than_four_hours`
      names `k3s Workload Health` as a member it must find.
    - `test_check_host.py:test_crash_loop_arm_gates_on_a_recent_restart_not_just_the_hour_window`
      calls `check_k8s_workloads`, and
      `test_check_host.py:test_the_recency_window_is_wider_than_the_worst_observed_restart_spacing`
      pins the 30m floor.
    - `test_check_longhorn.py` tests `ksm_resource_label` and `extended_resource_verdict`, and
      `test_check_loki.py` tests `with_log_errors`.
- **kube-state-metrics** (`ansible/roles/k8s/observability/templates/kube-state-metrics.yaml.j2`)
  runs with `--resources=pods,deployments,statefulsets,daemonsets,nodes` and
  `--metric-labels-allowlist=pods=[app],deployments=[app]`. The comment above its ClusterRole
  keeps the grants and the flag in step. Prometheus scrapes it every 1m.
- **The deploy tag reaches the object, not the pod.** `ansible/filter_plugins/role_label.py`
  stamps `homelab/role: <service>` on `metadata.labels` of every document an armed role
  renders. It never touches a pod template, so pods carry no tag. `_UNARMED_ROLES` in
  `ansible/tests/deploy/test_manifests_prune.py` names the two roles that skip it, `authelia`
  and `nut`.
- **Live label census** (read-only `kubectl get deploy,ds -A`, 2026-10-11): 71 Deployments, 11
  DaemonSets and no StatefulSets. 13 Deployments and 4 DaemonSets carry no `homelab/role`:
    - In `homelab`: `authelia`, `authelia-redis`, `nut` and `pihole-2`, all with no labels at
      all. `pihole-2` is pihole's deferred file (`manifests_deferred_files` in
      `ansible/roles/k8s/pihole/tasks/main.yml`).
    - In `kube-system`, `longhorn-system` and `metallb-system`: the k3s, Longhorn and MetalLB
      add-ons, including the `longhorn-manager`, `longhorn-csi-plugin`, engine-image and
      `speaker` DaemonSets. The `dri-device-plugin` DaemonSet in `kube-system` carries its
      own tag.
- **Pod owners** (`probe.py metric 'count by (namespace, owner_kind) (kube_pod_owner)'`,
  2026-10-11):
    - `homelab` has 54 ReplicaSet-owned, 10 DaemonSet-owned and 20 Job-owned pods, plus one
      pod with no owner, `ctx-probe`, created 2026-08-14 with no labels.
    - The add-on namespaces add Job-owned pods (Longhorn's backup Jobs and k3s's
      `helm-install-gateway-api`) and two `InstanceManager`-owned Longhorn pods.
    - Every `homelab` Job and CronJob runs with `restartPolicy: Never` (read-only
      `kubectl get cronjobs,jobs -n homelab`). No file under `ansible/roles` sets
      `restartPolicy: OnFailure`.
    - `max by (namespace) (max_over_time(kube_pod_container_status_restarts_total[7d]) * on(namespace, pod) group_left() max by (namespace, pod) (kube_pod_owner{owner_kind=~"Job|"}))`
      returned 0 for `homelab`, `kube-system` and `longhorn-system`.
- **The Kuma tile is live.** `probe.py metric 'monitor_status{monitor_name="k3s Workload
  Health"}'` returns one series (`monitor_type="push"`). A bridge tile's deadline is
  `uptime_kuma_k8s_bridge_push_interval`, 1200s, in the uptime-kuma role defaults. The comment
  above the bridge loop in `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2`
  says a row's `kuma_id` is the AutoKuma id. A row that keeps its `kuma_id` therefore keeps its
  monitor and its history. The bridge reads each row's token from `KUMA_PUSH_<NAME>`
  (`bridge/types.py:push_env`), which the env-secret derives from the row name.
- **Deploy order.** `ansible/deploy.yml` deploys the k8s play in the order
  `ansible/filter_plugins/toposort.py:toposort_containers` gives, which is stable on
  `containers_list` order. In `ansible/inventory/host_vars/daniel-box.yml`, `observability`
  comes before `uptime-kuma`, which comes before `monitor-bridge`.
  `scripts/deploy_tools/land_lib/deploy.py:deploy_by_host` runs one `deploy.sh` per host with
  every tag, and `scripts/deploy_tools/land_lib/options.py` gives `land.sh` a `--tags` option
  that skips the tag derivation. All three roles are `k8s_autodeploy: false`, so no GitOps tick
  deploys them.
- **What restarts Prometheus.** The `kube-state-metrics` scrape job sits inside the
  `observability_prometheus_config` block of `prometheus.yaml.j2`, which the Prometheus
  Deployment hashes into `checksum/config`, so any edit there rolls the `Recreate` Prometheus.
  `ansible/roles/k8s/observability/defaults/main.yml:observability_stabilise_workloads` gives
  `prometheus` and `grafana` `restart_on: [secret]`, and M1 adds `alertmanager`. The role's
  `CLAUDE.md` says `manifests_secret_render` is one register over all of the role's Secrets.
  M1's PR 5 narrows that restart to the Secret files each workload reads
  (`restart_secrets`), so an edit to `alertmanager-secret.yaml` restarts Alertmanager alone.
  M1 lands and deploys that PR before M2's first edit to `alertmanager.yml`, and M2 depends on
  all of M1. Alertmanager loads its config only at start, on SIGHUP or on `/-/reload` (M1,
  *Restart, PR 2*). The comment above the `kube-state-metrics` job justifies its 1m scrape
  partly by what `check_k8s_workloads` reads.
- **The weekly reboot window lives in the uptime-kuma role.** Kuma's maintenance window opens
  `uptime_kuma_k8s_maintenance_window_lead_minutes` (5) before the 07:30 UTC Sunday cron. It
  lasts lead + `weekly_reboot_shutdown_delay_minutes` (5) +
  `uptime_kuma_k8s_maintenance_recovery_allowance_minutes` (40) = 50 minutes. Kuma applies it
  to every monitor, the k8s workloads tile included.
  `ansible/roles/k8s/uptime-kuma/tests/test_maintenance_window.py` holds the derivation.

## Design

### Which label is the deploy tag

kube-state-metrics gains the tag labels in its allowlist, and the `replicasets` resource for
the pod-to-Deployment hop:

```text
--resources=pods,deployments,replicasets,statefulsets,daemonsets,nodes
--metric-labels-allowlist=pods=[app],deployments=[app,homelab/role,kustomize.toolkit.fluxcd.io/name],daemonsets=[homelab/role,kustomize.toolkit.fluxcd.io/name]
```

Prometheus exports the two labels as `label_homelab_role` and
`label_kustomize_toolkit_fluxcd_io_name`. Under Flux, C3 names each Kustomization after its
tag, and kustomize-controller labels every object it applies with
`kustomize.toolkit.fluxcd.io/name`. *Open questions* raises the choice between the two labels
as a C1 and C3 gap.

The kube-state-metrics ClusterRole gains `list` and `watch` on `replicasets`, in step with the
flag. The Prometheus `kube-state-metrics` job gains a `metric_relabel_configs` drop that keeps
`kube_replicaset_owner` and drops the other `kube_replicaset_*` families. The drop names each
family explicitly, because RE2 has no lookaround, as the comment in `prometheus.yaml.j2` warns.
The PR records `count({__name__=~"kube_replicaset_.*"})` after its deploy, which should equal
`count(kube_replicaset_owner)`, and states it against M1's series budget (C7). The head-series
total cannot resolve a delta this size, because it swings by about 16,500 a day.

### The workload-service mapping

A group `workload-service`, in `files/rules/workload-service.yaml`, holds three recording
rules. Each emits exactly one series per workload, with value 1:

- `homelab:deployment_service:info{namespace, deployment, service}`, over every series of
  `kube_deployment_status_replicas_unavailable`.
- `homelab:daemonset_service:info{namespace, daemonset, service}`, over every series of
  `kube_daemonset_status_number_unavailable`.
- `homelab:pod_service:info{namespace, pod, service}`, over the pod domain defined below.

Each rule takes its base from the family the census alerts count. A workload the alerts can see
is therefore never missing from the mapping. A missing label family or join leaves the workload
in the mapping with `service="unmapped"`, rather than dropping it.

For a Deployment or a DaemonSet, `service` is resolved in this order. Each step joins to the
earlier ones with `or on(namespace, <kind>)`, so a step fills only the workloads the earlier
steps left, and no workload gets two values:

1. `label_homelab_role`, when present.
2. `label_kustomize_toolkit_fluxcd_io_name`, with a trailing `-deferred` removed by
   `label_replace` and the RE2 regex `(.+?)(?:-deferred)?`. C3 names a deferred apply's
   Kustomization `<tag>-deferred`, and `pihole-2` is a deferred file, so under Flux it carries
   `pihole-deferred`. The strip maps it to `pihole`.
3. A literal override table for the rendered workloads that carry no label: `authelia` and
   `authelia-redis` map to `authelia`, `nut` to `nut`, and `pihole-2` to `pihole`.
4. `k3s` for anything else in `kube-system`, `longhorn-system` or `metallb-system`. The
   `setup/k3s` role applies those add-ons, and `k3s` is its tag (`k3s-bringup.yml --tags k3s`).
5. `unmapped` for anything left.

The pod rule comes after the Deployment and DaemonSet rules in the group. Prometheus evaluates
the rules of one group in order, so the pod rule reads this cycle's outputs. For a pod, the same
`or on(namespace, pod)` chain runs:

1. A ReplicaSet owner joins through `kube_replicaset_owner{owner_kind="Deployment"}` to
   `homelab:deployment_service:info`.
2. A DaemonSet owner joins to `homelab:daemonset_service:info`.
3. Any other pod in the three add-on namespaces maps to `k3s`. That covers Longhorn's
   `InstanceManager` and backup-Job pods and k3s's helm-install Job pods.
4. `unmapped` for anything left in the domain.

**The pod domain** is every pod in the three add-on namespaces, plus every pod elsewhere whose
owner is a ReplicaSet, a DaemonSet or a StatefulSet. A Job-owned or ownerless pod outside the
add-on namespaces is outside the domain. At 2026-10-11 that is 20 Job pods and `ctx-probe`,
all in `homelab`. The narrowing loses no crash-loop coverage today:

- The crash-loop arm reads `kube_pod_container_status_restarts_total`, and the kubelet never
  restarts a container in place under `restartPolicy: Never`.
- Every `homelab` Job and CronJob runs under `Never`. For the `homelab` roles this is
  structural: their templates set it, and no role sets `OnFailure`. For the add-on Jobs it is
  only measured, which is why step 3 keeps them in the domain.
- Over 7 days, every Job-owned and ownerless pod in every namespace held a restart count of 0.

Two things hold the narrowing in place. First, a pytest renders every `templates/*.j2` of every
k8s role through `ansible/tests/_k8s_render.py:rendered_docs`, which globs the `*-job.yaml`
templates too. It asserts that every rendered Job and CronJob sets `restartPolicy: Never`, and
it names `pi-peer-backup` as a member it must find. Second, the crash-loop alert falls back to
`service="unmapped"` for a pod outside the mapping, as described under *The rules*, so a
restarting Job pod still pages.

No StatefulSet exists (`count(kube_statefulset_labels)` returns nothing). A StatefulSet pod is
in the domain but has no step, so it lands in `unmapped`. The first StatefulSet therefore
pages `WorkloadServiceUnmapped` until the mapping gains that hop.

The override table is a literal (M1 convention 1). A pytest in `ansible/tests/k8s/` derives the
set from the rendered manifests. It renders every k8s role and applies the labelling rule the
manifests role applies: armed roles' non-deferred files get the label. It asserts that the table
maps exactly the remaining Deployments and DaemonSets to their own role. It names `authelia` and
`nut` as members it must find.

### The rules

`files/rules/k8s-workloads.yaml`, group `k8s-workloads`. Every alert is `severity: warning`,
matching the row's non-critical tile. Every alert carries `shadow: "true"` until PR 4b.

**The join shape.** Every per-workload alert joins its expression to the mapping as below,
written here for a Deployment:

```promql
(EXPR * on(namespace, deployment) group_left(service) homelab:deployment_service:info)
or
label_replace(EXPR unless on(namespace, deployment) homelab:deployment_service:info,
  "service", "unmapped", "", "")
```

A missing or failed mapping changes an alert's `service` to `unmapped`. It never removes the
alert. The replacement is the literal `unmapped`, with no `$1`, so M1 convention 7 does not
apply to it. *Open questions* records `service="unmapped"` as an exception to C7.

**`for:` is N × 300s**, the reading `bridge/config_cluster.py` gives each streak. An arm with
no streak has N = 1, so its rule holds 5m. The bridge pages (N − 1) × 300s after its first bad
sample. A condition the bridge saw on only N − 1 samples can still last up to N × 300s, and the
bridge's measured noise is made of exactly such episodes. A `for:` of (N − 1) × 300s would page
on them, and N × 300s does not. The cost is an episode lasting between (N − 1) × 300s and
N × 300s, which the bridge paged when its samples fell at both ends and the rule does not.
Soak query 1 shows any such episode, and the soak counts it as explained once its measured
duration is under the rule's `for:` (*Which soak minutes count as explained*). The crash-loop
arm is the exception: its 1h and 30m windows already hold it, so it has no `for:`.

| Arm | Alert | Expression, in words | `for` | `service` |
|---|---|---|---|---|
| Unavailable replicas | `K8sDeploymentReplicasUnavailable` | unavailable > 0 | 15m (N=3) | joined |
| Zero available | `K8sDeploymentZeroAvailable` | available == 0 and desired > 0 | 10m (N=2), held 5m past a recovery by `keep_firing_for` | joined |
| Stalled rollout | `K8sDeploymentRolloutStalled` | updated < desired | 15m (N=3) | joined |
| Crash loop | `K8sContainerCrashLooping` | the bridge's two-window restart test | — | joined through `homelab:pod_service:info` |
| DaemonSet unavailable | `K8sDaemonSetUnavailable` | number_unavailable > 0 | 5m (N=1) | joined |
| Deployment census | `K8sDeploymentSeriesMissing` | `absent(kube_deployment_status_replicas_unavailable)` or a count under 5 | 5m (N=1) | `observability` |
| DaemonSet floor | `K8sDaemonSetSeriesMissing` | `absent(kube_daemonset_status_number_unavailable)` or a count under 11 | 5m (N=1) | `observability` |
| Extended resource | `K8sExtendedResourceMissing` | no node advertises `devic_es_dri` above 0, while `kube_node_status_allocatable` exists | 5m (N=1) | `dri-device-plugin` |
| Node series | `K8sNodeAllocatableMissing` | `absent(kube_node_status_allocatable)` | 5m | `observability` |
| Other inputs | `K8sStateSeriesMissing` | `absent()` of each other input family, one series per family | 5m | `observability` |
| Mapping | `WorkloadServiceUnmapped` | a mapping series with `service="unmapped"`, or a workload in a base family with no mapping series | 30m | `observability` |

- **`K8sStateSeriesMissing`** covers the input families no census alert counts:
  `kube_deployment_spec_replicas`, `kube_deployment_status_replicas_available`,
  `kube_deployment_status_replicas_updated`, `kube_pod_container_status_restarts_total`,
  `kube_pod_owner`, `kube_replicaset_owner`, `kube_deployment_labels` and
  `kube_daemonset_labels`. Each `absent()` is tagged with a literal `family` label through
  `label_replace`, so the `or` keeps every one. `kube_statefulset_labels` is left out, because
  no StatefulSet exists and its `absent()` would fire forever.
- **`WorkloadServiceUnmapped`** has two clauses. The first matches any series of the three
  mapping rules with `service="unmapped"`. The second matches a workload in a base family with
  no mapping series at all, which happens when a mapping rule fails to evaluate. For pods, the
  second clause reads `max by (namespace, pod) (kube_pod_container_status_restarts_total)`,
  less the pods outside the domain. Its `labels:` set `service: observability`, which replaces
  the expression's `unmapped`. The fix lives in the mapping table, an observability file. The
  alert keeps `namespace` and the workload label, so its message names the workload.
- **The census and input alerts take `service: observability`**, the role that runs
  kube-state-metrics and these rules. `K8sExtendedResourceMissing` takes `dri-device-plugin`,
  the role whose DaemonSet advertises `devic.es/dri`.

`files/rules/alerting-meta.yaml` (M1) gains `PrometheusRuleEvaluationFailures`:
`increase(prometheus_rule_evaluation_failures_total[10m]) > 0` by `rule_group`, with
`service: observability` and `severity: warning`. A many-to-many match makes a join rule fail
every evaluation, and its output then goes stale. M1's meta rules watch only reloads. This alert
has no Kuma counterpart, so it pages from PR 1 without a shadow label. *Open questions* asks
whether M1 owns it instead.

Four choices differ from the bridge, and each is stated in the rule's `description`:

- **The arms with no bridge hold hold for 5m.** That covers the DaemonSet arm, the two census
  floors and the extended-resource arm. The bridge pages them on one bad sample, but it
  observes only every 300s, so one bad sample spans up to 5 minutes. A 1-minute evaluation
  without a hold would page every DaemonSet rollout and every kube-state-metrics roll, whose
  failed scrape marks its series stale at once. The soak measures whether 5m drops a real
  episode.
- **`K8sNodeAllocatableMissing` pages.** The bridge reports INERT and stays green. Paging is the
  fail-closed reading of defect class D4 in [design.md](../design.md).
- **A `for:` is per series.** The bridge's unavailable-replica streak counts any non-empty
  offender set, so two workloads in successive cycles add up to a page. A rule pages only on a
  workload that stayed unavailable for the whole hold. Soak query 1 attributes any miss of this
  kind through the bridge's per-cycle offender messages.
- **The census floors 5 and 11 are literals.** The DaemonSet floor mirrors the estate's
  DaemonSet count, so a pytest pins it to the tree, as *Tests and docs* describes. The
  Deployment floor of 5 is a sanity floor that mirrors no count. After PR 4a the rule is its
  own source, and its reasoning moves into the rule's comment.

A dead kube-state-metrics empties every input at once. The per-workload rules then go quiet.
`K8sDeploymentSeriesMissing`, `K8sDaemonSetSeriesMissing`, `K8sNodeAllocatableMissing` and
`K8sStateSeriesMissing` fire, all with `service="observability"`. Alertmanager groups by
`alertname` and `service`, so that is four notifications. That replaces the bridge's
`prometheus` gate for this family without an `inhibit_rules` entry.

### The log-pattern arm stays on the bridge

The log-pattern arm reads loki-homelab, and C7 puts log rules on the loki-homelab ruler, which
M5 builds. M2 therefore does not port it. PR 4a narrows the row to that arm:

- The row is renamed `workload_log_errors`, with `display="Workload Log Errors"` and a
  description that names the pattern, the window and `LOG_ERROR_MAX`.
- It keeps `token="monitor_bridge_k8s_workloads_push_token"` and
  `kuma_id="monitor-bridge-k8s-workloads"`. AutoKuma updates the tile in place, so its history
  survives and no secret changes.
- Its body is a function of about 10 lines, `check_workload_log_errors` in `checks/logs.py`,
  that calls `with_log_errors(cfg, src, True, ...)`.
- Its gate becomes `startup_grace`. The row no longer reads Prometheus, so the `prometheus`
  gate no longer applies. The arm fails open on a Loki error by itself, so it needs no
  reachability gate. Not naming `loki_reachable` also keeps the row independent of M5's PR 4a,
  which deletes that gate. With this row off it, `LOKI_DEPENDENT` holds only the three rows
  M5 takes off that gate, so it empties at M5's PR 4a, as M5 says.
- The cost of `startup_grace` is one held cycle: the row pages on its second consecutive bad
  cycle, 300s later than the arm pages today. A burst stays inside the 1h window far longer
  than that. The two bursts the `DECIDED stays at 20` marker records held the tile red for
  46 and 43 minutes, so the hold would have dropped neither.
- The `LOG_ERROR_*` keys and their `DECIDED stays at 20` marker stay as they are.

**Who ports the arm.** M6 ports it, as its batch A7. M6's A6-7 deletes the push path and
every bridge tile, so a narrowed row with no port would lose its only pager there. Open
question 6 is therefore a gate on PR 4a: before PR 4a merges, another slice's text adopts the
port, the deletion of the `workload_log_errors` row and the retirement of its token. M5
declined the port (M5 *Open questions* 6), and M6 adopted all three as A7 (M6 *Open
questions* 10). A7 runs in four steps:

- Its shadow PR adds a `WorkloadLogErrors` LogQL rule to loki-homelab, in shadow. The PR
  waits for M5's ruler. Its soak starts only after PR 4a, because the soak compares the
  rule with the "Workload Log Errors" tile, and PR 4a creates that tile.
- Its Cutover-a deletes the `workload_log_errors` row while the rule stays shadow. The row
  exists only from PR 4a, so Cutover-a follows PR 4a.
- Its Cutover-b drops `shadow`. The rule pages only once the row is gone, so the arm never
  pages twice.
- Its token PR retires `monitor_bridge_k8s_workloads_push_token` a week after Cutover-b.

The slice index already orders A7 after PR 4a, because M6 depends on all of M2. No step of
M5 waits on PR 4a.

### The weekly-reboot mute

Alertmanager gains a `weekly-reboot` time interval: Sunday, from `07:25` to `08:15`, with
`location: UTC`. Its three inputs follow:

- The reboot cron's `weekly_reboot_minute`, `weekly_reboot_hour`, `weekly_reboot_weekday` and
  `weekly_reboot_shutdown_delay_minutes`.
- The lead.
- The recovery allowance.

The lead and the recovery allowance move from the uptime-kuma role to `group_vars/all.yml`, as
`weekly_reboot_mute_lead_minutes` and `weekly_reboot_mute_recovery_minutes`. The uptime-kuma
templates read the new names in the same commit, because a role's defaults are invisible to
another role. The `discord`, `email` and catch-all routes gain
`mute_time_intervals: [weekly-reboot]`. The Watchdog route never does (M1, *The routing tree*).

A test in `ansible/tests/services/` derives the Alertmanager window from the same variables
`test_maintenance_window.py` reads. It asserts that the two windows are equal while Kuma lives,
and that moving the reboot moves the window.

That test proves the interval, not that this family's alerts reach it. A routing pytest in the
same directory proves the second part. It renders the observability role's Alertmanager Secret
through `ansible/tests/_k8s_render.py:render_role_template` and walks the routing tree of
`alertmanager.yml` the way Alertmanager does: children in order, stopping at the first match
unless `continue: true`. It walks two label sets that stand for this family once PR 4b drops
`shadow`:

- `alertname="K8sDeploymentReplicasUnavailable"`, `severity="warning"`, `service="valheim"`;
- the same set with `severity="critical"`, the value Open question 4 may give
  `K8sDeploymentZeroAvailable`.

For each set it asserts that every route reached carries `weekly-reboot` in
`mute_time_intervals`. Its red half walks the `Watchdog` label set on the same rendered tree.
That set reaches the Watchdog route, which never takes the mute, and the test must flag it.
The PR body records `amtool config routes test` on the rendered file for each label set, so
the walk and Alertmanager are shown to pick the same receivers. M4 adds its four alerts' label
sets to this walk rather than writing a second one.

A real Sunday is not the proof. Prometheus is down for most of the weekly reboot (M4
*Baseline*), so a Sunday on which nothing fired says nothing about the mute. The mute lands
before M2's cutover because the reboot takes workloads down, and Kuma's maintenance window
mutes this family today.

### pint, offline in CI and live by hand

- pint joins the provisioner (`scripts/validate/alert_tools.sh`) with its own version pin and a
  Renovate regex manager for `cloudflare/pint`. Its version follows no image.
- `scripts/validate/pint/offline.hcl` requires a `severity` label with the allowed values. It
  runs `pint --offline lint` from `alert_rules.py`.
- `alert_rules.py --prove-red` (M1) gains a pint fixture in M1's
  `scripts/validate/tests/fixtures/alert_rules/`: an alert with no `severity` label, which
  `pint --offline lint` must reject.
- `scripts/validate/pint/live.hcl` adds a `prometheus` block pointed at Prometheus's pinned
  ClusterIP, `observability_prometheus_cluster_ip`, the address `otelq` falls back to. The
  loopback hostPort answers only on the node the pod runs on, and Prometheus ran on
  daniel-server when PR 2 was written. It runs `promql/series` by hand in each rule PR's
  verification, because CI cannot reach the cluster. The block sets `required = true`. pint's
  configuration reference says that with the default, `false`, a server it cannot reach is
  reported at `warning` level, which `pint lint` does not fail on. With `true` it is a `bug`.
  pint reports a metric that was never present in Prometheus as a `promql/series` bug.

### The tree this adds and removes

```diff
 ansible/roles/k8s/observability/
+  files/rules/workload-service.yaml
+  files/rules/k8s-workloads.yaml
+  tests/rules/workload-service.test.yaml
+  tests/rules/k8s-workloads.test.yaml
~  files/rules/alerting-meta.yaml             PrometheusRuleEvaluationFailures
~  tests/rules/alerting-meta.test.yaml        its firing case
~  templates/kube-state-metrics.yaml.j2       allowlist, replicasets, ClusterRole
~  templates/prometheus.yaml.j2               kube_replicaset_* drop, and the job comment
~  templates/alertmanager-secret.yaml.j2      time_intervals, mute_time_intervals
~  templates/telemetry-health.sh.j2           its comment names the rules
 ansible/roles/k8s/monitor-bridge/files/
-  checks/cluster_rollout.py
-  checks/cluster_zero.py
~  checks/cluster.py                          check_k8s_workloads removed
~  checks/logs.py                             check_workload_log_errors added
~  verdicts/cluster.py                        k8s_workloads_verdict, extended_resource_verdict,
                                              ksm_resource_label removed
~  check_table.py                             k8s_workloads replaced by workload_log_errors
~  bridge/config_cluster.py, bridge/config.py the K8S_* fields removed
 ansible/roles/k8s/monitor-bridge/
~  templates/env-secret.yaml.j2               the K8S_* keys removed; LOG_ERROR_* stay
~  defaults/main.yml                          monitor_bridge_modules loses two paths
-  tests/test_check_k8s_workload_gates.py
-  tests/test_check_k8s_workload_replicas.py
-  tests/test_check_k8s_rollout_stall.py
-  tests/test_check_k8s_zero_available.py
 scripts/validate/
+  pint/offline.hcl
+  pint/live.hcl
```

## Steps, as PRs

### PR 1 — The workload-service mapping (about 220 lines)

- kube-state-metrics: allowlist, `replicasets`, ClusterRole and the relabel drop. Add
  `("kube-state-metrics", "kube_replicaset_created")` to `MUST_DROP` and
  `("kube-state-metrics", "kube_replicaset_owner", ...)` to `MUST_KEEP` in
  `ansible/tests/services/test_metric_drop_regexes.py`.
- Rewrite the comment above the `kube-state-metrics` job so that it names no reader PR 4a
  removes: rule groups evaluate every minute, monitor-bridge every 300s, and nothing reads
  these families at sub-minute resolution. The edit lands here because the drop already rolls
  Prometheus.
- Add `workload-service.yaml` and its tests. They are `promql_expr_test` cases, one per
  resolution step of each rule, plus these:
    - a Deployment with no label outside the table, which yields
      `homelab:deployment_service:info{service="unmapped"}`;
    - a workload carrying only `kustomize.toolkit.fluxcd.io/name=pihole-deferred`, which
      yields `service="pihole"`;
    - a workload carrying both labels, which yields one series;
    - a Job-owned pod and an ownerless pod in `homelab`, which yield no series;
    - a Job-owned pod in `longhorn-system`, which yields `service="k3s"`;
    - `kube_deployment_labels` absent, which maps every labelled Deployment to `unmapped`;
    - `kube_replicaset_owner` absent, which maps every ReplicaSet-owned pod outside the add-on
      namespaces to `unmapped`.
- Add `PrometheusRuleEvaluationFailures` to `alerting-meta.yaml`, with a case that raises
  `prometheus_rule_evaluation_failures_total` and asserts `service` and `severity`. Skip this
  if M1 has shipped it.
- Add the override derivation pytest and the Job restart-policy pytest.
- Deploys `observability`. kube-state-metrics rolls through its pod template. The relabel drop
  rolls the `Recreate` Prometheus through `checksum/config`, which leaves an evaluation gap and
  a WAL replay. While Prometheus is down, the bridge's `prometheus` gate suppresses its
  dependents, and Kuma's `Prometheus Reachable` tile can read DOWN. Deploy in a quiet window
  (M1 *Risks*).
- Two-writer check: the only alert added is `PrometheusRuleEvaluationFailures`, which no Kuma
  tile covers. The family still pages from Kuma only.

### PR 2 — The k8s-workloads rules in shadow (about 300 lines, two thirds of them promtool cases)

- Add `k8s-workloads.yaml` with `shadow: "true"` on every alert, `WorkloadServiceUnmapped`
  included, and its tests:
    - Each alert gets one case that fires it and asserts `service`, `severity` and `shadow`.
    - Each input family gets one zero-series case. For the families `K8sStateSeriesMissing`
      covers, the case asserts that alert with its `family` label.
    - A Job-owned pod in `homelab` with 4 restarts in the hour, the last inside 30m, fires
      `K8sContainerCrashLooping` with `service="unmapped"`, and `WorkloadServiceUnmapped` stays
      silent.
    - An unlabelled Deployment outside the table fires `WorkloadServiceUnmapped`.
    - A mapping rule that yields nothing leaves `K8sDeploymentReplicasUnavailable` firing with
      `service="unmapped"`, and fires `WorkloadServiceUnmapped` through its second clause.
- Add the rule-literal pytest in `ansible/tests/services/`, described under *Tests and docs*.
  Extend `test_dri_resource_name_agrees.py` to read the rule's `devic_es_dri` and compare it
  with the plugin's advertised name, sanitised.
- Add pint offline, its Renovate manager and its `--prove-red` fixture. Add
  `scripts/validate/pint/` to the `validate-alert-rules` hook's `files` regex, so a PR that
  edits only a pint config still runs the hook. Add `live.hcl`, and record in the PR its two
  red runs and its clean run, as *Verification* describes.
- Rewrite the comment in `ansible/roles/k8s/tdarr/defaults/main.yml` so that it holds on both
  sides of the cutover. It names the cluster's extended-resource check and the test that holds
  the strings together, instead of counting monitor-bridge's `K8S_EXTENDED_RESOURCES` default
  among "Four independent strings".
- This PR leaves alone the comment in
  `ansible/roles/k8s/crowdsec/templates/crowdsec-appsec-verify.sh.j2`, although it names
  `k3s Workload Health`. A `crowdsec` deploy runs the ban gate in
  `ansible/roles/k8s/crowdsec/tasks/verify.yml`, despite its `tags: [never, b1-gate]`. The
  comment above that file's resume probe says why: `deploy.yml` and `tasks/k8s_batch.yml`
  apply each entry's tags to every task in its role, so `--tags crowdsec` selects them. The
  gate bans daniel-pi at the edge for its run, and its task "Wait for the edge bouncer to pull
  after the ban" polls for 120s. The plugin stall that file's header describes can leave the
  bouncer without a pull for ten minutes, which fails the gate and the landing.
  `docs/crowdsec-waf-record.md` records the gate failing a deploy on 2026-09-27. A comment is
  not worth that. M6's B6 batch rewrites the AppSec check's script and deploys `crowdsec`
  anyway, so the comment moves there (a cross-slice note to M6). PR 4a, which makes the
  comment stale, files it with `findings.py open` so that it stays tracked until then.
- Deploys `observability` with no pod roll, because only the rules ConfigMap changes. The
  default `land.sh` flow also deploys `tdarr` for its comment. The render does not change, so
  nothing rolls. The tdarr role still runs its image pre-pull and the rollout-status gate in
  its `tasks/verify.yml`, and both pass against the running pod.
- Two-writer check: every new alert routes to the blackhole receiver. Kuma's tile remains the
  only pager.
- **Soak, shortened by the operator on 2026-10-17.** The soak ends after the 2026-10-18 Sunday
  reboot, about five days in, rather than on day 7. The window still spans that reboot, and the
  day-7 reading below is taken once its mute ends at 08:15 UTC. The rest of this bullet applies
  as written.
- **Soak.** Start the 7-day clock at the deploy. Run soak queries 1 to 3 from *Verification*,
  and the attribution step, on days 3 and 7. If retention reads under 7 days, run the second
  pass over the window retention covers and record it.

### PR 3 — The weekly-reboot mute (about 120 lines)

- Move the two variables to `group_vars/all.yml`, update the uptime-kuma consumers, add the
  time interval and the three route mutes, and add the derivation test and the routing
  pytest (*The weekly-reboot mute*).
- Deploys `observability` and `uptime-kuma`. The uptime-kuma render is byte-identical, so it
  rolls nothing.
- The edit to `alertmanager-secret.yaml` restarts Alertmanager alone, through M1 PR 5's
  `restart_secrets`, which has deployed before this PR. Prometheus and Grafana do not restart,
  so PR 3 leaves no evaluation gap and can land mid-soak without touching either soak query.
- **Verify that Alertmanager loaded the mute.** Read the three series under *PR 3* in
  *Verification* before and after the deploy. `alertmanager_config_hash` must change,
  `alertmanager_config_last_reload_success_timestamp_seconds` must be later than the deploy's
  start, and `kube_pod_start_time` must move for the Alertmanager pod alone. A deploy that
  restarted nothing leaves the old config running behind a green deploy (M1 *Risks*, class
  3). PR 4a does not merge until the PR records all three.
- Two-writer check: none. The mute applies only to alerts that already page through
  Alertmanager, that is the M1 meta rules and `PrometheusRuleEvaluationFailures`.

### The cutover is two PRs

M1 convention 5 has a cutover PR drop `shadow` and delete the Kuma producer in one commit.
That is unsafe here, because once the commit has merged, every deploy renders master's tip.
`scripts/deploy_tools/land_lib/deploy.py:deploy_by_host` renders each `deploy.sh` run at the
merge commit or at the primary checkout, and root `CLAUDE.md` says the tick pulls all of
master. M3, M4 and M5 each deploy `observability` in their own landings. Any such deploy
between this cutover's merge and its `monitor-bridge` deploy would ship live rules while the
full bridge still pages, which breaks convention 5.

The cutover therefore splits, and each half lands through the default `land.sh` flow:

- **PR 4a** narrows the bridge and the tile, and leaves every rule in shadow. Before and after
  it merges, every rule at master's tip is shadow. Whichever of the three roles deploys, and
  in whatever order, only the bridge can page the Kubernetes arms.
- **PR 4b** drops `shadow`. It merges only after PR 4a's verification has passed, so the
  bridge it meets is already the narrowed one.

Between PR 4a's `monitor-bridge` deploy and PR 4b's `observability` deploy, the Kubernetes arms
have no pager. That is the safe direction of convention 5. It departs from the convention's
one-commit rule, and the bound is stated under PR 4a.

### PR 4a — Narrow the bridge (about 50 lines added, about 1,100 removed)

- Replace the `k8s_workloads` row with the `workload_log_errors` row described under *The
  log-pattern arm stays on the bridge*. Add `check_workload_log_errors` to `checks/logs.py`,
  and rewrite the `checks/logs.py` module docstring and `with_log_errors`' docstring, which
  name `check_k8s_workloads` and the `LOKI_DEPENDENT` reasoning.
- Gate: Open question 6 is resolved, meaning another slice's text adopts the port, the row's
  deletion and the token's retirement. M6's batch A7 is that adoption (M6 *Open questions*
  10). The PR quotes A7's entry in M6 when it merges. If M6's text no longer carries A7, the
  gate is open again and this PR waits.
- Delete everything the tree above marks removed, including `ksm_resource_label` and
  `monitor_bridge_modules`' two paths. `ansible/tests/services/test_monitor_bridge_modules.py`
  fails if a path is left behind.
- Move two pieces of reasoning into `k8s-workloads.yaml` as `#` comments, so they outlive the
  bridge code:
    - The `DECIDED 30m` marker from `bridge/config_cluster.py`, placed at the crash-loop rule.
      `scripts/docs/reference/decisions.py` reads `#` lines from every tracked file, so the
      marker keeps its row in `docs/reference/decisions.md`.
    - The *Traps* item on sanitised resource names from the monitor-bridge `CLAUDE.md`, placed
      at the `devic_es_dri` literal.
- Rewrite every mention of the removed check in monitor-bridge's own code: the module
  docstrings of `checks/cluster.py` and `verdicts/cluster.py`, and the docstrings in
  `checks/host_thermal.py`, `checks/service.py`, `checks/storage.py` and
  `verdicts/host_smart.py`.
- Remove the family's Kubernetes rows from `docs/monitor-bridge-checks.md`, and update
  `docs/monitor-bridge-internals.md` for the renamed row and its `startup_grace` gate.
- File the stale comment in `crowdsec-appsec-verify.sh.j2` with `findings.py open`. PR 2 says
  why this PR does not edit it.
- **Deploy.** The default `land.sh` flow deploys `observability`, `uptime-kuma` and
  `monitor-bridge`, in that toposorted order. The `observability` deploy carries only the two
  rule comments, so every rule stays shadow and nothing rolls. `uptime-kuma` renames the tile,
  and `monitor-bridge` deploys the narrowed bridge, which keeps pushing on the same token.
  Run `probe.py health` for the three tags.
- **Verify before PR 4b merges.** `monitor_status{monitor_name="k3s Workload Health"}` returns
  nothing, `monitor_status{monitor_name="Workload Log Errors"}` returns one series, and
  `probe.py health monitor-bridge` passes, which shows the narrowed bridge's pod is the one
  running. The PR records the three reads and the `monitor-bridge` deploy time.
- **The no-pager bound.** PR 4b is open and green, stacked on this PR, before this PR merges.
  It is rebased onto master and landed as soon as the reads above pass. If PR 4b has not
  deployed within two hours of this PR's `monitor-bridge` deploy, revert this PR. The rules
  are still shadow, so the revert's default flow restores the full bridge as the only pager.
- Two-writer check: in this commit the Kubernetes arms page from neither side, the safe
  direction, and the log-pattern arm pages from Kuma only.

### PR 4b — Page from the rules (about 30 lines)

- Drop `shadow: "true"` from `k8s-workloads.yaml` and update its promtool cases, which assert
  the label.
- Rewrite the comment in `ansible/roles/setup/claude_code/templates/telemetry-health.sh.j2`,
  a host script, so that it names the `k8s-workloads` rules.
- Rewrite `ansible/roles/k8s/valheim/CLAUDE.md` so that pod death surfaces through
  `K8sDeploymentZeroAvailable` and `K8sContainerCrashLooping`.
- Gates: PR 4a's verification is recorded, PR 3's reload check and routing pytest have
  passed, and C7's amendments for Open questions 3 and 5 have landed in contracts.md, because
  this PR is the one that makes `service="k3s"` and `service="unmapped"` page.
- **Deploy.** `observability` is Flux-owned (`owner: flux`), so `land.sh` does not deploy it:
  the rules ConfigMap ships when `promote` verifies the merged bundle. The role's `.md` edit
  rolls no pod. Prometheus loads the rules within its 30s auto-reload, and only the rules
  ConfigMap changes, so nothing rolls. Run `probe.py health observability` after `promote`
  verifies.
- Any deploy at master's tip from here on ships the narrowed bridge and live rules, so no
  interleaving pages twice.
- Two-writer check: in this commit the Kubernetes arms page from Alertmanager only, and the
  log-pattern arm pages from Kuma only.

### PR 5 — No token retirement in this slice

The push token `monitor_bridge_k8s_workloads_push_token` stays, because the
`workload_log_errors` row still pushes on it. M6's A7 ports the arm, and A7's token PR
retires the token a week after A7's Cutover-b (Open question 6).

The rule for any token retirement still applies there. A retirement lands a week after the
cutover that frees the token. Until then, a revert of the cutover needs the token, and a
retired key cannot come back by revert: restoring it is a new secret (**operator-only**).

## Verification

```bash
# Coverage: each kind's workloads with no real mapping (expect empty)
uv run python scripts/diagnostics/probe.py metric 'kube_deployment_status_replicas_unavailable unless on(namespace, deployment) homelab:deployment_service:info{service!="unmapped"}'
uv run python scripts/diagnostics/probe.py metric 'kube_daemonset_status_number_unavailable unless on(namespace, daemonset) homelab:daemonset_service:info{service!="unmapped"}'
uv run python scripts/diagnostics/probe.py metric 'max by (namespace, pod) (kube_pod_container_status_restarts_total) unless on(namespace, pod) homelab:pod_service:info{service!="unmapped"} unless on(namespace, pod) kube_pod_owner{owner_kind=~"Job|",namespace!~"kube-system|longhorn-system|metallb-system"}'
# Which workloads are unmapped, when one of the three is not empty
uv run python scripts/diagnostics/probe.py metric '{__name__=~"homelab:.*_service:info",service="unmapped"}'
uv run python scripts/diagnostics/probe.py metric 'count(kube_replicaset_owner)'
# The rules are loaded, evaluate cleanly, and their inputs exist
uv run python scripts/diagnostics/probe.py metric 'prometheus_rule_group_rules{rule_group=~".*(k8s-workloads|workload-service)"}'
uv run python scripts/diagnostics/probe.py metric 'increase(prometheus_rule_evaluation_failures_total{rule_group=~".*(k8s-workloads|workload-service)"}[1h])'
uv run python scripts/diagnostics/probe.py metric 'ALERTS{alertname=~"K8s.*|WorkloadService.*"}'
# Soak, with <H> the whole hours since PR 2's deploy (M1 convention 6), run while the tile reads up.
# Query 1: minutes Kuma read DOWN with no shadow alert firing within 8 minutes either side
uv run python scripts/diagnostics/probe.py metric 'count_over_time(((monitor_status{monitor_name="k3s Workload Health"} == 0) unless on () (max_over_time((count(ALERTS{alertstate="firing",shadow="true",alertname=~"K8s.*"}) > 0)[16m:1m] offset -8m)))[<H>h:1m])'
# Query 2: minutes a shadow alert fired with Kuma up within 8 minutes either side (explain each episode)
uv run python scripts/diagnostics/probe.py metric 'count_over_time(((count(ALERTS{alertstate="firing",shadow="true",alertname=~"K8s.*"}) > 0) unless on () (max_over_time((monitor_status{monitor_name="k3s Workload Health"} == 0)[16m:1m] offset -8m)))[<H>h:1m])'
# Query 3: minutes the mapping alert fired; it has no Kuma counterpart (expect empty)
uv run python scripts/diagnostics/probe.py metric 'count_over_time((count(ALERTS{alertstate="firing",shadow="true",alertname=~"WorkloadService.*"}) > 0)[<H>h:1m])'
# Attribution: the bridge's DOWN episodes with their messages, <D> the whole days since PR 2's deploy
uv run python scripts/diagnostics/probe.py alerts --check k8s_workloads --days <D>
# Duration of one bridge episode's condition, in minutes: <ARM> is the arm's expression for the
# workload the episode names, <M> the episode's length plus 16, <END> its end plus 8m (Unix time)
uv run python scripts/diagnostics/probe.py metric 'count_over_time((<ARM>)[<M>m:1m] @ <END>)'
# PR 3: read before and after the deploy. The hash changes, the success time follows the
# deploy, and only the Alertmanager pod's start time moves
uv run python scripts/diagnostics/probe.py metric 'alertmanager_config_hash'
uv run python scripts/diagnostics/probe.py metric 'alertmanager_config_last_reload_success_timestamp_seconds'
uv run python scripts/diagnostics/probe.py metric 'kube_pod_start_time{namespace="observability"}'
# After PR 4a: the old tile is gone, and the narrowed one is live
uv run python scripts/diagnostics/probe.py metric 'monitor_status{monitor_name="k3s Workload Health"}'
uv run python scripts/diagnostics/probe.py metric 'monitor_status{monitor_name="Workload Log Errors"}'
uv run python scripts/diagnostics/probe.py health monitor-bridge
```

**Which soak minutes count as explained.**

- **The 8-minute margin.** Queries 1 and 2 accept a match within 8 minutes either side, using
  a negative `offset`, which Prometheus 3.15 accepts. The margin covers two lags of one
  episode. At recovery, the tile waits up to 300s for the bridge's next push. At onset, every
  rule's `for:` is 300s longer than the bridge's (N − 1) × 300s hold, the no-hold arms
  included. Each lag also carries up to 2 minutes of scrape and evaluation. The last 8 minutes
  of the window have no future data, so run the queries while the tile reads up.
- **The attribution step.** Every minute query 1 still returns sits in a bridge episode that
  `probe.py alerts` lists, with each cycle's message. Three kinds of episode are explained,
  and the PR lists each one:
    - An episode whose message leads with `fatal log lines` belongs to the log-pattern arm,
      which this slice leaves on the bridge.
    - An episode whose messages name a different workload in each cycle is the arm-wide streak
      that a per-series `for:` drops by design.
    - An episode shorter than the rule's `for:`. The bridge pages a no-hold arm on one bad
      sample, and a held arm on an episode as short as (N − 1) × 300s. The rule drops both by
      design (*The rules*). The duration query in the block above counts the minutes the arm's
      condition held for the workload the episode names. A count below the rule's `for:` in
      minutes explains the episode. The count includes every bad minute, contiguous or not, so
      a count below `for:` proves no contiguous run reached it. The crash-loop arm has no
      `for:`, so this kind never covers it.

  Any other minute is a miss, and the rule is tuned before PR 4a. In particular, an episode
  whose condition held at least as long as the rule's `for:` while no shadow alert fired is a
  miss, and only such a miss tunes a hold.

The live pint pass runs on daniel-box:
`scripts/validate/alert_tools.sh pint --config scripts/validate/pint/live.hcl lint
ansible/roles/k8s/observability/files/rules/`. `live.hcl` reaches Prometheus at its pinned
ClusterIP, because the loopback hostPort answers only on the node the pod runs on. A
`promql/series` finding fails it, and it is
clean before PR 2 merges. PR 2 records two red runs before the clean one, so a clean result
is shown to come from a Prometheus that answered:

- The same command over a scratch rule file outside the tree, whose one alert reads a series
  Prometheus has never held, such as `homelab_pint_red_proof_total > 0`. It must report
  `promql/series` at `bug` level and exit non-zero.
- The same command with a copy of `live.hcl` whose `uri` names a closed loopback port. Because
  the block sets `required = true`, it must report the connection failure at `bug` level and
  exit non-zero.

**Red proofs for the new checks:**

- Each alert: its promtool case with non-empty `exp_alerts`.
- The zero-series rules: their cases with no input series, one per family.
- The join fallback: the empty-mapping case and the restarting Job-pod case, each of which
  fires with `service="unmapped"`.
- The mapping: the unlabelled Deployment case, which fires `WorkloadServiceUnmapped`.
- `PrometheusRuleEvaluationFailures`: its case with a rising failure counter.
- The relabel drop: its `MUST_DROP` and `MUST_KEEP` pair, each half failing if the regex moves.
- pint offline: the `--prove-red` fixture with no `severity` label.
- pint live: PR 2's two recorded red runs, one for a never-held series and one for an
  unreachable Prometheus.
- The override derivation pytest: its `..._is_flagged` half feeds a rendered workload missing
  from the table.
- The Job restart-policy pytest: its `..._is_flagged` half feeds a Job with
  `restartPolicy: OnFailure`.
- The rule-literal pytest: an `..._is_flagged` half for each assertion, among them a render
  with one DaemonSet removed against the floor of 11.
- The mute derivation test: its half that moves `weekly_reboot_hour` and expects the window to
  move.
- The routing pytest: its `Watchdog` half, which reaches an unmuted route and must be flagged.
- PR 3's reload check: the before-and-after reads. An unchanged `alertmanager_config_hash`
  fails it.
- Live, during the soak: any organic workload outage fires the shadow rules. That proves the
  rules fire on real input. It proves nothing about the mute, because every shadow alert
  reaches the blackhole route whatever the mute does. The routing pytest is the mute's proof.

## Rollback

- **PR 1.** Revert and deploy `observability`, in a quiet window: kube-state-metrics and
  Prometheus roll back. The recording rules and `PrometheusRuleEvaluationFailures` disappear
  on the next reload.
- **PR 2.** Revert and land through the default flow, which deploys `observability` and
  `tdarr`. The shadow rules disappear, and nothing paged them.
- **PR 3.** Revert and deploy `observability` and `uptime-kuma`. The Secret edit restarts
  Alertmanager alone, as in PR 3. The variables return to the uptime-kuma role, and the meta
  rules page through reboots again.
- **PR 4b.** Revert and land through the default flow, which deploys `observability` and
  restores the `shadow` label. The Kubernetes arms then have no pager until PR 4a is reverted
  as well, or until PR 4b is landed again.
- **PR 4a.** Revert only while PR 4b is unmerged or already reverted and deployed. Reverting
  PR 4a while PR 4b's live rules run would restore the full bridge beside them, a second
  pager. The default flow deploys `observability`, `uptime-kuma`, which renames the tile back,
  and `monitor-bridge`, which deploys the full check on the same token. The token stays in
  `secrets.yml` until A7's token PR, so the revert works at any time before M6's A7 Cutover-a.
- **PR 4a after M6's A7 Cutover-a.** PR 4a no longer reverts alone. Cutover-a deletes
  `checks/logs.py:with_log_errors`, which the restored `k8s_workloads` row would call. After
  A7's Cutover-b, the restored log-pattern arm would also page beside `WorkloadLogErrors`.
  Revert A7's PRs first, in the order M6 *Rollback* gives for a Phase A batch, then PR 4b,
  then PR 4a. This works only while A7's token PR has not landed, because the restored row
  pushes on `monitor_bridge_k8s_workloads_push_token`. Restoring a retired token is a new
  secret (**operator-only**).
- **PR 4a and PR 4b together.** One revert commit of both is also safe. Its toposorted flow
  deploys `observability` first, which restores `shadow` before `monitor-bridge` restores the
  full check. The no-pager window lasts one `deploy.sh` run.
- **PR 5.** Nothing to undo.
- **A partial PR 4a deploy** (failure class 5). The k8s play applies the roles of a batch and
  waits on their rollouts at the batch's end (`ansible/tasks/k8s_batch.yml`), so a failed
  landing can leave any of the three roles applied. Every rule is still shadow, so no state
  pages twice. What matters is which bridge runs, and `probe.py health monitor-bridge`
  answers that:
    - The old bridge still runs: Kuma pages every arm, under either tile name. PR 4b must not
      merge. Fix and re-run the landing.
    - The narrowed bridge runs, and only `uptime-kuma` failed: the tile keeps its old name but
      carries only the log-pattern arm. Re-run the `uptime-kuma` deploy to rename the tile
      before PR 4b merges, because PR 4a's verification reads the tile's name.
- **A failed PR 4b deploy.** The rules ConfigMap is either applied or not, so the Kubernetes
  arms page from Alertmanager or from nothing, never from both. Re-run the landing. If it
  cannot succeed, revert PR 4a and PR 4b together within the hour.

## Risks

- **Class 3, empty read as clean.** A per-workload rule over an empty vector is silent.
  Mitigation:
    - the two census alerts, `K8sNodeAllocatableMissing` and `K8sStateSeriesMissing`, each
      with a zero-series case per family;
    - the join fallback, so an empty mapping never silences a per-workload alert;
    - `WorkloadServiceUnmapped`'s second clause and `PrometheusRuleEvaluationFailures`, for a
      mapping rule that stops evaluating.
- **Class 4, a selector drifting from the hazard.** A renamed label or a new unlabelled role
  drops workloads out of the mapping. Mitigation: the `unmapped` catch-all and its alert, and
  the derivation pytest with named members.
- **A derivation narrowing the list it replaces.** The pod domain leaves out Job-owned and
  ownerless pods outside the add-on namespaces, which the bridge read. Mitigation: the census in
  *Baseline* shows none of them can restart in place, the Job restart-policy pytest holds that,
  and the join fallback still fires on any that does.
- **Class 2, a proxy for the property.** A `for:` is not exactly a streak:
    - A streak resets on one good cycle. A `for:` resets on one good evaluation, and
      evaluations are five times as frequent.
    - A `for:` is per series, and the bridge's replica streak is per arm.
    - A mapping that flickers between a tag and `unmapped` changes the alert's label set, which
      restarts its `for:`.

  Mitigation: the soak measures the difference, its attribution step names the arm-wide case,
  and `keep_firing_for` is the remedy if flapping shows.
- **The N × 300s holds drop a short episode the bridge paged.** That is the design: a
  1-minute evaluation with no hold would page every DaemonSet rollout. Mitigation: the
  attribution step lists each such episode with its measured duration, and an episode whose
  condition outlasted the rule's `for:` with no shadow alert counts as a miss, which tunes the
  rule before PR 4a.
- **Prometheus restarts in PR 1.** It is a `Recreate` swap with an evaluation gap. Mitigation:
  deploy in a quiet window (M1 *Risks*). PR 3 restarts Alertmanager alone.
- **Class 3, PR 3 restarts nothing.** Alertmanager would keep its old config behind a green
  deploy, and the mute would never load. Mitigation: PR 3's reload check, which gates PR 4a.
- **Class 5, a sequence treated as a transaction.** The cutover is two PRs and three role
  deploys, and it can stop between any of them. Mitigation: every rule stays shadow until PR
  4b, PR 4b merges only after PR 4a's verification, the two-hour bound under PR 4a, and the
  partial-failure entries under *Rollback*.
- **The no-pager window between PR 4a and PR 4b.** An outage that starts in it pages only
  once PR 4b deploys and the rule's `for:` elapses. Mitigation: PR 4b is ready before PR 4a
  merges, and the two-hour bound reverts PR 4a if PR 4b cannot follow.
- **The log-pattern arm outlives this slice on the bridge.** It pages from Kuma until its
  port's cutover. M6's A6-7 deletes the push path and every bridge tile, so a narrowed row
  with no port would lose its only pager there. Its stay lasts at least as long as A7's
  soak, which cannot start before PR 4a creates the tile it compares against. Mitigation: Open
  question 6 gates PR 4a on another slice's text adopting the port, the row's deletion and
  the token's retirement. M6's A7 adopts all three, and M6's A6-7a waits for A7's Cutover-a.
- **Cardinality.** `replicasets` adds series. Mitigation: the relabel drop keeps one family. PR
  1 counts the `kube_replicaset_*` series it adds, against M1's series budget.
- **Flux substitution and `$1`.** The recording rules use `label_replace` with `$1`.
  Mitigation: M1 open question 7 resolves this in R2, before D5 moves `observability` to Flux.
- **D5 reads the same alerts.** D5's soak joins `homelab:*_service:info`, so a mapping bug would
  mislead the promote job. Mitigation: the `unmapped` alert pages first, and D5 reads
  `ALERTS{shadow!="true"}` (M1 open question 5).

## Tests and docs that change in the same PR

**`docs/facts.lock`, until G2 retires it.** PR 1, PR 3, PR 4a and PR 4b edit the
observability, uptime-kuma, monitor-bridge and valheim `CLAUDE.md` files, and each of those
files has sections with rows in the lock. Until G2 retires `docs/facts.lock`, each of the four
PRs runs this step:

- When the `facts-reverify-changed` hook rewrites the lock, stage the lock and commit it with
  the PR.
- When the hook or CI's `test_every_recorded_atom_hashes_as_recorded` reports a `moved` or
  `missing` finding that the hook does not fold, run
  `uv run python scripts/dev/fact_status.py verify '<doc>#<heading>'` for that section and
  commit the lock.

Once G2's PR 3 has merged, the hook, the `verify` subcommand and the lock are gone, and the
step drops. A branch cut before that merge still runs the hook from its own `prek.toml`. On
rebase it takes the lock's deletion, as G2's *Risks* says. G2's `lock-retired` rule fails a
branch that adds the file back.

- **PR 1.**
    - `workload-service.test.yaml`, and `PrometheusRuleEvaluationFailures`' case in
      `alerting-meta.test.yaml`.
    - The `MUST_DROP` and `MUST_KEEP` entries in `test_metric_drop_regexes.py`.
    - The override derivation pytest in `ansible/tests/k8s/`.
    - The Job restart-policy pytest in `ansible/tests/k8s/`.
    - The observability role's `CLAUDE.md` gains a paragraph on the mapping, the allowlist and
      the pod domain.
- **PR 2.**
    - `k8s-workloads.test.yaml`, `scripts/validate/pint/offline.hcl`,
      `scripts/validate/pint/live.hcl`, the pint `--prove-red` fixture, and the Renovate
      manager in `renovate.json`.
    - The rule-literal pytest in `ansible/tests/services/`. Until PR 4a it asserts that each
      rule literal equals its bridge source: the floors equal `K8S_MIN_WORKLOADS` and
      `K8S_MIN_DAEMONSETS`, each `for:` equals its `K8S_*_CONSECUTIVE` times `INTERVAL`, and the
      crash-loop `1h`, `3` and `30m` equal the three `K8S_RESTART_*` values. Two assertions
      survive PR 4a:
        - The DaemonSet floor equals the DaemonSets rendered by the roles in
          `containers_list`, plus a literal list of the add-on DaemonSets Longhorn and MetalLB
          bring (`longhorn-manager`, `longhorn-csi-plugin`, the engine-image DaemonSet by name
          prefix, and `speaker`). It names `dri-device-plugin` as a rendered member it must
          find.
        - The crash-loop recency window is at least 30m, the floor the `DECIDED 30m` marker
          derives from 15-19 minute restart spacing, and shorter than the evidence window.
    - `test_dri_resource_name_agrees.py` reads the rule's `devic_es_dri` as well.
    - The comment in the tdarr defaults. The crowdsec script template's comment stays, and M6's
      B6 batch rewrites it.
- **PR 3.**
    - The mute derivation test, plus the renamed-variable edits in
      `test_maintenance_window.py` if it reads the old names.
    - The routing pytest in `ansible/tests/services/`, with its `Watchdog` red half.
    - The uptime-kuma role's `CLAUDE.md` maintenance-window section names the new `group_vars`
      keys.
- **PR 4a.**
    - Deletes the four bridge test files.
    - Deletes `test_check_host.py:test_crash_loop_arm_gates_on_a_recent_restart_not_just_the_hour_window`
      and `test_check_host.py:test_the_recency_window_is_wider_than_the_worst_observed_restart_spacing`.
      The rule-literal pytest has held the second one's floor since PR 2.
    - Deletes the `ksm_resource_label` and `extended_resource_verdict` tests in
      `test_check_longhorn.py`.
    - `test_check_loki.py` gains a test that the `workload_log_errors` body pages on a burst
      and stays up on a Loki error. `test_check_table.py` and `test_check_streaks.py` accept
      the row's `startup_grace` gate unchanged.
    - The rule-literal pytest loses its bridge half, and `test_dri_resource_name_agrees.py`
      loses its `bridge/config.py` read.
    - `test_config_reads_the_rendered_env.py` swaps its named member for
      `("config_service.py", "HA_BAN_WINDOW", True)`.
    - `test_kuma_push_resend_beats.py` names `Workload Log Errors` instead of
      `k3s Workload Health`.
    - `test_kuma_discord_template.py` and `test_kuma_email_template.py` keep
      `k3s Workload Health`, because there it is only sample payload text.
    - `docs/monitor-bridge-checks.md` and `docs/monitor-bridge-internals.md` change as PR 4a
      lists. The `regen-doc-fragments` hook rewrites
      `docs/assets/generated/fragments/bridge-thresholds.md`, `bridge-checks.md` and
      `bridge-gate-sets.md`, and all three are committed with the PR. The gate-sets fragment
      moves because the row leaves the `prometheus` set and joins `startup_grace`.
    - `ansible/roles/k8s/monitor-bridge/CLAUDE.md` loses the *Traps* item on sanitised
      resource names, which moves to the rule. The *Traps* section has no row in
      `docs/facts.lock`. The monitor-bridge glance block regenerates.
    - The `ansible/roles/k8s/home-assistant/templates/deployment.yaml.j2` comment stays,
      because it records a dated incident ("2026-08-23: 3 restarts, paging k3s Workload
      Health") that remains true.
- **PR 4b.**
    - `k8s-workloads.test.yaml` drops `shadow` from each expected label set.
    - `ansible/roles/k8s/valheim/CLAUDE.md` and the `telemetry-health.sh.j2` comment change as
      PR 4b lists.
- **PR 5.** None.

## Complexity delta

- **Added:** about 90 lines of recording rules and about 120 lines of alert rules, with about
  350 lines of promtool tests. Also two pint configs and a pint fixture, three pytests
  (override derivation, Job restart policy, rule literals), one mute test, one routing pytest,
  one meta alert, a kube-state-metrics family and a 10-line bridge body.
- **Removed:** about 75 lines of check body, 172 lines of helper modules, about 150 lines of
  verdict code, the family's config fields and env keys, 586 lines of tests and four tests in
  two other files. The tile and the push token stay for the log-pattern arm until M6's A7
  ports it (Open question 6).
- **Net:** fewer lines. The Kubernetes arms move from five mechanisms (row, body, token, tile,
  streak) to one (rule plus test).

## Open questions

1. **Contract gap, C1 and C3.** No contract pins the label that names an object's deploy tag.
   The workload-service mapping reads `homelab/role`, and `kustomize.toolkit.fluxcd.io/name`
   with `-deferred` stripped under Flux. `ansible/templates/role-labelled.yaml.j2` stamps
   `homelab/role`, and `ansible/roles/k8s/manifests/tasks/main.yml` applies that wrapper to
   the roles that arm `manifests_prune`. R4, which follows D6, deletes that role. Resolution: C1
   requires the renderer to keep stamping `homelab/role`, or C7 adopts the Flux label. Either
   way, both stay in the allowlist until D6. Gate: before D6 merges. Requested as a row of
   contracts.md *Requested amendments not yet applied*.
2. **The four unlabelled homelab workloads.** The override table stands in for labels that
   `authelia`, `nut` and pihole's deferred render never stamp. Resolution: once D5 moves those
   roles to Flux, their objects carry the Flux label. Step 2 strips `-deferred`, so `pihole-2`'s
   `pihole-deferred` resolves to `pihole`. PR 1's derivation pytest then demands the table
   shrink to nothing.
3. **`service="k3s"` for cluster add-ons.** C7 says "deploy tag or host name". `k3s` is the
   setup role's tag, not a `deploy.sh` tag. Resolution: confirm in C7, or name the host
   (`daniel-box`). H1 moves the add-ons to Flux and settles it either way. Gate: before M2's
   PR 4b merges, because that PR makes alerts carrying `service="k3s"` page. Requested as a
   row of contracts.md *Requested amendments not yet applied*.
4. **Paging severity.** The family stays `warning`, matching the tile's Discord-only paging.
   Resolution: the operator decides after the soak whether `K8sDeploymentZeroAvailable` earns
   `critical`, which would also email.
5. **Contract gap, C7: `service="unmapped"`.** A per-workload alert carries `unmapped` when the
   mapping cannot resolve its workload. That value is neither a deploy tag nor a host name, so
   `probe.py health <tag>` never matches it. For a workload in the mapping's domain,
   `WorkloadServiceUnmapped` pages with `service="observability"` at the same time. A
   restarting pod outside the domain pages on its own. Resolution: C7 records `unmapped` as
   the one value outside its rule and names the mapping alert as what pages alongside it.
   Gate: before M2's PR 4b merges, because that PR makes alerts carrying `service="unmapped"`
   page. Requested as a row of contracts.md *Requested amendments not yet applied*.
6. **Owner of the log-pattern arm's port.** M2 does not port the arm. After PR 4a, the
   narrowed row is the only caller of `with_log_errors`. **Gate: before M2's PR 4a merges,
   another slice's text adopts three things:** a LogQL rule for the arm on loki-homelab,
   shadow until the row is gone; the deletion of the `workload_log_errors` row with
   `check_workload_log_errors`, `with_log_errors`, `verdicts/cluster.py:log_error_verdict`
   and the `LOG_ERROR_*` keys, with the `DECIDED stays at 20` marker moved into the rule
   file; and the retirement of `monitor_bridge_k8s_workloads_push_token` a week after the
   rule pages. Resolution: M6 adopts all three as its batch A7 (M6 *Open questions* 10), and
   that adoption meets the gate. M5 declined (M5 *Open questions* 6). M5's PR 5 retires three
   tokens, and this one is not among them. M5 also keeps
   `monitor_bridge_swallowed_verdicts_push_token` until M6's C-7 deletes its row. M6's count
   of 41 rows left after M2 and M5 includes `workload_log_errors`. A7's shadow PR waits for
   M5's ruler. A7's soak starts only after PR 4a creates the "Workload Log Errors" tile it
   compares against. A7's Cutover-a deletes the row, so it follows PR 4a. M6 depends on all of
   M2 in the slice index, which already orders that, so no contracts.md sub-edge is needed.
   No part of M5 waits on PR 4a, because the row no longer names `loki_reachable`.
7. **Owner of `PrometheusRuleEvaluationFailures`.** It is a meta rule, and M1 owns
   `alerting-meta`. Resolution: M1 adopts it in its PR 3. M2's PR 1 adds it only if M1 has not.

## Review log

### Round 1 — per-slice adversarial review

Findings raised, and what happened to each. None was refuted.

- **M2-R1-01 (critical): The log-pattern arm is a paging arm, and PR 4 deletes it with no
  replacement.** Fixed. Confirmed against `checks/logs.py:with_log_errors`, which returns
  `False` on a burst, and the `DECIDED stays at 20` marker. *Baseline* now names it as an arm.
  The fix takes option (a): PR 4 narrows the row to `workload_log_errors` on the same token and
  `kuma_id`, and M5 ports it. All of M5's PR 4 follows M2's PR 4, because it deletes the
  `loki_reachable` gate the narrowed row names. PR 5's token retirement moves to M5. The soak
  attributes the arm's DOWN minutes through `probe.py alerts`. Cross-slice notes go to M5, M6
  and contracts.
- **M2-R1-02 (high): Job-owned and ownerless pods have no path through
  `homelab:pod_service:info`.** Fixed by a justified narrowing. The live census confirmed 20
  Job pods and one ownerless pod in `homelab`, all under `restartPolicy: Never` with 0 restarts
  over 7 days. The pod domain now excludes them outside the add-on namespaces, and a pytest
  holds the restart policy. The join fallback still fires on a restarting excluded pod. The
  soak gains query 3 for `WorkloadService.*`, and exit criterion 2 widens its selector.
  promtool cases cover a Job-owned pod with and without restarts.
- **M2-R1-03 (high): PR 4's deploy order cannot be produced through `land.sh`.** Fixed.
  Confirmed through `toposort_containers` and the `containers_list` order. PR 4 runs `land.sh
  --tags uptime-kuma,monitor-bridge`, then `deploy.sh --tags observability` by hand, and states
  the departure. The partial-failure section follows that order. The rollback uses the default
  toposorted order, which puts `observability` first and so opens no double-pager window.
- **M2-R1-04 (high): The join inputs fail open.** Fixed. Every per-workload alert now falls
  back to `service="unmapped"` instead of inner-joining. The mapping rules take their base from
  the status families. `K8sStateSeriesMissing` covers `kube_pod_owner`,
  `kube_replicaset_owner`, the two label families and the three other Deployment families.
  `WorkloadServiceUnmapped` gains a coverage clause. Each join input has a zero-series case.
- **M2-R1-05 (high): Several alerts carry no valid C7 service label.** Fixed. The census, input
  and mapping alerts take `service: observability`, and `K8sExtendedResourceMissing` takes
  `dri-device-plugin`. `WorkloadServiceUnmapped` overrides `unmapped` with `observability`. The
  fallback value is recorded as a C7 exception in open question 5.
- **M2-R1-06 (medium): PR 1 rolls Prometheus, and PR 3 restarts Prometheus and Grafana.**
  Fixed. PR 1 and PR 3 now state the restarts, PR 3 adds Alertmanager, and both carry M1's
  quiet-window mitigation. The soak notes record PR 3's restart.
- **M2-R1-07 (medium): The `for:` values are shorter than the bridge's measured hold.** Fixed.
  `for:` is N × 300s: 15m, 10m and 15m for the three held arms, and 5m for the arms the bridge
  pages on one sample. The 8-minute soak margin then covers the onset lag of every arm. The
  per-series change from the arm-wide streak is stated and attributed in the soak. Soak query 1 sees it, not query 2, because an arm-wide page with
  no rule firing is a Kuma DOWN minute.
- **M2-R1-08 (medium): PR 4 breaks tests the plan does not list.** Fixed. *Tests and docs*
  lists each one, and the rule-literal pytest pins `devic_es_dri`, `1h`, `30m` and `3` from PR
  2 on. The `DECIDED 30m` marker moves into the rule file.
- **M2-R1-09 (medium): Exit criterion 4 cannot show that every Deployment is mapped.** Fixed.
  Exit criterion 4 and *Verification* now gate on three coverage queries returning nothing.
- **M2-R1-10 (medium): Soak query 1 counts the tile's lag after recovery as missed minutes.**
  Fixed. Queries 1 and 2 accept a match within 8 minutes either side, with the lag arithmetic
  stated. `WorkloadServiceUnmapped` gets its own query 3 rather than joining query 1, where it
  would explain away unrelated Kuma DOWN minutes.
- **M2-R1-11 (medium): Under Flux, a deferred workload's label resolves to `<tag>-deferred`.**
  Fixed. Step 2 strips `-deferred`, open question 2 says so, and PR 1 adds a `pihole-deferred`
  case.
- **M2-R1-12 (medium): Three new checks ship with no red proof.** Fixed. pint gets a
  `--prove-red` fixture, the drop gets its `MUST_DROP` and `MUST_KEEP` pair, and the
  rule-literal pytest pins the floor of 11 to the rendered DaemonSets plus the named add-ons,
  with an `..._is_flagged` half.
- **M2-R1-13 (medium): Docs and comments that describe the removed tile are not updated.**
  Fixed, except for one comment. The crowdsec and tdarr comments move to PR 2 with wording true
  on both sides, because in PR 4 their tags would fall outside the `--tags` landing. The
  Prometheus job comment moves to PR 1, which already rolls Prometheus. `ksm_resource_label` is
  deleted, and the *Traps* item moves to the rule. The home-assistant comment stays, because it
  records a dated incident that remains true.
- **M2-R1-14 (medium): PR 1 and PR 2 disagree about where `WorkloadServiceUnmapped` lives.**
  Fixed. It lands in PR 2, in shadow. PR 1's case asserts the recording-rule output only.
- **M2-R1-15 (medium): Rule evaluation failures go unwatched.** Fixed.
  `PrometheusRuleEvaluationFailures` lands in PR 1 with a promtool case, and open question 7
  offers it to M1.

### Round integration — cross-slice review

One finding was raised against this slice, and it was fixed. None was refuted.

- **X-21 (medium): Slices commit `docs/facts.lock` unconditionally, though G2 deletes it.**
  Fixed. M2 never ran `fact_status.py verify`. Its one lock step, in PR 4, was conditional on
  the `facts-reverify-changed` hook but not on G2. That step was also misplaced: the
  monitor-bridge *Traps* section has no row in the lock. The lock's units at `70591c307`
  show rows in all four role `CLAUDE.md` files that PR 1, PR 3 and PR 4 edit, among them the
  uptime-kuma maintenance-window section and valheim's *Notable*, which those PRs rewrite.
  *Tests and docs* now opens with one step for those three PRs, worded "Until G2 retires
  `docs/facts.lock`" as R4 words it. The
  step names the hook, `verify` for an unfolded `moved` or `missing` finding, and what drops
  once G2's PR 3 deletes the hook, `cmd_verify` and the lock.

### Round 10 — per-slice adversarial review

Eight findings were raised, two high and six medium. All eight were fixed, and none was
refuted.

- **M2-R10-01 (high): No slice ports the log-pattern arm, so the R1-01 fix rests on a handoff
  that never landed.** Fixed. Confirmed in the tree and in the other slices. `git grep
  with_log_errors` finds only `checks/cluster.py:check_k8s_workloads` and the tests, so M5's
  "which other checks still call" is false. M5's PR 5 omits the token, and M6's 39-row count
  omits the narrowed row. `gates.py:_members` derives `LOKI_DEPENDENT` from the `gate` column,
  so a row naming `loki_reachable` would outlive M5's gate deletion. The narrowed row now takes
  `gate="startup_grace"` unconditionally, at the cost of one 300s held cycle, and
  `bridge-gate-sets.md` joins PR 4a's regenerated fragments. Open question 6 is a gate on PR
  4a: M5's text adopts the port, the row's deletion and the token's retirement first. M6 is
  the fallback owner. *Unblocks*, PR 5, *Risks* and *Complexity delta* no longer credit M5.
  Cross-slice notes go to M5, M6 and contracts.
- **M2-R10-02 (high): PR 2's crowdsec deploy runs the b1 ban gate, which the plan said it
  skips.** Fixed. Confirmed by the comment above the resume probe in
  `ansible/roles/k8s/crowdsec/tasks/verify.yml` and by the `apply: tags` in `ansible/deploy.yml`
  and `ansible/tasks/k8s_batch.yml`. PR 2 drops the crowdsec comment edit, and its deploy and
  *Rollback* no longer name `crowdsec`. The task that waits for the bouncer's pull polls for
  120s, and a pull stall can last ten minutes, so the gate could fail the landing. M6's B6
  batch, which deploys `crowdsec` anyway, takes the comment as a cross-slice note.
- **M2-R10-03 (medium): PR 3's restart set contradicts M1 PR 5, and nothing checks that
  Alertmanager loaded the mute.** Fixed. PR 3 restarts Alertmanager alone, through M1 PR 5's
  `restart_secrets`. The Prometheus attribution is gone from the soak rules, *Risks* and
  *Rollback*. PR 3 reads `alertmanager_config_hash`,
  `alertmanager_config_last_reload_success_timestamp_seconds` and `kube_pod_start_time`
  before and after its deploy, and PR 4a waits on that record.
- **M2-R10-04 (medium): Nothing proved the routes carry the mute before the cutover, and the
  claim that M4 proves it was false.** Fixed. PR 3 adds a routing pytest that walks a warning
  and a critical label set through the rendered `alertmanager.yml`, with a `Watchdog` red
  half. The sentence about M4 is replaced, and the live red-proof item no longer claims to
  prove the mute. A cross-slice note asks M4 to extend this walk.
- **M2-R10-05 (medium): Another session's observability deploy could open a double-pager
  window during PR 4.** Fixed. Confirmed through
  `scripts/deploy_tools/land_lib/deploy.py:deploy_by_host`. PR 4 splits into PR 4a, which
  narrows the bridge with every rule still shadow, and PR 4b, which drops `shadow` after PR
  4a's verification. Both land through the default flow, so the hand deploy is gone. PR 4a
  bounds the no-pager window at two hours. *Rollback* reverts PR 4b before PR 4a, or both in
  one commit. This supersedes R1-03's `--tags` landing.
- **M2-R10-06 (medium): Soak query 1 counted episodes the 5m holds drop by design as
  misses.** Fixed. The attribution step gains a third explained kind: an episode whose
  condition, counted in minutes by the new duration query, held for less than the rule's
  `for:`. Only an episode that outlasted its `for:` tunes the rule.
- **M2-R10-07 (medium): The live pint `promql/series` pass was a merge gate with no red
  proof.** Fixed. `live.hcl` sets `required = true`, which pint's configuration reference
  says turns an unreachable server from a `warning` into a `bug`. PR 2 records two red runs:
  a never-held series and a closed port.
- **M2-R10-08 (medium): M2's contract exceptions named no gate and were missing from the
  contracts pending list.** Fixed. Open questions 3 and 5 are gated before PR 4b merges, and
  Open question 1 before D6 merges. Each asks for a row in contracts.md *Requested amendments
  not yet applied*, and a cross-slice note to contracts carries the rows.

### Round integration-2 — cross-slice review

One finding was raised against this slice, and it was fixed. None was refuted.

- **X2-12 (medium): M2 PR 4a's gate requires "M5's text adopts the port", but M5 declined
  and M6 A7 adopted it.** Fixed. Confirmed in the other slices' text. M5 *Open questions* 6
  declines the port. M5's PR 5 retires three push tokens and keeps
  `monitor_bridge_swallowed_verdicts_push_token` until M6's C-7. M6 *Open questions* 10
  adopts the port as batch A7 and says that M2's PR 4a gate reads A7 as the adoption. The
  changes:
    - PR 4a's gate and Open question 6 now require another slice's text to adopt the port,
      and name M6's A7 as that adoption. PR 4a quotes A7's entry when it merges.
    - The opening paragraph, *Who ports the arm*, PR 5, *Risks* and *Complexity delta* name
      M6's A7 as the owner, and M5 no longer appears as the owner.
    - *Who ports the arm* and Open question 6 state that A7's soak starts only after PR 4a
      creates the "Workload Log Errors" tile.
    - Open question 6 corrects the token count to three and M6's row count to 41. It drops
      the request for a contracts.md sub-edge, because the slice index makes M6 depend on all
      of M2.
    - *Rollback* gains an entry: once A7's Cutover-a has merged, PR 4a no longer reverts
      alone, because Cutover-a deletes `checks/logs.py:with_log_errors`, which the restored
      row calls.
    - The same pass corrects "M5's PR 4" to "M5's PR 4a", the PR that deletes the
      `loki_reachable` gate, and states that M5 takes three rows off that gate.
