# node-exporter — Prometheus host metrics, both nodes

Prometheus `node_exporter` as a DaemonSet, added at the Phase F drain so daniel-box gains
`node_*` coverage it never had while daniel-server's LAN-published 9100 retires.

## At a glance
- **Image:** `node_exporter_k8s_image` (Renovate-tracked pin).
- **Runs on:** both k3s nodes (DaemonSet, no `nodeSelector`).
- **`hostNetwork: true`, `hostPID: true`** — required so `node_*` describes the node
  (interfaces, `/proc`) rather than a pod netns; the standard node-exporter posture.
- **Mounts:** `/proc`, `/sys`, `/` (as `/rootfs`), all **read-only** `hostPath` — no write
  access despite `hostNetwork`/`hostPID`.
- **Deploy tag:** `--tags "node-exporter"`. `k8s_autodeploy: true` — stateless, no PVC,
  readinessProbe gates the rollout, image version-pinned.

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
- **`node-exporter-textfile` hostPath is an empty hook**, not yet used — a spot for a future
  host cron to drop `*.prom` gauges without a role change.

## Editing
`templates/daemonset.yaml.j2` (all the tuning history lives in its comments). Deploy:
`uv run ansible-playbook ansible/deploy.yml --tags "node-exporter"`.
