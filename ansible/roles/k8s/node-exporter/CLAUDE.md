# node-exporter — Prometheus host metrics, both nodes

Prometheus `node_exporter` as a DaemonSet, added at the Phase F drain so daniel-box gains
`node_*` coverage it never had while daniel-server's LAN-published 9100 retires.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=node-exporter` with exit 5
- **Image:** `prom/node-exporter` (`node_exporter_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Runs on:** both k3s nodes (DaemonSet, no `nodeSelector`).
- **`hostNetwork: true`, `hostPID: true`** — required so `node_*` describes the node
  (interfaces, `/proc`) rather than a pod netns; the standard node-exporter posture.
- **Mounts:** `/proc`, `/sys`, `/` (as `/rootfs`), all **read-only** `hostPath` — no write
  access despite `hostNetwork`/`hostPID`.
- **Flux applies it, not Ansible** (D5.7, the third Wave A PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. Its
  image-bump PRs therefore draw no `land-wait` and no `probe.py health` run. To give it back to
  Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects.
- **Where the task went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`,
  and no Flux path runs it. It stays for the handback. Its two vars, `manifests_rollout:
  node-exporter` and `manifests_rollout_kind: daemonset`, named the DaemonSet for the
  stabilisation gate. The Kustomization's `wait` replaces that gate: it reads the DaemonSet
  ready only when every scheduled pod is ready, so a node that goes NotReady mid-apply still
  fails the wait. `k8s_autodeploy` no longer gates an apply, because the deployer skips the
  tag. It still decides whether Renovate automerges the image bump.
- **Renovate may automerge its image bump because** it is stateless with no PVC, a
  readinessProbe gates the rollout, and the image is version-pinned.

## Notable
- **CPU limit is 1 core, not the usual 200m.** node-exporter renders ~320 metric families per
  request; under a tighter limit it was throttled on 42% of CFS periods, and kubelet's probes
  read a bounded prefix of `/metrics` and close early — a slow render meant the probe cut off
  mid-body, logging a `connection reset by peer` line per unwritten family. That flood was 97%
  of all `k8s`-namespace Loki ingest before the CPU limit and probe changes below.
- **Liveness probe interval is 300s, not 30s**, and still hits `/metrics` — the only probe that
  can catch a wedged collector (a dead NFS mount). Readiness instead hits `/` (200, no
  collector work) at the usual 10s, since it only needs to prove the listener is up before
  the Kustomization's `wait` calls the DaemonSet ready.

## The textfile collector directory

The `node-exporter-textfile` hostPath, `/var/lib/node-exporter-textfile` on each node, is
mounted at `/textfile` and read by `--collector.textfile.directory`. A host producer drops a
`*.prom` file there, and Prometheus scrapes it with no change to this role.

- **Three producers.** One is a host timer writing into the hostPath, and the other two are
  sidecars of this DaemonSet, each writing into its own directory.
- **The host producer: `claude_cgroup.prom`.** The `setup/claude_code` role installs
  `ansible/roles/setup/claude_code/files/claude-cgroup-metrics.sh` and
  `claude-cgroup-metrics.timer`, which runs it as root every 30s on both nodes (every host
  with `has_claude_code: true`). The script writes a `mktemp` file in the same directory,
  sets it to 0644 and moves it over `claude_cgroup.prom`, so the collector never reads a
  partial file.
- **monitor-bridge pages on it.**
  `ansible/roles/k8s/monitor-bridge/files/checks/host.py:with_claude_cgroups` folds the
  series into `check_mem`, and
  `ansible/roles/k8s/monitor-bridge/files/verdicts/host_cgroups.py:claude_cgroup_verdict`
  fails when an expected cgroup stops reporting. Arm 3 takes `expected_by_origin`, which
  pairs each host with the cgroups that host must report, so it judges each host on its own
  list. The check fills it from `CLAUDE_CGROUP_EXPECTED` in monitor-bridge's settings
  template, which expects `claude-rc` only on a host that runs the RC unit. A writer that
  stops on one node therefore pages from Kuma while the other node still reports, and so
  does a file that is missing or malformed on one host.
- **The sidecar producer: `iscsi-sessions.prom` (#5826).** The `iscsi-sessions` container
  runs `ansible/roles/k8s/node-exporter/files/iscsi-sessions.sh`, which the template inlines
  so that a script change rolls the DaemonSet. Every 60 s it reads
  `/sys/class/iscsi_session/*` and writes one `homelab_iscsi_session_info` series per
  session into the `node-exporter-iscsi-textfile` emptyDir. The exporter reads that
  directory through a second `--collector.textfile.directory`, a repeatable flag. A sidecar
  reaches both nodes on the next `promote`, where a timer in `setup/k3s` would be a hand
  apply that misses the agent node. observability's `IscsiSessionStale` pages on a session
  that is not `LOGGED_IN` for 15 minutes, and `IscsiSessionsNotReporting` pages on a file
  older than the script's `DEADLINE_S`.
  `ansible/roles/k8s/node-exporter/tests/test_iscsi_sessions.py::test_both_not_reporting_rules_carry_the_scripts_deadline`
  pins the rule literals to it. `docs/longhorn-disaster-recovery.md` has the cleanup.
- **The sidecar producer: `ghcr-mirror-pull.prom` (#5542).** The `ghcr-mirror-pull`
  container runs `ansible/roles/k8s/node-exporter/files/ghcr-mirror-pull.sh`, inlined the same
  way. Every 600 s it GETs nut's manifest through the node's ghcr.io mirror endpoint with
  busybox's wget, and writes the verdict into the `node-exporter-ghcr-mirror-textfile`
  emptyDir. It lives here because this pod is hostNetwork, so the GET reaches the registry's
  fence from the node grant containerd's pulls use, where a pod-network check is refused.
  `NODE_NAME`, from the downward API, picks the endpoint: the loopback hostPort on
  `k8s_registry_node`, the pinned ClusterIP elsewhere. The tag comes from nut's
  `nut-ghcr-mirror-probe` ConfigMap, the one ConfigMap this pod names from another role's
  render. It is mounted as an optional directory, so a new tag arrives without a roll and a
  missing ConfigMap writes a down verdict. The registry role's `CLAUDE.md` names the two alerts.
- **M3's producer metric contract governs every file here**
  (`docs/target-architecture/monitoring/m3-textfile-timer.md`, *The producer metric
  contract*). A *check file* carries a verdict and follows the whole contract. A *gauge set*,
  such as `claude_cgroup.prom`, carries measurements only. It is exempt from the
  `homelab_check_*` series, because the check that reads it owns its absence, and it still
  follows the atomic write and the 0644 mode.
- **Every check file writes the `homelab_check_*` HELP text byte for byte as the others
  do.** node_exporter drops a family whose HELP differs from the one an earlier file gave it,
  and it sets `node_textfile_scrape_error`. A new producer copies the HELP lines from
  `ansible/roles/setup/k3s/templates/disk-health.sh.j2`, and
  `ansible/roles/k8s/node-exporter/tests/test_iscsi_sessions.py::test_every_check_file_producer_writes_the_same_help_text`
  compares them.

## Editing
`templates/daemonset.yaml.j2` (all the tuning history lives in its comments).
