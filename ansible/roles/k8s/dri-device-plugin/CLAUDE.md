# dri-device-plugin — advertises the AMD render node as a schedulable resource

A `generic-device-plugin` DaemonSet that advertises `/dev/dri/renderD128` to the kubelet as
the extended resource `devic.es/dri`, so jellyfin and tdarr can request GPU transcoding
without either pod running privileged. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`platform: k8s`); `land.sh --tags=dri-device-plugin`
  gates the rollout
- **Images:** `ghcr.io/squat/generic-device-plugin` (`dri_device_plugin_k8s_image`), `python`
  (`dri_device_plugin_reaper_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Digest-pinned image** — validated against the exact VAAPI encode path it advertises.
- **Namespace: `kube-system`, not the workload namespace.** An extended resource is a node
  property, so every namespace's pods can request it once advertised there.
- **A DaemonSet**, one pod per node.
- **Flux applies it, not Ansible** (Wave D). Its `containers_list` entry is
  `platform: k8s`, so `deploy.sh` refuses it and the GitOps deployer skips it. A merged change
  reaches the cluster when `promote` publishes and verifies the next bundle.
- **The verify is an alert, not a Job.** The soak reads `K8sExtendedResourceMissing`
  (`verify="true"` in `ansible/roles/k8s/observability/files/rules/k8s-workloads.yaml`), which
  fires when no node reports allocatable `devic.es/dri` above 0. It replaces the rollout wait and
  the `devic.es/dri` readback. A running DaemonSet is not the property that
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
- **The `addon-job-reaper` CronJob deletes every Failed Job pod in `kube-system` and
  `longhorn-system`** (#6460). It runs the same script from `kube-system`.
  Those namespaces run Jobs this repo does not render, k3s's helm-install Jobs and Longhorn's
  backup Jobs, so no `ttlSecondsAfterFinished` ages a run the reboot interrupts. The Job object
  keeps the failure on record and Loki keeps the output; the rule is
  `ansible/roles/k8s/dri-device-plugin/files/dri_admission_reaper.py:is_failed_job_pod`.
  `homelab` keeps its Job pods, because the health readers read their logs. The rule also
  takes the failed pods of the one rendered CronJob in those namespaces, `longhorn-reconcile`;
  its `failedJobsHistoryLimit` keeps the failed Jobs, and Loki keeps their output. Each listed
  namespace (`dri_device_plugin_reaper_job_namespaces`) gets a Role and a RoleBinding to the
  `addon-job-reaper` ServiceAccount in `kube-system`. That grant reaches every pod in both
  namespaces, so it has its own account in `kube-system`: a pod in `homelab` cannot run as it,
  and the `homelab` reaper's account holds no grant outside `homelab`. `system-upgrade` is not listed: the SUC controller's
  `SYSTEM_UPGRADE_JOB_TTL_SECONDS_AFTER_FINISH` of 900 ages its Plan Jobs out. The sweep stays
  in this role rather than a role of its own because it runs the same script; a second role
  would add a `containers_list` entry and an image pin for one more predicate.

## Editing
- Manifests: `templates/daemonset.yaml.j2`, and the reaper's
  `templates/admission-reaper-cronjob.yaml.j2` and `templates/admission-reaper-rbac.yaml.j2`.
