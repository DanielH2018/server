# H6 — system-upgrade-controller for k3s

Status: PLANNED
Depends on: D2, H1.1a to H1.8, H4, M1 (the contracts.md row; *Open questions* 2 says why
each edge exists).
Unblocks: H7

This slice hands the k3s binary to system-upgrade-controller (SUC) and the k3s configuration
to the host packages. Flux reconciles the controller and two Plans, and SUC upgrades the
nodes after an in-cluster gate passes. Each node's package owns a `config.yaml.d` drop-in,
and a windowed unit applies a config change with one gated restart. Names and paths follow
H3's *Shared names for the host slices*, and secrets follow H4. The slice moves no join
token; *Open questions* 4 says why.

## Goal and exit criterion

Afterwards, no Ansible task upgrades or reconfigures k3s on a running node. The pin
`k3s_version` lives in the Flux-owned `system-upgrade` role and drives the SUC Plans.
`homelab-daniel-box` and `homelab-daniel-server` own the k3s flags and the files the flags
name. `homelab-k3s-restart` applies a config change inside a window. Ansible keeps only the
fresh-host install until H7.

The exit criterion is three observations, in order:

1. **A no-op pin.** With both Plans selecting their node at the running version, each
   node's `plan.upgrade.cattle.io/<plan>` label equals its Plan's `status.latestHash`.
   `kubectl get nodes` shows the version unchanged, and neither k3s unit's
   `ExecMainStartTimestamp` moves. The Plan's `Complete` is not the evidence here, because
   a selector-only change can leave it stale (*The Plans' outcome reaches
   `flux/system-upgrade`*).
