# dri-device-plugin — advertises the AMD render node as a schedulable resource

A `generic-device-plugin` DaemonSet that advertises `/dev/dri/renderD128` to the kubelet as
the extended resource `devic.es/dri`, so jellyfin and tdarr can request GPU transcoding
without either pod running privileged. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/squat/generic-device-plugin` (`dri_device_plugin_image`),
  digest-pinned — validated against the exact VAAPI encode path it advertises.
- **Deploy tag:** `--tags "dri-device-plugin"`. No route — infra role.
- **Namespace: `kube-system`, not the workload namespace.** An extended resource is a node
  property, so every namespace's pods can request it once advertised there.
- **Storage:** none — no PVC, DaemonSet.
- **Auto-deploy: denylisted.** `manifests_rollout: ''` skips the shared rollout gate, and even
  a `/health` readinessProbe would only prove the HTTP listener, not that the plugin
  registered its gRPC socket with the kubelet — a wedge here makes jellyfin and tdarr
  unschedulable with no rollout gate watching for it.

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
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "dri-device-plugin"`.
