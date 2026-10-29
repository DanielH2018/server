# dri-device-plugin — advertises the AMD render node as a schedulable resource

A `generic-device-plugin` DaemonSet that advertises `/dev/dri/renderD128` to the kubelet as
the extended resource `devic.es/dri`, so jellyfin and tdarr can request GPU transcoding
without either pod running privileged. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=dri-device-plugin` with exit 5
- **Images:** `ghcr.io/squat/generic-device-plugin` (`dri_device_plugin_k8s_image`), `python`
  (`dri_device_plugin_reaper_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Digest-pinned image** — validated against the exact VAAPI encode path it advertises.
- **Namespace: `kube-system`, not the workload namespace.** An extended resource is a node
  property, so every namespace's pods can request it once advertised there.
- **A DaemonSet**, one pod per node.
- **Flux applies it, not Ansible** (Wave D). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it. A merged change
  reaches the cluster when `promote` publishes and verifies the next bundle. `tasks/main.yml`
  holds the include of `k8s/manifests` alone, which the renderer reads for `manifests_*`. To give
  the role back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain
  revert of the flag leaves two writers on the objects.
- **The verify is an alert, not a Job.** The soak reads `K8sExtendedResourceMissing`
  (`verify="true"` in `ansible/roles/k8s/observability/files/rules/k8s-workloads.yaml`), which
  fires when no node reports allocatable `devic.es/dri` above 0. It replaces the rollout wait and
  the `devic.es/dri` readback `tasks/main.yml` ran. A running DaemonSet is not the property that
  matters: the plugin can be healthy while jellyfin and tdarr sit Pending for a resource nothing
  offers, and the alert reads that resource.

## Notable
- **The one privileged workload in the media stack**, on purpose: it needs privilege to
  register the gRPC socket and hand out device file descriptors, so none of the nine media
  workloads that consume `devic.es/dri` has to be.
- `dri_device_plugin_count: 4` is a scheduling allowance, not a hardware fact — it's one
  physical render node, multiplexed across however many pods request it concurrently.
- `priorityClassName: system-node-critical` — this must not be the pod evicted to make room
  for jellyfin or tdarr, which is exactly backwards from what losing it does to them.
- **The `dri-admission-reaper` CronJob deletes the pods a reboot rejects** (#6367). After a
  reboot the kubelet can re-admit a GPU pod before this plugin re-registers. The pod ends
  `Failed` with `UnexpectedAdmissionError`, its ReplicaSet replaces it, and nothing else
  deletes it, because the controller-manager's `terminated-pod-gc-threshold` is 12500 pods.
  Every 15 minutes the job deletes each `Failed` pod a ReplicaSet owns whose reason is
  `UnexpectedAdmissionError` or `OutOf<resource>`, and nothing else; the rule is
  `ansible/roles/k8s/dri-device-plugin/files/dri_admission_reaper.py:is_tombstone`. It runs
  in `homelab`, not `kube-system`, because a Role grants only in its own namespace. Its Role
  grants `list` and `delete` on every pod there: RBAC cannot name pods it has not seen.
  Lowering the GC threshold instead was rejected, because PodGC also deletes the Job pods
  whose logs the health readers read.
- **The same job deletes every Failed Job pod in `kube-system` and `longhorn-system`** (#6460).
  Those namespaces run Jobs this repo does not render, k3s's helm-install Jobs and Longhorn's
  backup Jobs, so no `ttlSecondsAfterFinished` ages a run the reboot interrupts. The Job object
  keeps the failure on record and Loki keeps the output; the rule is
  `ansible/roles/k8s/dri-device-plugin/files/dri_admission_reaper.py:is_failed_job_pod`.
  `homelab` keeps its Job pods, because the health readers read their logs. The rule also
  takes the failed pods of the one rendered CronJob in those namespaces, `longhorn-reconcile`;
  its `failedJobsHistoryLimit` keeps the failed Jobs, and Loki keeps their output. Each listed
  namespace (`dri_device_plugin_reaper_job_namespaces`) gets a Role and a RoleBinding to the
  ServiceAccount in `homelab`. `system-upgrade` is not listed: the SUC controller's
  `SYSTEM_UPGRADE_JOB_TTL_SECONDS_AFTER_FINISH` of 900 ages its Plan Jobs out. The sweep rides
  this reaper rather than a role of its own because it is the same list-and-delete job; a
  second role would add a `containers_list` entry, a ServiceAccount and an image pin for one
  more predicate.
- **A handback to Ansible must defer the reaper's documents first.** The role's Ansible path
  arms its prune with `manifests_prune_namespace: kube-system`, and an armed apply refuses a
  document in another namespace. The reaper's objects sit in `homelab` and in each add-on
  namespace, and `longhorn-system` is not `kube-system`, so a handback lists `admission-reaper-cronjob.yaml` and `admission-reaper-rbac.yaml` in
  `manifests_deferred_files`, as netpol-baseline does. Flux applies a role's documents in the
  namespaces they name, so the Flux render needs neither.

## Editing
- Manifests: `templates/daemonset.yaml.j2`, and the reaper's
  `templates/admission-reaper-cronjob.yaml.j2` and `templates/admission-reaper-rbac.yaml.j2`.