2. **A patch bump with no hand apply.** The operator lands a Renovate PR that raises
   `k3s_version` by one patch release, through `land.sh`. Nothing else is run by hand.
    - Both nodes report the new version.
    - Both k3s units' `ExecMainStartTimestamp` values differ from the ones recorded before
      the merge. daniel-box's new `k3s` value is earlier than daniel-server's new
      `k3s-agent` value. The agent Job's `prepare` finished after the server Job's
      `completionTime`. Those two orderings are the server-first property. daniel-box's
      `Ready` transition is not evidence, because a k3s restart never moves it (Baseline,
      *What moves when k3s restarts*).
    - `flux/system-upgrade` reads `success` on a revision R that equals the bump commit or
      descends from it, and `last-good` covers R (C6's coverage). Another PR can merge
      before promotion, so the status may sit on a later commit than the bump. The deployer
      wrote no `manual_plane` line for `k3s`.
3. **A harmless flag change.** It appends one member to daniel-box's `kubelet-arg` list,
   and `k3s.service` restarts exactly once, inside the window.

## Baseline at `70591c307`

**The pin and its readers.**

- `k3s_version` (`v1.37.1+k3s1`), `k3s_install_script_url` and `k3s_install_script_sha256`
  live in `ansible/roles/setup/k3s/defaults/main.yml`. `git grep -n k3s_version` finds no
  other definition. Role defaults are role-scoped, so a template under
  `ansible/roles/k8s/` cannot read that pin.
- Other readers of the file or the key:
    - `scripts/lib/repo_paths.py:K3S_DEFAULTS`. `scripts/validate/refresh_vendored_schemas.py`
      (`kubernetes_tag`) and
      `scripts/validate/tests/test_k8s_schema.py::test_vendored_core_schemas_match_the_cluster`
      read `k3s_version` through it, and the first one's error string names the old path.
      `git grep K3S_DEFAULTS` finds at least nine more importers that read keys which stay
      in `setup/k3s`. Two of them are `ansible/tests/k8s/_manifest_guards.py`
      (`K3S_DEFAULT_VALUES`) and `ansible/tests/k8s/test_k8s_manifests_metallb.py`
      (`k3s_metallb_pool`);
    - `ansible/tests/setup/test_k3s_node_ip_pinned.py::test_k3s_version_is_pinned`, which
      reads the pin through `load_defaults(K3S_ROLE)`, not through the constant;
    - `ansible/tests/setup/test_k3s_version_guard.py`;
    - `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_remediation.py:_K3S_CONTROL_PLANE_GATES`;
    - `scripts/validate/asset_pins.py`, which renders the installer URL from keys in the same
      file;
    - `renovate.json`'s k3s rules, whose text names the file.
- **A pin change is a hand apply.** k3s routes through `k3s-bringup.yml`, so
  `deploy_setup_roles.py:tick_applies_setup_role` is false for it. The comment in
  `scripts/deploy_tools/land_lib/land_tags.py` says "k3s lives in k3s-bringup.yml … the tick
  defers". `docs/k3s-upgrade.md` (*After merging the pin*) says the tick writes a
  `manual_plane` line that pages six hours later. That section cites
  `_SETUP_ROLES_OUTSIDE_INITIAL_SETUP`, which no longer exists in the tree.

**The install tasks.**

- `ansible/roles/setup/k3s/tasks/server.yml` (task `Install or reconfigure the k3s server`)
  passes `INSTALL_K3S_EXEC: "{{ k3s_server_args }}"` and `INSTALL_K3S_VERSION` to
  `/usr/local/bin/k3s-install.sh`. `agent.yml` does the same with `k3s_agent_args`,
  `K3S_URL` and `K3S_TOKEN`.
- Each task re-runs the installer when an argument is missing or the version differs. The
  task's own comment explains why a `creates:` guard is wrong: a change to `k3s_server_args`
  would then never reach an installed host.

**The flags.**

- `k3s_server_args` carries:
    - `--cluster-init` and `--disable=traefik`, `servicelb`, `local-storage`;
    - `--write-kubeconfig-mode 0640`;
    - `--node-ip {{ server_ip }}` and `--advertise-address {{ server_ip }}`;
    - `--kubelet-arg=allowed-unsafe-sysctls=…`;
    - `--secrets-encryption` and `--etcd-expose-metrics`, each behind its own variable;
    - five `--kube-apiserver-arg=audit-*` flags behind `k3s_audit_log_enabled`;
    - `--kube-apiserver-arg=authentication-config={{ k3s_authentication_config_path }}`.
- `k3s_agent_args` carries `--node-ip`, one `--node-label` and the same `--kubelet-arg`.
- The installer writes these into `ExecStart` of the unit under `/etc/systemd/system/`.
- The comment above `k3s_server_args` records the etcd peer-URL trap. Changing `--node-ip`
  or `--advertise-address` on a live cluster leaves k3s retrying forever, and `systemctl
  restart k3s` hangs because the unit is `Type=notify` with `TimeoutStartSec=0`.
  `systemctl show k3s` on daniel-box reads `Type=notify`, `Restart=always` and
  `TimeoutStartUSec=infinity`.
- `/etc/rancher/k3s/` on daniel-box holds no `config.yaml` and no `config.yaml.d/`.

**The files the flags name, and who writes them.**

- `server.yml` writes:
    - the audit policy at `k3s_audit_policy_path` (0600) and its log directory (0700);
    - the authentication config at `k3s_authentication_config_path` (0600);
    - `/etc/rancher/k3s/registries.yaml` (0600).
- The task `Write the apiserver authentication configuration` says that a missing file
  "wedges the node rather than failing the play".
- `agent.yml` writes `/etc/rancher/k3s/registries.yaml` (0644) after `Create the k3s config
  directory`, and restarts the agent when it changes.
- `coredns.yml` writes the `coredns.yaml.skip` marker. H1 leaves that task in `setup/k3s`
  for H6 to move.
- `unit-logging.yml` is imported by both `server.yml` and `agent.yml` (`k3s_log_unit`). It
  writes four things:
    - `k3s_log_file`, touched root:adm 0640;
    - the `.service.d` directory under `/etc/systemd/system/`;
    - `10-log-to-file.conf`, which registers `k3s_unit_logging_dropin`;
    - `/etc/logrotate.d/{{ k3s_log_unit }}`, with `copytruncate` and `maxsize 50M`.
- Both install files have a task `Restart … so the log drop-in takes effect` whose `when:`
  reads `k3s_unit_logging_dropin`.

**The join token and its second reader.**

- `ansible/k3s-bringup.yml`'s play `Read the join credentials on the server node` slurps
  daniel-box's `/var/lib/rancher/k3s/server/node-token`.
- `docs/k3s-etcd-restore.md` says k3s writes the same value to `token` and `node-token`
  when no `--agent-token` is set. `k3s_server_args` sets none, so the agent joins with the
  server token.
- `docs/break-glass.md` item 2 keeps that token out of band. The k3s role's
  `CLAUDE.md` contract says the off-box snapshot "never [uploads] the cluster token".
- The installer writes `K3S_TOKEN` into `/etc/systemd/system/k3s-agent.service.env`.
  `ansible/roles/setup/hypervisor/templates/etcd-restore-drill-vm.sh.j2` (`AGENT_ENV`) reads
  it there for the full etcd restore drill.

**Credentials for reading the cluster.**

- `ansible/roles/setup/k3s/tasks/kubeconfig.yml` is imported only by `tasks/main.yml`, the
  server entry. It reads the read-only ServiceAccount token live from the cluster.
- `k3s_readonly_kubeconfig_server` is `https://127.0.0.1:6443`, and `agent_verify.yml`
  opens with "Runs ON daniel-box (the kubectl credential lives here)". daniel-server has no
  read-only kubeconfig.
- `ansible/roles/setup/k3s/templates/readonly-rbac.yaml.j2` defines the read-only
  account's grants: the built-in `view` ClusterRole plus one additive ClusterRole. Neither
  grants `leases` in `coordination.k8s.io`. `kubectl auth can-i get leases -n kube-system`
  prints `no`, and `kubectl auth can-i list events -n default` prints `yes`.
- The additive ClusterRole grants read on every API group in
  `ansible/roles/setup/k3s/defaults/main.yml:k3s_readonly_crd_api_groups`. That list names
  no `upgrade.cattle.io`, so no grant covers SUC's Plans. A `kubectl auth can-i` check
  cannot show that yet: on 2026-10-11 it printed `no` beside a warning that the resource
  type does not exist, and it prints the same with or without a grant until the CRD is
  installed.
- **These grants move before H6 runs.** H1.2 moves `readonly-rbac.yaml.j2` and
  `k3s_readonly_crd_api_groups` into the Flux-owned `cluster-base` role by git mv (H1, the
  tag table), and `kubeconfig.yml` keeps only its host half. `cluster-base` renders
  `prune: false` (H1, *Plumbing tags are never pruned*). The list gains no Flux group: the
  read-only account reads the Flux kinds through `view`, into which the vendored
  `flux-view-flux-system` aggregates (D1 *Baseline*, corrected on 2026-10-12). So every
  grant this slice gives the read-only account lands in `cluster-base`, through promote.

**What moves when k3s restarts.** Measured on daniel-box on 2026-10-11, read-only.

- `uptime -s` printed `2026-10-11 07:39:50`, and `systemctl show k3s` printed
  `ExecMainStartTimestamp=Sun 2026-10-11 07:40:12 UTC`.
- `kubectl get nodes` printed daniel-box's `Ready` `lastTransitionTime` as
  `2026-08-01T23:41:59Z`, with a `lastHeartbeatTime` under a minute old. A reboot and a
  k3s start left the `Ready` transition unchanged. The node controller runs inside the
  same k3s process, so the server never sees its own node go NotReady. The `Ready`
  transition is therefore no evidence of a restart, and a fresh `lastHeartbeatTime` is.
- k3s runs kube-controller-manager with leader election unless `NoLeaderElect` is set
  (`pkg/daemons/control/server.go`, `controllerManager`). No flag in `k3s_server_args`
  sets it. kube-controller-manager builds its holder identity from the hostname and a
  fresh UUID per process (`cmd/kube-controller-manager/app/controllermanager.go`).
  client-go's `LeaderElector.tryAcquireOrRenew` writes a new `acquireTime` whenever a
  different holder takes the Lease. The `kube-system/kube-controller-manager` Lease's
  `acquireTime` should therefore move on every k3s server start.
- The operator measured that last step on daniel-box on 2026-10-24, with `sudo k3s kubectl`,
  because the read-only account cannot read Leases (above) and an agent cannot use `sudo`.
  The Lease read `daniel-box_adffef0e-d846-4ae6-b892-f68861a63f49`, `acquireTime`
  `2026-10-18T14:21:11.955974Z`, `renewTime` `2026-10-24T16:12:56.623907Z` and
  `leaseDurationSeconds` 15. `systemctl show k3s` read `ExecMainStartTimestamp=Sun
  2026-10-18 14:20:47 UTC`. The `acquireTime` is 25 seconds after the k3s start, so the
  Lease does not predate the restart and tracks it. The 25-second gap is far below the
  guard's 15-minute bound, so the guard refuses for the full 15 minutes after a restart.
- Events would show a kubelet `Starting` event per start, but k3s keeps events for an hour
  and none were listed. An absent event would also read as "no restart", which is failure
  class 3. This plan does not use events.

**kube-state-metrics collects no Jobs.**
`ansible/roles/k8s/observability/templates/kube-state-metrics.yaml.j2` runs it with
`--resources=pods,deployments,statefulsets,daemonsets,nodes`. The comment above its
ClusterRole requires the flag and the grant to change together. No `kube_job_*` series
exists. M2 plans to add `replicasets` to the same flag, and M6's B7 plans to add
`cronjobs,jobs`.

**The deploy lock and the hold, as a root reader finds them.** Measured on daniel-box on
2026-10-11.

- `sysctl fs.protected_regular` prints `2`. `/var/lock` is a symlink to `/run/lock`, which
  is `drwxrwxrwt root root` on tmpfs, so it is empty after every boot.
- `/run/lock/server-deploy-all.lock` is `-rw-r--r-- ubuntu ubuntu`.
  `deploy_locks.py:_take` opens every lock with `O_WRONLY | O_CREAT`, mode `0o666`, as the
  deployer's user.
- Under `protected_regular=2`, an `O_CREAT` open by root of that ubuntu-owned file fails
  with EACCES. A file that root creates there is root-owned `0644`, and the deployer's
  `O_WRONLY` open of it then fails. A root reader of the lock must therefore never pass
  `O_CREAT`. An `O_RDONLY` open is outside the rule, and `flock` `LOCK_EX` works on it.
- `/var/lib/gitops-deploy` is `drwxr-x---+ ubuntu ubuntu`. `hold_sha` is absent while no
  hold is set, which was its state when measured.

**The runbook and its gates.**

- `docs/k3s-upgrade.md` upgrades the server, verifies it, then upgrades the agent. Its
  *Rollback* section says a same-minor downgrade works "exactly like an upgrade" through the
  installer's version guard, and a cross-minor one needs `docs/k3s-etcd-restore.md`.
- `scripts/deploy_tools/runbook_gates.py k3s-upgrade` runs four gates:
    - `scripts/deploy_tools/runbook_gates_lib/gate_runner.py:unsafe_volumes` (no degraded or
      faulted volume);
    - `k3s_upgrade.py:in_flight_backups`;
    - `k3s_upgrade.py:held_sha` (the deployer's hold);
    - `k3s_upgrade.py:nodes_not_ready`.
- `scripts/deploy_tools/runbook_gates_lib/k3s_upgrade.py` imports `gitops_hold`,
  `gitops_markers`, `longhorn_lib` and `lib.kubectl`, so it cannot ship to a host as is.
- The land policy in `scripts/deploy_tools/land_lib/policy.py` is inert for interactive and
  Renovate landings. Its docstring says "A landing that sets neither runs exactly as before,
  which is every interactive session and renovate-agent's lander". `merge.py` calls
  `policy.check` only under `ln.opts.policy_active`.

**Schedules near midday.** These read the API server or the host.

- The k3s role (`## At a glance`): `Longhorn backup health` and `daniel-box disk health` at
  `*/10`, and `Release staleness drift check` at `*/30`.
- The initial_setup role: `Refresh homelab infrastructure map` at `*/15`, and
  `kuma-check-loki-read-route.timer` hourly at `:23`.
- The hypervisor role on daniel-server: `Full etcd restore drill in a throwaway guest` at
  `20 11 1 * *`.
- `gitops-deploy.timer` runs every 10 minutes from boot, so it has no fixed minute.
  The claude_code role's `## At a glance` lists four more monotonic timers, on the nodes
  where that role enables them: `claude-cgroup-metrics.timer` every 30 seconds, and
  `claude-clone-sync.timer`, `claude-dotfiles-sync.timer` and `claude-memory-sync.timer`
  every 15 minutes.
  `systemctl show gitops-deploy.timer` printed `AccuracyUSec=1min`, the systemd default
  for a timer that sets none, and `RandomizedDelayUSec=2min`. A timer fire is therefore
  an interval, not an instant.
- In the cluster, `kubectl get cronjobs -A` lists `homelab/kuma-status-page-sync` at
  `*/15` (`uptime_kuma_k8s_status_page_sync_schedule`), `homelab/kuma-maintenance-sync` at
  `20 * * * *`. The rest fire at fixed hours outside 11:00 to 12:00:
  `homelab/pi-peer-backup`, `homelab/configarr`, and the CronJobs Longhorn generates from
  its RecurringJobs (`longhorn-recurringjob.yaml.j2`), at `30 3 * * *` and
  `30 4 * * <d>`. `date` on daniel-box prints UTC.
- `k3s_health_cron_boot_grace_s` covers a boot, not a k3s restart.

**No stage cluster.** D2's Baseline records that `daniel-stage` was retired on 2026-09-29
(#2941). `scripts/lib/kubectl.py:RETIRED_CLUSTER_NODES` keeps it only so that a request
refuses. Production is the only standing cluster, so every live step here runs on it. A
throwaway k3s node is still available: the etcd restore drill's transient libvirt guest on
daniel-server, which H7's bring-up drill also uses. *Rehearsal in a drill guest* runs the
SUC Plan and the Flux apply there first.

**Upstream behaviour.** Read from `rancher/system-upgrade-controller` (release v0.20.2 is the
newest, 2026-09-23) and `k3s-io/k3s-upgrade` at their default branches on 2026-10-11, with
`gh api`.

- SUC's `pkg/upgrade/job/job.go` appends `prepare` to the Job's `InitContainers`, before
  `cordon` or `drain`. A Plan's Job therefore starts as soon as the Plan resolves, and the
  pod waits in `prepare`.
- The same file sets `defaultActiveDeadlineSeconds` to 600 and
  `defaultTTLSecondsAfterFinished` to 900. The controller reads
  `SYSTEM_UPGRADE_JOB_ACTIVE_DEADLINE_SECONDS`.
- `pkg/upgrade/handle_batch.go` clears `Unschedulable` only in the `ConditionComplete`
  branch. A failed Job leaves a cordoned node cordoned, and sets the Plan's `Complete`
  condition with reason `JobFailed`.
- The Plan API (`pkg/apis/upgrade.cattle.io/v1/types.go`) has a `window`, a `Complete`
  condition and `.status.applying`. A `VolumeSpec` is a host path only.
- The k3s-upgrade image's `upgrade.sh` has a `replace_binary` step. It exits 0 when
  `/opt/k3s` and the installed binary have the same sha256. It exits 1 when the installed
  version or build date is newer.
- The k3s-upgrade `Dockerfile` downloads the release's `k3s` asset and checks it against the
  release's `sha256sum-amd64.txt`.
- On daniel-box, `sha256sum /usr/local/bin/k3s` prints `92b94582…b2c0`, the same digest
  that `gh release download v1.37.1+k3s1 -R k3s-io/k3s -p sha256sum-amd64.txt` lists for
  `k3s`. A Plan at the running version is therefore a byte-identical no-op on daniel-box.
  daniel-server is unmeasured.
- The k3s token documentation (`docs.k3s.io/cli/token`) says the agent token "can be set
  before or after the cluster has been started", and that the server token "can be used to
  join both server and agent nodes".
- Flux's Kustomization documentation evaluates `healthCheckExprs` in the order
  `inProgress`, `failed`, `current`, and the first true expression decides.
- `fluxcd/pkg` `runtime/cel/status_evaluator.go` `StatusEvaluator.Evaluate` returns
  InProgress on a generation mismatch only when `status.observedGeneration` exists. When no
  expression is true, it also returns InProgress.
- SUC v0.20.2's `PlanStatus` carries `conditions`, `latestVersion`, `latestHash` and
  `applying`, and no `observedGeneration`. So Flux cannot tell a stale status from a current
  one by itself.
- `pkg/upgrade/handle_upgrade.go` creates `Complete` as Unknown on a new Plan. It sets
  `Complete` False with reason `Resolved` only when SUC reconciles and finds `latestVersion`
  differs from the resolved version. A bumped Plan therefore keeps the previous run's
  `Complete=True` until SUC reconciles it. A Plan that selects no node reads `Complete`
  True.
- `pkg/upgrade/plan/plan.go` `MungeVersion` replaces `+` with `-`. For `spec.version:
  v1.37.1+k3s1`, `status.latestVersion` reads `v1.37.1-k3s1`.
- `plan.go` `DigestStatus` hashes `latestVersion` and `serviceAccountName`, among other
  fields, into `latestHash`. The node selector `plan.go` builds excludes a node whose
  `plan.upgrade.cattle.io/<plan>` label equals `latestHash`, and that label is how a node
  records a completed run. A change to the selector alone keeps the hash.
- `job.go` sets the pod's `serviceAccountName` from the Plan, `BackoffLimit` to 2 and
  `RestartPolicy` to `Never`. A `prepare` that exits non-zero therefore ends in `JobFailed`
  after three pods.
- The Plan field `jobActiveDeadlineSecs` sets one Plan's Job deadline. The controller caps
  it at `SYSTEM_UPGRADE_JOB_ACTIVE_DEADLINE_SECONDS_MAX` (`job.go`,
  `ActiveDeadlineSecondsMax`).
- `pkg/upgrade/container/container.go` mounts the host's root at `/host` in every
  container, `prepare` included.
- SUC's `manifests/clusterrolebinding.yaml` binds ServiceAccount `system-upgrade` to two
  ClusterRoles. `manifests/clusterrole.yaml` grants them nothing in `longhorn.io`.

## Design

### Fixed premises

k3s and SUC are fixed premises of this slice. [design.md](../design.md) names k3s and Flux as
fixed premises of the migration, and its *Host plane* section chooses SUC to upgrade k3s.
This slice weighs no other Kubernetes distribution and no other k3s upgrade mechanism. Every
gate, window and check below assumes SUC's Job model and k3s's single binary.

### SUC through Flux

A k8s role, `system-upgrade`, is born Flux-owned. Its `containers_list` entry carries
`owner: flux` from its first commit, so no Ansible path ever reaches it, as C4 requires.

- **The pin moves here.** `k3s_version`, `k3s_install_script_url` and
  `k3s_install_script_sha256` move into `ansible/roles/k8s/system-upgrade/defaults/main.yml`,
  with the Renovate annotation and the comments. A pin change then routes to a Flux-owned
  tag. `land.sh` waits on promotion for it (D2's `promote.py land-wait`), and the tick
  writes no `manual_plane` line. The setup role's fresh-host install reads the same file
  with `include_vars`.
- **Vendored SUC.** The controller manifest and the CRD at
  `system_upgrade_controller_version` live under `files/upstream/`, refreshed by H1's
  `scripts/dev/vendor_manifest.py` against a pinned sha256. C1 forbids live reads, so the
  renderer cannot fetch them. A kustomize patch sets
  `SYSTEM_UPGRADE_JOB_ACTIVE_DEADLINE_SECONDS=3600` and
  `SYSTEM_UPGRADE_JOB_ACTIVE_DEADLINE_SECONDS_MAX=5400` in the controller's env ConfigMap.
- **Images by digest.** The vendored manifest names the controller image by tag. A
  kustomize `images:` entry pins it by digest instead, from
  `system_upgrade_controller_image` in the role's defaults, with a Renovate annotation. A
  render test asserts that every container image in the rendered `system-upgrade`
  directory carries `@sha256:`, the gate image and the verify Job's image included. The
  Plans' `upgrade.image` is outside that test, because SUC builds that reference from the
  Plan's `version` (*Open questions* 12).
- **Default-deny ingress.** The role ships one NetworkPolicy, `default-deny-ingress`, with
  `podSelector: {}`, `policyTypes: [Ingress]` and no ingress rule. [ADR-0009](../../adr/0009-networkpolicy-default-deny-ingress.md) sets that rule
  for `homelab` and `observability`, and this namespace takes it from its first commit. No
  caller connects into `system-upgrade`: the controller and the Plan Jobs call the API
  server, and nothing calls them. Egress stays open, because the cluster does not enforce
  egress (ADR-0009). A render test asserts the policy, with a red fixture that drops it.
  Whether a netpol-baseline probe row can prove it live depends on whether the controller
  listens on a port, which H6.1b reads from the vendored manifest (*Open questions* 13).
- **Forward-only.** The role sets `flux_forward_only: true` in its defaults, H1's flag
  (*the forward-only hold*). A failed or timed-out revision that changes this tag holds
  without a reset, so `promote` never lowers the Plans below a version a node already
  runs. The flag does not make `k3s_version` one of H1's gated pins: H1's
  `homelab/upgrade-gates` rule names `k3s_longhorn_version` and `k3s_metallb_version`, and
  this slice's gates run in `prepare`.
- **The gate's ServiceAccount.** The role ships ServiceAccount `k3s-plan` in
  `system-upgrade`, and both Plans name it in `serviceAccountName`. SUC's own
  `system-upgrade` account cannot read Longhorn (Baseline). Two bindings grant only what
  the gates read:
    - ClusterRole `k3s-plan-gate`, bound by a ClusterRoleBinding, grants `get` and `list` on
      `nodes`, `volumes.longhorn.io`, `backups.longhorn.io` and `plans.upgrade.cattle.io`;
    - Role `k3s-plan-kube-system` in `kube-system`, bound by a RoleBinding, grants `get` on
      the Deployment `coredns` and on the Lease `kube-controller-manager`, each by
      `resourceNames`. The Lease is the server-restart signal (*Gates at apply time*).
    - The account lands in H6.1b, while no node is selected. `DigestStatus` hashes
      `serviceAccountName`, so a later change would re-run the Jobs on every selected node.
      The Role's name and rules are not hashed.
- **Prune exemptions.** The `system-upgrade` Namespace is exempt under C3. The
  `plans.upgrade.cattle.io` CRD needs the C3 amendment H1 raises in its *Open questions* 2,
  which already names SUC.
- **Plan `k3s-server`.**
    - `nodeSelector`: `node-role.kubernetes.io/control-plane: "true"`, plus the two clauses
      below.
    - `concurrency: 1`, no `cordon` and no `drain`. The runbook neither cordons nor drains,
      and a failed Job would leave a cordoned node cordoned.
    - `version: {{ k3s_version }}` and `upgrade.image: rancher/k3s-upgrade`.
    - `jobActiveDeadlineSecs: 3600`.
    - `prepare` runs the gate script in `server` mode. See *Gates at apply time*.
- **Plan `k3s-agent`.** The same, except:
    - the `nodeSelector` requires `node-role.kubernetes.io/control-plane` to be absent;
    - `prepare` runs the gate script in `agent` mode, which first waits for the server Plan;
    - `jobActiveDeadlineSecs: 5400`. SUC starts both Jobs when the Plans resolve, so the
      agent's deadline covers the server's 60 minutes plus 30 for Longhorn's recovery and
      the agent's own upgrade. A fail-fast agent `prepare` is not an option: three failed
      pods end in `JobFailed` within minutes (Baseline).
- **Every Plan in the role carries `version: {{ k3s_version }}`**, the H6.1c drill Plan
  included, because the health expressions compare against it. A render test asserts it.
- **Two selector clauses on both Plans.**
    - `kubernetes.io/hostname In {{ system_upgrade_nodes }}`. The default list is empty and
      renders as `[no-node-selected]`, a valid label value that no node carries, so it
      matches no node. PRs widen it one node at a time, and
      setting it back to empty is the durable pause.
    - `homelab/k3s-upgrade-paused DoesNotExist`. Labelling a node pauses SUC on it at once,
      without a promotion. That is the immediate pause, and it is an operator write.

The order is server first, then agent. Kubernetes' version-skew policy forbids a kubelet
newer than its apiserver, and `docs/k3s-upgrade.md` already orders it that way. See *Open
questions* 1.

### The Plans' outcome reaches `flux/system-upgrade`

The role declares two keys in its defaults, `flux_health_check_exprs` and `flux_timeout`.
PR H6.1b teaches the renderer to copy them into `DIR/flux/system-upgrade.yaml` as
`spec.healthCheckExprs` and `spec.timeout`; a role without them renders neither. That is a
C3 change, raised in *Open questions* 5.

Flux has no generation check for a Plan (Baseline), so the expressions themselves must
reject a status that predates the apply. They compare `status.latestVersion` with the
version SUC resolves from the pin. That value is rendered as a literal, `V`, which is
`k3s_version` with `+` replaced by `-`, as `MungeVersion` does. A missing field reads as
stale, through `has()`. The expressions, for `upgrade.cattle.io` `Plan`:

- `inProgress`: `latestVersion` is absent or differs from `V`; or `.status.applying` is
  non-empty and no `Complete` condition carries reason `JobFailed`;
- `failed`: a `Complete` condition is `False` with reason `JobFailed`;
- `current`: `Complete` is `True` and `latestVersion` equals `V`.

A stale version reads InProgress whatever its old conditions say, so neither the previous
run's `Complete=True` nor its `JobFailed` decides the new revision.

**What the expressions cannot see.** A change to the selector alone, such as H6.3c and
H6.3d, keeps the version and the hash. Until SUC reconciles it, the Plan still reads the
`Complete=True` it had while it selected no node, so Flux can read Current before any Job
exists. For those PRs, and for exit 1, the evidence is the node, not the Flux status: each
selected node's `plan.upgrade.cattle.io/<plan>` label equals the Plan's
`status.latestHash` (*Verification*).

The agent Plan's 90-minute deadline is the longest, so the Kustomization's `timeout` is 100
minutes. A healthy bump ends well inside that: the server's gate wait, about ten minutes
of server upgrade, then the agent's wait for Longhorn's recovery and for 15 minutes after
the server's start, then about ten minutes of agent upgrade. A Job
that fails, or runs out its deadline, turns `flux/system-upgrade` red. `promote` then
writes `DEPLOY_HOLD` and, because the tag is forward-only, does not reset `prod`. The value
is H6's own value, the failed revision, and never C2's `<T> failed=<names>` form.
C2's proposed revert rule acts only on that form, so a merged revert never clears this
hold. Only the operator clears it (**operator-only**). While the
hold stands, SUC re-creates a failed Job after its 900-second TTL, so the gate keeps
retrying. A successful upgrade lets `last-good` move to the bump commit. PR H6.1c proves
the red path on production, and pins the reason string it observes.

### Gates at apply time

SUC upgrades when Flux applies a changed Plan. A rule in `land_lib/policy.py` would be inert
for the PRs that carry k3s bumps (Baseline), and it would gate the merge, not the apply. The
gates therefore run inside each Plan's `prepare` container, whatever path merged the PR.

- **One script.** `hosts/lib/k3s_gates.py` is stdlib Python 3.12. Both host packages ship it
  as `/usr/lib/homelab/k3s_gates.py` in the `k3s-restart` component.
- **In the Plan.** `prepare` uses a digest-pinned `python` image,
  `system_upgrade_gate_image`, and runs as root, the image's user. It declares no
  `volumes` entry. SUC already mounts the host's root at `/host` (Baseline), so it runs
  `/host/usr/lib/homelab/k3s_gates.py --host-root /host`. It reads the API with the token
  of `k3s-plan`, the Plans' ServiceAccount. A node without the package fails `prepare`,
  which is the closed direction.
- **What it checks.** Each check reads the API, refuses on a read error, and refuses when a
  named member is missing:
    - no volume degraded or faulted, with at least one volume listed;
    - no Longhorn backup in progress;
    - `daniel-box` and `daniel-server` both present and `Ready`;
    - the CoreDNS Deployment's rollout finished: `observedGeneration` equals
      `generation`, and `updatedReplicas`, `availableReplicas` and `replicas` are equal.
      `Available` alone stays True during a rolling update, so it would let a server
      restart start while H6.6's CoreDNS rollout is still running.
- **No windowed restart in progress.** Both `prepare` modes refuse while
  `/host/run/homelab-k3s-restart/` exists. The restart unit declares
  `RuntimeDirectory=homelab-k3s-restart`, so systemd creates that directory before the
  unit's `ExecStart` runs, and removes it when the unit stops, a kill or a timeout
  included. The unit in turn refuses while a Plan is applying or a SUC Job runs
  (*`homelab-k3s-restart`*, step 6). Each side sets its own mark before it reads the
  other's, so a SUC upgrade and a windowed restart never run on one node at once. Host mode
  skips this check, because it runs inside that unit.
- **The server-restart guard.** It refuses while the server restarted less than 15 minutes
  ago. The signal is the `kube-system/kube-controller-manager` Lease (Baseline, *What
  moves when k3s restarts*). The guard refuses in each of these cases:
    - the Lease is missing, Forbidden or unreadable;
    - `holderIdentity` is empty;
    - `renewTime` is older than `leaseDurationSeconds` plus 30 seconds. Just after a
      restart the old holder's record keeps its old `acquireTime` until it expires, so a
      stale `renewTime` is the handover gap, and it refuses;
    - `acquireTime` is less than 15 minutes old.
    - The node's `Ready` `lastTransitionTime` is not read, because a server restart never
      moves it.
- **Server mode** adds two daniel-box host checks until H7.19a retires the deployer. D6
  keeps the deployer for the host plane (D6, *What stays until H7*), so the checks outlive
  D6, and *Open questions* 10 names who removes them. Both read under `/host`.
    - `hold_sha` must be absent or empty, read as `k3s_upgrade.py:held_sha` reads it
      (runbook gate 3). An absent file is the deployer's no-hold state (Baseline). An
      unreadable `/var/lib/gitops-deploy` refuses.
    - The `all` deploy lock must be free. Its path is
      `deploy_locks.py:lock_path(SERVICE_LOCK_ALL)`, the one place the deployer names it,
      and a test holds the path `k3s_gates.py` uses equal to it.
    - `/var/lock` is an absolute symlink to `/run/lock` (Baseline). Under `/host` that
      link would resolve to the container's own `/run/lock`. The script therefore resolves
      each symlink in the path itself, re-rooting an absolute target under `/host`.
    - The lock is opened `O_RDONLY`, never with `O_CREAT`, and tested with `flock`
      `LOCK_EX | LOCK_NB`, then released. A root `O_CREAT` open fails under
      `protected_regular`, or creates a root-owned file that breaks every later deploy
      (Baseline). An absent lock file refuses, because the restart component's
      `tmpfiles.d` entry creates it at boot (*`homelab-k3s-restart`*).
    - It polls every 30 seconds until every check passes, inside the Job's deadline.
- **Agent mode** first waits for the server Plan. `.status.applying` must be empty,
  `Complete` must be `True`, the server Plan's `latestVersion` must equal
  `SYSTEM_UPGRADE_PLAN_LATEST_VERSION`, and every control-plane node's `kubeletVersion`
  must equal it after the same `+` to `-` mapping. The version clause keeps a stale
  `Complete=True` from passing. Upstream's `prepare k3s-server` checks the rest.
  Then it waits for the storage checks and the server-restart guard, so the agent restarts
  only after Longhorn has recovered and at least 15 minutes after the server's last start.
- **A refusal never touches the node.** `prepare` runs before the upgrade container, and
  there is no cordon. A refusal that outlasts the deadline is a `JobFailed`, which holds
  promotion and pages (*Alerts*).

A parity test feeds the same fixture JSON to `k3s_gates.py` and to the runbook's own
predicates: `gate_runner.py:unsafe_volumes`, `k3s_upgrade.py:in_flight_backups` and
`k3s_upgrade.py:nodes_not_ready`. It fails when they disagree.

### No-downgrade check

`upgrade.sh` already refuses an older binary, so a lowered Plan fails its Job rather than
downgrading. A CI check in `scripts/validate/` refuses a decrease of `k3s_version` unless
`k3s_downgrade_acknowledged` in the same file equals the new pin. A raise must leave that key
absent. The acknowledged decrease exists for the H6.6 rollback, which lowers the pin after a
hand downgrade.

### The installed binary matches the release

`rancher/k3s-upgrade` is pulled by tag from Docker Hub into a privileged Job that replaces
the k3s binary on both nodes. The Ansible install it replaces checks its script by sha256.
The image's build checks the asset against the release's sums (Baseline), but nothing on
the node checks what the Job installed. H6.3e adds that check.

- **The pin.** The role's defaults carry `k3s_binary_sha256_amd64`, the `k3s` line of the
  release's `sha256sum-amd64.txt` for `k3s_version`. The PR that bumps `k3s_version`
  finishes it, as it finishes the installer sha256 (H6.6). A CI check in
  `scripts/validate/`, beside the no-downgrade check, refuses a change to `k3s_version`
  that leaves the sum unchanged.
- **The mode.** `k3s_gates.py` gains a `binary` mode. It first reads the node's
  `kubeletVersion` through the API, as agent mode does, and executes no host binary. A
  version other than `k3s_version` reads pending, not a mismatch, because a hold can leave
  a node on the old version while the pin moved. At the pinned version, it compares the
  binary's sha256 with the pin and fails on a mismatch, naming the node.
- **The gate.** One Job per node in `system_upgrade_nodes` runs the mode in the
  `system-upgrade-verify` Kustomization (C3), C7's second verify-successor shape. Each Job
  mounts the node's binary and the packaged script read-only by hostPath, in the
  digest-pinned gate image, and reads the API as `k3s-plan`. A mismatch turns `flux/system-upgrade-verify` red, which holds
  promotion and keeps `last-good` where it was. `-verify` exists from D5.1a1, which is not
  one of this slice's edges (*Open questions* 14).

### Flags move from the unit to a package drop-in

- **One source of truth.** `k3s_server_config` and `k3s_agent_config` are YAML mappings in
  the `defaults:` of `hosts/components/k3s-server/component.yml` and
  `hosts/components/k3s-agent/component.yml` (H3's *Component variables*). Their comments
  carry the reasons from the `k3s_server_args` comment, including the peer-URL trap.
    - Each key is a flag name without `--`. A repeatable flag is a YAML list
      (`disable`, `kube-apiserver-arg`, `kubelet-arg`, `node-label`).
    - Mode strings are quoted (`write-kubeconfig-mode: "0640"`).
    - `node-ip` and `advertise-address` stay `{{ server_ip }}`.
    - The variables the flags and files read take one of five homes. Host mode layers
      `group_vars/all.yml` and `host_vars` above a component's defaults (H3, *Component
      variables*), so an inventory key needs no copy.
        - **Stay in the inventory:** `k3s_audit_log_enabled`, `k3s_audit_log_dir`,
          `k3s_audit_log_path` and `k3s_etcd_expose_metrics` in
          `ansible/inventory/group_vars/all.yml`, `server_ip` in each host's `host_vars`,
          and the `k8s_registry_*` keys that `registries.yaml.j2` reads. loki-homelab reads
          the audit keys, and `ansible/tests/setup/test_etcd_metrics_single_switch.py`
          holds the etcd switch to one definition, so none of them moves.
        - **Move in H6.4a:** `k3s_oidc_issuer_urls`, `k3s_oidc_client_id`,
          `k3s_oidc_username_claim`, `k3s_oidc_groups_claim` and `k3s_oidc_groups_prefix`.
          `authentication-config.yaml.j2` is their only template reader.
        - **Copied in H6.4a, deleted from `setup/k3s` in H6.4b:** `k3s_audit_policy_path`
          and `k3s_authentication_config_path`. The component ships the files at those
          paths, and `k3s_server_args` names them until H6.4b deletes it. Between the two
          PRs, the shipped-paths test holds each copy equal to its `setup/k3s` twin.
        - **Move in H6.4b:** `k3s_audit_log_maxage`, `k3s_audit_log_maxbackup` and
          `k3s_audit_log_maxsize`. Only the flags read them.
        - **A mirrored triple:** `audit-policy.yaml.j2` reads `k3s_readonly_sa_name` and
          `k3s_readonly_sa_namespace`. `kubeconfig.yml` keeps reading them in `setup/k3s`,
          and from H1.2 `cluster-base` holds the twins `cluster_base_readonly_sa_name` and
          `cluster_base_readonly_sa_namespace`. H6.4a gives the component
          `k3s_server_readonly_sa_name` and `k3s_server_readonly_sa_namespace`, which the
          moved audit policy reads, and extends H1's *Mirrored identity keys* test so the
          three members of each triple must be equal.
    - `k3s_secrets_encryption` does not move. `server.yml`'s secrets-encryption status and
      re-encrypt tasks still read it until H7. Until then it has two sources, so a test
      holds `secrets-encryption` in `k3s_server_config` equal to it.
- **The config drop-in.** `/etc/rancher/k3s/config.yaml.d/50-homelab.yaml` renders the
  mapping. No other file exists in that directory or at `config.yaml`, and a test asserts the
  package ships only this one.
- **The unit drop-in.** `/usr/lib/systemd/system/k3s.service.d/50-homelab.conf`, and the
  `k3s-agent` equivalent, carries two sets of lines from different PRs:
    - the logging lines from `unit-logging.yml` (`StandardOutput=append:`,
      `StandardError=inherit`), in PR H6.4a or H6.5a;
    - `ExecStart=` cleared, then `ExecStart=/usr/local/bin/k3s server` (or `agent`), in PR
      H6.4b or H6.5b.
- **The agent keeps its token where it is.** The agent's drop-in clears no
  `EnvironmentFile=`. The installer's env file still supplies `K3S_URL` and `K3S_TOKEN`, and
  the etcd drill still reads it.
- **Same path, so dpkg takes over.** The package ships these at the Ansible-era path and
  mode, and the PR deletes the task that wrote each:
    - `registries.yaml`;
    - the audit policy and its log directory;
    - the authentication config;
    - `coredns.yaml.skip`;
    - `/etc/logrotate.d/k3s` and `/etc/logrotate.d/k3s-agent`.
- **The log file.** A `tmpfiles.d` entry, `f /var/log/k3s.log 0640 root adm -`, creates it
  at boot on a fresh host. A running host keeps the file Ansible created. The package never
  ships the log file itself, because dpkg would replace it.
- **Different path, so a legacy entry.** Each unit's
  `/etc/systemd/system/<unit>.service.d/10-log-to-file.conf` is a legacy entry. Its lines
  equal the package's, so both apply harmlessly until reconcile removes it.
- **Forward-only files.** Once the package ships a file that a k3s flag or `ExecStart`
  names, no revert may drop it. dpkg removes a file the next version stops shipping
  (design.md, *Host plane*). A missing authentication config wedges the API server on the
  next start. Returning such a file to Ansible is the attended procedure under *Rollback*.

### `homelab-k3s-restart`

The `windowed` mark for `k3s.service` and `k3s-agent.service` lives in each host's
`hosts/<host>/package.yml`, not in a component, so reverting a component never drops it.
Reconcile therefore never restarts either unit (H3, *The reconcile unit*, step 3).

`homelab-k3s-restart.timer` fires at `k3s_restart_window`. Each host package sets its own
value, so the two restarts never run together, even for a PR that changes both
components:

- daniel-box: `Mon..Fri *-*-02..31 11:32:00`. That slot falls between the `*/10` and
  `*/30` fires at 11:30 and the next at 11:40. It also misses the `*/15` fire at 11:45, the
  hourly `:23` timer, and the 1st of the month, when the hypervisor drill starts at 11:20
  on daniel-server.
- daniel-server: `Mon..Fri *-*-02..31 11:52:00`. That runs to 11:59, after daniel-box's
  11:50 fire and before the 12:00 fires. An agent restart degrades Longhorn replicas that
  daniel-box's crons read, so this slot avoids those too.
- Agent-host mode also runs the server-restart guard (*Gates at apply time*). That holds
  the order when the server's restart ran long or was started by hand.
- The timer sets `AccuracySec=1s` and `RandomizedDelaySec=0`. Without them systemd may
  start the unit up to a minute late (Baseline), and an 11:32 start could then run into
  the 11:40 fire.

A test derives all of this from the schedules (see *Verification*). The service is
`Type=oneshot` with `TimeoutStartSec=7min` and `RuntimeDirectory=homelab-k3s-restart`, the
marker that `prepare` reads (*Gates at apply time*). `SyslogLevel=notice` keeps its lines
through journald's `MaxLevelStore=notice`. It reads its credential in H4's form:
`LoadCredential=k3s_readonly_token:/run/credstore/k3s_readonly_token`, with
`Wants=homelab-secrets.service` and `After=homelab-secrets.service`.

H4 makes reconcile the only process that starts or restarts a unit outside the boot path
(H4, *The restart contract*). This unit restarts `k3s` or `k3s-agent`, a `windowed` unit,
inside its window. SUC's upgrade container restarts the same unit after it replaces the
binary. *Open questions* 8 asks H4 to name both, and the two exclude each other on a node
(*Gates at apply time*, and step 6 below).
Each run does these steps:

1. It hashes the k3s config paths that the package ships, read from the manifest: the
   config drop-in, the unit drop-in, `registries.yaml`, the audit policy and the
   authentication config. A path Ansible still writes is outside the set, so an Ansible
   change never causes a second restart.
2. With no `/var/lib/homelab-k3s-restart/applied.sha256`, it records the hash and exits 0.
   The first install never restarts k3s.
3. It first renames a leftover `attempt.sha256` to `failed.sha256`. Step 7 writes that
   file before it issues a restart, and step 8 removes it, so a leftover one means a run
   restarted k3s and was killed before it recorded the outcome, by `TimeoutStartSec` or a
   reboot. Then, if the hash equals `applied.sha256`, it writes `pending 0` and exits 0. It
   writes `failed 0` only when no `failed.sha256` exists. A present one means the host was
   reverted after a failed restart and still owes the operator's recovery restart
   (*Rollback*, H6.4b), so `failed 1` stays and the unit logs that at notice.
4. If the hash equals `failed.sha256`, it writes `failed 1` and exits 0. It makes one
   attempt per hash, a killed attempt included.
5. On daniel-box until H7.19a retires the deployer, it takes the `all` deploy lock
   (`deploy_locks.py:lock_path(SERVICE_LOCK_ALL)`) exclusively without waiting, and holds
   it through step 8. It opens the file `O_RDONLY`, never with `O_CREAT` (Baseline,
   *The deploy lock and the hold*). It refuses when a deploy holds the lock, when the file
   is absent, or when `hold_sha` is non-empty.
    - The `k3s-restart` component on daniel-box ships a `tmpfiles.d` entry,
      `f /run/lock/server-deploy-all.lock 0644 <sys_user> <sys_user> -`. It creates the
      file at boot, owned by the user the deployer runs as, so neither side ever creates it
      as root. A test holds its path equal to `lock_path(SERVICE_LOCK_ALL)` through the
      `/var/lock` link, and its owner equal to the gitops-deploy service's `User=`.
6. It runs `k3s_gates.py host`. That mode reads the API with
   `$CREDENTIALS_DIRECTORY/k3s_readonly_token` and the node's server CA file. daniel-box
   reads `https://127.0.0.1:6443`, and daniel-server reads `https://10.0.0.215:6443`. A
   refusal writes `pending 1` and exits 0. An unreadable cluster refuses, and so do a
   missing credential and a missing CA file. Each API read times out after 10 seconds, and
   the whole step after 60. Beside the storage, node and CoreDNS checks, host mode runs
   three checks of its own:
    - **No SUC run in progress.** Plans `k3s-server` and `k3s-agent` must both exist, each
      with an empty `.status.applying`, and every Job in `system-upgrade` must carry a
      `Complete` or `Failed` condition. A missing Plan refuses. The Job read covers the gap
      between SUC creating a Job and writing `.status.applying`. systemd created
      `/run/homelab-k3s-restart/` before this read, so a `prepare` that starts later
      refuses on it.
    - **No promotion in flight.** Every `kustomize.toolkit.fluxcd.io` Kustomization in
      `flux-system` must read `Ready=True`, with `status.observedGeneration` equal to
      `metadata.generation`. Its `status.lastAppliedRevision` must equal the
      `status.artifact.revision` of the source its `spec.sourceRef` names. The root
      Kustomization `homelab` (D2's `flux/homelab` status) is a named member, so an empty
      or Forbidden list refuses. A Kustomization that is applying or polling health does
      not read `Ready=True`, so a windowed restart does not start while `promote verify`
      waits on one (D2, *Publication and verification*). The server `prepare` does not run
      this check, because the revision that changes a Plan is itself the one under
      verification. Neither does the agent `prepare`.
    - **A suspended Kustomization is left out of all three clauses.** D5 lets a role set
      `flux_suspend: true`, which renders `spec.suspend: true`, and its incident runbook
      suspends by hand (D5, *The stop lever (D5.1b)*). A suspended Kustomization never
      advances `lastAppliedRevision`. The suspend patch also raises `metadata.generation`,
      which its `observedGeneration` never reaches. Checked like any other, it would refuse every
      window for as long as the suspension stands. `promote.py verify` drops a suspended
      Kustomization from the statuses it waits on, so no verify can be polling it. The
      check therefore skips any Kustomization whose `spec.suspend` is `true`, and the unit
      names each one it skipped in a notice line. `homelab` must still be listed. Its own
      suspend state does not matter.
    - **The server-restart guard**, on daniel-server only. It reads the controller-manager
      Lease.
    - The read-only account reads the Plans and the Lease through the grants H6.3b adds to
      `cluster-base`, and the Jobs and the Flux objects through `view` (Baseline; Flux's
      vendored view role aggregates into it, as D1's *Baseline* records).
    - The gates need the API, and daniel-box hosts the only API server. So the unit
      cannot recover a k3s that a failed restart left down. That recovery is an
      operator step (*Rollback*, H6.4b).
    - The CA path is unmeasured, because the directory is root-only. H6.3b's pre-merge step
      reads it on both nodes.
7. It reads the unit's `MainPID` and takes the restart instant T0 from the host clock. It
   writes the hash to `attempt.sha256` through a temporary file and a rename. Then it runs
   `systemctl --no-block restart k3s` (or `k3s-agent`), and polls every 5 seconds until T0
   plus 300 seconds by the wall clock, with a 5-second timeout on each read. A poll
   succeeds only when all three of these hold at once:
    - `systemctl is-active` prints `active`. The unit is `Type=notify`, so a start that
      has not sent `READY=1` reads `activating`;
    - `MainPID` differs from the one read before the restart. Right after a `--no-block`
      enqueue, `is-active` can still describe the old process;
    - the node's `Ready` condition is `True` with a `lastHeartbeatTime` at least one
      second later than T0, because the field has one-second resolution.
      The kubelet posts a status when it starts, so a restarted node sends a fresh
      heartbeat. A `Ready=True` from before the restart has an older heartbeat and does
      not count. T0 and the heartbeat come from the same host's clock, through its own
      kubelet, so no cross-host skew applies.
    - An API read error counts as "not yet". On daniel-box the API is down while k3s
      restarts.
8. When a poll succeeds, it writes `applied.sha256`, removes `attempt.sha256`, and writes
   `pending 0`, `failed 0` and the success timestamp. When 300 seconds pass without one,
   it renames `attempt.sha256` to `failed.sha256`, writes `failed 1`, and exits 1.

**The time bound.** `TimeoutStartSec=7min` is 420 seconds. Step 6 is capped at 60 seconds
and step 7 at 300. Steps 1 to 5 read local files and take a non-blocking lock. About 60
seconds remain for step 8 and the metric write. The attempt marker covers a kill that the
bound misses: the hash loses its one attempt and pages, rather than restarting k3s again at
every window. When `$SERVICE_RESULT` is not `success` and an `attempt.sha256` exists, an
`ExecStopPost=` renames it and writes `failed 1`. A timeout on a Friday therefore pages at
once, not at Monday's window.

**Metrics on every run.** Every run, steps 2 and 3 included, writes
`/var/lib/node-exporter-textfile/homelab_k3s_restart.prom` with all three series:
`homelab_k3s_restart_pending{host}`, `homelab_k3s_restart_failed{host}` and
`homelab_k3s_restart_last_success_timestamp_seconds{host}`. So does the `ExecStopPost=`.
The writer fills `.homelab_k3s_restart.tmp` in the same directory, then renames it. The
textfile collector reads every `*.prom` file, so a temporary name ending in `.prom` would
be scraped half-written. The success timestamp is kept in
`/var/lib/homelab-k3s-restart/last_success`, so a run that restarts nothing re-emits it.
Step 2 sets it to the time it records the first hash.

- **Retry after a failure.** The operator removes `failed.sha256` with `sudo rm`, and the
  next window tries again. After a recovery restart by hand, the same `rm` clears
  `failed 1` at the next run, because the hash then equals `applied.sha256`.
- **Restart sooner.** The operator runs `sudo systemctl start homelab-k3s-restart.service`,
  and the gates still apply.

### Pause and resume

| Lever | Pause | Resume |
|---|---|---|
| SUC, one node, at once (**operator-only** write) | `sudo k3s kubectl label node <n> homelab/k3s-upgrade-paused=true` | the same command with `homelab/k3s-upgrade-paused-` |
| SUC, every node, through git | a PR setting `system_upgrade_nodes: []` | a PR restoring the list |
| The windowed restart, one host | `sudo systemctl mask --now homelab-k3s-restart.timer` | `sudo systemctl unmask homelab-k3s-restart.timer`, then `start` it |
| Every package change, one host | `sudo systemctl mask --now homelab-updater.timer` (H3) | `unmask`, then `start` |

Reconcile never starts a masked unit (H3, step 2). `docs/k3s-etcd-restore.md` gains a step
that masks both timers on daniel-box before k3s stops, and unmasks them after the restore is
verified.

### Rehearsal in a drill guest

Production is the only standing cluster (Baseline), so the SUC Plan and the Flux apply are
rehearsed on a throwaway k3s node first. The node is a transient guest on daniel-server,
built by the etcd restore drill's orchestrator on its `staging` network behind
`fence_check`. H7's bring-up drill uses the same guest.

- **When.** Before H6.1c, and before every PR that raises `k3s_version` by a minor
  release. The PR body quotes the run's result. A patch bump, such as H6.6's, needs none.
- **What it runs.**
    - k3s at the running pin, from the release installer checked against
      `k3s_install_script_sha256`.
    - Flux's controllers at D1's pinned version.
    - The `system-upgrade` bundle rendered from the PR's head, and nothing else. The
      orchestrator pushes it with `flux push artifact` to a registry container inside the
      guest, and points an OCIRepository at it. The guest never reads GHCR or `prod`, so
      it makes none of the real writes H7 *The proofs* rejects for a guest that applies
      `prod`.
    - A guest overlay sets `system_upgrade_nodes` to the guest's hostname and drops
      `prepare`. The gates read Longhorn, both nodes and the deployer, and the guest has
      none of them. The gate fixtures and the parity test prove the gates instead (*Gates at
      apply time*).
- **What it proves.**
    - The server Plan only. A lone guest can be a server, never an agent, because an agent
      could not reach daniel-box through the fence.
    - For an upgrade: the Plan moves the guest to the PR's version, the node's
      `plan.upgrade.cattle.io/k3s-server` label equals the Plan's `latestHash`, and the
      `system-upgrade` Kustomization reads Ready through the health expressions.
    - Before H6.1c: H6.1c's drill Plan turns the Kustomization's health `failed` within
      minutes, as H6.1c then expects on production.
- **Why the guest suffices, and why k3d does not.** SUC's upgrade container replaces the
  binary under `/host` and restarts the k3s unit. In a k3d node the k3s process is the
  container's entrypoint, so the replacement never takes the path a node takes. The guest
  is a systemd host like daniel-box. Under C8 it is the third substitute, an
  **operator-only** drill on production hardware, as H7 *Open questions* 11 rules for its
  own guest.
- **Blast radius.** A run touches a transient domain and its disk on daniel-server, the
  `staging` network and the fence. It takes CPU and memory from the agent node for one run.
  The guest holds no production credential: k3s generates a guest-only token, and the guest
  gets no SOPS key and no registry credential. It pulls `rancher/k3s-upgrade` and the SUC
  image from Docker Hub over its internet egress, as production does. Every service keeps
  serving, and `DEPLOY_HOLD` and `prod` stay untouched. The rollback is the orchestrator's
  teardown.

### Alerts

The rules go in `ansible/roles/k8s/observability/files/rules/k3s-upgrade.yaml`, with promtool
tests, under M1's conventions. The node-exporter DaemonSet already reads
`/var/lib/node-exporter-textfile` on both nodes (`ansible/roles/k8s/node-exporter/templates/daemonset.yaml.j2`).

| Alert | Expression, in words | `for` | Severity | `service` |
|---|---|---|---|---|
| `K3sUpgradeJobFailed` | `kube_job_failed{namespace="system-upgrade",condition="true"} == 1`, with `keep_firing_for: 2h` | 0m | critical | `system-upgrade` |
| `K3sNodeVersionSkew` | the nodes report more than one `kubelet_version` | 2h | warning | `system-upgrade` |
| `K3sConfigRestartFailed` | `homelab_k3s_restart_failed == 1` | 0m | critical | the host |
| `K3sConfigRestartPending` | `homelab_k3s_restart_pending == 1` | 72h | warning | the host |
| `K3sConfigRestartMetricsAbsent` | `homelab_k3s_restart_failed` is absent for `daniel-box`, or for `daniel-server` | 1h | warning | the host |

`K3sUpgradeJobFailed` reads kube-state-metrics' Job series, which do not exist at
`70591c307` (Baseline). PR H6.1b therefore adds `jobs` to kube-state-metrics'
`--resources` and `list` and `watch` on `batch` `jobs` to its ClusterRole, in the same
edit, as that template's comment requires. Two more choices in the expression:

- It reads the Job's `Failed` condition, not the failed-pod count. `job.go` sets
  `BackoffLimit` 2, so a pod can fail inside a Job that then succeeds, and a pod-count
  rule with `for: 0m` would page on it.
- `keep_firing_for: 2h` outlasts the 900-second TTL and the next Job's deadline of up to
  90 minutes. Without it, the alert would resolve when SUC deletes the failed Job, then
  fire again when the re-created Job fails. Prometheus at
  `observability_k8s_prometheus_image` is v3.15.0, which supports the field.

In three cases this alert is the only page: a selector-only change, which promotes before
any Job exists; the operator removing a pause label, which involves no revision; and a Job
that SUC re-creates after its TTL.

`K3sConfigRestartMetricsAbsent` has one `absent()` term per host. A host whose textfile
stops reaching Prometheus therefore fires, rather than reading as clean on the two restart
alerts. The collector already serves `claude_cgroup.prom` from this hostPath on both nodes
(`ansible/roles/k8s/node-exporter/CLAUDE.md`), but that file carries no `host` label, so it
proves the directory and the scrape and nothing about this writer's file or the label the
alerts read. So the rule lands in H6.4a, after H6.3b's live query has shown both series.

### Autonomous-role contract

`ansible/roles/k8s/system-upgrade/CLAUDE.md` carries an `## Autonomous-role contract` for SUC
and `homelab-k3s-restart`. The k3s defaults comment says `k3s-bringup.yml` is manual
"precisely so nothing automated ever applies it", and this slice reverses that. The ADR in
PR H6.1a records the operator's approval.

- **Scope.** SUC replaces the k3s binary with the pinned version on a selected node, and never
  drains, cordons or downgrades. The restart unit restarts one k3s unit once per changed
  config hash, inside the window.
- **Mode and arming.** `system_upgrade_nodes`, the node pause label, and the timer's mask.
- **Abort valves.** The `prepare` gates, the Job deadline, the restart unit's gates with
  their SUC and Flux in-flight checks, its deploy lock, and one attempt per hash, a killed
  attempt included.
- **Required evidence.** The Plans' `Complete` condition and `flux/system-upgrade`, the
  restart unit's notice lines and textfile metrics, and the five alerts.
- **Next-run review.** Read the last bump's Plan conditions and the restart unit's journal
  before widening the scope or narrowing the window.

### File tree

```diff
+ docs/adr/<n>-k3s-upgrades-and-restarts-run-unattended.md
+ ansible/roles/k8s/system-upgrade/                      (pin, vendored SUC, two Plans, CLAUDE.md)
+ ansible/roles/k8s/observability/files/rules/k3s-upgrade.yaml
  ansible/roles/k8s/observability/templates/kube-state-metrics.yaml.j2  (+ jobs, in the flag and the ClusterRole)
  ansible/roles/k8s/cluster-base/templates/readonly-rbac.yaml.j2  (+ a kube-system Role: get one Lease; H1.2 moved the file)
  ansible/roles/k8s/cluster-base/defaults/main.yml       (+ upgrade.cattle.io in k3s_readonly_crd_api_groups)
+ hosts/lib/k3s_gates.py
+ hosts/components/k3s-restart/                          (unit, timer, k3s_gates.py)
+ hosts/components/k3s-server/component.yml              (k3s_server_config and the file variables)
+ hosts/components/k3s-server/templates/                 (config drop-in, unit drop-in, audit policy, auth config, registries)
+ hosts/components/k3s-agent/component.yml
+ hosts/components/k3s-agent/templates/
+ hosts/daniel-box/package.yml                           (windowed: k3s.service)
  hosts/daniel-server/package.yml                        (+ k3s-agent, + k3s-restart, windowed: k3s-agent.service)
  ansible/roles/setup/k3s/defaults/main.yml              (- the pin, - k3s_server_args, - the moved variables)
  ansible/roles/setup/k3s/tasks/server.yml               (install only on a fresh host; file tasks removed)
  ansible/roles/setup/k3s/tasks/agent.yml                (join only on a fresh host; file tasks removed)
- ansible/roles/setup/k3s/tasks/unit-logging.yml
  ansible/roles/setup/k3s/tasks/coredns.yml              (.skip task removed)
  ansible/roles/setup/hypervisor/                        (+ the SUC rehearsal mode, H6.1b2)
  scripts/validate/                                      (+ the sum co-change check, H6.3e)
  docs/k3s-upgrade.md                                    (rewritten for SUC)
```

## Steps, as PRs

Vendored bytes are reviewed by their sha256 against upstream, as H1 does, and do not count
toward a PR's size. A PR that edits `ansible/roles/setup/k3s/defaults/main.yml` makes the
tick write one `manual_plane` line for `k3s`. That PR changes nothing an apply would do on a
running node, so its verification ends with `gitops_state.py clear-owed manual_plane k3s`.

1. **H6.1a ADR (docs only, about 80 lines).**
    - Add the ADR: SUC upgrades k3s and `homelab-k3s-restart` restarts it, unattended inside
      a gate and a window. It names the pause levers and the contract.
    - **Operator-only:** approve the ADR before H6.1b merges.
2. **H6.1b SUC role, selecting no node (about 280 lines, plus vendored bytes).**
    - Add the role, its `owner: flux` entry, both Plans with `system_upgrade_nodes: []` and
      their `jobActiveDeadlineSecs`, the `flux_health_check_exprs` and `flux_timeout` keys,
      `flux_forward_only: true`, and the role `CLAUDE.md` with the contract.
    - Add ServiceAccount `k3s-plan`, ClusterRole `k3s-plan-gate`, Role
      `k3s-plan-kube-system` and their two bindings, and set `serviceAccountName: k3s-plan`
      on both Plans.
    - Add `jobs` to kube-state-metrics' `--resources` and its ClusterRole, and add
      `K3sUpgradeJobFailed` to `k3s-upgrade.yaml` with its promtool test (*Alerts*). This
      lands before H6.1c, so one drill proves the Flux red path and the alert together.
      How the observability half deploys follows the narrow output below. While
      `observability` is Ansible-owned, the narrow output names it through
      `scripts/deploy_tools/narrow_lib/broad.py:_changed_half`, which maps these edits
      under `ansible/roles/k8s/observability/` to the role's tag. The added entry is not
      the cause, because an entry without `hostname`, `port`, `tier` or `metrics` reaches
      no observability template (D2 Open question 10). The landing's deploy-plane apply
      therefore includes observability, and `k8s_autodeploy: false` does not stop that
      plane (D1.2, *Landing*). If the narrow output does not list `observability`, the
      session stops until it knows why, then runs `./scripts/deploy.sh --tags
      observability` after landing. If that role is Flux-owned by then, promotion applies
      it. After landing, a Prometheus query must
      list `kube_job_failed` series for namespace `homelab`, which held 17 Jobs on
      2026-10-11. A missing `homelab` series means the flag or the grant did not take
      effect. H6.1c starts only after this query passes, so the drill never runs against a
      rule that was not deployed.
    - **The added `containers_list` entry reaches the readers of its fields.** At
      `70591c307`, `scripts/deploy_tools/narrow_lib/containers.py:changed_fields` returned
      None for an added entry. `scripts/deploy_tools/narrow_lib/broad.py:_list_reader_tags`
      with `changed` None then named 23 tags of 59 (D1, *Baseline*). D2.1c records an added
      `owner: flux` entry by its fields instead. At `a5abf8581` an entry that
      carries `name`, `platform` and `owner` reaches artifacts, blackbox-exporter, pihole
      and uptime-kuma, three of them `k8s_autodeploy: false` (D2 Open question 10). The
      tick applies them unattended and forward-only, because D2.2 drops the
      `system-upgrade` tag, until D6.12 retires its k8s arms. The own-tag-only outcome is
      refuted: artifacts' `known_services.json` names every entry. H6.1b therefore follows
      D1.2's procedure, as M4's PR 1 does:
        - Before the PR, run `uv run python scripts/deploy_tools/deploy_tags.py narrow
          origin/master HEAD`, and record the printed list in the PR body. Do not assume
          the count measured at `70591c307`.
        - Run D1.2's stop rule before the PR and again right before `land.sh`: `uv run
          python scripts/diagnostics/probe.py releases --stale-only` and `uv run python
          scripts/diagnostics/probe.py releases`. Read the service names, not the exit
          code. Do not merge while any service in the narrow output is stale, `unmerged`
          or `dirty`. Deploy this session's own such change first. Name another session's
          and wait for its owner to land it. The PR body quotes both runs.
        - A failed apply writes `hold_sha` and a `hold_plane` entry for the whole list.
          If the narrowing refuses, the session follows D1.2's *If the narrowing refuses*
          exit: run the stop rule again, then `./scripts/deploy.sh --tags` with the
          recorded list from a checkout at `origin/master`.
    - Teach the renderer to copy those two keys into the role's Kustomization, with a
      renderer test for a role that has them and one that does not.
    - Move the pin and its two siblings. The setup role reads them with `include_vars`.
    - Add a `repo_paths` constant for the `system-upgrade` defaults. Switch only the readers
      of `k3s_version` and the installer keys to it:
      `refresh_vendored_schemas.py` (`kubernetes_tag`, and its error string),
      `test_k8s_schema.py::test_vendored_core_schemas_match_the_cluster`,
      `test_k3s_version_is_pinned` (from `load_defaults(K3S_ROLE)`), and H1's CoreDNS
      image test, which reads `k3s_version` from `setup/k3s`. `K3S_DEFAULTS` stays as it
      is, because its other importers read keys that do not move (Baseline).
    - Update `renovate.json`'s rule text and prose notes. `asset_pins.py:KNOWN_PINS` must
      still find `k3s_install_script` in its new file, and the pin census proves it.
    - Fix the stale symbol and the pin path in `docs/k3s-upgrade.md`.
    - Add the `default-deny-ingress` NetworkPolicy and the controller's digest pin, each
      with its render test (*SUC through Flux*). Quote the vendored controller manifest's
      container ports in the PR body (*Open questions* 13).
    - Two-writer check: none. The Plans select no node.
3. **H6.1b2 The guest rehearsal (about 180 lines). Operator-only run.**
    - Add the rehearsal mode to the etcd drill's orchestrator in the `hypervisor` role,
      with the guest overlay (*Rehearsal in a drill guest*). Its tests join the
      orchestrator's own and cover the teardown on every exit path.
    - The change reaches daniel-server the way any `hypervisor` change does before H7.13,
      and the PR body names that path. H7.13 moves the mode with the role.
    - **Operator-only:** run the rehearsal twice, against H6.1c's drill Plan and against a
      server Plan one release above the guest's version. Both must pass before H6.1c
      starts.
    - **Recorded 2026-10-24:** both runs passed on daniel-server, after the guest-script
      fixes in #6130 and #6133.
        - Run 1, `--expect red --plan k3s-redproof`, run `20261024T193003Z`: the Plan failed,
          and the Kustomization read `Ready=False reason=HealthCheckFailed` with the message
          `health check failed after 5m5.044785708s: failed early due to stalled resources:
          [Plan/system-upgrade/k3s-redproof status: 'Failed']`.
        - Run 2, `--expect upgrade --guest-version v1.37.0+k3s1`, run `20261024T193748Z`: the
          guest node ran `v1.37.1+k3s1`, carried the Plan's `latestHash` as its label, and the
          Kustomization read Ready.
        - The reason string that H6.1b2's spec left unmeasured is now measured:
          `HealthCheckFailed`. H6.1c's expression test pins it.
4. **H6.1c Red-path drill (about 30 lines, then a revert). Operator-only.** This mirrors
   D2's hold drill, and C8 requires an operator-only drill to state its blast radius.
    - **Gate:** H6.1b2's rehearsal of this Plan passed in the guest, and this PR's body
      quotes it.
    - Add a Plan `k3s-redproof` that selects daniel-box by hostname and carries the
      `homelab/k3s-upgrade-paused DoesNotExist` clause. It carries `k3s_version` as its
      `version`, so the expressions can leave InProgress. It declares no `prepare`, because
      no host ships `k3s_gates.py` before H6.3b. Its `upgrade` container is the gate image
      with `command: [python3, -c, "raise SystemExit(1)"]`, so it touches nothing.
    - **Amended 2026-10-24:** the Plan also sets `jobActiveDeadlineSecs: 300`. The
      controller's `SYSTEM_UPGRADE_JOB_BACKOFF_LIMIT` is 99 (*Open questions* 16), so a
      failing pod retries until the Job deadline, and the red would otherwise arrive at 3600
      seconds. The rehearsal measured 5m5s to red, which matches the 300-second deadline.
    - **Blast radius.**
        - Objects: the drill Plan, and its Job and pods in `system-upgrade` on daniel-box.
          The pod runs privileged with the host's root mounted, and exits 1 without
          reading it. No binary, unit or file on the node changes.
        - The hold: `promote` writes the failed revision to `DEPLOY_HOLD` at the red
          status, and the value stays until the operator clears it. The no-hold value is
          `none` (D1, D2). While any other value stands, `promote` moves nothing, so every
          other session's merged work waits on master for the drill's duration (C2). The
          operator runs the drill when no other landing is in flight.
        - What keeps serving: every workload, because no node is touched and nothing
          restarts.
    - Expect `JobFailed`, a red `flux/system-upgrade` and `DEPLOY_HOLD` set, with `prod` not
      reset, because the tag is forward-only. Expect `K3sUpgradeJobFailed` firing in
      Prometheus' `ALERTS`, which is the alert's live red proof. Record the condition's
      reason string in the role's expression test.
    - Record the time from promotion to the red status, and the Kustomization's Ready
      message. The red must arrive within minutes, well inside the 100-minute timeout, and
      the message must name the Plan. A red that arrives only at the timeout proves the
      timeout, not the `failed` expression.
    - **Rollback.** Land the revert. `promote` cannot apply it while the hold is set, so
      the revert waits on master and the drill Plan stays live. SUC re-creates its failing
      Job every 900 seconds meanwhile, which re-fires the alert. To stop that, the operator
      labels daniel-box `homelab/k3s-upgrade-paused=true`. Then the operator clears the
      hold (**operator-only**) with `gh variable set DEPLOY_HOLD --body none -R
      DanielH2018/server`. The next `promote` moves `prod` to the revert, but the tag renders
      `prune: false` (the CRD census requires it for any tag rendering a CRD), so Flux does
      not prune the drill Plan. **Operator-only:** `kubectl -n system-upgrade delete plan
      k3s-redproof` removes it. The operator then removes the label, and silences or waits out the
      alert's 2-hour `keep_firing_for`.
5. **H6.2 daniel-box's package (about 60 lines).**
    - **Operator-only:** copy daniel-box's age key to `/etc/homelab/age.key`, as H4.2 did on
      daniel-server.
    - Add the creation rule for `ansible/vars/hosts/daniel-box.sops.yaml` ahead of the
      general rule in `ansible/.sops.yaml`, with the recipients H4 names. Add
      `homelab-daniel-box` to the named sets of both CI scans (H4).
    - **Operator-only:** run `sudo hosts/bootstrap-host.sh daniel-box`. `homelab-daniel-box`
      starts with only `reconcile`.
    - **Amended 2026-10-24:** the `windowed` mark for `k3s.service` moves from H6.2 to H6.4a,
      because the renderer refuses a unit policy that no package installs or drops in on.
      H6.4a adds `units: {k3s.service: {policy: windowed}}` with its first `k3s.service`
      drop-in.
    - The `secrets` component, daniel-box's SOPS file and its `host_secrets.py check` entry
      arrive in H6.3b with the first daniel-box key. H4's check compares the file's keys
      with the credentials the host uses, and before H6.3b daniel-box uses none.
    - The builder, the repository and every daniel-box timer stay with Ansible until H7.
    - Two-writer check: none, because no path moves.
6. **H6.3a The gate script (about 220 lines with tests).**
    - Add `hosts/lib/k3s_gates.py` with `server`, `agent` and `host` modes, and its tests in
      `hosts/lib/tests/`, including the parity test. Nothing ships yet.
    - **Operator-only**, before merging: measure the server-restart signal. Run `sudo k3s
      kubectl -n kube-system get lease kube-controller-manager -o
      jsonpath='{.spec.holderIdentity} {.spec.acquireTime} {.spec.renewTime}
      {.spec.leaseDurationSeconds}'` beside `systemctl show k3s -p
      ExecMainStartTimestamp`, and record both in this slice's Baseline. `acquireTime`
      must be at or after the k3s start, `2026-10-11 07:40:12 UTC` unless k3s restarted
      later. If it is older, the Lease does not track a restart, and the guard is not
      merged until *Open questions* 9 names another signal.
7. **H6.3b The restart component (about 230 lines).**
    - **Gate before merge:** H4's *The restart contract* names this unit and SUC's upgrade
      container as the two restarters of a `windowed` k3s unit, each with its condition
      and their mutual exclusion (*Open questions* 8). Without that amendment, H6.3b does
      not merge.
    - **Operator-only:** add `k3s_readonly_token` to SOPS with the `add-secret` skill. The
      value is the read-only ServiceAccount's token, read with `sudo k3s kubectl`, because
      that ServiceAccount cannot read Secrets.
    - **Operator-only:** run `host_secrets.py sync` for daniel-box and daniel-server, and
      commit both host files in this PR. It writes daniel-box's file for the first time.
      Add the `secrets` component to daniel-box's package, and daniel-box to the hosts
      `host_secrets.py check` must find. Declare `k3s_readonly_token` as a credential in
      both `package.yml` files.
    - **Operator-only**, before merging: `sudo ls -l` the server CA file on both nodes
      (`/var/lib/rancher/k3s/agent/server-ca.crt` is the expected path) and fix the
      component's CA path to what it shows.
    - Add two read-only grants to the `cluster-base` role, which owns the read-only
      account from H1.2 (Baseline):
        - a Role and a RoleBinding, both named `homelab-readonly-kcm-lease`, in
          `kube-system`, in that role's RBAC template. They grant `get` on `leases` in
          `coordination.k8s.io`, with `resourceNames: [kube-controller-manager]`. The
          RoleBinding's subject reads `cluster_base_readonly_sa_name` and
          `cluster_base_readonly_sa_namespace`;
        - `upgrade.cattle.io`, appended to `k3s_readonly_crd_api_groups` in that role's
          defaults, for host mode's Plan reads.
    - These land through promote, like any `cluster-base` change, and no Ansible task
      writes them (C4). After promotion, an agent runs the four `kubectl auth can-i`
      checks in *Verification*, and each must print `yes`. A restart-unit run before
      promotion reads Forbidden and refuses, which is the closed direction.
    - Add the `k3s-restart` component to both packages: the script, the unit, the timer with
      its enable link, `AccuracySec=1s` and `RandomizedDelaySec=0`, and the credential in
      H4's form, `LoadCredential=k3s_readonly_token:/run/credstore/k3s_readonly_token` with
      `Wants=` and `After=homelab-secrets.service`. On daniel-box it also ships the lock
      file's `tmpfiles.d` entry. Each `package.yml` sets its own `k3s_restart_window`. Add
      the `windowed` mark for `k3s-agent.service` to daniel-server's `package.yml`.
    - Add the other three alert rules and their promtool tests.
    - Add the window-derivation test.
    - The first run on each host records the hash, writes the three series and restarts
      nothing. It runs at the next window, or sooner when the operator runs `sudo
      systemctl start homelab-k3s-restart.service`, which on a first run only records.
    - Before H6.4a merges, `probe.py metric` must list `homelab_k3s_restart_failed` for
      both `daniel-box` and `daniel-server`, grouped by the `host` label the alerts read
      (*Verification*). A missing host means its textfile never reached Prometheus, and
      `K3sConfigRestartFailed` would read empty for it.
8. **H6.3c Server Plan to daniel-box (about 120 lines).**
    - **Operator-only**, before merging: for each read the gates make, run `sudo k3s kubectl
      auth can-i <verb> <resource> --as=system:serviceaccount:system-upgrade:k3s-plan`,
      with `-n kube-system` for the CoreDNS Deployment and the `kube-controller-manager`
      Lease. Each must print `yes`. Impersonation
      needs more than the read-only ServiceAccount holds, so this is not an agent step.
    - Set `system_upgrade_nodes: [daniel-box]`.
    - Change `server.yml`'s install guard. The version clause goes. A missing binary runs
      the full install. A missing argument re-runs the installer with
      `INSTALL_K3S_SKIP_DOWNLOAD=true`, which rewrites the unit and never the binary. Flag
      changes therefore still reach daniel-box until H6.4b.
    - Drop `k3s_version` from `_K3S_CONTROL_PLANE_GATES`.
    - Add the no-downgrade check.
    - Rewrite the server half of `docs/k3s-upgrade.md`.
    - Two-writer check: the server's version has one writer, SUC.
9. **H6.3d Agent Plan to daniel-server (about 80 lines).**
    - Before merging, read `sha256sum /usr/local/bin/k3s` on daniel-server in one batched
      read-only call. Compare it with the release's `sha256sum-amd64.txt`. A mismatch means
      the merge restarts the agent, so the operator merges only inside the window.
    - Set `system_upgrade_nodes: [daniel-box, daniel-server]`.
    - Make the same guard change in `agent.yml`, and rewrite the agent half of the runbook.
    - Two-writer check: the agent's version has one writer, SUC.
10. **H6.3e The binary-sum check (about 150 lines).**
    - Add `k3s_binary_sha256_amd64` to the role's defaults, the co-change check in
      `scripts/validate/`, and the `binary` mode of `k3s_gates.py` with its tests
      (*The installed binary matches the release*).
    - If D5.1a1's `-verify` support exists, add the verify Jobs here. Otherwise a
      follow-up PR adds them once D5.1a1 lands (*Open questions* 14). Until then H6.6's
      verification compares both nodes' sums with the pin by hand.
    - The Jobs pass at once: daniel-box's binary matches the release (Baseline), and
      H6.3d reads daniel-server's.
11. **H6.4a Server files to the package (about 180 lines; forward-only).**
    - Add the `k3s-server` component with the same-path files, the audit log directory, the
      logging lines of `50-homelab.conf`, `/etc/logrotate.d/k3s` and the `tmpfiles.d` entry.
      Add the legacy entry for the server's `10-log-to-file.conf`.
    - Add `units: {k3s.service: {policy: windowed}}` to daniel-box's `package.yml` in the
      same PR, because this PR ships the first `k3s.service` drop-in (H6.2's amendment).
    - Delete these from `server.yml`: the audit directory, audit policy, authentication
      config and `registries.yaml` tasks; the `unit-logging.yml` import; and `Restart k3s so
      the log drop-in takes effect`. Delete the `.skip` task from `coredns.yml`.
      `unit-logging.yml` stays for `agent.yml`.
    - Add the test that every path a k3s flag or `ExecStart` names is a file daniel-box's
      package ships.
    - Move or copy the variables these files read, as *Flags move from the unit to a
      package drop-in* lists for H6.4a. Extend H1's *Mirrored identity keys* test to the
      component's pair. Add a host-render test that renders the `k3s-server` component for
      daniel-box with an undefined-variable error, and a fixture template naming an
      undefined key fails it.
    - Add `K3sConfigRestartMetricsAbsent` and its promtool test (*Alerts*).
    - Two-writer check: the census in H3.2 fails if any deleted task's `dest:` survives.
    - The next window restarts k3s once, because the unit drop-in joins the hash set. That
      is the first unattended daniel-box API restart, so the operator merges H6.4a on a
      weekday and is present for that window.
    - During that restart, read kustomize-controller's events for any `HealthCheckFailed`
      on every Kustomization. Host mode's in-flight check keeps a promotion's health poll
      away from this restart, so an empty read shows only that the check held. It is not
      evidence of how Flux's `wait` treats an API restart, and *Risks* keeps that question
      open for H6.6.
12. **H6.4b Server flags to the package (about 150 lines).**
    - Add `k3s_server_config`, the config drop-in, and the `ExecStart` lines of
      `50-homelab.conf`.
    - Remove the installer's args clause from `server.yml`, and delete `k3s_server_args`
      with the `setup/k3s` copies of `k3s_audit_policy_path` and
      `k3s_authentication_config_path`. The three `k3s_audit_log_max*` keys move to the
      component.
      The fresh-host install passes `INSTALL_K3S_EXEC=server`, and asserts the config
      drop-in exists, so a fresh host installs the package first. H7 replaces this path.
    - Add the migration test. It compares the rendered drop-in with a fixture of the flags
      `k3s_server_args` rendered at the merge base. Add the test that holds
      `secrets-encryption` equal to `k3s_secrets_encryption`.
    - The next window gives one restart, which is the migration restart. `k3s.service` then
      runs `k3s server` with its flags from `config.yaml.d`.
    - During that restart, read kustomize-controller's events again, as in H6.4a, with the
      same limit on what an empty read shows.
13. **H6.5a Agent files to the package (about 120 lines; forward-only for
    `registries.yaml`).**
    - Add the `k3s-agent` component with `registries.yaml`, the logging lines,
      `/etc/logrotate.d/k3s-agent` and the legacy entry for the agent's `10-log-to-file.conf`.
    - Delete these from `agent.yml`: the `unit-logging.yml` import, `Restart k3s-agent so the
      log drop-in takes effect`, `Create the k3s config directory`, `Configure containerd's
      view of the in-cluster registry`, and `Restart the agent so containerd re-reads
      registries.yaml`. Delete `unit-logging.yml`, which nothing imports any more.
    - The host-render test from H6.4a gains the `k3s-agent` component for daniel-server.
    - Two-writer check: the H3.2 census, as in H6.4a.
14. **H6.5b Agent flags to the package (about 100 lines).**
    - Add `k3s_agent_config`, the config drop-in, and the `ExecStart` lines.
      `EnvironmentFile=` is left alone.
    - Remove the args clause from `agent.yml`, and delete `k3s_agent_args`.
    - The `Read the join credentials` play stays for a fresh join until H7.
    - The next window restarts `k3s-agent` once.
15. **H6.6 The patch bump.**
    - The operator takes the next Renovate k3s patch PR. In the PR it finishes the installer
      sha256, `k3s_binary_sha256_amd64`, the vendored schemas, and H1's CoreDNS pin and
      excerpt, as the Renovate rule's
      notes require.
    - The operator lands it with `land.sh`, which waits on promotion. SUC upgrades the server,
      then the agent, each behind its gates.
    - A minor bump, unlike this patch bump, first passes H6.1b2's rehearsal, and its PR body
      quotes the result (*Rehearsal in a drill guest*).
16. **H6.7 The harmless flag change.**
    - Append `image-gc-high-threshold=85` to the `kubelet-arg` list in
      `k3s_server_config`, in `hosts/components/k3s-server/component.yml`. 85 is the
      kubelet's default value.
    - Observe exactly one restart at the next window.

## Verification

```bash
# After H6.3b promotes and each host's first run: the grants, then the textfile series.
kubectl auth can-i get leases/kube-controller-manager -n kube-system
kubectl auth can-i list plans.upgrade.cattle.io -n system-upgrade
kubectl auth can-i list kustomizations.kustomize.toolkit.fluxcd.io -n flux-system
kubectl auth can-i list jobs -n system-upgrade
uv run python scripts/diagnostics/probe.py metric 'count by (host) (homelab_k3s_restart_failed)'

# Before H6.3c and H6.3d merge: record the pre-upgrade state (runbook, *Record pre-upgrade state*).
kubectl get nodes -o wide
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,STATE:.status.state,ROBUST:.status.robustness'
kubectl get svc -A --field-selector spec.type=LoadBalancer -o wide
uv run python scripts/diagnostics/probe.py monitors
systemctl show k3s -p ExecMainStartTimestamp
ssh daniel-server 'systemctl show k3s-agent -p ExecMainStartTimestamp; sha256sum /usr/local/bin/k3s'

# Exit 1, after H6.3c and H6.3d: each node recorded the Plan's hash, and nothing restarted.
# The node label is the evidence; the Plan's Complete and flux/system-upgrade can be stale.
kubectl -n system-upgrade get plans -o custom-columns='NAME:.metadata.name,HASH:.status.latestHash,VERSION:.status.latestVersion,COMPLETE:.status.conditions[?(@.type=="Complete")].status'
kubectl get nodes -L plan.upgrade.cattle.io/k3s-server -L plan.upgrade.cattle.io/k3s-agent
kubectl -n system-upgrade get jobs -o custom-columns=NAME:.metadata.name,DONE:.status.completionTime
kubectl get nodes -o custom-columns=NAME:.metadata.name,VERSION:.status.nodeInfo.kubeletVersion

# Exit 2, after H6.6 (run within 15 minutes for the Job evidence; the unit timestamps last).
kubectl -n system-upgrade get pods -o \
  custom-columns='NAME:.metadata.name,PREPARE_DONE:.status.initContainerStatuses[0].state.terminated.finishedAt'
kubectl -n system-upgrade get jobs -o custom-columns=NAME:.metadata.name,DONE:.status.completionTime
kubectl -n kube-system get lease kube-controller-manager \
  -o custom-columns='HOLDER:.spec.holderIdentity,ACQUIRED:.spec.acquireTime,TRANSITIONS:.spec.leaseTransitions'
systemctl show k3s -p ExecMainStartTimestamp
ssh daniel-server 'systemctl show k3s-agent -p ExecMainStartTimestamp; sha256sum /usr/local/bin/k3s'
sha256sum /usr/local/bin/k3s
uv run python scripts/diagnostics/probe.py health system-upgrade
uv run python scripts/diagnostics/probe.py landing

# Exit 2, the runbook's *Verify the change, not just the workload* checks.
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,STATE:.status.state,ROBUST:.status.robustness'
uv run python scripts/diagnostics/probe.py monitors
uv run python scripts/diagnostics/probe.py targets
# Pi-hole's VIP, from whichever node is NOT its MetalLB announcer.
dig +short @10.0.0.243 <a-known-name>

# Exit 3, after H6.4b and H6.7: exactly one restart, inside the window.
journalctl -u homelab-k3s-restart --since '<merge time>' --no-pager -p notice
systemctl show k3s -p ExecMainStartTimestamp -p ExecStart
```

The expected results are:

- after H6.3b promotes and each host has run once, each `can-i` prints `yes`, and the
  metric query lists both `daniel-box` and `daniel-server`. H6.4a does not merge before
  both hold;
- after H6.3c, daniel-box's `plan.upgrade.cattle.io/k3s-server` label equals the server
  Plan's `latestHash`, and after H6.3d daniel-server's `k3s-agent` label equals the agent
  Plan's. Within 15 minutes of the merge, each Job shows a `completionTime`. Both
  `ExecMainStartTimestamp` values equal the recorded ones. A `success` on
  `flux/system-upgrade` alone proves nothing for these two PRs;
- after H6.6, both nodes show the bumped version, each node's label equals its Plan's new
  `latestHash`, and the `k3s-agent` `prepare` finished after the `k3s-server` Job's
  `completionTime`;
- after H6.6, both `ExecMainStartTimestamp` values differ from the recorded ones, and
  daniel-box's new `k3s` value is earlier than daniel-server's new `k3s-agent` value. The
  Lease's `acquireTime` is at or after daniel-box's new value, and daniel-server's new
  value is at least 15 minutes after that `acquireTime`. These are the server-first
  evidence. daniel-box's `Ready` `lastTransitionTime` is not read (Baseline);
- after H6.6, `flux/system-upgrade` reads `success` on a revision R that equals the bump
  commit or descends from it, and `probe.py landing` shows `last-good` covering R;
- after H6.6, both nodes' `sha256sum /usr/local/bin/k3s` equals `k3s_binary_sha256_amd64`,
  and once H6.3e's Jobs exist, `flux/system-upgrade-verify` reads `success` on R;
- after H6.6, the volume count and robustness match the pre-upgrade capture, `monitors`
  shows nothing new, the VIP answers, and `probe.py landing` shows `DEPLOY_HOLD` reading
  `none`, the no-hold value (D1). An empty or missing value is an error, not a pass;
- after H6.4b and H6.7, the restart unit logged one `restarting` line in the window, and
  `ExecMainStartTimestamp` moved once;
- after H6.4b, `ExecStart` carries no `--node-ip`.

`journalctl -u k3s | grep 'Started k3s.service'` is not used. systemd logs that line at info,
which journald does not store on these hosts, so it reads zero on every run.

**Red proofs for the checks this slice adds.**

- *Images by digest (H6.1b).* A fixture role with one tag-only image fails the render test
  and names the image.
- *Default-deny ingress (H6.1b).* A fixture render without the policy fails its test.
- *Binary sum (H6.3e).* The `binary` mode's tests use fixture binaries and node fixtures. A
  node at the pinned version whose binary has a different sum fails and names the node. A
  node at another version reads pending and does not fail. The co-change check refuses a fixture diff that raises
  `k3s_version` and leaves the sum.
- *Rehearsal (H6.1b2).* The drill Plan's run in the guest is the rehearsal's own red: its
  Kustomization must read `failed`. The teardown tests fail on a fixture exit path that
  leaves the domain defined.
- *Plan health.* PR H6.1c makes `flux/system-upgrade` red on production and sets
  `DEPLOY_HOLD`. A static test asserts the rendered Kustomization carries the three
  expressions with the observed reason string. An expression test evaluates them with
  cel-go against Plan fixtures:
    - `Complete=True` with a stale `latestVersion` reads InProgress. This is the red
      fixture for the stale-status race;
    - a Plan with no `status` reads InProgress;
    - `Complete=True` with `latestVersion` in SUC's munged form, `v1.37.1-k3s1`, reads
      Current;
    - a stale `latestVersion` beside an old `JobFailed` reads InProgress, not Failed.
- *Job alert.* H6.1b's promtool test fires `K3sUpgradeJobFailed` on a `kube_job_failed`
  series with `condition="true"`, keeps it firing after the series disappears, and does
  not fire on a `kube_job_status_failed` pod count alone. Promtool uses synthetic series,
  so it cannot prove the series exist. Two live proofs cover that: H6.1b's query must find
  `kube_job_failed` for namespace `homelab`, and H6.1c must see the alert firing. A
  render test holds `jobs` in kube-state-metrics' `--resources` and in its ClusterRole
  together.
- *Plan access.* A render test asserts both Plans name `k3s-plan`, and that its bindings
  grant exactly the verbs and resources *The gate's ServiceAccount* lists. It also asserts
  that `prepare` declares no `volumes` entry and runs from `/host/usr/lib/homelab/`, and
  that every Plan's `version` renders `k3s_version`.
- *Pin routing.* A diff that changes only `k3s_version` in the `system-upgrade` defaults
  derives the Flux-owned tag `system-upgrade` (`land_tags.py:derive`) and no setup role
  (`deploy_changes.py:setup_roles_for`). The same diff applied to
  `ansible/roles/setup/k3s/defaults/main.yml` derives setup role `k3s`, and the test asserts
  that too.
- *No downgrade.* Lowering `v1.37.1+k3s1` to `v1.37.0+k3s1` is flagged. The same decrease
  with `k3s_downgrade_acknowledged: v1.37.0+k3s1` is clean. A raise that leaves the key set
  is flagged.
- *Gate parity.* A fixture with one degraded volume is refused by
  `gate_runner.py:unsafe_volumes` and by `k3s_gates.py`. A copy of `k3s_gates.py` patched to
  accept it fails the parity test. A fixture with zero volumes, or without `daniel-server`,
  is refused. The parity test also covers `k3s_upgrade.py:held_sha`.
- *Server-restart guard.* H6.3a's gate tests feed `k3s_gates.py` a Lease whose
  `acquireTime` is 5 minutes old beside a daniel-box `Ready` condition that last
  transitioned months ago, and the guard must refuse. On daniel-server the guard compares
  daniel-box's Lease timestamps with the local clock, and NTP skew is small next to its
  15-minute and 30-second bounds.
- *Host reads under `/host`.* A fixture host root whose `var/lock` is an absolute link to
  `/run/lock` resolves the lock under the fixture, not under the test's own `/run/lock`.
  An absent `hold_sha` passes, and an unreadable `gitops-deploy` directory refuses.
- *Restart marker.* A fixture host root holding `run/homelab-k3s-restart/` makes both the
  `server` and the `agent` mode refuse, and the same root without it passes. Host mode
  passes with the directory present.
- *Lock opener.* An AST test over `k3s_gates.py` and the restart script fails on any
  `os.open` that passes `O_CREAT` or any write-mode `open` of the lock path. A copy that
  adds `O_CREAT` fails it. A fixture lock file owned by another user, in a sticky
  directory, is opened and `flock`ed.
- *Agent deadline.* A render test asserts the agent Plan's `jobActiveDeadlineSecs` is at
  least the server's plus 1800, that the controller's maximum admits it, and that
  `flux_timeout` exceeds it.
- *Windowed restart.* Unit tests with a stub `systemctl` and a stub API cover these cases:
    - the first run records the hash and restarts nothing;
    - an unchanged hash restarts nothing;
    - a changed hash with a refusing gate writes `pending 1` and restarts nothing;
    - an agent-host run whose credential or CA file is missing, or that gets 401, or cannot
      connect, refuses;
    - on daniel-box, a held deploy lock refuses, and so does an absent lock file;
    - on daniel-server, a Lease whose `acquireTime` is 5 minutes old refuses, while the
      stub's daniel-box `Ready` condition last transitioned months ago. This is the red
      fixture for the server-restart guard. A Lease that is Forbidden, has no
      `holderIdentity`, or has a `renewTime` older than its duration plus 30 seconds also
      refuses. A Lease acquired 20 minutes ago and renewed 5 seconds ago passes;
    - passing gates restart once, and a poll that shows `active`, a new `MainPID` and a
      heartbeat after T0 writes `applied.sha256`;
    - the stub API keeps returning the pre-restart `Ready=True` with an old heartbeat, and
      the stub `systemctl` never prints `active`. The run writes `failed 1` and
      `failed.sha256`, and the next run skips that hash;
    - the stub `systemctl` prints `active` with an unchanged `MainPID`. The run writes
      `failed 1` and `failed.sha256`;
    - the unit file carries
      `LoadCredential=k3s_readonly_token:/run/credstore/k3s_readonly_token` with `Wants=`
      and `After=homelab-secrets.service`, and the timer carries `AccuracySec=1s` and
      `RandomizedDelaySec=0`. The unit also carries `RuntimeDirectory=homelab-k3s-restart`
      and the `ExecStopPost=`;
    - a failed hash H_b followed by a revert to the applied hash H_a restarts nothing and
      keeps `failed 1`. After `failed.sha256` is removed, the next run writes `failed 0`;
    - a run killed after it issued the restart leaves `attempt.sha256`. The next run
      renames it to `failed.sha256`, writes `failed 1` and restarts nothing. The
      `ExecStopPost=` with `SERVICE_RESULT=timeout` does the same rename at once;
    - a Plan with a non-empty `.status.applying` refuses, and so do a Job in
      `system-upgrade` with no `Complete` or `Failed` condition and a missing `k3s-agent`
      Plan. This is the red fixture for a SUC run in progress;
    - a Kustomization whose `Ready` is `Unknown`, or whose `lastAppliedRevision` lags its
      source's artifact revision, refuses. So do an empty Kustomization list and a list
      without `homelab`. This is the red fixture for a promotion in flight. A list where
      every Kustomization is `Ready=True` at its source's revision passes;
    - a Kustomization with `spec.suspend: true` whose `lastAppliedRevision` lags its
      source and whose `observedGeneration` is below its `generation` passes, and the run
      logs its name at notice. The same Kustomization without `spec.suspend` refuses;
    - the first run, and a refusing run, each write all three series, through a temporary
      name that does not end in `.prom`.
- *Window.* The derivation test reads three kinds of schedule:
    - every cron and timer that applies to each host, through the sources
      `scripts/docs/gen_role_glance.py` reads;
    - every timer a host package ships, read from the components each
      `hosts/<host>/package.yml` lists and its drop-ins, plus the `homelab-updater`
      package's timer under that host's drop-in. H3 onward moves timers into packages,
      and H7.19a's `homelab-checkout-sync.timer` is one. The scan must find
      `homelab-k3s-restart.timer` in both packages, which is also where it reads each
      window. A scan that misses it fails;
    - every rendered CronJob `spec.schedule` and every Longhorn RecurringJob `cron` under
      `ansible/roles/`, read in the CronJob's `timeZone` or UTC. Each scan has a named
      member that outlives M6's retirement of Kuma: the CronJob scan must find
      `homelab/configarr`, and the RecurringJob scan must find Longhorn's `daily-backup`.
      A scan that misses its member fails.
    - Each window runs from its start to its start plus the restart timer's
      `AccuracySec` plus `TimeoutStartSec`. Each other timer's fire is widened by its own
      `AccuracySec` and `RandomizedDelaySec`. The test asserts that no fire overlaps a
      window. Because an agent restart degrades the volumes daniel-box's crons read,
      daniel-server's window is checked against daniel-box's schedules too. It also asserts
      that the two hosts' windows do not overlap.
    - A monotonic timer, one set by `OnBootSec` or `OnUnitActiveSec`, has no calendar
      instant, so the test cannot place it. It passes only through an explicit exemption
      list, and each entry names the exclusion that replaces the schedule check:
        - `gitops-deploy.timer`, against daniel-box's window: the `all` deploy lock (step
          5). H7.19a's tick-stopping PR removes the entry with the lock (*Open questions*
          10). Against daniel-server's window no lock applies, and *Risks* names that
          residual. A monotonic timer that replaces the tick, such as H7's
          `homelab-checkout-sync.timer`, gets no entry by inheritance. It either uses
          `OnCalendar` or joins this list with its own exclusion;
        - `homelab-updater.timer`, on both hosts. H3 gives daniel-box and daniel-server
          the shared schedule, every 5 minutes from boot, so it overlaps every window.
          The exclusion is the `windowed` mark: reconcile never restarts `k3s` or
          `k3s-agent` (H3, *The reconcile unit*, step 3). An install that lands during a
          run can change a hashed file after step 1. The run then records the older hash,
          and the next window restarts once more, behind the same gates. A host that
          arms no updater yet has nothing to exempt, and the scan checks the timer once
          a package arms it;
        - `claude-cgroup-metrics.timer`, `claude-clone-sync.timer`,
          `claude-dotfiles-sync.timer` and `claude-memory-sync.timer`. Three sync a git
          clone, the dotfiles or the memory store (the claude_code role's contracts), and
          the fourth writes the Claude cgroups' metrics. None restarts or reconfigures k3s.
          A sync that fails during a restart can page once, as its contract says a failure
          does, and then repeats at its next fire.
    - Red fixtures: a host cron at `*/5`; a daniel-server window at 11:36; a fixture
      CronJob at `55 11 * * *`; a restart timer without `AccuracySec`, whose one-minute
      default carries the 11:32 window to 11:40:00; and an unlisted timer with
      `OnUnitActiveSec=10min`. Each fails it.
    - H2 moves host crons into CronJobs after this test exists. Each H2 PR that adds a
      CronJob re-runs it in CI, so a moved cron is still checked.
- *Files the flags name.* Removing the authentication config from the `k3s-server` component
  fails the test that every path a flag names is shipped.
- *Flag migration.* Each of these fixture drop-ins is flagged:
    - one missing `secrets-encryption`;
    - one whose `node-ip` differs from `server_ip`;
    - one whose `kubelet-arg` list lost a member;
    - one with an unquoted `write-kubeconfig-mode`.
- *Windowed mark.* A `package.yml` without `windowed` for its k3s unit fails the census.

## Rollback

- **H6.1a:** revert.
- **H6.1b:** revert. The `system-upgrade` tag renders `prune: false`, because the CRD census
  (`scripts/renderer/tests/test_renderer_crd_census.py`) requires `flux_prune: false` for any
  tag that renders a CRD. Flux therefore does not prune the Plans, the CRD or the Namespace.
  **Operator-only:** after the revert lands, delete the Plans by hand with `kubectl -n
  system-upgrade delete plan k3s-server k3s-agent`, because Flux no longer owns their removal.
  Both select no node, so a Plan left live upgrades nothing, but SUC keeps reconciling it. The operator deletes the
  CRD and Namespace by hand as well if the role is abandoned. The pin
  returns to `setup/k3s`. The revert removes the `system-upgrade` entry from
  `containers_list`. D2.1c records a removed Flux-born entry by its fields too, so the
  revert reaches the same readers as H6.1b and no own tag. Before D6.12, the revert
  therefore follows H6.1b's narrow procedure: the recorded narrow output,
  D1.2's stop rule before the PR and before `land.sh`, and the same refusal exit.
- **H6.1c:** the drill's own revert is part of the step. Flux does not prune the drill Plan
  (see H6.1b above), so the revert is followed by the **operator-only** `kubectl -n
  system-upgrade delete plan k3s-redproof`.
- **H6.2:** `sudo apt-get remove homelab-daniel-box` on daniel-box. This is valid only before
  H6.4a. After H6.4a, removing the package deletes files that `ExecStart` names.
- **H6.3a and H6.3b:** revert. dpkg removes the restart unit and the script. Before H6.3c,
  no Plan selects a node, so nothing reads the script. In `cluster-base`, the revert
  removes `upgrade.cattle.io` from the read-only ClusterRole, because it edits an object the
  tag still declares. The Lease Role and its RoleBinding stay live, because `cluster-base`
  renders `prune: false` (H1, *Plumbing tags are never pruned*). They grant one read.
  Removing them is an **operator-only** delete, `sudo k3s kubectl -n kube-system delete
  role,rolebinding homelab-readonly-kcm-lease`, if the slice is abandoned.
- **H6.3c and H6.3d:** revert. The Plans stop selecting the node, and the Ansible version
  clause returns. The next `k3s-bringup.yml` run owns that node's version again.
- **H6.4b and H6.5b:** revert on master. dpkg removes the config drop-in and the
  `ExecStart` lines, and keeps the files and the logging lines from H6.4a or H6.5a.
    - The `windowed` mark stays in `package.yml`, so reconcile does not restart k3s.
    - The installer's `ExecStart` still carries every flag. Nothing rewrote it after H6.3c,
      because the args clause found nothing missing. A restart therefore runs the pre-H6.4b
      configuration, with every file it names still present.
    - **If the migration restart succeeded**, the restart unit sees the hash change and
      restarts once inside the window, behind its gates.
    - **If the migration restart failed**, the unit cannot recover the node. The reverted
      hash equals `applied.sha256`, so step 3 restarts nothing. Its gates also need the API,
      and daniel-box hosts the only API server. A hang, such as the peer-URL trap, never
      ends on its own, because k3s runs with `TimeoutStartUSec=infinity`. Pi-hole and
      Traefik stay down until the operator acts. **Operator-only** recovery, on the failed
      host:
        1. Install the reverted package. The updater fetches it, because the repository is
           a host service on daniel-box (H3, `homelab-apt-repo.service`), not a cluster
           one. If the builder has not published it, run `sudo apt-get install
           homelab-<host>=<previous version>` from the same repository.
        2. Run `sudo systemctl daemon-reload`, then `sudo systemctl restart k3s` (or
           `k3s-agent`).
        3. Verify as the runbook's *Verify the change, not just the workload* says, then
           `sudo rm /var/lib/homelab-k3s-restart/failed.sha256`.
- **H6.4a and H6.5a are forward-only.** Their revert must not land, because it deletes files
  that a flag names. To return a file to Ansible, the operator follows this attended
  procedure:
    1. Mask `homelab-k3s-restart.timer` on the host. Do not run it on a Sunday, which holds
       the weekly reboot.
    2. Land a PR that drops the file from the component and restores its Ansible task.
    3. On the host, install the new package version by hand. Then run `k3s-bringup.yml
       --tags k3s_server` (or `k3s_agent`) at once, so the file returns before any k3s
       start.
    4. Check that the file exists, then unmask the timer.
- **H6.6:** a version bump is not undone by a revert. A lowered Plan fails its Job, because
  `upgrade.sh` refuses an older binary. Within a minor release:
    1. **Operator-only:** label both nodes `homelab/k3s-upgrade-paused=true`.
    2. Run `runbook_gates.py k3s-upgrade`.
    3. **Operator-only:** on daniel-box, fetch the older release's `k3s` asset and its
       `sha256sum-amd64.txt`, and check the sum. Install the binary over
       `/usr/local/bin/k3s` with `sudo install -m 0755`, then run `sudo systemctl restart
       k3s`. Verify the server as the runbook says, then do the same on daniel-server for
       `k3s-agent`.
       - This is what `upgrade.sh` `replace_binary` does. The installer is not used here: its
         `create_env_file` rewrites `k3s-agent.service.env` from the caller's `K3S_*`
         variables, which would drop the agent's URL and token.
    4. Land a PR that lowers `k3s_version` and sets `k3s_downgrade_acknowledged` to the same
       value, and wait for promotion.
    5. **Operator-only:** remove both labels. SUC finds the binary already at the Plan's
       version, and `upgrade.sh` exits 0 without a restart.
    - Across a minor release, the binary downgrade and the etcd restore follow
      `docs/k3s-etcd-restore.md`. The pin decrease lands before the restore, so the restored
      cluster's Flux applies the older Plan rather than re-upgrading.
- **H6.7:** revert. That is one more restart in the next window.

**What a partial failure leaves live:**

- **A gate that never passes.** The Job runs out its deadline in `prepare`, 60 minutes for
  the server and 90 for the agent. The node is untouched, the Plan reads `JobFailed`,
  `flux/system-upgrade` turns red, and `promote` holds without a reset, because the tag is
  forward-only. SUC re-creates the Job after its TTL, so the gate keeps retrying while the
  operator diagnoses.
- **An image pull that fails.** The same as a refusing gate. No cordon is involved, because
  the Plans carry none.
- **The agent after a failed server upgrade.** The agent's `prepare` keeps waiting, then
  fails at its deadline. The agent stays on the old version, which is the skew-safe
  direction.
- **No reset lowers the Plans.** A failed or timed-out revision that changes the
  `system-upgrade` tag holds without a reset, under H1's forward-only hold. A failed
  revision that leaves the tag alone resets to a `last-good` whose Plans are the ones
  already applied. `promote` therefore never applies a Plan below a version a node runs.
- **A restart whose node never returns.** No poll sees `active`, a new `MainPID` and a
  heartbeat after T0, so the restart unit writes `failed 1` and does not retry that hash.
  A stale `Ready=True` from before the restart does not count as success. The node's own
  probes and `K3sConfigRestartFailed` page.
- **Wrong flags after H6.4b.** The restart fails, and the node stays down until the revert
  lands and the operator restarts k3s by hand (*Rollback*, H6.4b). The windowed unit
  cannot do that restart. That is why H6.4b lands on a weekday with the operator present
  for the next window.

## Risks

- **A control-plane restart takes DNS and ingress down.** Pi-hole and Traefik sit behind the
  server.
    - The window holds every config restart to a weekday slot between health-cron fires.
    - A k3s bump applies when it is promoted, so the operator lands it in a quiet slot,
      such as just after a `:10` boundary.
    - Every restart passes the storage gates first.
- **The deployer, until H7.19a.** D6 keeps the deployer for the host plane, and H7.19a
  retires it. A tick that overlaps a SUC server restart fails its deploy and writes
  `hold_sha`. The server `prepare` refuses while a deploy holds the `all` lock, which
  narrows that gap without closing it. The windowed restart on daniel-box holds the lock
  itself. Both open it read-only, and the `tmpfiles.d` entry creates it as the deployer's
  user, so neither can lock the deployer out of its own file (Baseline). daniel-server's
  window takes no lock. A tick that rolls a pod onto daniel-server during an agent restart
  can still fail its rollout gate and hold, as a hand restart by the runbook can.
    - **The removal order.** The seven deployer-coupled items go in H7.19a's
      tick-stopping PR, before any PR that deletes `deploy_locks.py` or
      `/var/lib/gitops-deploy` (*Open questions* 10). In the other order, the server
      `prepare` refuses on the absent state directory, so every k3s bump ends `JobFailed`
      and holds. daniel-box's windowed restart refuses on the absent lock file, so every
      config change pends. The lock-path equality test fails at import, so CI goes red.
- **H6.1b's landing applies the readers of its entry's fields.** The added entry reaches
  four reader tags of `containers_list`, three of them `k8s_autodeploy: false` (*Steps*,
  H6.1b), and the deploy plane ignores that flag. The mitigation is D1.2's: a recorded
  narrow output, the stop rule before the PR and before `land.sh`, and its refusal exit.
  The revert carries the same procedure. The risk ends at D6.12 (*Open questions* 11).
- **A Flux status that predates the apply.** The expressions close the race for a version
  change only. A selector-only change can post `success` on `flux/system-upgrade` before
  SUC runs a Job, so H6.3c, H6.3d and exit 1 verify by the node label instead.
- **A held `system-upgrade` revision stops every later promotion** until the operator clears
  it, because the tag is forward-only. That is the price of never lowering a Plan, and the
  hold pages through `DEPLOY_HOLD`. The hold carries H6's own value, never C2's
  `failed=` form, so C2's proposed revert rule cannot clear it, and a merged revert waits
  behind it. The clear is **operator-only**: `gh variable set
  DEPLOY_HOLD --body none -R DanielH2018/server`, after the cause is fixed.
- **Flux's `wait` across an API restart.** Flux's documentation does not say whether a
  transient API error during a health poll reads as `HealthCheckFailed`. If it does, a
  restart that overlaps `promote verify` turns a Kustomization red and sets `DEPLOY_HOLD`.
  On a forward-only tag that holds without a reset. On any other tag `promote` also resets
  `prod` to `last-good`, which rolls back an innocent change (D2, *Publication and
  verification*). Two kinds of restart can overlap a verify:
    - **Every windowed restart on daniel-box**, starting with H6.4a's. Host mode refuses
      while any Kustomization is not `Ready=True` at its source's revision
      (*`homelab-k3s-restart`*, step 6). That narrows the gap without closing it.
      `promote` can move `prod` after the check reads, or before the source's next poll
      fetches the new tag, and the verify then runs during the restart. The window is a
      weekday at 11:32, so a landing in progress then is the exposure. The check skips a
      suspended Kustomization, and a resume after the check reads leaves the same
      residual: the resumed Kustomization reconciles, and `verify` may then wait on it.
    - **SUC's server restart in H6.6.** kustomize-controller polls health for every
      Kustomization the revision changes: `system-upgrade`, and also `coredns`, because H1
      moves the CoreDNS pin with `k3s_version`. Both tags are forward-only. The server
      `prepare` waits for the CoreDNS rollout to finish (*Gates at apply time*), so the
      `coredns` health poll normally ends before the API restarts. A poll that lags the
      rollout can still span the restart. The in-flight check cannot apply here, because
      this revision is the one under verification.
    - H6.4a's and H6.4b's event reads show only that the in-flight check held. H6.6's
      verification is the first proof. The fallback is the same for every tag. The
      operator verifies both nodes and the CoreDNS Deployment as the runbook says. Then the
      operator clears the hold (**operator-only**) with `gh variable set DEPLOY_HOLD --body
      none -R DanielH2018/server`, and re-runs promotion.
- **The etcd peer-URL trap.** The migration test pins `node-ip` and `advertise-address` to
  `server_ip`. H6.4b changes where flags come from, not their values.
- **A hung k3s start.** `--no-block` and the 300-second poll keep the restart unit
  bounded, while `TimeoutStartSec=0` on k3s leaves k3s itself trying. A hung start reads
  `activating`, and its node posts no fresh heartbeat, so no poll succeeds. The failed
  metric pages.
- **No drain on the agent.** This matches the runbook. The agent's `prepare` waits for
  storage health after the server restart, which the runbook did by hand.
- **Image pulls from Docker Hub.** `rancher/k3s-upgrade`, the SUC image and the gate image
  come from Docker Hub, like the cluster's other public images. The SUC and gate images are
  pinned by digest. `rancher/k3s-upgrade` is pulled by the tag SUC builds from the Plan's
  version, into a privileged Job that replaces the binary (*Open questions* 12). H6.3e's
  check catches an installed binary that differs from the release before `last-good`
  moves, but only after the Job ran.
- **A rehearsal is not production.** The guest runs the server Plan without `prepare`, on
  one node with no Longhorn. It proves the Plan and the Flux apply, not the gates or the
  agent's wait. The gates rest on their fixtures and the parity test.
- **dpkg lock contention on daniel-box.** The updater's install takes the dpkg lock for
  seconds, because it writes plain files and its postinst starts reconcile with
  `--no-block`. Ansible's `apt` module waits 60 seconds by default (`lock_timeout`).
  H6.2's verification times one install on daniel-box. A collision that outlasts the wait
  fails the tick's apply and writes `hold_sha`, as any apt failure does there.
- **Failure class 2, a check that observes a proxy.** A completed Job proves the binary was
  replaced, not that the cluster is healthy. Verification compares volume robustness with
  the pre-upgrade capture, reads `probe.py monitors`, and tests the VIP from a host that is
  not the announcer.
- **Failure class 3, empty read as clean.** Every gate refuses on a read error, on a missing
  credential, on zero volumes, on a missing node, Lease or Plan, and on a missing root
  Kustomization. The old `Started
  k3s.service` count is dropped because it always reads zero. `K3sUpgradeJobFailed` would
  have read an empty series without kube-state-metrics' `jobs`, so H6.1b adds the resource
  and a live query for a named namespace. The restart unit's textfile series get the same
  treatment: every run writes them, H6.3b's live query must find both hosts, and
  `K3sConfigRestartMetricsAbsent` fires when either goes missing.
- **Failure class 5, sequences treated as transactions.** The agent waits on the server
  Plan's recorded state, not on a script finishing. The restart unit records its hash only
  after evidence the restart produced: a new `MainPID`, `active`, and a heartbeat after
  T0. H6.4a is forward-only, because its revert is not a transaction.
- **A server-restart signal that never moves.** daniel-box's `Ready` transition stayed at
  2026-08-01 across a reboot (Baseline), so a guard on it would never refuse. The guard
  reads the controller-manager Lease instead. That Lease is unmeasured until H6.3a's
  operator read, and the guard does not merge if it fails that read.
- **Failure class 7, the execution context.** daniel-server has no kubeconfig. Its gates
  read the API with a credential from `/run/credstore` and the node's own CA file, and the
  agent-host tests cover the case where that credential is missing.

## Tests and docs that change in the same PR

- **H6.1a:** the ADR.
- **H6.1b:**
    - render tests for the role, including that both Plans render `k3s_version` and an
      empty `system_upgrade_nodes` matches no node, the *Plan access* and *Agent deadline*
      tests, and the expression test with its fixtures;
    - the pin-routing test, in `ansible/tests/deploy/`;
    - `K3sUpgradeJobFailed` in `k3s-upgrade.yaml` with its promtool test, and the render
      test holding `jobs` in kube-state-metrics' flag and ClusterRole together;
    - `ansible/tests/setup/test_k3s_node_ip_pinned.py`,
      `scripts/validate/tests/test_k8s_schema.py` and H1's CoreDNS image test read the new
      pin home through the new `repo_paths` constant;
    - `ansible/tests/setup/test_setup_cross_role_files.py` admits the `include_vars`;
    - `docs/k3s-upgrade.md` (pin path, stale symbol);
    - `ansible/roles/setup/k3s/CLAUDE.md`, whose `## At a glance` block regenerates;
    - the digest and default-deny render tests, each with its red fixture.
- **H6.1b2:** the orchestrator's rehearsal-mode tests, and
  `docs/hypervisor-libvirt-internals.md` names the mode.
- **H6.1c:** the expression test records the observed reason.
- **H6.2:** `docs/host-packages.md` lists daniel-box; `ansible/.sops.yaml` gains its
  creation rule; both CI scans' named sets gain `homelab-daniel-box`.
- **H6.3a:** the gate tests, including the *Server-restart guard* red fixture, the parity
  test, the *Host reads under `/host`* test, the *Restart marker* test and the *Lock
  opener* test in `hosts/lib/tests/`.
- **H6.3b:**
    - the restart-unit tests, the window-derivation test, the promtool tests for the other
      three rules, and the test that the packaged deploy-lock path and the `tmpfiles.d`
      entry equal `deploy_locks.py:lock_path` for `SERVICE_LOCK_ALL`;
    - `ansible/tests/k8s/test_k8s_manifests_rbac.py`, which renders the RBAC template from
      `cluster-base` after H1.2, covers the Lease Role, its `resourceNames`, its
      RoleBinding's subject and `upgrade.cattle.io` in the ClusterRole's groups;
    - both host SOPS files, `host_secrets.py check`'s host set, and the credential in both
      `package.yml` files;
    - `docs/secret-rotation.md` registers `k3s_readonly_token` as `assisted`;
    - `docs/k3s-etcd-restore.md` gains the mask and unmask steps.
- **H6.3c:**
    - `ansible/tests/setup/test_k3s_version_guard.py` asserts the version clause is gone and
      the args branch sets `INSTALL_K3S_SKIP_DOWNLOAD`;
    - `ansible/roles/setup/gitops_deploy/tests/test_deploy_remediation.py` follows
      `_K3S_CONTROL_PLANE_GATES`;
    - the no-downgrade check's tests;
    - the server half of `docs/k3s-upgrade.md`, its *Rollback* section, and the rehearsal
      that every minor bump takes;
    - the k3s role `CLAUDE.md` loses the server upgrade path.
- **H6.3d:** the agent half of `docs/k3s-upgrade.md`.
- **H6.3e:** the `binary` mode's tests in `hosts/lib/tests/`, the co-change check's tests in
  `scripts/validate/tests/`, and the role `CLAUDE.md`'s note on the sum pin.
- **H6.4a:**
    - `ansible/tests/setup/test_k3s_unit_logging.py` retargets the component's logging
      lines, logrotate file and `tmpfiles.d` entry, and keeps the agent case;
    - `ansible/tests/setup/test_k3s_oidc_trusts_authelia.py` reads the component template
      and the component's defaults, where the `k3s_oidc_*` keys now live;
    - H1's *Mirrored identity keys* test gains the component's pair, and the host-render
      test is new;
    - `K3sConfigRestartMetricsAbsent` and its promtool test;
    - `docs/headlamp-oidc-and-plugins.md`, `ansible/roles/k8s/headlamp/CLAUDE.md` and the
      comments in `ansible/roles/k8s/headlamp/defaults/main.yml` name the component as the
      home of the `k3s_oidc_*` keys;
    - the shipped-paths test;
    - `docs/k3s-node-plane-crons-and-incidents.md` (the log drop-in, the gated restart
      task and the home of `k3s_oidc_issuer_urls`);
    - the `## Notable` paragraph of the k3s role `CLAUDE.md`;
    - `_K3S_GATED_TASKS` in `deploy_remediation.py` drops the log restart.
- **H6.4b:**
    - `ansible/tests/setup/test_k3s_control_plane_hardening.py` and
      `ansible/tests/setup/test_k3s_node_ip_pinned.py` read the rendered drop-in;
    - the migration test and its fixtures;
    - the test holding `secrets-encryption` equal to `k3s_secrets_encryption`;
    - `_K3S_CONTROL_PLANE_GATES` drops `k3s_server_args`;
    - `docs/k3s-node-plane-crons-and-incidents.md` (the control-plane gates).
- **H6.5a:** `test_k3s_unit_logging.py` loses its Ansible half.
- **H6.5b:**
    - `ansible/tests/setup/test_k3s_join_port_symmetry.py` keeps the agent's server URL in
      step with the env file the installer wrote;
    - `docs/hypervisor-libvirt-internals.md` and `ansible/roles/setup/hypervisor/CLAUDE.md`
      say the env file is no longer rewritten. A deliberate server-token rotation therefore
      makes the drill fail loudly until the operator updates the file.

## Complexity delta

- **Added:**
    - SUC, which is one controller and one CRD, and two Plans with health expressions;
    - one ServiceAccount, one ClusterRole, one Role and their two bindings, for the gates;
    - one Role and RoleBinding in `cluster-base` that let the read-only account read one
      Lease, and `upgrade.cattle.io` in that account's read list;
    - host mode's SUC and Flux in-flight checks, the second skipping a suspended
      Kustomization, the restart marker that `prepare` reads,
      and the restart unit's attempt marker;
    - `jobs` in kube-state-metrics' resources and grant, which adds one series family;
    - one stdlib gate script with a parity test, used by `prepare` and by the restart unit;
    - one windowed restart unit and timer per host, with five alert rules;
    - two config components and two `tmpfiles.d` entries, one for the log file and one
      for the deploy lock file;
    - daniel-box's SOPS file and its creation rule;
    - a no-downgrade check, a window-derivation test and a pin-routing test;
    - one NetworkPolicy and two render tests, for default-deny ingress and image digests;
    - one sum pin, a co-change check, a `binary` gate mode, and one verify Job per selected
      node;
    - a rehearsal mode in the etcd drill's orchestrator, run before H6.1c and before each
      minor bump;
    - one SOPS variable, `k3s_readonly_token`;
    - one ADR.
- **Removed:**
    - the hand-run server and agent upgrade sections of the runbook;
    - the `manual_plane` line every k3s bump wrote;
    - the installer's role in changing flags, its args-comparison guard and the quoting rule
      that guard needed;
    - `unit-logging.yml`, two restart-for-log tasks and seven file tasks across `server.yml`,
      `agent.yml` and `coredns.yml`.

## Open questions

1. **Upgrade order.** The design's H6 text says "upgrades the agent, then the server". Its
   data-flow box says "server first, then agent". The skew policy and `docs/k3s-upgrade.md`
   require server first. Resolution: this plan upgrades server first. The design text needs
   correcting when it moves into `docs/adr/` (H7).
2. **Dependency edges.** Closed in round c2. The contracts.md row reads `D2, H1.1a to
   H1.8, H4, M1`, which matches this plan:
    - H1, for `scripts/dev/vendor_manifest.py`, the C3 amendment that exempts SUC's CRD
      from prune (H1 *Open questions* 2), and the CoreDNS tag whose pin moves with
      `k3s_version`;
    - M1, for the rule files the alerts join.
    - No M6 edge. `K3sUpgradeJobFailed` needs kube-state-metrics' Job series, which M6's
      B7 also plans to add. H6.1b adds `jobs` itself, so H6 does not wait on M6. M6 and M2
      edit the same `--resources` line, and whichever lands later keeps the others'
      members.
3. **The window.** `k3s_restart_window` is weekdays from the 2nd to the 31st, at 11:32 on
   daniel-box and at 11:52 on daniel-server. Resolution: the operator confirms or moves
   them in H6.3b's review, and the derivation test re-checks any move.
4. **The join token.** H6 moves no token, because the agent's token is the server token
   (Baseline). Putting it in SOPS would hold the credential that `docs/break-glass.md` item 2
   keeps out of band. Resolution: H7's fresh join decides. The k3s docs allow adding an
   agent-scoped token to a running server, and the server token still joins agents, so H7
   can add `agent-token` to the server drop-in and give only that value to SOPS. Whether an
   already-joined agent accepts the switch is undocumented. H7 verifies it inside a window
   with the operator present. design.md's sentence that the token "stays in SOPS"
   contradicts break-glass item 2, and needs correcting.
5. **Per-role health expressions.** Applied. C3's *Per-role fields* lets a role set its own
   `timeout` (H1, H6) and `healthCheckExprs` for its own custom resources (H6). PR H6.1b
   implements the renderer half, so this slice does not wait on another. The promote
   timeout is D4's chain derivation (D4, *The promote timeout follows the chain*). This
   tag's `timeout` is 100 minutes, and a k3s bump also changes `coredns`. D4's queueing
   term takes the largest changed `timeout`, which for such a revision is these 100
   minutes, not tdarr's 50. D4 is asked to restate its worst case (a cross-slice request).
6. **The `promote` reset and the Plans.** Closed in round c1. H1's forward-only hold,
   which H1.1b implements and H6 already depends on, holds a failed `system-upgrade`
   revision without a reset. H6.1b sets `flux_forward_only: true`, so no reset lowers the
   Plans, and C2 needs no amendment for H6. contracts.md's register still lists "C2, a
   reset that lowers the Plans"; that row is asked to be dropped (a cross-slice request).
7. **`k3s_readonly_token` in SOPS.** Adding it is **operator-only**. Resolution: the operator
   decides in H6.3b. The alternative is no windowed restart on daniel-server, in which case
   agent flag changes stay a hand step until H7.
8. **Two restarters beside reconcile.** H4's *The restart contract* says
   `homelab-reconcile` is the only process that starts or restarts a unit outside the boot
   path. Two processes restart `k3s` or `k3s-agent`, a `windowed` unit that reconcile never
   restarts:
    - SUC's upgrade container, after it replaces the binary. It runs behind `prepare`'s
      gates, whenever `promote` applies a changed Plan, so outside any window;
    - `homelab-k3s-restart`, inside its window and behind its own gates.
    - The two exclude each other on a node. `prepare` refuses while the unit's runtime
      directory exists, and the unit refuses while a Plan is applying or a SUC Job runs
      (*Gates at apply time*).
   Resolution: amend H4's contract to name both as the only restarters of a `windowed` k3s
   unit, each under its stated condition. H6.3b does not merge before that amendment
   lands.
9. **The server-restart signal.** The guard reads the `kube-controller-manager` Lease's
   `acquireTime`, which upstream source says moves on every k3s server start. No agent can
   measure it, because the read-only account cannot read Leases before H6.3b. Resolution:
   H6.3a's operator read records it. If `acquireTime` predates the k3s start, the
   server-restart guard does not merge. The windows' 20-minute spacing and the agent
   `prepare`'s storage gates then remain, and this question names the replacement
   signal before H6.3b.
10. **Who removes the deployer-coupled checks.** Seven items read the deployer: the server
    `prepare`'s hold and lock checks, the restart unit's step 5, daniel-box's lock
    `tmpfiles.d` entry, the lock-path equality test, the `held_sha` case of the parity
    test, and the window test's exemption for `gitops-deploy.timer`. D6 keeps the deployer
    for the host plane (D6, *What stays until H7*), and H7.19a retires it (H7's
    `gitops_deploy` row). contracts.md's H7 row already depends on H6, so no edge is
    missing. Resolution: H7.19a's fifth PR, the one that stops the tick, removes all
    seven. It names each: the server-mode hold and `all`-lock checks in `k3s_gates.py`,
    step 5 of `homelab-k3s-restart`, the `tmpfiles.d` lock entry, the lock-path equality
    test, the `held_sha` parity case, and the `gitops-deploy.timer` window exemption. That
    PR lands before any PR that deletes `deploy_locks.py` or `/var/lib/gitops-deploy`. The
    hold check refuses an absent state directory, as `k3s_upgrade.py:held_sha` does, and
    the equality test imports `deploy_locks.py`, so neither can outlive the deployer
    (*Risks*, *The removal order*). H7 is asked to add that step. H7 says
    `homelab-checkout-sync.timer` fires every 10 minutes, as the tick does. If it is
    monotonic like the tick, the window test fails it, because the test reads host-package
    timers (*Verification*, *Window*). It must either use `OnCalendar` or join the
    exemption list with its own exclusion.
11. **An added `containers_list` entry and the narrowing.** H6.1b adds the `system-upgrade`
    entry, and its revert removes it. Settled by D2.1c, and the own-tag request is
    withdrawn. D2 refuses the own-tag mapping because the bare-entry render moves
    artifacts' `known_services.json` (D2 Open question 10). D2.1c instead records the entry
    by its fields, so before D6.12 the deployer narrows either PR to the readers of those
    fields: artifacts, blackbox-exporter, pihole and uptime-kuma at `a5abf8581`
    (*Steps*, H6.1b). Resolution: H6.1b and its revert record their narrow output and carry
    D1.2's stop rule and refusal exit over every tag it names. D2.1c's render census,
    `ansible/tests/k8s/test_flux_born_entry_reader_census.py`, proves the expected shape.
12. **`rancher/k3s-upgrade` by digest.** SUC builds the upgrade container's reference from
    `upgrade.image` and the Plan's version, so the Plans name the image by tag. This plan
    cannot confirm whether SUC v0.20.2 accepts a digest there. Recommendation: before
    H6.3c, read SUC's job and plan source at the pinned version. If it accepts a digest,
    pin by digest. If not, mirror `rancher/k3s-upgrade` at each pinned version into the
    in-cluster registry, check the mirrored digest against the one the bump PR records,
    and point `upgrade.image` at the mirror. H6.3e's sum check stays in both cases.
    Resolution: the H6.3c PR body quotes the source read and names the choice.
13. **A probe row for `system-upgrade`.** netpol-baseline's probe fails every deploy on a
    target with no ready endpoint, so a Plan Job's transient pod can never be one. The
    controller is the only standing pod. Recommendation: if the vendored manifest declares
    a container port, add a `refused` row for it with `policy: default-deny-ingress`, and
    teach the probe its namespace. If it declares none, nothing in the namespace accepts a
    connection, and the render test is the whole proof. Resolution: H6.1b quotes the
    ports and takes the matching branch.
14. **The verify Kustomization is a D5 feature.** H6.3e's gate runs in
    `system-upgrade-verify`, and C3 makes `-verify` available from D5.1a1. contracts.md's
    H6 row names D2, H1.1a to H1.8, H4 and M1. Recommendation: add D5.1a1 as an edge of
    the verify Jobs only, not of H6 as a whole. H6.3e's pin, co-change check and mode land
    without it, and until the Jobs exist H6.6 compares the sums by hand. Resolved in the
    pillar review fold: the index's H6 row carries "H6.3e's verify Jobs also D5.1a1".
15. **A `ghcr.io` mirror in `registries.yaml`.** D5 Open question 2 recommends keeping the
    in-cluster registry as a pull-through mirror for GHCR. Images then still pull while the
    WAN is down, and the break-glass promotion has a registry to push to. If the operator
    accepts it, the k3s `registries.yaml` that H6's config drop-ins carry gains a
    `mirrors."ghcr.io"` entry pointing at that registry. Resolution: the PR that carries the
    entry lands after D5 Open question 2 is ruled, and before nut's Wave C PR.
16. **The Job backoff limit (opened 2026-10-24, for H6.3c).** The vendored controller manifest
    sets `SYSTEM_UPGRADE_JOB_BACKOFF_LIMIT: "99"` in `default-controller-env`
    (`ansible/roles/k8s/system-upgrade/files/upstream/system-upgrade-controller.yaml:201`).
    The Baseline and *Gates at apply time* say a failing `prepare` ends in `JobFailed` after
    three pods, from `job.go`'s `BackoffLimit` 2. If v0.20.2's `job.go` reads that variable,
    a failing `prepare` retries until the Job deadline instead, and `JobFailed` arrives only
    at 3600 or 5400 seconds. That changes the agent-deadline argument, the timing of
    `K3sUpgradeJobFailed` and the red-path drill's expected time to red. Resolution: before
    H6.3c widens a Plan, read `job.go` at the pinned version, then either restate those three
    places or set the variable in `files/patches/system-upgrade-controller-env.yaml`.

## Review log

### Round 1 (per-slice adversarial review)

Raised 23 findings: 2 critical, 11 high and 10 medium. 22 were fixed and one medium
(R1-22) was partly fixed. R1-02 was fixed by a third path, not by either option the finding
offered. Refuted: none.

| ID | Severity | Title | Outcome |
|---|---|---|---|
| H6-R1-01 | critical | The H6.4/H6.3 rollback deleted files the installer's `ExecStart` names | Fixed |
| H6-R1-02 | critical | `k3s_agent_join_token` was the cluster server token | Fixed |
| H6-R1-03 | high | H6.1 ran "on stage first", and no stage cluster exists | Fixed |
| H6-R1-04 | high | The pin's location made the Plans unrenderable and every bump a hand apply | Fixed |
| H6-R1-05 | high | Missing dependency on H1 | Fixed |
| H6-R1-06 | high | The `policy.py` gate is inert for k3s landings and gates the merge, not the apply | Fixed |
| H6-R1-07 | high | daniel-server's restart gates had no cluster credential | Fixed |
| H6-R1-08 | high | The exit check compared Job start times, but `prepare` runs inside the Job | Fixed |
| H6-R1-09 | high | A failed Job leaves a node cordoned, and C2's reset can cause one | Fixed |
| H6-R1-10 | high | Plan outcomes did not reach the `flux/*` statuses | Fixed |
| H6-R1-11 | high | No storage check between the server and agent upgrades | Fixed |
| H6-R1-12 | high | The H6.6 rollback and the no-downgrade rationale rested on false premises | Fixed |
| H6-R1-13 | high | H6.5 had no two-writer census, and deleting `unit-logging.yml` broke `agent.yml` | Fixed |
| H6-R1-14 | medium | H6.1 made flag changes inert until H6.4 | Fixed |
| H6-R1-15 | medium | The 11:30 window collided with the `*/10` and `*/30` crons | Fixed |
| H6-R1-16 | medium | The restart unit could hang, retry daily and fail silently | Fixed |
| H6-R1-17 | medium | No pause, no autonomous-role contract, no operator approval | Fixed |
| H6-R1-18 | medium | Deleting `unit-logging.yml` orphaned logrotate and the log file | Fixed |
| H6-R1-19 | medium | The flag translation was underspecified | Fixed |
| H6-R1-20 | medium | Verification did not check what the class-2 risk claimed | Fixed |
| H6-R1-21 | medium | Two PRs exceeded the size budget | Fixed |
| H6-R1-22 | medium | The daniel-box updater contends for the dpkg lock | Partly fixed |
| H6-R1-23 | medium | The same-PR docs list was incomplete | Fixed |

How each was fixed, with the evidence measured at `70591c307`:

- **R1-01.** Confirmed by the `server.yml` task `Write the apiserver authentication
  configuration`, which says a missing file wedges the node. The fix has four parts.
    - H6.4a and H6.5a ship the same-path files and are forward-only. Their return to Ansible
      is an attended procedure.
    - H6.4b reverts only the config drop-in and the `ExecStart` lines.
    - The `windowed` mark moved into `package.yml`, so a component revert cannot drop it.
    - A new test asserts that every path a flag names is shipped. H6.2's `apt-get remove` is
      valid only before H6.4a.
- **R1-02.** Confirmed by `docs/k3s-etcd-restore.md` and `etcd-restore-drill-vm.sh.j2`
  (`AGENT_ENV`). Fixed by a third path: H6 moves no token.
    - Not option (a): the k3s docs allow an agent token on a running server, but say nothing
      about an already-joined agent switching to it. That belongs in H7's fresh-join design,
      where the switch is tested with the operator present.
    - Not option (b): `docs/break-glass.md` item 2 keeps the server token out of band, and
      putting it in SOPS reverses that.
    - No drill repoint is needed. The agent's drop-in leaves `EnvironmentFile=` alone, so
      the installer's env file stays the agent's live credential and the drill's source,
      unchanged. H6.5b's doc change says the file is no longer rewritten.
    - *Open questions* 4 hands the agent-scoped token to H7 with the docs evidence, and
      flags the design.md sentence.
- **R1-03.** Confirmed by D2's Baseline and `kubectl.py:RETIRED_CLUSTER_NODES`. The stage step
  is gone. The Plans land selecting no node (H6.1b), then widen one node per PR (H6.3c,
  H6.3d), and `cordon` is dropped.
- **R1-04.** Confirmed: `git grep` finds one definition, `tick_applies_setup_role` is false for
  `k3s`, and `land_tags.py` says the tick defers. The pin and its two siblings move to the
  `system-upgrade` defaults. A pin-routing test with its red pair proves the change routes
  to the Flux-owned tag.
- **R1-05.** Confirmed by H1's *CoreDNS without a host read* and its *Open questions* 2.
  `Depends` and *Open questions* 2 now name H1 and M1, and cite the C3 amendment.
- **R1-06.** Confirmed by the `policy.py` docstring and the `policy_active` guard in
  `merge.py`. The merge-time rule is gone. The gates run in each Plan's `prepare`, so they
  hold whatever path merged the PR.
- **R1-07.** Confirmed: `kubeconfig.yml` is imported only by `tasks/main.yml`, with server
  `127.0.0.1`. The restart unit reads the API with `k3s_readonly_token` from
  `/run/credstore` (an **operator-only** key) and the node's CA. It refuses when unreadable,
  and the agent-host cases are tested. Failure class 7 is named.
- **R1-08.** Confirmed in SUC's `job.go`: `prepare` is an init container of the same Job, and
  Jobs are deleted 900 seconds after they finish. The exit check now compares the unit
  timestamps with daniel-box's Ready transition, and the `prepare` finish with the server
  Job's completion while the Job exists.
- **R1-09.** Confirmed in SUC's `handle_batch.go`, which uncordons only on completion.
  `cordon` is dropped. The reset loop is named under partial failures, and raised as
  *Open questions* 6.
- **R1-10.** The Kustomization reads the Plans through `healthCheckExprs`, with a 70-minute
  timeout. H6.1b adds the renderer support, and *Open questions* 5 raises the C3 field.
  H6.1c is the red proof on production, and it requires the red within minutes, so the
  `failed` expression is what it proves. The open behaviour of Flux's `wait` across an API
  restart is a named risk, with H6.4b's restart as early evidence.
- **R1-11.** Confirmed: upstream's `prepare` checks only the server Plan and the
  control-plane versions. The agent's `prepare` now also waits for storage health and
  CoreDNS, and Verification carries the runbook's checks.
- **R1-12.** Confirmed in `upgrade.sh` `replace_binary` and the runbook's *Rollback*. The
  rewritten H6.6 rollback pauses SUC, downgrades by hand, then lands an acknowledged
  decrease. The no-downgrade rationale now cites `upgrade.sh`.
- **R1-13.** Confirmed in `agent.yml`. H6.5a names each deleted task and the legacy entry,
  and carries a two-writer check. `unit-logging.yml` survives H6.4a for `agent.yml`, and both
  log-restart tasks go with their imports.
- **R1-14.** H6.3c keeps the args clause and adds `INSTALL_K3S_SKIP_DOWNLOAD=true`. H6.4b
  then removes it.
- **R1-15.** The window is 11:32 to 11:39 on weekdays, from the 2nd to the 31st. A test
  derives it from every schedule on each host.
- **R1-16.** The unit uses `--no-block`, a 300-second Ready poll, `TimeoutStartSec=7min`,
  one attempt per hash, a failed metric and two alerts.
- **R1-17.** H6.1a is an ADR for **operator-only** approval. The role carries an
  autonomous-role contract, *Pause and resume* lists four levers with their reverse, and
  `docs/k3s-etcd-restore.md` gains the mask steps.
- **R1-18.** The components ship both logrotate files at the same path, and a `tmpfiles.d`
  entry creates the log file on a fresh host.
- **R1-19.** The mappings in the components are the single source. Lists, quoting and the
  `kubelet-arg` append are specified, and the migration test has a red fixture for a
  dropped list member.
- **R1-20.** The runbook's capture and verify commands are in Verification. The daniel-box
  binary matches the release checksum (Baseline). daniel-server is read before H6.3d merges.
- **R1-21.** H6.1 is split into H6.1a, H6.1b and H6.1c, H6.3 into H6.3a to H6.3d, H6.4 into
  H6.4a and H6.4b, and H6.5 into H6.5a and H6.5b. Each PR states its size, and vendored
  bytes are reviewed by sha256.
- **R1-22.** Partly fixed: named and bounded under *Risks*, and timed in H6.2's
  verification. The finding also asked that a lock failure not write `hold_sha`. That part is
  declined: a lock failure still writes `hold_sha`, as any apt failure on daniel-box does.
  Exempting it would hide a real apt failure, and H7 removes Ansible from that host.
- **R1-23.** Each listed doc joins the PR that changes what it describes.

### Round c1 (per-slice adversarial review)

Raised 9 findings: 4 high and 5 medium. All 9 were fixed, and none was refuted. Four fixes
depart from the finding's literal wording, because a fact measured at `70591c307` or
upstream broke it. Each departure is named below.

| ID | Severity | Title | Outcome |
|---|---|---|---|
| H6-C1-01 | high | The health expressions read the Plan's stale `Complete=True` as current | Fixed, adjusted |
| H6-C1-02 | high | A root open or create of the shared `all` deploy lock breaks the unit or every deploy | Fixed |
| H6-C1-03 | high | The Plans' ServiceAccount cannot read Longhorn, so every `prepare` refuses | Fixed |
| H6-C1-04 | high | The H6.4b failure rollback never restarts k3s | Fixed |
| H6-C1-05 | medium | Open question 6 ignores H1's `flux_forward_only` hold | Fixed |
| H6-C1-06 | medium | The pin move's reader list was incomplete | Fixed |
| H6-C1-07 | medium | The secret steps did not follow H4's per-host SOPS interface | Fixed, adjusted |
| H6-C1-08 | medium | Both hosts' windowed restarts fire at the same instant | Fixed, adjusted |
| H6-C1-09 | medium | The agent Job's 60-minute deadline runs while it waits for the server | Fixed, adjusted |

How each was fixed, with the evidence:

- **C1-01.** Confirmed by `fluxcd/pkg` `runtime/cel/status_evaluator.go`
  `StatusEvaluator.Evaluate`, by SUC v0.20.2's `PlanStatus`, and by `handle_upgrade.go`.
    - Departure: the literal fix, `status.latestVersion == spec.version`, is never true. SUC's
      `plan.go` `MungeVersion` stores `v1.37.1-k3s1` for `v1.37.1+k3s1`, so every apply
      would time out red. The expressions compare against the munged pin, rendered as a
      literal, and guard missing fields with `has()`. A rendered literal works whatever
      Flux release D1 pins. The H6.1c drill Plan now carries `k3s_version` so that its
      `failed` expression can fire.
    - The red fixture is a Plan with `Complete=True` and a stale `latestVersion`, and it
      must read InProgress. Three more fixtures cover a Plan with no status, the munged
      green value, and an old `JobFailed` beside a stale version.
    - The selector-only gap is stated in the Design and in *Risks*. H6.3c, H6.3d and exit 1
      verify by each node's `plan.upgrade.cattle.io/<plan>` label against
      `status.latestHash`. The label outlives the Job's 900-second TTL.
- **C1-02.** Confirmed on daniel-box: `fs.protected_regular = 2`, `/var/lock` links to
  `/run/lock` (`drwxrwxrwt root root`), the lock file is `ubuntu ubuntu`, and
  `deploy_locks.py:_take` opens with `O_WRONLY | O_CREAT`.
    - Both readers open the lock `O_RDONLY` without `O_CREAT`, and an AST test fails on
      `O_CREAT`.
    - A `tmpfiles.d` entry creates the file at boot as the deployer's user. An absent file
      refuses. The finding offered this or "absent reads as free"; the first was chosen
      because a free read cannot hold the lock through the restart.
    - `prepare` reads through SUC's own `/host` mount (`container.go`) and needs no Plan
      `volumes` entry. Under that mount an absolute symlink still resolves from the
      container's root, so `/host/var/lock` would point at the container's own
      `/run/lock`. The script resolves the link under `/host`, and a fixture test proves
      it.
    - An absent `hold_sha` is the deployer's no-hold state. It was absent when measured.
- **C1-03.** Confirmed in SUC v0.20.2 `manifests/clusterrole.yaml` and
  `clusterrolebinding.yaml`. H6.1b adds the dedicated ServiceAccount `k3s-plan`, a
  ClusterRole and a CoreDNS-only Role, and both Plans name the account. It lands while no
  node is selected, because `plan.go` `DigestStatus` hashes `serviceAccountName`. A render
  test pins the grants. H6.3c gains an operator-only `sudo k3s kubectl auth can-i --as`
  check, because impersonation needs more than the read-only ServiceAccount holds.
- **C1-04.** Confirmed from the unit's own steps 3, 6 and 8. The H6.4b Rollback now splits
  on whether the migration restart succeeded. A failed one gets an operator-only recovery:
  install the reverted package, `daemon-reload`, restart, verify, then remove
  `failed.sha256`. The apt repository is a host service on daniel-box (H3,
  `homelab-apt-repo.service`), so the package arrives while k3s is down. Step 3 keeps
  `failed 1` while `failed.sha256` exists, so the alert does not clear before the
  recovery. A unit test covers a failed H_b, then a revert to H_a.
- **C1-05.** Confirmed in H1's *the forward-only hold*. H6.1b sets `flux_forward_only:
  true`, Open question 6 is closed, and the partial-failure bullets and H6.1c's
  expectation now read "holds without a reset". H1's `homelab/upgrade-gates` rule names
  only `k3s_longhorn_version` and `k3s_metallb_version`, so the flag adds no status
  requirement to a k3s bump.
- **C1-06.** Confirmed with `git grep K3S_DEFAULTS`, which finds at least nine importers
  that read keys which stay. H6.1b adds a new `repo_paths` constant and switches only the
  `k3s_version` and installer readers. `test_k3s_version_is_pinned` reads through
  `load_defaults(K3S_ROLE)`, not through the constant, so it is retargeted separately. H1's
  CoreDNS image test joins H6.1b's list.
- **C1-07.** Confirmed against H4's *per-host SOPS file* and H4.2.
    - Departure: the finding put the host file and the `check` entry in H6.2. H4's
      `check` compares a host file's keys with the credentials the host uses, and
      daniel-box uses none before H6.3b. So H6.2 adds the creation rule, the CI-scan
      named sets and the operator-only bootstrap.
    - H6.3b then adds the operator-only `host_secrets.py sync` for both hosts, daniel-box's
      `secrets` component, its `check` entry and the credential in both `package.yml`
      files. `homelab-daniel-box` starts with `reconcile` only.
- **C1-08.** Departure: the suggested 11:36 crosses daniel-box's 11:40 `*/10` fire, so
  daniel-server's window is 11:52 to 11:59.
    - An agent restart degrades Longhorn replicas that daniel-box's crons read, so the
      derivation test checks daniel-server's window against daniel-box's schedules too. It
      also asserts that the two windows do not overlap.
    - Agent-host mode also refuses within 15 minutes of daniel-box's `Ready` transition.
- **C1-09.** Departure: a fail-fast agent `prepare` is not used. SUC's `job.go` sets
  `BackoffLimit` 2 and `RestartPolicy: Never`, so three failed pods end in `JobFailed`
  within minutes.
    - The agent Plan sets `jobActiveDeadlineSecs: 5400`. The controller's
      `SYSTEM_UPGRADE_JOB_ACTIVE_DEADLINE_SECONDS_MAX` is set to admit it, and
      `flux_timeout` is 100 minutes.
    - The wall-clock bound is stated beside the timeout, and a render test holds the
      three values in order.

### Round c2 (per-slice adversarial review)

Raised 9 findings: 3 high and 6 medium. All 9 were fixed, and none was refuted. Three fixes
depart from the finding's wording, and each departure is named below.

| ID | Severity | Title | Outcome |
|---|---|---|---|
| H6-C2-01 | high | A k3s restart does not move the node's Ready transition, so the server-first exit check and the 15-minute guard cannot work | Fixed, adjusted |
| H6-C2-02 | high | The restart unit's Ready poll accepts a stale Ready=True, so a failed restart is recorded as applied | Fixed |
| H6-C2-03 | high | K3sUpgradeJobFailed reads Job series that kube-state-metrics does not collect | Fixed, adjusted |
| H6-C2-04 | medium | The H6.1c production drill is not marked operator-only and does not state its blast radius | Fixed |
| H6-C2-05 | medium | The H6.6 revision also changes the forward-only coredns tag | Fixed |
| H6-C2-06 | medium | H6's restart unit breaks H4's rule that reconcile is the only restarter | Fixed |
| H6-C2-07 | medium | The window proof ignores systemd's default AccuracySec | Fixed |
| H6-C2-08 | medium | The window-derivation test reads host schedules only | Fixed, adjusted |
| H6-C2-09 | medium | Exit 2 requires the bump commit itself to carry flux/system-upgrade | Fixed |

How each was fixed, with the evidence:

- **C2-01.** Confirmed on daniel-box: `uptime -s` printed `2026-10-11 07:39:50`, k3s's
  `ExecMainStartTimestamp` was `07:40:12 UTC`, and daniel-box's `Ready`
  `lastTransitionTime` read `2026-08-01T23:41:59Z`.
    - Exit 2 and *Verification* now prove server-first with the two new
      `ExecMainStartTimestamp` values, which must differ from the recorded ones and be in
      order, and with the agent `prepare` finishing after the server Job.
    - The guard reads the `kube-controller-manager` Lease. It refuses on a missing or
      Forbidden Lease, an empty holder, a `renewTime` older than the lease duration plus 30
      seconds (the handover gap), and an `acquireTime` under 15 minutes old. Agent-host
      mode and the agent `prepare` both run it.
    - Grants: the read-only account gets a kube-system Role for that one Lease in H6.3b,
      applied by the operator. `k3s-plan`'s kube-system Role, renamed
      `k3s-plan-kube-system`, gains the same grant in H6.1b. The red fixture is a 5-minute
      `acquireTime` beside a months-old `Ready` transition.
    - Departure: the finding asked for the Lease measurement in the Baseline. The read-only
      account got `Forbidden` on Leases, and an agent cannot use `sudo`. The Baseline
      records the upstream source instead: k3s's `controllerManager`,
      kube-controller-manager's per-process holder identity, and client-go's
      `LeaderElector.tryAcquireOrRenew`. H6.3a adds an operator-only read that must show
      `acquireTime` at or after the k3s start, and *Open questions* 9 says what happens if
      it does not.
- **C2-02.** Confirmed by the C2-01 measurement. Step 7 now captures `MainPID` and T0, and
  a poll succeeds only on `active`, a changed `MainPID` and a `Ready=True` heartbeat later
  than T0. Two unit tests cover a stale `Ready=True` with `systemctl` never active, and
  `active` with an unchanged PID. Both write `failed 1` and `failed.sha256`.
- **C2-03.** Confirmed: `kube-state-metrics.yaml.j2` passes
  `--resources=pods,deployments,statefulsets,daemonsets,nodes`.
    - Departure: the finding offered H6.3b. H6.1c, the only drill that makes a Job fail,
      runs before H6.3b. So H6.1b adds `jobs` to the flag and the ClusterRole together with
      `K3sUpgradeJobFailed`, and H6.1c proves the alert live under the same hold.
    - The rule reads `kube_job_failed{condition="true"}`, not the failed-pod count, which
      a retried pod raises inside a Job that then succeeds. `keep_firing_for: 2h` covers
      the TTL gap. H6.1b's live query must find `kube_job_failed` for namespace `homelab`,
      which holds 17 Jobs. *Open questions* 2 is closed, and no M6 edge is added.
- **C2-04.** H6.1c is now **operator-only**. It states its objects, the hold that blocks
  every other session's promotions, what keeps serving and the rollback. A revert cannot
  promote while the hold is set (C2), so the operator pauses daniel-box with the label,
  clears the hold, then removes the label. The drill Plan declares no `prepare`, because no
  host ships the gate script before H6.3b.
- **C2-05.** Confirmed: H1 sets `flux_forward_only: true` on `coredns` and moves its pin
  with `k3s_version`. No `dependsOn` joins `coredns` and `system-upgrade` in D4 or H1. The
  risk and its fallback now cover every Kustomization in the revision, and H6.4b reads
  events for all of them. The CoreDNS gate now requires a finished rollout, because
  `Available` stays True during a rolling update.
- **C2-06.** Confirmed in H4's *The restart contract*. *Open questions* 8 asks for H4's
  amendment. The credential is in H4's form, with `Wants=` and `After=` on
  `homelab-secrets.service`, and a restart-unit test asserts it.
- **C2-07.** Confirmed: the `gitops-deploy.timer` properties read `AccuracyUSec=1min` and
  `RandomizedDelayUSec=2min`. The restart timer sets `AccuracySec=1s` and
  `RandomizedDelaySec=0`. The test widens each window by its accuracy and each other
  timer's fire by its own slack. A timer without `AccuracySec` is a red fixture.
- **C2-08.** Confirmed: `kubectl get cronjobs -A` lists `kuma-status-page-sync` at `*/15`
  and `kuma-maintenance-sync` at `20 * * * *`, and Longhorn's RecurringJobs run at 03:30
  and 04:30. None falls inside either window. The test now reads rendered CronJob and
  RecurringJob schedules. The finding's suggested member, the Kuma status-page sync, would
  vanish when M6 retires Kuma, so the scans must find `homelab/configarr` and
  `daily-backup` instead. H2's CronJob PRs re-run it.
- **C2-09.** Confirmed against C2 and C6 in contracts.md. Exit 2 and *Verification* now
  read `success` on a revision R that equals the bump commit or descends from it, with
  `last-good` covering R.

### Round c3 (per-slice adversarial review)

Raised 8 findings: 1 high and 7 medium. All 8 were fixed. Three fixes depart from the
finding's wording, and H6-C3-07's premise was partly refuted. Each is named below.

| ID | Severity | Title | Outcome |
|---|---|---|---|
| H6-C3-01 | high | H6.3b's Lease grant edits a template and an apply path that H1.2 has already moved to the Flux-owned cluster-base tag | Fixed |
| H6-C3-02 | medium | The first unattended API restarts have no guard against an in-flight promote verify | Fixed, adjusted |
| H6-C3-03 | medium | A SUC upgrade and a windowed restart can run on the same node at once | Fixed, adjusted |
| H6-C3-04 | medium | The variables H6.4a and H6.4b move omit five that the moved templates read | Fixed, adjusted |
| H6-C3-05 | medium | A TimeoutStartSec kill after the restart records no attempt | Fixed |
| H6-C3-06 | medium | The window-derivation test cannot pass against the monotonic gitops-deploy.timer | Fixed |
| H6-C3-07 | medium | "Until D6" has no owner | Fixed, premise partly refuted |
| H6-C3-08 | medium | No live proof that the homelab_k3s_restart_* series reach Prometheus | Fixed |

How each was fixed, with the evidence measured at `70591c307`:

- **C3-01.** Confirmed against H1. Its tag table moves `readonly-rbac.yaml.j2` to
  `cluster-base` by git mv in H1.2, and *Plumbing tags are never pruned* makes that tag
  `prune: false`.
    - H6.3b now adds the Lease Role, its RoleBinding and `upgrade.cattle.io` to
      `cluster-base`, and promote applies them. The RoleBinding's subject reads H1.2's
      `cluster_base_readonly_sa_*` keys.
    - The `manual_plane`, `--tags kubeconfig` and `clear-owed` steps are gone. An agent
      checks the grants with `kubectl auth can-i` after promotion.
    - The Baseline, the file tree, the tests list and Rollback follow. Rollback says the
      Role stays because the tag renders `prune: false`, and its removal is an
      operator-only delete.
- **C3-02.** Confirmed: step 5's refusals read only the deployer's signals.
    - Host mode now refuses, on both hosts, while any Kustomization in `flux-system` is not
      `Ready=True` at its source's revision. The root Kustomization `homelab` is a named
      member. D1.2b already grants the read-only account the Flux groups, so this needs
      no new grant.
    - Departure: the event read moves to H6.4a, the first unattended restart. With the
      new check in place, an empty read there shows only that the check held, so the plan
      no longer calls it evidence of Flux's behaviour. H6.6 stays the first proof.
    - The Risks entry now covers every windowed restart, and it names the residual: a
      promotion that moves `prod` after the check reads.
- **C3-03.** Confirmed: no host-mode gate read a Plan, and
  `k3s_readonly_crd_api_groups` names no `upgrade.cattle.io`. A `kubectl auth can-i` check
  is no evidence either way while SUC's CRD is not installed, so H6.3b runs it after
  promotion.
    - Departure: the marker is `RuntimeDirectory=homelab-k3s-restart`, not a file the
      script creates. systemd removes it when the unit stops, a kill included, so no
      stale marker survives.
    - Host mode also refuses on a Job in `system-upgrade` without a terminal condition.
      SUC can create a Job before it writes `.status.applying`, and the Job read covers
      that gap. Host mode skips the marker check, because it runs inside that unit.
    - *Open questions* 8 now names both restarters.
- **C3-04.** Confirmed by grep. `authentication-config.yaml.j2` reads
  `k3s_oidc_client_id`, `k3s_oidc_groups_claim`, `k3s_oidc_groups_prefix` and
  `k3s_oidc_username_claim`. `audit-policy.yaml.j2` reads `k3s_readonly_sa_name` and
  `k3s_readonly_sa_namespace`.
    - Departure: the old list was also wrong in the other direction.
      `k3s_audit_log_enabled`, `k3s_audit_log_dir`, `k3s_audit_log_path` and
      `k3s_etcd_expose_metrics` are defined in `ansible/inventory/group_vars/all.yml`.
      loki-homelab and `ansible/tests/setup/test_etcd_metrics_single_switch.py` depend on
      that, so they must not move.
    - The design now gives every variable one of five homes, and names the PR that moves
      each one. The two path keys exist in two places from H6.4a to H6.4b, and the
      shipped-paths test holds them equal over that span.
    - The read-only pair becomes a mirrored triple under H1's *Mirrored identity keys*
      test. A host-render test fails on an undefined variable.
- **C3-05.** Confirmed: only step 8 wrote `failed.sha256`.
    - Step 7 now writes `attempt.sha256` before the restart, and step 3 turns a leftover
      one into `failed.sha256`. An `ExecStopPost=` does the same at once after a timeout.
    - The time bound across steps 1 to 8 is stated, with a timeout on each API read. A
      unit test kills a run after the restart.
- **C3-06.** Confirmed: the gitops_deploy role's `## At a glance` lists
  `gitops-deploy.timer` with `OnBootSec=10min` and `OnUnitActiveSec=10min`.
    - A grep for monotonic timers also found four `claude_code` timers, which would fail
      the old test too.
    - An explicit exemption list names each timer with the exclusion that replaces the
      schedule check. An unlisted monotonic timer is a red fixture.
- **C3-07.** Premise partly refuted: D6 does not delete the deployer.
    - D6's *What stays until H7* reads "H7 deletes the deployer", and H7's
      `gitops_deploy` row reads "H7.19a retires the deployer".
    - contracts.md's H7 row lists H6 as a dependency, so the order already exists.
    - The gap was real, because no PR owned the removal of the deployer-coupled checks.
      Every "until D6" now reads "until H7.19a", and *Open questions* 10 asks H7.19a to
      remove all seven items.
- **C3-08.** Confirmed: `ansible/roles/k8s/node-exporter/CLAUDE.md` called the textfile
  hostPath "an empty hook". M3 PR 1
  ([#4869](https://github.com/DanielH2018/server/pull/4869)) replaced that with the live
  `claude_cgroup.prom` producer.
    - Every run, step 2 included, now writes all three series, through a temporary file
      whose name does not end in `.prom`.
    - Before H6.4a merges, H6.3b's verification queries `probe.py metric` for both hosts.
    - H6.4a adds `K3sConfigRestartMetricsAbsent`.

### Round integration-2 (integration review)

Raised 6 findings against H6: 3 high and 3 medium. All 6 were fixed on H6's side. Five
also need a change in another slice or in contracts.md, sent as cross-slice notes. One
claim in X2-14 was refuted, and X2-11's "70 minutes" claim matched nothing live.

| ID | Severity | Title | Outcome |
|---|---|---|---|
| X2-01 | high | `DEPLOY_HOLD`'s no-hold value is `none` in D1/D2 but "empty" in C2, C6, H1.1g, D5's reset guard and the D4/H1 verifications | Fixed in H6; contracts, H1, D4 and D5 notes |
| X2-04 | high | H7 removes the deployer without the seven deployer-coupled H6 checks | Fixed in H6; H7 note |
| X2-06 | high | H1, H2.3b and H6 add `containers_list` entries while the deployer still narrows | Fixed with the fallback; D2/D4 note for the preferred fix |
| X2-11 | medium | The register and slice index lack about 20 requested rows and sub-edges | H6's part fixed; contracts and D4 notes |
| X2-14 | medium | H6's host-mode gate refuses whenever any Kustomization lags its source, which a D5 suspend causes | Fixed; one claim refuted |
| X2-15 | medium | H4's restart contract still names reconcile as the only restarter | H6's gate made explicit; H4 note |

How each was handled, with the evidence measured at `70591c307`:

- **X2-01.** Confirmed. D1's *Design* reads "`DEPLOY_HOLD`: the value `none` is 'no
  hold'", and D2 reads "`DEPLOY_HOLD` has no empty value". contracts.md C2 still reads
  "the repo variable `DEPLOY_HOLD` is empty". H6 never said "empty", but it named no clear
  command.
    - H6.1c's blast radius names `none` as the no-hold value.
    - H6.1c's rollback and both *Risks* entries name the operator-only clear, `gh variable
      set DEPLOY_HOLD --body none -R DanielH2018/server`, as D2 does.
    - Exit 2's expected result reads "`DEPLOY_HOLD` reading `none`", and an empty or
      missing value is an error.
    - H6's exit 2 also depends on the contracts fix. Under C6's literal text, `none` is
      non-empty, so every `land-wait` would end as `deploy-failed`.
- **X2-04.** Confirmed. A grep of `h7-delete-ansible.md` for `k3s_gates`,
  `server-deploy-all` and `held_sha` finds nothing. H7 says "H7.19a's fourth PR ships
  `homelab-checkout-sync` beside the running deployer, the fifth PR stops the deployer
  ticking".
    - *Open questions* 10 names the seven items and the fifth PR as their owner. It also
      says that PR lands before any PR that deletes `deploy_locks.py` or the state
      directory.
    - *Risks* gains *The removal order*, which says what breaks in the other order.
    - The package scan also reads `homelab-updater.timer`, whose shared schedule is
      monotonic on both k3s hosts (H3). It joins the exemption list with the `windowed`
      mark as its exclusion, so the test this slice adds still passes in H6.3b.
    - The window test could not have failed H7's timer, because it read only the sources
      `gen_role_glance.py` reads, which are Ansible role sources. It now also reads the
      timers each host package ships, with `homelab-k3s-restart.timer` as its named
      member. The `gitops-deploy.timer` exemption says a monotonic replacement gets no
      entry by inheritance.
- **X2-06.** Confirmed: `scripts/deploy_tools/narrow_lib/containers.py:changed_fields`
  returns None when the entry names differ, and its docstring says an added or removed
  entry "moves every reader that iterates the list". The preferred fix is D2's or D4's,
  so H6 applies the fallback.
    - H6.1b records `deploy_tags.py narrow origin/master HEAD`, and runs D1.2's stop rule
      before the PR and before `land.sh`. If the narrowing refuses, it takes D1.2's exit.
    - The H6.1b revert removes the entry, so *Rollback* gives it the same procedure.
    - H6.1b said the observability half was "a deploy of the `observability` tag, not the
      tick". That contradicted D1's *Baseline*, where `observability` is one of the 23
      readers, and the deploy plane ignores `k8s_autodeploy: false`. The step now follows
      the narrow output.
    - A *Risks* entry and *Open questions* 11 were added.
- **X2-11.** H6's part only.
    - *Open questions* 5 is marked applied. C3's *Per-role fields* reads "its own
      `timeout` (H1, H6); and `healthCheckExprs` for its own custom resources (H6)". The
      promote timeout now points at D4's chain derivation. D4 caps its queueing term at
      "tdarr's 50 minutes", but this tag's `timeout` is 100 minutes, so D4 gets a note.
    - *Open questions* 6 asks contracts.md to drop its stale "C2, a reset that lowers the
      Plans" row.
    - The "70 minutes" claim matches nothing live. `grep -rn -e "70 minutes" -e
      "70-minute" docs/target-architecture` finds only H6's own Round 1 log entry, R1-10.
- **X2-14.** Confirmed in part. D5's *The stop lever (D5.1b)* renders `spec.suspend: true`,
  and D5 says "A suspended Kustomization never posts and never advances its revision".
    - Host mode now skips a Kustomization with `spec.suspend: true` in all three clauses:
      `Ready`, generation and revision. A suspend patch raises `metadata.generation`, so
      skipping it only in the revision clause would still refuse. The unit names each
      skipped one at notice, and `homelab` must still be listed.
    - New fixtures: a suspended Kustomization lagging on both revision and generation
      passes and is logged, and the same object unsuspended refuses.
    - *Risks* names the residual of a resume after the check reads.
    - Refuted: "the agent upgrade then refuse[s]". Only host mode, the windowed restart on
      both hosts, runs the in-flight check (*`homelab-k3s-restart`*, step 6). The server
      and agent `prepare` modes do not, and the design now says so for the agent too.
- **X2-15.** Confirmed: H4 still reads "`homelab-reconcile` is the only process that starts
  or restarts a unit outside the boot path". H6's side already stood in *Open questions*
  8. H6.3b's step list now carries the same gate as a "Gate before merge" bullet. The
  1390-second `TimeoutStartSec` and the contract text are H4's to change.

### Cross-slice follow-up (2026-10-12)

- Cross-slice note from H1: no change. H6 edits `server.yml` in place (its install guard, its
  file tasks and its args clause) but does not move, rename or replace it, and adds no cluster
  writer to it, so H1.1h's guard placement and its test stay valid. (#5957 later retired the
  guard, once H1.7b had removed its last writer.)

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review round covers it.

- **F-11.** *Baseline* says production is the only standing cluster and names the drill
  guest. *Design* gains *Rehearsal in a drill guest*. PR H6.1b2 adds the rehearsal mode,
  and H6.1c gates on its run. H6.6 and the rewritten runbook require it for every minor
  bump. The guest is C8's third substitute, as H7 *Open questions* 11 rules for its own
  guest, so C8 needs no change. *Risks*, *Verification*, *Tests and docs*, the file tree
  and *Complexity delta* follow. H7.13 carries the mode when the role moves.
- **F-12.** *SUC through Flux* gains the controller's digest pin and a default-deny ingress
  NetworkPolicy, each with a render test and a red fixture, in H6.1b, which grows to about
  280 lines. The probe row went to *Open questions* 13, because it depends on the
  controller's ports. *Design* gains *The installed binary matches the release*, built by
  PR H6.3e: a sum pin, a co-change check and a `binary` gate mode, with verify Jobs whose
  D5.1a1 edge is *Open questions* 14. `upgrade.image` by digest went to *Open questions*
  12, with a mirror as the recommendation.
- **P8.** *Design* gains *Fixed premises*: k3s and SUC.
- **RA-1.** The outcome paragraph and *Risks* say the hold carries H6's own value,
  never C2's `failed=` form, so C2's revert rule cannot clear it. Only the operator clears
  it.

### Reader-set follow-up (2026-10-15)

- **#4975: H6.1b still branched on an own-tag-only outcome and said every reader applies.**
  Confirmed. D2.1c landed and records an added `owner: flux` entry by its fields, and D2
  refuses the own-tag mapping. Fixed: H6.1b, *Rollback*, *Risks* and *Open questions* 11
  name the four readers that D2 Open question 10 gives an entry carrying `name`, `platform`
  and `owner`, and drop the own-tag branch. H6.1b says its narrow output names
  observability through `broad.py:_changed_half`, because the PR edits that role's
  templates, and not through the added entry, which reaches no observability template.
