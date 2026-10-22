# H1 — Hand the cluster content to Flux

Status: IN PROGRESS

PR H1.1f, the CI pin checks: [#5256](https://github.com/DanielH2018/server/pull/5256). It
merged on 2026-10-16 at `2e8d5b1e3` and deployed nothing. `scripts/validate/cluster_pins.py`
refuses a decrease, a skipped Longhorn minor and a bump that shares its PR, and it reported
`moves no pin the wrong way` on its own merge. Its override refusal reads indented play
vars, `include_role` vars and INI inventory since
[#5257](https://github.com/DanielH2018/server/issues/5257); no such file sets either pin.
PR H1.1g, the land gate: [#5265](https://github.com/DanielH2018/server/pull/5265). It merged
at `722e98608` and deployed nothing. `land.sh` refuses a forward-only pin bump while
(`last-good`, master tip] already changes that pin or while `DEPLOY_HOLD` reads anything but
`none`, and it posts `homelab/upgrade-gates` at the merge. `land.sh --regate <pr>` re-posts the
status and never merges. Renovate's grouped rule is split into `k3s`, `metallb` and `longhorn`
rules, none automerged.
PR H1.1j, the takeover census: [#5273](https://github.com/DanielH2018/server/pull/5273). It
merged at `4c2a9f92e` and deployed nothing. `scripts/dev/takeover_census.py` imports the
cleanup list, the flattening and the server-default rule from `probe_lib/takeover.py` (D5.1g)
rather than copying them. Its inputs need the admin kubeconfig, so its first live run is the
operator's, before H1's first flip.
PR H1.1e, the vendoring tool: [#5279](https://github.com/DanielH2018/server/pull/5279). It
merged at `505a5a832` and deployed nothing. `scripts/dev/vendor_manifest.py` pins each
vendored manifest to its sha256 and fetched URL, and since
[#5293](https://github.com/DanielH2018/server/pull/5293) it fetches only an `https://` URL.
PR H1.1b, promote's forward-only hold and the gate status:
[#5278](https://github.com/DanielH2018/server/pull/5278). It merged at `4daf364a1`. The first
promote runs on its code promoted `505a5a832` and then `4daf364a1`, each with `verify` reading
`verdict=good failed=`, and `DEPLOY_HOLD` stayed `none`. No commit since 2026-08-22 changes a
forward-only pin, so the gate rule asked for no `homelab/upgrade-gates` status. C2 names the
`held` tag it adds since [#5281](https://github.com/DanielH2018/server/pull/5281), and the
excused-failures row that gated H1.1i moved to C2's applied list in
[#5287](https://github.com/DanielH2018/server/pull/5287).
PR H1.1i, a dependent that was already failing: [#5300](https://github.com/DanielH2018/server/pull/5300).
It merged at `a1e7699c0`. The first promote run on its code, for `b0f528953`, read `verify`
`verdict=good failed=` and moved `last-good`. H1's fixed four, H1.1f, H1.1g, H1.1b and H1.1i, are
all on master.
PR H1.1d, health for vendored targets: [#5295](https://github.com/DanielH2018/server/pull/5295). It
merged at `6595ea687`. `probe.py health` resolves vendored workloads, and `NEVER_SHED`, empty until
H1.3, keeps plumbing out of the shed set. Its `filter_plugins` edit made the tick run a broad
apply, which an outbound outage failed on 2026-10-16 around 18:46 UTC. The operator applied the
held plane and the owed `k3s` bring-up (`ok=24 changed=0`), then cleared the hold
([#5302](https://github.com/DanielH2018/server/issues/5302)).
PR H1.1c, the per-tag object census: [#5307](https://github.com/DanielH2018/server/pull/5307). It
merged at `a29cf92c7` and deployed nothing. Publish fails when a plumbing tag's render drops a
named member of *Plumbing tags are never pruned*.
PR H1.1a, renderer support: [#5312](https://github.com/DanielH2018/server/pull/5312). It merged at
`c5db379f7` and deployed nothing. The renderer publishes vendored files, Kustomize `patches:`,
`prune: false` and a per-role Kustomization `timeout`, and the renderer suite reads 636 passed on
master. It reads patches from `files/patches/`, the directory H1.1d's health gate reads, which closes
[#5283](https://github.com/DanielH2018/server/issues/5283).
PR H1.1h, the stale-checkout guard: [#5316](https://github.com/DanielH2018/server/pull/5316). It
merged at `c31833d3d` and owed `manual_plane k3s`. The operator applied `k3s-bringup.yml --tags
kubeconfig`, which read `ok=18 changed=0 failed=0`: the guard read the live Flux inventory and its
refusal skipped. The operator then cleared the line
([readings](https://github.com/DanielH2018/server/pull/5316#issuecomment-6224451468)).

Depends on: D2, D4; D3 for H1.7b only; R4 for H1.9 only
Unblocks: H2, which needs H1.1a to H1.8 and never H1.9

Related issues:

- [#4512](https://github.com/DanielH2018/server/issues/4512): an owed Longhorn backup-tag apply, which stays live drift until H1.7b.
- [#4331](https://github.com/DanielH2018/server/issues/4331): the etcd-first DR order drill. H1.2 rewrites that order, so the drill runs again
  after H1.2 and after H2.

`ansible/roles/setup/k3s` installs k3s, and it also applies the cluster's own plumbing:
Longhorn, MetalLB, CoreDNS, the Longhorn RecurringJobs and backup targets, the
PriorityClasses and the read-only RBAC. All of that ships through the hand-run
`k3s-bringup.yml`, so every Longhorn bump is a hand apply. This slice moves the plumbing into
Flux-born deploy tags. It also moves daniel-box's `containers_list` out of `host_vars` once no
Ansible play reads it. The Pi's entries stay in `daniel-pi.yml`, where H5 and H7 own them.

## Goal and exit criterion

Afterwards, no task file in `setup/k3s` writes a cluster object, with two exceptions. Both run
only when an operator asks for them:

- `storage_smoke.yml`, the new-cluster storage check, binds a PVC and deletes it again;
- `agent_verify.yml`, the post-join check, patches the joining node's `nodes.longhorn.io`.

`kubeconfig.yml` keeps its read of the read-only token, which is a read. The host crons that
write to the cluster, such as the restore drill, are H2's scope.

Five Flux Kustomizations own the plumbing: `cluster-base`, `metallb`, `coredns`, `longhorn`
and `longhorn-backup`. A sixth, `longhorn-reconcile`, runs the hourly reconcile of the
volumes' backup tiers. daniel-box's service list lives in `cluster/services.yml`, a renderer
input outside the inventory.

The exit criterion has two parts:

1. **A Longhorn bump with no hand apply.** A PR raises the Longhorn pin by one release step
   that the land gate admits, and `land.sh` merges it. The step is the next patch release in
   the pinned minor, or else the next minor release. Upstream lists `v1.12.1` and `v1.13.0`
   and no `v1.12.2` (`gh api repos/longhorn/longhorn/releases`, 2026-10-11). Until a
   `v1.12.2` ships, the step is therefore `v1.13.0`. The land gate admits it, because it skips
   no minor release (H1.1f), and it runs the `runbook_gates.py longhorn-upgrade` gates.
   Which step H1.8b takes depends on upstream's release timing. Afterwards:
    - the `longhorn` Kustomization reports `Ready`, and its `lastAppliedOriginRevision` names
      the merge commit or a later master commit;
    - every `longhorn-manager` pod runs the bumped image;
    - every volume's engine follows within the auto-upgrade concurrency, which H1.5b's
      render test holds above 0;
    - `probe.py health longhorn` passes, and its output names DaemonSet `longhorn-manager`
      among the targets it gated;
    - nobody ran `k3s-bringup.yml`, and the merge left no `manual_plane` line.
2. **No cluster writes left in `setup/k3s` tasks.** A census parses the role's task YAML. It
   finds no task that writes to the cluster, except in `storage_smoke.yml` and
   `agent_verify.yml`.

## Baseline at `70591c307`

- **The role is large and hand-applied.** `setup/k3s` has 67 tracked files. Its
  `defaults/main.yml` runs to 1,277 lines and its task files to 4,482.
  `ansible/k3s-bringup.yml` applies it, and that playbook is one of
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_changes.py:_BROAD_MANUAL_PREFIXES`.
  The deployer therefore never applies it.
- **A merge under `setup/k3s` leaves an owed line.** The module docstring of
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_defer.py` says that a setup role
  `initial_setup.yml` does not include, such as `k3s`, is written to the `owed` ledger's
  `manual_plane` class when a tick fast-forwards over it. The role's `tasks/` count as part of
  it. Measured at `70591c307` with `deploy_changes.py:services_from_changed_paths`,
  `ansible/roles/setup/k3s/tasks/main.yml` and `tasks/kubeconfig.yml` each return
  `setup_roles={'k3s'}`. The role's `CLAUDE.md` and a file under its `tests/` return an
  empty `setup_roles`. `docs/gitops-pipeline.md` documents the clear and its narrowed
  `--applied` form, and warns that a bare clear drops another PR's tags with yours.
- **The cluster objects it writes.** A grep for kubectl write verbs per task file counts 23 in
  `longhorn.yml`, 10 in `longhorn-backup.yml`, 3 in `cluster-addons.yml`, 3 in
  `coredns.yml`, 2 in `longhorn-weekly-shard.yml`, 2 in `storage_smoke.yml`, 1 in
  `kubeconfig.yml` and 1 in `agent_verify.yml`. That grep misses two shapes: `coredns.yml`'s
  task `Run the CoreDNS image k3s bundles` runs `kubectl set image`, and the read tasks in
  `longhorn.yml` split their command into an `argv:` list, one token per line. The task
  files that survive H1 hold only reads: `server.yml` calls `kubectl wait` and `kubectl get`,
  and `agent.yml`, `node.yml`, `health-crons.yml`, `static-address.yml` and
  `unit-logging.yml` run no `kubectl` and no `kubernetes.core` module.
- **Upstream installs are live fetches.**
    - `tasks/longhorn.yml` (task `Install Longhorn`) runs `kubectl apply -f
      https://raw.githubusercontent.com/longhorn/longhorn/{{ k3s_longhorn_version }}/deploy/longhorn.yaml`,
      with the pin at `v1.12.1`.
    - `tasks/cluster-addons.yml` (task `Install MetalLB`) does the same for
      `metallb-native.yaml`, with the pin `k3s_metallb_version` at `v0.16.0`.
    - C1 forbids the renderer from reading anything outside the git tree.
    - `docs/longhorn-upgrade.md` says Longhorn supports no downgrade, and its bump step says
      "Never skip a minor". The setup/k3s defaults file header says Longhorn "does not
      support arbitrary version skips".
- **Host prerequisites share `longhorn.yml`.** It opens with three host tasks:
  `Install Longhorn prerequisites` (open-iscsi and nfs-common; the comment says volumes hang in
  Attaching without them), `Blacklist Longhorn devices from multipathd` and
  `Restart multipathd after a blacklist change`. `tasks/agent.yml` carries its own copies for
  the agent only.
- **Imperative Longhorn patches.**
    - `Give longhorn-manager a memory request so it leaves BestEffort` patches the upstream
      DaemonSet.
    - `Give the Longhorn CSI sidecars a memory request so they leave BestEffort` patches four
      Deployments, `csi-attacher`, `csi-provisioner`, `csi-resizer` and `csi-snapshotter`.
      `longhorn-driver-deployer` creates them at runtime, so no manifest in the tree carries
      them.
    - Seven tasks patch `settings.longhorn.io` objects, from `default-replica-count` to
      `node-down-pod-deletion-policy`.
- **The B2 target is the BackupTarget `default`, and no template renders it.**
  `ansible/roles/setup/k3s/files/longhorn_lib/longhorn_backups.py:B2_TARGET` names it. Three
  kubectl patches write it:
    - `Point Longhorn at the backup target (or enforce the blanked target)` writes
      `backupTargetURL`, blank while `k3s_longhorn_backup_armed` is false;
    - `Point Longhorn at the backup credential` writes `credentialSecret: longhorn-b2`;
    - `Set the Longhorn backup-store poll interval`, in `longhorn.yml`, writes `pollInterval`.

  The first task's comment records that by v1.12.1 the `backup-target` and
  `backup-target-credential-secret` settings no longer exist, and that patching them fails
  NotFound. Only the second target, `r2`, has a template: `longhorn-backuptarget-r2.yaml.j2`.
- **The B2 URL derives its region by regex over a SOPS value.** The task `Derive the B2 region
  from the Kopia endpoint` runs `regex_search` over `longhorn_b2_endpoint`, and the task after
  it fails the play when the result is empty. That task's message already names the way out:
  "Set k3s_longhorn_b2_region explicitly in host_vars". `docs/break-glass.md` publishes the
  B2 target URL, region included. `grep -c '^<key>:' ansible/vars/secrets.yml` prints 1 for
  each of `longhorn_b2_endpoint`, `longhorn_b2_bucket` and `r2_bucket`. Both credential
  templates, `longhorn-b2-secret.yaml.j2` and `longhorn-r2-secret.yaml.j2`, put their values
  under `data:` through `b64encode`.
- **Two flags govern the backup plane.**
    - `k3s_longhorn_backup_armed` is the containment lever for a B2 transaction-cap spiral.
      Its comment says "DO NOT RE-ARM ON A TIMER". `k3s_longhorn_r2_armed` arms R2 on its
      own. `docs/longhorn-disaster-recovery.md` documents the disarm as the flag plus a
      deploy.
    - `tasks/main.yml` imports `longhorn-backup.yml` only `when: k3s_manage_backup_targets`.
      That file also applies the RecurringJobs (tasks `Apply the Longhorn recurring backup
      job` and `Remove the retired flat weekly-backup recurring job`). The flag therefore
      withholds the targets and the RecurringJobs together, as the `setup/k3s` `CLAUDE.md`
      contract states. That contract adds that both `k3s_manage_*` flags default to `true`
      and that no host overrides either. The comment above the key in the `setup/k3s`
      defaults says it is off only "on a cluster that holds no real credentials", which was
      staging.
- **Who writes the `longhorn` StorageClass.** A read-only `kubectl get` on 2026-10-11 found:
    - class `longhorn` created at 2026-10-10T14:26:29Z, with managers `longhorn-manager`
      (Update, 14:26:29Z) and then `kubectl-client-side-apply` (14:26:30Z);
    - ConfigMap `longhorn-storageclass` with managers `kubectl-client-side-apply` (14:26:22Z)
      and `kubectl-patch` (14:26:29Z);
    - class `longhorn-nobackup`, created 2026-08-03, with `kubectl-client-side-apply` only.

  longhorn-manager therefore created the `longhorn` class in the same second as the ConfigMap
  patch. The measurement does not show which writer deleted the class before it. The task
  `Apply the Longhorn StorageClass` carries an OPEN comment: it reports changed on every run,
  for a cause nobody has established.
- **No stage cluster exists.** D2's baseline records that `daniel-stage` was retired on
  2026-09-29 (#2941). Every measurement in this plan is a read-only one on production.
- **What a fresh cluster needs before Flux runs.**
    - D1's flux role sets each controller's `priorityClassName` to `homelab-critical`,
      through `tier_priority_class('flux')` (D1, *The role meets the fleet guards by
      patching*). `priorityclass.yaml.j2` defines that class, and the task `Apply the
      workload-tier PriorityClasses` in `cluster-addons.yml` applies it. The API server's
      Priority admission plugin refuses a pod that names a class that does not exist.
    - R4's *Bootstrapping Flux without Ansible*, step 1, already applies the PriorityClasses
      from the render of `cluster-base` by hand, for the time after R4 deletes the flux
      role's tasks.
    - `tasks/main.yml` imports `kubeconfig.yml` after `server.yml` and the Longhorn files,
      and before `node.yml`, `static-address.yml` and `coredns.yml`. `coredns.yml` holds the
      task `Stop k3s reconciling its bundled CoreDNS addon`, which writes the `.skip` marker.
    - The task `Read the read-only ServiceAccount token` in `kubeconfig.yml` retries 12 times
      at 5-second intervals. Today it follows `Apply the read-only cluster identity` in the
      same file, so the Secret it reads already exists.
    - D3's *Bootstrap material is `sops-age` alone* says a rebuilt cluster gets
      `flux-system/sops-age` by hand (**operator-only**). D3.0 gives the
      `kubectl create secret generic sops-age` command.
- **How Flux checks a dependency.** D4's baseline, read from kustomize-controller v1.9.6
  `checkDependencies`, says a dependency's Ready condition must be true and, for the same
  source, its `lastAppliedRevision` must equal the current revision. Otherwise the dependent
  applies nothing and reads `DependencyNotReady`.
- **Flux records what it owns.** kustomize-controller v1.9.6
  `internal/controller/kustomization_controller.go` sets `status.inventory` from the change
  set after every apply, whatever `prune` says. Each entry's `id` reads
  `<namespace>_<name>_<group>_<kind>`.
- **`flux diff` substitutes and masks only SOPS data.** fluxcd/flux2 v2.9.6
  `cmd/flux/diff_kustomization.go` takes `--path` and `--kustomization-file`.
  `internal/build/diff.go:diffSopsSecret` masks a Secret value only when it is SOPS
  ciphertext, so a plaintext value prints as it is.
- **What the backup-health verdict says about a disarm.**
  `ansible/roles/setup/k3s/files/longhorn_lib/longhorn_backup_health_logic.py:build_verdict`
  appends `DISARMED: <targets> (<n> volume(s) not checked)` to its message.
  `longhorn_backup_health.py:_require_bool_env` refuses an unrecognised arm export, because
  "False means DISARMED — a target's volumes get suppressed from every check".
  [ADR-0007](../../adr/0007-backup-tiering-r2-daily-b2-weekly.md) says a disarm needs its
  monitor gated "and what was suppressed has to be named". Its *Governs* section says "The
  tier lists live in `ansible/roles/setup/k3s/defaults/main.yml`".
- **The k3s defaults have readers outside the tasks H1 moves.**
    - `longhorn-backup-health.sh.j2` bakes `k3s_longhorn_backup_armed`,
      `k3s_longhorn_r2_armed`, `k3s_longhorn_backup_cron` and
      `k3s_longhorn_weekly_backup_minute_hour` into exported variables at template time. The
      b2-budget cron in `tasks/health-crons.yml` bakes `k3s_longhorn_weekly_backup_retain`
      into its `--retain` argument. Both crons stay host crons: H2 keeps backup health and the
      B2 pair on the host by decision.
    - `longhorn_backup_health.py:main` builds its armed and disarmed target sets from those
      exported flags, not from the cluster. It already reads each armed BackupTarget's
      `status.available` through `k3s kubectl` (`_fetch_target`), under the read-only
      kubeconfig the shim exports. That ServiceAccount reads every resource of each group in
      `k3s_readonly_crd_api_groups`, `longhorn.io` among them. The header of
      `longhorn-backup-health.sh.j2` sizes the reader at up to 17 kubectl calls against its
      600s period, and says two more calls fit.
    - `scripts/diagnostics/probe_lib/longhorn_cluster.py:backup_target_url` already reads a
      target's live `spec.backupTargetURL` and treats a blank one as disarmed. It returns ""
      on a failed read as well, so it cannot tell a failed read from a disarm.
    - The deployer routes a change to the two key files differently. Measured at
      `70591c307` with `deploy_changes.py:services_from_changed_paths`:
      `ansible/inventory/group_vars/all.yml` returns `broad_deploy=True` with an empty
      `setup_roles`, so it leaves no owed line and re-renders no `setup/k3s` file.
      `ansible/roles/setup/k3s/defaults/main.yml` returns `setup_roles={'deploy_ui', 'k3s'}`,
      which is the `manual_plane` line for `k3s`.
    - The RecurringJob template reads `k3s_longhorn_backup_cron`,
      `k3s_longhorn_backup_retain`, `k3s_longhorn_weekly_backup_minute_hour` and
      `k3s_longhorn_weekly_backup_retain`. It names `k3s_longhorn_weekly_volumes` only in a
      comment.
    - The three tier lists, `k3s_longhorn_r2_volumes`, `k3s_longhorn_weekly_volumes` and
      `k3s_longhorn_nobackup_volumes`, are filter expressions over `containers_list`. Their
      task readers are `Route the selected volumes to R2` and `Return de-listed volumes to
      the default backup target` in `longhorn-backup.yml` for the first, and tasks in
      `longhorn.yml` and `longhorn-weekly-shard.yml` for the other two.
    - The read-only identity keys have readers on both sides of the move.
      `readonly-rbac.yaml.j2` reads `k3s_readonly_sa_name`, `k3s_readonly_sa_namespace` and
      `k3s_readonly_crd_api_groups`. The host files read the first two as well: the task
      `Read the read-only ServiceAccount token` in `tasks/kubeconfig.yml`,
      `templates/readonly-kubeconfig.j2`, and `templates/audit-policy.yaml.j2`, which
      `server.yml` renders. `scripts/lib/render_context.py:render_context` layers only a
      role's own defaults, `group_vars/all.yml` and host_vars, so a k8s role cannot read
      `setup/k3s`'s defaults. The comment above `headlamp_k8s_crd_api_groups` in
      `ansible/roles/k8s/headlamp/defaults/main.yml` says the same of
      `k3s_readonly_crd_api_groups`.
    - Two tests read the identity template where it sits.
      `ansible/tests/k8s/test_k8s_manifests_rbac.py` renders
      `K3S_ROLE / "templates" / "readonly-rbac.yaml.j2"` with `K3S_DEFAULT_VALUES`.
      `scripts/deploy_tools/tests/test_narrow_setup.py:test_the_real_k3s_role_still_maps_readonly_rbac_to_kubeconfig`
      asserts that `readonly-rbac.yaml.j2` and `k3s_readonly_crd_api_groups` map to the
      `kubeconfig` tag. Its section header says it is the real-tree case "so the scan cannot
      go vacuous".
    - `scripts/lib/repo_paths.py:K3S_DEFAULTS` is named by 41 tracked files, and 74 name it
      or `K3S_ROLE`. Others read the file by path: `scripts/lib/service_tiers.py`,
      `scripts/docs/fragments_lib/fragments_storage.py`, the `FRAGMENTS` sources in
      `scripts/docs/gen_doc_fragments.py`, and
      `scripts/docs/catalog_lib/catalog_backup.py:load_longhorn_tier_lists`.
- **`probe.py health` reads only templates.**
  `scripts/diagnostics/probe_lib/health.py:_role_kind_targets` globs the role's
  `templates/*.j2`. `run_health` passes a role that declares no rollout-checkable workload,
  with exit 0, under a `DECIDED` marker (#3596).
- **Rollout timeouts are per role, through include vars.**
  `ansible/roles/k8s/manifests/defaults/main.yml:manifests_rollout_timeout_default` is `600s`.
  sonarr, radarr, tdarr, valheim and prowlarr pass `manifests_rollout_timeout` as an
  `include_role` `vars:` entry, taken from a role key such as `sonarr_k8s_rollout_timeout`. A
  Flux-born role has no tasks file, so its override has to be a defaults key.
    - Live, `longhorn-manager` rolls with `maxUnavailable: 100%` across 2 nodes.
    - No roll has been timed. Both manager pods were created at 2026-09-05T18:42:24Z, and
      their `Ready` transition times record later probe changes, not the roll.
- **CoreDNS follows k3s by a host read.**
    - `tasks/coredns.yml` writes a `coredns.yaml.skip` marker and patches the Corefile.
    - It sets the Deployment image from `manifests/coredns.yaml`, which k3s stages on the
      host at start.
    - `ansible/tests/setup/test_coredns_image_follows_bundled_manifest.py` pins that
      extraction against an excerpt of v1.37.1+k3s1's manifest. Its comment says the
      excerpt has `%{SYSTEM_DEFAULT_REGISTRY}%` "already substituted to the empty string".
    - k3s's source file is not YAML until k3s substitutes it. At `v1.37.1+k3s1`,
      `manifests/coredns.yaml` in k3s-io/k3s, read from raw.githubusercontent.com on
      2026-10-11, holds five `%{...}%` placeholders: `CLUSTER_DOMAIN` in the Corefile,
      `SYSTEM_DEFAULT_REGISTRY` in the image, and `clusterIP: %{CLUSTER_DNS}%`,
      `clusterIPs: %{CLUSTER_DNS_LIST}%` and `ipFamilyPolicy: %{CLUSTER_DNS_IPFAMILYPOLICY}%`
      on the Service. A plain YAML scalar cannot start with `%`, so kustomize cannot parse
      those bytes as they stand.
    - The ConfigMap also carries `NodeHosts`, which k3s's node controller writes. The comment
      on `Stage the CoreDNS Corefile patch` says a ConfigMap holding just the Corefile would
      strip it, and `Confirm NodeHosts survived the patch` checks it afterwards.
    - The file header says the rest of the bundled addon is ignored while the marker exists.
      The live Deployment can therefore differ from any excerpt.
- **The land policy is switched on by environment.** The module docstring of
  `scripts/deploy_tools/land_lib/policy.py` says a landing that sets neither of its variables
  "runs exactly as before, which is every interactive session". `docs/longhorn-upgrade.md`
  says gate 2 reads the restore drill's stamp, which exists only on daniel-box.
- **Renovate.** In `renovate.json`:
    - the `# renovate:` annotation manager covers `ansible/roles/k8s/*/defaults/main.yml` and
      `ansible/roles/setup/*/defaults/main.yml`;
    - the k8s image manager matches any `_image:` key in k8s role defaults and in
      `inventory/group_vars/all.yml`;
    - one packageRule groups `k3s-io/k3s`, `metallb/metallb` and `longhorn/longhorn` as
      `k3s control plane (manual — plan the upgrade, mind Longhorn's version skips)`, and its
      description says "the PR is a SIGNAL to plan an upgrade, and the operator drives it".
- **A standing decision on groups.** The template `longhorn-recurringjob.yaml.j2` carries
  `DECIDED groups: [default], and no recurringJobSelector on the StorageClass`. It records
  that a PVC-level label would be inert, because Longhorn reads labels on its Volume CRs.
- **Live-state reconciliation.** These tasks read live PVCs and Volumes and relabel or
  retarget them:
    - the `Find …` and `Move …` / `Return …` pairs in `longhorn.yml`;
    - `longhorn-weekly-shard.yml`;
    - `Route the selected volumes to R2` and `Return de-listed volumes to the default backup
      target` in `longhorn-backup.yml`.

  `Return volumes to the default recurring-job group` selects from live PVCs whose
  `storageClassName` is `longhorn`, not from a list of claims. The desired membership is
  computed at render time from `containers_list`, by `ansible/filter_plugins/longhorn_groups.py`,
  through `k3s_longhorn_r2_volumes`, `k3s_longhorn_weekly_volumes` and
  `k3s_longhorn_nobackup_volumes`.
- **The agent's kubeconfig names this role.** The task `Give the agent user the operator's
  read-only kubeconfig` in `ansible/roles/setup/claude_code/tasks/agent.yml` describes "The
  homelab-readonly ServiceAccount the k3s role writes".
  `ansible/roles/setup/claude_code/templates/claude-land-approval-paths.j2` carries
  `DECIDED a floor, not every path that widens the agent's authority`.
- **`containers_list` is everywhere.**
    - It is in `ansible/inventory/host_vars/daniel-box.yml` (56 entries),
      `daniel-server.yml` (empty) and `daniel-pi.yml` (4 entries, which H5 moves). The
      counts come from `scripts/lib/ansible_inventory.py:containers_entries_in` over each
      file.
    - `git grep -l containers_list` lists 426 tracked files, 422 of them outside the
      generated `docs/reference/`.
    - The shared loaders are `scripts/lib/render_guard.py:containers_entries`,
      `scripts/lib/ansible_inventory.py:containers_entries_in` and
      `scripts/lib/render_context.py`.
    - `ansible/preflight.yml` asserts `containers_list is defined` for every host, and its
      fresh-host GitOps warning reads the list's length.
      `ansible/inventory/host_vars/_example.yml` carries `containers_list: []`.
- **Flux takes over kubectl's fields, on every apply.** The design records, as validated,
  that Flux's first reconcile takes over every `kubectl-*` field manager by prefix and strips
  `last-applied-configuration`. The upstream code, read through `gh api` on 2026-10-11, says
  more:
    - kustomize-controller's `internal/controller/kustomization_controller.go` passes a
      cleanup list of four entries: `kubectl` with operation `Apply`, `kubectl` with
      `Update`, `before-first-apply` with `Update`, and the controller's own name with
      `Update`.
    - fluxcd/pkg `ssa/patch.go:matchFieldManager` matches a list entry by name prefix unless
      it sets `ExactMatch`, and it never matches an entry on a subresource. So
      `kubectl-client-side-apply`, `kubectl-patch`, `kubectl-set` and `kubectl-rollout` all
      match.
    - fluxcd/pkg `ssa/manager_apply.go:Apply` runs its dry run first and the cleanup after
      it, on every apply, not only the first. The cleanup hands each matched entry's fields
      to kustomize-controller, and the apply that follows removes each one the render does
      not declare. Neither a dry run of Flux's own nor a `kubectl diff` sees that removal.
    - Live field managers, read-only, 2026-10-11. DaemonSet `longhorn-manager` has
      `kubectl-client-side-apply`, `kubectl-patch` and `k3s`. Deployment `coredns` has
      `deploy@daniel-box`, `kubectl-set` and `k3s`. MetalLB's `controller` and `speaker` have
      `kubectl-client-side-apply` and `k3s`, and their pod templates carry
      `prometheus.io/port` and `prometheus.io/scrape`. No workload in `longhorn-system`,
      `kube-system` or `metallb-system` carries `kubectl.kubernetes.io/restartedAt` in its
      pod template.

## Design

### Six Flux-born tags

Each tag is a k8s role under `ansible/roles/k8s/<tag>/`. Its `containers_list` entry carries
`owner: flux` from its first commit. Adding that entry reaches every reader of the list until
D6.12 (*Steps*, *Rules for every PR from H1.2 on*). Ansible never reaches it, and the PR that adds it
deletes the `setup/k3s` tasks that wrote the same objects. That is C4 applied to a setup
writer.

| Tag | Objects | Source |
|---|---|---|
| `cluster-base` | the workload-tier PriorityClasses, the read-only ServiceAccount, its ClusterRole, binding and token Secret | `priorityclass.yaml.j2`, `readonly-rbac.yaml.j2` (git mv) |
| `metallb` | upstream `metallb-native.yaml`, the address pool and L2 advertisement | vendored file, `metallb-pool.yaml.j2` (git mv) |
| `coredns` | the CoreDNS Deployment, Service, RBAC and the Corefile key of its ConfigMap | templates derived from k3s's `manifests/coredns.yaml` at `k3s_version`, `coredns-corefile.j2` (git mv) |
| `longhorn` | upstream `longhorn.yaml` with the manager and StorageClass-ConfigMap patches, Setting objects, the `longhorn-nobackup` class and the metrics NetworkPolicy | vendored file, git mv of templates and files |
| `longhorn-backup` | the B2 and R2 credential Secrets, the BackupTargets `default` and `r2`, and the RecurringJobs | git mv of three templates, one new template |
| `longhorn-reconcile` | the `longhorn-reconcile` CronJob, its script and desired-tier ConfigMaps, and its ServiceAccount, Role and ClusterRole | new templates, `files/longhorn_reconcile.py` ported from the tasks |

`dependsOn` comes from toposort edges, as C3 requires. `longhorn-backup` declares
`depends_on: [longhorn]` on its entry, and `longhorn` declares `cluster-base`.
`longhorn-reconcile` declares `depends_on: [longhorn]` too, because its objects live in the
`longhorn-system` namespace that `longhorn`'s vendored file creates.

- **Why `longhorn-backup` waits on `longhorn`.** `longhorn`'s vendored file defines the
  BackupTarget and RecurringJob CRDs, so on a new cluster the edge stops a first apply that
  would fail on unknown kinds.
- **What the edge costs.** Flux applies a dependent only while its dependency is Ready at the
  current revision (Baseline). While `longhorn` is not Ready, for example while a node is down
  and `longhorn-manager` is short of a pod, `longhorn-backup` applies nothing. A disarm PR
  then promotes but does not reach the cluster. In that state the BackupTarget break-glass
  below is the only lever, and the incident runbook says so.
- **Why `longhorn-reconcile` is a tag of its own.** Its desired-tier ConfigMap renders from
  `containers_list`, so every PR that adds a claim or changes a tier changes it. Inside
  `longhorn`, each such PR would change a forward-only tag, and a failed revision would hold
  instead of reset. As its own tag it is not forward-only, and it prunes like any service, so
  it is not one of the plumbing tags below.
- **Each tag's shed disposition.** `probe.py shed-set` lists every untiered entry
  (`ansible/filter_plugins/service_tier.py:shed_entries`), and
  [Break-glass](../../break-glass.md) scales that list to zero when a node is lost. None of
  the six tags declares a `tier:`, so each one takes an explicit disposition in the PR that
  adds its entry:
    - **The five plumbing tags are never shed.** `service_tier.py` gains `NEVER_SHED`, a
      named set of their names, and `shed_entries` drops each member. The exclusion sits in
      `shed_entries`, not in `scripts/lib/service_tiers.py:shed_set`, so the probe and
      `ansible/tests/k8s/test_shed_set_is_derived_from_tier.py:_tiered_dependencies_on_shed`
      read one answer.
    - **The set cannot drift.** A test holds `NEVER_SHED` equal to the set of untiered k8s
      entries whose role defaults set `flux_prune: false`. A plumbing tag therefore cannot
      land without its disposition, and the set cannot name a tag that prunes or a tiered
      tag.
    - **Why untiered only.** `shed_entries` already keeps every tiered entry. D5 sets
      `flux_prune: false` on `traefik-crds`, the tag that carries traefik's CRDs, whose entry
      declares traefik's `tier: home-edge` in `ansible/inventory/host_vars/daniel-box.yml`,
      and on `flux`, whose entry D1 gives `tier: platform`. Neither is a shed candidate, so neither joins `NEVER_SHED`, and
      neither PR needs a shed ruling or an order against H1.1d. An untiered tag that a
      later slice exempts from pruning fails the test until its PR adds it to `NEVER_SHED`
      or gives it a tier.
    - **`longhorn-reconcile` stays shed-eligible.** It renders only a CronJob, so the probe
      names it on its "nothing to scale" line, and the break-glass scales nothing of it.
    - **Why it matters.** Without the exclusion, `coredns` joins the shed set at H1.4,
      because its Deployment is a template. MetalLB's `controller` and Longhorn's
      `longhorn-driver-deployer` and `longhorn-ui` join once H1.1d resolves vendored
      targets, because `health.py:role_scalable_targets` shares that resolution. CoreDNS at
      zero replicas stops cluster DNS, and Flux cannot then fetch the bundle that restores it.
    - **Rejected: a `tier:`.** `tier: platform` makes `tier_priority_class` derive
      `homelab-critical`, and
      `ansible/tests/k8s/test_priority_class_is_derived_from_tier.py:_CLASS_AT_DERIVATION`
      compares each tiered role's class with exact equality. CoreDNS keeps
      `system-cluster-critical` (*CoreDNS without a host read*), so a tier would either lower
      its class or record a class it does not run at.

Flux does not apply the `longhorn` StorageClass. longhorn-manager writes it from the
`longhorn-storageclass` ConfigMap, as the baseline measured, and Flux owns that ConfigMap.

### Plumbing tags are never pruned

Each of the five plumbing roles sets `flux_prune: false` in its defaults. The renderer then emits
`prune: false` on that Kustomization, and `prune: true` everywhere else.

C3's *Per-role fields* allows `prune: false` only "for H1's vendored CRDs". Three plumbing
tags hold no CRD: `cluster-base`, `coredns` and `longhorn-backup`. H1 therefore asks C3 to
allow `prune: false` "for a plumbing tag (H1) or a tag whose render holds a
CustomResourceDefinition (D5)", gated before H1.1a merges (*Open questions* 2).

- **Why.** Pruning a plumbing object is an outage: the Longhorn manager, the `kube-dns`
  Service, the MetalLB speaker or a BackupTarget. With `prune: false`, a render that drops
  an object leaves it serving. Deleting the Kustomization, by a revert or by a render that
  drops the tag, orphans its objects. That holds under D2's `deletionPolicy: Orphan` and
  after D6.20, because the default `MirrorPrune` follows `prune`.
- **Today's semantics.** `kubectl apply` never deletes what a manifest stopped mentioning, so
  `prune: false` keeps the behaviour these objects have now. An object upstream removes
  between versions stays orphaned, as it does today.
- **The way to see a removal.** `scripts/dev/vendor_manifest.py` prints the objects in the old
  vendored file that the new one lacks. The bump PR names each one and how it is removed.
- **The way out for a templated object.** Removing a Setting, a RecurringJob or any other
  templated object from a plumbing tag leaves it live. A lingering RecurringJob would keep
  running with its own `retain`. So the PR that removes one names it, shows the removal in
  the render diff, and carries a delete step for it, which is **operator-only**.
- **A dropped named member is detected.** H1.1c adds a per-tag object census with named
  members to publish. A render that drops a named member fails publish. A dropped object that
  is not a named member stays serving and goes unmanaged with nothing reporting it, so each
  tag names the objects whose loss matters most:
    - `cluster-base`: PriorityClass `homelab-critical`, ServiceAccount `homelab-readonly` and
      its token Secret `homelab-readonly-token`;
    - `metallb`: DaemonSet `speaker` and Deployment `controller`;
    - `coredns`: Deployment `coredns` and Service `kube-dns`;
    - `longhorn`: DaemonSet `longhorn-manager`;
    - `longhorn-backup`: BackupTarget `default`, BackupTarget `r2` and RecurringJob
      `daily-backup`. A disarm PR reaches a target only while Flux still declares it;
    - `longhorn-reconcile`, which prunes by default: CronJob `longhorn-reconcile`.
- **Why not per-object annotations.** One field covers both the in-tag drop and the deletion
  of the tag. A `prune: disabled` annotation on every object would need one metadata-only
  patch per upstream object, and a `commonAnnotations` shortcut would reach pod templates and
  roll every pod.

### Forward-only tags and the promote rule

Longhorn supports no downgrade, and MetalLB's CRDs carry a conversion webhook. Resetting
`prod` after a failed bump would re-apply the older vendored file over upgraded CRDs and a
manager that rolls every node at once. A reset to a revision before a disarm would re-arm
B2.

- **The flag.** `metallb`, `coredns`, `longhorn` and `longhorn-backup` set
  `flux_forward_only: true` in their defaults.
- **The hold's values.** H1 reads `DEPLOY_HOLD` as D1 and D2 define it. The value `none`
  is "no hold", and any other non-empty value, such as a SHA, is a hold. An empty or missing
  value is an error, never "no hold": `decide` prints `error` and moves nothing, and
  `land.sh` refuses. Clearing a hold is **operator-only** and is always the same command:
  `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`. contracts.md's C2 and C6
  read the same, since its X2-01 entry applied the change.
- **The rule.** When a revision fails or times out, `promote` writes `DEPLOY_HOLD` and does
  **not** reset `prod` if the revision changes a forward-only tag relative to `last-good`. A
  tag changes when its `footprint` digest in `out/digests.json` (C1) differs, when
  `last-good` lacks the tag, or when T lacks a tag that `last-good` has. The second case covers each takeover PR, whose
  `last-good` predates the tag. The third covers a revert of a takeover: a reset would
  re-create the Kustomization over objects that the restored tasks write, which is two
  writers (C4).
- **The hold's form.** A forward-only hold writes T's SHA alone. That is the form `decide`
  already writes for an ungated bump (*Longhorn and MetalLB bumps gate at the merge*).
  `verify` writes it in place of C2's `<T> failed=<names>`, never after it. The value never
  carries `failed=`, so the revert rule that C2 proposes never clears it. That rule clears a
  hold once a descendant restores the failed names' footprints. For a forward-only tag, such
  a descendant is a downgrade or a takeover revert, which is the two-writer case above. A
  forward-only hold therefore clears only by hand, with the command above.
- **Tier changes stay outside the rule.** No forward-only tag renders from
  `containers_list`. The desired tiers live in `longhorn-reconcile` (*Six Flux-born tags*),
  so a failed revision that only adds a claim or moves a tier resets like any other.
- **Where it lives.** H1.1b adds it to `promote.py` beside D2's hold-then-reset. D5.2 adds the
  same hold for snapshot roles, so the two share one predicate: a failed revision holds
  without reset when its changed tags include a snapshot role or a forward-only tag. It reads
  `out/digests.json`, which D2's first publishing PR adds (C1).
- **The range it reads.** C2's `decide` takes T, the newest published master commit, which
  can sit several commits past the last promotion. The digest rule and the gate rule
  therefore read the range (`last-good`, T]: the digest rule compares T's bundle with
  `last-good`'s, and the
  gate rule below checks every commit in the range. The commits a move newly applies are
  (`prod`, T]. The longer range also re-checks commits that passed when `prod` last moved,
  which costs a few API calls and never admits a commit the shorter range would hold.
- **The way out.** The operator diagnoses, fixes forward through a PR, and clears the hold
  with the command above.
  For a failed Longhorn bump, the recovery is the restore-from-backup procedure in
  `docs/longhorn-disaster-recovery.md`, never a revert.
- **A pin changes when its value changes.** The CI checks and `promote` both read the pin's
  value from whichever file holds the key at each SHA. H1.3 and H1.5a move the keys into k8s
  role defaults at the same version. Such a move is neither a bump nor a decrease, and
  `promote` does not ask for the gate status on it.
- **CI checks on the pins.** H1.1f adds a check over each PR's diff of
  `k3s_longhorn_version` and `k3s_metallb_version`:
    - it refuses any decrease, so a revert PR of a bump fails CI;
    - it refuses a Longhorn bump that skips a minor release;
    - it refuses a pin bump whose PR changes anything besides that role's files, its vendored
      file and docs.
- **At most one unverified bump per pin.** H1.1f checks one PR's diff, so two sequential
  PRs, `v1.12` to `v1.13` and then `v1.13` to `v1.14`, would each pass it. If both merged
  before the first applied, one promotion would apply `v1.14` over `v1.12`, a skipped minor.
  Two checks stop that:
    - **The land rule (H1.1g).** `land.sh` refuses to merge a forward-only pin bump when
      (`last-good`, master tip] already holds a change to that pin, or when `DEPLOY_HOLD`
      reads anything other than `none`. An empty or missing `DEPLOY_HOLD` refuses as well.
      It reads `last-good`'s commit with `promote.py`'s own reader, and a failed read refuses
      the merge.
    - **The backstop (H1.1b).** `decide` holds when (`prod`, T] changes one forward-only
      pin in more than one commit, or when `k3s_longhorn_version` moves more than one minor
      release between `prod` and T. It catches a merge that bypassed `land.sh`.

  The two checks read different lower bounds on purpose. A failed bump holds and leaves
  `prod` on that bump, so the land rule reads `last-good`: a rule over (`prod`, tip] would
  let a second bump merge during the hold. The backstop reads `prod`, the revision Flux last
  applied. `decide` moves `prod` only while `DEPLOY_HOLD` reads `none`, so `prod` equals
  `last-good` there unless an operator cleared a hold. A fix-forward bump after a failed
  bump is therefore **operator-only**: the operator merges it outside `land.sh`, runs
  `land.sh --regate` for it and clears the hold to `none`. The backstop then sees one hop
  from the failed bump and passes it.
  Other sessions' commits can still merge between a bump's merge and the next promote run.
  A promoted revision can therefore carry one bump together with unrelated commits, and the
  gate rule checks each commit in the range rather than assuming the bump stands alone.
- **The timeout cannot reset either.** A Kustomization that exceeds its timeout fails the
  revision. Under this rule that is a hold, never a reset.
- **A dependent that was already failing does not fail T (H1.1i).** Every per-tag
  Kustomization sets `wait: true` (C3), and every revision moves the shared source, so each
  one re-runs its health check. While a node is NotReady, the `longhorn-manager` and MetalLB
  `speaker` DaemonSets each keep a pod there that is not Ready, so `longhorn` and `metallb`
  fail every revision. C2's *Verification* expects only T's changed set (D2.5). A revision
  that leaves both tags' footprints, and every name upstream of them, unchanged does not
  expect them, and C7's `flux` rule group pages their failure instead. What is left for
  H1.1i is a dependent that was already failing (*Open questions* 3):
    - Before it moves `prod`, `decide` records the newest `flux/<name>` status on `prod`'s
      commit for every name in the changed set.
    - A dependent is a name in the changed set only through its `dependsOn` chain, with its
      own footprint unchanged. `verify T` reports a dependent whose recorded status was
      `failure` as `excused <name>` and does not expect it. C2's *Verification* states this
      rule. A T that changes `cluster-base` during an outage makes `longhorn` such a
      dependent, because `longhorn` declares `cluster-base`.
    - A name whose footprint T changed counts as before, so a revision that changes
      `longhorn` during an outage still holds. A dependent that read `success` before the
      move and fails after it counts, because T may have broken it. A dependent with no
      recorded status counts too. The footprint covers the `cluster-vars` files for a tag
      that substitutes (C1), so a `cluster-vars` change makes that tag a changed name.
    - The timeout excuse is gone. The pillar review fold narrowed contracts.md's request row
      to the recorded status. A dependent with no status at the timeout counts, even when
      its `dependsOn` chain reaches an excused name. *Risks* names the case that still holds.

  The excuse needs no cluster read, so `promote` stays a GitHub workflow.

### Longhorn and MetalLB bumps gate at the merge

After H1, merging a pin is the upgrade. Two pieces move the runbook gates to the merge.

- **`land.sh` runs the gates on every pin bump, whatever its environment.** The check sits in
  `land.sh`'s own merge path, not in `land_lib/policy.py`'s variable-switched rules. For a PR
  that changes a forward-only pin it runs:
    - `runbook_gates.py longhorn-upgrade` for `k3s_longhorn_version`;
    - for `k3s_metallb_version`, a new `metallb-upgrade` set that reuses the node-Ready and
      deployer-hold gates of `scripts/deploy_tools/runbook_gates_lib/k3s_upgrade.py`.

  It refuses to merge on a non-zero exit. It refuses outright on any host but daniel-box,
  because gate 2 reads the restore drill's stamp from daniel-box's disk (failure class 7).
  On success it posts the commit status `homelab/upgrade-gates` on the PR's head SHA, with
  the gate output in the description.
- **`promote` checks for that status, commit by commit.** With squash merges, T is a master
  commit, and T's own PR need not be the bump's PR. So `decide` walks every commit C in
  (`last-good`, T]:
    1. C changes a forward-only pin when the pin's value at C differs from its value at C's
       parent, each read from whichever file holds the key at that SHA.
    1. For each such C, `decide` maps C to its PR through GitHub's commit-to-pulls lookup,
       `gh api repos/{owner}/{repo}/commits/{sha}/pulls`, with the workflow's token.
    1. It requires a successful `homelab/upgrade-gates` status on that PR's head SHA.
       When C lies in (`prod`, T], the commits the move newly applies, the status must be
       less than six hours old. A commit at or below `prod` keeps the status it was
       promoted with, so a cleared hold on a failed bump is not held again for age.

  When one such C fails the requirement, `decide` writes T to `DEPLOY_HOLD`, prints `held`
  and leaves `prod`, so nothing is applied. A commit whose lookup returns no PR, such as a
  direct push, fails it. A lookup or status read that errors prints `error` and moves
  nothing, so the next run retries. Neither case is read as "no pin change" or as "gated"
  (failure class 3). A merge from the GitHub UI or a bare `gh pr merge` therefore deploys
  nothing until an operator runs `land.sh --regate` and clears the hold to `none`.
- **A gate status goes stale.** Gate 2's drill age and gate 3's volume health describe the
  moment the gates ran, not the moment Flux applies the bump. Promotion normally follows the
  merge within one CI run, so a status older than six hours means a hold or an outage came
  between, which is when those facts change. A stale status holds like a missing one.
  `land.sh --regate <pr>` (H1.1g) runs the same gates on daniel-box for a merged PR and
  re-posts the status without merging. The procedure that clears such a hold runs it first.
- **The residual bypass.** An operator can post the status by hand. That is an operator act,
  and the status description records who posted it.
- **The ruling.** "Merging a Longhorn or MetalLB pin is the upgrade" replaces the Renovate
  rule's "the operator drives it". Accepting it is **operator-only** (*Open questions* 6).
  H1.1g carries the `land.sh` half, the land rule, `--regate`, the `metallb-upgrade` set and
  the status post.
- **When the drill moves.** H2 moves the restore drill into a CronJob whose stamps live in a
  ConfigMap. Gate 2 then reads that ConfigMap, and the daniel-box refusal goes away with it.

### Vendored upstream manifests

- **Which roles vendor.** `metallb` and `longhorn` vendor an upstream file. `coredns` does
  not: k3s's `manifests/coredns.yaml` is not YAML until k3s substitutes its `%{...}%`
  placeholders (Baseline), so no byte-for-byte copy can render. `coredns` is a templated
  role derived from that file, described under *CoreDNS without a host read*.
- **Storage.** Upstream files live under the role's `files/upstream/`, byte for byte, with
  the version and the upstream sha256 pinned in the role defaults. Patches live under
  `files/patches/`. The renderer lists them as the Kustomization's `patches:`, and
  `probe.py health` reads the same directory (`health_vendored.py:VENDORED_SUBDIRS`).
- **Refresh.** `scripts/dev/vendor_manifest.py` fetches a pinned URL, checks its sha256,
  writes the file, and prints the objects the old file had and the new one lacks. A Renovate
  bump changes the version, and the `renovate-prs` skill runs the refresh as its manual half.
  That is the same shape as `asset_pins.py --refresh`.
- **Patches.** Changes to upstream objects are kustomize patches listed in the generated
  `kustomization.yaml`, never edits to the vendored bytes. They cover:
    - the longhorn-manager memory request;
    - the `longhorn-storageclass` ConfigMap's `storageclass.yaml`, replaced by the
      repo's class file so upstream's `numberOfReplicas: "3"` never reaches the cluster;
    - any field a controller writes at runtime, such as a webhook Secret's data or a
      conversion webhook's `caBundle`. The patch removes it, so Flux never declares it and
      never fights the controller over it.
- **One key set per directory decides substitution.** The unit is a Kustomization
  directory under `DIR/cluster/`, as in D3 *Which Kustomizations substitute (D3.3b)* and
  R2's *Open questions* 5. `<tag>` and `<tag>-deferred` are decided separately. A directory
  substitutes when its render holds at least one placeholder, read before R2's escape pass.
  The one source for that fact is the renderer's placeholder key set for the directory,
  computed in process from the render. Three things read that key set and nothing
  re-derives it:
    - D3's rule, which gives the directory's Kustomization `postBuild.substituteFrom` and
      its `cluster-vars` edge;
    - C1's escape, which turns a literal `${` into `$${` in that directory only;
    - the vendored-file refusal below.

  `spec.postBuild` in that directory's Kustomization under `DIR/flux/` is a projection of
  the key set, not a second source. R2's CI rules and its round trip read the key set,
  because R2's 56-tag CI render writes no `DIR/flux/` file for most tags (*Open questions*
  2).
- **No substitution with vendored files.** A directory carrying vendored files must not
  substitute, so its Kustomization has no `postBuild`. The renderer refuses a directory that
  holds vendored files and whose placeholder key set is not empty. That keeps any `${`
  inside upstream bytes away from Flux's substitution. This is why the backup targets get a
  tag of their own.
- **No escape either.** C1's `$${` is turned back only by Flux's substitution. A directory
  that does not substitute skips the escape, so a `${` in the reconcile script or an
  upstream file reaches the cluster unchanged.
- **The fleet guards do not read the vendored files (H1.1a).**
  `ansible/tests/_k8s_render.py:rendered_docs` renders each role's `templates/*.j2`, so the
  verbatim files and the kustomize patches stay outside it. The render-time guards never see
  the upstream workloads: the `_SYSTEM_TIER` priority-class guard, the non-root and uid
  guards of `ansible/tests/k8s/test_container_security_context.py`, the strategy guard,
  `shell_offences`, the volume-name guard, and the `pods-disable-service-link-env-vars` and
  `rendered-kinds-have-vendored-schemas` rows.
    - **Why that is accepted.** At `70591c307`, `Install Longhorn` and `Install MetalLB` apply
      the upstream file from its URL, outside every guard. Vendoring removes no check that
      runs today. Making the upstream bytes meet the fleet conventions would need a patch per
      object for each guard, and every refresh would re-merge them.
    - **What checks them instead.** The sha256 pin ties the bytes to upstream's release. The
      per-tag object census (H1.1c) names the members whose loss matters. H1.1d's health
      targets resolve the vendored workloads by name. The takeover census and the diff before
      each flip show every field that changes.
    - **Source-tree guards still read them.**
      `ansible/tests/k8s/test_namespace_resource_defaults.py:test_no_resourcequota_is_declared_anywhere`
      runs `git grep` over `ansible/`. H1.3 and H1.5a therefore grep their vendored file for
      each kind such a guard forbids, and strip or exempt each match in the same PR.
    - The templated objects of the same roles join `rendered_docs()` like any template. Each
      PR that moves one meets the guards for it.

### Where the Longhorn variables go

Each key goes to exactly one place, decided by who reads it. The one exception is a
mirrored pair, two keys with different names that a test holds equal:

- **Read only by objects that move:** the k8s role's defaults, under the name it has at
  `70591c307`. Renovate's annotation manager covers `ansible/roles/k8s/*/defaults/main.yml`,
  so `k3s_longhorn_version` and `k3s_metallb_version` stay tracked after they move.
  `k3s_readonly_crd_api_groups` moves to `cluster-base` in H1.2, and the headlamp comment
  that mirrors it names the new place.
    - A key moves in the PR that deletes its last host reader, never earlier, because a k8s
      role's defaults are invisible to `setup/k3s` (Baseline).
    - The three tier lists all move to the `longhorn-reconcile` role's defaults in H1.7a,
      which
      deletes `Route the selected volumes to R2` and `Return de-listed volumes to the
      default backup target`. H1.6 deletes the last readers of the weekly and no-backup
      lists, but its reconcile ConfigMap calls the same three filters directly over
      `containers_list` until H1.7a points it at the keys. The filters and their input are
      the same, so both forms render the same lists.
- **Read by a moved object and by a host file that stays:** a mirrored pair, never a shared
  file. `k3s_readonly_sa_name` and `k3s_readonly_sa_namespace` are the two members. The host
  files read them to write the kubeconfig and the audit policy, and `readonly-rbac.yaml.j2`
  reads them to name the ServiceAccount.
    - From H1.2, the template reads `cluster_base_readonly_sa_name` and
      `cluster_base_readonly_sa_namespace` from `cluster-base`'s defaults. The two `k3s_`
      keys stay in `setup/k3s`'s defaults for the host files.
    - A test asserts each pair equal, so a rename must change both in one PR.
    - That PR changes `setup/k3s`'s defaults, so its merge leaves a `manual_plane` line, and
      the host half is applied on purpose. Until that apply, the stale kubeconfig keeps
      working, because `cluster-base` is unpruned and the old ServiceAccount stays.
    - `group_vars/all.yml` was rejected for this pair. A change there leaves no owed line,
      which is the staleness X-10 found, and the deployer routes it as a broad deploy-plane
      change (Baseline).
    - `k3s_longhorn_r2_region` (`auto`) is the second pair. `longhorn-backuptarget-r2.yaml.j2`
      reads it in the R2 target's URL, and `etcd-snapshot-offbox.sh.j2` reads it as
      `--s3-region`. H1.7b defines `k3s_longhorn_r2_region` in the `longhorn-backup` role's
      defaults, under its baseline name, for the moved template. It adds the twin
      `k3s_etcd_s3_region` to `setup/k3s`'s defaults, beside the script's other
      `k3s_etcd_s3_*` keys, and repoints the script at it. A test holds the two equal. The
      rendered script does not change, because the value does not. H2.8b deletes the twin
      with the upload, and H2.8a's etcd S3 Secret reads `k3s_longhorn_r2_region` from
      `longhorn-backup`'s defaults.
- **Read by a moved object and by a host cron:** no key stays in this class. Five keys are
  in it at baseline: `k3s_longhorn_backup_armed`, `k3s_longhorn_r2_armed`,
  `k3s_longhorn_backup_cron`, `k3s_longhorn_weekly_backup_minute_hour` and
  `k3s_longhorn_weekly_backup_retain`. A sixth, `k3s_longhorn_r2_region`, leaves the class as
  the mirrored pair above, because its cron uploads etcd snapshots and never reads a
  target's state. A shared file such as `group_vars/all.yml` would leave
  the cron stale. A flip there promotes to the cluster on the next revision, while the
  deployer routes it to the deploy plane with no owed line, so the baked cron keeps the old
  value until someone hand-runs `k3s-bringup.yml` (Baseline). A stale disarm leaves the
  monitor permanently red, the failure ADR-0007 warns about, and a stale arm leaves the new
  target unchecked. So H1.7a makes the two host readers read the applied state from the
  objects Flux renders, and H1.7b moves all five keys into the `longhorn-backup` role's
  defaults:
    - `longhorn_backup_health.py` derives each target's arm state from one list of both
      BackupTargets: a blank `spec.backupTargetURL` is disarmed. The same list carries
      `status.available` and the condition message, so it replaces the up to four
      per-target calls. It derives the daily and weekly run times from the first two fields
      of each tier's RecurringJob `spec.cron`, read in one more list call. The worst case
      falls from 18 kubectl calls to 16. The planning-time figure of 17 to 15 predates check
      11's pod-log read, which the shim's header counts.
    - A disarmed target stays named. The verdict's `DISARMED: <targets> (<n> volume(s) not
      checked)` note, from `build_verdict`, carries it, which is the naming ADR-0007 asks
      for.
    - A failed list call, or a list without a target named `default`, is a problem the
      reader reports. It is never read as a disarm (failure class 3), unlike
      `longhorn_cluster.py:backup_target_url`.
    - From H1.7b, a target carrying `kustomize.toolkit.fluxcd.io/reconcile=disabled` is a
      problem the reader reports, and never a disarm. Flux's next apply restores a URL
      blanked by hand, so only a target that carries the annotation can stay blank against
      git. The annotation means a break-glass is in force, and the monitor stays red until
      step 4 of the break-glass removes it. A forgotten break-glass therefore cannot leave a
      target suppressed behind a green monitor. The same list call returns the annotation,
      so the check adds no kubectl call. Before H1.7b no break-glass exists, and only the
      arm flag, through the owed apply, writes a blank URL.
    - The b2-budget cron drops `--retain`. `probe.py b2-budget` then reads `spec.retain` from
      the live weekly RecurringJobs, and a failed read exits non-zero rather than falling back
      to the parser's default of 2. `--retain` stays for a hand run. The read happens after
      the prune snapshot is written, so a failure costs the report and never the prices
      `b2-deletions` needs. `format_backup_budget` accepts `retain` and does not read it in
      its present form, so the live read changes the exit status and not the report.
    - The health grace stays derived from the schedule, now the one the cluster runs.
- **Read only by host crons:** they stay in `setup/k3s/defaults/main.yml` until H2.

H1.7b adds a census row that keeps the middle class empty. No key defined in the defaults of
one of the six Flux-born roles may appear in a task or template file under
`ansible/roles/setup/`. Its named members are `k3s_longhorn_backup_armed` and
`k3s_longhorn_backup_cron`, which it must find in `longhorn-backup`'s defaults. Every PR that
moves a key runs `git grep -l <key>` for it and lists every reader it repoints in its body.

The row exempts two keys by name, `k8s_autodeploy` and `k8s_autodeploy_reason`. Every k8s role
defines them as a convention, and the release-staleness check and the Renovate prompt read the
convention across all roles, not the value of one Flux-born role. A flip of either changes a
stance and no host-visible state. H1.7b found this when the row first ran over the real tree.

### Longhorn objects that are not static

- **Settings.** Each of the seven settings tasks becomes one `longhorn.io/v1beta2` `Setting`
  object with `value:`, rendered from the same variables.
- **StorageClasses.**
    - Flux owns the `longhorn-storageclass` ConfigMap through the patch on the vendored
      object. longhorn-manager writes the `longhorn` class from it.
    - `longhorn-nobackup` renders as before and carries `kustomize.toolkit.fluxcd.io/force:
      enabled`. Flux then deletes and recreates it when an immutable parameter changes, which
      is what the deleted Ansible task did for `longhorn`.
    - `probe.py health longhorn` compares the live `longhorn` class's parameters with the
      ConfigMap's class and fails on a difference. The baseline shows that the manager
      creates the class, but not that it replaces a class whose parameters differ. If the
      check goes red after a parameter change, the fix is an **operator-only** delete of the
      class, which the manager recreates from the ConfigMap. The check is built ahead of
      H1.5a, in `scripts/diagnostics/probe_lib/health_plumbing.py` (#5923), and runs once
      the `longhorn` tag resolves to a k8s role.
- **`longhorn-reconcile`.** This CronJob runs hourly, with `concurrencyPolicy: Forbid`. It
  takes over the live-state tasks.
    - **What it runs.** A stdlib Python script, mounted from a ConfigMap, in a digest-pinned
      `python` image, as `uptime-kuma`'s `kuma-status-page-sync` does. It calls the API with
      its own ServiceAccount.
    - **Its permissions.** These are the verbs the ported tasks call, and nothing else:
        - in `longhorn-system`, `get`, `list` and `patch` on `volumes.longhorn.io`;
        - in `longhorn-system`, `get` and `patch` on Deployments, with `resourceNames`
          `csi-attacher`, `csi-provisioner`, `csi-resizer` and `csi-snapshotter`
          (`resourceNames` takes exact names, never a prefix);
        - cluster-wide, `list` on `persistentvolumeclaims`, through a ClusterRole.
    - **What it reconciles.**
        - The desired tier of each claim, rendered into its ConfigMap by the same filters
          that compute `k3s_longhorn_r2_volumes`, `k3s_longhorn_weekly_volumes` and
          `k3s_longhorn_nobackup_volumes`. H1.6 calls the filters directly, and H1.7a reads
          the three keys once they move into this role's defaults.
        - The default, no-backup and weekly-shard group labels. The return-to-`default` arm
          reads live PVC classes, as the task did: a `no-backup` volume whose PVC has class
          `longhorn`, and which no list names, returns to `default`. The ConfigMap's lists
          only override the class.
        - The legacy weekly label migration.
        - The CSI sidecar memory requests.
        - From H1.7a, each Volume's `spec.backupTargetName`.
    - **What it keeps.** It keeps the `DECIDED groups: [default]` decision: labels go on
      Volume CRs, exactly as the tasks wrote them.
    - **Contract.** The reconcile actuator's part of the `## Autonomous-role contract` moves
      from `ansible/roles/setup/k3s/CLAUDE.md` into the `longhorn-reconcile` role's
      `CLAUDE.md`.

### The backup plane and its levers

- **The `longhorn-backup` tag.** It holds the two credential Secrets, both BackupTargets and
  the RecurringJobs. The RecurringJobs move here, not into `longhorn`, because
  `k3s_manage_backup_targets` has always withheld them together with the targets. Keeping
  them together also puts every key the targets and jobs read in one role's defaults.
- **The `default` BackupTarget gets a template.** It declares only three fields:
    - `backupTargetURL`, `s3://<bucket>@<region>/<prefix>` while `k3s_longhorn_backup_armed`
      is true and an empty string while it is false;
    - `credentialSecret: longhorn-b2`;
    - `pollInterval`, from `k3s_longhorn_backupstore_poll_interval`.

  `r2` keeps its template and the same arm semantics under `k3s_longhorn_r2_armed`.
- **Placeholders only.** The B2 region becomes a plain variable, `k3s_longhorn_b2_region`, in the
  `longhorn-backup` role's defaults. The region is not a secret: `docs/break-glass.md`
  already publishes the B2 target URL, and the region assert's message already names this
  key as the override. Writing it needs no decryption.
    - The URL then holds one placeholder, `${longhorn_b2_bucket}`, and no Jinja filter runs
      over a secret.
    - The credential templates move from `data:` with `b64encode` to `stringData:` with bare
      placeholders, which R2's rule 5 requires.
    - `AWS_ENDPOINTS` renders from the plain region and carries no placeholder.
    - R2's rule 3 confines a placeholder to `kind: Secret`. R2 accepted H1's request in its
      *Open questions* 10: a frozenset `PLACEHOLDER_KIND_EXEMPT` of `(key, kind)` pairs sits
      beside `PLACEHOLDER_ANYWHERE`, with the members `(longhorn_b2_bucket, BackupTarget)`
      and `(r2_bucket, BackupTarget)`. R2 assigns that edit to H1.7b: the set, its
      `_is_clean` and `_is_flagged` pair, and its named member, the `default` BackupTarget
      in the `longhorn-backup` render, all land in H1.7b's change to `secret_checks.py`.
      H1.7b waits on D3, which waits on R2, so the file exists by then (*Open questions* 5).
    - **The named member also holds for a blank URL.** A disarm PR renders
      `backupTargetURL: ""`, so no placeholder is left to find, and a member that required one
      would fail the render of the PR that applies the incident lever. The member passes when the
      URL is blank, and fails a non-blank URL that names no `${longhorn_b2_bucket}`. The
      `# DECIDED:` marker beside the member in `secret_checks.py` records the choice.
- **Two endpoint values can now disagree.** The regex existed so that the region could not
  drift from `longhorn_b2_endpoint`. After H1.7b, nothing ties the two together. A wrong
  region makes the target unreachable, which the availability check below reports within
  one probe run.
- **`k3s_manage_backup_targets` retires in H1.7b, and H1 never sets `flux_suspend`.** The
  flag exists for a cluster without real credentials, and no host overrides it (Baseline).
  Staging, the one such cluster, was retired on 2026-09-29. Under Flux, both incident levers
  render content: each arm flag blanks one URL. This resolves D5's Open question 13 on the
  content side. H1 holds no lasting suspend, so D5.1 is the only slice that defines
  `flux_suspend`, and H1.1a adds nothing of it.
    - An operator who must stop the whole tag uses D5.1b's `flux_suspend` as D5 defines it, a
      temporary stop. D5.1d then refuses every render change to `longhorn-backup` until the
      resume, a disarm included, unless the same PR removes the key.
    - The incident path is therefore the disarm PR and the BackupTarget break-glass below,
      never a suspend.
- **Availability.** The task `Check whether Longhorn can reach the backup target` becomes a
  `probe.py health longhorn-backup` check. It reads each BackupTarget's `status.available`,
  skips a disarmed target with a blank URL, and fails when it finds no target named
  `default`.
- **The incident lever under Flux.** The normal path is a disarm PR: set
  `k3s_longhorn_backup_armed: false` and land it. Promote applies it like any revision. A
  later failed revision never resets across it, because `longhorn-backup` is forward-only.
  Three cases defeat the normal path: a disarm cannot promote while `DEPLOY_HOLD` holds a
  value other than `none`, a
  spiral can outrun a merge, and a promoted disarm does not apply while `longhorn` is not
  Ready (*Six Flux-born tags*). For all three the runbook carries an **operator-only**
  break-glass:
    1. Annotate both BackupTargets in the cluster with
       `kustomize.toolkit.fluxcd.io/reconcile=disabled`, so kustomize-controller skips them.
    2. Patch each `backupTargetURL` blank with `sudo k3s kubectl patch`.
    3. Land the disarm PR, then clear the hold with
       `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`.
    4. Wait until `longhorn-backup`'s `status.lastAppliedOriginRevision` names the disarm
       commit or a descendant of it, checked with `git merge-base --is-ancestor`. Only then
       remove the annotation. `prod` carrying the disarm is not enough, because the tag
       applies nothing while `longhorn` is not Ready.

  Until step 4, the host backup-health reader reports each annotated target as a problem
  (*Where the Longhorn variables go*). `probe.py landing` lists the annotation too.
  `landing_blockers.py:run_landing` reads the objects the two H1 break-glass procedures
  annotate and names each one that carries `kustomize.toolkit.fluxcd.io/reconcile=disabled`.
  H1.4 adds the listing with the CoreDNS Deployment, ConfigMap and Service, and H1.7b adds
  both BackupTargets. Those five are its named members. A cluster-wide search for an
  annotation is not one call, so the listing covers the objects a runbook tells an operator
  to annotate. D5 extends the same subcommand for `spec.suspend`.

  No break-glass rests on that annotation until it has been proven. H1.2 proves, against the
  installed Flux version, that kustomize-controller skips an inert ConfigMap in
  `cluster-base` that carries it in the cluster.
- **Contract.** The RecurringJob actuator and the arming text for the two arm flags move
  from `ansible/roles/setup/k3s/CLAUDE.md` into the `longhorn-backup` role's `CLAUDE.md`. The
  `k3s_manage_backup_targets` clause of the *Mode / arming* bullet is deleted with the flag.

### CoreDNS without a host read

The `coredns` role is a templated role. Its templates are derived from k3s's
`manifests/coredns.yaml` at `k3s_version`, with each `%{...}%` placeholder written as the
value k3s substitutes on this cluster. It vendors nothing, because those bytes are not YAML
(Baseline), so no sha256 pin and no `vendor_manifest.py` refresh apply to it. A k3s bump
refreshes the templates by hand, and the test below ties them to the version.

- **The ConfigMap carries only `Corefile`.** `NodeHosts` stays the field k3s's node
  controller writes, and Flux never declares it. A test asserts that the rendered ConfigMap
  has exactly one data key. `probe.py health coredns` fails when the live `NodeHosts` is
  empty. This check and the resolution check below are built ahead of H1.4, in
  `scripts/diagnostics/probe_lib/health_plumbing.py` (#5924), and run once the `coredns` tag
  resolves to a k8s role.
- **`clusterIP`** renders as the live `10.43.0.10`, because the field is immutable.
- **The image** becomes a pin, `coredns_image`. The test that pins it is
  `test_coredns_image_follows_bundled_manifest.py`, retargeted. It keeps an excerpt of the
  k3s manifest's image line, labelled with the k3s version it came from, and asserts three
  things:
    - the label equals `k3s_version`, read from `setup/k3s`'s defaults;
    - `coredns_image` equals the excerpt's image with `%{SYSTEM_DEFAULT_REGISTRY}%` replaced
      by the empty string, as k3s stages it;
    - the rendered Deployment's image equals `coredns_image`.

  H6's version bump moves `k3s_version`, the excerpt, the pin and any template change
  together. A packageRule disables Renovate for this pin, because the k8s image manager
  matches every `_image:` key in k8s role defaults.
- **The `.skip` marker stays a host file.** It stays in `setup/k3s`, after the server
  install, until H6 moves it into the package. On a new cluster it must appear only after
  k3s has first created CoreDNS. Otherwise no CoreDNS exists, and Flux cannot resolve GHCR
  to fetch the bundle that would create one. Today `Read the live Corefile` follows the
  marker task and fails while CoreDNS is absent. That read leaves with the Corefile tasks, so
  H1.4 keeps a bounded read of Deployment `coredns` before the marker task.
- **Flux depends on the DNS it owns.** Flux's controllers resolve GHCR through cluster DNS,
  so a broken CoreDNS revision can block its own fix.
    - The Corefile template already enables CoreDNS's `reload` plugin, which documents that
      a Corefile that fails to load leaves the running config in place. That protects only
      a running pod. A pod that restarts with a bad Corefile crashloops.
    - A test in the role's `tests/` parses the rendered Corefile and asserts named members:
      the `forward` line lists the Pi-hole VIP first, the `kubernetes cluster.local` block
      exists, and `reload` is present. It cannot prove that CoreDNS loads the file.
    - `probe.py health coredns` resolves a cluster name and an external name through the
      `kube-dns` Service, as the task `Verify cluster DNS resolves through the configured
      upstream` did. It catches a Corefile that loads but resolves wrongly.
    - `coredns` is forward-only, so a failed revision holds rather than reset `prod` into a
      fetch that needs the DNS that just failed.
    - `docs/k3s-upgrade.md` gains an **operator-only** break-glass. Annotate the CoreDNS
      objects in the cluster with `kustomize.toolkit.fluxcd.io/reconcile=disabled`, the skip
      H1.2 proves. Then re-apply the `coredns` tag rendered at the `last-good` SHA with
      `sudo k3s kubectl apply -k`. Then fix forward through a PR and remove the annotations.
- **The templates meet the fleet guards.** They sit under `templates/`, so `rendered_docs()`
  reads them. H1.4 gives each guard one answer, read from k3s's manifest at `v1.37.1+k3s1`
  through `gh api` on 2026-10-11:
    - The Deployment calls `spec_shell('RollingUpdate', max_unavailable=1)` and `pod_shell`,
      because `ansible/tests/k8s/test_workload_shell_uses_the_macros.py:shell_offences`
      requires both. `spec_shell` writes `revisionHistoryLimit: 3` over upstream's `0`. That
      is a spec field, so nothing rolls for it.
    - **Operator-visible:** `pod_shell` takes the literal `system-cluster-critical`, and
      `ansible/tests/k8s/test_pod_template_hygiene.py:_SYSTEM_TIER` gains a
      `("coredns", "deployment.yaml.j2")` row with its reason. CoreDNS outranks every homelab
      tier as k3s ships it, and every tier and Flux itself resolve names through it. The
      literal passes
      `ansible/tests/k8s/test_priority_class_is_derived_from_tier.py:test_no_tiered_role_passes_pod_shell_a_literal_class`,
      which reads tiered roles only, because the entry stays untiered (*Six Flux-born tags*).
    - `pod_shell` writes `enableServiceLinks: false`, which the
      `pods-disable-service-link-env-vars` row requires and upstream lacks.
    - Upstream asserts no `runAsNonRoot`, so
      `ansible/tests/k8s/test_container_security_context.py:test_every_container_asserts_non_root_or_is_allowlisted`
      needs a decision. H1.4 reads the uid that the pinned image declares. A non-root uid is
      pinned beside `runAsNonRoot`, as D1 pins Flux's. A root uid takes a `_ROOT_ALLOWED` row
      with its reason. The container keeps upstream's `drop: [all]` and `NET_BIND_SERVICE`,
      which binding port 53 needs.
    - `config-volume` and `custom-config-volume` are not in
      `ansible/tests/k8s/test_volume_names_descriptive.py:GENERIC`, so both names stay. Every
      kind is in a core group, so the schema row passes.
- **The takeover rolls the pod, so H1.4 lands in a window.** `enableServiceLinks: false`, and
  any uid pin, change the pod template, so the single replica rolls at the promote with a DNS
  gap. The derived Deployment can also differ from the frozen addon in resources and probes.
  The diff before the flip, run with `--force-conflicts`, shows each value that changes. The
  takeover census shows the rest: a `kubectl-set` field the render omits is `removed`, and a
  `deploy@daniel-box` field the render omits is `kept` and stays live.

### Bring-up and DR order after H1

Two objects Flux owns are needed before Flux can run. Flux's own controllers name the
`homelab-critical` PriorityClass, and `kubeconfig.yml` reads the read-only token Secret. Both
live in `cluster-base` (Baseline). So the operator applies `cluster-base` by hand before
installing Flux.

**Who owns the order.** The order below governs from H1.2 until D5's flux PR, while D1's
Ansible-owned `flux` role still installs Flux. From the flux PR on, D5's *Bootstrapping Flux
on a rebuilt cluster* in `docs/flux-deploy-plane.md` is the single owner. It carries this
order with its own install in place of steps 4 and 11, and its kind drill covers it. R4's
PR 7b and PR 8 cite that section and write no order of their own. Cross-slice notes ask D5
and R4 for those changes.

On a new cluster, after H1.2, the order is below. The rule of
`docs/longhorn-disaster-recovery.md` stays: restore volumes before any deploy. Once the root
`homelab` Kustomization runs, every Flux-owned stateful tag at `prod` provisions fresh empty
PVCs under the backed-up names, and Flux re-applies any PVC the operator deletes. So the root
stays suspended until the restores finish. Steps 1, 3, 4, 7a and 12 run playbooks that need
`become`, so the operator runs them as well.

1. Run `k3s-bringup.yml --skip-tags kubeconfig`. It installs k3s, and k3s creates its
   bundled CoreDNS. The play skips the token wait, which would fail here, so it goes on to
   the join firewall rules, the static address, the CoreDNS read and the `.skip` marker.
   From H1.5a until H1.7b, step 1 skips two more tags:
   `k3s-bringup.yml --skip-tags kubeconfig,longhorn,longhorn_backup`. *The Longhorn window,
   H1.5a until H1.7b* below gives the reason.
2. **Operator-only.** Render `cluster-base` and apply it under kustomize-controller's own
   field manager, so Flux's first apply finds its own fields:
   `uv run python scripts/renderer/homelab_render.py cluster --out <out> --role cluster-base`,
   then
   `sudo k3s kubectl apply --server-side --field-manager=kustomize-controller -k <out>/cluster/cluster-base`.
   This creates the PriorityClasses and the read-only identity. The directory substitutes
   nothing, so the placeholder-mode render applies as it stands.
3. Run `k3s-bringup.yml --tags kubeconfig`. The token Secret exists, so the bounded wait
   passes.
4. Run D1's `flux` role with `flux_sync_suspend: true`, which renders `suspend: true` on the
   OCIRepository `homelab` and on the root Kustomization (D1 *Design*). Its pods admit,
   because step 2 created `homelab-critical`. Then, **operator-only**, resume the
   OCIRepository alone with D5's hand-patch form, so the children have a source:
   `sudo k3s kubectl -n flux-system patch ocirepository homelab --type merge --field-manager=homelab-operator -p '{"spec":{"suspend":false}}'`.
   The root stays suspended, so no D1 variable is needed for a root-only suspend. Nothing
   runs the `flux` role again before step 11: a run with the role's defaults would resume
   the root before the restores.
5. **Operator-only.** Create `flux-system/sops-age` from the password manager with D3.0's
   command. `cluster-vars` cannot decrypt without it, so `longhorn-backup`, which substitutes,
   cannot apply.
6. **Operator-only, from H1.7b.** Confirm that `prod`'s `longhorn-backup` render arms both
   backup targets, which the defaults do. The arm flags are git from H1.7b. If `prod`
   disarms one, as a B2 cap incident can leave it, the arm PR merges and promotes now.
    - A promotion needs `DEPLOY_HOLD` to read `none` (C2). If it holds a value here, a
      promotion timed out after the cluster was lost or during steps 1 to 5. The operator
      clears it with step 12's command before the arm PR merges.
    - The arm PR's verify cannot pass while the root is suspended. The revision then holds
      and leaves `prod` on the arm commit, because `longhorn-backup` is forward-only.
    - That hold stays until step 12. It also does step 7's DR hold's job, so step 7 sets no
      DR value over it.
7. **Operator-only.** Fix the revision, then apply the child Kustomizations the restore
   needs, rendered at the commit `prod` names. First the operator waits until no run of the
   `promote` workflow is queued or in progress, so that `DEPLOY_HOLD` and `prod` stop
   moving. An arm PR's verify from step 6 writes its hold by then.
    1. **A hold that step 6 did not leave comes first.** When `DEPLOY_HOLD` holds a value
       other than `none` that step 6 did not leave, a promotion timed out after the cluster
       was lost or during steps 1 to 6, because no Flux status could reach it. That run
       left `prod` at `last-good`, or at T when T changes a forward-only tag. The operator
       decides whether `prod` stays there. Once D5.2b2 adds it, the operator may instead
       dispatch `reset_to_last_good`, as D5's bootstrap step 2 allows. The hold itself
       stays until step 12, because it already stops promotions.
    2. **The DR hold.** When `DEPLOY_HOLD` reads `none`, the operator sets
       `gh variable set DEPLOY_HOLD --body dr-restore -R DanielH2018/server`. `decide` then
       moves nothing (C2), so `prod` names the commit rendered here until step 12. Without
       it, another session's merge would promote, time out in `verify` with the root
       suspended, write a hold and reset `prod` to `last-good`. The OCIRepository would then
       serve a commit other than the one the children were rendered at.
    3. **Read `prod`'s commit from GHCR.** The operator reads it from the
       `org.opencontainers.image.revision` key of `prod`'s manifest in GHCR, which C2's push
       sets to `master@sha1:<sha>`, as D5's bootstrap step 2 does. The cluster holds no
       Kustomization status to read it from yet.

   Then apply:
    - the `flux/<tag>.yaml` of each plumbing tag that exists at `prod`, and of every
      Kustomization in their `dependsOn` closure;
    - `flux/cluster-vars.yaml`, which `longhorn-backup` substitutes from;
    - once D5.1a1 adds it, `flux/homelab-namespace.yaml`. Step 10 creates claims in
      `homelab`, and while the root is suspended nothing else creates that namespace.

   Use
   `sudo k3s kubectl apply --server-side --field-manager=kustomize-controller -f <file>`.
   That is the root's own field manager, so the root adopts them when it resumes. The
   operator waits until each reads `Ready`.
    - **Why `prod` and not `last-good`.** The OCIRepository serves `prod`'s artifact, and the
      root applies `prod`'s Kustomizations when it resumes. A child rendered at another
      commit would differ from what the root then applies. For a forward-only tag that
      difference is a Longhorn or MetalLB version change right after the restores.
    - **Why the hold stays until step 12.** Every hold present from here on, step 6's,
      a stray one from sub-step 1 or the DR value, stops `decide` from moving `prod`. The
      restores can take hours, and the children of this step follow whatever `prod` names.
    - **Step 7a, from H1.5a until H1.7b only.** The window's own step runs here, after
      `longhorn` reads `Ready` and before step 8 (*The Longhorn window, H1.5a until H1.7b*).
8. The agent join and the storage smoke test run. The join waits for the node's Longhorn
   CR, and the smoke test binds a Longhorn volume, so both need step 7. The smoke test
   deletes its claim again, and no backup carries that claim's name.
9. **Operator-only.** Force the backupstore sync and wait for both targets to read
   available, as the DR document's step 3 says.
10. **Operator-only.** Run `uv run python scripts/deploy_tools/runbook_gates.py longhorn-dr`.
    Then restore each volume and create its PV and PVC, as the DR document's step 4 says.
11. **Operator-only.** Resume the root with the same hand-patch form:
    `sudo k3s kubectl -n flux-system patch kustomization homelab --type merge --field-manager=homelab-operator -p '{"spec":{"suspend":false}}'`.
    The root applies every Kustomization at `prod` and adopts the children of step 7. The
    stateful tags bind the restored claims.
12. Ansible-owned workloads deploy. Until D5, an Ansible-owned role with a PVC or a
    LoadBalancer has no `dependsOn` edge to `longhorn` or `metallb`. Step 7's wait is what
    orders it. Once every status on `prod`'s commit reads `success`, the operator clears the
    hold that steps 6 and 7 left, whether step 6's SHA, a stray hold or `dr-restore`, with
    `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server` (**operator-only**). The
    next `promote` run then moves `prod` to the newest published master commit as usual.

Step 2 exists from H1.2. Before H1.2, k3s bring-up applies the PriorityClasses and the
identity itself. Before H1.5a, step 1 installs Longhorn and creates the backup targets, and
the play's variables arm them, as the DR document's step 2 says. From H1.5a until H1.7b,
step 7a creates and arms the targets. From H1.7b, step 7 applies `longhorn-backup`, which
creates them, and step 6 confirms that its render arms them. Step 5 applies once D3.0 has
created the cluster key.

The stale-checkout guard runs in every Ansible step that selects a mapped writer task, after
the `server.yml` import (*Steps*, H1.1h). Its instances pass in steps 1 and 3, because Flux
is not installed yet and the API has no Kustomization resource type. They pass in step 7a
for the reason that step gives.

**The Longhorn window, H1.5a until H1.7b.** H1.5a deletes `Install Longhorn`, so from H1.5a
Longhorn first reaches a new cluster at step 7, through Flux. Until H1.7b, `setup/k3s` still
holds tasks that read or write Longhorn objects. None of them carries the `kubeconfig` tag.
An unchanged step 1 would run them against a cluster with no `longhorn-system` namespace and
no Longhorn CRDs, and step 1 would fail. At `70591c307` these tasks are:

- in `tasks/longhorn.yml`, all tagged `longhorn`: `Apply the Longhorn metrics
  NetworkPolicy`, `Wait for the Longhorn driver deployer`, the CSI sidecar patches, the
  `settings.longhorn.io` patches and the label reconcile;
- in `tasks/longhorn-backup.yml`, all tagged `longhorn_backup`: `Reconcile the Longhorn
  backup credential`, the `backuptargets.longhorn.io` patches, the R2 target and the
  RecurringJob.

The window's order changes two steps:

- **Step 1** also skips `longhorn` and `longhorn_backup`. The three host tasks that Longhorn
  needs on the node, `Install Longhorn prerequisites` and the multipathd pair, still run.
  H1.5a moves them into `tasks/longhorn-host.yml` under the tag `longhorn_host`. They must
  carry neither skipped tag, because a `--skip-tags` exclusion matches any tag a task
  carries.
- **Step 7a (operator-only)** runs once `longhorn` reads `Ready` in step 7, and before
  step 8. The operator runs `k3s-bringup.yml --tags longhorn,longhorn_backup`, with the arm
  variables `k3s_longhorn_backup_armed` and `k3s_longhorn_r2_armed` set to `true` as the DR
  document's step 2 sets them. The run applies what step 1 skipped: the leftover Longhorn
  tasks, both credentials, both backup targets and the RecurringJob.
    - **Which checkout.** Step 7a runs from a checkout at the commit `prod` names, which
      step 7 read. Each H1 PR deletes a task's guard-map row in the commit that hands the
      task's objects to Flux. So at any one commit, no row names an object that the same
      commit's Kustomizations hold, and the guard passes. A checkout older than `prod`
      fails the guard, which is the guard's purpose. A checkout newer than `prod` would
      apply nothing for the objects it has already handed to Flux.
    - **Why before step 8.** The Settings, the replica count among them, are then in place
      before the smoke test binds its volume, as they are after today's step 1. Step 9's
      sync then finds both targets.
    - **What the tags select as the window goes on.** The command stays the same, and
      `--tags longhorn` selects fewer tasks after each PR. From H1.5b, the Settings,
      `longhorn-nobackup` and the NetworkPolicy come from `longhorn` in step 7. From H1.6,
      `tasks/longhorn.yml` is gone. The tag then selects only `Set the Longhorn backup-store
      poll interval`, which H1.6 moves into `longhorn-backup.yml`. The label reconcile is
      then `longhorn-reconcile`'s CronJob, which the root applies at step 11.

From H1.7b, neither tag selects a task, and step 7 applies `longhorn-backup`. H1.7b
therefore removes step 7a and step 1's two extra skips.

**Which PR edits each runbook.**

- H1.2 writes this order into `docs/longhorn-disaster-recovery.md`'s *Procedure (fresh
  host, total loss)*, into `docs/break-glass.md` and into the header of `storage_smoke.yml`.
  Each later H1 PR that removes an install updates the same three places. For the Longhorn
  window:
    - H1.5a writes step 1's two extra skips and step 7a, with the list of what step 7a
      applies;
    - H1.5b and H1.6 each rewrite that list;
    - H1.7b removes step 7a and the two skips, and says that step 7 creates the targets.
- D5's flux PR replaces the order in the two documents with a pointer to its section, which
  carries the order from then on. It also rewrites `docs/k3s-etcd-restore.md`'s *After the
  restore*, which no H1 PR touches.
- R4's PRs edit none of the three runbooks.

### `containers_list` leaves the inventory (H1.9)

After R4, no Ansible k8s play reads `containers_list`, and every k8s entry is `owner: flux`.
Two Ansible readers remain: `ansible/preflight.yml`, and the Pi's setup roles, which read the
Pi's own four entries until H5. PR H1.9 then does these things:

- moves the 56 daniel-box entries to `cluster/services.yml`, admitted by `!/cluster` in
  `.gitignore`;
- drops the `owner` key, because every k8s entry is Flux-owned;
- rewrites, in the same PR, every reader of `owner` that is still in the tree, so each reads
  every entry of `cluster/services.yml`. At planning time those are D2's `flux_owned_tags`,
  the publish census, promote's expected-status set and R4's ownership test. A `git grep` for
  `owner` readers at H1.9's base is the census of record;
- gives the publish census a floor of named members, `longhorn`, `coredns`, `metallb` and
  `traefik`, so an empty or shrunken set fails publish;
- deletes `containers_list` from `daniel-box.yml` and `daniel-server.yml`;
- narrows `preflight.yml`'s assert to hosts that run Docker, and points its GitOps warning at
  the service list the renderer reads;
- points the three loaders at the moved file.

`_example.yml` keeps `containers_list: []`, because a new Pi host still needs it. The
renderer exposes the list under the same variable name, so templates do not change.

H1.9 removes `containers_list` from daniel-box's and daniel-server's `host_vars` only.
`daniel-pi.yml` keeps its `containers_list`, and H5 adds `compose_services` beside it. H5
moves the Pi's entries from the first key to the second. The removal of the Pi's
`containers_list` key, and where `compose_services` lives once Ansible is gone, belong to H7
(*Open questions* 4).

### File tree

```diff
+ ansible/roles/k8s/cluster-base/
+ ansible/roles/k8s/metallb/            (files/upstream/metallb-native.yaml, vendored)
+ ansible/roles/k8s/coredns/            (templates derived from k3s's manifests/coredns.yaml; tests/)
+ ansible/roles/k8s/longhorn/           (files/upstream/longhorn.yaml, vendored; tests/)
+ ansible/roles/k8s/longhorn-reconcile/ (files/longhorn_reconcile.py; tests/)
+ ansible/roles/k8s/longhorn-backup/    (templates/longhorn-backuptarget-default.yaml.j2, new)
+ ansible/roles/setup/k3s/tasks/longhorn-host.yml   (the three host tasks, moved from longhorn.yml in H1.5a)
+ scripts/dev/vendor_manifest.py
+ scripts/dev/takeover_census.py        (H1.1j)
+ cluster/services.yml                  (H1.9)
- ansible/roles/setup/k3s/tasks/cluster-addons.yml
- ansible/roles/setup/k3s/tasks/longhorn.yml
- ansible/roles/setup/k3s/tasks/longhorn-backup.yml
- ansible/roles/setup/k3s/tasks/longhorn-weekly-shard.yml
+ ansible/roles/setup/k3s/tasks/stale-checkout-guard.yml   (H1.1h: one check import per writer tag set, with its rows)
+ ansible/roles/setup/k3s/tasks/stale-checkout-check.yml   (H1.1h: the bounded read and the assert, untagged)
  ansible/roles/setup/k3s/tasks/main.yml          (+ the guard's import after server.yml; imports change)
  ansible/roles/setup/k3s/tasks/coredns.yml       (a bounded CoreDNS read, then the .skip marker task)
  ansible/roles/setup/k3s/tasks/kubeconfig.yml    (host half only: wait for the token, write kubeconfig)
  ansible/roles/setup/k3s/defaults/main.yml       (moved keys leave; cron-only keys and SA mirrors stay; - k3s_manage_backup_targets; + k3s_etcd_s3_region)
  ansible/roles/setup/k3s/files/longhorn_backup_health.py      (arm state and run times read from the cluster)
  ansible/roles/setup/k3s/templates/longhorn-backup-health.sh.j2   (- the four baked exports)
  ansible/roles/setup/k3s/templates/etcd-snapshot-offbox.sh.j2     (--s3-region reads k3s_etcd_s3_region, H1.7b)
  ansible/roles/setup/k3s/tasks/health-crons.yml  (b2-budget drops --retain)
  scripts/diagnostics/probe_lib/                  (b2-budget reads the live weekly retain)
  ansible/inventory/host_vars/daniel-box.yml      (- containers_list, H1.9)
  ansible/preflight.yml                           (assert narrowed, H1.9)
  scripts/deploy_tools/promote.py                 (forward-only hold, upgrade-gates status, backstop, excused failures)
  scripts/deploy_tools/land_lib/                  (unconditional pin gate, land rule, --regate)
  scripts/diagnostics/probe_lib/health.py         (vendored targets, the new checks)
  scripts/diagnostics/probe_lib/landing_blockers.py   (lists the break-glass annotation)
  scripts/renderer/renderer_lib/secret_checks.py  (+ PLACEHOLDER_KIND_EXEMPT, H1.7b, as R2 assigns)
  ansible/roles/k8s/headlamp/defaults/main.yml    (comment names cluster-base)
  docs/adr/0007-backup-tiering-r2-daily-b2-weekly.md   (amended, operator-only, H1.7a and H1.7b)
  renovate.json                                   (k3s, MetalLB and Longhorn rules split; coredns pin disabled)
```

## Steps, as PRs

**Rules for every PR from H1.2 on.**

- **One writer.** Each PR deletes the `setup/k3s` tasks for the objects it hands to Flux, in
  the same commit. Its body carries the evidence, as D2.7's does: the write-verb census, and
  a `git grep` showing that no remaining task writes the moved kinds and names.
- **A diff before each flip.** Before merge, the operator runs a server-side diff of the
  rendered tag against live and the PR body quotes it:
  `sudo k3s kubectl diff --server-side --force-conflicts --field-manager=kustomize-controller -k <out>/cluster/<tag>`.
  That is **operator-only**, because the read-only ServiceAccount cannot dry-run a patch.
    - **Why `--force-conflicts`.** kustomize-controller applies with force, so the flag models
      it. Without the flag, a value the render changes in a field another manager owns, such
      as `deploy@daniel-box` on `coredns` (Baseline), returns a conflict error in place of a
      difference.
    - **Exit codes.** `kubectl diff` exits 0 when it finds no difference and 1 when it finds
      one. Any other exit is a stop, and so is any conflict error. Neither ever counts as an
      empty diff.
    - **What it shows.** Each field whose value the render changes is either intended and
      named, or patched out. The diff cannot show a field the takeover removes, because its
      dry run owns no field beforehand (Baseline). The takeover census below lists those.
- **A takeover census before each flip.** In the same window as the diff, the operator runs
  `scripts/dev/takeover_census.py` (H1.1j) over the tag, and the PR body quotes its output.
  It reads every object in the tag twice: the live object with `--show-managed-fields`, and
  a server-side dry-run apply of the render as `kustomize-controller` with
  `--force-conflicts`. It then prints two lists for each object:
    - **`removed`:** the fields that the four cleanup entries own (Baseline) and that neither
      the render declares nor a manager outside the cleanup list owns. A field that two
      cleanup entries share, such as `kubectl-client-side-apply` and `kubectl-patch`, is
      still removed, because both hand it to kustomize-controller. The apply at the flip deletes each one,
      and again at every later reconcile. Each is either named in the PR body as an intended
      removal, or added to the render. `last-applied-configuration` is the one expected
      entry, which Flux strips by design.
    - **`kept`:** the fields that a manager outside the cleanup list owns and the render does
      not declare, such as `deploy@daniel-box`'s or `k3s`'s. They stay live after the flip,
      owned by that manager and invisible to Flux. The PR body names each one, or the PR
      adds it to the render.
    - **The roll check.** For a Deployment, DaemonSet or StatefulSet, a `removed` field under
      `spec.template` rolls the workload at the promote. The census checks
      `spec.template.metadata.annotations` for `kubectl.kubernetes.io/restartedAt` by name,
      and it prints `rolls` for each such workload. Either the PR body names the roll and
      the window the promote lands it in, or the PR declares the field in the render so
      that nothing rolls.
    - **Who runs it.** The dry run needs the admin kubeconfig, so the census is
      **operator-only**, and `cluster-base` and `longhorn-backup` hold Secrets the read-only
      ServiceAccount cannot read anyway. The census prints field paths only, never values.
    - **When it goes stale.** A hand write to the tag's objects after the census, such as a
      `kubectl rollout restart`, invalidates it, and the operator runs it again before merge.
- **Every reader of a moved template, a moved key, a deleted task file or a deleted task.**
  A PR that moves a template runs `git grep -l <basename>` for each one, the way the per-key
  census does for keys. A PR that deletes a task file runs the same grep on the file's
  basename. A PR that deletes or renames a task runs `git grep -l -F` on the task's name.
  The PR body lists each reader it repoints. A reader that only names the old path in a
  comment or docstring gets the new path. A test that writes the path or the name into a
  synthetic tree, or into a source string, as sample input needs no change. A test that
  loads a real task file and anchors on a deleted task names its replacement anchor in the
  PR body. *Tests and docs that change in the same PR* names the readers found at
  `70591c307`.
- **The diff for a tag that substitutes.** `longhorn-backup` is the one such tag in H1. A
  diff of its placeholder-mode render would show every placeholder field changing, so it
  proves nothing. The operator diffs the substituted render instead:
  `flux diff kustomization longhorn-backup --path <out>/cluster/longhorn-backup --kustomization-file <out>/flux/longhorn-backup.yaml`,
  run with the admin kubeconfig. It substitutes from the live `homelab-cluster-vars`, so it
  reads a Secret, and its output holds plaintext values, because `flux diff` masks only SOPS
  ciphertext (Baseline). Running it is **operator-only**. The repo is public, so the PR body
  quotes a redacted summary only: each changed object, each field path and its change kind
  (added, changed or removed). It quotes a value only for a field that holds no placeholder,
  and never a value from `secrets.yml`.
- **Size.** A PR that adds a vendored file exceeds the 200-line budget by that file alone.
  Reviewers check the vendored file by its sha256, not line by line.
- **The guard map.** Each PR from H1.2 to H1.7b deletes, in the same commit as the tasks,
  the guard-map rows of the writer tasks it deletes (H1.1h). The rows live in
  `tasks/stale-checkout-guard.yml`. A PR whose deletion leaves a guard instance with no row
  deletes that instance too.
- **An added entry reaches readers of the list until D6.12.** H1.2, H1.3, H1.4, H1.5a,
  H1.6 and H1.7b each add one entry to `containers_list` in
  `ansible/inventory/host_vars/daniel-box.yml`, and the tick applies that file as a
  deploy-plane change.
    - **The baseline.** At `70591c307`,
      `scripts/deploy_tools/narrow_lib/containers.py:changed_fields` returns None when the
      entry names differ, and `scripts/deploy_tools/narrow_lib/broad.py:_list_reader_tags`
      then adds every role that renders the list: 23 tags of 59 (D1 *Baseline*).
    - **After D2.1c, which merges before H1.2.** D2.1c records an added entry that is not
      Ansible-owned by its fields, so the entry reaches only the readers of the fields it
      carries (D2 Open question 10). H1's six entries carry `name`, `platform`, `owner` and
      at most `depends_on`. At `a5abf8581` such an entry applies four reader
      tags. artifacts reads it through `entry_names`, which lists every entry.
      blackbox-exporter, pihole and uptime-kuma read its `platform` through
      `lan_hostnames`. No reader reads `depends_on`, and `owner` is a deploy-mechanics
      field. The D2.1c render census,
      `ansible/tests/k8s/test_flux_born_entry_reader_census.py`, holds that every render the
      entry moves belongs to a tag the narrowing names. An entry that gains `hostname`,
      `port`, `tier` or `metrics` adds that field's readers, so each PR expects its own
      narrow output rather than a fixed list.
    - **Why the rule stays.** Three of the four, blackbox-exporter, pihole and uptime-kuma,
      set `k8s_autodeploy: false` in their defaults. The deploy plane ignores that flag, and
      its apply is forward-only, so it would also apply any change of theirs that is on
      master and not yet applied. The entry's own tag is `owner: flux`, which the deployer
      skips (C4).

  Until D6.12 retires the deployer's k8s arms, each of the six PRs, and each revert of one,
  does this:
    - **Quote the narrow output.** Before the PR, run `uv run python
      scripts/deploy_tools/deploy_tags.py narrow origin/master HEAD` and quote its list in
      the body. The bullets below apply to every reader that list names.
    - **D1.2's stop rule, before the PR and again right before `land.sh`.** Run `uv run
      python scripts/diagnostics/probe.py releases --stale-only` and `uv run python
      scripts/diagnostics/probe.py releases`, and read the service names, not the exit
      code. Do not merge while any service in the narrow output is named by the first run,
      or flagged `unmerged` or `dirty` by the second. The body quotes both runs.
    - **Which renders move.** The body names the renders the new entry moves, from a
      `./scripts/deploy.sh --tags <readers> --dry-run`. It names the landing window too,
      because every reader's own verify tasks run whether its render moved or not.
    - **The hold exit is D1.2's.** A failed apply writes `hold_sha` and a `hold_plane` entry
      for the whole list. A refused narrowing applies nothing and records `k8s_unapplied`
      lines. In either case the landing session follows D1.2's *Landing* and *If the
      narrowing refuses* text and quotes the tick's journal line.

  This deploy-plane apply comes on top of the owed `manual_plane` line below. The tick
  applies the readers, and the operator applies the `k3s` tags.

**The owed `manual_plane` line.** Each of H1.1h and H1.2 to H1.7b changes paths under
`setup/k3s` that map to the `k3s` setup role: its tasks, and the templates, files and keys it
moves out. Its merge therefore leaves a `manual_plane` line for `k3s` (Baseline).

**Each of these nine PRs is expected to refuse the narrowing.** This is a reading at
planning time. `scripts/deploy_tools/narrow_setup.py:role_tags` builds `RoleIndex` at the new
ref, and each of the nine is planned to make at least one change that the index refuses:

- an edit to `tasks/main.yml`, whose imports carry no tags, so
  `scripts/deploy_tools/narrow_lib/setup_index.py:RoleIndex.tags_of` refuses the file as one
  that "carries no tags of its own": H1.1h, H1.3, H1.5a, H1.6 and H1.7b;
- a deleted task file, which `tags_of` refuses as "not a task file of this role": H1.3,
  H1.6 and H1.7b;
- a moved template that no surviving task names, which `RoleIndex.readers_of` refuses with
  "nothing in this role names": H1.2, H1.3, H1.4 and H1.7b among them;
- a moved or deleted defaults variable that no surviving task reads, which
  `RoleIndex.key_readers` refuses with "nothing in this role reads": H1.2, H1.3, H1.5a,
  H1.5b, H1.7a and H1.7b among them.

`scripts/deploy_tools/land_lib/land_tags.py:own_narrow_tags` then leaves `k3s` out, and the
tick records the line with an empty tag set, which means the whole role. Two consequences
follow.

- `land.sh` and the banner print `k3s-bringup.yml --tags k3s`. That is the maximal apply.
  It makes `Install or reconfigure the k3s server` reachable, which restarts k3s when
  `k3s_server_args` gained an argument or `k3s_version` moved
  (`docs/k3s-node-plane-crons-and-incidents.md`, *What opens the three control-plane gates
  under `--tags k3s`*). It would also ship any other session's merged, unapplied `k3s`
  change.
- A narrowed clear keeps the line. `scripts/deploy_tools/gitops_state.py:clear_manual_plane`
  says that "a narrowed apply against an empty or missing row keeps the line", and
  `deploy_state.py:clear_manual_plane_tags_applied` returns `{role}` for that case.

So each PR body states the line and its clear in this form:

1. The landing reads what `land.sh` prints.
    - If it prints narrowed tags, the PR narrowed after all. The operator applies those
      tags and clears with `gitops_state.py clear-owed manual_plane k3s --applied <tags>`.
      Steps 2 to 4 do not apply.
    - If it prints `--tags k3s`, the operator does not run it, and steps 2 to 4 apply.
2. The body lists the tags to apply by hand, read at the merge commit. It takes the tags of
   three kinds of changed path:
    - each surviving task file the PR edits;
    - each surviving template or `files/` entry the PR edits, through the task files that
      read it;
    - each changed defaults variable that a surviving task still reads, through those
      tasks.

   A file whose only change is a deleted writer task needs no apply for that deletion,
   because the object now belongs to Flux. The guard's own files write nothing, so a row
   deletion adds no tag. The body quotes
   `k3s-bringup.yml --tags <tags> --list-tasks`, which executes nothing. When the list is
   empty, the body says so and names the reason for each changed path.
3. The operator applies those tags with `k3s-bringup.yml --tags <tags>`. Each is safe to
   apply, because the PR deleted every task that wrote a moved object. The apply needs
   `become`, so it is operator-run.
4. Clear with the bare `gitops_state.py clear-owed manual_plane k3s`, and only after the
   ledger line's range shows no other commit under `ansible/roles/setup/k3s/`. If the range
   holds another session's commit, its tags go into the same hand apply, or the line stays
   for that session to clear. A bare clear would otherwise drop another session's owed
   apply.

The lists at planning time:

- **H1.1h:** `kubeconfig`. The apply runs the `kubeconfig` guard instance once against the
  live API, before any object is Flux-owned, and re-applies the idempotent `kubeconfig`
  tasks.
- **H1.2:** `kubeconfig`, for the host half that `kubeconfig.yml` keeps.
- **H1.4:** `coredns`, for the bounded read and the marker task.
- **H1.5a:** `longhorn_host`, for the moved host tasks.
- **H1.6:** `longhorn`, for the poll-interval task that moves into `longhorn-backup.yml`.
  That task carries `[longhorn, longhorn_backup]` at `70591c307`, and after H1.6 `longhorn`
  selects it alone (*The Longhorn window, H1.5a until H1.7b*). `longhorn_backup` would run
  all of `longhorn-backup.yml`.
- **H1.7a:** `backup-health`, for the shim and the reader script, plus the tag of the
  b2-budget cron task in `health-crons.yml`.
- **H1.7b:** `etcd-snapshot`, for the off-box script that `health-crons.yml` renders from
  `etcd-snapshot-offbox.sh.j2`, which H1.7b repoints at `k3s_etcd_s3_region`. The body may
  instead show that the render is byte-identical, which the test holding the pair equal
  leads one to expect, and then list nothing for it.
- **H1.3 and H1.5b:** empty, unless the PR edits a surviving path of the three kinds, which
  its body then names.

Each PR confirms its list against the merge commit with the `--list-tasks` run of step 2.
A preparatory change to `narrow_setup` that read these four shapes as reaching no host was
rejected. It would widen a fail-closed derivation in four places, each needing its own red
proof, for a migration that ends at H1.7b. Nine hand lists cost less and fail closed: the
line stays until the operator clears it.

H1.8a changes only a test file, and the exit bump in H1.8b changes only the k8s `longhorn`
role, so neither leaves a line.

**The preparatory PRs, H1.1a to H1.1j.** No objects move in any of them. All ten merge
before H1.2. Four of them merge in a fixed order: H1.1f, then H1.1g, then H1.1b, then H1.1i.

- H1.1b makes `promote` require the status that only H1.1g's `land.sh` posts. A Longhorn or
  MetalLB bump merged before H1.1g would hold every promotion, so H1.1b also waits until
  (`last-good`, master tip] holds no pin change from before H1.1g.
- H1.1g's land gate relies on H1.1f's check that a bump stands alone in its PR.
- H1.1i edits the `verify` path whose failure write H1.1b changes.

The other six merge in any order. H1.1b keeps its id because contracts.md's C2 amendment
gates on it. Each estimate counts hand-written lines, tests included.

- **H1.1a Renderer support** (about 150 lines).
    - Verbatim files and kustomize `patches:` in the generated `kustomization.yaml`.
    - The per-directory placeholder key set, as one renderer function, if R2 or D3 has not
      added it. The refusal of a placeholder in a directory with vendored files, and no
      `$${` escape in a directory that does not substitute, both read it.
    - `flux_prune: false` renders `prune: false`.
    - A role's `manifests_rollout_timeout` defaults key renders as its Kustomization
      `timeout`, in place of `manifests_rollout_timeout_default`.
    - The verbatim files and the patches stay outside `rendered_docs()`. *Vendored upstream
      manifests* names the guards they bypass and why that is accepted.
- **H1.1b Promote: the forward-only hold and the gate status** (about 220 lines, over the
  guide because the age bound and the backstop reuse the range walk; split out, they would
  repeat it).
    - The forward-only hold in `promote.py`, over the range (`last-good`, T], with a tag
      that T drops counted as changed.
    - The per-commit `homelab/upgrade-gates` check, with its commit-to-PR lookup and the
      six-hour bound on the status's age for commits in (`prod`, T].
    - The backstop: two changes to one forward-only pin in (`prod`, T], or a Longhorn move
      of more than one minor, hold.
    - The hold's form: T's SHA alone, written in place of C2's `failed=` value. The
      predicate reads `footprint` from the `out/digests.json` that D2 publishes.
- **H1.1c Publish: the per-tag object census** (about 120 lines). The named members are the
  ones *Plumbing tags are never pruned* lists for each of the six tags. The census applies
  to a tag from the commit that adds the tag.
- **H1.1d Health: vendored targets** (about 160 lines). `health.py` resolves targets from a
  role's vendored files and patches as well as its templates. A vendored role that resolves
  no workload fails instead of passing.
    - `role_scalable_targets` shares that resolution, so a vendored workload would join
      `probe.py shed-set`. H1.1d therefore adds the shed exclusion of *Six Flux-born tags*:
      `NEVER_SHED`, empty, the drop in `shed_entries`, the three fixtures and the equality
      test.
    - No role has vendored files before H1.3, so H1.1d changes no shed set. Each plumbing tag
      enters `NEVER_SHED` in the PR that adds its entry.
- **H1.1e The vendoring tool** (about 180 lines). `scripts/dev/vendor_manifest.py` and the
  vendored-sha256 test.
- **H1.1f The CI pin checks** (about 150 lines): no decrease, no skipped Longhorn minor, and
  a bump alone in its PR.
- **H1.1g The land gate** (about 240 lines, over the guide by the Renovate split, which
  stays here because its rule descriptions name this gate).
    - The `land.sh` pin gate, the `metallb-upgrade` gate set and the status post.
    - The land rule. `land.sh` refuses a forward-only pin bump while (`last-good`, master
      tip] already changes that pin, or while `DEPLOY_HOLD` reads anything other than
      `none`, empty and missing included. It reads `last-good` with `promote.py`'s reader.
    - `land.sh --regate <pr>`, which re-runs the gates for a merged PR and re-posts the
      status without merging.
    - `renovate.json`: split the grouped rule into a `k3s` rule, a `metallb` rule and a
      `longhorn` rule, none automerged. Each description names the merge gate or, for k3s,
      the hand apply that H6 replaces. Add the packageRule that disables `coredns_image`.
    - The gate section of `docs/longhorn-upgrade.md` says that `land.sh` runs the
      `runbook_gates.py longhorn-upgrade` gates at the merge and posts
      `homelab/upgrade-gates`.
- **H1.1h The stale-checkout guard and the write-verb census** (about 220 lines). Its change
  to `tasks/main.yml` leaves a `manual_plane` line, cleared like the later PRs' lines. An
  apply runs a guard instance exactly when its selection runs a writer task that the
  instance covers.
    - **What the guard is.** Two new task files. `tasks/stale-checkout-check.yml` holds the
      bounded read and the assert, and carries no tags of its own.
      `tasks/stale-checkout-guard.yml` imports it once for each distinct tag set that the
      mapped writer tasks carry, such as `[priorityclass]`, `[metallb]`, `[kubeconfig]`,
      `[coredns]`, `[longhorn]` and `[longhorn_backup]`. Each import carries that tag set and
      holds, in its own `vars:`, the rows of the writers with exactly those tags. `main.yml`
      imports `stale-checkout-guard.yml` directly after the `server.yml` import and before
      every other import. At `70591c307` the next import is `cluster-addons.yml`, the first
      that writes to the cluster. Each instance's read carries `check_mode: false`, so a
      `--check` run reaches it.
    - **Why one instance per tag set.** Ansible selects a task by its effective tag set, under
      `--tags` and `--skip-tags` alike. An instance and the writers it covers carry the same
      set: their own tags, plus the inherited role tag `k3s`. So a selection runs the
      instance exactly when it runs one of those writers. A `--tags k3s` run reaches every
      instance. `--tags node-address`, `node-dns`, `longhorn_host`, `backup-health`,
      `k3s_server` and every other host-only tag reach none, so those applies never read the
      API. Two alternatives were rejected:
        - *One guard task tagged with the union of the writers' tags.* `--skip-tags` drops a
          task when any one of its tags is skipped. Bring-up step 1 runs
          `--skip-tags kubeconfig`, which would drop that guard while the `metallb`,
          `priorityclass` and `longhorn` writers still ran.
        - *One `always` guard gated by a `when:` over `ansible_run_tags` and
          `ansible_skip_tags`.* That reimplements Ansible's selection rules in Jinja, and a
          mismatch would fail silently in one direction or the other.
    - **Why after `server.yml`.** A whole-role run (`--tags k3s`) with the API down must reach
      `server.yml` before any instance reads the API. After a bump whose server does not
      start, the API refuses connections. An instance at the top of `main.yml` would fail its
      read there and stop the play before `server.yml` could reinstall a version. After
      `server.yml`, the instances read an API that `Wait for the node to report Ready` has
      already brought up. The *Rollback* section of `docs/k3s-upgrade.md` re-runs
      `--tags k3s_server`, which reaches no instance, so the rollback does not depend on the
      placement and needs no change to that document.
    - **Why that placement loses nothing.** At `70591c307`, `server.yml` runs `kubectl
      wait`, `kubectl get` and k3s's own `secrets-encrypt` commands, and writes host files
      such as the containerd registry configuration. `k3s secrets-encrypt rotate-keys`
      re-encrypts stored Secrets and changes no declared field. No `server.yml` task writes an object
      that a Flux tag declares, so no stale writer runs ahead of the guard. A placement test
      keeps it so (*Verification*).
    - **A bounded read.** The read retries on an error for a bounded time, 12 tries 10
      seconds apart. Its `until` accepts a successful read and the named answer that the
      Kustomization resource type does not exist, so a cluster without Flux passes on the
      first try. When the tries run out, the guard fails and names the API as unreachable.
    - **Its map.** The instances' `vars:` hold, between them, a map from each writer task
      that a later H1 PR deletes to the Flux inventory ids of the objects it writes, such as
      `Apply the workload-tier PriorityClasses` to
      `_homelab-critical_scheduling.k8s.io_PriorityClass` and `Install Longhorn` to
      `longhorn-system_longhorn-manager_apps_DaemonSet`. Each row sits in the instance whose
      tags equal its task's effective tags. One file, `stale-checkout-guard.yml`, holds every
      row, so `main.yml` changes only in H1.1h. Each PR from H1.2 to H1.7b deletes a task's
      row in the commit that deletes the task, and deletes an instance left with no row. A
      row deletion alone does not make a PR narrow, because each of these PRs also makes a
      change that the narrowing refuses (*The owed `manual_plane` line*).
    - **What it reads.** One read: the `status.inventory` entry ids of every Kustomization in
      `flux-system`, which kustomize-controller records after each apply (Baseline).
    - **When it fails.** It fails when an id that a row of this checkout's map names appears
      in that inventory. The checkout still holds a task that would write an object Flux
      owns. The message names the task, the object and the Kustomization.
    - **What it tolerates.** It passes when no `k3s` binary exists. On a fresh host that is
      a `--check` run, where `server.yml` installs nothing, or a narrowed apply that skips
      `k3s_server`. It passes when the API answers that it has no Kustomization resource
      type, which means Flux is not installed. In both cases nothing Flux-owned exists. Any
      other read error that outlasts the bounded retry fails it (failure class 3).
    - **Why task names and not files.** Several files outlive the objects they once wrote:
      `kubeconfig.yml` keeps its host half after H1.2, `coredns.yml` keeps the CoreDNS read
      and the marker task after H1.4, and `longhorn.yml` lives from H1.5a to H1.6. A file-keyed guard would refuse
      the narrowed owed apply each of those PRs needs.
    - **The limit.** A checkout older than H1.1h lacks the guard, so that window stays open.
      The guard covers Flux-owned objects only. Because it runs after `server.yml`, a stale
      checkout still runs `Install or reconfigure the k3s server` at its own `k3s_version`
      before the guard stops the play. The k3s pin stays a hand apply outside the guard's
      scope, as it is at `70591c307`, and a downgrade across a minor stays the operator's
      call under `docs/k3s-upgrade.md`'s *Rollback*.
    - The write-verb census is a `Census` row of `ansible/tests/repo/test_census_rows_roles.py`,
      starting with every task file allowlisted. Later PRs shrink the allowlist.
    - A test keeps the map honest. Every row names a task that exists in the role, and every
      writer task the census flags outside `storage_smoke.yml` and `agent_verify.yml` has a
      row. The instance that holds a row carries exactly the effective tags of the task the
      row names. The test computes those tags as
      `scripts/deploy_tools/narrow_lib/task_walk.py:_task_tags` does, with block tags and an
      include's `apply:` tags inherited, so a writer in `longhorn-weekly-shard.yml` counts
      under `longhorn`.
    - `scripts/deploy_tools/tests/test_narrow_setup.py:test_the_real_k3s_roles_untagged_task_files_are_named_as_such`
      names `tasks/stale-checkout-check.yml` as a fourth untagged file, because its tags come
      from each import site.
- **H1.1i Verify: a dependent that was already failing** (about 70 lines). `decide` records
  the `flux/<name>` statuses on `prod`'s commit before it moves `prod`. `verify` reports a
  dependent in T's changed set whose own footprint T did not change and whose recorded
  status was `failure`, and does not expect it. C2's *Verification* states that rule, so C2's
  text meets the request row's gate for this PR (*Open questions* 3). H1.1b still waits on
  the row for the hold.
- **H1.1j The takeover census** (about 120 lines). `scripts/dev/takeover_census.py`, which
  the rule *A takeover census before each flip* runs, with tests in `scripts/dev/tests/`.
  It reads both inputs as JSON on its standard input or from files, so it makes no API call
  of its own. It flattens each `fieldsV1` tree into paths and works by set difference. Its
  cleanup list repeats kustomize-controller's four entries and the prefix and subresource
  rules of `matchFieldManager` (Baseline), and a comment names the upstream file it copies.
- **H1.2 `cluster-base`.**
    - Move the PriorityClasses and the RBAC objects. `kubeconfig.yml` keeps its host half,
      which waits for the token Secret with a bounded retry.
    - Move `k3s_readonly_crd_api_groups` into `cluster-base`'s defaults. Add the mirrored
      pair `cluster_base_readonly_sa_name` and `cluster_base_readonly_sa_namespace`, which
      the moved template reads, and the test that holds each equal to its `k3s_` twin.
    - Retarget the two tests that read the template in `setup/k3s`:
      `ansible/tests/k8s/test_k8s_manifests_rbac.py` renders it from `cluster-base` with that
      role's defaults, and
      `test_narrow_setup.py:test_the_real_k3s_role_still_maps_readonly_rbac_to_kubeconfig`
      takes a real-tree pair that stays in `setup/k3s`: `readonly-kubeconfig.j2` and
      `k3s_readonly_kubeconfig_server`, both read only under the `kubeconfig` tag. The test
      keeps a named member in the real tree, so the scan cannot go vacuous.
    - Write the bring-up order, with its hand steps, into
      `docs/longhorn-disaster-recovery.md`, `docs/break-glass.md` and the header of
      `storage_smoke.yml` (*Bring-up and DR order after H1*).
    - Add the red proof for the PriorityClass order: every `priorityClassName` in the
      rendered `flux` tag names a PriorityClass in the `cluster-base` render.
    - The comment on the agent kubeconfig task in
      `ansible/roles/setup/claude_code/tasks/agent.yml` names the `cluster-base` role in
      place of the k3s role.
    - Add `cluster-base` to `NEVER_SHED`. The real-tree case of the equality test names it
      as a member it must find in both sets.
    - Add the `scheduling.k8s.io` group file to
      `scripts/validate/refresh_vendored_schemas.py:CORE_GROUP_FILES` and run the refresh.
      At `70591c307` that list holds no `scheduling` file, and the PriorityClasses are the
      first objects of that group in `rendered_docs()`. Without it the
      `rendered-kinds-have-vendored-schemas` row fails this PR.
    - Rewrite the shed scenario of `docs/break-glass.md`. Step 1 says the shed set leaves out
      `NEVER_SHED` as well as every tiered service. For a service whose entry carries
      `owner: flux`, step 2 annotates each object with
      `kustomize.toolkit.fluxcd.io/reconcile=disabled` before it scales, because Flux's next
      reconcile would otherwise restore the count. Step 3 removes the annotation, and Flux's
      next reconcile restores the declared count. A `grep -L replicas` over the 59
      Deployment and StatefulSet templates at `70591c307` finds none without it, and
      kustomize-controller applies with force. `deploy.sh --tags` stays the restore only for
      a service that Ansible still deploys (C4).
    - Prove the skip that the break-glass procedures rely on, the shed scenario's among them.
      The role carries an inert
      ConfigMap. The operator annotates it in the cluster with
      `kustomize.toolkit.fluxcd.io/reconcile=disabled` and edits a value by hand
      (**operator-only**). The PR quotes the value surviving the next two reconciles, then
      removes the annotation and quotes Flux restoring the value.
- **H1.3 `metallb`.**
    - Vendor `metallb-native.yaml` at `v0.16.0`, move the pool and the pin.
    - Patch out the fields the controller writes at runtime, as the diff before the flip
      shows them.
    - The takeover census must show that the vendored file declares the `prometheus.io/port`
      and `prometheus.io/scrape` pod-template annotations, which only
      `kubectl-client-side-apply` owns live (Baseline). Otherwise the flip removes them and
      rolls the `controller` and every `speaker` pod.
    - Move the `k3s_metallb_pool` exemption in
      `ansible/tests/repo/test_lan_facts_have_one_source.py` from `setup/k3s`'s defaults to
      the `metallb` role's defaults, which become the pool's definition site.
    - Verify the VIPs from a host that is not the announcer, because ETP-Local VIPs have
      blacked out with every host-local probe green.
    - Add `metallb` to `NEVER_SHED`.
    - Add `IPAddressPool` and `L2Advertisement` to
      `scripts/validate/refresh_vendored_schemas.py:VENDORED` and run the refresh. The moved
      pool template joins `rendered_docs()`, and the `rendered-kinds-have-vendored-schemas`
      row refuses a kind with no schema.
    - Grep the vendored file for each kind a source-tree guard forbids, such as
      `ResourceQuota`, and strip or exempt each match.
- **H1.4 `coredns`.**
    - Write the templates derived from k3s's manifest at `k3s_version`, pin the image, and
      move the Corefile template.
    - `coredns.yml` keeps two tasks. The first is a bounded read,
      `k3s kubectl -n kube-system get deployment coredns`, with `retries`, `until` and
      `check_mode: false`. The second is the `.skip` marker task. The read keeps the order
      that `Read the live Corefile` gives today. Without it, step 1 of the bring-up order
      could write the marker before k3s first applies CoreDNS.
    - The marker's path becomes a defaults key, so the stub test can point it at a
      temporary directory.
    - `test_coredns_image_follows_bundled_manifest.py` retargets the pin and the rendered
      Deployment.
    - Add the break-glass to `docs/k3s-upgrade.md`, and the CoreDNS half of the
      `probe.py landing` annotation listing.
    - Meet the fleet guards as *CoreDNS without a host read* lists them: the two shell
      macros, the **operator-visible** `_SYSTEM_TIER` row, `enableServiceLinks: false` and
      the measured uid decision. The PR body lists each guard's answer and each pod-template
      field the render changes, and the takeover census confirms the list.
    - The pod template changes, so the single replica rolls at the promote. H1.4 lands in a
      window, and the PR body names it.
    - Add `coredns` to `NEVER_SHED`. The real-tree shed test gains `coredns` as its named
      member: present in `k8s_entries()`, absent from `shed_set()`.
- **H1.5a `longhorn` takeover.** Everything here must move at once. Flux re-applying the
  upstream DaemonSet without the request, or the upstream ConfigMap, would fight the
  Ansible patches.
    - Vendor `longhorn.yaml` at `v1.12.1`, with the manager patch and the ConfigMap patch.
      Move the pin and the manager memory key.
    - Delete `Install Longhorn`, the manager memory task, and the six tasks that render,
      patch, read, delete and apply the `longhorn` class and its ConfigMap, from `Render the
      Longhorn StorageClass` to `Apply the Longhorn StorageClass`.
    - Move the three host tasks, `Install Longhorn prerequisites` and the multipathd pair,
      into `tasks/longhorn-host.yml`, imported from `main.yml` ahead of `longhorn.yml`. They
      carry `tags: [longhorn_host]` and neither `longhorn` nor `longhorn_backup`, so the
      window's step 1 still installs them (*The Longhorn window, H1.5a until H1.7b*). Add
      the test that holds this (*Verification*).
    - Write the window's order into the three runbooks: step 1's two extra skips and
      step 7a. The PR body quotes `k3s-bringup.yml --skip-tags
      kubeconfig,longhorn,longhorn_backup --list-tasks`, which lists the three host tasks
      and no task from `longhorn.yml` or `longhorn-backup.yml`. `--list-tasks` executes
      nothing.
    - Record a measured manager roll: in a window, the operator deletes both
      `longhorn-manager` pods at once (**operator-only**). The PR quotes the time from the
      delete until both replacement pods are Ready. The tag's `manifests_rollout_timeout` is
      twice that, rounded up to a minute. The PR also confirms that promote's derived timeout
      reads the value.
    - **Why a pod delete and not `kubectl rollout restart`.** A rollout restart writes
      `kubectl.kubernetes.io/restartedAt` into the pod template under the `kubectl-rollout`
      manager. The flip's apply would remove it and roll both manager pods a second time, at
      an unscheduled promote. A pod delete writes no template field. The DaemonSet rolls with
      `maxUnavailable: 100%` (Baseline), so a rollout also replaces both pods at once, and the
      delete measures the same shape. The takeover census before the flip confirms that no
      `restartedAt` exists.
    - Add `longhorn` to `NEVER_SHED`, and grep the vendored file as H1.3 does.
    - Rewrite the per-hop procedure of `docs/longhorn-upgrade.md`. Step 1 bumps the pin in
      the `longhorn` role's defaults and runs the `vendor_manifest.py` refresh. Step 2
      re-syncs against the `longhorn` role's class file. Step 3 merges through `land.sh`,
      which is the apply, in place of `k3s-bringup.yml --tags longhorn`.
- **H1.5b `longhorn` settings.**
    - The seven Setting objects, `longhorn-nobackup`, and the metrics NetworkPolicy.
    - Delete their tasks, and move their keys after the per-key census.
    - Add `Setting` to `VENDORED` and run the refresh.
    - Rewrite the list of what the window's step 7a applies in the three runbooks. The
      Settings, `longhorn-nobackup` and the NetworkPolicy leave it.
    - `k3s_longhorn_concurrent_engine_upgrade` renders the
      `concurrent-automatic-engine-upgrade-per-node-limit` Setting. Exit part 1 needs every
      engine to follow the manager, so a render test asserts that the value is greater than
      0. It is an `_is_clean` and `_is_flagged` pair, and that Setting is the named member it
      must find.
    - Correct step 4 of `docs/longhorn-upgrade.md`, which says the limit is `0`. The key's
      default is `3`, and a read-only `kubectl get settings.longhorn.io` printed `3` on
      2026-10-11. Engines follow the manager on their own, and step 4 waits until the old
      engine image holds zero references.
- **H1.6 `longhorn-reconcile`.**
    - Add the `longhorn-reconcile` role, with `depends_on: [longhorn]` on its entry. Port the
      group, weekly-shard, legacy-label and CSI-request tasks into its
      `files/longhorn_reconcile.py`, with tests in the role's `tests/`.
    - The reconcile ConfigMap calls the three tier filters over `containers_list` directly.
      The tier keys stay in `setup/k3s`'s defaults until H1.7a.
    - Move `Set the Longhorn backup-store poll interval` into `longhorn-backup.yml`, where it
      stays until H1.7b. The three host tasks are already in `tasks/longhorn-host.yml`
      (H1.5a).
    - Rewrite the list of what the window's step 7a applies in the three runbooks: the
      poll interval and the backup plane only.
    - Delete the rest of `longhorn.yml`, and delete `longhorn-weekly-shard.yml`.
    - Move the reconcile actuator's contract.
    - `longhorn-reconcile` stays out of `NEVER_SHED` and untiered. Its CronJob meets the
      fleet guards like any new role: `pod_shell`, a pinned non-root uid, every capability
      dropped and a volume named for the role.
- **H1.7a Target routing and the host readers.** This PR does not need D3.
    - Add the `backupTargetName` arm to `longhorn-reconcile`.
    - Delete `Read where each Longhorn volume currently sends its backups`, `Route the
      selected volumes to R2` and `Return de-listed volumes to the default backup target`.
    - Make `longhorn_backup_health.py` read the arm state and the run times from the cluster,
      and drop the four baked exports from `longhorn-backup-health.sh.j2`. Update its header's
      kubectl-call arithmetic.
    - Make `probe.py b2-budget` read the live weekly retain, and drop `--retain` from the
      b2-budget cron.
    - Move the three tier lists into the `longhorn-reconcile` role's defaults, and point the
      reconcile
      ConfigMap at them. The glance blocks and doc fragments must not change.
    - **Operator-only:** amend [ADR-0007](../../adr/0007-backup-tiering-r2-daily-b2-weekly.md).
      *Governs* names the `longhorn-reconcile` role's defaults as the tier lists' home. A new
      consequence says backup health takes each target's arm state from its live URL, so a
      disarm no longer turns the monitor red, and the verdict's `DISARMED` note names what
      is suppressed.
    - The five arm and schedule keys do not move yet, so the readers report the same
      verdicts before and after. The PR quotes one health run from before the owed apply and
      one from after it.
- **H1.7b `longhorn-backup`.** This PR needs D3 and H1.7a.
    - Move the two credential templates, now `stringData:`, and the R2 target. Add the
      `default` target template.
    - Move the RecurringJob template. Confirm with a read-only `kubectl get recurringjobs`
      that the retired flat weekly job is gone, then drop the task that deleted it.
    - Move the five keys of *Where the Longhorn variables go* into the role's defaults, and
      add the census row that keeps them off the host plane.
    - Define `k3s_longhorn_r2_region` in the role's defaults for the moved R2 target. Add
      the twin `k3s_etcd_s3_region` to `setup/k3s`'s defaults, repoint
      `etcd-snapshot-offbox.sh.j2` at it, delete `k3s_longhorn_r2_region` from `setup/k3s`'s
      defaults, and add the test that holds the pair equal. Without the twin, the census row
      refuses the off-box script's read.
    - Delete `longhorn-backup.yml` and its import.
    - Retire `k3s_manage_backup_targets`: delete the key. Its only task reader, the `when:` on
      that import, goes with the import.
    - Add `PLACEHOLDER_KIND_EXEMPT` to `scripts/renderer/renderer_lib/secret_checks.py`,
      with its two `(key, BackupTarget)` members, its `_is_clean` and `_is_flagged` pair and
      its named member, as R2's *Open questions* 10 assigns. Without it, rule 3 fails this
      PR's render.
    - Add the availability check, and the BackupTarget break-glass to the runbooks, with
      step 4's condition on `lastAppliedOriginRevision`.
    - Close the Longhorn window in the three runbooks: remove step 7a and step 1's two
      extra skips, and say that step 7 creates the targets through `longhorn-backup`.
    - The backup-health reader reports a target that carries the reconcile-disabled
      annotation. `probe.py landing` adds both BackupTargets to its annotation listing.
      The listing (`RECONCILE_MEMBERS` in `scripts/diagnostics/probe_lib/promote_state.py`) is
      H1.4's, so a stack whose base predates H1.4 adds the two members after its rebase onto
      master. Until then, `probe.py landing` does not name an annotated BackupTarget, and the
      backup-health monitor and `probe.py health longhorn-backup` still report it.
    - **Operator-only:** amend ADR-0007 again. Its new consequence gains the break-glass
      case: a target held by the annotation reads red until step 4 removes it.
    - Move the RecurringJob actuator's contract.
    - Add `longhorn-backup` to `NEVER_SHED`. Add `BackupTarget` and `RecurringJob` to
      `VENDORED` and run the refresh.
- **H1.8 The exit proof, as two PRs.** H1.1f refuses a pin bump whose PR changes anything
  besides that role's files, its vendored file and docs, so the census change cannot ride
  with the bump. A dependency edge that names H1.8, such as "H1.1a to H1.8", covers both.
    - **H1.8a The census.** Shrink the census allowlist to the two final files. The PR
      changes only `ansible/tests/repo/test_census_rows_roles.py`.
    - **H1.8b The bump, after H1.8a.** Land the next Longhorn release step through the gate,
      with `land.sh` and no `k3s-bringup.yml`. The step is the next patch in the pinned minor
      if upstream has shipped one, or else the next minor (*Goal and exit criterion*). With
      upstream as read on 2026-10-11, that is `v1.13.0`, taken through the
      `runbook_gates.py longhorn-upgrade` gates. A later `v1.13.x` is a patch step after it.
      Which step this PR takes therefore follows upstream's release timing, and a patch step
      waits until upstream ships one.
- **H1.9 `containers_list` leaves `host_vars`.** This PR needs R4, as described above.
    Because the PR is mostly a move, it reviews as a move with `git diff --color-moved`.

## Verification

```bash
# Each tag is Flux-owned, unpruned and Ready at the promoted revision.
kubectl -n flux-system get kustomizations cluster-base metallb coredns longhorn longhorn-backup longhorn-reconcile
kubectl -n flux-system get kustomizations -o custom-columns=NAME:.metadata.name,PRUNE:.spec.prune,TIMEOUT:.spec.timeout
kubectl -n longhorn-system get settings.longhorn.io default-replica-count default-backup-block-size -o custom-columns=NAME:.metadata.name,VALUE:.value
kubectl get storageclass longhorn longhorn-nobackup -o yaml | grep -A8 '^parameters:'
kubectl -n longhorn-system get backuptargets.longhorn.io -o custom-columns=NAME:.metadata.name,AVAILABLE:.status.available
kubectl -n longhorn-system get cronjob longhorn-reconcile
kubectl -n longhorn-system get jobs -l app=longhorn-reconcile --sort-by=.status.startTime | tail -n 2

# No plumbing workload is in the shed set.
uv run python scripts/diagnostics/probe.py shed-set

# Exit part 1, after H1.8b: the bump landed with no hand apply.
kubectl -n longhorn-system get ds longhorn-manager -o jsonpath='{.spec.template.spec.containers[0].image}'
kubectl -n longhorn-system get volumes.longhorn.io -o custom-columns=NAME:.metadata.name,ENGINE:.status.currentImage
uv run python scripts/diagnostics/probe.py health longhorn
# Prints master@sha1:<S>. lastAppliedRevision would print prod@sha256:<digest> (D1 Baseline).
kubectl -n flux-system get kustomization longhorn -o jsonpath='{.status.lastAppliedOriginRevision}'
git merge-base --is-ancestor <merge-sha> <S>
uv run python scripts/diagnostics/probe.py landing

# Exit part 2, after H1.8a: the census over setup/k3s.
uv run pytest ansible/tests/repo/test_census_rows_roles.py
```

The expected results are:

- every Kustomization reads `Ready`, and the five plumbing tags read `prune: false`;
- the settings and StorageClass parameters equal the role defaults;
- `default` and `r2` read `available: true` while armed;
- the reconcile job's last run reads `Complete`;
- `probe.py shed-set` lists no workload of the five plumbing tags, and it names
  `longhorn-reconcile` on its "nothing to scale" line;
- the manager image and every engine image carry the bumped version;
- `probe.py health longhorn` names DaemonSet `longhorn-manager` among its targets;
- the `longhorn` Kustomization's `lastAppliedOriginRevision` reads `master@sha1:<S>`, and
  `merge-base --is-ancestor` exits 0, so S is the merge commit or a later one;
- `probe.py landing` shows no `manual_plane` line, `DEPLOY_HOLD` reading `none` and no object
  held by the reconcile-disabled annotation;
- the census passes with two allowlisted files.

**MetalLB and CoreDNS after H1.3 and H1.4.** Run `dig +short @10.0.0.243 <a-known-name>` from
daniel-pi, which is not the announcer. Then run `uv run python
scripts/diagnostics/probe.py targets` and `uv run python scripts/diagnostics/probe.py health
coredns`.

**No unplanned roll at each flip, H1.2 to H1.7b.** Before the merge and after the promote,
list the pods of every workload in the tag by name, with the read-only ServiceAccount:
`kubectl -n <ns> get pods -o custom-columns=NAME:.metadata.name,START:.status.startTime`.
The two lists match, except for a roll that the takeover census or the diff named. After the promote, the
census run again over the tag prints an empty `removed` list for every object, because the
flip's apply already removed those fields.

**Red proofs for the checks this slice adds.** Each test joins the directory that holds its
subject's tests.

- *Takeover census (H1.1j).* Fixtures are a live object and a dry-run object, as JSON.
    - A Deployment whose live pod template carries `restartedAt` under `kubectl-rollout`,
      with a render that omits it, prints that path under `removed` and prints `rolls`.
    - The same field co-owned by a manager outside the cleanup list is not under `removed`.
      A field owned only by `deploy@daniel-box` and absent from the render is under `kept`.
    - An entry on the `status` subresource under a `kubectl-` name is ignored, as
      `matchFieldManager` ignores it.
    - A fixture pair with no object in common fails, so an empty match cannot pass.

- *Write-verb census.* It parses each task file and joins `argv:` lists before it matches.
    - A fixture task holding `k3s kubectl apply -f x.yaml` is flagged, and so are an `argv:`
      list spelling `kubectl patch`, a `kubectl set image`, a `kubectl rollout restart`, and
      a `kubernetes.core.k8s` module.
    - `kubectl rollout status` and `kubernetes.core.k8s_info` are not flagged.
    - The census asserts that it read `server.yml` and found `storage_smoke.yml`, so an empty
      glob cannot pass.
- *Vendored sha256.* A one-byte change to a vendored fixture fails the test.
- *No `postBuild` with vendored files.* A fixture vendored role whose directory holds a
  placeholder is refused by the renderer.
- *No escape without substitution.* A `${` in a template of a fixture tag with no
  placeholder renders unchanged, and that tag's `flux/<tag>.yaml` has no `postBuild`.
  Adding one placeholder to the same fixture turns the `${` into `$${` and adds `postBuild`
  in the same render, because one key set drives both. A second fixture puts the
  placeholder in `<tag>-deferred` only: that directory escapes and gets `postBuild`, and
  `<tag>` gets neither, because the two directories are decided separately.
- *Prune.* A renderer test asserts that `longhorn`, `coredns` and `metallb` render
  `prune: false`, and that `bento-pdf` renders `prune: true`. A fixture plumbing role that
  renders `prune: true` is flagged.
- *Timeout.* A fixture role with `manifests_rollout_timeout: 30m` renders `timeout: 30m`. A
  role without the key renders the default.
- *Forward-only hold.* Tests in `scripts/deploy_tools/tests/`:
    - a failed revision that changes `longhorn` holds and leaves `prod`;
    - a failed revision whose `last-good` lacks the `longhorn` tag holds and leaves `prod`;
    - a failed revision that changes only `bento-pdf` resets, as D2 specifies;
    - a failed revision whose T lacks `metallb`, which `last-good` has, holds and leaves
      `prod`;
    - a failed revision that only adds a fixture stateful entry to `containers_list`,
      with no snapshot claim, resets. In the same fixture the digests of `metallb`, `coredns`, `longhorn` and
      `longhorn-backup` stay unchanged and only `longhorn-reconcile`'s moves, so a template
      that widens a forward-only tag's inputs goes red;
    - a revision that changes `k3s_longhorn_version` with no `homelab/upgrade-gates` status
      holds without moving `prod`;
    - with a stubbed GitHub API, a gated bump commit followed by an unrelated commit from
      another PR promotes to the unrelated commit, and the same range with an ungated bump
      holds without moving `prod`;
    - a bump commit whose commit-to-pulls lookup returns no PR holds, and one whose lookup
      errors prints `error` and moves nothing;
    - a range (`prod`, T] holding two commits that bump `k3s_longhorn_version` holds, and so
      does one commit that moves it from `v1.12.1` at `prod` to `v1.14.0` at T. After a
      cleared hold that leaves `prod` on a failed bump, one more gated bump promotes;
    - a gate status posted seven hours before `decide` holds, and the same status posted
      one hour before promotes. A seven-hour-old status on a commit in (`last-good`,
      `prod`] does not hold.
- *The hold's form (H1.1b).* A failed revision that changes `longhorn` writes
  `DEPLOY_HOLD` as T's SHA alone. A stub `verify` that writes C2's `<T> failed=<names>` form
  for it goes red.
- *Already-failing dependents (H1.1i).* Tests in `scripts/deploy_tools/tests/`, with stubbed
  statuses and digests. T changes only `cluster-base`'s footprint, so its changed set holds
  `cluster-base` and its dependents `longhorn`, `longhorn-backup` and `longhorn-reconcile`:
    - `flux/longhorn` read `failure` on `prod`'s commit and reads `failure` on T, and every
      other expected name reads `success`. `last-good` moves to T, and the output names
      `excused longhorn`;
    - the same case with `success` recorded on `prod`'s commit holds;
    - the same case with T also changing `longhorn`'s footprint holds;
    - the same case with no recorded status for `longhorn` holds;
    - `flux/longhorn-reconcile` has no status on T at the timeout, and its `dependsOn`
      reaches the excused `longhorn`. The revision holds, because the timeout excuse is gone;
    - the outage regression: a T that changes only `bento-pdf` expects neither
      `flux/longhorn` nor `flux/metallb`, and a stubbed `failure` on both does not hold it.
- *CI pin checks.* `v1.12.1` to `v1.12.0` is flagged as a decrease, and `v1.12.1` to
  `v1.14.0` as a skipped minor. `v1.12.2` is clean. A bump PR that also edits another role is
  flagged. A PR that moves `k3s_longhorn_version` into another file at `v1.12.1`, and
  deletes `setup/k3s` tasks, is clean. `promote` does not ask for the gate status on that
  revision.
- *Land gate.* With no lander variables set, a stubbed gate exit of 1 refuses the merge. A run
  on a host other than daniel-box refuses before the gate. A stub whose (`last-good`, tip]
  already holds a bump of `k3s_longhorn_version` refuses a second bump. So do a
  `DEPLOY_HOLD` holding a SHA, an empty `DEPLOY_HOLD`, a missing one and a failed read of
  `last-good`. The same stub with the earlier bump at `last-good` and `DEPLOY_HOLD`
  reading `none` merges. `--regate` against a merged PR posts the status and makes no
  merge call.
- *Per-tag object census.* A fixture `longhorn` render whose `kustomization.yaml` lost
  `files/upstream/longhorn.yaml` fails publish and names DaemonSet `longhorn-manager`. A
  fixture `longhorn-backup` render without BackupTarget `default` fails and names it, and so
  does a fixture `cluster-base` render without PriorityClass `homelab-critical`.
- *PriorityClasses before Flux (H1.2).* A test in `ansible/tests/k8s/` renders the `flux`
  and `cluster-base` tags. Every `priorityClassName` in the `flux` render is either a
  Kubernetes built-in class or a PriorityClass in the `cluster-base` render, and the test
  asserts that it found `homelab-critical`. A fixture `flux` render naming a class that
  `cluster-base` lacks fails.
- *Mirrored identity keys (H1.2).* `cluster_base_readonly_sa_name` equals
  `k3s_readonly_sa_name`, and the namespace pair is equal too. A fixture that changes one
  side alone fails.
- *Mirrored region keys (H1.7b).* `k3s_etcd_s3_region` in `setup/k3s`'s defaults equals
  `k3s_longhorn_r2_region` in `longhorn-backup`'s defaults. A fixture that changes one side
  alone fails. The census row refuses a fixture template under `ansible/roles/setup/` that
  names `k3s_longhorn_r2_region`.
- *Health targets.* `longhorn` resolves DaemonSet `longhorn-manager`, `coredns` resolves
  Deployment `coredns`, and `metallb` resolves DaemonSet `speaker`. A fixture vendored role
  that resolves nothing fails.
- *Backup targets.*
    - A disarmed render blanks `backupTargetURL` on both `default` and `r2`. An armed render
      carries a URL on each. Each arm flag is tested on its own.
    - The rendered `longhorn-backup` Kustomization carries no `suspend` field.
    - The rendered `default` target carries no unresolved Jinja, and only `${key}`
      placeholders whose keys exist in `secrets.yml`.
    - R2's `PLACEHOLDER_KIND_EXEMPT` pair: `${r2_bucket}` in a BackupTarget passes rule 3,
      and the same placeholder in a ConfigMap fails. The rule finds its named member, the
      `default` BackupTarget, in the `longhorn-backup` render.
- *Host readers follow the cluster (H1.7a).* Each test renders `longhorn-backup-health.sh.j2`
  once and runs the reader twice against a stubbed kubectl, so the script stays fixed while
  the cluster flips. That is the stale-cron case the old export shape could not see.
    - `default` with a URL and `available: false` is flagged. The same rendered script with
      the stub's URL blanked is clean, and `default`'s tier is not required.
    - A stub whose daily RecurringJob runs at `0 3 * * *` grants the first-run grace at
      03:00. Changing only the stub to `0 5 * * *` moves it to 05:00.
    - A failed BackupTarget list is flagged, never read as a disarm. A list without
      `default` is flagged.
    - `probe.py b2-budget` with no `--retain` uses a stubbed weekly retain of 3. A failed
      RecurringJob read exits non-zero instead of using the parser's default of 2.
    - From H1.7b: a stub whose `default` target has a blank URL and carries
      `kustomize.toolkit.fluxcd.io/reconcile=disabled` is flagged, and the problem names the
      target. The same stub without the annotation stays clean, with `default` named in the
      `DISARMED` note.
- *No Flux-born key on the host plane (H1.7b).* A `Census` row of
  `ansible/tests/repo/test_census_rows_roles.py`. A fixture setup template that reads
  `k3s_longhorn_backup_armed` while `longhorn-backup`'s defaults define it is flagged. The
  tree at H1.7b is clean, and the row asserts that it found both named members in
  `longhorn-backup`'s defaults.
- *Availability check.* An `_is_clean` and `_is_flagged` pair: an armed target with
  `available: false` is flagged, and a disarmed target with a blank URL is clean. The check
  must find a target named `default`, so an empty list cannot pass.
- *`longhorn_reconcile.py`.* Unit tests against a stub API cover five cases:
    - a volume whose claim moved to the weekly tier gains its shard label and loses
      `default`;
    - a no-backup claim's volume gains `no-backup`;
    - an already-correct volume gets no patch;
    - a `no-backup` volume whose PVC has class `longhorn-nobackup`, and which no list names,
      stays in `no-backup`;
    - a `no-backup` volume whose PVC has class `longhorn`, and which no list names, returns
      to `default`.

  A failed list call fails the run. A test asserts that the rendered Role and ClusterRole
  grant every verb and resource the script calls.
- *CoreDNS.* The rendered ConfigMap has exactly one data entry, `Corefile`. The health check
  fails on a stub ConfigMap with an empty `NodeHosts`. The Corefile test fails on a fixture
  whose `forward` line lists the upstreams before the Pi-hole VIP. The image test fails when
  the excerpt's version label differs from `k3s_version`, and when `coredns_image` differs
  from the excerpt's image. A test runs `coredns.yml` against a stub `k3s` with the marker
  path in a temporary directory. A stub with no Deployment `coredns` fails the play before
  the marker exists, and a stub that returns the Deployment writes the marker.
- *Break-glass listing.* `probe.py landing` against a stub where BackupTarget `r2` carries
  `kustomize.toolkit.fluxcd.io/reconcile=disabled` names `r2`. A stub with no annotation
  names nothing. A failed read of a named member is reported, never read as "no annotation".
- *Stale-checkout guard.* Each case runs the guard instances against a stubbed inventory
  read.
    - A checkout holding `Install MetalLB`, with `metallb`'s inventory listing DaemonSet
      `speaker`, fails and names the task.
    - A checkout whose `coredns.yml` holds only the CoreDNS read and the marker task passes
      while the `coredns` Kustomization owns Deployment `coredns`.
    - A checkout whose `kubeconfig.yml` holds only its host half passes while `cluster-base`
      owns the read-only ServiceAccount. The same checkout with `Apply the read-only cluster
      identity` restored fails.
    - A stub answering that the Kustomization resource type does not exist passes on the
      first try, and so does a host with no `k3s` binary. A stub answering with any other
      error on every try fails after the bounded retry and names the API as unreachable.
    - A stub that refuses the connection twice and then returns an inventory without a
      mapped id passes. The same stub returning a mapped id fails and names the task.
    - **Selection.** Three cases run the role's real tag selection against a fixture checkout:
        - `--tags node-address` against a stub that refuses every connection completes
          without failing, because no instance is selected. A guard that regains an `always`
          tag, or a union tag set that includes `node-address`, goes red here.
        - `--tags kubeconfig`, with a mapped `kubeconfig` id in the stubbed inventory, fails
          and names the task. An instance that loses its tags goes red here.
        - `--skip-tags kubeconfig`, with a mapped `metallb` id in the inventory, fails and
          names the task. A single guard tagged with the union of the writers' tags goes red
          here, because the skip would drop it.
    - The map test fails on a fixture row naming a task that does not exist, on a fixture
      writer task with no row, and on a fixture row held by an instance whose tags differ
      from its task's effective tags. `longhorn-weekly-shard.yml`'s include, with its
      `apply:` tags, is the named member of the tag computation.
    - **Placement.** A test reads `tasks/main.yml` and asserts that the guard's import comes
      directly after the `server.yml` import and before every other import. The `server.yml`
      import is the named member it must find. It also asserts that the write-verb census
      flags no task in `server.yml`. A fixture `main.yml` with the guard first fails, so a
      whole-role `--tags k3s` run with the API down reaches `server.yml`. A fixture
      `server.yml` that gains a `kubectl apply` fails too, so with the API up a stale writer
      still meets the guard before it runs.
- *Host tasks survive the window's skip (H1.5a).* A test reads `tasks/longhorn-host.yml`
  and asserts that it holds `Install Longhorn prerequisites`,
  `Blacklist Longhorn devices from multipathd` and `Restart multipathd after a blacklist
  change`, its three named members. None of them may carry `longhorn` or `longhorn_backup`.
  A fixture copy with one task tagged `longhorn` fails.
- *Shed disposition (H1.1d, H1.2, H1.4).* In
  `ansible/tests/k8s/test_shed_set_is_derived_from_tier.py`:
    - a fixture untiered entry named in `NEVER_SHED` is absent from `shed_entries`, and a
      fixture untiered plumbing entry outside it is present;
    - a fixture role tree in which an untiered role sets `flux_prune: false` and
      `NEVER_SHED` lacks it fails the equality test, and so does a `NEVER_SHED` member whose
      role prunes;
    - a fixture tiered role that sets `flux_prune: false` and is absent from `NEVER_SHED`
      passes it, as traefik and `flux` do once D5 sets their `flux_prune`;
    - from H1.2 the real tree finds `cluster-base` in both sets, and from H1.4 `coredns` is
      in `k8s_entries()` and not in `shed_set()`.
- *Engine-upgrade limit (H1.5b).* A fixture render with the
  `concurrent-automatic-engine-upgrade-per-node-limit` Setting at `0` is flagged, and the
  real render at `3` is clean. A render without that Setting fails.
- *CoreDNS fleet guards (H1.4).* `uv run pytest ansible/tests/k8s` passes over the derived
  templates with one `_SYSTEM_TIER` row added and at most one `_ROOT_ALLOWED` row.
- *Publish census floor (H1.9).* A fixture with the `owner` keys removed, read through an
  unchanged `owner: flux` selector, fails publish because `longhorn` is missing.

## Rollback

- **H1.1a to H1.1j:** revert the PR. Nothing moved, so nothing on the cluster changes.
    - A revert of H1.1d after H1.2 keeps `NEVER_SHED` and its tests. A full revert would
      return the plumbing tags to the shed set.
- **H1.2 to H1.6, and H1.7b:** revert the PR. The tag leaves the bundle, and its objects stay
  serving, because the tag renders `prune: false`. That holds before and after D6.
    - The reverted commit restores the Ansible tasks and their guard-map rows, but nothing
      runs them.
    - The revert removes the tag's `containers_list` entry, which reaches every reader of
      the list as the addition did. Before D6.12 the revert PR carries the same narrow
      output, stop rule and hold exit (*Rules for every PR from H1.2 on*).
    - The root `homelab` Kustomization deletes the child only after the revert promotes. Until
      then the child's inventory still lists the objects, and the guard refuses the
      restored tasks. So the operator waits until
      `kubectl -n flux-system get kustomization <tag>` returns NotFound, and until
      `last-good` reaches the revert commit. A revert whose verify fails holds when the tag
      is forward-only, because T lacks a tag that `last-good` has. `cluster-base` is not
      forward-only, so a failed revert of H1.2 resets and re-creates its Kustomization.
      Waiting for `last-good` keeps the owed apply out of that window.
    - The operator then applies the restored tags with `k3s-bringup.yml --tags <tags>`,
      which takes the objects back. A revert's range refuses the narrowing for the same
      reasons its PR's did, so the revert PR's body lists the restored task files' tags,
      and the operator uses the bare clear after the range check (*The owed `manual_plane`
      line*).
    - Until that apply, the objects are unmanaged and unchanged.
    - A revert of H1.5a, H1.5b, H1.6 or H1.7b also reverts that PR's edit to the bring-up
      order: step 1's two extra skips, step 7a and its list. The reverted text matches the
      tasks the revert restores. A revert of H1.5a also returns the three host tasks to
      `longhorn.yml` under `longhorn`, so step 1 goes back to `--skip-tags kubeconfig`.
    - A PR that moved a pin is not reverted after a later bump, because CI refuses the
      decrease. The pin moves back in a forward PR that keeps the version.
    - `longhorn-reconcile` prunes by default. A revert of H1.6 orphans its CronJob under
      D2's `deletionPolicy: Orphan` and deletes it after D6.20. An orphaned CronJob writes the
      same labels that the restored tasks write, and the revert PR names its
      **operator-only** delete.
- **H1.7a:** revert the PR while H1.7b has not landed. The routing tasks return, and they
  patch the same field the reconcile arm patched, to the same values. The two host readers
  bake the keys again, which still sit in `setup/k3s`'s defaults, and the owed apply
  re-renders them. After H1.7b, a revert of H1.7a alone fails CI on H1.7b's census row,
  because the restored template reads a key that `longhorn-backup` defines. Revert H1.7b
  first.
- **H1.7b and the retired flag.** Reverting H1.7b restores `k3s_manage_backup_targets` at
  `true`, its default, together with the tasks it gates.
- **H1.8a:** revert the census change.
- **H1.8b:** never reverted. Longhorn does not downgrade, CI refuses the decrease, and the
  recovery from a bad bump is the restore-from-backup runbook.
- **H1.9:** revert. The loaders read `host_vars` again.

**What a partial failure leaves live:**

- A Kustomization that fails health keeps the objects it applied. Flux retries. `promote`
  holds without reset for a forward-only tag, and resets for any other.
- A failed `longhorn-reconcile` run leaves labels as they were. The next hourly run retries,
  and `kube_job_status_failed` records the failure.
- A failed vendoring refresh leaves the old file. The sha256 test then fails the Renovate PR
  until someone runs the refresh.
- A merged H1 PR whose owed line is not yet cleared leaves the `manual_plane` line. The
  banner and monitor-bridge report it.

## Risks

- **Prune deletes what a render drops.** That is the design's own named risk for Flux. The
  five plumbing tags render `prune: false`, so nothing in them is pruned. The per-tag object
  census still fails publish when a named member disappears. A dropped object outside the
  named members goes unmanaged unreported, which is why each tag names the objects whose
  loss matters most.
- **A reset downgrades Longhorn, or two bumps apply as one skip.** The forward-only hold and
  the CI decrease check stop the reset. H1.1g's land rule reads (`last-good`, tip] and
  H1.1b's backstop reads (`prod`, T], so two sequential minor bumps cannot apply in one
  promotion.
- **A node outage fails `longhorn` and `metallb` on every revision.** C2's changed set
  expects neither unless T changes it or a name upstream of it, so most revisions promote,
  and C7's `flux` rule group pages the failing tags. H1.1i reports a dependent that was
  already failing. Three cases still hold:
    - a revision that changes `longhorn` or `metallb` during the outage;
    - a revision that changes a name upstream of them, such as `cluster-base`. Their
      transitive dependents `longhorn-backup` and `longhorn-reconcile` never apply while
      `longhorn` is not Ready, so they may have no recorded status and time out;
    - an outage that starts while `verify` waits.

  The operator clears those holds once the node is back. H6's node
  upgrades take a node down by design, and D5's node-pinned workloads fail health while
  their node is down, so both widen the exposure.
- **A held bump's gates go stale.** `promote` refuses a gate status older than six hours,
  and `land.sh --regate` re-runs the gates before the operator clears the hold.
- **A DR bring-up deploys before it restores.** The root stays suspended until the restores
  finish, and the operator applies only the plumbing Kustomizations, their `dependsOn`
  closure and the shared kinds the restore needs before them. The root-only suspend is a
  `homelab-operator` hand patch that resumes the OCIRepository after D1's
  `flux_sync_suspend` run, so D1 adds no variable. A `flux` role run with its defaults
  before step 11 would resume the root early, so the order forbids one. From D5's flux PR
  on, D5's section owns the order (*Open questions* 12).
- **A merge during a DR moves `prod` under the hand-applied children.** While the root is
  suspended, no promotion can verify, so a merge by another session would promote, time
  out, hold and reset `prod` to `last-good`. The children of step 7 would then apply a
  commit other than the one they were rendered at. Step 7 sets the **operator-only** DR
  hold `dr-restore` before it renders, unless step 6's hold or a stray one already holds,
  and step 12 clears it. A hold that a promotion left after the cluster was lost, or during
  steps 1 to 6, is settled first: before step 6's arm PR, or at step 7.
- **A DR inside the Longhorn window, H1.5a until H1.7b, runs from the wrong order.** From
  H1.5a, an unchanged step 1 fails on the first task that reaches `longhorn-system`, and
  until H1.7b nothing but step 7a creates the backup targets. H1.5a writes the window's
  step 1 and step 7a into the three runbooks. A skipped step 7a fails loudly: step 9 finds
  no target to read available, and the restores cannot start. A step 7a run from a
  checkout older than `prod` fails the guard and applies nothing.
- **A k3s rollback with the API down.** The re-pin in `docs/k3s-upgrade.md`'s *Rollback*
  runs `--tags k3s_server`, which selects no guard instance, so it never reads the API. A
  whole-role `--tags k3s` run reaches the instances only after `server.yml` has reinstalled
  k3s and waited for the node. The placement test fails a guard import moved back to the
  top of `main.yml`.
- **A host-only apply with the API down.** The documented remedy for a wrong LAN address pin
  is `k3s-bringup.yml --tags node-address` on daniel-box, and it runs while k3s cannot bind
  (`docs/k3s-node-plane-crons-and-incidents.md`, *Both nodes pin their LAN address, because
  k3s binds it*). `static-address.yml` comes after the guard's import in `main.yml`, and
  `server.yml`'s tasks all carry `k3s_server`, so nothing brings the API up first. No guard
  instance carries `node-address`, `node-dns`, `longhorn_host`, `backup-health` or any other
  host-only tag, so these applies select none and run as they do at `70591c307`. The
  `--tags node-address` selection case fails if an instance gains that tag or `always`.
- **A takeover PR re-applies readers of `containers_list`.** Until D6.12, an added or
  removed Flux-born entry narrows the deploy plane to the readers of the fields it carries,
  applied unattended and forward-only. For H1's entries that is artifacts,
  blackbox-exporter, pihole and uptime-kuma at `a5abf8581` (D2 Open question
  10). Three of them, blackbox-exporter, pihole and uptime-kuma, set
  `k8s_autodeploy: false`. Each of the six PRs and each revert quotes
  the narrow output and runs D1.2's stop rule, so no reader carries another session's
  unlanded change into that apply. The cost ends at D6.12, because no narrowing can drop
  artifacts, whose `known_services.json` names every entry (D2.1c).
- **A tier change could make a revision forward-only.** The desired tiers live in
  `longhorn-reconcile`, which is not forward-only. A red fixture fails when adding a stateful
  entry moves a forward-only tag's digest.
- **An ungated bump rides in with other commits.** A promote run can take a T several
  commits past the bump. The gate check walks every commit in (`last-good`, T] and maps each
  pin change to its own PR, so neither a gated bump nor an ungated one hides behind T's PR.
- **A fresh cluster deadlocks on its own PriorityClass.** Flux's controllers name
  `homelab-critical`, which only `cluster-base` defines. The bring-up order applies
  `cluster-base` by hand before Flux, a test ties the classes in the two renders together,
  and R4's drill later exercises the order.
- **Two writers on the `longhorn` StorageClass.** Flux does not apply that class. It owns the
  ConfigMap that longhorn-manager writes the class from, which the baseline measured.
- **The takeover rolls a plumbing workload, or deletes a field, outside any window.** D2's
  Baseline bullet *The takeover rolls workloads once* measures this for the services: 59 of
  71 Deployments and 5 DaemonSets carry `restartedAt`, and each rolls once at its takeover.
  The same cleanup runs on every plumbing tag's apply (Baseline). On 2026-10-11 no plumbing
  workload carried `restartedAt`. A `kubectl rollout restart` before a flip would add one,
  and the apply would then roll `longhorn-manager` with `maxUnavailable: 100%`, both pods at
  once, at an unscheduled promote. The takeover census lists each such field before merge,
  H1.5a measures its roll with a pod delete, and the post-promote pod read confirms that
  nothing else rolled.
    - **After the flip, the same cleanup reverts hand writes.** It runs on every apply, so a
      later `kubectl rollout restart` or `kubectl patch` of a plumbing object is removed at
      the next reconcile, which rolls a workload a second time. Restart a plumbing workload
      by deleting its pods. Each break-glass procedure annotates its object
      reconcile-disabled before its hand write, and Flux skips an object with that
      annotation before its cleanup runs (`ssa/manager_apply.go:Apply`). The hand write
      lasts until the procedure removes the annotation, and the render then wins.
    - **A manager outside the cleanup list keeps its fields.** `deploy@daniel-box` on
      `coredns` and `k3s` on every workload are not `kubectl` names, so the takeover leaves
      their entries alone. A field one of them owns and the render omits stays live after
      the flip, owned by that manager and invisible to Flux's drift view. The census's
      `kept` list names each one before merge.
- **Sidecars that only exist at runtime.** `longhorn-driver-deployer` recreates the CSI
  Deployments on every Longhorn upgrade, without the memory requests. The hourly reconcile
  restores them within an hour, which is no worse than the hand apply it replaces.
- **DNS that Flux depends on.** CoreDNS's `reload` keeps the old config on a parse error, the
  tag is forward-only, and the operator-only break-glass needs no fetch.
- **The incident lever.** A disarm needs a merge, a promote and a Ready `longhorn`, because
  `longhorn-backup` depends on it. The break-glass covers a hold, a spiral that outruns a
  merge, and a `longhorn` that is not Ready. Its step 4 waits on `longhorn-backup`'s own
  applied revision, so the annotation never comes off before the disarm has applied.
- **Failure class 2, the check observes a proxy.** Flux `Ready` and `probe.py health` see only
  the objects the tag declares. The CSI sidecars and engine images sit outside the bundle.
  The health check therefore resolves vendored workloads by name. The exit check reads the
  manager image and every volume's engine image directly.
- **Failure class 3, empty read as clean.** `longhorn_reconcile.py` treats a failed list call
  as a failed run, never as zero volumes to fix. The health check fails a vendored role that
  resolves no workload, and the availability check fails without a `default` target. The
  host backup-health reader treats a failed BackupTarget list as a problem, never as a
  disarm. The `promote` gate check treats a failed or empty commit-to-PR lookup as a hold or
  an error, never as "gated". The stale-checkout guard passes only on two named absences,
  and fails on any other read error that outlasts its bounded retry.
- **Failure class 4, selector drift.** The write-verb census, the object census and the
  health targets each assert named members. The census that keeps Flux-born keys off the host
  plane names two members it must find.
- **Failure class 4, a derived set takes in entries nobody chose.** `shed_entries` sheds
  every untiered entry, so each plumbing tag would join `probe.py shed-set` when its entry
  landed. The node-loss break-glass would then scale CoreDNS to zero. Flux could not fetch
  the bundle that restores it, because Flux resolves GHCR through that DNS. `NEVER_SHED`,
  held equal to the unpruned roles, gives each plumbing tag its disposition in the PR that
  adds it. `coredns` is the named member the real-tree test must find.
- **The vendored workloads bypass the render-time fleet guards.** The guards read
  `rendered_docs()`, which renders templates only. This is accepted because the same bytes
  reach the cluster unchecked at `70591c307`. The sha256 pin, the object census, the health
  targets and the takeover census check them instead (*Vendored upstream manifests*).
- **Failure class 5, sequences treated as transactions.** Each PR hands a whole object set to
  Flux at once. H1.5a moves everything that would otherwise fight over the manager and the
  ConfigMap. The RecurringJobs move with the targets, so no window has two writers on them. A
  key flip is one change, so no key is read by both a Flux tag and a host cron, which apply
  it at different times (H1.7a, H1.7b).
- **The host reader reports the live arm state, not the intended one.** An arm PR that has not
  promoted yet reads as disarmed until it does. `promote` reports that case as a hold or a
  pending revision. Flux restores a URL blanked by hand at its next reconcile, unless the
  break-glass annotation holds it. From H1.7b the reader reports an annotated target as a
  problem, so a held break-glass reads red until step 4 removes the annotation, and a
  forgotten one cannot suppress a target behind a green monitor. `probe.py landing` names
  each annotated object as well. A disarm through git stays green, and the verdict's
  `DISARMED` note names the target and the count of volumes not checked, as ADR-0007
  requires.
- **Failure class 7, the execution context differs.** The tasks ran as root with the admin
  kubeconfig. The CronJob runs under a narrow ServiceAccount, and a test ties its Role to
  the calls the script makes. Gate 2 reads a stamp on daniel-box's disk, so `land.sh` refuses
  the pin gate elsewhere.
- **Failure class 8, prose that nothing re-derives.** The `setup/k3s` `CLAUDE.md` contract
  moves with the actuators, and its ENFORCED node ids move in the same PR. Until G2 retires
  `docs/facts.lock`, each touched section is re-verified with `fact_status.py verify`.

## Tests and docs that change in the same PR

- **H1.1d:** `ansible/tests/k8s/test_shed_set_is_derived_from_tier.py`, the module docstring
  of `ansible/filter_plugins/service_tier.py` for `shed_entries`, and the module docstring of
  `scripts/diagnostics/probe_lib/shed_set.py`, which says a service leaves the set only by
  gaining a `tier:`.
- **H1.1g:** `renovate.json` and the tests under `scripts/tests/` that read its rules, and the
  gate section of `docs/longhorn-upgrade.md`.
- **H1.1h:**
  `scripts/deploy_tools/tests/test_narrow_setup.py:test_the_real_k3s_roles_untagged_task_files_are_named_as_such`
  gains `tasks/stale-checkout-check.yml`.
- **Readers of a deleted task file or a deleted task.** The census below ran
  `git grep -l` on each deleted basename and `git grep -F` on each deleted task name that a
  test loads, over `ansible/tests`, `ansible/roles/setup/k3s/tests`,
  `scripts/deploy_tools/tests` and `scripts/tests` at `70591c307`. Each PR re-runs it at
  its base for the tasks it deletes.
    - *H1.4.*
        - `ansible/tests/deploy/test_conditional_register_consumers.py:test_the_widened_rule_would_have_caught_the_coredns_read`
          asserts one `Read the live Corefile` task with `check_mode: false` in the real
          `coredns.yml`. H1.4 deletes that task. The anchor takes a fixture instead: the
          two tasks `Read the live Corefile` and `Point cluster DNS at Pi-hole first`, as
          they read at `70591c307`, written inline in the test. The anchor pins the rule's
          behaviour, which needs no real file. `_KNOWN_TASK_FILES` drops
          `setup/k3s/tasks/coredns.yml`, which then holds no consumer of a conditional
          register.
        - `ansible/tests/deploy/test_retried_commands_survive_check_mode.py:NON_RETRIED_READS_THAT_MUST_OPT_OUT`
          loses its `coredns.yml` row. The bounded read carries `retries` and `until`, so
          the retry census in the same module reads it directly. The same module's list
          that names `Verify cluster DNS resolves through the configured upstream` loses
          that name, because H1.4 deletes the probe and `probe.py health coredns` takes
          over its check (*CoreDNS without a host read*).
        - `ansible/tests/setup/test_k3s_stage_gates.py:test_the_coredns_probe_name_defaults_to_a_real_name`
          guards the probe's name variable. It goes with the probe, and so does the
          variable `k3s_coredns_probe_name` unless the per-key census finds another reader.
    - *H1.5a.* `ansible/tests/longhorn/test_longhorn_control_plane_resources.py` loads
      `Install Longhorn` and `Give longhorn-manager a memory request so it leaves
      BestEffort`. `test_manager_patch_runs_after_longhorn_is_installed` is deleted,
      because a kustomize patch applies with the object it patches and has no order.
      `test_manager_patch_sets_a_request_and_no_limit` and
      `test_manager_patch_names_the_longhorn_manager_container` retarget to the `longhorn`
      role's manager patch.
    - *H1.5b.* `ansible/tests/longhorn/test_snapshot_data_integrity_patch_is_a_json_map.py`
      reads its Setting patch from a task in `longhorn.yml`. It retargets to the rendered
      Setting object in the `longhorn` role.
    - *H1.6.*
        - The CSI-sidecar cases of `test_longhorn_control_plane_resources.py` move to
          `longhorn-reconcile`'s tests, beside the ported CSI-request logic.
        - `ansible/roles/setup/k3s/tests/test_longhorn_group_labels.py` loads the label tasks
          from `longhorn.yml` and `longhorn-weekly-shard.yml`. Its label cases move to
          `longhorn-reconcile`'s tests, against `files/longhorn_reconcile.py`.
        - `ansible/tests/longhorn/test_daily_group_membership_is_the_r2_list.py:test_the_reconcile_returns_only_the_unlisted_volumes_to_default`
          loads `Return volumes to the default` from `longhorn.yml`. It retargets to the
          reconcile script's default-group rule.
        - `test_conditional_register_consumers.py:_KNOWN_TASK_FILES` drops
          `setup/k3s/tasks/longhorn.yml` and `longhorn-weekly-shard.yml`. Its non-vacuity
          test still needs members. `longhorn-host.yml` holds `Restart multipathd after a
          blacklist change`, whose `when:` reads the register `k3s_multipath_conf`. The PR
          adds that file only if `_offenders` reads that consumer, and otherwise names
          another surviving file that holds one.
        - `test_narrow_setup.py:test_the_real_k3s_roles_untagged_task_files_are_named_as_such`
          drops `tasks/longhorn-weekly-shard.yml`.
        - `scripts/deploy_tools/tests/test_narrow_setup_task_file_kinds.py:test_the_real_k3s_weekly_shard_file_narrows_to_longhorn`
          loses its real-tree member. No surviving file of the role has the
          include-with-`apply:` shape, so the case moves to the fixture role tree that its
          neighbouring cases build.
        - `ansible/tests/longhorn/test_every_longhorn_pvc_has_a_tier.py` names a line range
          of `longhorn.yml` in its docstring and in its failure message. Both name the
          `longhorn-reconcile` role instead.
    - *H1.7b.* `_KNOWN_TASK_FILES` drops `setup/k3s/tasks/longhorn-backup.yml`. The two cases
      of `test_longhorn_group_labels.py` that load `longhorn-backup.yml` retarget to the
      `longhorn-backup` role's render.
    - *Sample input, no change.* `ansible/tests/deploy/test_registering_consumers.py` holds
      `Read the live Corefile` inside a fixture string.
    - *H1.3.* No test names `cluster-addons.yml`, `Install MetalLB` or
      `Apply the workload-tier PriorityClasses`.
- **H1.2:**
    - the comment on the agent kubeconfig task in
      `ansible/roles/setup/claude_code/tasks/agent.yml`;
    - `docs/gitops-pipeline.md`, whose `--tags kubeconfig` example names a tag that keeps
      only its host half;
    - `ansible/tests/k8s/test_k8s_manifests_rbac.py`, which renders the identity template
      from `cluster-base`;
    - `scripts/deploy_tools/tests/test_narrow_setup.py`, whose real-tree case takes
      `readonly-kubeconfig.j2` and `k3s_readonly_kubeconfig_server`;
    - `ansible/tests/k8s/test_priorityclass_omits_defaulted_fields.py`, whose `TEMPLATE`
      names `setup/k3s/priorityclass.yaml.j2`, and
      `ansible/tests/k8s/test_pod_template_hygiene.py`, whose `_PRIORITYCLASS_TEMPLATE` and
      module docstring read the tier names from that template. Both render it from
      `cluster-base`;
    - the comments that name either template's old path: above
      `scripts/dev/new_k8s_service.py:PRIORITY_CLASSES`, in
      `scripts/diagnostics/probe_lib/health_cronjob.py`, and in a reason string of
      `ansible/tests/k8s/_edge_property_rows.py`;
    - the comment above `headlamp_k8s_crd_api_groups` in
      `ansible/roles/k8s/headlamp/defaults/main.yml`, which names `cluster-base`'s defaults;
    - `docs/longhorn-disaster-recovery.md`, `docs/break-glass.md` and the header of
      `storage_smoke.yml`, which gain the bring-up order with its hand apply of
      `cluster-base`, its `--skip-tags kubeconfig` first run, its `sops-age` step, its
      suspended root and its restores before the root resumes;
    - the shed scenario of `docs/break-glass.md`, steps 1 to 3, for `NEVER_SHED` and the
      restore of a Flux-owned service.
- **H1.2 to H1.7b:** each moved template's tests retarget the k8s role. The PR finds them with
  `git grep -l` on each moved basename, as *Rules for every PR from H1.2 on* requires, and
  the lists here are that census at `70591c307`. Other candidates are the
  `test_longhorn_*` modules under `ansible/roles/setup/k3s/tests/` that render tasks. The
  role glance blocks regenerate.
    - Four tests name a moved path only as sample input, so they need no change:
      `scripts/deploy_tools/tests/test_land_reads_the_tick_receipt.py` writes
      `readonly-rbac.yaml.j2` into a synthetic tree, and the two `gitops_deploy` tests
      `test_gitops_deploy_broad_park.py` and `test_gitops_deploy_setup_narrow.py` pass its
      path to a stubbed tick. `ansible/tests/repo/test_guard_tests_read_renders_not_templates.py`
      holds `longhorn-recurringjob.yaml.j2` inside a source string that it feeds to the guard.
    - A docstring in
      `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_defer.py` and the module
      docstring of `scripts/deploy_tools/narrow_setup.py` describe a past
      `readonly-rbac.yaml.j2` case, and they stay.
- **H1.3:**
    - `ansible/tests/k8s/test_k8s_manifests_metallb.py`, which renders `metallb-pool.yaml.j2`
      with `k3s_metallb_pool` from `setup/k3s`'s defaults, and whose
      `test_metallb_version_still_supports_the_metallb_io_annotations` reads
      `k3s_metallb_version`. Both read the `metallb` role;
    - `ansible/tests/k8s/test_vip_pins.py`, which renders the pool template through
      `rendered_setup_text`, and the docstring of
      `ansible/tests/services/test_pihole_redundancy.py`, which names it;
    - `ansible/tests/repo/test_lan_facts_have_one_source.py`, whose exemption for the pool's
      address range moves to the `metallb` role's defaults;
    - the reason string in `ansible/tests/k8s/_edge_property_rows.py` that names
      `k3s_metallb_version`.
- **H1.7b, the RecurringJob template's readers:**
  `ansible/tests/longhorn/test_daily_group_membership_is_the_r2_list.py` (`RECURRING_JOB`),
  `ansible/tests/longhorn/test_longhorn_reap_opt_out_groups.py`
  (`RECURRING_JOB_TEMPLATE`), `scripts/docs/catalog_lib/catalog_backup.py` and
  `scripts/docs/fragments_lib/fragments_storage.py` read `longhorn-recurringjob.yaml.j2`
  from `setup/k3s`. The PR repoints each one to `longhorn-backup`.
- **H1.3 to H1.7b:** the `setup/k3s` `CLAUDE.md` intro, which lists MetalLB, Longhorn, the
  backup targets and CoreDNS, loses each one as it moves.
- **H1.4 and H1.7b:** `scripts/deploy_tools/tests/test_narrow_setup_facts.py`, which asserts
  the set of task-made facts `k3s_coredns_bundled_image`, `k3s_coredns_corefile` and
  `k3s_longhorn_b2_region`. H1.4 deletes the first two set_facts, and H1.7b deletes the third.
- **H1.4:**
    - `ansible/tests/setup/test_coredns_image_follows_bundled_manifest.py`;
    - `ansible/tests/k8s/test_pod_template_hygiene.py:_SYSTEM_TIER`, which gains the CoreDNS
      row, and `ansible/tests/k8s/test_container_security_context.py:_ROOT_ALLOWED` if the
      image runs as root;
    - `docs/k3s-upgrade.md`, whose `--tags coredns` step becomes the CoreDNS pin move and
      gains the break-glass;
    - the tests of `scripts/diagnostics/probe_lib/landing_blockers.py`, for the annotation
      listing.
- **H1.3 to H1.7b:** `docs/longhorn-disaster-recovery.md`, `docs/break-glass.md` and the
  header of `storage_smoke.yml`, for the bring-up step each PR changes. H1.5a, H1.5b, H1.6
  and H1.7b also open, narrow and close the Longhorn window's step 1 and step 7a there.
- **H1.5a:** `ansible/roles/k8s/longhorn-ui/CLAUDE.md`, which says `roles/setup/k3s`
  installs Longhorn, and the per-hop procedure of `docs/longhorn-upgrade.md`.
- **H1.2, H1.3, H1.5b and H1.7b:** `scripts/validate/refresh_vendored_schemas.py`, whose
  `CORE_GROUP_FILES` gains `scheduling.k8s.io` in H1.2 and whose `VENDORED` gains each CRD
  kind in the other three, and the schema files its refresh writes.
- **H1.5a, H1.5b, H1.7a and H1.7b:** every reader the per-key census lists, including
  `scripts/lib/service_tiers.py`, `scripts/docs/fragments_lib/fragments_storage.py`, the
  `FRAGMENTS` sources in `scripts/docs/gen_doc_fragments.py`,
  `.claude/rules/generated-docs.md` and
  `scripts/docs/catalog_lib/catalog_backup.py:load_longhorn_tier_lists`.
    - The tier lists move to the `longhorn-reconcile` role's defaults in H1.7a, and the
      glance blocks
      must not change. An unchanged `uv run python scripts/docs/gen_role_glance.py` proves
      that.
    - The doc fragments must not change either. An unchanged `uv run python
      scripts/docs/gen_doc_fragments.py` proves that.
- **H1.5b:** `docs/deploying.md`, which says to land an entry "before the next
  `k3s-bringup.yml --tags longhorn` apply", and step 4 of `docs/longhorn-upgrade.md`.
- **H1.6:** `ansible/roles/setup/k3s/CLAUDE.md` loses the reconcile actuator from its contract,
  and the `longhorn-reconcile` role `CLAUDE.md` gains it.
- **H1.7a:**
    - `scripts/validate/tests/test_backup_health_shim.py`,
      `ansible/roles/setup/k3s/tests/test_longhorn_backup_health_reader.py`,
      `test_longhorn_backup_grace_cron.py` and `_longhorn_reader_stubs.py` beside them,
      which set the four exports the shim no longer bakes;
    - `scripts/diagnostics/tests/test_probe_longhorn.py`, for `b2-budget`'s live retain;
    - `docs/longhorn-disaster-recovery.md`, whose disarm step (flag plus deploy) gains the
      fact that backup health follows the live URL, so a disarm no longer turns its monitor
      red;
    - the tests that read the tier lists from `setup/k3s`'s defaults, among them the label
      cases that H1.6 moved from `ansible/roles/setup/k3s/tests/test_longhorn_group_labels.py`
      to `longhorn-reconcile`'s tests, and the tier tests under `ansible/tests/longhorn/`;
    - `docs/adr/0007-backup-tiering-r2-daily-b2-weekly.md`, the **operator-only** amendment
      of *Governs* and the new consequence.
- **H1.7b:**
    - `ansible/roles/setup/k3s/CLAUDE.md` loses the RecurringJob actuator and the two arm
      flags' text, and the `longhorn-backup` role `CLAUDE.md` gains them. Its *Mode /
      arming* bullet drops `k3s_manage_backup_targets`;
    - `ansible/tests/setup/test_k3s_stage_gates.py`, whose docstring names
      `longhorn-backup.yml` as one of the gated imports. `health-crons.yml` keeps its gate,
      so `test_some_imports_are_gated` still finds one;
    - `docs/longhorn-backup-tiering.md`, `docs/longhorn-disaster-recovery.md` and
      `docs/k3s-node-plane-crons-and-incidents.md` name the `longhorn-backup` tag and the
      break-glass in place of `k3s-bringup.yml --tags longhorn_backup`.
- **H1.8b:** `docs/longhorn-upgrade.md` records what the exit proof measured. Its gate
  section and per-hop procedure already changed in H1.1g, H1.5a and H1.5b.
- **H1.9:**
    - the loader tests in `scripts/lib/tests/`;
    - `ansible/preflight.yml` and its tests;
    - `.claude/rules/` and `CLAUDE.md` prose that names `host_vars/daniel-box.yml` as the
      service list.
- **Every PR that edits a `CLAUDE.md` section citing atoms:** until G2 retires
  `docs/facts.lock`, run `uv run python scripts/dev/fact_status.py verify '<doc>#<heading>'`
  and commit the lock.

## Complexity delta

- **Added:**
    - six k8s roles: two of mostly vendored bytes, one derived from k3s's manifest, and
      `longhorn-reconcile`, which holds the ported reconcile;
    - operator-only bring-up steps: the hand apply of `cluster-base`, `sops-age`, the hand
      apply of the child Kustomizations while the root is suspended, the two
      `homelab-operator` hand patches that resume the OCIRepository and then the root, the
      restores between them, and the `dr-restore` hold from step 7 to step 12;
    - a mirrored pair of identity keys and a mirrored pair of R2 region keys, each with the
      test that holds it equal;
    - the narrow output and D1.2's stop rule over the reader tags it names on each PR that
      adds or removes an entry, until D6.12;
    - a commit-to-PR lookup in `promote`'s gate check;
    - a break-glass check in the backup-health reader and an annotation listing in
      `probe.py landing`;
    - the `PLACEHOLDER_KIND_EXEMPT` set that R2 assigns to H1.7b;
    - one CronJob and its script, which ports the task logic;
    - the vendoring script;
    - the takeover census script, and its run before each flip (**operator-only**);
    - kustomize patches, `prune: false` and a per-tag timeout in the renderer, with the
      per-directory placeholder key set if R2 or D3 has not added it;
    - the forward-only hold, the status check with its age bound and the backstop in
      `promote`, and the report of an already-failing dependent in `verify`, narrowed from
      an excuse with a timeout case;
    - the per-tag object census in publish;
    - vendored targets and three checks in `probe.py health`;
    - the CI pin checks, the `land.sh` pin gate, its land rule and `--regate`;
    - the stale-checkout guard: two task files, one check instance per writer tag set
      placed after the `server.yml` import, its bounded read, and its placement, selection
      and tag-parity tests;
    - a hand-listed apply and a bare clear in each of the nine PR bodies from H1.1h to
      H1.7b, because their ranges are expected to refuse the narrowing;
    - the `longhorn_host` tag on `tasks/longhorn-host.yml`, and, from H1.5a until H1.7b,
      two extra skips in bring-up step 1 and the **operator-only** step 7a;
    - `NEVER_SHED` in `service_tier.py`, with its equality test against the untiered
      unpruned roles;
    - one **operator-visible** `_SYSTEM_TIER` row for CoreDNS, and five CRD kinds in
      `VENDORED`;
    - a render test that keeps the engine-upgrade limit above 0;
    - two list calls in the backup-health reader, a live retain read in `b2-budget`, and the
      census row that keeps Flux-born keys off the host plane;
    - the plain `k3s_longhorn_b2_region` key and `cluster/services.yml`.
- **Removed:**
    - four task files and about 70 lines of `coredns.yml` from `setup/k3s`;
    - every live URL fetch at apply time;
    - every Longhorn bump's hand apply;
    - the regex that derived the B2 region;
    - four baked exports in the backup-health shim, up to four per-target kubectl calls, and
      `--retain` in the b2-budget cron;
    - the `k3s_manage_backup_targets` flag;
    - the `containers_list` keys from daniel-box's and daniel-server's `host_vars`.
- About 56 of the 74 line edits that the design counted in `setup/k3s/defaults/main.yml`
  move to roles that deploy on merge.

## Open questions

1. **Dependency edges the slice index lacks.** H1.7b needs D3, because the backup credentials
   are cluster secrets (C3). H1.9 needs R4, because C4's removal is safe only when no Ansible
   k8s play reads the list. Everything else needs only D2 and D4. *Resolution:* the index
   lists H1 as depending on D2 and D4, with D3 for H1.7b and R4 for H1.9.
    - H2's row narrows to H1.1a to H1.8. contracts.md's index row reads that, H2's header
      reads "H1 (H1.1a to H1.8 only)", and H2's *Open questions* 1 proposes "H1
      (H1.1a–H1.8)". H1.8 is the exit proof. No H2 PR reads it, and the edge is acyclic
      either way, so H1 adopts the form the index already holds.
    - The narrowing is required, not cosmetic. H1.9 waits on R4, R4 on D6 and D6 on D5. D5's
      wave D waits on H2.1 and on H2's role PRs, so an edge from H2 to all of H1 would be a
      cycle.
    - The alternative is to make H1.9 a slice of its own.
2. **C1 and C3 for vendored manifests.** C1 does not say that a role may ship verbatim files
   or kustomize `patches:`. C1 decides substitution per tag and names the rendered
   Kustomization as the one source. D3 *Which Kustomizations substitute (D3.3b)* decides per
   directory, and R2's 56-tag CI render writes no `DIR/flux/` file for most tags. C3 fixes
   `prune: true`, and names no per-tag timeout. *Resolution:*
    - H1 adopts R2's *Open questions* 5 wording for C1 *Substitution and escaping*: "A
      Kustomization directory under `DIR/cluster/` *substitutes* when its render holds at
      least one placeholder. `<tag>` and `<tag>-deferred` are decided separately. The one
      source for this fact is the renderer's placeholder key set for that directory,
      computed in process from the render." `spec.postBuild` is its projection, and C1
      *Only placeholder mode publishes* takes the same unit.
    - contracts.md records R2's four rows, each gated before R2's PR 1 merges, and reopens
      X-12 (a cross-slice note). H1's *One key set per directory decides substitution*
      already reads the per-directory unit, so it needs no further row.
    - Add the vendored files and `patches:` to C1, with one sentence: a directory that holds
      vendored files never substitutes.
    - Amend C3's *Per-role fields* so that its first field reads "`prune: false`, for a
      plumbing tag (H1) or a tag whose render holds a CustomResourceDefinition (D5)".
      **Gate: before H1.1a merges**, because H1.1a adds `flux_prune`. This replaces the
      earlier proposal to make every vendored CRD prune-exempt. C3 already names a role's
      own `timeout`, so that half needs no row.
    - The register's row *`prune: false` beyond vendored CRDs* carries D5's half only, gated
      before traefik's flip. Under it, `cluster-base`, `coredns` and `longhorn-backup`, which
      hold no CRD, stay outside C3, and H1.2, H1.4 and H1.7b could not ship. A cross-slice
      note asks contracts.md to replace that row with one row that carries both halves and
      H1.1a's gate. That gate also comes before traefik's flip, which waits on H1.1a, so D5
      loses nothing. A second note asks D5 to quote the merged wording.
    - **Applied on 2026-10-16.** C3 *Per-role fields* carries the merged wording as one row,
      and D5 Open question 5 quotes it. contracts.md's round 2 applied C1's *Vendored files*
      and the per-directory unit (X2-09). The gate before H1.1a merges is clear.
3. **C2's reset is unsafe for forward-only tags.** *Resolution:* amend C2. contracts.md's
   pending row, *The forward-only hold* under *Requests on C2*, carries this text: "A failed
   revision that changes a forward-only tag (H1) or a snapshot role (D5) writes
   `DEPLOY_HOLD` and leaves `prod`. `prod` moves to T only when every commit in
   (`last-good`, T] that changes a forward-only pin maps to a PR whose head carries a
   successful `homelab/upgrade-gates` status. A commit with no PR fails that test, and a
   failed lookup ends the run as `error`. `prod` also stays when (`prod`, T] changes one
   forward-only pin in more than one commit, moves `k3s_longhorn_version` more than one
   minor, or carries a required gate status older than six hours. A tag that T drops counts
   as changed."
    - **The excused failures.** The pillar review fold narrowed the row's last part. C2's
      changed set no longer expects an unchanged name, so what remains is a dependent whose
      `flux/<name>` status on `prod`'s commit already read `failure`. C2's *Verification*
      states that rule. The timeout excuse for a dependent whose `dependsOn` chain reaches an
      excused name is dropped.
    - **The gates.** The row keeps "before H1.1b merges" for the hold and "before H1.1i
      merges" for the excused failures. C2's *Verification* text meets the second. Until the
      hold half lands, C2 resets `prod` on every failure, so H1.1b does not merge before
      contracts.md applies it.
    - **The hold's form.** H1's holds write T's SHA alone and never C2's `failed=` form, so
      the revert rule that C2 proposes never clears one. Each clears only by hand
      (**operator-only**).
    - **The no-hold value.** C2's *`DEPLOY_HOLD`'s values* and C6 read `none` as no hold,
      any other non-empty value as a hold, and an empty or missing value as an error.
      contracts.md applied that in its X2-01 entry, so H1 needs no row for it. The DR hold
      `dr-restore` of the bring-up order is such a non-empty value.
    - **Applied on 2026-10-16** for the hold, and the gate before H1.1b merges is clear. The
      excused failures were applied the same day, after H1.1b merged, so the gate before H1.1i
      merges is clear too.
4. **Where the service list lives.** C4 says H1 removes `containers_list` from the inventory
   once every entry is `owner: flux`, but the Pi's entries never become `owner: flux`.
   *Resolution:*
    - Amend C4's *Removal*: "H1.9 moves daniel-box's entries to `cluster/services.yml` and
      removes `containers_list` from daniel-box's and daniel-server's `host_vars`. H5 moves
      the Pi's entries into `compose_services`. H7 removes the Pi's `containers_list` key and
      states where `compose_services` lives after Ansible."
    - R4's ownership predicate reads every entry of `cluster/services.yml`.
    - H7 names that removal in its own steps, because its keep-list keeps
      `ansible/inventory/` whole.
5. **Secrets in a BackupTarget URL.** R2's rule 3 confines placeholders to `kind: Secret`, but
   each BackupTarget URL holds a bucket name. *Resolved by R2's Open questions 10:* R2
   accepted a `(key, kind)` frozenset, `PLACEHOLDER_KIND_EXEMPT`, with the members
   `(longhorn_b2_bucket, BackupTarget)` and `(r2_bucket, BackupTarget)`. The reason is that
   the live CR already shows them, and `docs/break-glass.md` already publishes the B2 one.
    - R2 makes H1 the owner. H1.7b adds the set, its rule pair and its named member to
      `secret_checks.py`, in the PR whose render first needs them.
    - No new index edge is needed. H1.7b waits on D3 and D3 on R2, so the file exists by
      then. Rule 3 is R2's own rule, not a contract row, so contracts.md needs no amendment.
    - D3 derives `homelab-cluster-vars`'s key set from the directory key sets, so it carries
      both keys without further work.
    - The rejected alternative was that the operator moves the two values into plaintext
      inventory. That needs their plaintext, so it would be **operator-only**, and H1.7b
      would wait on it.
6. **"Merging the pin is the upgrade."** The Renovate rule records that the operator drives
   each upgrade. *Resolution:* the operator rules on the new stance, **operator-only**, and
   H1.1g rewrites the three rule descriptions to match. **Ruled 2026-10-16: accepted.**
   Merging the pin is the upgrade. Automerge stays off for these rules, so the operator
   still chooses when each upgrade happens, at merge time rather than in a later hand step.
7. **The approval floor.** `ansible/roles/k8s/cluster-base/` holds the agent's read-only RBAC,
   and it now applies on merge. *Resolution:* whether that path joins the lander's approval
   floor in `claude-land-approval-paths.j2` is an **operator-only** decision, taken before
   H1.2. **Ruled 2026-10-16: it joins.** Before H1 the RBAC lives in
   `ansible/roles/setup/k3s/templates/readonly-rbac.yaml.j2`, which only `k3s-bringup.yml`
   applies, so an agent PR that widens it does nothing until the operator's next bring-up.
   Once Flux applies `cluster-base` on merge, the floor restores that human gate. The
   floor is a plain prefix, so the path joins it before the role exists.
8. **The design said "CoreDNS" with no image plan.** Moving CoreDNS to Flux replaces a host
   read with a pin that H6 moves together with `k3s_version`. *Resolution:* none needed beyond
   H6's PR text and the marker ordering above.
9. **Which status field carries the commit.** H1's exit reads `lastAppliedOriginRevision`,
   because `lastAppliedRevision` holds `prod@sha256:<digest>` for an OCI source (D1
   Baseline). *Resolution:* C6 names `lastAppliedOriginRevision` as the field that carries
   `master@sha1:<sha>`, as D1's *Open questions* 6 proposes.
10. **Retiring `k3s_manage_backup_targets`.** H1.7b deletes the flag rather than map it to a
    lasting suspend (*The backup plane and its levers*). *Resolution:* the operator confirms
    the retirement in H1.7b's review. If the operator keeps the mode instead, H1.7b depends
    on D5.1b and D5.1d, renders the flag through D5's `flux_suspend`, and accepts D5.1d's
    freeze, under which a disarm PR must also resume the tag. **Ruled 2026-10-16: retire.**
    H1.7b deletes the flag and needs no part of D5.
    - D5's text still has H1 render `flux_suspend` and H1.7b wait on D5.1b and D5.1d: its
      header, its bullet *D5.1b is the only definer of `flux_suspend`*, its *Open questions*
      13 and its X-13 entry. Under the default here, that edge does not exist, and a
      cross-slice note asks D5 to drop it. The edge returns only with the alternative above.
11. **ADR-0007 changes twice.** H1.7a moves the tier lists out of the file ADR-0007's
    *Governs* names and changes how a disarm reads on the monitor. H1.7b adds the
    break-glass case. *Resolution:* each PR carries its amendment, and the operator approves
    each one in that PR's review (**operator-only**). If the operator prefers one superseding
    ADR instead, it lands in H1.7a, and H1.7b amends it. **Ruled 2026-10-22: two
    amendments.** H1.7a carries its own amendment and the operator approves it in that PR's
    review. H1.7b amends ADR-0007 again, and there is no superseding ADR.
12. **A root-only suspend for bring-up.** Steps 4 to 11 of the bring-up order need the root
    `homelab` Kustomization suspended while the OCIRepository runs. D1's
    `flux_sync_suspend` suspends both. *Resolution:* no D1 variable. Step 4 runs D1's role
    with `flux_sync_suspend: true` and resumes the OCIRepository alone with D5's
    `homelab-operator` hand patch, and step 11 resumes the root with the same form.
    - From D5's flux PR on, D5's *Bootstrapping Flux on a rebuilt cluster* owns the order.
      The renderer then refuses `flux_sync_suspend: true`. A hand suspend sent after the
      root exists would race the root's first reconcile, so a cross-slice note proposes that
      D5's procedure applies the root last, after the restores, which needs no suspend.
    - The same note proposes rendering the hand-applied children at the commit `prod`
      names, for the reason step 7 gives.
    - A further note asks D5's procedure to carry step 7's DR hold: set `dr-restore` before
      its step 2 renders, unless its step 1's hold or a stray one already holds, and clear
      it in its step 9. D5's order applies the root last, but until then no Flux status
      reaches a promotion either, so a merge during the restores times out and resets
      `prod` there too.
    - R4's PR 7b drill and PR 8 cite D5's section rather than carrying an order.

## Review log

### Round 1 (per-slice adversarial review)

Raised: 3 critical, 13 high, 15 medium. All were fixed. None was refuted, because each claim
checked out against the tree at `70591c307`. One finding's mechanism was stated differently
from the tree, as noted under H1-R1-16.

- **H1-R1-01 (critical)** Promote's reset to `last-good` would downgrade Longhorn after a
  failed bump. *Fixed:* `longhorn`, `metallb`, `coredns` and `longhorn-backup` are
  forward-only. A failed revision that changes one, or whose `last-good` lacks it, holds
  without reset (H1.1b). CI refuses a pin decrease and a bump mixed with other changes
  (H1.1f), and `land.sh` merges a bump only while `prod` equals the master tip (H1.1g).
  Round c1 corrected these ids and replaced the land rule with one over (`last-good`, tip].
  The C2 amendment
  is Open question 3. Rollback names the restore-from-backup runbook, never a revert.
- **H1-R1-02 (critical)** H1.7 set a Longhorn setting that no longer exists. *Fixed:* a
  templated BackupTarget `default` declares only `backupTargetURL`, `credentialSecret` and
  `pollInterval`, with armed and disarmed render tests. The baseline now quotes
  `longhorn_backups.py:B2_TARGET` and the three patches.
- **H1-R1-03 (critical)** Dropping `owner` without re-deriving the flux-owned set could
  publish an empty bundle. *Fixed:* H1.9 rewrites every surviving `owner` reader in the same
  PR. The publish census gains a floor of four named members, with a red proof.
- **H1-R1-04 (high)** The B2 region came from a regex over a SOPS value. *Fixed:* the region
  becomes the plain variable `k3s_longhorn_b2_region`. It is not a secret, because
  `docs/break-glass.md` already publishes the B2 target URL, and the region assert's message
  already names that key. The URL holds only the bucket placeholder, the Secrets move to
  `stringData:`, and a test refuses unresolved Jinja. The R2 rule-3 gap for the bucket
  placeholder is Open question 5.
- **H1-R1-05 (high)** The disarm lever and `k3s_manage_backup_targets` were not carried over.
  *Fixed:* the RecurringJobs move with the targets, the manage flag renders `suspend: true`,
  `longhorn-backup` is forward-only, and an operator-only break-glass covers a hold. The
  contract text moves in H1.7b, and both arm flags have render tests.
- **H1-R1-06 (high)** Moved keys are still read by crons that stay. *Fixed:* the five shared
  keys go to `group_vars/all.yml`, cron-only keys stay, and each moving PR runs a per-key
  reader census. The baseline names the other readers.
- **H1-R1-07 (high)** `probe.py health` could not see vendored workloads. *Fixed:* H1.1b
  resolves vendored targets, with named members and a red case for a role that resolves
  nothing. The exit check requires `longhorn-manager` among the gated targets.
- **H1-R1-08 (high)** Prune would delete plumbing that a render drops. *Fixed:* the five
  plumbing tags render `prune: false`, and publish runs a per-tag object census with named
  members. One Kustomization field was chosen over per-object annotations; *Plumbing tags
  are never pruned* gives the reason.
- **H1-R1-09 (high)** The rollback contradicted `deletionPolicy` and relied on an override
  nothing provided. *Fixed:* `prune: false` lands in H1.1a, before any tag. A revert then
  orphans the objects both before and after D6, and Rollback says so.
- **H1-R1-10 (high)** Deleting `longhorn.yml` removed the host prerequisites. *Fixed:* the
  three host tasks move to `tasks/longhorn-host.yml` in H1.6, and the file tree shows it.
- **H1-R1-11 (high)** The RecurringJobs and the poll interval had gaps between PRs. *Fixed:*
  the RecurringJobs move in H1.7b with the targets. The poll-interval task moves into
  `longhorn-backup.yml` in H1.6 and dies with it in H1.7b. Every PR carries two-writer
  evidence.
- **H1-R1-12 (high)** The StorageClass mitigation relied on a stage cluster. *Fixed:* a
  read-only production measurement of `managedFields`, recorded in the baseline, shows that
  longhorn-manager writes the `longhorn` class. Flux owns only its ConfigMap and
  `longhorn-nobackup`, and a health check compares the two. The stage reference is gone.
- **H1-R1-13 (high)** Flux depends on the CoreDNS it owns. *Fixed:* CoreDNS stays in Flux,
  forward-only and unpruned, with an operator-only break-glass, a structural Corefile test
  and a resolution check. The bring-up order puts the `.skip` marker after k3s first starts.
- **H1-R1-14 (high)** No PR said how its `manual_plane` line is cleared. *Fixed:* *Steps*
  states the line, the narrowed apply, the `--applied` clear, and the range check that
  must come before a bare clear.
- **H1-R1-15 (high)** The merge gate did not run for interactive landings and could be
  bypassed. *Fixed:* the gate sits in `land.sh`'s merge path whatever the environment, runs
  only on daniel-box, and posts `homelab/upgrade-gates`. `promote` holds a pin change without
  that status. The residual bypass and the operator-only ruling are stated, and failure
  class 7 is named, along with what changes once H2 moves the drill.
- **H1-R1-16 (high)** The per-tag timeout had no provider. *Fixed:* H1.1a renders a role's
  `manifests_rollout_timeout` defaults key as its timeout, with a test, and H1.5a records a
  measured manager roll. The finding said the override exists at baseline. In the tree,
  sonarr, radarr, tdarr, valheim and prowlarr pass it as `include_role` `vars:` from a
  role key, so a Flux-born role needs a defaults key instead.
- **H1-R1-17 (medium)** The reconcile RBAC could not run the ported logic. *Fixed:* get, list
  and patch on Volumes, a ClusterRole list on PVCs, and get and patch on the four
  Deployments. The names come from the task's own loop, and a test covers every call.
- **H1-R1-18 (medium)** The reconcile test narrowed the reverse arm. *Fixed:* the arm reads
  live PVC classes, test case 4 is reworded, and a fifth red case returns a
  `longhorn`-class volume to `default`.
- **H1-R1-19 (medium)** Renovate would open stray CoreDNS PRs and group mismatched pins.
  *Fixed:* a packageRule disables `coredns_image`, and H1.1g splits the grouped rule into
  three. The annotation manager already covers k8s role defaults, so the moved pins stay
  tracked.
- **H1-R1-20 (medium)** The `$${` escape breaks a tag without `postBuild`. *Fixed:* the
  renderer skips the escape there, with a test, and Open question 2 amends C1.
- **H1-R1-21 (medium)** The claim that the first apply changes nothing was unproven. *Fixed:*
  an operator-only server-side diff precedes each flip. Runtime-written fields are patched
  out, the ConfigMap carries only `Corefile` and has a `NodeHosts` check, and the CoreDNS
  roll is planned.
- **H1-R1-22 (medium)** The census was textual and narrow, and the Goal overclaimed.
  *Fixed:* the census parses task YAML, joins `argv:`, and covers `set`, `scale`, `rollout
  restart` and the `kubernetes.core` writers. The Goal and the allowlist now name the same
  two files, and the crons are left to H2.
- **H1-R1-23 (medium)** The bring-up order was unstated. *Fixed:* a *Bring-up and DR order*
  section, a bounded wait in `kubeconfig.yml`, and the docs that change.
- **H1-R1-24 (medium)** Failure classes 2 and 7 were missing. *Fixed:* both are in Risks with
  their mitigations.
- **H1-R1-25 (medium)** Docs describing the retired hand applies were not listed. *Fixed:*
  each one is listed under the PR that retires it, with the `fact_status.py verify` step.
- **H1-R1-26 (medium)** H1.9's premise about `containers_list` readers was false. *Fixed:*
  H1.9 narrows `preflight.yml`, `_example.yml` keeps its empty list, and C4's removal is said
  to complete with H5.
- **H1-R1-27 (medium)** The header omitted D3 and R4. *Fixed:* the header names both, scoped
  to H1.7b and H1.9, and Open question 1 asks the index to let H2 wait on H1.8 only.
- **H1-R1-28 (medium)** The agent's RBAC moves to a plane that applies on merge. *Fixed:*
  Open question 7 is operator-only, and H1.2 updates the `agent.yml` comment.
- **H1-R1-29 (medium)** H1.5 was far over budget. *Fixed:* H1.5a carries the takeover that
  must move as one piece, and H1.5b carries the settings. H1.1 and H1.7 are split as well.
  PR numbers stay stable, because G5 cites H1.9.
- **H1-R1-30 (medium)** The availability check had no red proof. *Fixed:* an `_is_clean` and
  `_is_flagged` pair, with `default` as a named member.
- **H1-R1-31 (medium)** A stale checkout could re-apply an old Longhorn. *Fixed:* H1.1h adds a
  guard as the first task of `tasks/main.yml`, tagged `always`, so a narrowed apply reaches
  it. It is a `tasks/`-only change, so it does not park the deployer, whereas a pre_task in
  `k3s-bringup.yml` would have parked it. The limit is stated: a checkout older than the
  guard lacks it.

### Round 2 (integration review)

Raised against H1: X-03, X-06 and X-10 at high; X-12, X-13, X-19 and X-21 at medium. Every
H1 part is fixed. None was refuted. One fix takes a narrower edge than the finding asked for,
as noted under X-06. The parts that belong to other slices are cross-slice notes.

- **X-03 (high)** The exit read `lastAppliedRevision`, which holds the OCI digest revision.
  *Confirmed:* `gh api` on fluxcd/kustomize-controller at `v1.6.0`,
  `api/v1/kustomization_types.go`, documents `LastAppliedRevision` as the artifact revision
  and `LastAppliedOriginRevision` as the OCI `org.opencontainers.image.revision` value. D1's
  baseline quotes the same at v1.9.6. *Fixed:* the Goal and the exit read
  `lastAppliedOriginRevision`, and an ancestry check accepts a later promoted commit.
  *Open questions* 9 asks C6 to name the field.
- **X-06 (high)** Registry GC has two owners, and no edge orders H2 and D5. *Fixed, H1's
  part:* the header and *Open questions* 1 narrow H2's edge and state the cycle the narrowing
  avoids. The edge is H1.1a to H1.7b, not H1.1a to H1.8 as the finding proposed. H2's own
  *Open questions* 1 asks for "H1.1 to H1.7", and no H2 PR reads H1.8's exit proof. Both
  forms are acyclic. Which slice owns registry GC is for H2 and D5. *Superseded in round 10
  (H1-R10-05):* the index, H2's header and H2's *Open questions* 1 all read H1.1a to H1.8,
  and H1 adopts that form.
- **X-10 (high)** The arm and schedule keys moved to `group_vars/all.yml`, and nothing
  re-rendered the host cron that bakes them. *Confirmed:* at `70591c307`,
  `services_from_changed_paths(['ansible/inventory/group_vars/all.yml'])` returns
  `broad_deploy=True` with an empty `setup_roles`. `longhorn-backup-health.sh.j2` exports the
  four keys, and `health-crons.yml` bakes the fifth into `--retain`. *Fixed:* the first
  option. H1.7a makes the reader take the arm state from each BackupTarget's live URL and
  the run times from the live RecurringJobs, in two list calls that replace up to four.
  `b2-budget` reads the live retain. H1.7b moves all five keys into `longhorn-backup`'s
  defaults, and a census row with named members keeps any Flux-born key off the host plane.
  The red proof renders the script once and flips only the stubbed cluster. H2 keeps backup
  health on the host, which ruled out the second option.
- **X-12 (medium)** The `$${` rule was split three ways. *Fixed, H1's part:* one per-tag
  flag, computed once, drives D3's `postBuild`, the escape and the vendored-file refusal.
  `spec.postBuild` in `flux/<tag>.yaml` exposes it. *Open questions* 2 proposes the single C1
  wording and names that output as the source R2 and D3 read. The red proof flips the
  escape and `postBuild` together.
- **X-13 (medium)** `flux_suspend` was a lasting mode in H1 and a temporary stop in D5.
  *Fixed:* H1.7b retires `k3s_manage_backup_targets`. Its defaults comment ties it to a
  cluster without credentials, staging was retired on 2026-09-29, and no host overrides it.
  H1 sets no `flux_suspend`, and H1.1a no longer defines it, so D5.1 is its only definer.
  This resolves D5's *Open questions* 13 on the content side. *Open questions* 10 gives the
  alternative if the operator keeps the mode.
- **X-19 (medium)** Nobody removed the Pi's `containers_list`. *Fixed, H1's part:* H1.9 says
  it removes the key from daniel-box and daniel-server only. The wrong claim that H5 moves
  the Pi's entries into `hosts/daniel-pi/package.yml` is gone. *Open questions* 4 proposes
  the C4 wording that names H7.
- **X-21 (medium)** `fact_status.py verify` ran unconditionally. *Fixed:* both steps read
  "until G2 retires `docs/facts.lock`", which is R4's wording.

### Round 10 (per-slice adversarial review)

Raised: 4 high and 11 medium. All fifteen are fixed. H1-R10-04's central claim, that no
slice records the R2 exemption, is refuted below, and the stale H1 text it pointed at is
fixed. Each confirmation was measured at `70591c307` or from the upstream source named.

- **H1-R10-01 (high)** Fresh-cluster bring-up deadlocks, because Flux's controllers need the
  `homelab-critical` PriorityClass that only Flux applies. *Confirmed:* D1 sets the class
  through `tier_priority_class('flux')`, `priorityclass.yaml.j2` defines it, and H1.2 moved
  it into `cluster-base`. *Fixed:*
    - The bring-up order gains an **operator-only** step 2, before the Flux install. It
      renders `cluster-base` and applies it server-side under kustomize-controller's field
      manager. This matches R4's bootstrap step 1.
    - H1.2 writes the order into the three DR documents.
    - A red proof in `ansible/tests/k8s/` ties every `priorityClassName` in the `flux` render
      to a class in the `cluster-base` render.
- **H1-R10-02 (high)** The upgrade-gates check did not fit C2's newest-commit promotion, and
  "one bump per revision" was false. *Confirmed* against C2's `decide`. *Fixed:*
    - `decide` walks every commit in (`last-good`, T]. It maps each pin-changing commit to its
      PR through the commit-to-pulls lookup and requires the status on that PR's head.
    - A commit with no PR holds. A failed lookup prints `error`.
    - A red and green pair puts an unrelated commit after a gated bump and after an ungated
      one.
    - "One bump per revision" now reads "at most one unpromoted bump" in Design, Risks and
      Open question 3.
- **H1-R10-03 (high)** H1.7a read a blank live URL as a deliberate disarm, so a forgotten
  break-glass would silently stop backup monitoring. *Confirmed:*
  `longhorn_backup_health.py:_require_bool_env` names the wrong-default direction, and
  ADR-0007 requires a named suppression. *Fixed:*
    - From H1.7b, the reader reports any target that carries
      `kustomize.toolkit.fluxcd.io/reconcile=disabled`. The list call it already makes
      returns the annotation.
    - `probe.py landing` names the annotation on the five objects the two break-glass
      procedures annotate.
    - The red proof flags a blank, annotated target and keeps a blank, unannotated one clean
      with its `DISARMED` note. The Risks entry is rewritten.
    - Not taken: an intent annotation rendered from the arm flag. Flux restores a URL
      blanked by hand at its next reconcile unless the target is annotated, so the
      annotation alone marks every case that can persist.
- **H1-R10-04 (high)** H1.7b depended on an R2 rule-3 exemption that no slice recorded.
  *Refuted in its central claim.* `grep -n 'BackupTarget\|PLACEHOLDER_KIND_EXEMPT'
  docs/target-architecture/render/r2-secret-placeholders.md` returns matches:
    - rule 3's own text: "H1 adds the first two pairs, for its BackupTargets (Open question
      10)";
    - R2's *Open questions* 10, which accepts `PLACEHOLDER_KIND_EXEMPT` with
      `(longhorn_b2_bucket, BackupTarget)` and `(r2_bucket, BackupTarget)`, and says "H1.7b
      edits `secret_checks.py` and adds the set, the pair and the named member";
    - R2's review entry R2-10-4, which records the acceptance.

  H1.7b already waits on D3, and D3 on R2, so `secret_checks.py` exists in time. Rule 3 is
  R2's rule, not a contract row. So no index edge, gate or contracts.md amendment is needed.
  *Fixed on H1's side:* H1's text still treated the exemption as an open request. H1.7b now
  carries the set, its rule pair and its named member, Verification holds the pair, and
  Open question 5 cites R2's acceptance.
- **H1-R10-05 (medium)** H1 stated H2's edge as H1.1a to H1.7b and misquoted H2. *Fixed:* the
  header and Open question 1 read H1.1a to H1.8, as the index and H2 do. The X-06 entry
  carries a note that this round supersedes it.
- **H1-R10-06 (medium)** D5 still said that H1 renders `flux_suspend` and that H1.7b waits
  on D5.1b and D5.1d. *Confirmed* in D5's header, its definer bullet, Open question 13 and
  X-13. *Fixed:* Open question 10 records the stale text, and a cross-slice note asks D5 to
  drop it. The edge stays only under Open question 10's alternative.
- **H1-R10-07 (medium)** The read-only ServiceAccount keys had no valid home, and H1.2 omitted
  two tests. *Confirmed:* `render_context` layers only a role's own defaults, `all.yml` and
  host_vars. Both tests read the template in `setup/k3s`. *Fixed:*
    - A new placement class, a mirrored pair held equal by a test, covers
      `k3s_readonly_sa_name` and `k3s_readonly_sa_namespace`. `group_vars/all.yml` was
      rejected, because a change there leaves no owed line and is a broad deploy.
    - `k3s_readonly_crd_api_groups` moves to `cluster-base`, and the headlamp comment
      follows it.
    - The tier lists move in H1.7a, with their last host readers, and H1.6's ConfigMap calls
      the filters directly until then.
    - H1.2 lists both tests. `test_narrow_setup.py` keeps a real-tree named member.
    - Also corrected: the RecurringJob template names `k3s_longhorn_weekly_volumes` only in
      a comment.
- **H1-R10-08 (medium)** The CoreDNS "vendored excerpt" could not be byte-for-byte.
  *Confirmed:* k3s v1.37.1+k3s1 `manifests/coredns.yaml`, read from raw.githubusercontent.com,
  holds five `%{...}%` placeholders. *Fixed:* `coredns` is a templated role derived from that
  file, with no sha256 pin and no `vendor_manifest.py` refresh. The retargeted image test
  keeps a version-labelled excerpt, and the table and the file tree changed with it.
- **H1-R10-09 (medium)** The DR order aborted at `kubeconfig.yml` and lacked `sops-age`.
  *Confirmed:* `tasks/main.yml` imports `kubeconfig.yml` before `node.yml`,
  `static-address.yml` and `coredns.yml`. *Fixed:* the order is now eight steps. Step 1 runs
  with `--skip-tags kubeconfig`. Then come the hand apply of `cluster-base`,
  `--tags kubeconfig`, the Flux install, the **operator-only** `sops-age` from D3.0 and the
  reconcile. All of it goes into the three documents.
- **H1-R10-10 (medium)** The stale-checkout guard's predicate and placement could block the
  plan's own applies. *Fixed:*
    - The guard maps each writer task to the Flux inventory ids of its objects. It fails only
      when one of those ids sits in a Kustomization's `status.inventory`, which
      kustomize-controller v1.9.6 records after every apply.
    - It passes on two named absences, no `k3s` binary and no Kustomization resource type,
      and fails on any other read error.
    - New red cases cover the marker-only `coredns.yml` and the host-half `kubeconfig.yml`.
    - Rollback waits for NotFound on the child Kustomization before the owed apply.
    - Also corrected: H1-R1-31 said a `tasks/`-only change under `setup/k3s` leaves no owed
      line. `services_from_changed_paths(['ansible/roles/setup/k3s/tasks/main.yml'])` returns
      `setup_roles={'k3s'}` at `70591c307`. The Baseline and *The owed `manual_plane` line*
      now say that H1.1h leaves a line too.
- **H1-R10-11 (medium)** The edge from `longhorn-backup` to `longhorn` coupled the disarm to
  Longhorn's health. *Confirmed* by D4's dependency rule. *Fixed:* Design, the incident
  lever and Risks name the coupling and the break-glass as the lever in that state. Step 4
  waits on `longhorn-backup`'s `lastAppliedOriginRevision`. The edge stays, because it
  orders a new cluster's first apply after the CRDs exist.
- **H1-R10-12 (medium)** The object census named no member of `longhorn-backup` or
  `cluster-base`. *Fixed:* each of the five tags lists its named members, and two more red
  fixtures cover them. The text now says that only named members are detected.
- **H1-R10-13 (medium)** H1 contradicted ADR-0007 without listing it. *Confirmed* by its
  *Governs* section. *Fixed:* H1.7a and H1.7b each carry an **operator-only** amendment, and
  Open question 11 records the choice of a superseding ADR instead.
- **H1-R10-14 (medium)** The diff before the flip was meaningless for a tag that substitutes,
  and the PR body had no redaction rule. *Fixed:*
    - For `longhorn-backup`, the operator runs `flux diff kustomization` with `--path` and
      `--kustomization-file`. Both flags are confirmed in fluxcd/flux2 v2.9.6
      `cmd/flux/diff_kustomization.go`.
    - `internal/build/diff.go:diffSopsSecret` masks only SOPS ciphertext. So the run is
      **operator-only**, and the PR body quotes field paths and change kinds with no value
      from `secrets.yml`.
- **H1-R10-15 (medium)** H1.1b and H1.1c were far over the size guide. *Fixed:* there are now
  eight preparatory PRs, H1.1a to H1.1h, each with a line estimate, and all merge before
  H1.2. H1.1b keeps the promote work and its id, because contracts.md's C2 gate cites it.
  The H1.1x ids stay inside "H1.1a to H1.8", so H2's and H6's edges still cover them.

### Round c1 (per-slice adversarial review)

Raised: 3 high and 5 medium. All eight are fixed, and none is refuted. Each claim was checked
against the tree at `70591c307` or against contracts.md before the fix. Two fixes differ from
the finding's own proposal, as noted under H1-C1-01 and H1-C1-02.

- **H1-C1-01 (high)** The land rule that keeps a second Longhorn bump from merging before the
  first promotes was assigned to no PR and had no test, so two minor hops could apply as one
  skip. *Confirmed:* H1.1g's scope named only the gate run, the daniel-box refusal and the
  status post. `docs/longhorn-upgrade.md` says "Never skip a minor", and the header of
  `ansible/roles/setup/k3s/defaults/main.yml` says Longhorn "does not support arbitrary
  version skips". *Fixed:*
    - H1.1g's land rule refuses a forward-only pin bump while (`last-good`, master tip]
      already changes that pin, or while `DEPLOY_HOLD` is set. It reads `last-good` with
      `promote.py`'s reader, and a failed read refuses.
    - H1.1b's backstop holds when (`prod`, T] changes one pin in more than one commit, or
      moves Longhorn more than one minor.
    - The land rule reads `last-good`, not the finding's `prod`. A failed bump holds and
      leaves `prod` on it, so a land rule over (`prod`, tip] would admit a second bump during
      the hold. The backstop reads `prod`, so a deliberate fix-forward after a cleared hold
      is not held again. That fix-forward is **operator-only**.
    - Red and green cases cover the land rule and the backstop. The stale ids in H1-R1-01,
      H1-R1-19 and H1-R1-31 now read H1.1f, H1.1g and H1.1h.
- **H1-C1-02 (high)** A node outage fails every promotion once `longhorn` and `metallb` are
  Flux tags, and each failure writes `DEPLOY_HOLD`. *Confirmed:* C3 sets `wait: true` on
  every per-tag Kustomization, and C2's `verify` writes the hold on any `failure`. *Fixed*
  with a narrowed form of option (b), in a new PR, H1.1i. `verify` excuses a failure from a
  tag that T did not change and whose status on `prod`'s commit was already `failure`.
  It also excuses a timeout from an unchanged dependent of an excused tag, because
  `longhorn-backup` and `longhorn-reconcile` never apply while `longhorn` is not Ready.
    - Not taken: option (a), because C3 limits `healthCheckExprs` to a role's own custom
      resources.
    - Not taken: plain option (b), because a `cluster-vars` change can break a tag whose
      placeholder render did not change. A name with no digest therefore always counts.
    - Not taken: option (c) alone, which has no red proof.
    - Risks names the two cases that still hold and the wider exposure from H6 and D5.
      Open question 3 carries the C2 amendment.
- **H1-C1-03 (high)** The DR bring-up order let Flux deploy stateful workloads before any
  restore. *Confirmed:* step 4 of `docs/longhorn-disaster-recovery.md` reads "Restore
  volumes BEFORE any deploy", and C3's root applies every Kustomization the bundle
  publishes. *Fixed:* the order has twelve steps.
    - Flux installs with only the root suspended.
    - The operator hand-applies the plumbing Kustomizations, their `dependsOn` closure,
      `cluster-vars` and, from D5.1a1, `homelab-namespace`, under kustomize-controller's
      field manager.
    - The sync, `runbook_gates.py longhorn-dr` and the restores run. Then the root resumes.
    - From H1.7b, arming is a git change. Its hold is cleared at the end.
    - Each step that needs plaintext, `sudo` or `become` is marked.
    - The root-only switch is Open question 12, because D1's `flux_sync_suspend` suspends the
      OCIRepository too.
- **H1-C1-04 (medium)** Forward-only by tag digest turned every claim or tier change into a
  hold without reset. *Confirmed:* the reconcile ConfigMap rendered from `containers_list`
  inside `longhorn`. *Fixed:*
    - `longhorn-reconcile` is a sixth tag. It is not forward-only, it prunes by default, and
      it carries `depends_on: [longhorn]`.
    - H1.7a moves the tier lists into its defaults.
    - A red fixture adds a stateful entry and asserts that no forward-only digest moves.
- **H1-C1-05 (medium)** H1.1b could merge before H1.1g. *Fixed:* *Steps* fixes the order
  H1.1f, H1.1g, H1.1b, H1.1i. H1.1b also waits until no pin change from before H1.1g is
  unverified.
- **H1-C1-06 (medium)** The upgrade-gates status had no freshness bound. *Confirmed* against
  gates 2 and 3 in `docs/longhorn-upgrade.md`. *Fixed:*
    - `decide` refuses a status older than six hours on a commit in (`prod`, T]. A commit
      at or below `prod` is exempt, so a cleared hold on a failed bump is not held again.
    - `land.sh --regate` re-runs the gates for a merged PR and re-posts the status. The
      procedure that clears the hold runs it first.
    - A red and green pair covers the bound.
- **H1-C1-07 (medium)** Once H1.4 lands, nothing checks that k3s created CoreDNS before the
  marker. *Confirmed:* in `ansible/roles/setup/k3s/tasks/coredns.yml`, `Stop k3s reconciling
  its bundled CoreDNS addon` is the first task, and `Read the live Corefile` follows it.
  *Fixed:*
    - H1.4 keeps a bounded read of Deployment `coredns`, with `check_mode: false`, before
      the marker task.
    - A stub run fails before the marker exists.
    - The file tree, the guard's rationale and the guard's red case name both tasks.
- **H1-C1-08 (medium)** The forward-only predicate did not say what happens when a revision
  drops a forward-only tag. *Fixed:*
    - A tag in `last-good` that is absent at T counts as changed. A red case covers
      `metallb`.
    - Rollback waits until `last-good` reaches the revert. `cluster-base` is not
      forward-only, so a failed revert of H1.2 still resets.

### Round c2 (per-slice adversarial review)

Raised: 1 high and 4 medium. All five are fixed. One claim inside H1-C2-03 is refuted for one
file. Each claim was checked against the tree at `70591c307`, against upstream code through
`gh api`, or against read-only live reads on 2026-10-11, before the fix.

- **H1-C2-01 (high)** The diff before each flip cannot see what Flux's takeover removes, and
  H1.5a's own manager restart would create such a removal. *Confirmed:*
    - fluxcd/pkg `ssa/patch.go:matchFieldManager` matches by name prefix unless
      `ExactMatch` is set, and skips subresource entries. kustomize-controller's
      `internal/controller/kustomization_controller.go` lists `kubectl` (Apply and Update),
      `before-first-apply` and its own name.
    - fluxcd/pkg `ssa/manager_apply.go:Apply` runs its dry run before the cleanup, on every
      apply, so no dry run sees the removal.
    - Live: `longhorn-manager` has `kubectl-client-side-apply`, `kubectl-patch` and `k3s`,
      `coredns` has `deploy@daniel-box`, `kubectl-set` and `k3s`, and no plumbing pod
      template carries `restartedAt`.

  *Fixed:*
    - A new rule, *A takeover census before each flip*, and a new PR, H1.1j, that adds
      `scripts/dev/takeover_census.py`. It prints a `removed` list, a `kept` list and a
      `rolls` flag, and it checks `restartedAt` by name. It is **operator-only**.
    - The diff rule no longer claims to show every changing field.
    - H1.5a measures the roll with a pod delete, which writes no template field.
    - H1.3 must show that the vendored file declares MetalLB's `prometheus.io/*` template
      annotations.
    - H1.4's Design bullet names the `kept` fields of `deploy@daniel-box`.
    - A Risks entry cites D2's *The takeover rolls workloads once*. It states that the
      cleanup also reverts later hand writes and that a foreign manager keeps its fields.
    - Verification gains a pod-name read across each flip and the census's red proofs. The
      Baseline bullet on the takeover, the file tree, Rollback and the Complexity delta
      follow.
- **H1-C2-02 (medium)** The diff command had no `--force-conflicts`. *Confirmed:*
  `kubectl diff --help` prints `--force-conflicts=false`, and `coredns` carries
  `deploy@daniel-box`. *Fixed:* the command carries the flag. An exit other than 0 or 1,
  or a conflict error, is a stop and never an empty diff.
- **H1-C2-03 (medium)** The same-PR test lists missed tests that read the moved templates
  and keys. *Confirmed* by a grep for each moved basename and key. *Fixed:*
    - The test lists for H1.2, H1.3 and H1.7b name every reader.
    - H1.3 also moves the `k3s_metallb_pool` exemption in
      `ansible/tests/repo/test_lan_facts_have_one_source.py`, which the finding did not name.
    - A new rule makes every template move run `git grep -l <basename>` and list each
      reader it repoints.
    - *Refuted for one file:* `test_guard_tests_read_renders_not_templates.py` does not
      read `longhorn-recurringjob.yaml.j2`. Its
      `test_a_new_longhorn_guard_reading_a_role_template_is_named_as_an_offender` holds
      that path inside a source string that it feeds to the guard, so the move needs no
      change there. The same holds for the three `readonly-rbac.yaml.j2` fixture tests,
      which the plan now names as needing no change.
- **H1-C2-04 (medium)** The exit's "one patch release" does not exist upstream.
  *Confirmed:* `gh api repos/longhorn/longhorn/releases` lists `v1.13.0` and `v1.12.1` and
  no `v1.12.2`. *Fixed:* exit part 1 and H1.8 name one release step the land gate admits.
  That is the next patch if upstream has shipped one, or else the next minor, which today
  is `v1.13.0`. Which step H1.8 takes follows upstream's release timing.
- **H1-C2-05 (medium)** contracts.md's pending C2 row omits H1.1i's gate and most of the
  amendment text. *Confirmed:* the row names the hold, the gate status and "before H1.1b
  merges" only. *Fixed:* Open question 3 and H1.1i's step say that H1.1i waits on that
  amendment. A cross-slice note asks contracts.md to carry the full text with a second
  gate, "before H1.1i merges".

### Round c3 (per-slice adversarial review)

Raised: 1 high and 3 medium. All four are fixed, and none is refuted. Each claim was checked
against the tree at `70591c307`, against k3s's manifest at `v1.37.1+k3s1` through `gh api`,
or against a read-only live read on 2026-10-11, before the fix.

- **H1-C3-01 (high)** The six new entries carry no tier, so the node-down break-glass would
  scale CoreDNS to zero. *Confirmed:* `ansible/filter_plugins/service_tier.py:shed_entries`
  sheds every untiered entry, `scripts/lib/estate.py:Estate.k8s_entries` filters on
  `platform` only, and `docs/break-glass.md` restores a shed service with
  `deploy.sh --tags`. *Fixed:*
    - The exclusion route, not a tier. `NEVER_SHED` in `service_tier.py` names the five
      plumbing tags, and `shed_entries` drops them, so the probe and the dependency test
      read one answer. A test holds the set equal to the roles that set `flux_prune: false`.
    - H1.1d adds the mechanism with the set empty. Each plumbing PR adds its own tag. H1.2
      names `cluster-base` and H1.4 names `coredns` as real-tree members.
    - `longhorn-reconcile` stays shed-eligible, with nothing to scale.
    - H1.2 rewrites the shed scenario. A Flux-owned service is held by the reconcile-disabled
      annotation and restored by Flux's next reconcile, because all 59 Deployment and
      StatefulSet templates declare `replicas`.
    - Rejected: a `tier:`. `_CLASS_AT_DERIVATION` would force CoreDNS below
      `system-cluster-critical`.
    - *Risks* gains a failure-class-4 entry, and *Verification* a `probe.py shed-set` read.
- **H1-C3-02 (medium)** The templated `coredns` role trips the fleet guards, and the vendored
  files bypass them unseen. *Confirmed:* k3s's manifest sets `system-cluster-critical`,
  `revisionHistoryLimit: 0`, no `enableServiceLinks` and no `runAsNonRoot`.
  `ansible/tests/_k8s_render.py:rendered_docs` renders `templates/*.j2` only. *Fixed:*
    - *CoreDNS without a host read* gives each guard one answer: the shell macros, an
      **operator-visible** `_SYSTEM_TIER` row, `enableServiceLinks: false` and a measured
      uid decision. The pod template always changes, so H1.4 always lands in a window.
    - *Vendored upstream manifests* states that the verbatim files stay outside
      `rendered_docs()`, and why that is accepted. *Risks* names the bypass.
    - Found beyond the finding: `scripts/validate/refresh_vendored_schemas.py:VENDORED`
      holds only Traefik kinds, and `CORE_GROUP_FILES` holds no `scheduling.k8s.io` file.
      H1.2 adds that group, and H1.3, H1.5b and H1.7b each add their CRD kinds, or the
      `rendered-kinds-have-vendored-schemas` row fails their templated objects.
    - `longhorn-reconcile`'s CronJob meets the guards like any new role (H1.6).
- **H1-C3-03 (medium)** H1.8 as one PR fails H1.1f's bump-alone check. *Confirmed:* H1.1f
  admits only the role's files, its vendored file and docs, and the census allowlist is a
  test file. *Fixed:* H1.8a shrinks the census, and H1.8b lands the bump after it. Each has
  its own Rollback line, and *Verification* names which exit part follows which PR. "H1.1a
  to H1.8" covers both, so no dependency edge changes.
- **H1-C3-04 (medium)** `docs/longhorn-upgrade.md` stays wrong until H1.8, and it states an
  engine-upgrade limit of `0`. *Confirmed:*
  `ansible/roles/setup/k3s/defaults/main.yml:k3s_longhorn_concurrent_engine_upgrade` is `3`,
  the live Setting read `3`, and steps 1 and 3 name the file and the apply that H1.5a
  retires. *Fixed:* H1.1g updates the gate section, and H1.5a rewrites the per-hop
  procedure. H1.5b corrects step 4 and adds a render test that keeps the limit above 0.
  H1.8b records only what the exit proof measured.

### Round integration-2 (integration review)

Findings raised: X2-01 (high), X2-06 (high), X2-07 (high), X2-09 (medium), X2-11 (medium)
and X2-17 (medium). All six were checked against the tree at `70591c307` and the sibling
slices. None was refuted.

- **X2-01 (high)** `DEPLOY_HOLD`'s no-hold value is `none` in D1 and D2 but "empty" in H1.
  *Confirmed:* D1 *Design* reads "the value `none` is 'no hold'", and D2 reads "`DEPLOY_HOLD`
  has no empty value". H1's land rule refused while the variable was "not empty", which
  under `none` refuses every Longhorn and MetalLB bump, so the exit was unreachable.
  *Fixed:*
    - *Forward-only tags and the promote rule* gains *The hold's values*: `none` is no hold,
      empty or missing is an error, and every clear is the **operator-only**
      `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`.
    - The land rule (H1.1g), its step text, `decide`'s condition and the backstop text read
      "other than `none`". An empty or missing value refuses.
    - The disarm break-glass step 3 and bring-up step 12 name the clear command.
    - *Verification* reads "`DEPLOY_HOLD` reading `none`". The land-gate test refuses a SHA,
      an empty value and a missing one, and merges on `none`.
    - *Open questions* 3 states the value and points at D1's register row, gated before
      D1.3. A cross-slice note asks contracts.md for the row.
- **X2-06 (high)** H1's six entry-adding PRs trigger D1's unattended apply of 23 reader
  tags. *Confirmed:* `scripts/deploy_tools/narrow_lib/containers.py:changed_fields` returns
  None when `list(old) != list(new)`, and its docstring says an added entry "moves every
  reader that iterates the list". `grep -c` of H1 for `changed_fields` was 0. *Fixed, H1's
  part, with the fallback:*
    - *Rules for every PR from H1.2 on* gains *An added entry reaches every reader of the
      list until D6.12*. H1.2, H1.3, H1.4, H1.5a, H1.6 and H1.7b, and each revert of one,
      quote the `deploy_tags.py narrow` output, carry D1.2's stop rule before the PR and
      again before `land.sh`, name the moved renders from a `--dry-run`, and follow D1.2's
      hold exit.
    - The narrow output decides the case. A list naming only the PR's own tag means D2.1a or
      D4.1 has taken the preferred fix, and the stop rule falls away. A cross-slice note
      asks D2 or D4 for that fix.
    - H1.9 is outside the window, because it waits on R4, which waits on D6.
    - *Rollback* and *Risks* carry the same rule. *Complexity delta* names it.
- **X2-07 (high)** Three incompatible post-H1 bring-up procedures. *Confirmed:* H1's
  steps 4 and 11 needed a D1 variable that D1 does not have. D5's flux PR then makes the
  renderer refuse `flux_sync_suspend: true`, and D5 rendered at `last-good` where H1
  rendered at `prod`. *Fixed, H1's part:*
    - *Bring-up and DR order after H1* states its window: from H1.2 until D5's flux PR. From
      then on, D5's *Bootstrapping Flux on a rebuilt cluster* is the single owner.
    - Step 4 runs D1's role with `flux_sync_suspend: true`, then resumes the OCIRepository
      alone with D5's `homelab-operator` hand patch. Step 11 resumes the root with the same
      form. No D1 variable is needed, and nothing runs the `flux` role between the two.
    - Step 7 renders at the commit `prod` names and says why: the OCIRepository serves
      `prod`, and a mismatch on a forward-only tag is a version change right after the
      restores.
    - *Which PR edits each runbook* names H1.2 and the later H1 PRs for the DR document,
      `docs/break-glass.md` and `storage_smoke.yml`'s header. It names D5's flux PR for the
      pointer to its section and for `docs/k3s-etcd-restore.md`, and no R4 PR.
    - *Open questions* 12, *Risks* and *Complexity delta* follow. Cross-slice notes ask D5
      to carry the order, apply the root last and render at `prod`, and ask R4 to cite D5's
      section.
- **X2-09 (medium)** Substitution was decided per tag in H1 but per directory in D3 and R2.
  *Confirmed:* D3 *Which Kustomizations substitute (D3.3b)* decides "in the render of its
  directory, `cluster/<name>/`", and R2's *Open questions* 5 names H1's per-tag flag as
  needing the same change. *Fixed:*
    - *One key set per directory decides substitution* replaces the per-tag flag. `<tag>`
      and `<tag>-deferred` are decided separately, the in-process key set is the source and
      `spec.postBuild` is its projection.
    - The vendored refusal, the escape, H1.1a, the two renderer tests and *Complexity
      delta* use the same unit. A new fixture places the placeholder in `<tag>-deferred`
      only.
    - *Open questions* 2 adopts R2's wording in place of H1's earlier C1 proposal. A
      cross-slice note asks contracts.md to record R2's four rows and reopen X-12.
- **X2-11 (medium)** The register and the index lack rows and sub-edges that slices gate
  on. *Fixed, H1's part:* *Open questions* 3 says the register lacks the C2 row for the
  excused failures, gated before H1.1i. Cross-slice notes list H1's rows and the D5 sub-edge
  on H1.1a for contracts.md.
- **X2-17 (medium)** H1.7b moved the R2 BackupTarget template without
  `k3s_longhorn_r2_region`. *Confirmed:* at `70591c307`, `git grep k3s_longhorn_r2_region`
  finds the `setup/k3s` defaults, `longhorn-backuptarget-r2.yaml.j2` and
  `etcd-snapshot-offbox.sh.j2`. *Fixed:*
    - *Where the Longhorn variables go* makes it the second mirrored pair. H1.7b defines
      `k3s_longhorn_r2_region` in `longhorn-backup`'s defaults and adds the twin
      `k3s_etcd_s3_region` to `setup/k3s`'s defaults, the name H2 already uses. It repoints
      the off-box script, and a test holds the two equal.
    - The H1.7b step, *File tree*, *Tests* and *Complexity delta* follow. H2.8a's gate is
      met, and H2.8b deletes the twin (cross-slice note).

### Round i1 (per-slice adversarial review)

Findings raised: H1-I1-01 (high), H1-I1-02 (medium), H1-I1-03 (medium), H1-I1-04 (medium)
and H1-I1-05 (medium). Each claim was checked against the tree at `70591c307`, contracts.md
and the sibling slices. All five were fixed, H1-I1-05 by a route the finding did not name.
None was refuted.

- **H1-I1-01 (high)** `prune: false` on `cluster-base`, `coredns` and `longhorn-backup` is
  outside C3. *Confirmed:* C3's *Per-role fields* reads "`prune: false`, for H1's vendored
  CRDs", and the only pending row, *`prune: false` beyond vendored CRDs*, widens it to tags
  whose render holds a CRD, gated before traefik's flip. H1's own table puts no vendored
  file in those three tags. *Fixed:*
    - *Open questions* 2 asks for "`prune: false`, for a plumbing tag (H1) or a tag whose
      render holds a CustomResourceDefinition (D5)", gated before H1.1a merges. It drops
      the request for `timeout`, which C3 already names.
    - *Plumbing tags are never pruned* states the gap and points at the request.
    - A cross-slice note asks contracts.md to replace D5's row with one merged row under
      H1.1a's gate, which comes before traefik's flip. A second note asks D5 to quote it.
- **H1-I1-02 (medium)** The X2-06 fallback cited a fix D2 refuted. *Confirmed:* D2's
  *An added or removed entry that is not Ansible-owned (D2.1c)* reads "A bare Flux-born
  entry therefore applies five reader tags" and calls the own-tag outcome "not sound".
  `narrow_lib/containers.py:changed_fields` does return None for an added entry at
  `70591c307`, so the baseline stands. *Fixed:*
    - *Rules for every PR from H1.2 on* keeps 23 as the baseline, then names D2.1c's five
      readers. It shows that H1's entries carry no `FILTER_FIELDS` field, because no row of
      `narrow_lib/containers.py:FILTER_FIELDS` names `owner`, `platform` or `depends_on`.
      It names pihole, uptime-kuma and monitor-bridge as the `k8s_autodeploy: false`
      readers, as their defaults read. (Corrected on 2026-10-15, #4975: 8e79e95d6 moved
      the five readers onto named filters, and `lan_hostnames` reads `platform`. The rule
      now names artifacts, blackbox-exporter, pihole and uptime-kuma, as D2 Open question
      10 does, with blackbox-exporter, pihole and uptime-kuma denylisted.)
    - The "own tag only" branch, its D2.1a or D4.1 citation and its cross-slice note are
      gone. *Risks* and *Complexity delta* follow, and the cost now ends at D6.12.
- **H1-I1-03 (medium)** Bring-up step 7's "A hold comes first" contradicted steps 6 and 12.
  *Confirmed* against D5's bootstrap step 2, which scopes the rule to "a hold that step 1
  did not leave". *Fixed:* step 7's first sub-step reads "a value other than `none` that
  step 6 did not leave", and every hold present from step 7 on stays until step 12. Step 7
  reads `prod`'s commit from the `org.opencontainers.image.revision` key in GHCR, as D5
  does. Step 6 clears a hold left after the cluster was lost, or during steps 1 to 5,
  before its arm PR, because a hold would stop that promotion. `reset_to_last_good` is offered only from D5.2b2, the PR that
  adds it.
- **H1-I1-04 (medium)** Nothing stopped promotions while the root was suspended.
  *Confirmed* against C2's *On failure*: a merge during the restores would promote, time
  out, hold and reset `prod` to `last-good`. *Fixed:*
    - Step 7 waits until no `promote` run is queued or in progress, then sets the
      **operator-only** DR hold `dr-restore`, unless step 6's hold or a stray one already
      holds. C2 reads any non-empty value other than `none` as a hold, so `decide` moves
      nothing.
    - Step 12 clears whichever hold steps 6 and 7 left once every status on `prod`'s
      commit reads `success`.
    - *Risks* gains the merge-during-DR bullet, and *Complexity delta* names the DR hold.
      *Open questions* 12 and a cross-slice note ask D5 to carry the same hold.
- **H1-I1-05 (medium)** The `NEVER_SHED` equality test fails on D5's `flux_prune: false`
  for traefik and `flux`. *Confirmed:* D5 sets `flux_prune: false` on both, and nothing
  orders that against H1.1d. *Fixed by narrowing the test, not by a new marker:* the
  traefik entry in `ansible/inventory/host_vars/daniel-box.yml` declares
  `tier: home-edge`, and D1 gives the `flux` entry `tier: platform`. `shed_entries` already
  keeps every tiered entry. The test therefore compares `NEVER_SHED` with the untiered k8s
  entries whose role sets `flux_prune: false`. Neither D5 PR needs a shed ruling, and
  *Verification* gains a tiered fixture that passes. No other slice plans an untiered
  `flux_prune` setter: `grep -l flux_prune` over the other slices finds contracts.md, D5,
  D6 and H2, and D6 and H2 only cite H1's five tags.
- **Incidental fixes.** *The hold's values* and *Open questions* 3 no longer say that C2
  and C6 read "empty", or that the register lacks the excused-failure row. contracts.md's
  C2 reads `none`, and its *Requests on C2* row carries both gates.

### Round f0 (per-slice adversarial review)

Findings raised: H1-I2-01 (high) and H1-I2-02 (high). Both were checked against the tree at
`70591c307` and confirmed. Both were fixed. None was refuted.

- **H1-I2-01 (high)** From H1.5a until H1.7b, bring-up step 1 fails, and nothing creates the
  backup targets. *Confirmed:*
    - In `ansible/roles/setup/k3s/tasks/longhorn.yml`, `Apply the Longhorn metrics
      NetworkPolicy`, `Wait for the Longhorn driver deployer`, the CSI sidecar patches and
      the `settings.longhorn.io` patches all carry `tags: [longhorn]` and no `kubeconfig`
      tag. So `--skip-tags kubeconfig` runs them with no `longhorn-system` namespace once
      H1.5a deletes `Install Longhorn`.
    - In `tasks/longhorn-backup.yml`, `Reconcile the Longhorn backup credential` and the
      target patches carry `longhorn_backup`. Before H1.7b no Flux tag creates the targets.
    - `Install Longhorn prerequisites` and the multipathd pair carry `tags: [longhorn]`, and
      `ansible/tests/deploy/test_dual_tagged_producers.py`'s docstring records that an
      exclusion matches any tag. So skipping `longhorn` alone would drop open-iscsi.

  *Fixed:*
    - H1.5a moves the three host tasks into `tasks/longhorn-host.yml` under `longhorn_host`,
      rather than H1.6, and a test with three named members keeps both skipped tags off them.
    - *The Longhorn window, H1.5a until H1.7b* gives step 1's variant,
      `--skip-tags kubeconfig,longhorn,longhorn_backup`. It also adds the **operator-only**
      step 7a: `--tags longhorn,longhorn_backup` with the two arm variables, after
      `longhorn` reads `Ready` and before step 8, from a checkout at `prod`'s commit.
    - The guard passes there, because each PR deletes a row in the commit that hands its
      objects to Flux. One command pair covers the window: H1.5a writes it, H1.5b and H1.6
      rewrite what step 7a applies, and H1.7b removes it.
    - The "Before H1.7b, step 1 creates the backup targets" sentence now says "Before
      H1.5a", and names step 7a and, from H1.7b, step 7.
    - *Risks*, *Tests and docs that change in the same PR* and *Complexity delta* follow.
- **H1-I2-02 (high)** The stale-checkout guard blocked the k3s rollback while the API was
  down. *Confirmed:*
    - `docs/k3s-upgrade.md`'s *Rollback* re-runs the `--tags k3s_server` apply.
    - The guard sat first in `tasks/main.yml` with `tags: [always]`, and it failed on any
      read error.
    - The `Load encrypted secrets and set the become password (shared preamble)` pre_task in
      `ansible/k3s-bringup.yml` carries `tags: always`, so `--skip-tags always` loses become.

  *Fixed:*
    - The guard moves directly after the `server.yml` import. `server.yml`'s
      `Wait for the node to report Ready` then brings the API up before the guard reads it.
      No `server.yml` task writes an object a Flux tag declares: its kubectl calls are
      `wait` and `get`, and `Rotate the encryption key and re-encrypt Secrets already stored
      in etcd` runs `k3s secrets-encrypt rotate-keys`.
    - The read retries 12 times, 10 seconds apart. Its `until` accepts the "no Kustomization
      type" answer at once, and it still fails closed when the tries run out.
    - The "no `k3s` binary" pass now covers a `--check` run or a narrowed apply that skips
      `k3s_server` on a fresh host.
    - A placement test, with `server.yml`'s import as its named member, fails a guard
      moved first and a `server.yml` that gains a writer. A retry stub pair joins the guard
      cases. The rollback in `docs/k3s-upgrade.md` needs no change. An escape variable was
      not added, because the placement makes it unnecessary. *Round f1 found this last claim
      false for host-only tags such as `node-address` (H1-F1-01), and replaced the `always`
      tag with per-tag-set instances.*
- **Checked and not affected.** The window's skip set also drops `Derive the B2 region from
  the Kopia endpoint`, which sets `k3s_longhorn_b2_region`. At `70591c307` its only readers
  are tasks in `tasks/longhorn-backup.yml` and `templates/longhorn-b2-secret.yaml.j2`, which
  that file renders, so step 1 reads no undefined variable.
- **Incidental fixes.** The guard's *The limit* bullet names the trade of the new
  placement: a stale checkout reaches `server.yml` before the guard. *Rollback* says that a
  revert of a window PR reverts its runbook edit. Three sentences that put "key" directly
  before an inline-code name now say "variable" or "data entry".

### Round f1 (per-slice adversarial review)

Findings raised: H1-F1-01 (high), H1-F1-02 (medium) and H1-F1-03 (medium). Each was checked
against the tree at `70591c307` and confirmed. All three were fixed. None was refuted. Two
parts of H1-F1-02's required fix were not taken, because the tree contradicts them; the
entry below gives the evidence.

- **H1-F1-01 (high)** Host-only applies failed at the guard while the API was down.
  *Confirmed:*
    - `ansible/roles/setup/k3s/tasks/main.yml` imports `server.yml` first and
      `static-address.yml` eighth. All 24 `tags:` lines in `server.yml` read `[k3s_server]`,
      so `--tags node-address` runs no `server.yml` task.
    - An `always` guard after `server.yml` therefore read a refusing API under the remedy
      that `docs/k3s-node-plane-crons-and-incidents.md` gives for a wrong address pin, and
      stopped the play before `static-address.yml`.

  *Fixed:*
    - The guard is no longer tagged `always`. `tasks/stale-checkout-guard.yml` imports
      the untagged `tasks/stale-checkout-check.yml` once per distinct effective tag set of
      the mapped writers, each import carrying that set and its rows. `main.yml` imports the
      guard file directly after `server.yml`.
    - The finding's option 1, one guard tagged with the union of the writers' tags, was
      rejected. `--skip-tags kubeconfig`, which is bring-up step 1, would skip that guard
      while the `metallb`, `priorityclass` and `longhorn` writers still ran. Option 2, a
      `when:` over `ansible_run_tags`, was rejected because it reimplements Ansible's
      selection in Jinja.
    - *Verification* gains the two selection cases the finding asks for, plus the
      `--skip-tags kubeconfig` case that tells per-set instances from a union tag. A
      tag-parity test holds each row in the instance whose tags equal its task's effective
      tags.
    - *Why after `server.yml`* now rests on the whole-role run alone. The `k3s_server`
      rollback selects no instance.
    - The f0 sentence about the escape variable carries a correction note. *Risks* gains
      *A host-only apply with the API down*. *Verification*, *Complexity delta*, the file
      tree and the bring-up order's guard sentence follow.
- **H1-F1-02 (medium)** The owed-line procedure assumed narrowed tags. *Confirmed, and wider
  than raised:*
    - `narrow_setup.py:role_tags` builds `RoleIndex` at the new ref. `RoleIndex.tags_of`
      refuses `tasks/main.yml`, whose imports carry no tags (`task_walk.py:file_tags`), and a
      deleted task file.
    - `RoleIndex.readers_of` also refuses a moved template that no surviving task names, and
      `RoleIndex.key_readers` refuses a moved variable that no surviving task reads. Every
      PR from H1.1h to H1.7b makes at least one of these four changes.
    - `land_tags.py:own_narrow_tags` then leaves the role tag, and the line's tag set is
      empty.

  *Not taken, with evidence:*
    - Moving the guard map out of `main.yml` does not restore narrowing, because the
      template and variable refusals remain. The map still moved, into
      `stale-checkout-guard.yml`, for H1-F1-01.
    - The clear cannot use `--applied`. `gitops_state.py:clear_manual_plane` says that "a
      narrowed apply against an empty or missing row keeps the line", and
      `deploy_state.py:clear_manual_plane_tags_applied` returns `{role}` for that case.

  *Fixed:*
    - *The owed `manual_plane` line* names the four refusal shapes and the PRs that hit
      each. It says that `land.sh` prints `--tags k3s` and that the operator does not run it.
    - The landing branches on what `land.sh` prints, because the refusals are a reading at
      planning time. Narrowed tags take the `--applied` clear.
    - Otherwise each PR body lists the hand tags from the surviving task files, templates,
      files and read variables the PR edits, and quotes a `--list-tasks` run. The section
      lists them for all nine PRs. H1.7b's list carries `etcd-snapshot` for the repointed
      off-box script, unless its render is byte-identical.
    - The clear is then the bare form, after the range check.
    - The preparatory `narrow_setup` change was rejected: four fail-closed refusals to widen,
      each with its own red proof, for a migration that ends at H1.7b.
    - *Rollback* applies the same procedure to a revert. *Complexity delta* counts the nine
      hand lists.
- **H1-F1-03 (medium)** Tests that load deleted task files or tasks were missing from the
  same-PR lists. *Confirmed:* every test the finding names loads the file or task it names.
  The census, run over `ansible/tests`, `ansible/roles/setup/k3s/tests`,
  `scripts/deploy_tools/tests` and `scripts/tests`, found five more readers:
    - `test_retried_commands_survive_check_mode.py:NON_RETRIED_READS_THAT_MUST_OPT_OUT`
      names `Read the live Corefile`, and the same module names the DNS probe task (H1.4);
    - `test_k3s_stage_gates.py:test_the_coredns_probe_name_defaults_to_a_real_name` guards
      the probe that H1.4 deletes;
    - `test_narrow_setup.py:test_the_real_k3s_roles_untagged_task_files_are_named_as_such`
      names `longhorn-weekly-shard.yml` (H1.6), and gains the new check file (H1.1h);
    - `test_narrow_setup_task_file_kinds.py:test_the_real_k3s_weekly_shard_file_narrows_to_longhorn`
      loads `longhorn-weekly-shard.yml` (H1.6);
    - `test_every_longhorn_pvc_has_a_tier.py` names a line range of `longhorn.yml` in prose
      (H1.6).

  *Fixed:*
    - The rule *Every reader of a moved template, a moved key, a deleted task file or a
      deleted task* adds the basename grep and the task-name grep.
    - *Tests and docs that change in the same PR* lists each reader under the PR that breaks
      it, with its replacement anchor. The CoreDNS anchor takes an inline fixture.
      `_KNOWN_TASK_FILES` swaps in a surviving file only after `_offenders` reads its
      consumer. The weekly-shard narrowing case moves to a fixture tree.
    - `test_longhorn_group_labels.py` moves under H1.6 and H1.7b, and the H1.7a entry no
      longer claims it.

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review round covers it.

- **F-01 (the expected set).** C2's `verify` expects T's changed set, and the H1 request
  row's excuse covers only a dependent that was already failing.
    - *Design*, *Forward-only tags and the promote rule*: the H1.1i bullet is rewritten
      around the changed set. The timeout excuse is gone, and the forward-only predicate
      reads the `footprint` digest. A bullet, *The hold's form*, says that H1's holds write
      T's SHA alone, never C2's `failed=` form, and clear only by hand. The stale clause on
      adding `digests.json` before D5.1 is replaced: D2's first publishing PR adds it (C1).
    - *Steps*: H1.1b drops `digests.json` and gains the hold's form, about 230 to about 220
      lines. H1.1i is renamed and shrinks from about 130 to about 70 lines, and C2's text
      meets its gate. The order note names the shared `verify` path.
    - *Verification*: a hold-form case for H1.1b. H1.1i's cases are rewritten on the
      `cluster-base` dependency edge: the timeout case holds, and an outage regression case
      joins them.
    - *Risks*: the node-outage entry names the three cases that still hold, among them a
      transitive dependent with no recorded status.
    - *Complexity delta*: the `verify` item is narrowed. No count changes.
    - *Open questions* 3 quotes contracts.md's row as it stands and records the narrowing
      and the hold's form.
