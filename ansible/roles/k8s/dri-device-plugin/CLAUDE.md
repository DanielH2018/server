# dri-device-plugin — advertises the AMD render node as a schedulable resource

A `generic-device-plugin` DaemonSet that advertises `/dev/dri/renderD128` to the kubelet as
the extended resource `devic.es/dri`, so jellyfin and tdarr can request GPU transcoding
without either pod running privileged. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=dri-device-plugin` with exit 5
- **Image:** `ghcr.io/squat/generic-device-plugin` (`dri_device_plugin_k8s_image`)
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

## Editing
- Manifest: `templates/daemonset.yaml.j2`.
