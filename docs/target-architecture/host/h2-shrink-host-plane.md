# H2 — Shrink the host plane (crons to CronJobs)

Status: IN PROGRESS

PR H2.1, the censuses: [#5335](https://github.com/DanielH2018/server/pull/5335). It merged at
`7d9901058` and deployed nothing. It adds the flux-ownership, retired-cron and reaper-CronJob
census rows, the RBAC-against-script test and the trim-label fixtures, and none of the six roles
carries `owner: flux`.
PR H2.3a, the drill's readers: [#5349](https://github.com/DanielH2018/server/pull/5349). It merged
at `252cd470c` and owed `manual_plane k3s`. `narrow_setup.py` mapped the shared `longhorn_lib`
change to six health-cron tags, and the operator applied all six with no gated control-plane
task selected: `ok=115 changed=33 failed=0`. Nothing writes the stamp ConfigMap yet, so the
readers take their host-directory fallback
([readings](https://github.com/DanielH2018/server/pull/5349#issuecomment-6226115250)).
PR H2.7, the two Claude crons re-homed: [#5387](https://github.com/DanielH2018/server/pull/5387),
together with D5's run-once cleanup PR. It merged at `471397804`. daniel-box's tick re-rendered
both scripts at 01:41 UTC, and the peer sync cron rewrote its lock at 01:45:01, the first slot
after. On daniel-server, `initial_setup.yml --tags claude_code` read `ok=123 changed=4
failed=0`. The node gate kept both crons off that host, and `Restart claude-rc` did not run.
PR H2.4b, crowdsec's three API jobs, worked under #5649. The operator ruled on
2026-10-18 that crowdsec's jobs run in a sidecar of the LAPI pod (*Open questions* 2). The
remote allowlist, the AppSec verify and the bouncer prune run in the `crowdsec-jobs` sidecar,
and their three root crons become absent arms. No `pods/exec` grant is made.
H2.4c, the home allowlist, waits on #5701. The sidecar shares the pod's IPv4-only network, so it
cannot see the home IPv6 /64 the job keeps allowlisted, and the operator picks its form.
Depends on: H1, D3 and H6, per PR. H2.2a needs H1.5a and H1.6. H2.3b needs H1.5a and the C3
row of *Open questions* 12. H2.8a needs H1.7b (and through it D3) and H6.4b, with the
region-key gate of *Open questions* 11. No other H2 PR waits outside H2 (*Open questions*
1).
Unblocks: H7, and D5 wave D for six roles: crowdsec, configarr, janitorr, qbittorrent,
artifacts and observability

daniel-box runs about a dozen crons that read or act on the cluster. Most live on the host only
because the role that wrote them was Ansible. This slice moves those into the cluster, one
family per PR, so the host plane that H7 packages is smaller. Two get native replacements:
Longhorn's `filesystem-trim` RecurringJob and k3s's own etcd-s3 snapshots. Four stay on the
host by decision, because their state or their evidence is on the host. Registry garbage
collection does not move: D5 retires it. The reapers stay off any schedule.

## Goal and exit criterion

Afterwards, these host crons are gone from daniel-box and have in-cluster replacements:

- the Longhorn filesystem trim, replaced by a native RecurringJob;
- the Longhorn restore drill, with its state in a ConfigMap;
- the four CrowdSec crons;
- the configarr, janitorr and qbittorrent checks;
- the off-box etcd snapshot upload, replaced by k3s's native etcd-s3 schedule.

Registry garbage collection is not on this list. D5 retires it while registry is still
Ansible-owned (Design, *Registry GC is D5's to retire*).

The two Claude crons that the `artifacts` and `observability` roles install move to the
`claude_code` setup role, on daniel-box only.

These stay on daniel-box, and H7 packages them:

- `Longhorn backup health`. It reads the cluster through the read-only kubeconfig already, and
  its B2 evidence arm reads the host journal of the B2 pair below.
- `B2 deletion accounting` and `B2 backup budget listing`. Their ledger lives in the operator's
  home, and `probe.py b2-spend` reads it there.
- The cluster-token drift comparison, cut out of the off-box snapshot script into its own cron.
- `daniel-box disk health` (M3 converts it), `etcd restore drill` and `Release staleness drift
  check` (D6 retires it), unchanged.

The exit criterion has three parts:

1. **Operator-only:** `sudo ls /etc/cron.d/` on daniel-box lists none of these files:
   `longhorn-trim`, `longhorn-restore-drill`, `crowdsec-home-allowlist`,
   `crowdsec-remote-allowlist`, `crowdsec-bouncer-prune`, `crowdsec-appsec-verify`,
   `configarr-health`, `janitorr-health`, `qbittorrent-prefs-check` and
   `etcd-snapshot-offbox`. `/etc/cron.d` is `drwx------ root` on daniel-box (measured), so an
   agent cannot list it. The agent-side half is `kubectl get cronjobs -A`, which lists each
   replacement with a `lastSuccessfulTime` inside its own schedule.
2. The newest `ETCDSnapshotFile` with an S3 location was created after the H2.8b hand apply and
   is younger than 14 hours. Backup health's check 12 reads it as UP.
3. The flux-ownership census passes. The retired-cron census finds an absent arm for every
   cron listed in part 1 and for every host file this slice retires, either in the role that
   installed it or in that role's task file in `k8s_host_files`, where D5's conversion PR moves
   it.

## Baseline at `70591c307`

- **The k3s role's crons.** `ansible/roles/setup/k3s/tasks/health-crons.yml` installs nine
  crons and two timers, all as `cron_file:` entries on daniel-box. The role `CLAUDE.md` glance
  block lists them. Each cron's user and tag:
    - `Longhorn backup health` (`*/10`), as `sys_user`, tag `backup-health`;
    - `Longhorn filesystem trim` (`10 6 * * *`), as `sys_user`, tag `backup-health`;
    - `B2 deletion accounting` (`10 7`) and `B2 backup budget listing` (`20 7`), as
      `sys_user`, gated `when: has_repo_checkout`, tag `backup-health`;
    - `Longhorn restore drill` (`10 4`), as root, tag `backup-health`. Its task reads the
      schedule as `k3s_longhorn_restore_drill_cron.split()[0]` and the next two fields;
    - `Off-box etcd snapshot` (`45 2`) and `etcd restore drill` (weekly), as root, tag
      `etcd-snapshot`;
    - `daniel-box disk health` and `Release staleness drift check`, which this slice leaves
      alone.
  `ansible/k3s-bringup.yml` applies the role. It is one of the `_BROAD_MANUAL_PREFIXES`
  playbooks, so every change to it is a hand apply.
- **A `setup/k3s` hand apply decrypts, and its owed line has a set clear.** Each play in
  `ansible/k3s-bringup.yml` imports `pre_tasks/load_secrets.yml`, which loads the SOPS secrets
  and the become password. C8 therefore makes every hand apply **operator-only**. H1, *The
  owed `manual_plane` line*, gives the four-step clear: `land.sh` prints the PR's narrowed
  tags, the operator applies them, `gitops_state.py clear-owed manual_plane k3s --applied
  <tags>` drops only those tags, and the bare clear is used only after the ledger line's range
  shows no other commit under `ansible/roles/setup/k3s/`. `scripts/deploy_tools/gitops_state.py:clear_owed`
  routes a `manual_plane` clear through `clear_manual_plane` with the `applied` tags.
- **The render stamp lists two scripts this slice retires.**
  `ansible/roles/setup/k3s/defaults/main.yml:k3s_render_stamp_groups` puts
  `longhorn-restore-drill.sh.j2` and `longhorn-trim-volumes.sh.j2` in the `k3s-backup-health`
  group, which the task `Stamp the backup-plane scripts this host has rendered` records for the
  setup-drift check.
- **What backup health checks, and how.** `ansible/roles/setup/k3s/files/longhorn_backup_health.py`
  runs eleven checks today:
    - checks 1 to 8 read Longhorn CRs, and checks 7 and 8 also read the drill's state
      directory through `DRILL_STAMP_DIR = _require_env("LONGHORN_RESTORE_DRILL_STAMP_DIR")`;
    - checks 9 and 10 (`longhorn_lib/longhorn_cron_evidence_logic.py`) read the journal lines
      of the `longhorn-trim` and `b2-deletions` tags, the cron files `LONGHORN_TRIM_CRON_FILE`
      and `LONGHORN_B2_DELETIONS_CRON_FILE`, and their fire stamps. They do not read the drill
      or the budget listing;
    - check 11 (#3968) runs `kubectl logs -l recurring-job.longhorn.io --prefix --tail=-1` and
      matches `longhorn_lib/longhorn_skipped_volumes_logic.py:_SKIP_RE`, which accepts any
      RecurringJob name.
  The shim exports `KUBECONFIG` as the read-only ServiceAccount's. That account has the `view`
  ClusterRole, which covers ConfigMaps, batch Jobs and `pods/log`, and
  `ansible/roles/setup/k3s/defaults/main.yml:k3s_readonly_crd_api_groups` adds `longhorn.io` and
  `k3s.cattle.io`. `kubectl get etcdsnapshotfiles.k3s.cattle.io` succeeds under the agent's
  read-only kubeconfig (measured 2026-10-11).
- **Backup health's channels.** The shim calls `boot_grace_active {{ k3s_health_cron_boot_grace_s }}`
  and then pings two Healthchecks.io slugs, `longhorn-backup-health` and `uptime-kuma-alive`.
  It carries `DECIDED a host cron, not a monitor-bridge check (#3287)`.
- **Backup health bakes its arming and schedule inputs.** `longhorn-backup-health.sh.j2`
  exports `LONGHORN_BACKUP_ARMED`, `LONGHORN_R2_ARMED`, `LONGHORN_BACKUP_CRON` and
  `LONGHORN_WEEKLY_BACKUP_MINUTE_HOUR` from the `k3s_longhorn_*` keys at template time. It
  exports `LONGHORN_RESTORE_DRILL_STAMP_DIR`, `LONGHORN_RESTORE_DRILL_MAX_AGE_DAYS` and
  `LONGHORN_RESTORE_DRILL_COVERAGE_SLACK_DAYS` the same way. Only an apply of `setup/k3s`
  re-renders the shim. Measured with `deploy_changes.py:services_from_changed_paths` at the
  baseline:
    - `['ansible/inventory/group_vars/all.yml']` returns `broad_deploy=True` and no setup
      role, so nothing re-renders the shim;
    - `['ansible/roles/setup/k3s/defaults/main.yml']` returns
      `setup_roles={'k3s', 'deploy_ui'}`. `scripts/deploy_tools/setup_routing.py` routes `k3s`
      to `k3s-bringup.yml` with `on_tick_host: false`, so the change is owed as a hand apply.

  H1 removes the four baked backup keys in two PRs (H1, *Where the Longhorn variables go*).
  H1.7a makes backup health read the arm state from the BackupTargets and the schedules from
  the RecurringJobs at run time. H1.7b moves those keys, and `k3s_longhorn_weekly_backup_retain`,
  into the `longhorn-backup` role's defaults. H1.7b also adds a census row: no key defined in
  the defaults of one of the six Flux-born roles may appear in a task or template file under
  `ansible/roles/setup/` (H1, *Six Flux-born tags*).
- **The drill keeps eight state items, and three of them are file mtimes.**
  `ansible/roles/setup/k3s/templates/longhorn-restore-drill.sh.j2` writes under
  `STAMP_DIR` (`/var/lib/longhorn-restore-drill`):
    - four files: `last-success`, `candidates`, `excluded_oversize` and `excluded_nopvc`;
    - four per-volume directories: `ATTEMPT_DIR` (`attempts/`), `SUCCESS_DIR` (`success/`),
      `SEEN_DIR` (`seen/`) and `RETRY_DIR` (`retries/`).
  The rotation picks the least recently attempted candidate from `stat -c %Y` of
  `attempts/<pvc>`, and "A missing stamp is epoch 0". The one-retry bound reads the mtime of
  `retries/<pvc>`. `seen/<pvc>` is touched once, when a candidate first appears. `success/<pvc>`
  holds the epoch of the newest pass. The attempt is stamped before the restore, which is the
  anti-starvation property.
- **The drill's state has these readers** (`git grep -l` for the path and for every
  `k3s_longhorn_restore_drill_` key):
    - backup health's checks 7 and 8, through `longhorn_lib/longhorn_restore_drill_stamps.py`.
      `read_seen` takes `os.stat(...).st_mtime` of `seen/<cand>`, and `read_success` reads
      `success/<cand>`. `longhorn_backup_health_logic.py:check_restore_coverage` skips a
      candidate with no seen entry (`if seen_at is None: continue`);
    - `scripts/deploy_tools/runbook_gates_lib/longhorn_upgrade.py:DRILL_STATE_DIR`, which reads
      an absent directory as a failure. Its `restore_drill_max_age_s` reads the k3s role
      defaults through `scripts/lib/repo_paths.py:K3S_DEFAULTS`;
    - `scripts/docs/reference/state.py:longhorn_restore_drill_run`, whose `state_dir` defaults
      to `/var/lib/longhorn-restore-drill`. Its `_loops` resolves the cadence through
      `_k3s_default_var("k3s_longhorn_restore_drill_cron")`. The docs-refresh cron runs it with
      `KUBECONFIG=~/.kube/config` (`docs-refresh.sh.j2`) to write the `longhorn-restore-drill`
      row of `docs/reference/state.md`;
    - `docs/longhorn-disaster-recovery.md`, `docs/longhorn-upgrade.md` and
      `docs/k3s-node-plane-crons-and-incidents.md`;
    - tests: eight drill test files under `ansible/tests/longhorn/` plus
      `ansible/tests/longhorn/_restore_drill.py`,
      `ansible/roles/setup/k3s/tests/test_longhorn_restore_drill_checks.py`,
      `ansible/roles/setup/k3s/tests/test_longhorn_backups.py`,
      `scripts/deploy_tools/tests/test_longhorn_upgrade_gates.py`,
      `scripts/docs/tests/test_gen_reference_state.py`,
      `ansible/tests/services/test_pi_peer_backup_schedule_clears_the_longhorn_slots.py`
      (the cron key), `ansible/tests/k8s/test_base_images_digest_pinned.py` (the image key),
      `scripts/tests/test_renovate_managers.py` (why `roles/setup/*/defaults` is scanned),
      `ansible/tests/repo/test_shared_render_accessors.py` (the stamp-dir key),
      `ansible/tests/setup/test_kuma_env_renders_for_every_cron_tag.py`,
      `scripts/validate/tests/test_shell_template_cron_rules.py` and
      `scripts/validate/tests/test_validate_shell_templates.py`.
- **The drill's cluster calls.** Its `KUBECTL` is `k3s kubectl`, the root admin kubeconfig.
  The calls are: `get backups.longhorn.io -o json` and `get volume -o json` (lists),
  `get backuptarget "$TARGET"` (whose empty output fails the run as "is it disarmed?"),
  `apply -f` of a heredoc holding a `Volume`, a `PersistentVolume` and a
  `PersistentVolumeClaim`, `get volume "$DRILL"`, `run` of a probe pod, `get pod`, `logs`, and
  `delete` of the pod, PVC, PV and Volume in `cleanup()`. The PVC and the probe pod go in
  `PVC_NS`, the source claim's namespace. Measured 2026-10-11: all 30 `longhorn`-class claims
  are in `homelab`, and `observability` holds only four `longhorn-nobackup` claims.
- **The drill's evidence and hand runs.** It writes `PASS`, `FAIL` and `RETRY` lines only
  through `logger -t longhorn-restore-drill`. The operator pin is an argument or
  `RESTORE_DRILL_PIN`; a pinned run stamps `success/<pvc>` but not `last-success`, and
  `ansible/tests/longhorn/test_longhorn_restore_drill_operator_pin.py::test_argv_pin_drills_that_volume_not_the_rotations_pick`
  holds the pin. `docs/longhorn-upgrade.md` gate 2 and
  `docs/k3s-node-plane-crons-and-incidents.md` tell the operator to run
  `sudo /usr/local/bin/longhorn-restore-drill.sh`.
- **The drill's keys.** Twelve `k3s_longhorn_restore_drill_*` keys sit in `setup/k3s`
  defaults. Outside the drill script, `setup/` reads four: the cron key in `health-crons.yml`,
  and the stamp-dir, max-age and slack keys in the shim.
  `runbook_gates_lib/longhorn_upgrade.py:MAX_AGE_KEY` also reads the max-age key.
- **The B2 pair runs from the repo checkout.** Both crons run `uv run python
  scripts/diagnostics/probe.py` (`b2-deletions`, `b2-budget`) in `/home/<sys_user>/server`.
  `scripts/diagnostics/probe_lib/b2_ledger.py:DELETIONS_LOGQL` reads Loki, and the ledger and
  `prune-costs.json` (`PRUNE_SNAPSHOT_NAME`) live under `B2_LEDGER_DIR`, in the operator's
  home. The budget listing writes the price snapshot that the next day's accounting reads, and
  `probe.py b2-spend` reads the ledger on the host.
- **The off-box snapshot script does two jobs.** `etcd-snapshot-offbox.sh.j2` runs `k3s
  etcd-snapshot save --name offbox --snapshot-compress` with `--s3-*` flags,
  `--s3-region "{{ k3s_longhorn_r2_region }}"` and `--s3-retention {{ k3s_etcd_s3_retention }}`
  (14). It then compares the sha256 of `/var/lib/rancher/k3s/server/token` with
  `/var/lib/homelab/etcd-token.sha256` and reports DOWN on drift, on a missing stamp or on an
  unreadable side. It reports to the `Off-box etcd Snapshot` Kuma tile and the
  `etcd-snapshot-offbox` Healthchecks.io slug. It is the only reader of `k3s_etcd_s3_armed`
  (`ARMED="{{ 1 if k3s_etcd_s3_armed else 0 }}"`), which reports DOWN as "DISARMED".
- **The region key has two readers.** `k3s_longhorn_r2_region` (`auto`) is read by the
  off-box script and by `longhorn-backuptarget-r2.yaml.j2`, which H1.7b moves to
  `longhorn-backup`. H1's list of keys read by a moved object and a host cron omits it.
- **The retention fragment reads the script's key.**
  `scripts/docs/gen_doc_fragments.py:_etcd_offbox_retention` reads `k3s_etcd_s3_retention`
  from the k3s role, and `scripts/docs/fragment_renderers.py:render_etcd_offbox_retention`
  names that key in its sentence.
- **Its credentials are SOPS values.** The task `Deploy the off-box etcd snapshot credentials`
  renders `/etc/rancher/k3s/etcd-s3.env` (root 0600, `no_log: true`) from `r2_access_key_id`,
  `r2_secret_access_key`, `r2_bucket` and `r2_account_id`. `grep -cE '^r2_bucket: ENC\['
  ansible/vars/secrets.yml` prints 1, and the same grep for `r2_account_id` prints 1.
  `longhorn-r2-secret.yaml.j2` reads the same `r2_*` keys.
- **Both etcd drills depend on the script's output.** `scripts/backup/etcd_restore_drill.sh`
  sources the env file, requires `ETCD_S3_BUCKET` and `ETCD_S3_ENDPOINT`, and names the newest
  `offbox-*` object. The monthly full drill's guest script,
  `ansible/roles/setup/hypervisor/files/etcd-drill-guest-run.sh`, lists the
  `etcd-snapshots/offbox-` prefix and dies with no match. Its credentials come from the
  hypervisor role's own `templates/etcd-drill-s3.env.j2` on daniel-server.
  `docs/break-glass.md` item 3 and `docs/k3s-etcd-restore.md` name the `offbox-*` shape and the
  env file.
- **Live snapshots, 2026-10-11.** `kubectl get etcdsnapshotfiles.k3s.cattle.io` shows native
  local snapshots named `etcd-snapshot-daniel-box-<ts>` at 00:00 and 12:00, 83 to 87 MB each,
  uncompressed. The script's `offbox-daniel-box-<ts>.zip` copies are 14 to 15 MB, locally and
  in S3, and every S3 entry was created at 02:45. The defaults comment's 7.85 MB figure is
  stale.
- **What k3s documents about the config Secret.** `https://docs.k3s.io/cli/etcd-snapshot`,
  section *S3 Configuration Secret Support*, read 2026-10-11:
    - "Pass only the `--etcd-s3` and `--etcd-s3-config-secret` flags to enable the Secret. If
      any other S3 configuration flags are set, the Secret will be ignored."
    - "Keys in the Secret correspond to the `--etcd-s3-*` CLI flags", in a Secret of type
      `etcd.k3s.cattle.io/s3-config-secret`.
    - "The S3 config Secret cannot be used when restoring a snapshot."
    - `--etcd-snapshot-retention` (default 5) sets the S3 retention when `--etcd-s3-retention`
      is not set. `--etcd-snapshot-name` defaults to `etcd-snapshot`, and the schedule
      defaults to `0 */12 * * *`.
- **The installed k3s accepts every etcd key this plan sets.** On daniel-box,
  `/usr/local/bin/k3s --version` prints `v1.37.1+k3s1`, the value of
  `ansible/roles/setup/k3s/defaults/main.yml:k3s_version`. Its `k3s server --help` lists
  `--etcd-s3`, `--etcd-s3-config-secret`, `--etcd-snapshot-name`, `--etcd-snapshot-compress`,
  `--etcd-snapshot-schedule-cron` and `--etcd-snapshot-retention` (measured 2026-10-11, without
  root).
- **The etcd-s3 flag was rejected for a reason.** The defaults comment above
  `k3s_etcd_s3_armed` says `DELIBERATELY A CRON, NOT A SERVER FLAG`: changing the server's
  arguments re-runs the installer and restarts the control plane, with MetalLB VIPs and Pi-hole
  behind it.
- **k8s roles install host crons.** `git grep -n 'ansible.builtin.cron:'` under
  `ansible/roles/k8s/*/tasks/` finds eight roles. Seven install a present cron, and traefik's
  `Cloudflare IP drift` task is the one absent arm:
    - `crowdsec` installs four root cron files that run `cscli` through `k3s kubectl exec
      deploy/crowdsec -c crowdsec`. `files/remote_allowlist.py:main` also runs `kubectl get
      ingressroute -A -o json` and `kubectl -n homelab logs deploy/traefik -c
      access-log-rotate`. IngressRoutes live in three namespaces (measured 2026-10-11): 72 in
      `homelab`, 1 in `longhorn-system` and 3 in `observability`. The AppSec verify and the
      remote allowlist wrapper source `/usr/local/lib/kuma-push-lib.sh` and use `jq`. The
      home allowlist, `crowdsec-update-home-allowlist.sh.j2`, gates its IPv6 arm on `ip -6
      addr show scope global`, fetches `https://api6.ipify.org`, pushes with an inline `curl`,
      and keeps its last values in `/var/lib/crowdsec-home-allowlist/{ipv4,ipv6}`.
    - `configarr` installs `configarr-health`, and `janitorr` installs `janitorr-health`. Each
      imports `install_host_lib.yml` and `stamp_deployed.yml`. janitorr's task `Record the
      deployed janitorr health reader code` sets `stamp_deployed_name: janitorr`, and
      configarr's sets `configarr`. Each import writes
      `/var/lib/homelab/setup-deployed-manifest.d/<name>`.
      `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py:_K8S_HOST_LIB`
      holds both roles for the host-lib, `kuma_push.py` and `stamp_deployed.yml` rows of
      `K8S_ROLES_IMPORTING_SETUP_FILES`.
    - `qbittorrent` installs `qbittorrent-prefs-check`, which runs `apply_prefs.py --dry-run`
      against the WebUI and logs the result. It pushes no tile.
    - `registry` installs `registry-gc` as root. Its script is an orchestration, not a Job
      create: it skips while a `build-*` Job is active, untags superseded content tags over
      HTTP against the serving registry, scales the Deployment to 0, waits for the pod to go,
      runs the GC Job from the staged `/etc/rancher/k3s/manifests/registry-jobs/gc-job.yaml`,
      scales back up from `trap scale_up_and_report EXIT`, proves the store with `crane`, and
      writes `/var/lib/registry-gc/state.json`, which nothing reads. D5's Wave D registry
      bullet retires this cron rather than moving it, and D5's census of host scripts uses its
      `scale` call as the red fixture for a second writer under C4.
    - `artifacts` installs `sync-artifacts`, and `observability` installs
      `claude-otel-health`. `has_claude_code` is true on both daniel-box and daniel-server.
- **A fragment left behind turns setup drift DOWN.**
  `ansible/roles/setup/initial_setup/files/setup-drift-lib.sh` reports any listed live path
  that is gone as `(missing)`. The in-tree precedent, the `setup/k3s` task `Retire the
  host-installed Longhorn reapers`, lists its fragment
  `/var/lib/homelab/setup-deployed-manifest.d/k3s-longhorn-reap` in its absent arm.
- **The pod network is IPv4-only.** `kubectl get nodes -o custom-columns=NAME:.metadata.name,CIDRS:.spec.podCIDRs`
  prints `10.42.0.0/24` and `10.42.1.0/24`.
- **How a pod pushes to Kuma.** Host scripts source `/usr/local/lib/kuma-push-lib.sh` (from
  `ansible/roles/setup/initial_setup/files/kuma-push-lib.sh`) and pin `KUMA_RESOLVE_IP` to the
  ingress VIP. A pod uses `ansible/roles/setup/common/files/kuma_push.py`, staged into its
  ConfigMap, against Kuma's in-cluster Service. autofix-bridge and uptime-kuma do this, and
  `K8S_ROLES_IMPORTING_SETUP_FILES` lists them. uptime-kuma admits callers through the
  `netpol_from` list on its `containers_list` entry. monitor-bridge's swallowed-verdict check
  reads host pushers through `ansible/roles/k8s/monitor-bridge/files/checks/logs.py:SWALLOWED_VERDICTS_LOGQL`
  and pod pushers through `SWALLOWED_VERDICTS_POD_LOGQL`, which names only `pi-peer-backup`.
- **No in-cluster job runs `kubectl` today.** The repo's Python jobs use a digest-pinned
  `python:3.14-alpine` (for example `ansible/roles/k8s/uptime-kuma/defaults/main.yml:uptime_kuma_k8s_status_page_sync_image`).
  `registry.k8s.io/kubectl` exists (its tag list answers, read 2026-10-11) and is distroless,
  so it has no shell to copy its binary out. The fleet rule
  `ansible/tests/k8s/test_base_images_digest_pinned.py::test_every_external_image_pin_carries_a_digest`
  covers every `*_image` default.
- **Retiring a host cron.** Only an absent arm removes one. traefik's `Cloudflare IP drift`
  task is the in-tree precedent. An inventory-only change routes as deploy-plane
  (`deploy_changes.py:_BROAD_DEPLOY_PREFIXES`), and the narrowing greps only
  `scripts/deploy_tools/narrow_lib/broad.py:ROLE_TREES`, which excludes `roles/setup/` on
  purpose. A key that only a setup role reads therefore maps to no apply.
- **A field edit to a `containers_list` entry narrows, and still applies denylisted tags.**
  Measured at `70591c307` by calling
  `scripts/deploy_tools/narrow_lib/containers.py:changed_fields`,
  `containers.py:entry_change_tags` and `scripts/deploy_tools/narrow_lib/broad.py:_list_reader_tags`
  on an in-memory copy of `ansible/inventory/host_vars/daniel-box.yml`, with one label
  appended to an entry's `netpol_from`:
    - For uptime-kuma's entry, `changed_fields` returns `{'uptime-kuma': {'netpol_from'}}`,
      `entry_change_tags` returns `uptime-kuma`, and the readers are artifacts,
      monitor-bridge, netpol-baseline, pihole and uptime-kuma.
    - For qbittorrent's entry, the own tag is `qbittorrent` and the readers are the same five.
    - No `containers.py:FILTER_FIELDS` row names `netpol_from`, so the five are the
      whole-entry readers, which `reader_paths` keeps for any field edit.
    - 8e79e95d6, 17 minutes after `70591c307`, moved those five onto named filters. The
      same call at `a5abf8581` returns netpol-baseline alone as the reader of
      either edit, beside the edited entry's own tag. D2 Open question 10 states what an
      added Flux-born entry reaches on that tree.

  `broad.py:narrow` unions `broad.py:_changed_half` with that deploy-plane half.
  `_changed_half` maps a range's edited `ansible/roles/k8s/<role>/` paths to the role's tag,
  because `deploy_changes.py` puts the whole role directory, `tasks/` included, in `cs.k8s`.
  The tick applies the union as a broad deploy-plane apply, and every broad arm is
  forward-only. The marker `deploy_narrow.py:DECIDED the broad plane ignores` means
  `K8S_AUTODEPLOY_DENYLIST` does not stop that apply. uptime-kuma, monitor-bridge, pihole,
  blackbox-exporter, crowdsec, janitorr and qbittorrent each set `k8s_autodeploy: false` in
  `defaults/main.yml`. artifacts, configarr and netpol-baseline set `true`.
- **The armed prune.** `ansible/roles/k8s/manifests/defaults/main.yml:manifests_prune_allowlist`
  lets a role's next deploy delete a CronJob, ConfigMap, ServiceAccount or NetworkPolicy it no
  longer renders. It never deletes a Role, RoleBinding, Secret or PVC.
- **`k8s/cronjob-gate`** runs a caller's CronJob once at deploy time. Its `CLAUDE.md` requires
  the job to be idempotent under one extra out-of-band run.
- **The default backup group.** `longhorn-recurringjob.yaml.j2` carries `DECIDED groups:
  [default]`. Longhorn adds a volume to `default` only when the volume has no recurring job of
  its own.
- **Readers that treat every group label as a tier.** Each of these reads any
  `recurring-job-group.longhorn.io/` key on a Volume as its backup tier:
    - `ansible/roles/setup/k3s/templates/longhorn-restore-drill.sh.j2`, whose shell variable
      `IN_BACKUP_GROUP` admits a volume with any group key other than `no-backup`. The
      ELIGIBLE, OVERSIZE and NOPVC sets share it.
    - `scripts/backup/longhorn_reap_lib/logic.py:backup_owner_map` and `snapshot_owner_map`,
      which keep the last group key as the volume's one owner. kubectl's JSON emits label keys
      sorted, so a second key that sorts later wins. `volumes_with_group_label` counts the
      volumes with any group key.
    - `scripts/diagnostics/probe_lib/longhorn_cluster.py:volume_shard_labels`, which maps each
      volume to a group name the same way.
    - `ansible/seed_volume_backup.yml`'s `seed_group`, which takes the first group key in sorted
      order.
  None of them reads a `recurring-job.longhorn.io/` per-job key. Measured 2026-10-11 with
  read-only `kubectl`: no Volume carries a per-job key, no Volume carries more than one group
  key, and 18 `no-backup` Volumes are all `attached`.
- **The trim's abort valve.** The `setup/k3s` contract says the trim aborts when the node's
  longhorn-manager pod is not ready, and the script skips a volume that is not attached and
  healthy.
- **Longhorn attaches a detached volume to run a recurring job.**
  `kubectl -n longhorn-system get settings.longhorn.io allow-recurring-job-while-volume-detached`
  prints `true` (measured 2026-10-11). `setup/k3s`'s task `Allow recurring backups of detached
  volumes` sets it, because `pi-peer-backup-data` is always detached at the nightly backup. A
  trim job whose group held a detached volume would therefore attach that volume.
- **Longhorn's recurring job swallows a per-volume failure.** In longhorn-manager `v1.12.1`
  (the `k3s_longhorn_version` pin), the `recurringjob` package's `StartVolumeJobs` has an
  unnamed `error` result. Its `return err` copies the result before the deferred
  `ewg.Wait()` assigns the per-volume error to the local `err`, so the function returns nil.
  A failed trim logs `Failed to run volume job` and raises a `FailedFilesystemTrim` Warning
  event, and the Job still succeeds (source read through `gh api`, 2026-10-11).
- **Who a volume serves is on the Volume CR.** `status.kubernetesStatus.workloadsStatus`
  lists each consuming pod's `workloadType` and `podStatus`. Measured 2026-10-11: 39 volumes
  are `attached` with `ReplicaSet`/`Running`; `detached` volumes show `Job`/`Succeeded` (one),
  `ReplicaSet`/`Failed` (two) and `ReplicaSet`/`Succeeded` (one). A Deployment reports as
  `ReplicaSet`.
- **H1's tags.** H1 gives `cluster-base`, `metallb`, `coredns`, `longhorn` and
  `longhorn-backup` `flux_prune: false`. Removing a templated object from one of them leaves it
  live, and its removal PR carries an **operator-only** delete step (H1, *Plumbing tags are
  never pruned*). `longhorn` and `longhorn-backup` also set `flux_forward_only: true`, so a
  failed revision that changes them writes `DEPLOY_HOLD` and does not reset `prod` (H1,
  *Forward-only tags and the promote rule*). `longhorn` carries vendored upstream bytes and
  never substitutes. A tag that does not substitute gets no `$${` escape, so a `${` in its
  script reaches the cluster unchanged (C1; H1, *No escape either*). H1's
  `longhorn-reconcile` is the precedent for a script CronJob beside `longhorn`. It is a tag and
  a role of its own, `ansible/roles/k8s/longhorn-reconcile/`, outside `longhorn`'s render, and
  it is not forward-only (H1, *Six Flux-born tags*). `longhorn-backup` substitutes the `r2_*`
  keys for its credential Secret.
- **The reapers are never scheduled.**
  `ansible/tests/longhorn/test_longhorn_reap_never_scheduled.py::test_no_setup_role_schedules_a_reaper`
  scans setup roles only. `test_census_finds_the_scheduled_drill` in the same file requires
  `longhorn-restore-drill.sh` among the setup texts as its named member.
- **CronJob timezones.** monitor-bridge's defaults say every Healthchecks.io slug but
  `pi-peer-backup` is a daniel-box host cron that runs in UTC. The B2 ledger is keyed on the
  UTC day. Of those slugs, only `registry-gc` belongs to a host cron that a k8s role
  installs, and D5 retires it.
- **No stage cluster exists.** `docs/archive/staging-cluster.md` opens with "ARCHIVED
  2026-09-29 (#2941). The staging cluster is retired", and
  `scripts/lib/kubectl.py:RETIRED_CLUSTER_NODES` holds `daniel-stage`. Every proof in this
  plan therefore runs in CI, as a read-only measurement on production, or as a production
  change whose blast radius its step states.

## Design

### Who writes each change at merge time

- **The six k8s roles** (crowdsec, configarr, janitorr, qbittorrent, artifacts,
  observability) are Ansible-owned when their H2 PR lands, because D5 wave D waits on it
  (*Open questions* 1). H2.4c's range touches only its role, so `land.sh` deploys the role
  through `deploy.sh`. H2.4b and H2.5b to H2.5d also edit a `containers_list` entry, so the
  tick applies the role with that entry's tag and its readers as one narrowed, forward-only
  apply, and the PR follows *Landing a `containers_list` edit*. The role's new Secrets
  render from Ansible's SOPS lookups, like its existing ones. D3 is not involved. The edge
  keeps the writer simple; it is not what keeps C4. D5 moves host tasks into
  `k8s_host_files` before a flip, which C4 allows. If a role flips first anyway, its H2 PR
  renders through Flux, needs D3 for its Secrets, and puts its absent arms in that role's
  `k8s_host_files` task file.
- **Registry** gets no H2 PR. D5 retires its GC (*Registry GC is D5's to retire*).
- **The trim RecurringJob** goes in H1's `longhorn` tag, which is `owner: flux` from H1.5a.
  Its `trim` and `default` arms go in H1's `longhorn-reconcile` tag, `owner: flux` from H1.6.
- **The drill CronJob, its RBAC, its script and its stamp ConfigMap** go in a tag of their
  own, `longhorn-restore-drill`, and not in `longhorn` (*The drill's own tag*). The drill
  writes objects in `homelab`, and `longhorn` is one of the tags H1's DR order waits on
  before anything creates `homelab`. The new tag is a Flux-born k8s role, as H1's five are:
  its `containers_list` entry carries `owner: flux` and `depends_on: [longhorn]` from its
  first commit. It reads no secret and does not substitute, so the script reaches its
  ConfigMap byte for byte, with no escape and no D3 edge (C1). C1's publish refusal scans a
  non-substituting tag for `${<key>}` with a `secrets.yml` key name. The drill's shell
  variables are upper-case apart from `cand` and `pvc`, and no `secrets.yml` key is
  upper-case or named either of those (measured), so the scan passes. A collision would fail
  publish, not deploy.
- **The etcd-s3 config Secret** goes in `longhorn-backup`, which already substitutes the same
  four `r2_*` keys for the R2 credential Secret. The region key lives in the same role as the
  R2 target that reads it. `cluster-base` stays a tag that does not substitute. The etcd keys
  go in the daniel-box package's drop-in (H6.4b).
- **`setup/k3s` changes** are hand applies of `uv run ansible-playbook ansible/k3s-bringup.yml
  --tags <tags>`, with the tags named in each PR. Each is **operator-only**, because the
  playbook decrypts (Baseline). The deployer records each merge as a `manual_plane` line for
  `k3s`, and the PR clears it with H1's four steps: `land.sh` prints the narrowed tags, the
  operator applies them, `gitops_state.py clear-owed manual_plane k3s --applied <tags>` drops
  them, and the bare clear is used only after the line's range shows no other commit under
  `ansible/roles/setup/k3s/`. This plan calls that *the owed-line procedure*.
- **`claude_code`** is a setup role, so the tick applies it as `initial_setup.yml --tags
  claude_code`.

### The pattern for one moved cron

- **Shape.** A CronJob in the role that owns the cron, with `concurrencyPolicy: Forbid`, an
  `activeDeadlineSeconds` below its period, and `timeZone: Etc/UTC`. The schedule keeps the
  host cron's UTC fields unchanged.
- **Identity, derived from the script.** Each job gets its own ServiceAccount. Its rules are
  the verbs and resources its script calls, and nothing else. A `kubectl exec deploy/<name>`
  or `kubectl logs deploy/<name>` resolves the Deployment and lists its pods, so it needs
  `get` on `deployments.apps` and `list` on `pods` beside `create` and `get` on `pods/exec`
  (both, because the exec transport has changed across kubectl releases) or `get` on
  `pods/log`. A `kubectl apply` reads, then creates or patches, so it needs `get`, `create`
  and `patch` on each kind it applies. A `kubectl delete` waits for the object to go by
  default, with a `list` and a `watch` on a `metadata.name` field selector. When either is
  Forbidden, kubectl skips the wait and still exits 0 (upstream `pkg/cmd/delete/delete.go`,
  the `errors.IsForbidden` branch after the wait). So a script that deletes a kind also needs
  `list` and `watch` on it, or its teardown order is lost without an error.
- **The RBAC-against-script test.** One test in `ansible/tests/k8s/` derives, for each moved
  job, the `(namespace, apiGroup, resource, verb)` set its script calls, and requires a
  matching rule in that job's rendered RBAC. For a shell script it reads every `$KUBECTL`
  line, and the heredoc's `kind:` lines for an `apply`. For a Python module it runs `main`
  with a recording runner, as the modules' unit tests already do, and maps each recorded
  `kubectl` argv. It adds the implied verbs above: `exec` and `logs` on a Deployment,
  `apply`'s `get`, `create` and `patch`, and `delete`'s `list` and `watch`. Its named member
  is the drill's `get backuptarget`. Its red cases are a fixture script line with no matching
  grant, and a fixture `delete pvc` line whose Role grants `delete` alone.
- **Images, each a digest-pinned `*_image` key in the role's defaults.** Renovate's
  k8s-defaults manager covers `ansible/roles/k8s/*/defaults/main.yml`, and the fleet digest
  rule covers every `*_image` default, so no new wiring is needed. Two shapes:
    - *Shell jobs* (the drill, the AppSec verify and the home allowlist) need bash, `jq`,
      `curl` and `kubectl`. They run in one community image that carries all four,
      `alpine/k8s` at a tag whose kubectl minor is within one of the server's (v1.37).
    - *Python jobs* (the remote allowlist, the bouncer prune, configarr and janitorr health)
      run in the repo's pinned `python:3.14-alpine`. An init container from the same
      `alpine/k8s` digest copies its `kubectl` binary into an `emptyDir`. The qbittorrent
      prefs check calls no `kubectl` and needs no init container.
  Each family PR quotes, for its pinned digest, `command -v` of every tool its script calls,
  run in that image. *Open questions* 9 asks the operator to accept the community publisher.
- **Behaviour under the image, not only presence.** `alpine/k8s` is built `FROM alpine:edge`
  and installs no `coreutils` (its upstream Dockerfile, read through `gh api`), so `mktemp`,
  `date`, `sort` and `stat` are BusyBox applets over musl. Each shell job's script tests
  therefore gain an image leg that runs the same script and fixtures inside the pinned digest
  with `docker run --rm`. For the drill, the leg goes in the `launch` call of
  `ansible/tests/longhorn/_restore_drill.py`. The leg skips when `docker` is missing and `CI`
  is unset, and fails when `CI` is set, so CI always runs it. The drill's red case is a
  fixture copy that keeps `mktemp /tmp/restore-drill.XXXXXX.yaml`, which fails under the
  image. The AppSec verify has no script harness at `70591c307`, so H2.4b adds one with the
  leg. The home allowlist's tests under `ansible/tests/services/` gain the leg in H2.4c. No
  moved job sets `readOnlyRootFilesystem`, and the only property row that requires it is
  karakeep's chrome in `ansible/tests/k8s/_workload_property_rows.py` (measured), so `/tmp`
  stays writable and no `emptyDir` is needed.
- **Pushes.** A Python job pushes with `kuma_push.py`, staged into its ConfigMap. A shell job
  that pushes sources `kuma-push-lib.sh`, staged into its ConfigMap the same way, with Kuma's
  Service as the target. The role joins `kuma_push.py`'s row in
  `K8S_ROLES_IMPORTING_SETUP_FILES`. No moved job pings Healthchecks.io (Baseline, *CronJob
  timezones*).
- **No secret in a ConfigMap.** Six of the moved host scripts template a secret into their
  body today: the push tokens in `configarr-health.sh.j2`, `janitorr-health.sh.j2`,
  `crowdsec-appsec-verify.sh.j2`, `crowdsec-update-home-allowlist.sh.j2` and
  `crowdsec-update-remote-allowlist.sh.j2`, and `qbittorrent_password` in
  `prefs-check.sh.j2` (measured with `grep -rnoE` at `70591c307`). A script staged into a
  ConfigMap keeps no such reference. Each value goes into the role's Secret, rendered from
  Ansible's SOPS lookup like the role's existing Secrets, and reaches the container as an env
  var through `secretKeyRef`: `KUMA_PUSH_TOKEN` for a pusher, `QBITTORRENT_PASSWORD` for the
  prefs check. A shell job builds its push URL from the variable at run time and still hands
  it to `kuma_push` as an argument, which feeds curl on stdin. A Python entry point reads the
  variable and passes it to `kuma_push.push` as `token`. The home allowlist's inline `curl`
  reads the same variable. A render test in `ansible/tests/k8s/` holds the rule. It binds
  every key name in `ansible/vars/secrets.yml` to a unique sentinel value (key names are
  plaintext, so it decrypts nothing), renders the moved roles' ConfigMaps, and fails when any
  ConfigMap's data holds a sentinel. It reads only ConfigMaps, so the Secrets that configarr's
  and crowdsec's `config-secret.yaml.j2` render are out of its scope.
- **Evidence goes to stdout.** A pod has no `/dev/log`. Every verdict line that the host
  script sent through `logger -t <tag>` is echoed to stdout as `<tag>: <message>`, the syslog
  shape pi-peer-backup uses, and the pod log is the evidence. `probe.py loki-query
  '{container="<container>"}'` reads it. `SWALLOWED_VERDICTS_POD_LOGQL` widens to name each
  moved pusher's container.
- **Each grant lands with its CronJob.** A `netpol_from` caller lands in the same commit as
  the CronJob whose pod template carries its label, never a PR ahead.
  `ansible/tests/k8s/test_netpol_from.py::test_every_caller_is_a_label_some_rendered_pod_carries`
  fails on a caller that no rendered pod carries. Its exemption,
  `ansible/tests/k8s/test_netpol_from.py:UNRENDERED_CALLERS`, admits only a pod that a task
  starts with `--labels app=<caller>`, so a CronJob label cannot use it. A revert removes the
  grant and the CronJob together. The grants are uptime-kuma's `netpol_from` for the pushers,
  and qbittorrent's `netpol_from` for the prefs check, the one target fence a moved job calls.
- **Who applies each grant.**
    - qbittorrent's entry sets `netpol_role_owned: true`, so the qbittorrent role renders that
      fence. The role's one `k8s/manifests` apply creates the fence and the CronJob together,
      before the role's `k8s/cronjob-gate` include runs. The landing's narrowed apply also
      names netpol-baseline, the one reader of `netpol_from`.
    - uptime-kuma's entry carries `netpol_from` without `netpol_role_owned`, so
      netpol-baseline's `templates/networkpolicy-callers.yaml.j2` renders that fence. The
      landing's narrowed apply is `deploy.yml --tags <role>,uptime-kuma,netpol-baseline`
      (Baseline, *A field edit to a `containers_list` entry narrows*). It applies the workload role first and
      netpol-baseline last, the order `ansible/roles/k8s/netpol-baseline/CLAUDE.md`
      requires. A deploy-time gate run of a pusher therefore pushes before the fence admits
      it. *First run* says how each pusher handles that.
    - A fresh pod's first dial can be dropped even through an open fence, because kube-router
      learns a new pod's labels late. `kuma_push` already retries three times, 30 seconds
      apart. The prefs check gains a bound of its own: it retries a connection that gets no
      response for up to 60 seconds before its first login. It never retries a login that got
      an answer, because qBittorrent's `web_ui_max_auth_fail_count` is 5.
- **Retirement.** In the same PR, the installing role's cron task becomes `state: absent`, with
  absent arms for the script files it ran and for any `setup-deployed-manifest.d` fragment the
  role wrote. An absent arm names no schedule key. In a k8s role, the absent arms run before
  the role's `k8s/manifests` include. The host cron is therefore gone before the CronJob
  exists.
- **First run.** `k8s/cronjob-gate` proves at deploy time that a job's image runs. It gates
  three jobs, each idempotent under an extra run:
    - the qbittorrent prefs check, whose fence applies in the role's own apply;
    - the home allowlist, which uses the host network, pushes through Traefik and needs no
      grant (*CrowdSec*);
    - configarr health, because configarr sets `k8s_autodeploy: true`, and
      `ansible/tests/deploy/test_k8s_autodeploy_batch_gates.py::test_auto_deployable_roles_gate_every_batch_workload_they_render`
      refuses an ungated CronJob in such a role. Its landing's gate run is the one run whose
      push meets the closed fence. `kuma_push` gives up after three attempts, and H2.5b sets
      the CronJob's `activeDeadlineSeconds` above three `KUMA_PUSH_TIMEOUT_S` attempts plus
      the two 30-second waits, and below `cronjob_gate_timeout`. The gate counts a started
      container as a run, so the denied push does not change the deploy's verdict. Every later
      gate run pushes through the open fence.

  The remote allowlist, the AppSec verify and janitorr health are not gated, because crowdsec
  and janitorr set `k8s_autodeploy: false`. No run of theirs exists before netpol-baseline's
  apply. The bouncer prune and the drill are not gated, because an extra run is not harmless.
  For each ungated job, and for configarr health's push, the PR body records the first
  scheduled run: the Job name, its `status.succeeded` and its log tail with the push's
  result.

### Landing a `containers_list` edit (until D6.12)

Five H2 PRs edit `containers_list`. H2.4b, H2.5b and H2.5c each add a label to
uptime-kuma's `netpol_from`, and H2.5d adds one to qbittorrent's. H2.3b adds the drill's
entry. A revert of any of them edits the list the same way. Each such range routes as
deploy-plane, so the tick applies a narrowed `deploy.yml`, forward-only, and
`k8s_autodeploy: false` does not stop it (Baseline, *A field edit to a `containers_list`
entry narrows*). That apply also ships any change of a named tag that is on `origin/master`
and not yet applied, and a failing verify task in any named tag writes `hold_sha` over the
deploy plane. This holds until D6.12 deletes the deployer's k8s arms.

The narrow output each PR expects, measured at `a5abf8581` with `deploy_tags.py narrow`.
One scratch commit appended a label to both uptime-kuma's and qbittorrent's `netpol_from`,
and printed `netpol-baseline,qbittorrent,uptime-kuma`. A second added the drill's entry. A
`netpol_from` edit is a field edit, so it names the edited entry's tag and the one reader of
`netpol_from`, netpol-baseline. Each family PR's own role tag (crowdsec, configarr,
janitorr, qbittorrent) comes from `broad.py:_changed_half`, which maps the PR's edits under
`ansible/roles/k8s/<role>/`, and was not part of the scratch commits. The added drill entry
names the readers D2 Open question 10 gives a bare entry.

- **H2.4b:** crowdsec, uptime-kuma and netpol-baseline. Two are denylisted: crowdsec and
  uptime-kuma.
- **H2.5b:** configarr, uptime-kuma and netpol-baseline. One is denylisted: uptime-kuma.
- **H2.5c:** janitorr, uptime-kuma and netpol-baseline. Two are denylisted: janitorr and
  uptime-kuma.
- **H2.5d:** qbittorrent and netpol-baseline. One is denylisted: qbittorrent.
- **H2.3b:** `longhorn-restore-drill`, artifacts, blackbox-exporter, pihole and
  uptime-kuma. `deploy_tags.py narrow` prints the drill's tag, and D2.2's deployer skip
  drops it because Flux owns it, so the tick applies the four readers. Three are
  denylisted: blackbox-exporter, pihole and uptime-kuma. The drill's entry carries
  `platform` and `depends_on`. `lan_hostnames` reads `platform`, and no reader reads
  `depends_on`.
- **A revert** expects its forward PR's list. A revert of H2.3b expects the four readers
  alone, because `entry_change_tags` gives a removed entry no tag of its own.

A list measured later can differ when a template starts or stops reading a field. The
quoted output is the expectation the stop rule binds, and the D2.1c render census
(`ansible/tests/k8s/test_flux_born_entry_reader_census.py`) holds that it covers every
render an added Flux-born entry moves.

Before the PR is opened, and again right before `land.sh`, the session runs four steps:

1. **Narrow.** Run `uv run python scripts/deploy_tools/deploy_tags.py narrow origin/master
   HEAD` and quote its output. An output that differs from the expected list stops the
   session until it knows why. Examples are a list without the edited entry's own tag, a
   list without netpol-baseline, an H2.3b list without artifacts, the drill's tag alone, or
   the 23 readers of a tree without D2.1c.
2. **Stop rule.** Apply D1.2's stop rule over every tag the output names. Run `uv run python
   scripts/diagnostics/probe.py releases --stale-only` and `uv run python
   scripts/diagnostics/probe.py releases`, and read the service names, not the exit code. Do
   not merge while any named tag is listed by the first command or flagged `unmerged` or
   `dirty` by the second. The PR body quotes both runs.
3. **Render.** Quote `./scripts/deploy.sh --tags <the narrow list> --dry-run` and name each
   tag whose render moved. Leave out a Flux-owned tag, which `deploy.sh` refuses with exit 5
   (D2, *What each entry point does with a tag that is not Ansible-owned*). A tag whose
   render did not move still runs its own verify tasks, as M4's PR 1 records for crowdsec's
   ban gate.
4. **Landing.** The tick follows D1.2's *Landing* and *If the narrowing refuses* paths. A
   failed apply writes `hold_sha` and a `hold_plane` entry naming `ansible/deploy.yml` and
   the whole list. Recovery is D1's way out of a hold (D1 *Rollback*).
    - A fleet-coverage refusal records a `k8s_unapplied` line for each narrowed tag. The
      landing session runs the stop rule again, then `./scripts/deploy.sh --tags <the
      narrow list>` from a checkout at `origin/master`.
    - Any other refusal records a line for every declared service. The session stops and
      quotes the tick's `narrow: cannot narrow` journal line. The operator deploys the
      pre-PR list or rules otherwise, and clears the lines of services outside it
      (**operator-only**).

### Retirement by absent arm, not a list

This slice keeps no `retired_host_crons` list. A list in `group_vars` is an inventory change
that maps to no apply, and moving it into `initial_setup` defaults would make every addition
an apply of the whole `initial_setup` role. An absent arm in the role that installed the cron
is applied by the writer that installed it, in the same landing:

- in a k8s role, by `land.sh`'s deploy of that role;
- in `setup/k3s`, by the PR's named hand apply.

H7 deletes the arms with Ansible.

A retired-cron census, a new `Census` row in `ansible/tests/repo/test_census_rows_roles.py`,
holds the cron names and the fragment names this slice retires. It fails in two cases:

- a retired cron has no `state: absent` task in the role that installed it, or in that role's
  task file in `k8s_host_files` once D5's conversion PR has moved the arm there;
- a retired fragment (`configarr`, `janitorr`) has no absent arm for its path under
  `/var/lib/homelab/setup-deployed-manifest.d/`.

That catches a PR that deletes an install task, which would leave the cron running with no
writer, and one that drops a `stamp_deployed.yml` import, which would leave a fragment that
turns setup drift DOWN. Its named members are traefik's `Cloudflare IP drift` and the
`k3s-longhorn-reap` fragment.

### The flux-ownership census (the D5 coordination guard)

A second new `Census` row fails when a `containers_list` entry with `owner: flux` names a role
whose tasks install something a host keeps running:

- an `ansible.builtin.cron` with `state` present, explicitly or by default;
- a `copy` or `template` whose `dest` is under `/usr/local/bin/`, `/usr/local/lib/`, `/opt/`,
  `/etc/cron.d/` or `/etc/systemd/`, on any host, `delegate_to` included;
- an import of `install_host_lib.yml` or `stamp_deployed.yml`.

A `dest` under `/etc/rancher/k3s/` is kubectl staging for the Ansible apply path. That path
stops at the flip and R4 deletes it, so the census leaves those files to D5's task classes.
The census has no allowlist. It reads only `owner: flux` entries, so every unflipped role
passes. H2.1 checks that none of the six roles is flipped yet, and converts any that is
before it lands.

Two roles outside the six would fail once flipped, and each has an owner:

- registry's `Registry garbage collection` cron, until D5's retirement PR turns it into absent
  arms and moves them to `k8s_host_files`. The census therefore also holds D5's
  retire-before-flip order for registry.
- pi-peer-backup's task `Install the forced-command wrapper on daniel-pi`, which writes
  `/usr/local/bin/pi-peer-backup-shell` through `delegate_to: daniel-pi`. D5 moves it to
  `k8s_host_files` (*Open questions* 4).

game-stats's `stage.yml` writes under `/etc/rancher/k3s/`, so the census leaves it alone.

D5.1c's host-write census covers the same flips more strictly. *Open questions* 6 records the
overlap.

### What H2 leaves in `k8s_host_files`

D5 moves each flipped role's host-side task rows into one setup role,
`ansible/roles/setup/k8s_host_files/`, in a conversion PR before the flip (D5, *Host-side
rows*). D5 wave D waits on H2's family PR for each of the six roles, so H2 takes over no
`k8s_host_files` row. It changes what D5's conversion PRs find:

- **crowdsec, configarr, janitorr and qbittorrent** keep only absent arms after H2.4b, H2.4c
  and H2.5b to H2.5d: the cron files, the script paths and, for configarr and janitorr, the
  host-lib copies and their fragments. Their conversion PRs move those arms into
  `k8s_host_files`, and H7 deletes them.
- **observability** keeps no host row after H2.7. **artifacts** keeps its three directory
  tasks, the star list directory included, which its conversion PR moves.
- **Rows H2 never touches:**
    - docs's site directory, which stays in `k8s_host_files` until H7;
    - registry's GC absent arms and traefik's `Cloudflare IP drift` arm, which H7 deletes;
    - crowdsec's Pi probe timer, which H5.14 moves into the Pi package, and pi-peer-backup's
      two Pi writes, which stay until H7 (H5, *Rows H5 takes from D5's `k8s_host_files`*).
      The Pi Alloy metrics stage that D5 adds lives in
      `ansible/roles/containers/alloy/templates/config.alloy.j2`, not in `k8s_host_files`.

If a conversion PR lands before its role's H2 family PR, the family PR puts its absent arms
in that role's `k8s_host_files` task file instead (*Who writes each change at merge time*).

### Native trim

- **The job.** A Longhorn RecurringJob named `trim` in `longhorn` with `task: filesystem-trim`,
  `cron: "10 6 * * *"`, `concurrency: 1` and `groups: []`. H2.2a writes a `# DECIDED` marker
  at `groups: []` that gives the reason below, beside the backup jobs' `DECIDED groups:
  [default]`.
- **A per-job label, not a group label.** A volume joins the trim through Longhorn's per-job
  label, `recurring-job.longhorn.io/trim: enabled`, called *the trim label* below. It never
  carries a `recurring-job-group.longhorn.io/trim` key. Four readers treat every
  `recurring-job-group.longhorn.io/` key as a backup tier (Baseline, *Readers that treat every
  group label as a tier*). A `trim` group would put a `no-backup` volume back into the drill's
  `IN_BACKUP_GROUP` set. It would also replace `default` or the weekly shard as the reapers'
  owner, because `trim` sorts after each of them. No reader in the tree reads a
  `recurring-job.longhorn.io/` key on a Volume, so the per-job label changes no reader.
  Longhorn still runs the job on it: in longhorn-manager `v1.12.1`,
  `app/recurringjob/volume.go` lists the volumes labelled
  `recurring-job.longhorn.io/<job>=enabled` first, then each group in `spec.groups`.
- **Membership.** `longhorn-reconcile` (H1.6) puts the trim label on a volume only
  when two things hold, both read from the Volume CR: `status.state` is `attached`, and
  `status.kubernetesStatus.workloadsStatus` has an entry with `podStatus: Running` whose
  `workloadType` is `ReplicaSet` or `StatefulSet` (Baseline). A claim that only a CronJob's
  Job mounts, such as `pi-peer-backup-data`, is detached between runs and never joins. A
  scaled-to-zero workload's volume is detached and drops out. The arm also removes the trim
  label when either condition stops holding. The detached-volume setting is `true` (Baseline),
  so this rule is what keeps the trim from attaching an idle volume.
- **The rule lags by up to an hour.** `longhorn-reconcile` runs hourly (H1), so the rule
  removes the trim label only on the pass after a volume detaches. A workload scaled to zero
  after the last pass before 06:10 UTC keeps the trim label for that run, and Longhorn attaches
  its volume for the trim and detaches it afterwards. That is the same attach Longhorn already
  makes nightly for `pi-peer-backup-data`'s backup, the reason the setting is `true`. The
  window is accepted (*Risks*). It opens at H2.2b, because under H2.2a's bound only the canary
  carries the trim label.
- **No new grant.** The rule reads only `volumes.longhorn.io`, which H1's reconcile Role
  already grants with `get`, `list` and `patch`. A unit test in the `longhorn-reconcile` role's
  `tests/`
  runs the reconcile arms against a recording client and fails when any call names a
  resource or verb outside the rendered reconcile RBAC.
- **A canary before the whole set.** No stage cluster exists, so the first proof runs on
  production against one volume. H2.2a ships the trim arm with
  `longhorn_reconcile_trim_claims`, a list of claim names that bounds the arm. An empty list
  trims nothing, and the arm never reads an empty list as "every volume". H2.2a sets it to one
  canary claim, and H2.2b deletes the key so the arm covers every volume the label rule admits.
  The canary is the smallest default-tier claim whose owner is a Deployment with one replica.
  The PR names it and quotes that reading, and the operator approves the choice in review.
  The blast radius is that one volume. The trim discards only blocks ext4 has already freed,
  and the worst backup outcome is one missed daily backup of that volume, which backup
  health's age check reports.
- **The unbounded rule is measured before it acts.** The bound keeps `trim` off every other
  volume whatever the rule decides, so the canary cannot test the rule. H2.2a's arm therefore
  also logs, on every run and without acting, the claims the unbounded rule would admit
  (`trim would-admit: <claims>`). H2.2b's body quotes that line beside a `kubectl get
  volumes.longhorn.io` reading of the same hour, and shows that the set excludes every
  detached, CronJob-only and scaled-to-zero volume.
- **The default backup group.** A volume that gains the trim label has "a job of its own",
  which stops Longhorn from adding `default`. longhorn-manager `v1.12.1`'s
  `datastore/longhorn.go:labelRecurringJobDefault` counts both `recurring-job.longhorn.io/`
  and `recurring-job-group.longhorn.io/` keys, so the per-job label does not avoid this.
  `longhorn-reconcile` therefore sets `recurring-job-group.longhorn.io/default` explicitly on
  every default-tier volume that carries the trim label. It never sets it on a `no-backup`
  volume or on a weekly-shard volume. The arm writes no other `recurring-job-group.longhorn.io/`
  key, so each volume keeps exactly one group label, the invariant
  `scripts/backup/longhorn_reap_lib/logic.py:abort_reason`'s rule 1 states.
- **The abort valve.** The native job runs inside longhorn-manager, so a manager that is not
  ready runs no trim at all. The moved contract states that in place of the script's check.
- **Evidence reads the job pod's log, not only its status.** A per-volume trim failure leaves
  the Job succeeded (Baseline, *Longhorn's recurring job swallows a per-volume failure*). The
  trim arms of checks 9 and 10 therefore read, through the read-only kubeconfig, the CronJob
  Longhorn creates for the RecurringJob, its newest Job, and that Job's pod log through
  `pods/log`, as check 11 does. A `Failed to run volume job` or `failed to complete
  filesystem-trim` line is a failure. A missing CronJob reads as not live. The expected cadence
  is that CronJob's `spec.schedule`, read at run time, not a baked key. The arms stop reading
  the `longhorn-trim` journal tag and cron file. Check 11 narrows `_SKIP_RE`'s matches to the
  backup RecurringJobs by job name, so a trim pod's skip line no longer reads as a backup skip.

### The drill: state into a ConfigMap first, then the move

The drill's state cannot live in a pod's filesystem: an ephemeral pod would see no attempts,
drill the first candidate every night, reset the retry bound and leave check 8 with no seen
entries to judge. Nor does it go on a hostPath: that would keep state on the host plane H7
packages, pin the drill to daniel-box, and keep gate 2 reading daniel-box's disk. The state
moves into a ConfigMap while the drill is still the host cron, so production exercises the
rewrite under the host cron's own evidence before anything moves.

- **The store.** `longhorn-restore-drill-stamps` in `longhorn_drill_namespace` (`homelab`),
  rendered empty in the `longhorn-restore-drill` tag. The host readers find it through
  `k3s_longhorn_restore_drill_namespace`, which the key-pair test holds equal (*Keys*). It carries two
  annotations. `kustomize.toolkit.fluxcd.io/ssa: IfNotPresent` makes Flux create it once and
  never revert the drill's writes. `kustomize.toolkit.fluxcd.io/prune: disabled` keeps it when
  a revert drops it from the render, because the tag prunes as every per-tag Kustomization
  does (C3). C3 lists that exemption for PVCs and Namespaces only, so *Open questions* 12
  asks for this one ConfigMap to join them. Its keys:
    - `last-success`, `candidates`, `excluded_oversize` and `excluded_nopvc`, each holding the
      file's content;
    - `attempts.<pvc>`, `retries.<pvc>` and `seen.<pvc>`, each holding as an epoch the mtime
      the file had;
    - `success.<pvc>`, holding the epoch the file held;
    - `imported-at`, the epoch of the one-time import below.
  A PVC name is a DNS subdomain, so each key matches a ConfigMap key's character set. Twenty-
  five candidates make about a hundred short keys, far below the 1 MiB limit.
- **The rewrite (H2.3c).** H2.3c rewrites the rotation, retry and seen logic rather than
  moving it. The drill reads the ConfigMap once per run with `get` and writes each stamp with a
  merge `patch`. A missing key is epoch 0, as a missing file is today. The attempt is still
  stamped before the restore, and a failed patch fails the run, so a run that cannot stamp
  restores nothing. `stat -c %Y` leaves the script.
- **A TERM trap (H2.3c).** The script installs only `trap cleanup EXIT` today. In a pod, bash
  runs as PID 1, which ignores a signal it has no handler for, so the kubelet's SIGTERM at
  `activeDeadlineSeconds` would do nothing and the SIGKILL after the grace period would skip
  `cleanup`. That would leave the `restore-drill-<pvc>` Volume, PV, PVC and probe pod behind.
  H2.3c adds `trap 'exit 143' TERM` and `trap 'exit 130' INT` beside the EXIT trap, so a
  signal runs `cleanup` through the exit. The trap goes in on the host first, where it also
  covers a shutdown. Bash runs a trap only after its foreground command returns, and the
  script's long waits are `sleep 10` and `sleep 5` polls, so the delay is one poll interval
  or one `kubectl` call.
- **A `mktemp` template BusyBox accepts (H2.3c).** The script creates its manifest with
  `mktemp /tmp/restore-drill.XXXXXX.yaml`. GNU `mktemp` accepts a suffix after the X's.
  BusyBox's `mktemp` hands the template to `mkstemp`, which requires it to end in `XXXXXX`,
  and `alpine/k8s` ships BusyBox (*Behaviour under the image, not only presence*). The script
  runs under `set -uo pipefail`, without `-e`, so an empty `MANIFEST` would reach the heredoc
  and the apply. Every run would then end in `could not create the drill volume` after
  stamping its attempt. H2.3c drops the suffix: `mktemp /tmp/restore-drill.XXXXXX`. kubectl
  filters by extension only while it walks a directory, and reads a file named on the command
  line whatever its name, so the host cron's behaviour does not change. The edit goes in on the
  host, so H2.3d stays a four-edit move.
- **The import.** On a run where the ConfigMap has no `imported-at`, the H2.3c script, still on
  daniel-box as root, reads `/var/lib/longhorn-restore-drill`, converts each item as above, and
  writes it with `imported-at` in one patch. A later run imports nothing. H2.3d deletes the
  import arm, because a pod cannot see the directory.
- **Readers (H2.3a, before the producer).** `longhorn_lib/longhorn_restore_drill_stamps.py`
  gains a ConfigMap arm that returns the shapes its file arm returns, `seen` and `success`
  included.
    - Backup health's checks 7 and 8, `runbook_gates_lib/longhorn_upgrade.py` and
      `scripts/docs/reference/state.py:longhorn_restore_drill_run` read the ConfigMap through
      the read-only kubeconfig. The shim gains `LONGHORN_RESTORE_DRILL_NAMESPACE`. `state.py`
      uses `lib.kubectl` with the cluster `prod`, as the runbook gate does, under the
      docs-refresh cron's `KUBECONFIG`. A failed read renders the row unreadable, never fresh.
    - While the ConfigMap holds no `last-success`, every reader falls back to the host
      directory. H2.3f deletes the fallback.
    - In the ConfigMap arm, a candidate with no `seen.<pvc>` takes the ConfigMap's
      `metadata.creationTimestamp` as its seen time, so check 8 flags it once the coverage
      window passes rather than skipping it.
    - Check 8 also reports a problem at once when `candidates` is non-empty and no candidate
      has a seen entry. That makes state that did not persist loud.
    - No reader treats "both absent" as a pass.
- **The move (H2.3d).** `git mv` puts the script in the `longhorn-restore-drill` role's
  `templates/`, rendered into a script ConfigMap. `git diff -M` shows four edits against the
  H2.3c version: `KUBECTL="kubectl"`, the import arm removed, each `logger -t
  longhorn-restore-drill` line echoed to stdout instead (*Evidence goes to stdout*), and the
  namespace fallback reading `longhorn_drill_namespace`. The CronJob ships with `suspend:
  true`. H2.3e flips it once the operator confirms the host cron is gone, so the one
  non-idempotent job never runs twice. Both runners name their restore
  `restore-drill-${PVC}`, and `cleanup()` deletes that name on every exit, so two concurrent
  runs could tear down each other's restore.
- **The deadline (H2.3d).** The CronJob's `activeDeadlineSeconds` is
  `k3s_longhorn_restore_drill_timeout_s` (2700) plus the probe pod's 300-second bound plus a
  600-second margin for the candidate query, the restore's setup and `cleanup`: 3600 seconds,
  far below the daily period. `terminationGracePeriodSeconds` is 300, which covers one
  10-second poll and the four `--ignore-not-found` deletes in `cleanup`, each of which waits
  for its object to go (*RBAC, derived from the script's calls*). Both are rendered from
  the role's defaults, and a render test holds the deadline above the timeout key plus the
  probe bound.
- **Keys.** These stay in `setup/k3s` defaults, because a host reader reads them:
    - `k3s_longhorn_restore_drill_max_age_days` and
      `k3s_longhorn_restore_drill_coverage_slack_days`, read by the shim and by
      `runbook_gates_lib/longhorn_upgrade.py:restore_drill_max_age_s`;
    - `k3s_longhorn_restore_drill_namespace`, which the shim exports from H2.3a;
    - `k3s_longhorn_restore_drill_stamp_dir`, until H2.3f drops the fallback, the shim's
      `LONGHORN_RESTORE_DRILL_STAMP_DIR` export and `longhorn_backup_health.py`'s
      `_require_env` of it.

  The other eight drill keys (`_cron`, `_image`, `_pvc`, `_min_bytes`, `_empty_ok_pvcs`,
  `_empty_ok_max_actual_bytes`, `_max_actual_bytes` and `_timeout_s`) move to the
  `longhorn-restore-drill` role's defaults under their names in H2.3d, beside the deadline
  and grace keys. That role gets its own namespace variable, `longhorn_drill_namespace`, and a test
  holds it equal to the `setup/k3s` one. The drill's absent arm names no schedule key. After
  H2.3d, no key defined in the new role's defaults appears in a `setup/` task or template. At
  `70591c307`, `git grep -l` for the eight keys under `ansible/roles/setup/` finds only
  `setup/k3s`'s defaults, `tasks/health-crons.yml` (the cron task H2.3d turns into the
  absent arm) and the drill template H2.3d moves. H1.7b's census row reads only H1's six
  Flux-born roles, so it does not read this one, and H2.3d does not need H1.7b. H2.3d
  therefore adds a census row of its own that holds the claim (*Verification*, *Drill keys off
  the host plane*). A change to a key that stays is owed as a hand apply. None goes to `group_vars/all.yml`,
  which no apply of `setup/k3s` follows.
- **RBAC, derived from the script's calls.** A ServiceAccount in `homelab`, bound to three
  grants:
    - a Role in `homelab`: `create`, `get`, `list`, `watch` and `delete` on `pods`; `get` on
      `pods/log`; `get`, `list`, `watch`, `create`, `patch` and `delete` on
      `persistentvolumeclaims`; `get` and `patch` on `configmaps`, with `resourceNames:
      [longhorn-restore-drill-stamps]`;
    - a Role in `longhorn-system`: `get` and `list` on `backups.longhorn.io`; `get` on
      `backuptargets.longhorn.io`; `get`, `list`, `watch`, `create`, `patch` and `delete` on
      `volumes.longhorn.io`;
    - a ClusterRole: `get`, `list`, `watch`, `create`, `patch` and `delete` on
      `persistentvolumes`.
  `list` and `watch` on each kind that `cleanup()` deletes keep `kubectl delete`'s wait
  (*Identity, derived from the script*). The probe pod is then gone before the PVC delete, the
  PVC before the PV, and the PV before the Volume, the order the script's teardown comment
  relies on.
  Kubernetes RBAC cannot narrow `create` by name, and `resourceNames` match exact names only,
  so the PV and Volume grants cannot be scoped to the drill's name prefix. They replace the
  root admin kubeconfig the drill uses today, which holds every verb. The `homelab` Role covers
  every claim the drill can pick today (Baseline). A backed-up claim in another namespace
  fails its slot as Forbidden, and check 8 reports it unproven after the coverage window. The
  RBAC-against-script test holds the set.
- **The image.** The shell-job image (*The pattern for one moved cron*), under
  `longhorn_restore_drill_runner_image`. The probe pod keeps `k3s_longhorn_restore_drill_image`.
- **Hand runs.** The host script's argument and `RESTORE_DRILL_PIN` stay. A plain hand run is
  `kubectl -n homelab create job --from=cronjob/longhorn-restore-drill <name>`, which works on a
  suspended CronJob. A pinned run cannot set the variable through `create job`, so the
  documented form renders the Job with `--dry-run=client -o json`, adds `RESTORE_DRILL_PIN` to
  the container's `env` with `jq`, and creates it. Both are **operator-only**, because the
  agent's kubeconfig cannot create, and both run outside the 04:10 UTC slot. A pinned run still
  stamps `success.<pvc>` and not `last-success`.

### The drill's own tag

The drill's objects in `homelab` cannot go in `longhorn`. H1's *Bring-up and DR order* waits,
at step 6, until `cluster-base`, `metallb`, `coredns`, `longhorn` and `longhorn-backup` read
`Ready`, and nothing creates `homelab` until step 8. At `70591c307`, the tasks `Build the
workload namespace manifest` and `Apply the workload namespace` in `ansible/deploy.yml` create
it, and D5's namespace PR later replaces them with the shared kind `homelab-namespace`. A
`homelab` object in `longhorn` would fail its apply on a fresh cluster with namespace not
found, `longhorn` would never read `Ready`, and step 6 would stall with `longhorn-backup`, the
agent join and the storage smoke test behind it.

- **The tag.** `longhorn-restore-drill` is a k8s role, `ansible/roles/k8s/longhorn-restore-drill/`,
  whose entry carries `owner: flux` and `depends_on: [longhorn]`. It renders the stamp
  ConfigMap (H2.3b) and, from H2.3d, the script ConfigMap, the CronJob, the ServiceAccount,
  both Roles, the ClusterRole and the three bindings. It renders no Namespace: a Namespace
  object here would be a second writer of `homelab` beside `ansible/deploy.yml`, which C4
  forbids.
- **Its place in the DR order.** It is not in step 6. Before D5's namespace PR, its
  Kustomization fails on a fresh cluster until step 8 creates `homelab`, and Flux's next retry
  applies it. From D5's namespace PR on, D5's rule that every flux-owned tag rendering into
  `homelab` gains a `dependsOn` on `homelab-namespace` generates its edge. Before H2.3b, this
  tag does not exist. *Open questions* 1 and the cross-slice note to H1 carry the order.
- **Not forward-only.** It is not one of H1's tags, so a failed revision follows C2's ordinary
  path: `DEPLOY_HOLD`, then a reset of `prod` to `last-good`. Drill churn stays out of the
  forward-only `longhorn` tag.
- **The guard.** A renderer test, beside H1.1c's per-tag object census, holds a literal set
  of the step-6 tags, with `longhorn` as its named member. For each of them it collects the
  namespaced objects the render carries and the Namespaces the step-6 renders create
  (`longhorn-system` and `metallb-system`), adds k3s's own (`default`, `kube-system`,
  `kube-public` and `kube-node-lease`), and fails when an object's namespace is in neither
  set. It asserts that it read all five tags, and it must find DaemonSet `longhorn-manager`
  in `longhorn-system`, H1's named member for `longhorn`. `longhorn-reconcile` is not a
  step-6 tag, so the guard does not read it.
- **Its census row.** The tag's named member in H1.1c's per-tag object census is the stamp
  ConfigMap, from H2.3b, and the CronJob joins it in H2.3d.

### CrowdSec

- **Three jobs on the API.** The remote allowlist, the bouncer prune and the AppSec verify
  become CronJobs in `crowdsec`, each under its own ServiceAccount. The grants, from each
  script's calls:
    - all three: in `homelab`, `get` on `deployments.apps`, `list` on `pods`, and `create` and
      `get` on `pods/exec`, for `exec deploy/crowdsec -c crowdsec -- cscli …`;
    - the remote allowlist also: `get` on `pods/log` in `homelab`, for `logs deploy/traefik -c
      access-log-rotate`, and a ClusterRole with `get` and `list` on
      `ingressroutes.traefik.io`, for `get ingressroute -A`. Routes live in three namespaces
      (Baseline). Narrowing the script to `homelab` would change which routers count as
      authelia-gated, so the grant follows the script.
  `remote_allowlist.py` and `bouncer_prune.py` ship in the ConfigMap. Their thin bash wrappers
  become Python entry points that push with `kuma_push.py`, since the Python image has no
  `jq`. The AppSec verify keeps its bash and `jq` and pushes through the staged
  `kuma-push-lib.sh`.
- **The home allowlist needs the host's IPv6.** The pod network is IPv4-only, so a pod there
  sees no global IPv6 address. Its CronJob therefore runs with `hostNetwork: true`,
  `dnsPolicy: ClusterFirstWithHostNet` and a `nodeSelector` for daniel-box, as node-exporter's
  DaemonSet does. Its grants are those of the three API jobs' `exec`. The consequences:
    - The namespace's `pod-security.kubernetes.io/warn: baseline` label records a warning on
      apply. No `enforce` label exists, so nothing refuses it.
    - A host-network pod leaves from the node's address, outside pod NetworkPolicy selection,
      as the host cron does today. It therefore pushes through Traefik to the `.local.`
      hostname with its inline `curl`, the host cron's path, and not to Kuma's Service, whose
      `netpol_from` fence would not admit the node address.
    - It therefore needs no `netpol_from` grant, and H2.4c gates it with `k8s/cronjob-gate`.
    - It reads the host's global IPv6 addresses from `/proc/net/if_inet6` in the shared
      network namespace.
- **The allowlist's state moves into the allowlist.** Each run reads the stored entries with
  `cscli allowlists inspect home-ips` in place of `/var/lib/crowdsec-home-allowlist/`. The rule
  stays: an empty or failed IPv6 discovery never removes a stored /64.
- **Window.** crowdsec is a home-edge role with `k8s_autodeploy: false`. H2.4b and H2.4c each
  land in a window, as D5's *Waves and windows* defines for that role.

### The *arr checks

- **configarr and janitorr health** become CronJobs that read their workload through their
  own ServiceAccount. Each grant set is derived from its module's calls:
    - `configarr_health.py:main` runs `get jobs -l` and `logs job/<name>`. It gets `get` and
      `list` on `jobs.batch`, `list` on `pods` and `get` on `pods/log`.
    - `janitorr_health.py:main` runs `get pod -l app=janitorr` and `logs deploy/janitorr`. It
      gets `get` on `deployments.apps`, `get` and `list` on `pods`, and `get` on `pods/log`.
  The RBAC-against-script test's recording runner covers both modules. Their status modules,
  `kuma_push.py`, and `host_lib.py` where a module imports it, ship in the role's ConfigMap,
  and each push token moves into the role's Secret (*No secret in a ConfigMap*). Each role's `install_host_lib.yml` and `stamp_deployed.yml` imports leave it, and
  each role gains absent arms for `/opt/<role>-health`, its cron script and its fragment under
  `setup-deployed-manifest.d`. Each role's membership of `_K8S_HOST_LIB` goes in its own PR,
  and the host-lib, `install_host_lib.yml` and `stamp_deployed.yml` rows go when the set is
  empty.
- **The qbittorrent prefs check** becomes a CronJob that runs `apply_prefs.py --dry-run`
  against the WebUI Service, with the WebUI credentials from the role's Secret. It stays
  log-only. qbittorrent's `netpol_from` admits it from the same commit, and the role applies
  that fence with the CronJob (*Who applies each grant*).
- **One PR per role** (H2.5b, H2.5c and H2.5d), so each reverts alone.

### Registry GC is D5's to retire

D5's Wave D registry bullet retires the GC in a conversion PR while registry is still
Ansible-owned. It sets `registry_k8s_manage_gc: false`, extends that lever's off arm to the
cron file, `/usr/local/bin/registry-gc.sh` and `/var/lib/registry-gc`, and removes the
`Registry GC` tile, the `registry-gc` Healthchecks.io entry and `registry_gc_push_token`'s
rotation entry. H2 does not move the GC, for two reasons:

- **An in-cluster orchestrator is still a second writer.** Its scale to zero writes the
  `spec.replicas` that registry's Deployment renders as `replicas: 1`. Once Flux owns
  registry, kustomize-controller corrects that drift on its next reconcile, which can land in
  the middle of a GC run. That is the corruption the script's "STOP-THE-WORLD" comment exists
  to prevent. Moving the script into a pod changes where the write comes from, not whether it
  conflicts.
- **D5 removes the need.** By Wave D every image consumer pulls from GHCR, so no build pushes
  to the in-cluster registry and nothing new needs collecting (D5).

Until D5's retirement PR, the host cron runs as it does today under the Ansible-owned registry
role. The flux-ownership census fails registry's flip while its present cron task remains.

### etcd-s3

This replacement needs H6.4b, for the drop-in and its windowed restart, and H1.7b, for the
Secret's tag.

- **The drop-in carries no S3 configuration but the two enabling keys.** H2.8a adds these to
  `k3s_server_config` in the `defaults:` of `hosts/components/k3s-server/component.yml`, which
  renders `config.yaml.d/50-homelab.yaml` (H6, *Flags move from the unit to a package
  drop-in*):
    - `etcd-s3: true` and `etcd-s3-config-secret: k3s-etcd-snapshot-s3-config`. Any other S3
      flag would make k3s ignore the Secret.
    - `etcd-snapshot-name: offbox`. Native snapshots then keep the `offbox-<node>-<ts>.zip`
      shape that both drills, `docs/break-glass.md` and `docs/k3s-etcd-restore.md` match.
    - `etcd-snapshot-compress: true`. Local snapshots shrink from 83–87 MB to about 15 MB.
    - `etcd-snapshot-schedule-cron: "0 */12 * * *"`, k3s's default written down on purpose.
      The local recovery point stays at 12 hours, and the off-box one improves from 24 hours
      to 12.
    - `etcd-snapshot-retention: 28`. Local copies rise from 5 to 28 at about the same bytes
      (28 × 15 MB against 5 × 85 MB). No `etcd-s3-retention` is set, so S3 keeps 28 as well:
      14 days off-box, the window `k3s_etcd_s3_retention` gives today. R2 then holds about
      420 MB of snapshots, about 210 MB more than today.
  None of these keys names a `secrets.yml` value. The drop-in ships with mode 0644, and an
  H2.8a test asserts that mode on the rendered package file. H6 makes no such commitment yet
  (*Open questions* 10).
- **The disarm lever.** `etcd-s3: false` in `k3s_server_config` disarms the native upload at
  the next window restart. Check 12 reads `etcd-s3` from the installed drop-in and reports
  DOWN with "disarmed" while it is false, as the script reports "DISARMED" today. Between
  H2.8a and H2.8b both writers upload, so a disarm in that window sets both
  `k3s_etcd_s3_armed: false` and `etcd-s3: false`. H2.8b deletes `k3s_etcd_s3_armed`, whose
  only reader is the upload it removes.
- **The Secret carries every S3 value.** `k3s-etcd-snapshot-s3-config` lives in `kube-system`,
  with type `etcd.k3s.cattle.io/s3-config-secret`, and `longhorn-backup` renders it. Its keys
  are `etcd-s3-endpoint` (`${r2_account_id}.r2.cloudflarestorage.com`), `etcd-s3-bucket`
  (`${r2_bucket}`), `etcd-s3-access-key` (`${r2_access_key_id}`), `etcd-s3-secret-key`
  (`${r2_secret_access_key}`), `etcd-s3-folder` (`etcd-snapshots`) and `etcd-s3-region`, from
  `k3s_longhorn_r2_region` in `longhorn-backup` defaults, the key its R2 BackupTarget reads.
  H1's plan does not move that key: a grep of its text for `r2_region` finds nothing. H2.8a
  therefore carries a gate (*Open questions* 11). If H1.7b has not defined the key in
  `longhorn-backup` defaults when H2.8a is ready, H2.8a adds `longhorn_backup_etcd_s3_region`
  there instead and the Secret reads it. No `setup/` file names that key, so H1.7b's census
  row passes, and H2.8a still touches no `setup/k3s` path. The off-box script keeps a region
  key of its own in `setup/k3s` defaults until H2.8b. If H1.7b moves `k3s_longhorn_r2_region`,
  its census row forbids that key in a `setup/` template, so H1.7b gives the script a twin
  with another name, held equal by a test. If it does not, the script keeps reading
  `k3s_longhorn_r2_region`. H2.8b cuts the script to the token comparison, which reads no
  region, and deletes that key and any test that holds it once the script is its last
  reader. A render test holds the Secret's
  `etcd-s3-region` equal to the region in the R2 BackupTarget's rendered
  `backupTargetURL`. The renderer's `kustomization.yaml`
  lists files and sets no `namespace:` (C1), so the Secret keeps `kube-system`; a render
  assertion holds both that and the absence of `spec.postBuild` on `flux/cluster-base.yaml`.
  k3s reads the Secret at each snapshot, not at start, so control-plane boot stays independent
  of `homelab-secrets` and the age key.
- **Effect on H1's bring-up and DR order.** `cluster-base` gains nothing and keeps no
  dependency on the secrets plane. Native S3 uploads start once `longhorn-backup` is Ready,
  after `longhorn`. A restore does not read the Secret (*Restores keep a host credential*), so
  the order costs only the first off-box snapshots after a rebuild, and check 12 reports them
  missing meanwhile. A failed revision that changes the Secret holds promotion without reset,
  because `longhorn-backup` is forward-only.
- **A host package never renders a secret.** A host-render test fails when any file under a
  host package's root names a `secrets.yml` key, either as a `${<key>}` placeholder or as a
  template variable. *Open questions* 5 proposes the same check for every package under C5.
- **Proof with no stage cluster (H2.8a and H2.8b).**
    - **The flags exist.** The installed `v1.37.1+k3s1` lists all six keys (Baseline). If H6
      has moved `k3s_version` when H2.8a lands, the PR reruns that `k3s server --help` grep
      against the new binary and quotes it.
    - **The first native S3 snapshot is a production event with a bounded blast radius.** The
      drop-in change takes the one restart that H6.4b's windowed unit gives any drop-in
      change. The off-box cron keeps writing until H2.8b, so a Secret that k3s ignores or
      cannot use costs no off-box recovery point. H2.8b merges only after `kubectl get
      etcdsnapshotfiles.k3s.cattle.io` lists an S3 snapshot from the 00:00 or 12:00
      schedule, and its PR body quotes that entry with its size.
    - **Whether native retention prunes the script's objects** is read on production. Both
      writers use the `offbox` name, but the script runs at 02:45 and the native schedule at
      00:00 and 12:00, so the creation time tells them apart. Fifteen days after H2.8b, when
      28 native snapshots postdate the last script object, a read-only `kubectl get
      etcdsnapshotfiles.k3s.cattle.io` shows whether any S3 entry created at 02:45 remains.
      If one does, native retention does not prune them, and the operator deletes them
      (H2.8c).
    - If a later `k3s_version` drops `--etcd-s3-config-secret`, the fallback is a
      secret-bearing drop-in, `60-etcd-s3.yaml`, rendered into `/run` by `homelab-secrets`
      (H4). `k3s.service` then gains `Wants=` and `After=` on `homelab-secrets.service`, and
      *Risks* records the boot dependency. H6's test that the package ships only
      `50-homelab.yaml` would then need amending (*Open questions* 10).
- **Restores keep a host credential.** The config Secret cannot serve a restore, and a restore
  runs when the API may be gone. `/etc/rancher/k3s/etcd-s3.env` therefore stays on daniel-box,
  rendered by `setup/k3s`. Its task is renamed to say restore credentials. It is the restore
  runbook's and the weekly drill's credential source until H4's `/run/credstore` takes it
  over in H7. Reading it is root-only, as today.
- **The drop-in reaches every restore on daniel-box.** k3s's `Reset` retrieves the snapshot
  from S3 whenever S3 is enabled and `--cluster-reset-restore-path` is set (`pkg/etcd/etcd.go`
  at `v1.37.1+k3s1`, read through `gh api`). With only the config Secret, it fails with
  `cannot use S3 config secret when restoring snapshot`. A restore from one of the 28 local
  snapshots therefore passes `--etcd-s3=false`. An S3 restore keeps its explicit
  `--etcd-s3-*` flags from the restore credential file above, which make k3s ignore the
  Secret, as `docs/k3s-etcd-restore.md` documents. H2.8a writes both into
  `docs/k3s-etcd-restore.md` and `docs/break-glass.md`.
- **The token comparison stays on daniel-box.** H2.8b cuts `etcd-snapshot-offbox.sh` down to
  the token comparison and renames it `etcd-token-drift.sh`, under a root cron `etcd token
  drift` at the same 02:45 UTC, from the same `k3s_etcd_s3_cron_*` keys.
    - It keeps the `Off-box etcd Snapshot` tile and the `etcd-snapshot-offbox` slug, so no
      Healthchecks.io check changes name or cadence.
    - The tile's `description` changes in `static-monitors.yaml.j2`.
    - **Operator-only:** edit the Healthchecks.io check's description to match.
    - It stays fail-closed: a missing stamp or an unreadable token reports DOWN.
    - It reads the cluster through nothing and changes no state, so it is no actuator. H2.8b
      removes the off-box actuator from the `setup/k3s` *Autonomous-role contract*.
    - `setup/k3s` owns it until H7 packages it.
- **Check 12 (H2.8b).** Backup health gains check 12. It reads the newest `ETCDSnapshotFile`
  with an S3 location and reports DOWN when that snapshot is older than the schedule's period
  plus two hours (14 hours), when none exists, or when the drop-in's `etcd-s3` is false. The
  bound derives from `etcd-snapshot-schedule-cron`, not a second constant. Check 12 reads that
  key from `/etc/rancher/k3s/config.yaml.d/50-homelab.yaml` at run time, so a schedule change
  in the package moves the bound with no `setup/k3s` apply. A missing file or key reports DOWN.
  A file that `sys_user` cannot read reports DOWN with its own message, `drop-in unreadable`,
  so a mode fault on the file or on `config.yaml.d` reads as what it is. The directory does not
  exist on daniel-box at `70591c307` (`stat` reports No such file or directory), and siblings
  under `/etc/rancher/k3s/` range from `drwxr-xr-x` to `drwx------`, so H6 must ship it at
  0755 (*Open questions* 10).
  Check 12 lands in H2.8b, after the native schedule has written to S3. Landing it with H2.8a
  would read the script's 02:45 uploads only, so it would report DOWN from about 16:45 to
  02:45 each day and page through backup health's slug.
- **The retention fragment.** H2.8a changes `gen_doc_fragments.py:_etcd_offbox_retention` to
  read `etcd-snapshot-retention` from `k3s_server_config` in the component's defaults, and
  `render_etcd_offbox_retention` to name that key. The fragment regenerates to 28 in the same
  PR, so deleting `k3s_etcd_s3_retention` in H2.8b raises no `KeyError`.
- **The marker.** H2.8b rewrites `DELIBERATELY A CRON, NOT A SERVER FLAG` in `setup/k3s`
  defaults, because H6's windowed unit applies the drop-in change with one gated restart. The
  edit goes in H2.8b, not H2.8a, so H2.8a touches no `setup/k3s` path and owes no hand apply.

### Backup health and the B2 pair stay on the host

This is a decision, and *Open questions* 3 records the condition that reopens it.

- **The B2 pair.** Moving it needs a durable home that every writer and `probe.py b2-spend`
  share, an image carrying the multi-module probe package, and a Loki grant. Keeping it costs
  H7 one more cron family.
- **Backup health follows the B2 pair.** Its B2 arm reads that pair's journal lines and cron
  files. Its two Healthchecks.io slugs and its boot grace are host-shaped. It already reads the
  cluster through the read-only kubeconfig, whose grants cover every new read this slice adds:
  the trim Job and its pod log, the stamp ConfigMap and `ETCDSnapshotFile`. Its marker
  `DECIDED a host cron, not a monitor-bridge check (#3287)` stands.
- **It bakes no key whose other reader Flux renders.** The decision rests on H1.7a: backup
  health reads the armed state from each BackupTarget's `backupTargetURL` and the schedules
  from the RecurringJobs' `cron` at run time, so a disarm made in the cluster changes the
  verdict with no `setup/k3s` apply. H1.7b then moves the five shared keys into
  `longhorn-backup`, and its census row keeps every Flux-born key off `setup/`. H2's own
  additions follow the same rule:
    - the trim arm reads its CronJob's schedule;
    - the drill arms read the stamp ConfigMap;
    - check 12 reads the drop-in;
    - the drill's two bounds, its namespace and its stamp directory stay in `setup/k3s`
      defaults, whose changes are owed as a hand apply.
- **What changes in it.** The trim arms and check 11's narrowing (H2.2c), the drill arms
  (H2.3a and H2.3f) and check 12 (H2.8b).

### The reapers stay unscheduled

The reaper census gains a CronJob arm. It renders every k8s role's CronJobs and fails when a
CronJob's command, or the data of any ConfigMap the CronJob mounts, names `longhorn_reap.py`.
Its red fixture is a reaper call inside a mounted script, so a scheduled reaper hidden in a
script ConfigMap is caught. It asserts that it rendered `pi-peer-backup`'s CronJob and the
drill's script ConfigMap, so an empty render cannot pass. H2.3d moves the setup arm's named
member in `test_census_finds_the_scheduled_drill` from `longhorn-restore-drill.sh`, which
leaves `setup/`, to `longhorn-backup-health.sh`, which stays until H7.

### File tree

```diff
  ansible/tests/repo/test_census_rows_roles.py         (+ flux-ownership and retired-cron rows)
  ansible/tests/longhorn/test_longhorn_reap_never_scheduled.py  (+ CronJob and mounted-ConfigMap arm)
  ansible/tests/k8s/                                   (+ RBAC-against-script test, no-secret-in-ConfigMap test)
  ansible/roles/k8s/longhorn/templates/                (+ trim RecurringJob)
  ansible/roles/k8s/longhorn-reconcile/files/          (+ longhorn_reconcile.py trim and default arms)
  ansible/roles/k8s/longhorn-reconcile/tests/          (+ trim, default, bound and RBAC-subset cases)
  ansible/tests/longhorn/_restore_drill.py             (+ persisting fake API, image leg)
+ ansible/roles/k8s/longhorn-restore-drill/templates/  (stamp and script ConfigMaps, drill CronJob, RBAC)
+ ansible/roles/k8s/longhorn-restore-drill/defaults/main.yml  (the eight moved keys, namespace copy, runner image, deadline, grace)
+ ansible/roles/k8s/longhorn-restore-drill/CLAUDE.md   (the drill's contract and DECIDED markers)
  ansible/inventory/host_vars/daniel-box.yml           (+ longhorn-restore-drill entry, owner: flux)
  ansible/roles/k8s/longhorn-backup/templates/         (+ the etcd-s3 config Secret)
  ansible/roles/k8s/crowdsec/templates/                (+ four CronJobs, RBAC, ConfigMap)
  ansible/roles/k8s/crowdsec/files/                    (+ Python entry points for the two Python jobs)
  ansible/roles/k8s/configarr/templates/               (+ health CronJob)
  ansible/roles/k8s/janitorr/templates/                (+ health CronJob)
  ansible/roles/k8s/qbittorrent/templates/             (+ prefs CronJob)
  ansible/roles/k8s/*/tasks/main.yml                   (the six roles' cron and host-file tasks become absent arms or move)
  ansible/inventory/host_vars/daniel-box.yml           (+ netpol_from entries on uptime-kuma and qbittorrent, each in its CronJob's commit)
  ansible/roles/k8s/monitor-bridge/files/checks/logs.py       (SWALLOWED_VERDICTS_POD_LOGQL names the moved pushers)
  ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py  (_K8S_HOST_LIB shrinks to empty)
  ansible/roles/setup/claude_code/tasks/               (+ artifacts sync, telemetry health crons, daniel-box only)
  ansible/roles/setup/claude_code/defaults/main.yml    (+ claude_code_artifacts_node, the sync and telemetry variables)
  ansible/roles/setup/k3s/tasks/health-crons.yml       (trim, drill, off-box cron: absent arms; + etcd token drift cron)
  ansible/roles/setup/k3s/files/                       (backup-health trim and drill arms, check 11 narrowing, check 12)
  ansible/roles/setup/k3s/defaults/main.yml            (- eight drill keys, two etcd keys, two stamp-group entries)
- ansible/roles/setup/k3s/templates/longhorn-trim-volumes.sh.j2
- ansible/roles/setup/k3s/templates/longhorn-restore-drill.sh.j2   (git mv to the longhorn-restore-drill role)
  ansible/roles/setup/k3s/templates/etcd-snapshot-offbox.sh.j2     (cut to the token comparison, renamed)
  scripts/deploy_tools/runbook_gates_lib/longhorn_upgrade.py       (reads the stamp ConfigMap)
  scripts/docs/reference/state.py                      (reads the stamp ConfigMap; cadence from longhorn defaults)
  scripts/docs/gen_doc_fragments.py                    (retention from k3s_server_config)
  hosts/components/k3s-server/component.yml            (+ etcd keys in k3s_server_config, no S3 values)
```

## Steps, as PRs

H2.6 is retired, because registry GC is D5's (*Registry GC is D5's to retire*). H2.4a and H2.5a
are retired too: each `netpol_from` grant lands in its CronJob's commit (Design, *Each grant
lands with its CronJob*). The drill runs
as H2.3a to H2.3f, and the *arr checks as H2.5b to H2.5d, one PR per role. Every `setup/k3s`
hand apply below is **operator-only** and follows the owed-line procedure (*Who writes each
change at merge time*).

1. **H2.1 The censuses.**
    - Add the flux-ownership row, the retired-cron row (crons and fragments), the reaper
      census's CronJob and mounted-ConfigMap arm, and the RBAC-against-script test with its
      fixture cases.
    - Add the trim-label fixtures to the drill's and the reapers' tests (*Verification*, *The
      trim label is no tier*). They pass at `70591c307`, because no reader reads a per-job
      key, and they land before H2.2a so that a reader widened later fails them.
    - Confirm that none of the six roles carries `owner: flux`, and quote the grep in the PR
      body.
2. **H2.2a Native trim, one canary (Flux).** This PR needs H1.5a and H1.6.
    - Add the `trim` RecurringJob, with `groups: []` and its `DECIDED` marker, to
      `longhorn`. Add the trim-label and explicit-`default` arms to the
      `longhorn-reconcile` role, `ansible/roles/k8s/longhorn-reconcile/`, with their unit
      tests and the reconcile-RBAC subset test in its `tests/`. The arm writes
      `recurring-job.longhorn.io/trim` and never `recurring-job-group.longhorn.io/trim`
      (Design, *A per-job label, not a group label*). The PR spans two tags:
      `longhorn` is forward-only, and `longhorn-reconcile` is not.
      `longhorn_reconcile_trim_claims` holds the one canary claim, and the arm logs its
      `trim would-admit:` line (Design, *Native trim*).
    - No trim RecurringJob exists before this PR promotes, so nothing is measured before the
      merge. That a per-volume failure leaves the Job succeeded rests on the source reading
      (Baseline) and on the trim-evidence red proofs, which feed the arm a succeeded Job whose
      log holds a failure line. A healthy canary produces no failure line to read.
    - After the promote, record with the read-only `kubectl` every volume whose
      `status.state` is `detached` before the next 06:10 UTC run. After the run, read two
      things:
        - each of those volumes is still `detached` and carries no trim label;
        - the canary carries the trim label and `default`, and no other group key; the trim
          Job's pod log shows
          `Finished recurring filesystem trim` for it, and the next `daily-backup` run created
          a backup of the canary.
    - The host trim keeps running, and a second trim of the same day is harmless.
3. **H2.2b Native trim, every eligible volume (Flux).**
    - Delete `longhorn_reconcile_trim_claims` and the bound it sets. The PR touches only the
      `longhorn-reconcile` tag, so a failed revision resets `prod` rather than holding.
    - Merge only after H2.2a's post-promote readings hold. They are this PR's gate, and the
      `Finished recurring filesystem trim` line proves that the native job reached the canary
      and trimmed it. The PR body quotes them, and quotes the `trim would-admit:` set from the
      last reconcile pass before 06:10 UTC beside a volume reading taken at the run, showing
      it excludes every detached, CronJob-only and scaled-to-zero volume. A volume the line
      admits that was detached at the run is a hit of the accepted window (*Risks*), and the
      body names it.
4. **H2.2c Native trim, host half.**
    - Switch the trim arms of checks 9 and 10 to the RecurringJob's CronJob, Job and pod log,
      and narrow check 11.
    - Turn `Longhorn filesystem trim` into an absent arm, delete
      `longhorn-trim-volumes.sh.j2`, and drop it from `k3s_render_stamp_groups`.
    - Hand apply: `k3s-bringup.yml --tags backup-health` (**operator-only**).
    - The two cron-evidence tests change, and their node ids in
      `ansible/roles/setup/k3s/CLAUDE.md` change with them. Until G2 retires
      `docs/facts.lock`, run `uv run python scripts/dev/fact_status.py verify` for the edited
      section and commit the lock.
5. **H2.3a Drill readers first.**
    - Add the ConfigMap arm, `seen` and `success` included, to the stamps module, checks 7 and
      8, `runbook_gates_lib/longhorn_upgrade.py` and `state.py`, each with its host-directory
      fallback. Add check 8's empty-seen guard and the creation-time rule for a missing seen
      entry. Export `LONGHORN_RESTORE_DRILL_NAMESPACE` from the shim.
    - Hand apply: `--tags backup-health` (**operator-only**). Nothing writes the ConfigMap
      yet, so readings do not change.
6. **H2.3b The drill's tag and its stamp ConfigMap (Flux).** This PR needs H1.5a.
    - Gate: C3 carries the prune exemption that *Open questions* 12 requests, or
      contracts.md's *Requested amendments not yet applied* register lists it with this gate,
      before this PR merges.
    - Add the `longhorn-restore-drill` role and its `containers_list` entry with `owner: flux`
      and `depends_on: [longhorn]` (Design, *The drill's own tag*).
    - Render `longhorn-restore-drill-stamps` empty in it, with `ssa: IfNotPresent` and `prune:
      disabled`.
    - Add the tag's rows to H1.1c's per-tag object census, with the ConfigMap as the named
      member, and add the step-6 namespace test.
    - **The added entry reaches four readers of the list.** D2.1c records an added Flux-born
      entry by its fields, so `deploy_tags.py narrow` names `longhorn-restore-drill` and
      the readers of those fields: artifacts, blackbox-exporter, pihole and uptime-kuma,
      measured at `a5abf8581`. D2.2 drops the drill's tag because Flux owns it,
      so the tick applies the four, unattended and forward-only. Three of them are
      denylisted: blackbox-exporter, pihole and uptime-kuma. D2.1c is on `origin/master` before this PR, because H2 needs
      H1 and H1 needs D2. No output is accepted as a reason to skip the stop rule, which
      binds until D6.12.
    - Before the PR, and again right before `land.sh`, run *Landing a `containers_list`
      edit* (Design): the narrow quote, the stop rule over every tag the output names, the
      `--dry-run` quote without the drill's tag, and D1.2's landing paths.
7. **H2.3c Drill state into the ConfigMap, on the host.** This PR needs H2.3b, live.
    - Precondition, read before the hand apply: `kubectl -n homelab get configmap
      longhorn-restore-drill-stamps` succeeds. A merged H2.3b is not enough, because a held
      promote leaves the ConfigMap absent, and the rewritten script would then fail every run.
    - Rewrite the drill's state handling to the ConfigMap keys, with the one-time import arm
      (Design, *The drill: state into a ConfigMap first, then the move*). The import arm fails
      with its own message, `stamp ConfigMap absent`, when the `get` returns NotFound. Add the
      TERM and INT traps, and drop the `.yaml` suffix from the `mktemp` template (Design, *A
      `mktemp` template BusyBox accepts*). The script keeps `logger`, `k3s kubectl` and its
      cron.
    - Hand apply: `--tags backup-health` (**operator-only**).
    - The PR body quotes, after the next 04:10 UTC run, the ConfigMap's key count beside the
      host directory's file count, and the run's `PASS` line.
8. **H2.3d The drill moves to a CronJob (Flux).** This PR needs H2.3b and H2.3c.
    - `git mv` the script into the `longhorn-restore-drill` role, with the four edits `git diff
      -M` shows. Add the CronJob with `suspend: true`, its deadline and grace period, its RBAC
      and its runner image key.
    - Move the eight keys and add `longhorn_drill_namespace` with its equality test, and the
      deadline render test.
    - Add the census row that keeps the drill role's keys off the host plane (*Verification*,
      *Drill keys off the host plane*). It is a row of its own because H2.3d does not need
      H1.7b, so H1's row may not exist yet.
    - Add the drill tests' image leg under the pinned runner digest, with the suffixed
      `mktemp` red case (Design, *Behaviour under the image, not only presence*).
    - Turn `Longhorn restore drill` into an absent arm with no schedule fields, with an
      absent arm for `/usr/local/bin/longhorn-restore-drill.sh`, and drop the template from
      `k3s_render_stamp_groups`. Hand apply: `--tags backup-health` (**operator-only**).
    - Move the drill's contract and its `DECIDED` markers into the `longhorn-restore-drill`
      role's `CLAUDE.md`.
    - Most of the diff is moves. The new lines are the CronJob, the three RBAC objects and
      the tests.
9. **H2.3e Unsuspend (Flux).** After the operator confirms that
   `/etc/cron.d/longhorn-restore-drill` is gone (**operator-only**, root), flip `suspend:
   false`. The PR stays open for the first scheduled run's Job name, `status.succeeded` and
   log tail, and for two consecutive runs that drilled different volumes.
10. **H2.3f Drop the fallback.** After the CronJob has written a `last-success`, delete the
    host-directory fallback from every reader, `k3s_longhorn_restore_drill_stamp_dir`, the
    shim's export of it and its `_require_env`. Hand apply: `--tags backup-health`
    (**operator-only**). A one-time `rm -r /var/lib/longhorn-restore-drill` follows
    (**operator-only**, root).
11. **H2.4b CrowdSec, three API-only jobs** (Ansible, in a home-edge window).
    - **As landed:** a `crowdsec-jobs` sidecar of the LAPI pod, not three CronJobs, by the
      operator's ruling in *Open questions* 2. It needs no exec grant. Its ServiceAccount
      keeps the remote allowlist's `pods/log` and IngressRoute reads, and uptime-kuma's
      `netpol_from` admits `crowdsec`, the pod's label. The bullets below are the CronJob
      plan the ruling replaced, kept for the review trail.
    - Gate: the operator has decided *Open questions* 2 for each of the three jobs.
    - Add the remote allowlist, bouncer prune and AppSec verify CronJobs with their RBAC,
      images and the two Python entry points. In the same commit, add the three jobs' labels
      to uptime-kuma's `netpol_from`.
    - Add the AppSec verify's script harness with its image leg.
    - Move `crowdsec_remote_allowlist_push_token` and `monitor_bridge_appsec_push_token` into
      the role's Secret, read through `secretKeyRef` (Design, *No secret in a ConfigMap*).
      The first of H2.4b, H2.4c and H2.5b to H2.5d to land adds the no-secret-in-ConfigMap
      test with its red fixture, and each later one adds its role to the test's list.
    - Turn their three cron and script tasks into absent arms.
    - Gate none of the three (Design, *First run*). The PR stays open for each job's first
      scheduled run.
    - The `netpol_from` edit makes the tick apply crowdsec, uptime-kuma and
      netpol-baseline, two of them denylisted. Before the PR, and
      again right before `land.sh`, run *Landing a `containers_list` edit* (Design).
    - *Open questions* 2 is decided here.
12. **H2.4c CrowdSec home allowlist** (Ansible, in a home-edge window).
    - **Blocked on #5701.** The sidecar form the operator ruled has no IPv6, so the home /64
      cannot be discovered from it, and the operator picks the form.
    - Gate: the operator has decided *Open questions* 2 for the home allowlist.
    - Add the host-network CronJob, with its state kept in the allowlist itself, and
      `monitor_bridge_home_allowlist_push_token` in the role's Secret. It needs no
      `netpol_from` grant, and it is gated with `k8s/cronjob-gate`. Its tests gain the image
      leg.
    - Turn the home allowlist's cron and script tasks into absent arms, with one for
      `/var/lib/crowdsec-home-allowlist`.
13. **H2.5b configarr health** (Ansible).
    - Add the CronJob, gated with `k8s/cronjob-gate`, with
      `monitor_bridge_configarr_push_token` in the role's Secret, and its label in
      uptime-kuma's `netpol_from` in the same commit.
    - The landing's gate run pushes into the closed fence (Design, *First run*). Its
      `activeDeadlineSeconds` stays above `kuma_push`'s three attempts and below
      `cronjob_gate_timeout`. The PR body records the first scheduled run's push.
    - Remove the `install_host_lib.yml` and `stamp_deployed.yml` imports, and configarr from
      `_K8S_HOST_LIB`.
    - Absent arms for the cron, `/opt/configarr-health`, the script and the `configarr`
      fragment.
    - The `netpol_from` edit makes the tick apply configarr, uptime-kuma and
      netpol-baseline, one of them denylisted. Before the PR, and again right before `land.sh`, run *Landing a
      `containers_list` edit* (Design). `_K8S_HOST_LIB` lives in
      `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py`, a
      setup-plane path. `broad.py:_changed_half` leaves it to its own `initial_setup.yml`
      plan, so it adds no tag to the narrow output. H2.5c's edit of the same set behaves the
      same way.
14. **H2.5c janitorr health** (Ansible). The same as H2.5b for janitorr, with
    `monitor_bridge_janitorr_push_token` in the role's Secret, except that the CronJob is not
    gated, because janitorr sets `k8s_autodeploy: false`. The PR body records the first
    scheduled run. The host-lib,
    `install_host_lib.yml` and `stamp_deployed.yml` rows of `K8S_ROLES_IMPORTING_SETUP_FILES`
    go with the emptied set. The tick applies janitorr, uptime-kuma and netpol-baseline,
    two of them denylisted, so the PR runs *Landing a `containers_list` edit* (Design) before the PR and
    again right before `land.sh`.
15. **H2.5d qbittorrent prefs check** (Ansible). Add the CronJob, gated with
    `k8s/cronjob-gate`, reading `qbittorrent_password` from the role's Secret, with the
    connect retry (Design, *Who applies each grant*). In the same commit, add its label to
    qbittorrent's `netpol_from`, which the role applies with the CronJob, before the gate.
    Turn the cron and its script into absent arms. The tick applies qbittorrent and
    netpol-baseline, one of them denylisted, so the PR runs *Landing a `containers_list` edit*
    (Design) before the PR and again right before `land.sh`.
16. **H2.7 Re-home the two Claude crons.**
    - Move `Sync peer Claude artifacts` and `Claude Code telemetry health` into
      `ansible/roles/setup/claude_code/`, keeping their cron files and script paths.
    - Gate both tasks on `inventory_hostname` equal to `claude_code_artifacts_node`, a new
      `claude_code` default. A test holds it equal to `artifacts_k8s_node`, which stays in the
      artifacts role. A change then edits both, and the `claude_code` half is a setup-plane
      change that the tick applies. A shared key in `group_vars/all.yml` would route as
      deploy-plane only and never re-apply `claude_code` (Baseline). The sync and telemetry
      variables move into `claude_code` defaults.
    - On daniel-server, the gate skips both tasks. daniel-server holds no copy today, so a
      skip leaves nothing behind.
    - The tick applies `initial_setup.yml --tags claude_code`, and the PR body quotes that
      run.
    - D5 wave D waits on this PR for artifacts and observability, so D5's conversion PRs find
      only artifacts' three directory tasks (*What H2 leaves in `k8s_host_files`*).
17. **H2.8a etcd-s3 on.** This step needs H1.7b and H6.4b.
    - Gate: `longhorn-backup` defaults define `k3s_longhorn_r2_region` before this PR merges.
      If they do not, the PR adds `longhorn_backup_etcd_s3_region` there and the render test
      that holds it equal to the R2 BackupTarget's region (*Open questions* 11).
    - Add the etcd keys to `k3s_server_config`, the Secret to `longhorn-backup`, the
      host-package secret test, the drop-in mode test, the render assertions on the Secret's
      namespace and on `cluster-base`'s missing `postBuild`, and the retention fragment's new
      source.
    - Keep the off-box cron. This PR touches no `setup/k3s` path.
    - Add the restore flags to `docs/k3s-etcd-restore.md` and `docs/break-glass.md`: a local
      snapshot restore needs `--etcd-s3=false` (Design, *The drop-in reaches every restore on
      daniel-box*).
    - The next window restarts k3s once.
    - If `k3s_version` has moved since the baseline, the PR body quotes the `k3s server
      --help` grep against the new binary.
18. **H2.8b etcd-s3, off-box upload retired.**
    - Merge only after `kubectl get etcdsnapshotfiles.k3s.cattle.io` lists an S3 snapshot from
      the 00:00 or 12:00 schedule. The PR body quotes that entry with its size.
    - Add check 12. Cut the script down to the token comparison, and turn the `Off-box etcd
      snapshot` cron into an absent arm. Delete `k3s_etcd_s3_armed` and
      `k3s_etcd_s3_retention`, and rewrite the defaults marker.
    - Delete the region key the off-box script reads from `setup/k3s` defaults, and any test
      that holds it equal to `longhorn-backup`'s region. That key is
      `k3s_longhorn_r2_region` at `70591c307`, and the twin that H1.7b names once H1.7b has
      landed (*Open questions* 11). The token comparison reads no region. Before deleting,
      the PR body quotes `git grep -n <the key> -- ansible/ scripts/` showing the off-box
      script and the key's definition as its only hits. If any other reader remains, such as
      the moved BackupTarget template in `longhorn-backup`, H2.8b leaves the key in place.
    - Remove the off-box actuator from the `setup/k3s` contract. Until G2 retires
      `docs/facts.lock`, run `fact_status.py verify` for that section and commit the lock.
    - Hand apply: `k3s-bringup.yml --tags backup-health,etcd-snapshot` (**operator-only**).
      The PR thread then quotes check 12's first UP reading.
    - **Operator-only**, root: delete the five orphaned local `etcd-snapshot-daniel-box-*`
      snapshots with `k3s etcd-snapshot delete`.
19. **H2.8c The retention reading.** Fifteen days after H2.8b, read `kubectl get
    etcdsnapshotfiles.k3s.cattle.io` for any S3 entry created at 02:45 UTC.
    - If none remains, native retention prunes the script's objects. This docs-only PR records
      that in `docs/k3s-etcd-restore.md`.
    - If one remains, the same PR records that native retention does not prune them.
      **Operator-only**, root: delete those `offbox-*` objects with `k3s etcd-snapshot
      delete`, using the restore credentials that *Restores keep a host credential* names.

## Verification

```bash
# After each family PR: the replacement exists and has run.
kubectl get cronjobs -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,SCHEDULE:.spec.schedule,TZ:.spec.timeZone,SUSPEND:.spec.suspend,LAST:.status.lastSuccessfulTime

# Operator-only (root): the retired cron files are gone.
sudo ls /etc/cron.d/

# H2.2a and H2.2b: each volume's state with its trim and default labels. Run it before and
# after the 06:10 UTC trim; a volume detached before it must still be detached after it.
kubectl -n longhorn-system get recurringjobs.longhorn.io
kubectl -n longhorn-system get volumes.longhorn.io -L recurring-job.longhorn.io/trim -L recurring-job-group.longhorn.io/default
# H2.2a: the trim job's per-volume lines, and the canary's newest backups.
kubectl -n longhorn-system logs -l recurring-job.longhorn.io=trim --prefix --tail=-1 \
  | grep -E 'would-admit|Finished recurring filesystem trim|Failed to run volume job'
kubectl -n longhorn-system get backups.longhorn.io --sort-by=.metadata.creationTimestamp \
  -o custom-columns=VOLUME:.status.volumeName,CREATED:.metadata.creationTimestamp \
  | grep '<canary volume>' | tail -n 2

# H2.3b, H2.4b and H2.5b to H2.5d, and each one's revert, before D6.12: the tags the tick
# applies, then the stop rule. After the landing, the second command names none of them.
uv run python scripts/deploy_tools/deploy_tags.py narrow origin/master HEAD
uv run python scripts/diagnostics/probe.py releases --stale-only
uv run python scripts/diagnostics/probe.py releases

# H2.3c to H2.3f: the drill's state.
kubectl -n homelab get configmap longhorn-restore-drill-stamps -o jsonpath='{.data.last-success}'
kubectl -n homelab get configmap longhorn-restore-drill-stamps -o json \
  | jq -r '.data | keys[]' | cut -d. -f1 | sort | uniq -c
uv run python scripts/diagnostics/probe.py loki-query '{container="longhorn-restore-drill"} |= "PASS"' --since 48h

# H2.8a to H2.8c: off-box snapshots, with the bucket name redacted. H2.8c reads the CREATED
# column for 02:45 UTC entries.
kubectl get etcdsnapshotfiles.k3s.cattle.io --sort-by=.metadata.creationTimestamp \
  -o custom-columns=NAME:.spec.snapshotName,LOC:.spec.location,CREATED:.metadata.creationTimestamp,SIZE:.status.size \
  | sed -E 's#s3://[^/]+/#s3://<bucket>/#' | tail -n 4
uv run python scripts/diagnostics/probe.py monitors | grep -iE 'longhorn backup|etcd'

# Exit part 3: the censuses.
uv run pytest ansible/tests/repo/test_census_rows_roles.py ansible/tests/longhorn/test_longhorn_reap_never_scheduled.py
```

A CronJob has no rollout. Where `k8s/cronjob-gate` does not apply, the PR body records the
first scheduled run: the Job name, its `status.succeeded` and its log tail. A daily job's PR
stays open for that evidence.

**Red proofs for the checks this slice adds.**

- *Flux-ownership census.*
    - A fixture `owner: flux` crowdsec entry with its present home-allowlist cron is flagged.
      That fixture is the named member, and it persists.
    - The same role with `state: absent` is clean.
    - A `delegate_to` copy into `/usr/local/bin/` is flagged, and a copy into
      `/etc/rancher/k3s/` is clean.
    - A fixture `owner: flux` janitorr that still imports `stamp_deployed.yml` is flagged.
    - The census asserts that it read as many roles as `containers_list` has k8s entries.
- *Retired-cron census.* A fixture role that deletes a retired cron's task, where it should
  flip it to absent, is flagged. A fixture role that drops its `stamp_deployed.yml` import with
  no absent arm for its fragment is flagged. traefik's `Cloudflare IP drift` and the
  `k3s-longhorn-reap` fragment are found.
- *RBAC against the script.* A fixture drill line `$KUBECTL -n longhorn-system get backuptarget
  x` with no `backuptargets` grant is flagged, and the real drill's `get backuptarget` is
  found. A fixture `apply` heredoc with a `kind: PersistentVolume` and no `patch` on
  `persistentvolumes` is flagged. A recorded `get ingressroute -A` with only a namespaced Role
  is flagged. A recorded `logs deploy/janitorr` against a Role with no `deployments.apps`
  grant is flagged. A fixture `delete pvc` line whose Role grants `delete` but not `list` and
  `watch` is flagged, and the real drill's `delete volume` finds `watch` on
  `volumes.longhorn.io`.
- *Step-6 namespaces.* A fixture `longhorn` render that carries a ConfigMap in `homelab` is
  flagged, because no step-6 tag and no k3s default creates `homelab`. The real `longhorn`
  render is clean, and its DaemonSet `longhorn-manager` is found.
- *No secret in a ConfigMap.* A fixture ConfigMap whose script holds `{{
  monitor_bridge_appsec_push_token }}` is flagged, because the render puts that key's sentinel
  in its data. The same value in a Secret is clean. The test asserts that it rendered at
  least one ConfigMap per listed role.
- *Reaper census, CronJob arm.* A fixture CronJob that runs `scripts/backup/longhorn_reap.py
  backups` is flagged, and so is one whose mounted script ConfigMap calls it. A CronJob that
  names neither is clean, and `pi-peer-backup`'s CronJob must be among those rendered.
- *Trim evidence arm.* A succeeded trim Job whose pod log holds `Failed to run volume job` is
  flagged. A RecurringJob Job that failed is flagged. A missing RecurringJob CronJob is flagged
  as not live.
- *Check 11.* A trim pod's skip line is ignored, and a `daily-backup` pod's skip line for a
  weekly volume is flagged.
- *`longhorn-reconcile`.* Each of these is a separate case:
    - a default-tier volume gains `trim` and keeps `default`;
    - a `no-backup` volume and a weekly-shard volume never gain `default`;
    - a volume whose only workload entry is `Job`, and a detached `ReplicaSet` volume, never
      gain `trim`;
    - with `longhorn_reconcile_trim_claims` set to one claim, no other volume gains `trim`, an
      empty list gives `trim` to no volume, and the `would-admit` line still names every
      volume the unbounded rule admits;
    - the arm's patches name `recurring-job.longhorn.io/trim` and no
      `recurring-job-group.longhorn.io/` key other than `default`. A fixture arm that writes
      `recurring-job-group.longhorn.io/trim` fails the case;
    - an arm that reads a resource outside the reconcile RBAC fails the subset test.
- *The trim label is no tier (H2.1).* Each fixture's red half gives the same volume
  `recurring-job-group.longhorn.io/trim` in place of the per-job label, and the reader then
  misreads it. Each reaper fixture writes its labels in sorted key order, the order kubectl's
  JSON emits, because the owner maps keep the last key they meet. A fixture that wrote `trim`
  before `default` would leave the red half's owner at `default`, and the red half could never
  fail:
    - in `ansible/tests/longhorn/test_longhorn_restore_drill_oversize.py`, a `no-backup`
      volume that carries the trim label and has a Completed backup above the cap stays out of
      OVERSIZE, ELIGIBLE and NOPVC. The red half lands in OVERSIZE;
    - in `scripts/backup/tests/test_longhorn_reap_logic.py`, a `default` volume that carries
      the trim label keeps `default` as its `backup_owner_map` owner, and its current-tier
      backups still count in `classify_backups`. The red half's owner is `trim`;
    - in `scripts/backup/tests/test_longhorn_reap_opt_out_snapshots.py`, a `no-backup` volume
      that carries the trim label keeps `OWNER_NO_JOB_BY_DESIGN` in `snapshot_owner_map`. The
      red half loses the sentinel.
- *Drill keys off the host plane (H2.3d).* A `Census` row of
  `ansible/tests/repo/test_census_rows_roles.py`: no key defined in the `longhorn-restore-drill`
  role's defaults appears in a task or template file under `ansible/roles/setup/`. A fixture
  `setup/` template that names `k3s_longhorn_restore_drill_timeout_s` is flagged. The tree at
  H2.3d is clean, and the row asserts that it found its named members,
  `k3s_longhorn_restore_drill_cron` and `k3s_longhorn_restore_drill_timeout_s`, in that
  role's defaults.
- *Drill state in the ConfigMap.* The script tests run against a fake API whose ConfigMap
  persists across runs, with a fresh working directory each run:
    - two consecutive runs drill different volumes. Against a script that kept its state on
      the local filesystem, the second run drills the first volume again;
    - a retry already spent survives a fresh run: the second run does not retry again;
    - an import from a fixture directory writes epochs equal to the fixture files' mtimes, and
      a second run with `imported-at` set imports nothing;
    - a failed patch of `attempts.<pvc>` fails the run before any restore;
    - a NotFound on the ConfigMap fails the run with `stamp ConfigMap absent`, before any
      restore;
    - TERM sent to a run while it waits in the restore poll ends the run, and the fake API
      then holds no `restore-drill-<pvc>` Volume, PV, PVC or probe pod. The red half runs a
      fixture copy that sets `trap '' TERM` in place of the trap, which is what PID 1's
      default amounts to, sends TERM, then SIGKILL after the grace period, and finds the
      objects still there. It needs no PID namespace, so it runs in CI.
- *Shell jobs under the image.* The drill's image leg runs the fixture runs inside the pinned
  `alpine/k8s` digest and passes. The red half runs a fixture copy that keeps `mktemp
  /tmp/restore-drill.XXXXXX.yaml` in the same leg, and the run fails before any restore.
- *Grants with their CronJobs.* Each family PR's commit passes the existing
  `test_every_caller_is_a_label_some_rendered_pod_carries`, because the CronJob that carries
  each new caller label renders in that commit.
- *Drill deadline.* The rendered CronJob's `activeDeadlineSeconds` exceeds
  `k3s_longhorn_restore_drill_timeout_s` plus the probe's 300-second bound. A fixture default
  that sets the deadline to the timeout key alone fails.
- *Drill script render.* The test renders the drill's script ConfigMap. If the tag's
  Kustomization carries `spec.postBuild`, it runs the pinned Flux CLI's `flux envsubst` over
  it first, as Flux would. The result must equal the script template's render byte for byte.
  A fixture tag with `postBuild` and an unescaped `${PVC}` fails the comparison.
- *Drill stamp readers.* With both the ConfigMap and the directory absent, every reader
  reports a failure, not a pass. A ConfigMap stamp wins over a directory stamp. In the
  ConfigMap arm, a candidate with no `seen` entry and no success is flagged once the coverage
  window has passed since the ConfigMap's creation. A non-empty `candidates` with no `seen`
  entry at all is flagged at once.
- *Home allowlist.* An empty IPv6 discovery leaves a stored /64 in the allowlist.
- *CronJob timezone.* Every CronJob this slice adds renders `timeZone: Etc/UTC`.
- *Check 12.* A newest S3 entry 15 hours old is flagged. A list with only local entries is
  flagged, and so is an empty list. A fixture drop-in whose schedule is `0 */6 * * *` flags an
  entry 9 hours old with the same rendered shim, which proves the bound is read at run time. A
  missing drop-in or key is flagged, and so is `etcd-s3: false`. A drop-in `sys_user` cannot
  read is flagged with its own message, `drop-in unreadable`, not as missing.
- *A `containers_list` edit's landing (H2.3b, H2.4b, H2.5b to H2.5d and their reverts,
  before D6.12).* The stop rule refuses on a name, never on an exit code. A tag whose role
  moved on `origin/master` past its applied commit appears in the `--stale-only` lines, and
  one running a worktree's commit reads `unmerged` in the plain table. Either refuses the
  merge. The red case is a stale uptime-kuma. While uptime-kuma's role has a merged change
  that no apply has shipped, `--stale-only` names uptime-kuma. The narrow output of H2.4b,
  H2.5b and H2.5c names uptime-kuma as the edited entry, and H2.3b's names it as a reader of
  `platform`, so the stop rule refuses each of their merges. H2.5d's output does not name
  uptime-kuma, so its red case is a stale qbittorrent, its edited entry, which refuses the
  same way.
  A narrow output that differs from the PR's expected list also stops the session: a family
  PR's list without its edited entry's tag or without netpol-baseline, and an H2.3b list
  naming the drill's tag alone. After the landing, `probe.py releases --stale-only` names
  none of the narrowed tags, and `hold_sha` is empty. A failed apply leaves `hold_sha` set,
  and a refused narrowing leaves each owed service named as stale, so both checks can go
  red.
- *Key pairs.* A `longhorn_drill_namespace` that differs from
  `k3s_longhorn_restore_drill_namespace` fails the equality test, and so does a
  `claude_code_artifacts_node` that differs from `artifacts_k8s_node`.
- *Host package files.* A fixture drop-in that reads `r2_bucket` is refused. A fixture package
  that ships `50-homelab.yaml` with a mode other than 0644 is refused, and so is one that
  ships `config.yaml.d` at 0700.
- *Secret placement.* A fixture `cluster-base` render that gains `postBuild` fails, and so
  does an etcd-s3 Secret rendered outside `kube-system`.
- *Claude crons.* A render for daniel-server produces neither cron.
- *Token drift.* A missing stamp and an unreadable token each report DOWN.

## Rollback

`longhorn` and `longhorn-backup` set `prune: false`, so a revert of a Flux PR that touches
them leaves its objects live (Baseline, *H1's tags*). Each such revert therefore names the
objects it leaves and carries an **operator-only** delete step for each. Both tags are also
forward-only, so a failed revision holds promotion without a reset, and recovery is a fix
forward (*Risks*). The `longhorn-restore-drill` tag prunes like any per-tag Kustomization, so
a revert that keeps the tag removes the objects it dropped, except the stamp ConfigMap, which
carries `prune: disabled`.

- **H2.1:** revert. Nothing on a host changed.
- **H2.2a:** revert. The reconcile arms go. Left live: the `trim` RecurringJob, and the trim
  label and explicit `default` label on the canary. **Operator-only**, as one step: delete
  the RecurringJob, then remove the canary's trim label with `kubectl label` and the key
  `recurring-job.longhorn.io/trim-`. The `default` label keeps the canary in the daily
  backups. If the label removal is missed, the leftover label names no job, and no reader
  treats it as a tier, which the H2.1 fixtures hold.
- **H2.2b:** revert. The one-claim bound returns, and the reconcile arm removes the trim label
  from every other volume on its next run. Nothing is left that needs a delete. H2.2b changes only
  `longhorn-reconcile`, which is not forward-only, so a failed revision of it resets `prod`
  to `last-good` (C2) and needs no fix forward.
- **H2.3b:** revert only after H2.3d and H2.3c are reverted, because the host script of H2.3c
  writes the ConfigMap. The revert removes the tag, and C3's `deletionPolicy: Orphan` leaves
  the ConfigMap live until D6.20; after D6.20 switches the policy, its `prune: disabled`
  annotation keeps it. It holds the drill's state, so it is kept until the operator decides
  to delete it (**operator-only**). The revert removes the `containers_list` entry. D2.1c
  records a removed Flux-born entry by its fields, as it records an added one, so before
  D6.12 the revert reaches the same four readers as the forward PR. The revert PR therefore
  runs *Landing a `containers_list` edit* (Design) and lands in a quiet window.
- **H2.3d and H2.3e:** revert H2.3e first, so Flux applies `suspend: true` before any revert
  restores the host cron. Then revert H2.3d and run its hand apply (**operator-only**), which
  restores the host cron with the H2.3c script that writes the same ConfigMap. The tag stays,
  so Flux prunes the CronJob, its script ConfigMap, the ServiceAccount, the two Roles, the
  ClusterRole and their bindings. Nothing is left that needs a delete.
- **H2.3c:** revert it together with H2.3a, in one revert PR with one hand apply
  (**operator-only**) and the owed-line procedure; revert H2.3f first if it has landed. The
  readers prefer the ConfigMap whenever it holds `last-success`, and the restored
  directory-based script writes only the directory. Reverting H2.3c alone would leave checks 7
  and 8, the upgrade gate and `state.py` reading a ConfigMap nothing writes, so check 7 would
  report a stale drill and gate 2 would refuse while the real drill passes. With H2.3a
  reverted too, every reader reads the directory. The import left the directory as it was at
  the H2.3c merge, so check 7 can read stale until the next 04:10 UTC run writes a new
  `last-success`.
- **Another `setup/k3s` PR (H2.2c, H2.3a, H2.3f, H2.8b):** revert, then run the PR's own hand
  apply (**operator-only**) and the owed-line procedure. That restores the cron, the script or
  the reader arm from the reverted role. H2.3a reverts alone only while H2.3c has not landed.
- **An Ansible-owned k8s PR (H2.4b, H2.4c, H2.5b, H2.5c, H2.5d):** revert. For H2.4c,
  `land.sh` deploys the role. The other four reverts remove a `netpol_from` label, a field
  edit, so the tick applies the role with the edited entry's tag and netpol-baseline, and
  each revert runs *Landing a `containers_list` edit* (Design). The armed prune deletes the CronJob, its ConfigMap and its ServiceAccount, and the
  restored cron task reinstalls the host cron in the same run. The Role, RoleBinding,
  ClusterRole and ClusterRoleBinding are not prunable kinds, so they stay, inert, until a
  follow-up removes them. A crowdsec revert lands in a home-edge window.
- **A grant reverts with its CronJob.** The family PR's revert removes the `netpol_from`
  entry and the CronJob in one commit, so no commit names a caller that no pod carries. The
  revert landing applies the role first and netpol-baseline last, as the forward landing
  does.
- **H2.7:** revert. The crons return to the k8s roles under the same cron file names.
- **H2.8a:** revert. dpkg removes the keys, and the next window restarts k3s without them.
  Left live: the `k3s-etcd-snapshot-s3-config` Secret, which nothing reads once the drop-in is
  gone. **Operator-only:** delete it. Local snapshots taken meanwhile are compressed and named
  `offbox-*`, and the script's retention ages them out.
- **H2.8c:** revert the docs PR. An object the operator deleted does not come back, but each
  was older than the 28 newest snapshots that remain.

**What a partial failure leaves live:**

- In a k8s role, the absent arms run before the manifests apply. A deploy that fails between
  them leaves neither runner until the rerun, and the job's Kuma tile goes DOWN.
- For a `setup/k3s` retirement, the cluster replacement exists from the promote until the hand
  apply. The trim is idempotent, so two runs are harmless, and the drill waits suspended.
- Between H2.3c and H2.3d's hand apply, the host drill writes the ConfigMap, and the readers
  already prefer it.
- On a fresh cluster before D5's namespace PR, the `longhorn-restore-drill` Kustomization
  reads not Ready until step 8 of H1's DR order creates `homelab`. No step-6 tag waits on it.
- Between H2.8a and H2.8b, the script's `--s3-retention 14` and native retention both prune the
  shared `offbox` prefix. The off-box window shrinks to about five days for that overlap, which
  lasts about a day.

## Risks

- **A forward-only hold (failure class 5).** H2.2a and H2.8a change `longhorn` or
  `longhorn-backup`. H2.2a's reconcile arms and H2.2b are in `longhorn-reconcile`, and the
  drill's PRs in `longhorn-restore-drill`, and neither tag is forward-only, so a failed
  revision of H2.2b or a drill PR resets `prod` to `last-good`. A failed revision of H2.2a or
  H2.8a writes `DEPLOY_HOLD` and does not reset `prod`, which also holds every other merge
  behind it. Recovery is a fix
  forward, and clearing the hold is **operator-only** (C2; H1, *Forward-only tags and the
  promote rule*). Each of these PRs is small for that reason, and none rides with a pin bump.
- **A `containers_list` edit applies denylisted tags unattended (until D6.12)** (failure
  class 4). H2.3b adds the drill's entry. H2.4b, H2.5b, H2.5c and H2.5d each edit a
  `netpol_from` field, and every revert of the five edits the list again. A field edit
  narrows, and so does an added Flux-born entry once D2.1c has landed, but neither empties
  the apply. A family PR's tick applies the edited role, the edited entry's own tag and
  netpol-baseline. H2.3b's applies artifacts, blackbox-exporter, pihole and uptime-kuma
  (Design, *Landing a `containers_list` edit*). Up to three of those tags declare
  `k8s_autodeploy: false`: blackbox-exporter, pihole and uptime-kuma in H2.3b, and
  uptime-kuma, crowdsec, janitorr or qbittorrent in the family PRs that name them. The apply is forward-only. The stop
  rule keeps another session's unlanded change from riding along, and the `--dry-run` quote
  names the renders that move. A tag's own verify tasks still run, so a flaky gate in one of
  them fails the landing and holds the deploy plane until D1's way out of a hold. The
  obligation ends only at D6.12, when the deployer's k8s arms go.
- **`pods/exec` on CrowdSec.** A Role that grants exec into the namespace is broader than the
  four commands need. It is bound to one ServiceAccount per job and used only by these
  CronJobs. *Open questions* 2 covers the sidecar alternative.
- **A host-network pod.** The home allowlist runs in the host's network namespace, and the
  baseline PSA warning records it. It reaches what the host cron reached, and no more. It is
  pinned to daniel-box.
- **Broad drill grants.** The drill's ServiceAccount can create and delete any PV and any
  Longhorn Volume, and list and watch every PV, because RBAC cannot scope by name prefix.
  That is narrower than the root admin kubeconfig it replaces.
- **One denied push per configarr health landing.** configarr's `k8s_autodeploy: true` makes
  the gate mandatory, and netpol-baseline applies the fence after the gate run. That one run's
  push fails after `kuma_push`'s three attempts, inside the CronJob's deadline. The Kuma tile
  waits for the first scheduled run, as it would with no gate (Design, *First run*).
- **A community image under RBAC grants.** `alpine/k8s` runs the shell jobs and supplies
  `kubectl` to the Python ones, under the grants above. It is digest-pinned, so its bytes
  change only through a reviewed or soaked Renovate digest PR. *Open questions* 9 asks the
  operator to accept it or name another.
- **The drill rewrite changes the rotation.** H2.3c is exercised by the host cron, whose
  `logger` evidence and checks 7 and 8 already work, before anything moves. The script tests
  cover rotation, retry and import against a fake API that persists across runs.
- **The trim attaches idle volumes, or drops a backup.** The label rule excludes detached and
  Job-only claims, and the explicit `default` label keeps daily backups. No stage cluster
  exists, so H2.2a measures both on production against one canary claim, and logs the
  unbounded rule's set, before H2.2b widens the rule. The reconcile tests hold both cases.
- **The trim label read as a backup tier.** A `trim` group label would make the drill,
  the reapers, the probe and the seed playbook read `trim` as a tier (Baseline, *Readers that
  treat every group label as a tier*). The trim uses Longhorn's per-job label, which none of
  them reads. The H2.1 fixtures fail if a reader starts reading per-job keys, and the
  reconcile case fails if the arm writes the group form.
- **The label rule lags by up to an hour (accepted).** From H2.2b, a volume whose workload
  scales to zero after the last hourly reconcile pass before 06:10 UTC still carries `trim`,
  and Longhorn attaches it for the trim and detaches it afterwards. That is one attach of an
  idle volume, the same attach the detached setting already causes nightly for
  `pi-peer-backup-data`'s backup, and the next pass removes the label. Closing the window
  would need a second reconcile schedule or a trim time tied to the reconcile's minute. H2.2b's
  body names any volume that hit it.
- **A secret staged into a ConfigMap.** Six of the moved scripts template a secret today. Each
  family PR moves it into the role's Secret, and the no-secret-in-ConfigMap test refuses a
  ConfigMap whose data holds any `secrets.yml` value.
- **A drill killed at its deadline.** The TERM trap runs `cleanup`, and the grace period and
  deadline leave it time (Design, *A TERM trap* and *The deadline*).
- **A key baked into a host script goes stale behind a Flux reader (failure class 5).** A
  change to a key that both a Flux tag and a `setup/k3s` template read lands in one plane on
  promote and in the other only at a hand apply. H2 adds no such key: its new host reads happen
  at run time, and the keys it leaves in `setup/k3s` have no Flux reader. H2.3d's census row
  holds the converse for the eight keys it moves: no `setup/` task or template names a key the
  `longhorn-restore-drill` defaults define (*Drill keys off the host plane*). H1.7a and H1.7b
  remove the five that exist. `k3s_longhorn_r2_region` needs the same treatment: H1.7b moves
  it to `longhorn-backup` and gives the off-box script a `setup/k3s` twin with another name,
  held equal by a test, and H2.8b deletes the twin and its test (*Open questions* 11). Until
  H2.8b, a region change edits both keys, and the script's half reaches the host only at the
  next `etcd-snapshot` hand apply.
- **Control-plane boot and the age key.** Only the etcd-s3 fallback puts a secret-bearing file
  in `config.yaml.d`. With it, a failed `homelab-secrets` at boot leaves a dangling symlink
  there, and k3s does not start. The primary design avoids this. The flag exists at the
  baseline's k3s (Baseline), and H2.8a rechecks it if `k3s_version` has moved.
- **R2 storage.** 28 snapshots of about 15 MB cost about 420 MB. H2.8a's PR body quotes the
  bucket's stored bytes from monitor-bridge's `r2_usage` reading, and sets the retention to 14
  if 28 would cross the free tier.
- **Failure class 2, a check that observes a proxy.** A trim Job's success says nothing about
  each volume (Baseline), so the trim arm reads the pod log.
- **Failure class 3, empty read as clean.** Check 12 reads an empty list as DOWN. Every drill
  reader reads "no stamp anywhere" as a failure, and check 8 flags candidates with no seen
  entry. The censuses each name a member they must find.
- **Failure class 4, a guard's selector drifts from the hazard.** The reaper census reads the
  mounted ConfigMaps as well as the command, and the RBAC test reads the script itself.
- **Failure class 5, sequences treated as transactions.** The absent arms run before the apply
  in k8s roles. The drill starts suspended, the readers change before their producer, and the
  state moves before the script does.
- **Failure class 7, execution context.** Each job leaves cron's sparse environment for a pod
  with an explicit `env`, `timeZone: Etc/UTC` and an image whose tools each PR lists. Its
  evidence moves from `logger` to stdout. The host-network pod sets `ClusterFirstWithHostNet`
  so that it still resolves cluster names.

## Tests and docs that change in the same PR

- **H2.1:** `ansible/tests/repo/test_census_rows_roles.py`,
  `ansible/tests/longhorn/test_longhorn_reap_never_scheduled.py`, the RBAC-against-script
  test in `ansible/tests/k8s/`, and the trim-label fixtures in
  `ansible/tests/longhorn/test_longhorn_restore_drill_oversize.py`,
  `scripts/backup/tests/test_longhorn_reap_logic.py` and
  `scripts/backup/tests/test_longhorn_reap_opt_out_snapshots.py`.
- **H2.2a:** the `longhorn-reconcile` unit tests in that role's `tests/`, including the
  one-claim bound, the `would-admit` line, the per-job-label case and the RBAC subset case; `longhorn`'s and
  `longhorn-reconcile`'s rows in H1.1c's per-tag object census.
- **H2.2b:** the same tests lose the bound's cases.
- **H2.2c:**
    - the two cron-evidence tests and the skipped-volume tests;
    - `scripts/validate/tests/test_shell_template_cron_rules.py` and
      `scripts/validate/tests/test_validate_shell_templates.py`, which name
      `longhorn-trim-volumes.sh.j2`;
    - `ansible/roles/setup/k3s/CLAUDE.md`, whose contract restates the trim's abort valve and
      evidence;
    - `docs/facts.lock`, until G2 retires it;
    - `docs/k3s-node-plane-crons-and-incidents.md`.
- **H2.3a:** the stamps module's tests, `ansible/roles/setup/k3s/tests/test_longhorn_restore_drill_checks.py`,
  `scripts/deploy_tools/tests/test_longhorn_upgrade_gates.py`,
  `scripts/docs/tests/test_gen_reference_state.py`, and `docs/longhorn-disaster-recovery.md`,
  which names the ConfigMap keys beside the files.
- **H2.3b:** the `longhorn-restore-drill` tag's rows in H1.1c's per-tag object census; the
  step-6 namespace test; the role's `CLAUDE.md`, whose generated glance block is written with
  it.
- **H2.3c:** the drill script tests under `ansible/tests/longhorn/` and their helper
  `ansible/tests/longhorn/_restore_drill.py`, which gain the persisting fake API, the import
  cases, the NotFound case, the TERM case and a `mktemp` template with no suffix;
  `docs/k3s-node-plane-crons-and-incidents.md`, which describes the state.
- **H2.3d:**
    - the drill tests under `ansible/tests/longhorn/` retarget the `longhorn-restore-drill`
      render, the operator-pin test included, and gain the deadline render test;
    - `ansible/tests/repo/test_census_rows_roles.py` gains the drill-key row;
    - `ansible/tests/services/test_pi_peer_backup_schedule_clears_the_longhorn_slots.py` reads
      the cron key from the `longhorn-restore-drill` defaults;
    - `ansible/tests/k8s/test_base_images_digest_pinned.py`, if its fixture names the moved
      image key;
    - `scripts/tests/test_renovate_managers.py` and `renovate.json`: the reason for scanning
      `roles/setup/*/defaults` moves with the image key, and the scan goes if no other image
      key remains there;
    - `ansible/tests/setup/test_kuma_env_renders_for_every_cron_tag.py`,
      `scripts/validate/tests/test_shell_template_cron_rules.py` and
      `scripts/validate/tests/test_validate_shell_templates.py`, which name the drill template;
    - the reaper census's setup arm, whose named member moves to `longhorn-backup-health.sh`;
    - `scripts/docs/reference/state.py`, whose cadence lookup reads the
      `longhorn-restore-drill` defaults;
    - the namespace equality test and the drill script render test;
    - the image leg in `ansible/tests/longhorn/_restore_drill.py`, with the suffixed-template
      red case;
    - the contract moves from `ansible/roles/setup/k3s/CLAUDE.md` to the
      `longhorn-restore-drill` role's page, and states that the evidence is the pod log, read with `probe.py loki-query
      '{container="longhorn-restore-drill"}'`. Until G2 retires `docs/facts.lock`, run `uv run
      python scripts/dev/fact_status.py verify` for both sections and commit the lock;
    - `docs/longhorn-upgrade.md` gate 2 and `docs/k3s-node-plane-crons-and-incidents.md` give
      the **operator-only** hand run and the pinned run (Design, *Hand runs*).
- **H2.3f:** `docs/longhorn-disaster-recovery.md` drops the host paths;
  `ansible/tests/repo/test_shared_render_accessors.py` drops its stamp-dir case; the readers'
  tests drop their directory cases.
- **H2.4b and H2.4c:**
    - `ansible/roles/k8s/crowdsec/CLAUDE.md`'s allowlist and bouncer-prune sections name the
      CronJobs, and their `probe.py loki-query` examples change from the syslog tag to the
      container label;
    - the crowdsec module tests, which gain the two entry points and read the push token from
      the environment;
    - the AppSec verify's new script harness, and the home allowlist's tests under
      `ansible/tests/services/`, each with its image leg;
    - uptime-kuma's `netpol_from` in `ansible/inventory/host_vars/daniel-box.yml`, in the
      CronJobs' commit;
    - `SWALLOWED_VERDICTS_POD_LOGQL` and its tests;
    - the static-monitor descriptions that say "on daniel-box";
    - the no-secret-in-ConfigMap test in `ansible/tests/k8s/`, added by the first family PR to
      land and extended by each later one.
- **H2.5b to H2.5d:**
    - the no-secret-in-ConfigMap test's role list, and the `configarr` and `janitorr` module
      tests, which read the push token from the environment;
    - uptime-kuma's and qbittorrent's `netpol_from`, each in its CronJob's commit, and the
      prefs check's connect-retry case;
    - the deployer's cross-role tests, if D6 has not deleted the deployer;
    - `ansible/tests/setup/test_host_lib_sibling_copies.py`, whose subject leaves with the
      last import;
    - the `configarr`, `janitorr` and `qbittorrent` `CLAUDE.md` files;
    - `docs/qbittorrent-vpn-and-prefs.md`.
- **H2.7:** the `artifacts`, `observability` and `claude_code` `CLAUDE.md` files, whose
  generated glance blocks regenerate, and the test that holds `claude_code_artifacts_node`
  equal to `artifacts_k8s_node`.
- **H2.8a:**
    - `docs/k3s-etcd-restore.md` and `docs/break-glass.md` item 3: local snapshots become
      compressed `offbox-*` at 00:00 and 12:00, 28 of them kept, and a local snapshot restore
      passes `--etcd-s3=false` while an S3 restore keeps its explicit flags;
    - the region render test, and `longhorn_backup_etcd_s3_region` only if H1.7b has not
      defined `k3s_longhorn_r2_region` in `longhorn-backup` defaults;
    - the host-package secret test and the drop-in mode test, which covers the file and
      `config.yaml.d`;
    - `scripts/docs/gen_doc_fragments.py`, `scripts/docs/fragment_renderers.py`,
      `scripts/docs/tests/test_gen_doc_fragments.py`, and
      `docs/assets/generated/fragments/etcd-offbox-retention.md`, which regenerates to 28;
    - `longhorn-backup`'s rows in H1.1c's per-tag object census.
- **H2.8b:**
    - check 12's tests, including the run-time schedule case and the disarmed case;
    - the token-drift script's tests;
    - the `k3s_etcd_s3_armed` defaults comment and its marker, now deleted with the key;
    - the off-box script's region key in `setup/k3s` defaults, deleted with any equality test
      that holds it;
    - the `setup/k3s` `CLAUDE.md` contract, and `docs/facts.lock` until G2;
    - the tile description in `static-monitors.yaml.j2`;
    - `docs/healthchecks-io-deadman.md`;
    - `docs/k3s-etcd-restore.md`, which names `etcd-token-drift.sh` as the daily comparison
      and the drop-in's `etcd-s3` as the disarm lever;
    - `docs/assets/generated/fragments/deadman-cadences.md`, if its source moves.
- **H2.8c:** `docs/k3s-etcd-restore.md`, which records whether native retention prunes the
  script's objects.

## Complexity delta

- **Added:**
    - eight CronJobs, each with a ServiceAccount and Role, one ClusterRole for the drill and
      one for the remote allowlist;
    - one RecurringJob with `groups: []`, whose volumes carry a per-job label, and two arms
      in `longhorn-reconcile`, one of them bounded until H2.2b;
    - one Flux-born role and tag, `longhorn-restore-drill`, whose entry's landing applies
      four readers of `containers_list`, three of them denylisted, until
      D6.12 deletes the deployer's k8s arms;
    - one stamp ConfigMap, and a drill whose state lives in it;
    - four census rows, three trim-label fixtures in existing reader tests, the
      RBAC-against-script test, one host-package secret test, two
      tests that hold a key pair equal, the step-6 namespace test, the no-secret-in-ConfigMap
      test and the drill deadline test;
    - six secret values in the moved roles' Secrets, each read through `secretKeyRef`;
    - check 12, the etcd keys and one config Secret;
    - one host cron, the token-drift comparison, cut from the off-box script;
    - two image shapes, one of them a community image, and an image leg in each shell job's
      script tests;
    - about six `netpol_from` entries, whose four PRs each run D1.2's stop rule over the
      edited role, the edited entry's tag and netpol-baseline until D6.12.
- **Removed:**
    - ten host cron files from daniel-box, with their scripts under `/usr/local/bin` and
      `/opt`, and two `setup-deployed-manifest.d` fragments;
    - the off-box upload, its S3 flags, `k3s_etcd_s3_armed` and `k3s_etcd_s3_retention`;
    - the host-lib copies for `configarr` and `janitorr`, and the `_K8S_HOST_LIB` rows;
    - the syslog-matching trim arm of checks 9 and 10;
    - the root crons that ran `kubectl exec`, and the drill's root admin kubeconfig;
    - `/var/lib/longhorn-restore-drill` and `/var/lib/crowdsec-home-allowlist`.
- **Not H2's, by decision:** registry GC, which D5 retires along with `/var/lib/registry-gc`.
  Dropping H2.6 removes an orchestrator CronJob, its RBAC, a GC spec ConfigMap and two fence
  entries from this plan. Folding H2.4a and H2.5a into their CronJob PRs removes two PRs.
- **Unchanged, by decision:** backup health and the B2 pair stay host crons. Two Claude crons
  change owner and stay on the host.

## Open questions

1. **Edges the slice index lacks.** Each H2 PR's own edges:
    - H2.2a needs H1.5a (`longhorn`) and H1.6 (`longhorn-reconcile`).
    - H2.3b needs H1.5a, because its tag `depends_on: [longhorn]`. H2.3c needs H2.3b, read
      live: the stamp ConfigMap exists in the cluster before the hand apply, not only on
      master. H2.3d needs H2.3b and H2.3c, and through them H1.5a.
    - H2.3b to H2.3f need nothing from D5. Before D5's namespace PR, the drill's tag
      reconciles after `ansible/deploy.yml` creates `homelab`. After it, D5's rule generates
      the `longhorn-restore-drill` → `homelab-namespace` edge.
    - H2.8a needs H1.7b (`longhorn-backup`, and through it D3) and H6.4b (the drop-in and the
      windowed restart, and through it H6.4a). It also gates on the region key (*Open
      questions* 11).
    - Every other H2 PR needs nothing outside H2: H2.1, H2.2b and H2.2c, H2.3a, H2.3c,
      H2.3e, H2.3f, H2.4b to H2.5d, H2.7, H2.8b and H2.8c. The six roles are Ansible-owned at
      merge, so none of these needs D3.
    - No H2 PR needs H1.8, H1.9 or any part of D3 except through H1.7b.
    - D5 wave D waits on H2.1. For crowdsec, configarr, janitorr, qbittorrent, artifacts and
      observability it also waits on that role's H2 family PR: H2.4b and H2.4c, H2.5b, H2.5c,
      H2.5d, and H2.7. Registry waits on no H2 PR, because D5 retires its GC.

   No cycle results. H2 never needs H1.9, which waits on R4, then D6, then D5. The H2 PRs
   that D5 waits on need nothing outside H2. Resolution: contracts.md's index row and its
   paragraph below the table state these per-PR edges, H2.3c → H2.3b read live among them,
   drop H1.8 and H2.3b → D3, and replace "H2.8a also D3, H6.4a and H6.4b" with "H2.8a also
   H1.7b and H6.4b". Until they do, contracts.md's *Requested amendments not yet applied*
   register lists this request, and *Open questions* 12's C3 row with its gate before H2.3b.
   D5's plan names H2.5b to H2.5d where it names H2.5b.
   **Applied on 2026-10-16.** contracts.md's H2 row and the paragraph below the table state
   the per-PR edges. No gate was named.
2. **A sidecar or exec for CrowdSec?** A sidecar in the `crowdsec` pod, running the allowlist
   and prune loops against the shared database, needs no `pods/exec` grant. It is no longer a
   CronJob, though, and it restarts with the LAPI. Resolution: H2.4b picks one, and records the
   measured cost of the sidecar's memory request against the exec grant.
   **Measured 2026-10-18 (#5649): no third form exists over the network.** The four jobs
   need three kinds of `cscli` call: `allowlists` writes, `bouncers list` and `bouncers
   prune`, and `appsec-configs list` and `appsec-rules list`. The LAPI at the pinned
   `crowdsecurity/crowdsec:v1.8.1` serves none of these. Its route table
   (`pkg/apiserver/controllers/controller.go` at the `v1.8.1` tag) serves allowlists
   read-only (`GET /v1/allowlists`, `GET /v1/allowlists/:allowlist_name` and the `check`
   routes). It has no bouncer route and no AppSec route. Each job therefore needs one of
   three things:
    - the `pods/exec` grant this section's design gives it;
    - the sidecar above;
    - a Job that runs `cscli` from the crowdsec image against `crowdsec-db` mounted directly.
      That PVC is ReadWriteOnce on Longhorn, so the Job must schedule on the LAPI's node. It
      would also write the SQLite file beside a running LAPI, which nothing here measures.
   The sidecar cannot host either Python job as it stands, because the crowdsec image carries
   no Python. `bouncer_prune.py` and `remote_allowlist.py` are both Python modules, so each
   needs a port to shell or an image that adds an interpreter. `remote_allowlist.py` also
   reads IngressRoutes and Traefik's access log through `kubectl`, which the image lacks too. The AppSec verify checks what the live engine
   loaded, so only an exec or a sidecar in the engine's pod reads the same thing. The
   operator picks the form per job in the H2.4b PR's review (**operator-only**). Until then
   no cron is retired and no `netpol_from` label is added, because a retired cron with no
   running replacement would let the home address be banned and would remove the only check
   that the WAF enforces.
   **Operator ruling, 2026-10-18 (#5655): the sidecar form, for all four jobs.** They run in a
   sidecar of the crowdsec LAPI pod that shares its `crowdsec-db` and config volumes and runs
   `cscli` locally. No `pods/exec` grant is made, and no Job mounts `crowdsec-db` from outside
   the pod. The suggested shape keeps both Python modules and gives a `python:3.14-alpine`
   sidecar the `cscli` binary from the crowdsec image, copied by an initContainer. Porting the
   helpers to shell in the crowdsec image was the fallback if the copied binary did not run.
   **Measured 2026-10-18 (H2.4b):** `file` on `/usr/local/bin/cscli` in the amd64 layer of the
   pinned `crowdsec_k8s_image` digest reads `statically linked`, and the extracted binary runs
   `cscli version` (`v1.8.1-909b5157`) outside its image. So the Python variant holds.
   H2.4b runs the three API jobs in the `crowdsec-jobs` sidecar under one scheduler,
   `files/crowdsec_jobs.py`, with the AppSec verify ported to Python because the image has no
   `jq`. The two Python modules change only their transport: `cscli` and `kubectl` run from the
   sidecar's `/tools` instead of through `k3s kubectl exec`. The sidecar's memory request is
   64 MiB against a measured worst case near 250 MB (`crowdsec_k8s_jobs_*` in the role's
   defaults), the cost the resolution above asked to weigh against the exec grant.
   **The home allowlist does not fit the sidecar.** A container in the LAPI pod sees the pod's
   IPv4-only network (`podCIDRs: ["10.42.0.0/24"]` on daniel-box), so it cannot discover the
   home IPv6 /64. #5701 puts the options for H2.4c to the operator.
3. **Backup health and the B2 pair stay on the host.** design.md's host-plane section says
   about 15 cluster-reading crons become CronJobs. This plan retires ten cron files from
   daniel-box, D5 retires registry GC, and these three stay.
   Resolution: the operator confirms in H2.1's review. The question reopens when the B2 ledger
   gets a home that the accounting, the listing and `probe.py b2-spend` all share. Backup
   health then follows, and its marker is answered again.
4. **Roles the census flags that no H2 PR clears.** pi-peer-backup's Pi-side wrapper is a host
   file on daniel-pi, and registry keeps its GC cron until D5 retires it. Resolution: D5's
   conversion PRs move both into `k8s_host_files` before each flip. pi-peer-backup's two
   writes stay there until H7 (H5, *Rows H5 takes from D5's `k8s_host_files`*).
5. **A general C5 check.** C5 says packages carry SOPS ciphertext only, and nothing checks
   that. Resolution: H3 or R2 adopts H2.8a's host-package secret test for every package, and C5
   names it.
6. **Two censuses guard the same flip.** D5.1c's host-write census refuses a flip while the
   role keeps any host write, `file` absent arms included. That is stricter than H2's
   flux-ownership row, which flags only present installs. Resolution: whichever of H2.1 and
   D5.1c lands second checks that the stricter row covers every member of the other, and
   deletes H2's row if it does (`.claude/rules/python-layout.md`, *A check retires when its
   subject does*).
7. **Backup health's baked keys.** H2 keeps backup health on the host only if it reads, at
   run time, every input whose other reader Flux renders. That rests on H1.7a, which makes the
   two host readers read the BackupTargets and RecurringJobs, and on H1.7b, which moves the
   five shared keys into `longhorn-backup` and adds the census row that keeps them off
   `setup/`. Resolution: H2.2c and H2.3a land after H1.7a or rebase onto it. If H1.7a slips,
   H2's decision still holds, and the cost is a hand `--tags backup-health` apply after each
   lever change until it lands.
8. **Order against M6.** M6 batch B6 converts the k8s-role crons' Kuma checks, and its *Open
   questions* 3 and 5 say whichever slice reaches a cron first converts it. Resolution, per
   family PR:
    - If B6 has landed for a cron that H2.4b, H2.4c or H2.5b to H2.5d moves, that PR takes
      M6's kube-state-metrics CronJob recipe: no Kuma push, no `kuma_push.py` or
      `kuma-push-lib.sh` in the ConfigMap, no uptime-kuma `netpol_from` grant, and no
      `SWALLOWED_VERDICTS_POD_LOGQL` entry. That PR then adds no uptime-kuma grant. H2.5d
      still adds the prefs check to qbittorrent's fence, in its CronJob's commit.
    - If H2's PR lands first, M6's batch converts that check from the CronJob, as M6's
      *Cross-area* paragraph says.
    - H2.7's two crons stay host crons, so M6's textfile recipe applies to them either way.
9. **The shell-job image.** `alpine/k8s` is a community publisher, and the moved jobs run it
   under RBAC grants. Resolution: the operator accepts it in H2.1's review (**operator-only**),
   or names another image that carries bash, `jq`, `curl` and `kubectl`. The rejected
   alternative is `registry.k8s.io/kubectl`, which is official but has no shell, so neither a
   script nor an init container can use it.
10. **H6's commitments for the drop-in.** H2.8a needs three things H6 does not state: the
    drop-in ships with mode 0644 and holds no secret, and `config.yaml.d` ships at 0755, so
    check 12 can read the file as `sys_user`; the etcd keys go in `k3s_server_config`; and the
    fallback `60-etcd-s3.yaml` would need H6's single-file test amended. The directory does
    not exist on daniel-box at `70591c307`, so H6's package creates it, and its siblings under
    `/etc/rancher/k3s/` include `drwx------` ones. Resolution: H6 records these in an Open
    question of its own, and H2.8a's mode test holds the file and the directory modes.
11. **`k3s_longhorn_r2_region` has two readers.** The R2 BackupTarget template, which H1.7b
    moves, and the off-box script, which reads it until H2.8b. H1.7b's census would refuse the
    key in `longhorn-backup` defaults while the script reads it. H1's plan does not move the
    key at all: a grep of its text for `r2_region` finds nothing. Without the key in
    `longhorn-backup` defaults, the moved BackupTarget template reads a key that only
    `setup/k3s` defines. Resolution: H1.7b moves the key into `longhorn-backup` defaults and
    repoints the off-box script to a `setup/k3s` twin with another name, proposed
    `k3s_etcd_s3_region` beside the script's other `k3s_etcd_s3_*` keys, held equal by a
    test. H2.8b deletes the twin and that test with the upload. H2.8a's Secret then reads the
    region from `longhorn-backup`'s own defaults. Gate: before H2.8a merges,
    `longhorn-backup` defaults define `k3s_longhorn_r2_region`. If H1.7b has not carried
    that, H2.8a adds `longhorn_backup_etcd_s3_region` to `longhorn-backup` defaults, a key no
    `setup/` file names, and a render test that holds the Secret's region equal to the R2
    BackupTarget's. H2.8a then still touches no `setup/k3s` path.
12. **A prune exemption for the drill's state.** C3 exempts PVCs and Namespaces from prune.
    The stamp ConfigMap holds state that no render can restore, so it carries
    `kustomize.toolkit.fluxcd.io/prune: disabled` as well. Without the exemption, a revert or
    a render bug that drops it from the tag deletes the rotation, retry and seen state. The
    loss would be loud: check 8 flags a candidate with no seen entry once the coverage window
    passes, and a reader with no stamp reports a failure. Resolution: C3's prune-exemption
    line gains "and a ConfigMap that holds state no render restores, named by its slice", with
    this one as its member. Gate: before H2.3b merges. Neither revert in *Rollback* depends on
    the annotation until D6.20: H2.3d's keeps the ConfigMap in the render, and H2.3b's removes
    the whole tag, which C3's `deletionPolicy: Orphan` leaves live. Once D6.20 switches
    per-tag Kustomizations to the default policy, the annotation is what keeps it.
13. **Who stops a `containers_list` edit from applying denylisted readers.** Settled by
    D2.1c, and the earlier request is withdrawn. D2.1c is on `origin/master` before any H2
    PR, because H2 needs H1 and H1 needs D2. H2 asked D2.1a or D4.1 to map an
    added or removed `owner: flux` entry to its own tag alone. D2 refuses that: "The entry's
    own tag alone, which X2-06 proposed, is not sound, because the bare-entry render moves
    artifacts' `known_services.json`." D2.1c instead records such an entry by its fields,
    which cuts the drill entry's reach from 23 reader tags to four at `a5abf8581`:
    artifacts, blackbox-exporter, pihole and uptime-kuma (D2 Open question 10).
    Three of the four remain denylisted. A `netpol_from` field edit reaches netpol-baseline
    alone on that tree (Baseline, *A field edit to a `containers_list` entry narrows*). Resolution: every H2 PR that edits the
    list, and every revert of one, runs *Landing a `containers_list` edit* (Design). No
    narrow output exempts a PR from the stop rule. The obligation ends at D6.12.

## Review log

### Round 1 (2026-10-11)

Every finding was checked against the tree at `70591c307` or a read-only command. All 23 are
fixed, and none is refuted. Four (03, 07, 10 and 22) are fixed by a different means from
their `required_fix`, and each entry says why.

- **H2-R1-01 (critical): the etcd-s3 drop-in published SOPS values, and k3s would ignore the
  Secret.** Fixed. Confirmed: `r2_bucket` and `r2_account_id` are ciphertext, and the k3s docs
  say any other S3 flag makes k3s ignore the Secret. The drop-in now holds only `etcd-s3`,
  `etcd-s3-config-secret` and the non-S3 snapshot keys. Every `etcd-s3-*` value is in the
  Secret, from `${r2_*}` placeholders. The "none of them is a secret" sentence is gone. A
  host-package secret test is added. H2.8a's stage proof records `k3s server --help` and a
  native S3 save.
- **H2-R1-02 (high): the secret-bearing CronJobs went into `longhorn`, which H1 forbids
  substitution.** Fixed. Backup health and the B2 pair now stay on the host (R1-07). The drill
  CronJob, its ConfigMaps and its RBAC go in `longhorn-backup`, which has `postBuild`. The
  drill reads no secret, but its bash script's `${…}` reaches the ConfigMap as `$${…}` under
  C1's escape, and only substitution restores it. A round-trip test holds that. The file tree,
  the Design and H2.3b say so, and H2.3b now needs H1.7. Only the trim RecurringJob, which
  carries no `${`, goes in `longhorn`.
- **H2-R1-03 (high): `retired_host_crons` in `group_vars` is never applied by the tick.**
  Fixed differently. Confirmed: `inventory/` is deploy-plane, and
  `narrow_lib/broad.py:ROLE_TREES` excludes `roles/setup/`. Moving the list into
  `initial_setup` defaults would turn each addition into a whole-role `initial_setup` apply.
  The list is replaced by absent arms in the installing role, which the reviewer offered as
  the alternative: traefik's `Cloudflare IP drift` is the precedent. A retired-cron census
  catches a deleted install task.
- **H2-R1-04 (high): the census allowlist exempted the roles the guard is for, and no edge
  ordered D5 after H2.** Fixed. The allowlist is gone, because the census reads only
  `owner: flux` entries. H2.1 checks that none of the seven is flipped. *Open questions* 1 adds
  H2.1 to D5's row and narrows H2's edge to H1.1–H1.7, which avoids a cycle through H1.9. The
  fixture crowdsec entry is a named member that persists.
- **H2-R1-05 (high): the drill and B2 PRs removed inputs that backup health and the Longhorn
  merge gate still read.** Fixed. H2.3a changes checks 7 and 8 and
  `runbook_gates_lib/longhorn_upgrade.py` to read the stamp ConfigMap before the producer
  moves, with a host-directory fallback until H2.3d. `restore_drill_max_age_s` follows the
  defaults. `docs/longhorn-disaster-recovery.md` changes in H2.3a and H2.3d. The B2 crons no
  longer move, so check 10's B2 arm keeps its inputs.
- **H2-R1-06 (high): check 11 already exists, and the planned RBAC would fail it and the etcd
  check.** Fixed. The Baseline names check 11, and the etcd check is check 12. Backup health
  stays on the host under the read-only kubeconfig, so no new ServiceAccount is needed. Its
  `view` binding covers `pods/log`, and `k3s_readonly_crd_api_groups` lists `k3s.cattle.io`.
  `kubectl get etcdsnapshotfiles.k3s.cattle.io` succeeded under that kubeconfig on
  2026-10-11. H2.2b narrows check 11 to the backup RecurringJobs, with red and green fixtures
  for a trim-pod skip line.
- **H2-R1-07 (high): the B2 data source was wrong, and the persistent ledger had no home.**
  Fixed differently. Confirmed: `b2_ledger.py` reads Loki, and the ledger lives under
  `B2_LEDGER_DIR` in the operator's home. Of the reviewer's options, this plan keeps both crons
  on the host. The `pods/log` claim is gone, and *Open questions* 3 records when to revisit.
- **H2-R1-08 (high): the CrowdSec home allowlist needs the host's IPv6 and host state.** Fixed.
  Confirmed: `podCIDRs` are `10.42.0.0/24` and `10.42.1.0/24`. The Baseline is corrected. The
  job runs with `hostNetwork: true` on daniel-box, and its PSA, NetworkPolicy and push-path
  consequences are stated. Its state moves into the LAPI allowlist. A fixture shows that an
  empty IPv6 discovery keeps a stored /64.
- **H2-R1-09 (high): registry GC is an orchestration, not a Job create.** Fixed. The whole
  script moves into an orchestrator pod in order, with RBAC, a fence grant and an order test.
  The TERM trap and the grace period cover the deadline path, and the images come from public
  registries. The Baseline is corrected.
- **H2-R1-10 (high): retiring the off-box snapshot broke both etcd drills and the restore
  runbook.** Fixed differently. With `etcd-snapshot-name: offbox`, native snapshots keep the
  `offbox-<node>-<ts>.zip` shape, so the drills, `docs/break-glass.md` and the runbook still
  match. `/etc/rancher/k3s/etcd-s3.env` stays as the restore credential. H2.8b deletes the
  orphaned local `etcd-snapshot-*` files, operator-only, and the stage proof decides whether
  stale `offbox-*` objects need deleting.
- **H2-R1-11 (high): retiring the script dropped the cluster-token drift check.** Fixed. The
  comparison stays on daniel-box as `etcd-token-drift.sh`, owned by `setup/k3s` until H7, and
  keeps its fail-closed red proofs.
- **H2-R1-12 (high): the native keys changed the local snapshot plane and dropped
  compression.** Fixed. `etcd-snapshot-compress: true` is listed. The 12-hour schedule is
  kept, and the change from 5 to 28 local snapshots is stated as a decision, with the byte
  arithmetic from live sizes (14 to 15 MB, not the stale 7.85 MB). The defaults comment,
  `docs/break-glass.md` and `docs/k3s-etcd-restore.md` change in H2.8a.
- **H2-R1-13 (high): the plan never named the writer of the seven roles' CronJobs, and the
  rollback assumed a Flux prune.** Fixed. *Who writes each change at merge time* names
  Ansible for the seven and gives their secret source. Rollback uses the armed prune, after
  checking `manifests_prune_allowlist`: it covers CronJob, ConfigMap and ServiceAccount, and
  the Role and RoleBinding are left inert. The reviewer's "orphaned CronJob" therefore does
  not occur. `k8s/cronjob-gate` proves the idempotent jobs. The CrowdSec PRs land in a
  home-edge window. The D3 edge narrows to the two Flux-owned PRs: H2.3b through H1.7, and
  H2.8a.
- **H2-R1-14 (high): a `trim` label may take volumes out of the `default` group.** Fixed.
  `longhorn-reconcile` sets `default` explicitly on default-tier volumes that carry `trim`, and
  never on `no-backup` or weekly-shard volumes, so the fix cannot start backups on a no-backup
  volume. H2.2a measures it on stage and quotes the result before merge.
- **H2-R1-15 (high): no NetworkPolicy or push-transport plan.** Fixed. Each family's grants
  land one PR ahead: uptime-kuma's and qbittorrent's `netpol_from`, and registry's fence. Pods
  push through `kuma_push.py`. The host-network job pushes through Traefik, as the host cron
  does. loki-homelab needs no grant, because no moved job reads Loki.
- **H2-R1-16 (medium): the census flagged roles that no H2 PR clears.** Fixed. A host `dest`
  is now a path list, and `/etc/rancher/k3s/` staging is excluded, which clears headlamp, n8n,
  prowlarr, netpol-baseline, monitor-bridge, observability's dashboards, traefik and
  game-stats. pi-peer-backup's Pi wrapper goes to D5 or H5 (*Open questions* 4).
- **H2-R1-17 (medium): H2.7 would install both Claude crons on daniel-server too.** Fixed.
  Both are gated on `artifacts_k8s_node`, the shared variables move, and the PR quotes the
  `claude_code` apply.
- **H2-R1-18 (medium): the plan missed monitor-bridge, Healthchecks.io and deadman-fragment
  consumers.** Fixed. Backup health stays a host cron, so `HC_ROUTED_TAGS` and its
  `uptime-kuma-alive` ping do not change. `SWALLOWED_VERDICTS_POD_LOGQL` names the moved
  pushers. The `etcd-snapshot-offbox` slug keeps its name and cadence for the token
  comparison, and an operator-only step updates its description. The UTC-slug comment and the
  fragments change where their sources move.
- **H2-R1-19 (medium): CronJob timezone and boot grace were underspecified.** Fixed. Every
  moved CronJob sets `timeZone: Etc/UTC`, and a rendered-property test holds it. On boot
  grace: `git grep -l boot_grace_active -- ansible/roles` finds no moved script, and backup
  health, which does call it, stays on the host, so `test_health_cron_boot_grace.py` keeps
  its target.
- **H2-R1-20 (medium): the trim lost its abort valve, and the group rule missed CronJob-owned
  volumes.** Fixed. Membership requires an attached volume and a Deployment or StatefulSet
  owner with replicas above zero, which excludes `pi-peer-backup-data`. The moved contract
  restates the abort valve for the native job.
- **H2-R1-21 (medium): the drill's RBAC scope and the PR sizes were understated.** Fixed. The
  drill's two Roles and its ClusterRole are stated, with RBAC's limit on name scoping. H2.2,
  H2.3 and every family are split into smaller PRs. Each `setup/k3s` PR names its hand apply
  tag and the `manual_plane` clear.
- **H2-R1-22 (medium): the exit checks needed root, and part 2 already passed.** Fixed
  differently. The suggested `ls /etc/cron.d/` also needs root: `stat` shows `drwx------ root
  root /etc/cron.d`, and an agent's `ls` is refused. Exit part 1 is therefore operator-only,
  with `kubectl get cronjobs` as the agent's half, and the `crontab -l` calls are gone. Part 2
  now needs an S3 snapshot created after the H2.8b hand apply. A name prefix cannot separate
  native snapshots, because both writers use `offbox`.
- **H2-R1-23 (medium): the reaper census's CronJob arm named no member.** Fixed. The arm
  asserts that it rendered `pi-peer-backup`'s CronJob and has a green fixture. The
  flux-ownership census's crowdsec fixture persists after the allowlist is gone.

### Round integration (2026-10-11)

The cross-slice integration review raised five findings against H2. Each claim was checked
against the tree at `70591c307` or a read-only command. All five are fixed in this slice, and
none is refuted. Three also need changes in other slices, which this round hands over as
cross-slice notes rather than edits.

- **X-06 (high): registry GC had two owners and no ordering edge.** Fixed. Confirmed: D5's
  Wave D registry bullet retires GC through `registry_k8s_manage_gc: false`, and D5's census of
  host scripts uses the `scale` call in `registry-gc.sh.j2` as its red fixture. H2 drops H2.6a
  and H2.6b and keeps its other step ids. *Registry GC is D5's to retire* gives two reasons:
  an in-cluster orchestrator still writes the `spec.replicas` that Flux renders, and D5's move
  to GHCR removes the need for GC. The flux-ownership census now also holds D5's
  retire-before-flip order for registry. *Open questions* 1 lists the edges per PR. D5 wave D
  waits on H2.1, H2.4b, H2.4c, H2.5b and H2.7, which need nothing outside H2, so no cycle
  forms through H1.9. The header and the index proposal read H1.1a to H1.8.
- **X-08 (high): H2.2a and H2.8a rested on the retired stage cluster.** Fixed. Confirmed:
  `docs/archive/staging-cluster.md` is archived (#2941), and `RETIRED_CLUSTER_NODES` holds
  `daniel-stage`. The Baseline now says so.
    - H2.2a becomes a production canary on one claim, bounded by
      `longhorn_reconcile_trim_claims`, with its blast radius stated. H2.2b widens the rule
      after two read-only readings, and the old host half becomes H2.2c. The live setting
      `allow-recurring-job-while-volume-detached` reads `true`, which is why the
      detached-volume reading matters.
    - For H2.8a, `k3s server --help` on the installed `v1.37.1+k3s1` lists all six keys,
      measured without root and recorded in the Baseline. The first native S3 snapshot is a
      production event, and the off-box cron keeps writing until H2.8b. The retention
      question becomes H2.8c, a read-only reading fifteen days after H2.8b.
- **X-10 (high): H1's key move leaves backup health's baked values stale.** Fixed for H2's
  part, and H1's part is a cross-slice note. Confirmed: the shim exports
  `LONGHORN_BACKUP_ARMED`, `LONGHORN_R2_ARMED`, `LONGHORN_BACKUP_CRON` and
  `LONGHORN_WEEKLY_BACKUP_MINUTE_HOUR` at template time. `services_from_changed_paths` routes a
  `group_vars/all.yml` change as deploy-plane with no setup role. It routes a `setup/k3s`
  defaults change to the `k3s` setup role, which `setup_routing.py` marks `on_tick_host:
  false`.
    - H2 keeps backup health on the host, so it does not take the CronJob option. *Backup
      health and the B2 pair stay on the host* now says the decision holds only with run-time
      reads, and assigns the four existing keys to H1's key-move PR (*Open questions* 7).
    - H2's own additions bake no such key. The trim arm reads its CronJob's schedule. Check
      12 reads the drop-in's schedule at run time, with a red case. The drill's two bounds and
      its namespace stay in `setup/k3s`, and a test holds the namespace pair equal.
    - The same hazard was in H2.7's plan to move `artifacts_k8s_node` to
      `group_vars/all.yml`. H2.7 now uses a test-held key pair instead.
- **X-14 (medium): no slice said which `k8s_host_files` rows H2 takes over.** Fixed. *What H2
  leaves in `k8s_host_files`* lists them. H2 takes over none, because D5 wave D waits on its
  family PRs. The section names what each conversion PR finds and who retires each remaining
  row. The retired-cron census accepts an arm in `k8s_host_files`. The ordering note between
  D5's crowdsec conversion and H5.8 belongs to D5 and H5, and H5 already carries it.
- **X-21 (medium): the lock steps ran unconditionally.** Fixed. H2.2c and H2.3b run
  `fact_status.py verify` and commit `docs/facts.lock` only until G2 retires the lock, in R4's
  wording.

Two edits follow from other slices' renumbering: H6.4 is now H6.4b, the PR that adds the
config drop-in, and H1.7 is now H1.7b.

### Round 10 (2026-10-11)

Twenty-six findings: one critical, nine high and sixteen medium. Each claim was checked against
the tree at `70591c307`, a read-only `kubectl` command, or the pinned upstream source read
through `gh api`. All 26 are fixed, and none is refuted. Five need changes in other slices,
handed over as cross-slice notes.

- **H2-R10-01 (critical): the drill's state is eight items, three of them mtimes, and a
  ConfigMap of one key per file would lose the rotation.** Fixed. Confirmed: the template keeps
  `attempts/`, `success/`, `seen/` and `retries/` beside the four files, and the rotation and
  retry read `stat -c %Y`. The Baseline lists all eight with their semantics. The store is the
  ConfigMap with epoch keys (`attempts.<pvc>` and the rest), read with `get` and written with
  `patch`. A hostPath was rejected because it keeps state on the host plane, pins the drill and
  keeps gate 2 on daniel-box's disk. H2.3c rewrites the state logic while the drill is still
  the host cron, and its one-time import arm copies the host directory first. The stamps
  module's ConfigMap arm covers `seen` and `success`, a missing seen entry takes the
  ConfigMap's creation time, and check 8 flags a candidate list with no seen entry at once.
  Red proofs cover the three cases asked for, plus the import.
- **H2-R10-02 (high): the drill's RBAC omitted `get backuptarget`.** Fixed. Confirmed: the
  template's `get backuptarget "$TARGET"` discards stderr and fails as "is it disarmed?". The
  grants are derived from every `$KUBECTL` call, with `patch` for each applied kind and `get`
  and `patch` on the stamp ConfigMap. The RBAC-against-script test names `get backuptarget`
  as its member. The `homelab`-only Role is checked against `PVC_NS`: all 30 `longhorn`-class
  claims are in `homelab` (measured).
- **H2-R10-03 (high): the CrowdSec Roles could not serve their scripts.** Fixed. Confirmed:
  `remote_allowlist.py:main` runs `get ingressroute -A` and `logs deploy/traefik`, and routes
  sit in three namespaces (72, 1 and 3, measured). The remote allowlist gets a ClusterRole for
  IngressRoutes, and every job gets `get` on deployments, `list` on pods and `create` and `get`
  on `pods/exec`. The RBAC test covers the Python modules through a recording runner.
- **H2-R10-04 (high): rollback assumed a prune that H1's tags never do.** Fixed. *Rollback*
  names what each Flux revert leaves live and gives an **operator-only** delete for each. H2.3e
  is reverted before H2.3d, so the CronJob is suspended before the host cron returns. *Risks*
  names the forward-only hold and the fix-forward recovery.
- **H2-R10-05 (high): the etcd-s3 Secret made `cluster-base` substitute.** Fixed. The Secret
  goes in `longhorn-backup`, which already substitutes the same `r2_*` keys. `cluster-base`
  stays non-substituting, with a render assertion. The Design states the effect on H1's DR
  order: native uploads start once `longhorn-backup` is Ready, and restores never read the
  Secret.
- **H2-R10-06 (high): check 12 in H2.8a would read DOWN from about 16:45 to 02:45.** Fixed.
  Confirmed: the S3 entries were created at 02:45 only. Check 12 and its `backup-health` apply
  move to H2.8b, after the native schedule has written to S3. The defaults comment edit moves
  with it, so H2.8a owes no hand apply.
- **H2-R10-07 (high): the bare owed-line clear, and hand applies not marked operator-only.**
  Fixed. Confirmed: `k3s-bringup.yml` imports `pre_tasks/load_secrets.yml` in each play. *Who
  writes each change* gives H1's four steps as the owed-line procedure, and every hand apply
  is **operator-only**.
- **H2-R10-08 (high): moving the drill's keys broke the shim and the absent arm.** Fixed.
  Confirmed: the shim exports `LONGHORN_RESTORE_DRILL_STAMP_DIR` and the cron task splits the
  cron key. The stamp-dir key stays until H2.3f, which drops its export and `_require_env`.
  The absent arm names no schedule key. The `longhorn` copy of the namespace key is
  `longhorn_drill_namespace`, so H1.7b's census row passes in either order.
- **H2-R10-09 (high): the drill state has more readers than three.** Fixed. Confirmed:
  `state.py:longhorn_restore_drill_run` and `_loops` read the path and the cron key. The
  Baseline lists every reader from `git grep -l`. H2.3a repoints `state.py` through
  `lib.kubectl`, under the docs-refresh cron's `KUBECONFIG`, and H2.3d moves its cadence lookup
  to `longhorn` defaults. Every test named is in H2.3a's, H2.3d's or H2.3f's list.
- **H2-R10-10 (high): janitorr also imports `stamp_deployed.yml`, and the fragments stayed
  behind.** Fixed. Confirmed in janitorr's task `Record the deployed janitorr health reader
  code` and `setup-drift-lib.sh`'s `(missing)` arm. Both imports go, each fragment gets an
  absent arm, and the retired-cron census flags a dropped import with no fragment arm. The same
  check found that `k3s_render_stamp_groups` lists the trim and drill templates, so H2.2c and
  H2.3d drop them.
- **H2-R10-11 (medium): the key-move description did not match H1.** Fixed. The Baseline,
  *Backup health and the B2 pair stay on the host* and *Open questions* 7 cite H1.7a, H1.7b and
  H1.7b's census row.
- **H2-R10-12 (medium): the drill's tag reason inverted C1.** Fixed. Confirmed against C1's
  X-12 wording. The drill moves to `longhorn`, which does not substitute, so the D3 edge is
  gone. The render test runs the pinned Flux CLI's `flux envsubst` only when the tag carries
  `postBuild`.
- **H2-R10-13 (medium): the canary could not go red on the group rule.** Fixed. H2.2a logs the
  set the unbounded rule would admit, and H2.2b's body quotes it against a volume reading.
- **H2-R10-14 (medium): the trim rule needed workload facts the reconcile RBAC lacks.** Fixed.
  The rule reads the Volume CR's `workloadsStatus`, measured on 2026-10-11, where a Deployment
  reports as `ReplicaSet`. No new grant is needed, and a test holds the arms' reads inside the
  reconcile RBAC.
- **H2-R10-15 (medium): the region key had no home.** Fixed. The Secret reads
  `k3s_longhorn_r2_region` from `longhorn-backup` defaults. *Open questions* 11 hands the
  off-box script's reader to H1.7b.
- **H2-R10-16 (medium): H6 makes none of H2.8a's commitments.** Fixed. The keys go in
  `k3s_server_config` in the component's defaults, H2.8a tests the 0644 mode, and *Open
  questions* 10 hands the rest to H6.
- **H2-R10-17 (medium): the retention fragment would keep saying 14.** Fixed. H2.8a points
  `_etcd_offbox_retention` at the drop-in's `etcd-snapshot-retention` and regenerates the
  fragment.
- **H2-R10-18 (medium): the off-box actuator stayed in the contract, and the disarm lever
  died.** Fixed. H2.8b edits the contract with its facts-lock step. The disarm is `etcd-s3:
  false` in `k3s_server_config`, and check 12 reports it as DOWN.
- **H2-R10-19 (medium): the images were unspecified.** Fixed. Shell jobs run in a
  digest-pinned `alpine/k8s`. Python jobs run in the pinned `python:3.14-alpine` with
  `kubectl` copied from that image by an init container. Each PR quotes the tools in its
  digest, and *Open questions* 9 asks the operator to accept the publisher.
  `registry.k8s.io/kubectl` was measured and rejected: it has no shell.
- **H2-R10-20 (medium): `logger` has no sink in a pod.** Fixed. Every verdict goes to stdout,
  and the moved contract names `probe.py loki-query '{container="longhorn-restore-drill"}'`.
- **H2-R10-21 (medium): the hand-run drill disappeared.** Fixed. *Hand runs* gives the
  **operator-only** `create job --from` forms, with the pin added through the Job's `env`.
  H2.3d updates `docs/longhorn-upgrade.md` and the incidents page.
- **H2-R10-22 (medium): the reaper census read only the command.** Fixed. The CronJob arm also
  reads every mounted ConfigMap, with a red fixture there. The setup arm's named member moves
  to `longhorn-backup-health.sh`.
- **H2-R10-23 (medium): the per-PR edges contradicted the header and the index.** Fixed. The
  header and *Open questions* 1 state each PR's edges and drop H1.8. The index change is a
  cross-slice note.
- **H2-R10-24 (medium): no ordering with M6.** Fixed. *Open questions* 8 names M6 batch B6
  and its Open questions 3 and 5, and says which path each family PR takes.
- **H2-R10-25 (medium): a trim Job's status may hide a per-volume failure.** Fixed. Confirmed
  from longhorn-manager `v1.12.1`: `StartVolumeJobs` returns nil whatever a volume job
  returns. The trim arm reads the job pod's log through `pods/log`, and H2.2a measures it on
  the canary.
- **H2-R10-26 (medium): H2.3b and H2.5b were too large.** Fixed. The drill runs as six PRs,
  in which the state rewrite precedes a move that `git diff -M` shows as four edits. H2.5b
  splits into H2.5b, H2.5c and H2.5d, one per role.

### Round c1 (2026-10-11)

Nine findings: two high and seven medium. Each was checked against the tree at `70591c307`,
against H1, D5 and contracts.md, or against a read-only command on daniel-box. All nine are
fixed, and none is refuted. Five documents need changes, handed over as cross-slice notes:
contracts.md, H1, H6, D5 and R4.

- **H2-C1-01 (high): the drill's `homelab` objects went in `longhorn`, which H1's DR order
  waits on before anything creates `homelab`.** Fixed by option (b). Confirmed: H1's step 6
  waits on `longhorn` Ready, `ansible/deploy.yml`'s tasks `Build the workload namespace
  manifest` and `Apply the workload namespace` create `homelab`, and D5's `homelab-namespace`
  edge arrives only at its namespace PR. The drill's objects move to a new Flux-born tag,
  `longhorn-restore-drill`, with `depends_on: [longhorn]`. It does not substitute, renders no
  Namespace, and is not in step 6. D5's rule generates its `homelab-namespace` edge. Option (a)
  was rejected because it parks the drill behind D5's namespace PR, D5's step 29, and keeps
  drill churn in the forward-only tag. A renderer test fails when a step-6 tag renders into a
  namespace that neither a step-6 tag nor k3s creates, with a `homelab` ConfigMap in
  `longhorn` as its red fixture. The tag prunes, so the stamp ConfigMap carries `prune:
  disabled`, and *Open questions* 12 asks C3 for that exemption. The Design, Steps, *Open
  questions* 1, Rollback, Risks, File tree, tests and Complexity delta follow.
- **H2-C1-02 (high): five moved scripts template push tokens, and one the qBittorrent
  password, into bodies the plan stages in ConfigMaps.** Fixed. Confirmed with `grep -rnoE`
  over the six templates. *No secret in a ConfigMap* moves each value into the role's Secret,
  read through `secretKeyRef`, and names the variable each job reads. Each family PR says
  which key it moves. A render test in `ansible/tests/k8s/` binds every `secrets.yml` key name
  to a sentinel and fails when a ConfigMap's data holds one. Its red fixture holds `{{
  monitor_bridge_appsec_push_token }}`.
- **H2-C1-03 (medium): the drill's deadline path was unspecified, and a killed pod would
  orphan the restore.** Fixed. Confirmed: the template installs only `trap cleanup EXIT`, and
  its waits are `sleep 10` and `sleep 5` polls with a 300-second probe bound. H2.3c adds TERM
  and INT traps on the host first, so H2.3d stays a four-edit move. H2.3d sets
  `activeDeadlineSeconds` to 3600 and `terminationGracePeriodSeconds` to 300, with a render
  test on the deadline and a red proof that sends TERM during the restore poll.
- **H2-C1-04 (medium): H2.3c had no gate on H2.3b's ConfigMap being live.** Fixed. *Open
  questions* 1 adds H2.3c → H2.3b, read live. Step 7 starts with a `kubectl get configmap`
  precondition, and the script fails with `stamp ConfigMap absent` on NotFound.
- **H2-C1-05 (medium): reverting H2.3c left the readers on a frozen ConfigMap.** Fixed by
  reverting H2.3a in the same revert PR and hand apply, after H2.3f's revert if it has landed.
  The alternative, readers that take the newer stamp per item, would have needed a red proof
  of a newer directory stamp beating a frozen ConfigMap one. It was not taken, because the
  joint revert adds no reader logic.
- **H2-C1-06 (medium): H2.2a's pre-merge measurement could not be taken.** Fixed. Confirmed:
  H2.2a itself adds the RecurringJob. The canary reading moves after the promote and gates
  H2.2b, and it proves that the native job trimmed the canary. The swallowed failure rests on
  the source reading and the trim-evidence red proofs.
- **H2-C1-07 (medium): nothing held the mode of `config.yaml.d`.** Fixed. Confirmed: `stat`
  reports the directory missing on daniel-box, and `ls -la /etc/rancher/k3s/` shows sibling
  directories at `drwx------`. H2.8a's mode test covers the directory at 0755, with a 0700 red
  fixture, *Open questions* 10 asks H6 for it, and check 12 reports `drop-in unreadable` apart
  from a missing file.
- **H2-C1-08 (medium): the hourly group rule lags the 06:10 trim.** Fixed by accepting the
  window in *Risks* and the Design. It opens at H2.2b, because H2.2a's bound keeps `trim` on
  the canary alone. Its cost is one attach and detach of an idle volume, the same attach
  `pi-peer-backup-data` gets nightly. H2.2b compares the pre-trim pass's `would-admit` line
  with the volume states at the run and names any hit. No production scale-to-zero drill was
  added: it would only reproduce the accepted window.
- **H2-C1-09 (medium): janitorr's grants omitted `get` on `deployments.apps`.** Fixed.
  Confirmed in `janitorr_health.py`'s `logs deploy/janitorr` and `configarr_health.py`'s
  `logs job/<name>`. *The *arr checks* derives each module's grants, and the RBAC test gains a
  red case for the janitorr call.

One correction rode along: H1.1c, not H1.1b, adds the per-tag object census, and the four
references to it now say so.

### Round c2 (2026-10-11)

Six findings: two high and four medium. Each was checked against the tree at `70591c307`,
against H1's plan text, or against upstream source read through `gh api`. All six are fixed,
and none is refuted. One fix keeps a bounded exception, recorded under H2-C2-01. H1 needs
changes, handed over as cross-slice notes.

- **H2-C2-01 (high): the grants-first PRs H2.4a and H2.5a fail CI, because the netpol test
  refuses a caller label that no rendered pod carries.** Fixed. Confirmed:
  `ansible/tests/k8s/test_netpol_from.py::test_every_caller_is_a_label_some_rendered_pod_carries`
  asserts `unknown == set()`, and `UNRENDERED_CALLERS` admits only a `--labels app=<caller>`
  task. uptime-kuma's entry carries `netpol_from` without `netpol_role_owned`, and
  `ansible/roles/k8s/netpol-baseline/CLAUDE.md` orders netpol-baseline last. H2.4a and H2.5a
  are retired, and each grant lands in its CronJob's commit. *Each grant lands with its
  CronJob*, *Who applies each grant*, *First run*, Steps 11 to 15, *Rollback*, *Risks*,
  *Open questions* 1 and 8, and the tests follow. The first gated run is handled per job:
    - The qbittorrent prefs check keeps its gate. Its fence is role-owned, so the role's one
      apply creates it with the CronJob, before the gate. It gains a bounded connect retry for
      kube-router's late learning of a new pod's labels.
    - The home allowlist keeps its gate. It uses the host network and pushes through Traefik,
      so it needs no grant.
    - The remote allowlist, the AppSec verify and janitorr health lose their gate. crowdsec and
      janitorr set `k8s_autodeploy: false`, so the gate is optional there, and each PR records
      the first scheduled run.
    - configarr health keeps its gate, as a bounded exception to the finding's two options.
      `ansible/roles/k8s/configarr/defaults/main.yml:k8s_autodeploy` is `true`, and
      `ansible/tests/deploy/test_k8s_autodeploy_batch_gates.py::test_auto_deployable_roles_gate_every_batch_workload_they_render`
      refuses an ungated CronJob in such a role. Applying netpol-baseline first would invert
      the order its `CLAUDE.md` requires. The landing's one gate run therefore pushes into the
      closed fence. `kuma_push` stops after three attempts, the deadline sits between that
      bound and `cronjob_gate_timeout`, and the gate counts a started container as a run. A
      push-off mode was not adopted, because it adds a code path whose failure is a silent
      missing push.
- **H2-C2-02 (high): the drill's `mktemp` template carries a `.yaml` suffix, which BusyBox
  `mktemp` in `alpine/k8s` rejects.** Fixed. Confirmed: the template's `MANIFEST` line holds
  `XXXXXX.yaml`, the script runs without `-e`, and `alpine-docker/k8s`'s Dockerfile is `FROM
  alpine:edge` with no `coreutils`. H2.3c drops the suffix on the host, so H2.3d stays a
  four-edit move. kubectl reads a file named on the command line whatever its extension. Each
  shell job's script tests gain an image leg under the pinned digest, with the suffixed
  template as the drill's red case. The AppSec verify has no harness, so H2.4b adds one.
  `readOnlyRootFilesystem` is required only of karakeep's chrome, so no `emptyDir` is added.
- **H2-C2-03 (medium): the drill's RBAC omitted the `list` and `watch` that `kubectl
  delete`'s wait uses.** Fixed. Confirmed: kubectl's `pkg/cmd/delete/delete.go` returns nil
  on `errors.IsForbidden` after the wait. The drill's grants gain `list` and `watch` on pods,
  PVCs and PVs, and `watch` on Volumes. *Identity* states that `delete` implies both, and the
  RBAC test gains a red case for a `delete`-only Role. The weaker `--wait=false` option was
  not taken.
- **H2-C2-04 (medium): `longhorn-reconcile` was placed inside the `longhorn` tag and role.**
  Fixed. Confirmed: H1's *Six Flux-born tags* makes it a tag and role of its own, and H1 names
  DaemonSet `longhorn-manager` as `longhorn`'s member. The arms, their unit and RBAC-subset
  tests and the file tree point at `ansible/roles/k8s/longhorn-reconcile/`. The step-6
  guard's named member is `longhorn-manager`. The Baseline, H2.2a, H2.2b, *Rollback* and
  *Risks* now say that H2.2b changes only a tag that is not forward-only, so a failed H2.2b
  revision resets.
- **H2-C2-05 (medium): H2.8a depended on H1 moving `k3s_longhorn_r2_region`, which H1 does
  not carry.** Fixed. Confirmed: `grep -c r2_region` on H1's plan prints 0. *Open questions*
  11, the etcd-s3 design and H2.8a gain a gate. If H1.7b has not defined the key in
  `longhorn-backup` defaults, H2.8a adds `longhorn_backup_etcd_s3_region` there, a name no
  `setup/` file holds, with a render test against the R2 BackupTarget's region. H2.8a then
  still touches no `setup/k3s` path. The finding's literal fallback, adding the `setup/k3s`
  twin in H2.8a, was not taken, because it would make H2.8a a hand apply.
- **H2-C2-06 (medium): `etcd-s3: true` in the drop-in sends a local-path restore to S3.**
  Fixed. Confirmed: k3s's `pkg/etcd/etcd.go` at `v1.37.1+k3s1` retrieves from S3 when
  `EtcdS3 != nil` under `ClusterResetRestorePath`, and returns `cannot use S3 config secret
  when restoring snapshot`. *The drop-in reaches every restore on daniel-box* and H2.8a's
  docs edits state that a local restore passes `--etcd-s3=false` and an S3 restore keeps its
  explicit flags.

### Round c3 (2026-10-11)

Two findings: one high and one medium. Each was checked against the tree at `70591c307`,
against upstream source read through `gh api`, and against the live cluster with read-only
`kubectl`. Both are fixed. One sub-claim of H2-C3-01's evidence is refuted, and the finding
stands without it. H1 needs changes, handed over as cross-slice notes.

- **H2-C3-01 (high): the `trim` group label is read as a backup tier by the drill, the
  reapers and the probe.** Fixed, with the finding's first option: the per-job label.
  Confirmed in the tree:
    - the drill template's `IN_BACKUP_GROUP` admits any group key but `no-backup`, and the
      ELIGIBLE, OVERSIZE and NOPVC sets share it;
    - `scripts/backup/longhorn_reap_lib/logic.py:backup_owner_map` and `snapshot_owner_map`
      keep the last group key, and `trim` sorts after `default`, `no-backup` and every
      `weekly-backup-d*` shard;
    - `scripts/diagnostics/probe_lib/longhorn_cluster.py:volume_shard_labels` maps the same
      way.
  The check also found a fifth reader, `ansible/seed_volume_backup.yml`'s `seed_group`. It
  takes the first sorted key, so it happened to survive, and the label choice covers it too.
  Confirmed upstream at longhorn-manager `v1.12.1`: `app/recurringjob/volume.go` selects
  `recurring-job.longhorn.io/<job>=enabled` volumes before it reads `spec.groups`, and
  `datastore/longhorn.go:labelRecurringJobDefault` counts both prefixes. The explicit
  `default` arm therefore stays. Measured live: no Volume carries a per-job key, no Volume
  carries two group keys, and 18 `no-backup` Volumes are `attached`. The Design (*A per-job
  label, not a group label*), the Baseline, H2.1, H2.2a, the Verification command and red
  proofs, *Rollback* H2.2a and H2.2b, *Risks*, the tests list and *Complexity* follow. The
  finding's fixtures (a) and (b) land in H2.1, before H2.2a, each with a red half that uses
  the group form. They come with a `snapshot_owner_map` sentinel case and a reconcile case
  that refuses an arm writing the group form. The trim RecurringJob carries a `DECIDED`
  marker at `groups: []`.
    - **Refuted sub-claim:** `scripts/infra_map/live.py` is not a group-label reader. Its only
      label partition, in `parse_kubectl_nodes`, compares the `node-role.kubernetes.io` group,
      and `grep -n -i -E 'group|recurring'` on the file prints nothing else.
- **H2-C3-02 (medium): no census covers the drill role's keys, which keep the `setup/k3s`
  prefix.** Fixed. Confirmed: H1's census row reads H1's six Flux-born roles (H1, *Six
  Flux-born tags*), and H2's Baseline and *Keys* said five. H2.3d adds a `Census` row of its
  own, because H2.3d does not need H1.7b. The row's red fixture is a `setup/` template that
  names `k3s_longhorn_restore_drill_timeout_s`, and its named members are the `_cron` and
  `_timeout_s` keys in the drill role's defaults. A `git grep -l` for the eight keys at
  `70591c307` finds, under `ansible/roles/setup/`, only `setup/k3s`'s defaults, the cron task
  H2.3d turns into an absent arm, and the template H2.3d moves, so the row is clean at H2.3d.
  *Risks* now cites the row. The count reads six in the Baseline and *Keys*. The stamp
  ConfigMap's namespace now renders from `longhorn_drill_namespace`, the role's own key, so the
  `setup/k3s` key that stays has no Flux reader, as *Risks* claims.

### Round integration-2 (2026-10-11)

Three findings: one high and two medium. Each was checked against the tree at `70591c307`
and against the other slice plans in this directory. All three are fixed in H2, and none is
refuted. The parts that belong to D2, H1, H6 and contracts.md are handed over as cross-slice
notes.

- **X2-06 (high): H1, H2.3b and H6 add `containers_list` entries while the deployer still
  narrows, which triggers D1's unattended 23-reader-tag apply.** Fixed in H2 with the
  finding's fallback, and the preferred fix is requested from D2 (*Open questions* 13).
  Confirmed: `scripts/deploy_tools/narrow_lib/containers.py:changed_fields` returns None when
  `list(old) != list(new)`, so an added or a removed entry reaches
  `scripts/deploy_tools/narrow_lib/broad.py:_list_reader_tags` with `changed` None. D1's
  *Baseline* measured 23 tags at `70591c307`. D2.1a's `DEPLOY_MECHANICS_FIELDS` covers only
  an `owner`-only edit of an entry that exists. A grep of H2's *Steps* for `entry` finds one
  PR that adds or removes an entry, H2.3b. A `netpol_from` edit in a family PR changes a
  field, so `changed_fields` returns a dict for it. (Corrected in round i1: that dict avoids
  only the 23-tag fan-out. The field edit still narrows to the edited role, the entry's own
  tag and five whole-entry readers, and the tick applies the denylisted ones among them
  forward-only, so the family PRs carry the stop rule too.) H2.3b now
  quotes the narrow output, runs D1.2's stop rule before the PR and before `land.sh`, quotes a
  `--dry-run` of the narrowed tags, and follows D1.2's *Landing* and *If the narrowing
  refuses* paths, with D1's two refusal cases kept apart. A narrow output that names only
  `longhorn-restore-drill` is accepted only once D2's mapping is on `origin/master`, and the PR
  body quotes its test. Before then such an output stops the session, as D1.2 rules.
  *Rollback* H2.3b gives the revert the same handling, because a removed entry fans out the
  same way. *Verification* gains the commands and the red proof. *Risks* and
  *Complexity* name the unattended apply and when it ends.
- **X2-11 (medium): the register and slice index lack about 20 requested rows and sub-edges
  that slices name as merge gates.** Fixed in H2's part. Confirmed: contracts.md's
  *Requested amendments not yet applied* lists nine rows and none of them is H2's, and its
  index row for H2 still reads "H2.3b also D3; H2.8a also D3, H6.4a and H6.4b". H2.3b's step
  and the header now name the C3 prune-exemption gate of *Open questions* 12 at the PR it
  gates. *Open questions* 1 asks for the register to list H2's requests until the index
  carries them, with H2.3c → H2.3b, read live, named. The edits to contracts.md go in a
  cross-slice note.
- **X2-17 (medium): H1.7b moves the R2 BackupTarget template without the
  `k3s_longhorn_r2_region` key it reads.** Fixed in H2's part. Confirmed: `git grep -n
  r2_region 70591c307` finds the key defined only in `setup/k3s` defaults and read by
  `longhorn-backuptarget-r2.yaml.j2` and `etcd-snapshot-offbox.sh.j2`, and a grep of H1's
  plan for `r2_region` still finds nothing. H2.8a already carried the gate and the fallback
  variable, `longhorn_backup_etcd_s3_region`. The gap was H2.8b, which cut the script's region
  read but deleted no region key. H2.8b now deletes the region key the script reads from
  `setup/k3s` defaults, whether `k3s_longhorn_r2_region` or H1.7b's twin, with any test that
  holds it. Before deleting, it quotes a `git grep` of `ansible/` and `scripts/` showing the
  script as the key's last reader, and keeps the key if another reader remains. *Open
  questions* 11 proposes the twin's name, `k3s_etcd_s3_region`, which `setup/k3s` defaults do
  not use at `70591c307`. The Design's *etcd-s3*, *Risks* and the H2.8b tests list follow.

### Round i1 (2026-10-12)

Two findings from the per-slice review: one high and one medium. Both were checked against
the tree at `70591c307` and against D1 and D2. Both are fixed in H2, and neither is refuted.
The H1 and D2 parts go in cross-slice notes.

- **H2-I1-01 (high): the family PRs that edit `netpol_from` also trigger an unattended
  forward-only apply of denylisted tags, and H2 exempts them from D1.2's stop rule.**
  Fixed. Confirmed in code: `scripts/deploy_tools/narrow_lib/broad.py:_containers_list_tags`
  returns the entry's own tags from `containers.py:entry_change_tags` together with
  `broad.py:_list_reader_tags`, and `broad.py:narrow` adds `broad.py:_changed_half`, which
  maps the edited k8s role through `cs.k8s`. The marker `deploy_narrow.py:DECIDED the broad
  plane ignores` keeps `K8S_AUTODEPLOY_DENYLIST` out of that apply. Measured by calling
  those functions on an in-memory copy of `daniel-box.yml` with one label appended to an
  entry's `netpol_from`. uptime-kuma's edit gives `uptime-kuma` plus artifacts,
  monitor-bridge, netpol-baseline, pihole and uptime-kuma, and qbittorrent's gives
  `qbittorrent` plus the same five. uptime-kuma, monitor-bridge, pihole, crowdsec, janitorr
  and qbittorrent set `k8s_autodeploy: false`. The finding names H2.4c's sibling. H2.4c
  itself adds no grant and edits no entry, so it keeps the plain `land.sh` path. The four
  PRs that edit an entry are H2.4b, H2.5b, H2.5c and H2.5d. Changes:
    - *Baseline* gains the measurement.
    - *Who writes each change at merge time* and *Who applies each grant* name the real
      landing list, `<role>,uptime-kuma,artifacts,monitor-bridge,netpol-baseline,pihole`,
      in place of `<role>,netpol-baseline`.
    - A new Design section, *Landing a `containers_list` edit*, lists each PR's expected
      narrow output and holds the one four-step block: the narrow quote, the stop rule over
      every named tag, the `--dry-run` quote and D1.2's landing paths. H2.3b, H2.4b and H2.5b
      to H2.5d each run it, and so does each one's revert (*Rollback*).
    - *Verification* gains the red case of a stale uptime-kuma that refuses a family PR's
      merge.
    - *Risks* replaces "H2.3b is the only H2 PR that adds or removes an entry" with the five
      editing PRs and their reverts.
    - The X2-06 log sentence carries a correction.
- **H2-I1-02 (medium): H2.3b, *Open questions* 13, *Risks* and *Complexity* rest on an "own
  tag only" mapping that D2 has refuted.** Fixed. Confirmed: D2 (*An added or removed entry
  that is not Ansible-owned (D2.1c)*) rejects the own-tag mapping because the bare-entry
  render moves artifacts' `known_services.json`, and it names five reader tags, three of
  them denylisted (D2 *Risks*). contracts.md's index makes H1 depend on D2 and H2 on H1, so
  D2.1c is on `origin/master` before H2.3b. D2's entry-point table and D2.7's quote show
  that `deploy_tags.py narrow` still prints a Flux-owned tag, and D2.2's deployer skip drops
  it, so H2.3b expects `longhorn-restore-drill` plus the five readers. Changes:
    - H2.3b drops the branch that accepted a single-tag output and states that expected list.
    - *Verification*'s red-proof bullet, *Risks* and *Complexity delta* say that the stop-rule
      obligation ends only at D6.12.
    - *Open questions* 13 cites D2.1c as landed policy, quotes D2's reason and withdraws the
      own-tag request.
    - *Rollback* H2.3b no longer says `changed_fields` returns None for the removed entry.
      Under D2.1c, the revert reaches the same five readers.
    - The block's `--dry-run` step leaves out the drill's tag, which `deploy.sh` refuses with
      exit 5 (D2's entry-point table).

### Cross-slice follow-up (2026-10-12)

- Cross-slice note from H1: no change. H1.1h's guard sits after the `server.yml` import,
  and a grep of this plan finds no `server.yml`, so no H2 PR moves it or adds a writer to it.

### Reader-set follow-up (2026-10-15)

- **#4975: the per-PR narrow outputs named the `70591c307` whole-entry readers.** Confirmed.
  8e79e95d6 moved those readers onto named filters, so the round i1 lists no longer match
  the tree. `deploy_tags.py narrow` over a scratch commit appending a label to uptime-kuma's
  and qbittorrent's `netpol_from` printed `netpol-baseline,qbittorrent,uptime-kuma`. Over a
  scratch commit adding the drill's entry it printed
  `artifacts,blackbox-exporter,longhorn-restore-drill,pihole,uptime-kuma`, and over the
  reverse range `artifacts,blackbox-exporter,pihole,uptime-kuma`. Fixed: *Baseline*,
  *Landing a `containers_list` edit*, *Who applies each grant*, H2.3b, H2.4b, H2.5b to
  H2.5d, *Verification*'s red case, *Rollback*, *Risks*, *Complexity delta* and *Open
  questions* 13 now carry those lists and recount the denylisted tags. H2.5d's output no
  longer names uptime-kuma, so its red case is a stale qbittorrent. The round i1 and
  integration-2 entries above keep the lists they measured.
