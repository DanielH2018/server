# node-exporter — Prometheus host metrics, both nodes

Prometheus `node_exporter` as a DaemonSet, added at the Phase F drain so daniel-box gains
`node_*` coverage it never had while daniel-server's LAN-published 9100 retires.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "node-exporter"`
- **Image:** `prom/node-exporter` (`node_exporter_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **Runs on:** both k3s nodes (DaemonSet, no `nodeSelector`).
- **`hostNetwork: true`, `hostPID: true`** — required so `node_*` describes the node
  (interfaces, `/proc`) rather than a pod netns; the standard node-exporter posture.
- **Mounts:** `/proc`, `/sys`, `/` (as `/rootfs`), all **read-only** `hostPath` — no write
  access despite `hostNetwork`/`hostPID`.
- **Auto-deploy-eligible because** it is stateless with no PVC, a readinessProbe gates the
  rollout, and the image is version-pinned.

## Notable
- **CPU limit is 1 core, not the usual 200m.** node-exporter renders ~320 metric families per
  request; under a tighter limit it was throttled on 42% of CFS periods, and kubelet's probes
  read a bounded prefix of `/metrics` and close early — a slow render meant the probe cut off
  mid-body, logging a `connection reset by peer` line per unwritten family. That flood was 97%
  of all `k8s`-namespace Loki ingest before the CPU limit and probe changes below.
- **Liveness probe interval is 300s, not 30s**, and still hits `/metrics` — the only probe that
  can catch a wedged collector (a dead NFS mount). Readiness instead hits `/` (200, no
  collector work) at the usual 10s, since it only needs to prove the listener is up before
  `rollout status` calls the DaemonSet ready.

## The textfile collector directory

The `node-exporter-textfile` hostPath, `/var/lib/node-exporter-textfile` on each node, is
mounted at `/textfile` and read by `--collector.textfile.directory`. A host producer drops a
`*.prom` file there, and Prometheus scrapes it with no change to this role.

- **One live producer: `claude_cgroup.prom`.** The `setup/claude_code` role installs
  `ansible/roles/setup/claude_code/files/claude-cgroup-metrics.sh` and
  `claude-cgroup-metrics.timer`, which runs it as root every 30s on both nodes (every host
  with `has_claude_code: true`). The script writes a `mktemp` file in the same directory,
  sets it to 0644 and moves it over `claude_cgroup.prom`, so the collector never reads a
  partial file.
- **monitor-bridge pages on it.**
  `ansible/roles/k8s/monitor-bridge/files/checks/host.py:with_claude_cgroups` folds the
  series into `check_mem`, and
  `ansible/roles/k8s/monitor-bridge/files/verdicts/host_cgroups.py:claude_cgroup_verdict`
  fails when an expected cgroup stops reporting. A missing or malformed `claude_cgroup.prom`
  therefore pages from Kuma.
- **M3's producer metric contract governs every file here**
  (`docs/target-architecture/monitoring/m3-textfile-timer.md`, *The producer metric
  contract*). A *check file* carries a verdict and follows the whole contract. A *gauge set*,
  such as `claude_cgroup.prom`, carries measurements only. It is exempt from the
  `homelab_check_*` series, because the check that reads it owns its absence, and it still
  follows the atomic write and the 0644 mode.

## Editing
`templates/daemonset.yaml.j2` (all the tuning history lives in its comments).
