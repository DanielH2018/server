# H7 — Remaining roles, then delete Ansible

Status: PLANNED
Depends on: H2, H5, H6, R4, per PR (the *Waits on* column of *Steps, as PRs*)
Unblocks: none (it closes Area 3)

Re-sliced on 2026-10-17 at the operator's request: 65 steps became 37 PRs. The 65 count each
PR the old list planned. Its 20 numbered items name 48 steps, and the canary triples of H7.6
and H7.7, the two host PRs of H7.12c and the five PRs of H7.19a count once per PR. The
re-sliced PRs are numbered from H7.21, so no re-sliced ID equals an old one. No step has
landed.

Related issues: [#4561](https://github.com/DanielH2018/server/issues/4561), daniel-server's IPv6 default route. Its netplan file moves into the
host package.

H1 to H6 move the cluster plumbing, the cluster-reading crons, the Pi's heartbeats and
Compose, the UPS chain on daniel-server, and k3s's binary and flags. This slice moves
everything the setup roles still own into the three host packages or into chezmoi. It then
finishes `bootstrap-host.sh`, proves it by rebuilding the Pi from a blank SD card and by
installing daniel-box's packages and k3s in a drill guest, retires the GitOps deployer that
D6 kept, and deletes the bring-up playbooks and ansible-core. Names and paths follow H3's
*Shared names for the host slices*. Every move follows H3's *host handoff rule*, and
secrets follow H4.

## Goal and exit criterion

Afterwards, no Ansible playbook, role task or collection remains in the tree. `ansible-core`
is out of `pyproject.toml` and `uv.lock`. Each host is configured by `homelab-<hostname>`,
`homelab-updater` and `homelab-tools`. The operator's user-level tooling is
configured by chezmoi. The GitOps deployer is gone, and a package timer fast-forwards the
primary checkout on daniel-box.

One piece of the old model remains, and this slice keeps it on purpose: that primary
checkout. `homelab-checkout-sync` keeps it current with a 10-minute timer, the tree lock, a
dirty-tree refusal and the CI ancestor walk, because the checkout-reading crons run from it.
*Open questions* 12 proposes how a later slice retires it.

The exit criterion is seven observations:

1. **Nothing of Ansible is left.** Four listings, each with its expected output:
    - `git ls-files ':(glob)ansible/*.yml' ':(glob)ansible/*.sh'` prints only
      `ansible/secret_rotation.yml`, which is a data registry, not a playbook.
    - `git ls-files ansible/roles/setup ansible/roles/containers ansible/pre_tasks
      ansible/post_tasks ansible/tasks ansible/prune_backups ansible.cfg .ansible-lint
      ansible/requirements.yml` prints nothing. That includes D5's setup role
      `k8s_host_files`, which H7.18c deletes.
    - `git ls-files 'ansible/roles/*/*/tasks/*'` prints nothing, so R4's two runbook roles
      are gone too.
    - `git grep -n '^containers_list:' -- ansible/inventory` prints nothing. H1.9 removes
      the key from daniel-box and daniel-server, and H7.20d removes the Pi's.

   `uv tree | grep -c ansible-core` prints 0, and `grep -c ansible-lint prek.toml` prints 0.
   The keep-list is `ansible/vars/`, `ansible/inventory/`, `ansible/.sops.yaml`,
   `ansible/README.md`, `ansible/secret_rotation.yml`, `ansible/filter_plugins/`,
   `ansible/templates/`, `ansible/tests/`, and the `templates/`, `defaults/` and `files/` of
   each `ansible/roles/k8s/` role that C1's renderer reads. The Pi's `compose_services`
   stays in `ansible/inventory/host_vars/daniel-pi.yml` as renderer data.
2. **The Pi rebuild.** The operator follows the Pi runbook in `docs/break-glass.md` on a
   blank SD card flashed with stock Ubuntu 24.04. The Pi installs `homelab-daniel-pi` at the
   version the builder last published, runs the Compose project healthy with the restored
   wg-easy peers, and pushes its heartbeats. Prometheus reads
   `homelab_host_applied_info{host="daniel-pi"}` at the installed version, which shows the
   package recreated the textfile directory the updater writes. A WireGuard client connects through it. The
   repo gains at most two commits: the recipient commit, and the host-key pin unless the
   break-glass kit restored the old host keys.
3. **The libvirt drill.** The etcd restore drill in a throwaway guest runs green on
   daniel-server from its package timer. It reads the join token from the agent's installer
   env file, `/etc/systemd/system/k3s-agent.service.env`, which H6 leaves in place.
4. **The cluster-node bring-up.** Two proofs, because no single guest can carry both.
    - *The host half, in a drill guest.* A transient guest on daniel-server plays
      daniel-box. `bootstrap-host.sh daniel-box --local-build <clone>` builds and installs
      `homelab-tools`, `homelab-updater` and `homelab-daniel-box` with no
      homelab repo reachable. Exactly one k3s unit exists, `k3s.service`, and
      `k3s kubectl get node daniel-box` reads Ready. `k3s --version` reports the script's
      embedded `k3s_version`, which the *Bootstrap pins* test holds equal to H6's
      `system-upgrade` key. The guest holds no production
      credential: k3s generates a guest-only server token, and the guest's age key is no
      SOPS recipient.
    - *The Flux half, on kind.* R4 PR 7b's `flux-bootstrap-drill.yml` runs green on the PR
      that writes the cluster-node procedure. The procedure's Flux steps are R4's, so that
      run proves them: the operator creates only `sops-age`, and Flux's `cluster-vars`
      Kustomization reads Ready and produced `homelab-cluster-vars`.

   The token sources are named, not implied. daniel-box's server token returns from
   `docs/break-glass.md` item 2. daniel-server's `K3S_URL` and `K3S_TOKEN` reach the
   installer from the operator, and the installer writes them to the agent's env file.
5. **No deployer.** On daniel-box, `systemctl list-unit-files 'gitops-deploy*'` lists
   nothing, `/opt/gitops-deploy` is absent, and `homelab-checkout-sync.timer` is active. The
   primary checkout's `HEAD` is the newest first-parent ancestor of `origin/master` whose CI
   verdict is green. It equals `origin/master` within one timer interval of master's CI
   verdict turning green. No tracked module outside the deployer's role imports a deployer
   module, and `uv run pytest --collect-only` reports no import error.
6. **The ADR.** An ADR under `docs/adr/` records this design and supersedes the bring-up
   sections of the setup roles' `CLAUDE.md` files. Approving it is **operator-only**.
7. **Reconcile stays under its ceiling.** The reconcile-size census in `hosts/lib/tests/`
   passes at the head of H7.20a. It counts the source lines of `hosts/lib/homelab-reconcile`
   and of the `hosts/lib/` modules that only reconcile imports, without tests, blank lines
   or comments. The count is at or below the ceiling that *Reconcile's line ceiling*
   derives.

## Baseline at `70591c307`

- **The setup tree.** `git ls-files ansible/roles/setup ansible/roles/containers | wc -l`
  prints 408. Per role, the counts are:
    - `gitops_deploy` 92, `k3s` 67, `claude_code` 43 and `initial_setup` 31;
    - `renovate_agent` 23, `fake_remux` 20, `hypervisor` 19 and `renovate_notify` 18;
    - `common` 17, `deploy_ui` 16, `optimize_pi` 8, `docker_install` 8 and `nut_host` 7;
    - `render_records` 6, `config_files` 4, `chezmoi_setup` 4, `sops_setup` 3 and
      `github_cli` 2.

  The design's figure of 595 files counted more than these two trees. `homelab_builder`
  does not exist yet; H3 adds it.
- **What the setup roles install.** 78 task files hold 37 `ansible.builtin.cron` tasks
  (`git grep -c 'ansible.builtin.cron:'` over `ansible/roles/setup/*/tasks/*.yml`, summed),
  some of them absent arms. The roles carry 9 `*.timer.j2`, 17 `*.service.j2` and 8
  `*.conf.j2` templates. Each role's generated `## At a glance` block lists its crons and
  timers. `initial_setup`'s block lists 13 crons and 3 timers, among them `Weekly apt
  autoremove`, `Weekly dpkg purge orphaned configs`, `Clean unused Docker images`,
  `kuma-check-secret-rotation-audit` and `kuma-check-loki-read-route`. `claude_code` ships
  five timers and `claude-rc.service`, plus `claude-land@.service.j2`,
  `fleet-slice-caps.conf.j2`, `login-slice-caps.conf.j2`, `pytest-fanout-cap.conf.j2` and
  the `claude-rc-alert` unit that `common/tasks/alert_unit.yml` installs.
- **Playbooks.** A plain pathspec is the wrong tool here: `git ls-files 'ansible/*.yml' |
  wc -l` prints 288, because a git pathspec `*` crosses `/`. The glob form
  `git ls-files ':(glob)ansible/*.yml'` lists ten files:
    - the playbooks `ansible/initial_setup.yml` (68 lines), `ansible/bootstrap.yml` (38),
      `ansible/k3s-bringup.yml` (184), `ansible/deploy.yml` (210, which R4 and D6 shrink),
      `ansible/preflight.yml` (98), `ansible/prune_backups.yml` (87),
      `ansible/seed_volume_backup.yml` (292) and `ansible/k3s-storage-smoke.yml` (36);
    - `ansible/requirements.yml` and `ansible/secret_rotation.yml`.

  Playbook-only task files live in `ansible/prune_backups/tasks/b2-drain.yml`,
  `ansible/pre_tasks/load_secrets.yml`, `ansible/tasks/` and `ansible/post_tasks/` (R4
  deletes the last two). The shell wrapper is `ansible/bring-up.sh` (203 lines).
  `ansible.cfg` and `.ansible-lint` are tracked at the repo root.
- **A bring-up playbook edit parks the deployer.**
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_changes.py:_BROAD_MANUAL_PREFIXES`
  holds `ansible/bootstrap.yml`, `ansible/k3s-bringup.yml` and `ansible/initial_setup.yml`.
  The `deploy_defer.py` module docstring says a park means "no ff-merge, a journal line every
  tick, a page once per SHA", that "`cs.broad_manual` still parks", and that while parked
  "every other session's landing exits 4 from `deploy.sh` (tree behind origin) until a hand
  pulls the primary checkout". `initial_setup.yml` lists 15 setup roles, D5 adds
  `k8s_host_files` to it, and `k3s-bringup.yml` lists `k3s`. A play that names a missing role
  fails to parse, so deleting a role means editing its playbook. H3's Baseline records the
  same trap, and H4 avoided it with a `meta: end_role` guard inside the role.
- **Some setup roles record a `manual_plane` line instead of applying.**
  `deploy_defer.py:unapplyable_setup_roles` returns each changed role that
  `deploy_setup_roles.py:tick_applies_setup_role` rejects, which is any role whose
  `initial_setup.yml` entry does not admit daniel-box. At `70591c307` that is `optimize_pi`
  (gated on `optimize_pi_host`, which is daniel-pi), `k3s` (applied by `k3s-bringup.yml`)
  and `common` (applied by no playbook). `deploy_cross_role.SETUP_FILES_SHIPPED_BY_OTHER_ROLES`
  routes a `common/files/host_lib.py` change to its consumers, `k3s` among them, so it
  records a `k3s` line too. `hypervisor`, `docker_install`, `initial_setup`, `nut_host` and
  `claude_code` admit daniel-box, so the tick applies them there and records nothing. Root
  `CLAUDE.md`'s *When to wait* lists an uncleared line as a stop.
- **R4 leaves two runbook roles.** R4's exit 1 keeps `ansible/roles/k8s/volume-revert/` and
  `ansible/roles/k8s/longhorn-api/` with their tasks, and R4's *Open questions* 2 says H7
  replaces the runbook with a task-free tool. `longhorn-api/tasks/resolve.yml` reads
  `ansible_hostname`.
- **Ansible imports outside the playbooks.** `git grep -l -E '^\s*(from|import) ansible\b'
  -- '*.py'` lists 25 files:
    - `ansible/tests/_helpers.py`, which imports `ansible.plugins.filter.core.FilterModule`,
      `ansible.plugins.filter.mathstuff.FilterModule` and
      `ansible.plugins.test.core.TestModule` at module top and registers all three sets into
      its Jinja env. That is every core filter and test, far more than R4's seven.
      `git grep -l -E '^\s*(from _helpers import|import _helpers)' -- ansible/tests | wc -l`
      prints 361: 105 in `setup`, 63 in `services`, 52 in `deploy`, 50 in `repo`, 43 in `k8s`,
      33 in `longhorn`, 4 in `staging`, and 11 shared helper modules at the top of
      `ansible/tests/`;
    - ten tests that import `Templar` or `DataLoader`: three image-builder tests in
      `ansible/tests/deploy/`, `test_derived_manifest_files.py` in `k8s`,
      `test_snapshot_name_length.py` in `longhorn`, `test_crowdsec_ban_gate.py` in
      `services`, and four in `setup` (`test_agent_clone_fast_forward.py`,
      `test_agent_operator_config.py`, `test_claude_agent_memory_seed.py` and
      `test_coredns_image_follows_bundled_manifest.py`);
    - five tests that import `ansible.errors`, one per filter plugin they test
      (`test_toposort.py`, `test_k8s_autodeploy_denylist.py` and
      `test_k8s_retired_services.py` in `deploy`, `test_authelia_access_tiers.py` in
      `services`, and `test_claude_agent_profiles.py` in `setup`);
    - `ansible/tests/setup/test_pi_escalation_timeout.py`, which imports the ssh connection
      plugin;
    - `scripts/deploy_tools/deploy_lib/flags.py` (`PlaybookCLI`), and
      `scripts/lib/ansible_jinja_env.py` with its test in `scripts/lib/tests/`;
    - the five filter plugins below.

  R1, R3 and R4 add tests that use ansible-core's `Templar` as an oracle, and R4's exit 4
  proves only cluster mode without ansible-core.
    - `scripts/lib/ansible_jinja_env.py:register_ansible_filters` imports `b64encode`,
      `comment`, `get_hash`, `mandatory`, `to_bool` and `to_uuid` from
      `ansible.plugins.filter.core`, and `search` from `ansible.plugins.test.core`.
      `to_json` is the local `lib.k8s_yaml:to_json_stub`. R4 PR 10 reimplements these in
      `renderer_lib/filters.py`, and R4's parity tests import ansible-core as the oracle
      "until H7 removes it".
    - Five filter plugins import `ansible.errors`: `toposort.py`, `authelia_access.py`,
      `claude_agents.py`, `k8s_retire.py` and `k8s_autodeploy.py`. R4 PR 10 converts the
      first two, R4 deletes `k8s_retire.py`, and D6 deletes `k8s_autodeploy.py`.
      `claude_agents.py` serves the `claude_code` role and its tests.
    - `pyproject.toml` pins `ansible-core==2.21.5`. `ansible/requirements.yml` pins four
      collections: `ansible.posix`, `community.general`, `community.sops` and
      `community.docker`.
    - The lint is the prek hook `ansible-lint` in `prek.toml`, held by
      `scripts/tests/test_ansible_lint_hook.py`. `.github/workflows/ci.yml` has no job by
      that name.
- **Code outside the setup tree reads files inside it.** Outside `docs/`, Markdown and
  tests, 103 tracked files name a `roles/setup/` path, most of them in comments. The
  load-bearing reads are:
    - `ansible/filter_plugins/longhorn_groups.py` puts `roles/setup/common/files` and
      `roles/setup/k3s/files` on `sys.path` and imports `longhorn_lib.longhorn_backups`,
      which imports `host_lib`. `register_ansible_filters` imports `longhorn_groups`, so the
      cluster renderer depends on both directories.
    - `ansible/roles/k8s/autofix-bridge/defaults/main.yml` stages
      `roles/setup/common/files/kuma_push.py`, and
      `ansible/roles/k8s/uptime-kuma/templates/status-page-sync-configmap.yaml.j2` reads it
      with `lookup('file', ...)`. `configarr` and `janitorr` stage `host_lib.py` and
      `kuma_push.py` until H2.6 removes those imports.
    - Inside the tree, `ansible/roles/setup/common/tasks/install_host_lib.yml` copies both
      files with `src: "{{ playbook_dir }}/roles/setup/common/files/{{ item }}"`. Its
      header says "Eight callers across seven roles": `fake_remux`, `gitops_deploy`
      (`tasks/code.yml`, "Install the shared host_lib helper beside the deployer"), `k3s`
      twice in `health-crons.yml`, `renovate_agent`, `renovate_notify`, `configarr` and
      `janitorr`. Each caller's stamp pair names the same `src`, and
      `ansible/tests/setup/test_host_lib_sibling_copies.py` hardcodes
      `HOST_LIB_SRC = "ansible/roles/setup/common/files/host_lib.py"`.
      `deploy_cross_role.SETUP_FILES_SHIPPED_BY_OTHER_ROLES` keys its routing on the same two
      paths.
    - The deployer runs from `/opt/gitops-deploy/`, which only the copy `loop:` in
      `gitops_deploy/tasks/code.yml` fills. `ansible/tests/deploy/test_gitops_deploy_ship_list.py`
      says a module "forgotten in the copy loop passes CI and kills the deployer at import on
      its next tick".
    - `initial_setup/files/kuma-push-lib.sh` and `setup-drift-lib.sh` have 15 references
      outside the setup tree: 14 in tests and test helpers under `ansible/tests/` and
      `scripts/deploy_tools/tests/`, and one in an eval record. No running code outside the
      tree sources either library. The tests follow the crons that source them.
    - Scripts import deployer modules or `host_lib`: `scripts/deploy_tools/land_lib/*`,
      `scripts/deploy_tools/await_ci.py`, `scripts/deploy_tools/gitops_state.py`,
      `scripts/deploy_tools/runbook_gates_lib/{k3s_upgrade,longhorn_dr,longhorn_upgrade}.py`,
      `scripts/backup/longhorn_reap_lib/*`, `scripts/lib/deployer_park.py`,
      `scripts/diagnostics/probe_lib/{gitops_view,landing_blockers,longhorn_budget,longhorn_cluster}.py`,
      `scripts/docs/reference/state.py` and `scripts/secrets_mgmt/{consumers,rotation_tools}.py`.
    - H3's builder authenticates through `host_lib.py:github_token` and grades CI as
      `deploy_git.py:ci_verdict` does. Root `CLAUDE.md` cites
      `deploy_git.py:_CI_NO_VERDICT_CONCLUSIONS`.
    - `pyproject.toml`'s pytest `pythonpath` lists
      `ansible/roles/setup/{common,gitops_deploy,deploy_ui}/files`. Its ty `extra-paths` also lists the `files/` of `initial_setup`, `fake_remux`,
      `render_records`, `k3s`, `renovate_notify` and `renovate_agent`.
      `ansible/tests/repo/test_ty_config_covers_the_repo.py` holds the two lists together.
- **The deployer outlives D6.** D6's *Goal* says "The deployer itself stays, because it
  still applies the host plane until H7". D6's table gives H7 "the deployer, setup narrowing,
  and `land.sh`'s setup and Pi arms", and D6's exit 2 is a setup-plane change applied by the
  tick.
    - The contract in `ansible/roles/setup/gitops_deploy/CLAUDE.md` says "Every 10 minutes
      this role may fast-forward the primary checkout". Its fast-forward runs before its
      apply.
    - The role installs `gitops-deploy.service` and `.timer` under `/etc/systemd/system/`,
      its code under `/opt/gitops-deploy/`, `/etc/gitops-deploy/config.env` and
      `/etc/polkit-1/rules.d/50-gitops-deploy.rules`. Its glance block also lists the timers
      `kuma-check-github-ruleset-drift` and `kuma-check-github-interaction-limit`.
    - Setup narrowing is `scripts/deploy_tools/narrow_setup.py`, `setup_routing.py`,
      `setup_gates.py`, `setup_role_chains.py` and `narrow_lib/{setup_index,setup_playbook,task_walk}.py`.
    - `.claude/hooks/session-health.py` reads the deployer's `manual_plane`, `k8s_deferred`
      and parked state, with tests under `.claude/hooks/tests/`.
    - **land.sh runs the tick through a subprocess.** `land_lib/pipeline.py:_STEPS` runs
      `_step_tick` on every landing that `_step_resolve` does not finish, and every
      landing whose `tick_is_the_apply` holds or that has no tag awaits it there. It calls
      `land_lib/tick.py:run_tick`, which runs `gitops_tick.sh` through
      `land_lib/tools.py:run_tick`. `deploy.py:deploy_phase` calls `run_tick` again on its
      stale retry and `kick_tick` after a deploy, and `health_verdict.py:health` calls
      `rearm_tick`. With no `gitops-deploy.service` installed, `gitops_tick.sh` calls
      `tick_verdict 2 not-installed`, and `run_tick` ends any code other than `TICK_OK`,
      `TICK_STILL_RUNNING` and lock contention as `Verdict.DEPLOY_FAILED`. An import census
      cannot see this dependency.
    - **Code outside the role reads the deployer's paths.**
      `ansible/roles/k8s/monitor-bridge/templates/deployment.yaml.j2` mounts
      `/var/lib/gitops-deploy` as the hostPath volume `monitor-bridge-gitops-state`, with
      `type: Directory`, and `bridge/config_service.py` reads it as `GITOPS_STATE_DIR` for
      `checks/gitops.py:check_gitops_alive` and `check_gitops_status`. The same pod also
      mounts the etcd drill's stamp directory from daniel-box. M6 keeps both gitops checks.
      `scripts/dev/fanout_lib/brief.py:APPLY_OWED` tells an agent to `cat` `hold_sha` and
      `owed.jsonl`. `renovate_agent/tasks/main.yml` and `claude_code/tasks/agent_access.yml`
      grant access to `/var/lib/gitops-deploy`. `land_renovate_pr.py:DEPLOYER_CONFIG` reads
      `/etc/gitops-deploy/config.env`, which D6.13 repoints at
      `k8s_autodeploy_counts.py --denylist`.
    - `git grep -n -i checkout-sync` over the slice plans matches only this file, so no other
      slice builds the timer that replaces the fast-forward.
- **The inventory is read beyond Ansible.** `scripts/lib/ansible_inventory.py:inventory_hosts`
  reads `ansible/inventory/hosts.ini`, `scripts/lib/render_context.py` builds every render's
  variable layers from `group_vars` and `host_vars`, and G2's `retired-host` rule reads
  `hosts.ini`. `ansible/inventory/host_vars/daniel-pi.yml` sets `ansible_host: "{{ server_ip }}"`.
  `ansible/roles/k8s/pi-peer-backup/templates/cronjob.yaml.j2` renders
  `hostvars['daniel-pi'].ansible_user` and `.ansible_host` into the rsync target.
  `scripts/docs/reference/hosts.py` and `.claude/hooks/block-footguns.py` name
  `ansible_connection`.
- **pi-peer-backup writes the Pi.** `ansible/roles/k8s/pi-peer-backup/tasks/main.yml`
  installs the forced-command wrapper and authorizes the pull key with `delegate_to:
  daniel-pi`. The key it authorizes is the tracked public half,
  `ansible/roles/k8s/pi-peer-backup/files/pi-peer-backup.pub`, so it is not a secret.
- **D5 adds a setup role that H7 must empty.** `git ls-files
  ansible/roles/setup/k8s_host_files | wc -l` prints 0, because D5 creates the role. D5's
  *Host-side rows* moves into it the host-side tasks of the k8s roles it flips. The role's
  playbook entry runs on daniel-box, and the tick applies it. D5 says "No row stays there
  past H7". The rows are:
    - docs's "Ensure the built docs site directory exists" on daniel-box, which D5 gives to
      H7;
    - pi-peer-backup's two writes on daniel-pi, which keep `delegate_to: daniel-pi`. H5's
      *Open questions* 3 leaves both to H7;
    - crowdsec-verify's probe timer on daniel-pi, which H5.14 moves into the Pi package. If
      H5.8 landed before D5's crowdsec conversion, D5 adds the timer to the Pi package
      directly. Either way the Alloy metrics stage sits in H5's `config.alloy.j2`. H7 depends
      on H5 and, through R4 and D6, on D5, so neither is H7's work;
    - traefik's absent arm for the `cloudflare-ip-drift` cron file and script, and
      registry's three GC absent arms, which D5 says H7 deletes with the role. H2's exit
      observation 3 also allows a retired-cron absent arm to sit in a role's task file there.
- **The Pi's service list after H5.** H1.9 removes `containers_list` from daniel-box's and
  daniel-server's `host_vars` only. H5 empties the Pi's `containers_list`, adds
  `compose_services` beside it, and points every reader at both keys (H5, *Every reader
  reads both keys*). At `70591c307` the Pi's readers are the three templates that `git grep
  -n -F "hostvars['daniel-pi'].containers_list"` lists in `ansible/roles/k8s/`
  (`artifacts`, `monitor-bridge` and `observability`), `render_context.py:_HOSTVARS_KEYS`
  and `health_docker.py:declared_on_pi`. H5's *What stays with Ansible until H7* hands the
  empty key to H7 and keeps `compose_services` in the inventory.
- **Seven `ansible.posix.sysctl` tasks also edit a file in place.** `git grep -c
  'ansible.posix.sysctl:'` over the setup tasks prints `initial_setup/tasks/network.yml` 2,
  `system-tuning.yml` 1, `k3s/tasks/agent.yml` 1, `k3s/tasks/node.yml` 1 and
  `optimize_pi/tasks/main.yml` 2. `git grep -n sysctl_file` over `ansible/roles/setup/`
  prints nothing, so each task writes the module's default file, `/etc/sysctl.conf`, as a
  `<key>=<value>` line. The tasks set 23 keys, 19 of them in `network.yml`'s hardening loop,
  `net.core.bpf_jit_harden` among them. `dpkg -S /etc/sysctl.conf` prints `procps:
  /etc/sysctl.conf`, and procps' `${Conffiles}` lists the file. `man 8 sysctl`'s *SYSTEM
  FILE PRECEDENCE* reads `/etc/sysctl.conf` after every `sysctl.d` directory, so a key there
  overrides the same key in any drop-in. The count of 29 below excludes these seven.
- **In-place edits.** 29 tasks edit a file in place with `lineinfile`, `replace` or
  `blockinfile` (`git grep -c` over `ansible/roles/setup/*/tasks/*.yml`): `optimize_pi` 12,
  `initial_setup` 12, `k3s` 3, `docker_install` 1 and `claude_code` 1. *Design* lists every
  path. `dpkg -S` on daniel-box, a read-only local command, prints these owners:
    - `login: /etc/login.defs`, `ufw: /etc/ufw/sysctl.conf`,
      `libpam-modules:amd64: /etc/security/limits.conf`, `sysstat: /etc/sysstat/sysstat`,
      `acct: /etc/default/acct`, `auditd: /etc/audit/auditd.conf`,
      `zsh-common: /etc/zsh/zshenv` and `multipath-tools: /etc/multipath.conf`;
    - no owner for `/etc/ssh/sshd_config`, `/etc/postfix/main.cf`, `/etc/default/rkhunter`,
      `/etc/default/sysstat` and `/etc/hosts`. Maintainer scripts generate those.

  `dpkg-query -W -f='${Conffiles}'` lists `login.defs`, `ufw/sysctl.conf`, `limits.conf`,
  `sysstat/sysstat`, `default/acct`, `auditd.conf` and `zsh/zshenv` as conffiles. The
  Pi-only paths cannot be measured from here. Every owned path measured here is therefore a
  conffile, and `lineinfile` or `blockinfile` already edits each one in place at
  `70591c307`. Two of the 29 edits are `blockinfile` blocks, not lines:
  `k3s/tasks/kubeconfig.yml` on `/etc/zsh/zshenv` and `docker_install/tasks/install.yml` on
  `/etc/containerd/config.toml`. One is a `replace`: `optimize_pi` on
  `/usr/local/bin/log2ram`.
- **No diversion exists, and H3 admits none.** H3's *Package rules* says "`preinst` refuses
  a hostname mismatch and does nothing else" and that its maintainer-script whitelist
  "admits no `dpkg-divert` line". H4-R1-04 dropped H4's two diversions for a
  `NUT_CONFPATH` drop-in, because diverting a conffile broke upgrades and rollback. H4's
  *Open questions* says "H7's *In-place edits* cites a divert pattern from H4, which this
  slice no longer has, so H7 defines that lifecycle itself."
- **ufw.** 15 `community.general.ufw` tasks across five roles write rules.
    - `initial_setup/tasks/network.yml` sets `deny incoming` and `allow outgoing`, leaves the
      routed default alone, and adds the uncommented SSH rules. Its task "Configure UFW
      firewall - allow SSH from the LAN without rate-limiting" carries `DECIDED the LAN is
      exempt from SSH rate-limiting, and this rule must sort ABOVE the `limit` rule below`.
    - `k3s/tasks/node.yml` and `k3s/tasks/agent.yml` add uncommented inter-node and
      forwarder-scrape rules.
    - Three roles add commented rules: `deploy_ui/tasks/firewall.yml` (`deploy-ui from the
      k3s cluster`), `docker_install/tasks/install.yml` (`docker_install: dockerd metrics`
      and `docker_install: containerd metrics`) and `optimize_pi/tasks/main.yml`
      (`optimize_pi: node_exporter`).
- **Activation.** `optimize_pi/CLAUDE.md` says "A zram config change reboots the Pi" and
  that GPU, watchdog and Log2Ram changes `notify: Reboot Pi`. `initial_setup` notifies
  `Restart SSH`, `Restart Postfix` and `Reload Postfix`, among others. H3's reconcile step 3
  restarts a service only when its unit file or drop-in hash changes. `Weekly system
  restart` in `initial_setup/tasks/crons.yml` reboots every host on Sunday at 07:30.
- **The Docker engine is held.** `docs/docker-engine-pins-and-runtime.md` records the
  2026-09-18 dist-upgrade that replaced the engine "with every container running (#1961)".
  autoheal's shim died and ssh refused connections for about 20 minutes.
  `ansible/roles/setup/docker_install/CLAUDE.md` says the hold prevents "an engine swapped
  under running containers", that `tasks/engine-upgrade.yml` "stops every Compose project
  before apt runs and recreates each one afterwards", and that a `daemon.json` edit restarts
  dockerd and leaves docker-proxy on a stale socket inode. H5.7 changes `engine-upgrade.yml`
  to stop and start `homelab-compose.service`.
- **The Docker networks are external.** H5's Baseline says "The `networks.yml.j2` macro
  declares each network `external: true`", and that `docker_install` creates `proxy` and
  `lifecycle`, the two the Pi's project attaches to. The task `Create Docker networks` in
  `docker_install/tasks/install.yml` loops over eight names, not two, and sets no `labels:`.
  Compose labels a network it creates itself, so the live networks lack the labels a
  project-managed network carries.
- **The drill's inputs.** `ansible/roles/setup/hypervisor/templates/etcd-restore-drill-vm.sh.j2`
  sources `K3S_TOKEN` from `AGENT_ENV=/etc/systemd/system/k3s-agent.service.env`. H6 leaves
  that file in place. Its *Design* says "The agent keeps its token where it is. The agent's
  drop-in clears no `EnvironmentFile=`. The installer's env file still supplies `K3S_URL`
  and `K3S_TOKEN`, and the etcd drill still reads it." The drill reads
  `hypervisor_etcd_drill_s3_env` (`/etc/homelab/etcd-drill-s3.env`). Its guest attaches to
  the nwfilter `staging-egress-fence`, which drops `lan_subnet`, `k3s_pod_cidr` and
  `k3s_service_cidr` and leaves the internet reachable. `hypervisor/CLAUDE.md` says "the
  network template pins its UUID too", that `fence_check` runs "before the guest is handed
  the cluster token or the R2 credentials", and that the guest is "transient, never
  defined" and touches "never the live cluster".
- **The agent's token is the server token.** H6's Baseline says k3s writes one value to
  `token` and `node-token` when no `--agent-token` is set, and `k3s_server_args` sets none.
  `docs/break-glass.md` item 2 keeps that token out of band. H6 *Open questions* 4 moves no
  token and hands H7 the choice of an agent-scoped token: "Whether an already-joined agent
  accepts the switch is undocumented."
- **The server's k3s config names its LAN address.** `k3s_server_args` in
  `ansible/roles/setup/k3s/defaults/main.yml` carries `--node-ip {{ server_ip }}
  --advertise-address {{ server_ip }}`, and `static-address.yml` pins `server_ip` in
  `/etc/netplan/90-homelab-static-address.yaml`. H2 turns on `etcd-s3` with
  `etcd-s3-config-secret: k3s-etcd-snapshot-s3-config`, a Secret Flux applies from
  `cluster-base`, so the credential lives in the cluster, not in the host config.
- **R4 owns the Flux bootstrap.** R4's *Bootstrapping Flux without Ansible* has four steps:
  apply the PriorityClasses from the render of `cluster-base`, because D1 sets
  `priorityClassName: homelab-critical` on Flux's controllers; apply the Flux components
  and sync objects; create `sops-age`; then apply the `OCIRepository` and the root
  Kustomization. R4 PR 7b writes it into `docs/flux-deploy-plane.md` as *Bootstrapping Flux
  on a rebuilt cluster* and proves it on kind with `flux-bootstrap-drill.yml`, a fixture
  bundle and a throwaway key.
- **Users.** `ansible/roles/setup/common/tasks/agent_user.yml` creates each Claude agent's
  Unix user and hands it its home, both as root. chezmoi applies as the operator, without
  root.
- **The secret rotation cron runs on daniel-box.** `Weekly secret rotation (auto tier)` in
  `initial_setup/tasks/crons.yml` is gated `when: has_gitops` and runs as `sys_user`, so it
  reads the operator's `~/.config/sops/age/keys.txt` on daniel-box. H4's *Risks* says H4.2
  copied daniel-server's key to `/etc/homelab/age.key` and "H7 removes the user-home copy".
- **Generators read setup tasks.** `scripts/docs/reference/crons.py`'s `build_rows` globs
  `roles.rglob("tasks/*.yml")`. `git grep -l 'roles/setup'` also matches
  `scripts/docs/gen_role_glance.py`, `scripts/docs/fragments_lib/fragments_hosts.py`,
  `scripts/docs/reference/state.py`, `scripts/docs/catalog_lib/glance_facts.py` and
  `scripts/secrets_mgmt/secret_rotation.py`.
- **Retired crons live in absent arms, not in a list.** `git grep -n retired_host_crons`
  prints nothing. The only list is `docker_install/defaults/main.yml:docker_install_stale_crons`,
  which the task `Remove crons belonging to retired Compose services` in
  `docker_install/tasks/teardown.yml` loops over. Of the 37 `ansible.builtin.cron` tasks, six
  carry a literal `state: absent`. Three retire a cron for good: that loop,
  `Retire the TLS cert-expiry watch` in `initial_setup/tasks/crons.yml`, and
  `Remove the cron the kuma-check timer replaces` in `common/tasks/kuma_check_timer.yml`.
  The other three are the absent half of a cron still live on another host, such as
  `Remove the firmware cron on hosts without fwupd`.
- **`docs/facts.lock` keys rows on setup-role docs.** `grep -c` for
  `ansible/roles/setup/*/CLAUDE.md#` in `docs/facts.lock` prints 57, among them 7 for
  `claude_code`, 7 for `initial_setup` and 5 for `gitops_deploy`. Root `CLAUDE.md` says a
  vanished heading "reads as `section-gone` instead: verify the new key, then
  `fact_status.py forget '<old key>'`".
- **Contract checks derive from setup tasks.**
  `ansible/tests/setup/test_setup_cron_roles_have_a_contract.py` derives the cron-installing set from `tasks/`, and `ansible/tests/repo/test_role_claude_md.py`
  requires a `CLAUDE.md` per setup role. Both pass vacuously once the roles are gone.
  `ansible/tests/setup/` holds 114 tracked files.
- **Docs that name the playbooks.** `git grep -l 'ansible-playbook' -- ':!docs/archive'`
  prints 144 files. Outside `docs/archive/`, `docs/target-architecture/` and
  `docs/reference/`, 22 pages under `docs/` name a bring-up playbook or `ansible-playbook`,
  among them `docs/longhorn-disaster-recovery.md` (step 2, "Cluster bring-up") and
  `docs/k3s-etcd-restore.md`. Root `CLAUDE.md`, four skills (`add-secret`, `deploy`,
  `ha-edit-automation`, `new-k8s-service`) and the `CLAUDE.md` files of `k8s/deploy-ui`,
  `k8s/headlamp` and `k8s/nut` name them too. `docs/failure-classes.md` cites no test under
  `ansible/tests/setup/`.
- **The NUT drill powers off both nodes.** `ansible/roles/k8s/nut/CLAUDE.md` says the
  manual drill "actually powers BOTH nodes off now that daniel-box is armed".

## Design

### What goes where

| Owner at `70591c307` | daniel-pi | daniel-server | daniel-box | Not a package |
|---|---|---|---|---|
| `initial_setup` | apt and unattended-upgrades config, sshd drop-ins, sysctl keys in `/etc/sysctl.conf` through the line arm, fail2ban, journald caps, rkhunter, AIDE, postfix, accounting, the weekly reboot and maintenance timers, ufw | same | same, plus the checkout timers (docs refresh, eval sweep, infra map, secret rotation, git repair) and two kuma-check timers | `Clear ansible log file` and `kuma-check-setup-drift` are deleted |
| `optimize_pi` | zram, Log2Ram, watchdog, earlyoom, apt-timer drop-ins, node exporter unit, resolver and hosts files | — | — | the debloat `apt purge` becomes a one-time bootstrap step |
| `docker_install` | Docker source in the `apt-docker` component; `homelab-docker-engine.service` with the pins; `daemon.json`; Go-runtime drop-ins; the Compose unit creates the external `proxy` and `lifecycle` networks | — | — | the networks stay `external: true` (H7.5) |
| `sops_setup` | `homelab-tools` (sops), `Depends: age` | same | same | key generation is bootstrap step 3; collections are deleted |
| `hypervisor` | — | libvirt `Depends`, network and nwfilter XML, drill timer, libvirt arm | — | — |
| `k3s` (host halves) | — | `agent.yml`, `agent_verify.yml`, `static-address.yml`, the node files and the agent ufw rules | node files, netplan, `coredns-host.service`, disk health, etcd list drill, kubeconfig oneshot, the join-firewall rules | install moves to bootstrap step 4 |
| `nut_host` (box half) | — | — | `nut-secondary` with the pinned ClusterIP | — |
| `common` | the alert unit, resolver template | the alert unit | the alert unit, resolver template | `host_lib.py` and `kuma_push.py` move to `hosts/lib/`; `stamp_*` and `release_bin` are deleted |
| `claude_code`, `renovate_agent`, `renovate_notify`, `fake_remux`, `deploy_ui`, `homelab_builder` | — | `claude_code` units and the agent users | all of them | per-user tooling goes to chezmoi |
| `config_files` | `/etc/profile.d/homelab.sh` | — | — | the Pi's tracked `~/.bashrc` reverts to Ubuntu's |
| `github_cli`, `chezmoi_setup` | — | — | — | chezmoi, which bootstrap step 5 installs |
| `gitops_deploy` | — | — | the two GitHub check timers; `homelab-checkout-sync` replaces the fast-forward | H7.19a retires the deployer and empties the role; H7.20a deletes the role |
| `render_records` | — | — | — | R4 deletes it |
| `k8s_host_files` (added by D5) | pi-peer-backup's forced-command wrapper, and its authorized key through the line arm (H7.4) | — | `tmpfiles.d` lines for the docs site directory (H7.10a), media-volume's three directories under `/srv/media` (H7.9b), and artifacts' local, per-peer and star list directories (H7.12c) | the absent arms become legacy entries, H7.18c empties the role and H7.20a deletes it; the crowdsec probe timer and the Alloy stage are H5's or D5's, not H7's |

### The component checklist and the setup census

The table above is a summary, and a summary misses items. H7.2f adds a census that derives
the checklist from the tree, so no item is listed by hand. For every setup role it reads
three sources:

- the crons and timers in the role's generated `## At a glance` block;
- every task under its `tasks/`, whatever its module, except a named set of control and
  read-only modules: `stat`, `assert`, `debug`, `set_fact`, `slurp`, `find`, `getent`,
  `fail`, `meta`, `wait_for`, `service_facts`, `docker_host_info`, and `import_tasks` and
  `include_tasks`, which the census follows instead. A `command` or `shell` task with
  `changed_when: false` and no `creates:` or `removes:` counts as a read. Every other
  `command` and `shell` task needs an assignment, like any other item;
- each `import_tasks` of a `common/tasks/` file.

The census maps each module to the place its item must resolve:

- `cron`, `template`, `copy`, `file`, `get_url` and `unarchive` to a package path or a
  legacy entry, by `dest:` or `path:`;
- `systemd`, `systemd_service` and `service` to a component unit or a legacy unit;
- `apt` to a component `Depends`, or, for `state: absent`, to bootstrap's debloat step or
  an allowlist entry; `dpkg_selections` to H7.5's engine hold;
- `user` and `group` to the users arm, `authorized_key` and the in-place edits to the line
  arm or a drop-in, by `path:`, and `ansible.posix.sysctl` to the line arm on
  `/etc/sysctl.conf`, by key;
- `community.general.ufw` to the ufw arm, `modprobe` to a `modules-load.d` file, and
  `mount`, `acl`, `snap`, `git_config`, `docker_network` and `docker_container` to a
  package path, chezmoi, bootstrap or the allowlist, each by the census's per-module key.

Two binaries in `/usr/local/bin` sit ahead of a packaged copy on `PATH`, so each needs a
legacy entry, not just a deleted task. `sops_setup/tasks/main.yml` installs
`/usr/local/bin/sops` through `get_url`, and H7.1 lists that path as a legacy entry in all
three host packages. `initial_setup/tasks/host-basics.yml` unpacks vale into
`/usr/local/bin` through `unarchive`; the PR that moves it ships vale in `homelab-tools`
and lists `/usr/local/bin/vale` as a legacy entry, or the allowlist names it with a reason.

A task counts against its `delegate_to:` host when it names one, and otherwise against the
hosts its play targets. D5's `k8s_host_files` runs on daniel-box, but its pi-peer-backup
tasks count against daniel-pi.

Each item must resolve to a package path, a legacy entry, a ufw or libvirt declaration, or
an entry in the census's short allowlist of deleted items. The allowlist names each item
with a reason, for example `Clear ansible log file`. The census runs in report mode from
H7.2f, which prints the items left per host. H7.19c turns it strict, so it fails while any
setup role still has a task that applies to a host. Its named members are
`Weekly apt autoremove`, `claude-land@.service.j2`, `k3s/tasks/static-address.yml`,
`/usr/local/bin/sops`, the `systemd_service` task `Enable and start the host upsmon
service`, the `ansible.posix.sysctl` setting `net.core.bpf_jit_harden` and the ufw task
`Enable UFW firewall`. The emptied role's `meta: end_role` stub is a control module, so
strict mode never flags it.
Until H7.4 moves it, the report must also list pi-peer-backup's forced-command wrapper in
`k8s_host_files` under daniel-pi. That report is also the list of record for the rows
`k8s_host_files` holds when H7 starts.

### Roles are emptied before H7.20a, never deleted

No PR before H7.20a deletes a role directory or edits `initial_setup.yml`,
`k3s-bringup.yml` or `bootstrap.yml`. Each of those three paths is in
`_BROAD_MANUAL_PREFIXES`, so a range that touches one parks the tick with no fast-forward
and holds every other session's landing (Baseline). A play that names a missing role fails
to parse, so deleting a role would force that edit.

- **Emptying.** The PR that moves a role's last component reduces its `tasks/main.yml` to one
  `ansible.builtin.meta: end_role` task with no `when:`, under a `# DECIDED` marker that
  names H7.20a and the park. H4.3b2's reader in `scripts/deploy_tools/setup_lib/setup_gates.py`
  evaluates a leading `end_role` guard per host, and an unconditional one applies to no host,
  so H3.2's two-writer census and H7.2f's setup census both read the role as empty. The
  playbook line stays, and the tick applies the emptied role on daniel-box as a no-op.
- **What stays in the emptied role.** The stub `tasks/main.yml` and a stub `CLAUDE.md`,
  because `ansible/tests/repo/test_role_claude_md.py` requires one per setup role until
  H7.20c retires that half. The stub points at the component. Every template, file and
  default that a component renders is `git mv`'d into the component's directory in the PR
  that moves it, as H3.8b does, so H7.20a deletes nothing a render reads.
- **Deletion.** H7.20a deletes every role directory and the three playbooks together. It
  merges only after daniel-box lists no `gitops-deploy` unit (exit 5), so no tick sees it.

### Moving one host at a time

A subject that applies to every host moves as one PR per host (*Canary order*). The first
two host PRs must stop Ansible writing the subject on their host while the role still writes
it on the others. Each role whose subject moves per host gains a default,
`<role>_packaged_hosts`, a map from subject name to the hosts whose package owns it. The map
lives in the role's own `defaults/main.yml`, never in `host_vars`, because an `inventory/`
edit is a deploy-plane change. Each task of the subject carries
`when: inventory_hostname not in <role>_packaged_hosts['<subject>']`. When the subject's
tasks already sit in their own task file, as `initial_setup`'s mostly do, the guard goes on
that file's `import_tasks` instead, which applies it to every imported task.

- **The censuses read the map.** H3.2's two-writer census, H7.2b's ufw census and H7.2d's
  line-arm census evaluate each task's `when:` per host with `setup_gates.py:eval_when_strict`,
  through H4.3b2's helper, extended to a task-level or import-level guard. A host listed in
  the map counts as not written by that task.
- **The evaluator must see role defaults.** `eval_when_strict` merges only
  `group_vars/all.yml` and the host's `host_vars` file (`setup_gates.py:_host_vars`), and
  raises `GateUnreadable` on a name it cannot resolve. H7.2b's extension therefore also loads
  the guarded role's `defaults/main.yml`, below `group_vars`, where Ansible's precedence
  puts role defaults. Its red fixture defines the map only in role defaults: without the
  load the census raises, and with it the guard reads per host.
- **The setup census reads it too.** A host in the map whose package declares nothing for
  the subject is reported as missing, so a guard can never hide an unmoved item.
- **A whole role moving per host** uses H4's leading `end_role` guard naming the moved hosts
  instead, such as `claude_code` in H7.12c.
- **The last host PR** deletes the subject's tasks and its map entry.
- **A revert of one host PR** removes that host from the map and the component from that
  host's package. The role writes the subject on that host again after the hand re-apply
  that *Rollback* orders, and the other hosts are untouched.

### How moved config becomes live

dpkg installs a file, and nothing applies it. H3's reconcile restarts a service only when
its unit file or drop-in changes. Each file in a `component.yml` therefore declares an
`activate:` action, and reconcile runs it when that file's hash changes:

- `none`, for a file the program reads on each use, such as `limits.d`;
- `try-restart:<unit>` or `reload:<unit>`;
- `sysctl`, which runs `sysctl --system`;
- `modules-load`, which restarts `systemd-modules-load.service`;
- `udev`, which is H4's arm;
- `reboot`, which marks the host reboot-pending;
- `manual:<unit>`, which marks the unit pending and changes nothing live.

Some actions validate before they run. `reload:ssh.service` runs `sshd -t` first,
`try-restart:fail2ban.service` runs `fail2ban-client -t`, and the journald and sysctl
actions parse their files. A validation failure leaves the running service untouched, and
reconcile exits non-zero. It also writes `/run/homelab/validate-failed`, which blocks the
reboot below. A validation failure is a fault, so H3's updater holds the host (*Which
outcomes hold the host*).

Reboot-pending and manual-pending states go to the textfile metrics
`homelab_host_reboot_pending{host}` and `homelab_host_activation_pending{host,unit}`. The
weekly reboot moves from a cron to `homelab-reboot.timer` in H7.7. That timer keeps the
Sunday 07:30 slot and the Healthchecks.io ping, and refuses while
`/run/homelab/validate-failed` exists. A reboot-class change therefore goes live at the
next weekly reboot, or sooner when the operator reboots. *Complexity delta* compares this
with a separate windowed reboot unit.

Verification reads live state, never only dpkg: `sshd -T`, `sysctl -n`, `zramctl`,
`ufw status numbered`, `journalctl --header` and `fail2ban-client status`.

### In-place edits: a drop-in, the line arm or a homelab copy

Shipping a path another package owns makes dpkg refuse the unpack with "trying to
overwrite", which stops every later update, so no package ships an owned path. Each of the
29 edits gets one of four mechanisms, and none of them is a divert.

- **A drop-in directory that the program reads (6 edits).** Each directory is checked on
  Ubuntu 24.04 in the CI container before its PR merges.
    - `/etc/ssh/sshd_config`, 2 edits. The settings go to
      `/etc/ssh/sshd_config.d/10-homelab.conf`. sshd keeps the first value it reads, and
      `10-` sorts before cloud-init's `50-cloud-init.conf`. The `Match User` block goes to its
      own file. The CI test runs `sshd -T -C user=<sys_user>,host=x,addr=10.0.0.1` and the
      same for another user, and asserts the forwarding settings differ only for
      `sys_user`.
    - `/etc/security/limits.conf`, 1 edit, goes to `/etc/security/limits.d/50-homelab.conf`.
    - `/etc/multipath.conf`, 2 edits (`k3s/tasks/agent.yml` and `longhorn.yml`), goes to
      `/etc/multipath/conf.d/50-homelab-longhorn.conf`, with `reload:multipathd.service`.
      The Ansible-era block stays in the owned file as a harmless duplicate, and the PR body
      names it for removal by hand.
    - `/etc/default/earlyoom`, 1 edit on the Pi, becomes a unit drop-in for
      `earlyoom.service` that clears `EnvironmentFile=` and sets `EARLYOOM_ARGS`.
- **The line arm, on a conffile (8 measured edits, and up to 5 Pi edits).** dpkg never
  replaces a conffile that was changed locally without asking. On an upgrade it keeps the
  local file when the package's own copy is unchanged, and otherwise a non-interactive run
  keeps the local file or holds the package back. That is the state Ansible's in-place edit
  leaves at `70591c307`, so the arm editing the same line keeps today's behaviour. A divert
  of a conffile is what H4-R1-04 rejected.
    - Measured conffiles: `/etc/login.defs` (3 lines), `/etc/ufw/sysctl.conf` (1 line),
      `/etc/zsh/zshenv` (1 block), `/etc/sysstat/sysstat` (1), `/etc/default/acct` (1) and
      `/etc/audit/auditd.conf` (1). The last three are `optimize_pi` edits, measured on
      daniel-box, so H7.3 confirms them on the Pi.
    - `/etc/sysctl.conf`, procps' conffile, for the seven `ansible.posix.sysctl` tasks
      (Baseline), which sit outside the 29. Every key those tasks set stays in
      `/etc/sysctl.conf`, one line-arm declaration per key, with `activate: sysctl`. The
      arm's anchor for a key is `^\s*<key>\s*=`, with the key's dots escaped, which matches
      the `<key>=<value>` line the module wrote, so the first run adopts it. No homelab
      `sysctl.d` file may declare such a key, because `/etc/sysctl.conf` is read last and
      would override it. A census in `hosts/lib/tests/` holds that rule. Moving a key to a
      drop-in would need a line-arm absent form, a deleting arm with its own floor, and this
      plan adds none. The keys move with their subjects: zram's `vm.swappiness` and
      `vm.page-cluster` in H7.3, the rest of `initial_setup`'s in H7.6g–i, and
      `fs.inotify.max_user_instances` in H7.9a and H7.9b.
    - Pi paths whose owner H7.3 and H7.5 measure first: `/etc/default/zramswap` (1),
      `/etc/watchdog.conf` (2), `/etc/log2ram.conf` (1) and `/etc/containerd/config.toml`
      (1 block). A conffile stays here, and an unowned file moves to the next arm in the
      same PR.
- **The line arm, on a file no package owns (8 edits).** Maintainer scripts regenerate
  these files, so the package must not ship them.
    - `/etc/postfix/main.cf` (3 edits) and `/etc/default/rkhunter` (1);
    - `/etc/default/sysstat` (1) and the Pi's `/etc/hosts` (1);
    - the Pi's firmware `config.txt` (2), with `activate: reboot`.
- **A homelab copy, for an owned file that is not a conffile (at most 1 edit).** This is
  `/usr/local/bin/log2ram`'s `--sparse` patch, whose `DECIDED` marker moves with it. If
  H7.3 finds the file unowned, it takes the line arm. If the log2ram package owns it, the Pi
  package ships the patched script as `/usr/lib/homelab/log2ram`, and a drop-in for
  `log2ram.service` resets `ExecStart=`, `ExecStop=` and `ExecReload=` to that path. The
  upstream script it was taken from is vendored beside it, and a test holds the difference
  between the two to the `--sparse` hunk. The PR body names the installed log2ram version.
- **chezmoi (1 edit).** `claude_code/tasks/agent_memory_seed.yml` edits a file in the agent's
  home, which the agent's own tooling owns after H7.12.

**If a divert survives the measurement.** H7.3 or H7.5 may find an owned path that is not a
conffile and that no unit drop-in can redirect. Then the PR before that move defines the
divert lifecycle, and the same PR amends H3's `preinst` rule and its whitelist lint in
`hosts/lib/tests/`:

- `preinst` runs `dpkg-divert --add --rename --package homelab-<host> --divert <path>.distrib
  <path>` only when `$1` is `install` or `upgrade`;
- `postrm` runs the matching `--remove --rename` only when `$1` is `remove`, `purge` or
  `abort-install`, never on `upgrade`;
- a version that stops shipping a diverted path removes that divert in the same version, in
  its `postinst`. A census in `hosts/lib/tests/` compares each package's divert list with the
  list at the PR's base, and fails when a path leaves without that removal. A plain
  `git revert` therefore cannot leave the owner's file renamed behind a live divert;
- the CI container test covers a first install, an upgrade from N to N+1, a version that
  retires the divert, and an upgrade of the owning package, each with a red fixture.

At `70591c307` no edit needs a divert, so H3's whitelist stays closed (*Open questions* 9).

One more line-arm edit comes from outside the 29: pi-peer-backup's authorized key, which D5
moves into `k8s_host_files`. The arm keeps it in the Pi user's `~/.ssh/authorized_keys`,
anchored on the key's comment, `pi-peer-backup-cronjob`, so a rotated key replaces the line
rather than adding a second one, as `ansible.posix.authorized_key` would.

The CI container test installs each owning package first, then the homelab package. Its red
fixture ships one owned path, and the test asserts the "trying to overwrite" failure. A
census in `hosts/lib/tests/` fails when a component ships a path that `dpkg -S` in the CI
container reports as owned by another package. The conffile arm has two upgrade cases. An
upgrade of the owning package whose conffile is unchanged keeps the arm's line. An upgrade
that ships a changed conffile, run non-interactively as unattended-upgrades runs it, either
keeps the local file or holds the package back. The test records which, and asserts the
line is present after the next reconcile. The red fixture points the arm at an owned path
that is not a conffile, and the arm must refuse it.

### Reconcilers for state dpkg does not hold

These are reconcile arms (H3), each small and each with tests in `hosts/lib/tests/`. Every
arm keys only on what it labelled or recorded, and every deleting arm has a floor.

- **ufw.** A component declares each rule with an `id`, its spec, a `position` and an
  optional `before:` relation. The arm writes it with `comment 'homelab:<id>'`, or
  `comment 'homelab:keep:<id>'` for a rule the arm must never delete. The SSH limit rule,
  the LAN SSH allow rule and the inter-node join rules are `keep`. Each run does these
  steps in order:
    1. It sets the default policies `deny incoming` and `allow outgoing`. It never touches
       the routed default, for the reason `initial_setup/tasks/network.yml` gives: Docker
       owns FORWARD on the Pi.
    2. It adds each missing declared rule. A `position: first` rule is inserted at position
       1, with the same empty-list fallback the Ansible task uses. Other rules are appended.
    3. It checks every `before:` relation in `ufw status numbered`. The LAN SSH allow rule
       declares `before: ssh-limit`, and the design cites the marker `DECIDED the LAN is
       exempt from SSH rate-limiting`. A violated relation fails the run and is never
       repaired by deleting a `keep` rule.
    4. Only when steps 1 to 3 succeeded, it deletes each Ansible-era legacy rule whose
       replacement it has confirmed present. A legacy rule is listed by its exact spec and
       its exact comment, which is empty for the `initial_setup` and `k3s` rules and is the
       role's own comment for `deploy_ui`, `docker_install` and `optimize_pi`.
    5. It deletes `homelab:` rules that are no longer declared, under a floor. It deletes
       nothing when the declared set is empty, never more than two rules in one run, and
       never a `homelab:keep:` rule. A rule the floor keeps is drift, not a failure: the
       arm counts it in `homelab_host_reconcile_drift{host,arm="ufw"}`, and the run
       succeeds. The floor guards a revert, and a revert must leave the host updating.
    6. It enables ufw, with `ufw --force enable`, only when `ufw status` reads `Status:
       inactive`, and only after steps 2 and 3 succeeded with every `keep` rule present: the
       SSH limit rule, the LAN SSH allow rule and the inter-node join rules. A remote host
       therefore never enables before SSH is allowed. It then reruns step 3's `before:`
       check on the active ruleset. A failed enable, or a violated relation after it, is a
       fault and holds the host. This step replaces `initial_setup/tasks/network.yml`'s
       `Enable UFW firewall` task, which the last host PR of H7.7g–i deletes. The live hosts
       are already active, so there the step does nothing. Stock Ubuntu ships ufw installed
       and inactive, so a rebuilt host is the case it exists for.

  Steps 2 to 5 read the rules with `ufw show added`, which lists the added rules
  whether or not ufw is active. `ufw status numbered` prints only `Status: inactive` on an
  inactive host, and a check over that empty list would pass for any order. H7.2b measures
  both outputs in the CI container, and the stub `ufw` replays them.

  Reconcile runs the line arm before the ufw arm. Enabling ufw applies
  `/etc/ufw/sysctl.conf`, so the line arm's `log_martians=1` edit is in place first, and
  Ubuntu's stock `log_martians=0` never goes live on a rebuilt host (#2977).

  H7.2b measures in the CI container what `ufw` does when a rule with an existing spec is
  added under a different comment: skip, update or duplicate. Step 4 follows the measured
  behaviour, and the test fixture records it. H7.2b also extends H3.2's two-writer census to
  ufw specs. For each host, no Ansible `community.general.ufw` task in a role that applies
  to that host may declare a spec that the host's package also declares.
- **libvirt.** A component declares networks and nwfilters by name, each with pinned XML and
  a pinned UUID. The arm defines or updates each declared object. It undefines only an
  object whose name is in its own `applied.json` record and is no longer declared, never one
  matched by prefix. The `staging` network and the `staging-egress-fence` nwfilter keep
  their names and UUIDs, so no rename happens and `net-define` cannot collide. libvirt
  refuses to undefine a filter a running guest references. The arm leaves that object
  recorded, counts it in `homelab_host_reconcile_drift{host,arm="libvirt"}`, and does not
  retry within the run. The drill's transient guest stays the drill's own business.
- **Users.** `/usr/lib/sysusers.d/homelab.conf` creates the users the units need through
  `systemd-sysusers`, each with a fixed uid. sysusers creates only missing users, so each
  uid must equal the live user's uid, which the PR that adds it records from `getent
  passwd`. The Claude agent users join this list in H7.12b, with their live uids, home
  directories and shells. The arm never deletes a user. A user that leaves the list is
  expired with `usermod --expiredate 1`, and removing its home is a documented operator
  step.
- **Lines and blocks in files dpkg does not overwrite.** The arm keeps one anchored line,
  the contract of `lineinfile` with a `regexp`, or one marker-delimited block, the contract
  of `blockinfile`. Its regexp and its block markers equal the Ansible task's, so its first
  run adopts the line or block Ansible wrote instead of adding a second one. Before it
  writes, it runs `dpkg -S` on the path. It accepts an unowned file, and an owned file only
  when the owner lists the path among its conffiles (`dpkg-query -W -f='${Conffiles}'`). It
  refuses any other owned file, so it can never fight dpkg. H7.2d extends H3.2's two-writer
  census to line-arm declarations: for each host, no Ansible in-place edit task that applies
  to that host may name a path that host's line arm declares. For `/etc/sysctl.conf` the
  census keys on the path and the sysctl key, because the keys move in different steps: an
  `ansible.posix.sysctl` task with no `sysctl_file` counts as an edit of that key in
  `/etc/sysctl.conf`. A path-only key would flag `initial_setup`'s hardening tasks on the Pi
  between H7.3 and H7.6g.
- **The kubeconfig.** A oneshot on daniel-box reads the read-only ServiceAccount's token
  with the k3s admin kubeconfig, which is on the host, and writes the kubeconfig file. That
  replaces `k3s/tasks/kubeconfig.yml`'s host half, which is a live read.

#### Which outcomes hold the host

H3's updater holds a host whose reconcile record does not show the installed versions
reconciled. It writes `/var/lib/homelab-updater/hold`, reports `homelab_host_reconcile_ok
0`, and leaves `homelab_host_applied_info` at the record's `last_ok`. That host then
installs no newer version, the reverted one included, until the operator clears the hold.
H3's builder publishes a host's package only when its render changed, so "the next run"
of reconcile means the next version of that host's package. Each arm outcome therefore
falls in one of two classes:

- **A fault fails the run and holds the host.** These are a violated ufw `before:`
  relation, a failed `ufw --force enable`, a declared uid that differs from the live uid,
  and a validation failure. Each
  means the package shipped something wrong, and a hold stops the next change from landing
  on top of it.
- **Drift is reported and the run succeeds.** These are a `homelab:` rule kept by the ufw
  floor, whether because the declared set is empty, because the rule is `keep`, or because
  the run already deleted two rules, and a libvirt object that a running guest still
  references. The item stays unadvanced in `applied.json`, so the next reconcile run
  retries it. `homelab_host_reconcile_drift{host,arm}` counts the items, and H3's
  `host_packages` check names a non-zero count in its message without going down. Holding
  on drift would wedge a revert: the reverted version declares nothing, every rerun meets
  the same floor, and `--clear-hold` could never succeed. Drift has no page and no
  deadline, so to retry it before that host's next version, the operator runs `sudo
  systemctl start homelab-reconcile.service` on the host (**operator-only**).
- **A legacy service is retired only while it is inactive.** An active one stays
  unretired as drift, so retiring a unit never stops a run in progress. H7.2a adds this
  rule to reconcile's legacy-entry step.

**The way out of a hold is operator-only**, because it needs root on the host:

1. Merge the fix or the revert, and wait until `homelab_repo_published_info` shows the new
   version of `homelab-<host>`.
2. On the host, run `sudo apt-get install homelab-<host>=<that version>`. Its postinst
   starts reconcile on the new version.
3. Run `sudo /usr/lib/homelab/homelab-updater --clear-hold`. It reruns reconcile and removes
   the hold only if the record then shows the new version reconciled.

An executing agent stops at step 2 and hands the two commands to the operator.

The design names three reconcilers. This plan has four, because the line arm is the only
mechanism that fits a maintainer-script-generated file or a conffile that another package
owns. *Open questions* 7 records it.

### Reconcile's line ceiling

The host plane grows a second mechanism beside dpkg. Reconcile gains the ufw, libvirt,
users and line arms, H4's udev arm and `pending.json` queue, and the activation step.
[design.md](../design.md) asks in its *Open questions* 7 whether those stay small enough to
justify that mechanism, and it makes H7's line ceiling the trigger. The ceiling comes from
the sizes the slices state:

- H3.2b states no size. Its source line count at H3.2b's merge commit is therefore the
  base, B, which H7.2a's census measures and its PR body records.
- H4.1b states about 200 lines, half of them tests, so about 100 source lines. H4.3b1's
  udev arm is about 50 of its 150.
- This slice states its own: about 120 source lines in H7.2a, 150 in H7.2b, 100 in H7.2c
  and 200 in H7.2d (*Steps*). Each PR body quotes its measured count.

The ceiling is (B + 720) × 1.3, rounded up to the next 50. The 30% is headroom for fixes
inside the stated scope, not room for another arm. H7.2a writes the number into the census
as a constant, and the H7 ADR records how it was derived.

A PR that crosses the ceiling fails the census. Raising the constant is **operator-only**,
because crossing the ceiling is design.md's trigger to switch the host plan to ansible-pull,
the design's runner-up. design.md's *Open questions* 6, the `Depends` question, is the other
trigger. In the crossing PR, the operator rules whether to cut reconcile, to raise the
ceiling in the ADR with a reason, or to start the switch.

### The Docker engine stays held

The engine never moves through the updater. `homelab-daniel-pi` declares no versioned
`Depends` on `docker-ce`, `containerd.io` or the plugins, and ships no apt preferences pin
for them, because a pin above priority 1000 forces a version change on the next apt run.

- The `apt-docker` component of `homelab-daniel-pi` ships Docker's source and key.
- `homelab-daniel-pi` ships `/usr/lib/homelab/docker-engine.pins`, rendered from
  `docker_install_package_specs`, and `homelab-docker-engine.service`, a oneshot the
  manifest marks `manual`. Reconcile never starts it.
- The unit reads the pins and compares them with the installed versions.
    - With no `docker-ce` installed, as on a fresh host, it installs the pinned versions and
      holds them.
    - With the pins installed, it confirms the hold and exits 0.
    - Otherwise it stops `homelab-compose.service`, unholds, runs `apt-get install` for the
      pinned specs, and re-holds in `ExecStopPost=`, so a failed apt run still leaves the
      hold. It then starts `homelab-compose.service`, which recreates every service, as H5.7
      does. That keeps the recreate docker-proxy needs for the new socket inode.
- The operator starts the unit from a LAN session, as today. The updater writes
  `homelab_docker_engine_pending{host}` while the installed versions differ from the pins,
  which keeps today's "reports the gap" behaviour.
- `daemon.json`, `/etc/containerd/config.toml` and the Go-runtime drop-ins declare
  `activate: manual:docker.service` or `manual:containerd.service`. A change to one goes
  live through the unit's restart mode, which restarts the daemon and recreates the project.
  That keeps "one daemon per day" an operator decision.

### Shared modules leave the setup tree

H7.2e moves every module that outlives the deployer and that code outside its own role
imports, reads, copies or stages. It derives the list with four commands rather than a
hand list, and the PR body quotes all four:

- an import grep for every module under `ansible/roles/setup/*/files/`;
- a grep for `roles/setup/` in non-comment lines outside the setup tree;
- a grep over `ansible/roles/` for `src:` lines and `lookup('file'` calls that name another
  role's `files/`, which finds the in-tree copiers the first two miss by construction;
- an import grep, top-level and function-level, for `deployer_lib`, `gitops_hold`,
  `gitops_markers` and `gitops_ledger` in every tracked file outside
  `ansible/roles/setup/gitops_deploy/`, tests included. The first three greps select the
  modules that outlive the deployer, so they cannot find a surviving file that imports a
  module that does not. This one finds those files, and *Surviving importers of deployer
  modules* below assigns each one a PR.

At `70591c307` the first three greps find three groups of modules:

- `host_lib.py` and `kuma_push.py` go to `hosts/lib/`, which packages ship to
  `/usr/lib/homelab/`. The same PR repoints every reader of the old path:
    - `install_host_lib.yml`'s `src`, and every other `src:` that names the old path: the
      stamp pairs of the six callers that declare one, and `configarr`'s and `janitorr`'s
      lines unless H2.6 has removed those imports;
    - `HOST_LIB_SRC` in `ansible/tests/setup/test_host_lib_sibling_copies.py`;
    - the two keys in `deploy_cross_role.SETUP_FILES_SHIPPED_BY_OTHER_ROLES`, so a later
      change under `hosts/lib/` still re-applies the Ansible consumers. A test shows that a
      change to `hosts/lib/host_lib.py` routes to them, and its red fixture keeps the old
      key and routes nothing;
    - autofix-bridge's staging and uptime-kuma's `lookup`, unless M6 has retired Kuma, which
      removes both.
- `longhorn_lib/` goes to `scripts/lib/longhorn_lib/`, unless H2 has moved it.
  `longhorn_groups.py` repoints its `sys.path` lines.
- `deploy_git.py:ci_verdict`, `_CI_NO_VERDICT_CONCLUSIONS` and `ci_walk_candidates` go to
  `hosts/lib/ci_verdict.py`, because the builder ships them and checkout-sync walks
  ancestors with the third. `deploy_git.py` imports no module, so the move carries no
  dependency. `deploy_git.py` imports the three from the new module until H7.19a. The same
  PR repoints the two scripts that import them outside the deployer:
  `scripts/deploy_tools/await_ci.py`, which reaches them through the `deploy_logic` facade,
  and `scripts/diagnostics/probe_lib/landing_blockers.py`, which imports
  `_CI_NO_VERDICT_CONCLUSIONS` from `deploy_git`.
- `gitops_deploy_ci_contexts` moves from `gitops_deploy/defaults/main.yml` to
  `ansible/inventory/group_vars/all.yml` as `ci_required_contexts`, because H7.20a deletes
  the role defaults and four readers outlive the deployer: D1's `promote.py decide`, H3's
  builder, checkout-sync and `ansible/tests/repo/test_ci_contexts_match_workflows.py`. The
  role default becomes a reference to the new key until H7.19a, and `await_ci.py`'s comment
  names it.
    - **Every reader repoints in this PR.** D1's `decide` reads the list from
      `gitops_deploy/defaults/main.yml` in its own checkout, by path and not through
      Ansible, and refuses an empty or missing list with `error`. A role default rewritten
      as a reference is a string to that read, not a list, so `decide` would print `error`
      on every promote run from this merge on, and cluster promotion would stop. H7.2e
      therefore repoints `decide` to `ci_required_contexts` in `group_vars/all.yml`, and
      its empty-list fixture with it. It repoints H3's builder too: its context list to
      `ci_required_contexts`, and its walk bound from `gitops_deploy_ci_ancestor_walk_max`
      to `ci_ancestor_walk_max`. Both programs exist at H7.2e's base, because H7 depends on
      H6, which depends on D2 and so on D1, and on H5, which depends on H3.
    - **The key grep finds every reader.** One more derivation grep, `git grep -n -E
      'gitops_deploy_ci_(contexts|ancestor_walk_max)' -- ':!ansible/roles/setup/gitops_deploy/'
      ':!docs/'`, lists every file that names either old key, whatever its transport. At
      `70591c307` it finds two workflow comments, `await_ci.py`'s comment and two tests
      under `ansible/tests/repo/`. At H7.2e's base it must also find `promote.py` and the
      builder; the PR body quotes it and marks each hit repointed or edited. The import
      greps cannot find these readers, because each reads a YAML file and imports nothing.
    - **The deployer's copy.** The deployer runs from `/opt/gitops-deploy/`, so the same PR
      adds `hosts/lib/ci_verdict.py` to the copy `loop:` in `gitops_deploy/tasks/code.yml`,
      with its stamp pair, and gives it a routing key that re-applies `gitops_deploy`. The
      PR body quotes `test_gitops_deploy_ship_list.py` passing, including its
      scratch-install import check. Root `CLAUDE.md`'s citation and
      `ansible/tests/deploy/test_ci_cancelled_is_not_a_verdict.py` repoint in the same PR,
      with the `docs/facts.lock` steps that *Tests and docs* gives.

`initial_setup/files/kuma-push-lib.sh` and `setup-drift-lib.sh` do not move. Only tests and
one eval record name them from outside the tree (Baseline), and those tests move or retire
with the crons that source the libraries.

The deployer's own state readers, `gitops_hold.py`, `gitops_markers.py` and
`gitops_ledger.py`, do not move. H7.19a stops the writes they read, so H7.19b deletes each
reader or switches it to C2's `DEPLOY_HOLD`.

#### Surviving importers of deployer modules

The fourth grep at `70591c307` lists these files outside the deployer's role, tests
excluded. Each lands in one of four groups.

- **D6 deletes them first:** `land_lib/land_rerolls.py` (`deploy_locks`),
  `probe_lib/releases.py` (`deploy_changes._is_test_only_path`, one of D6's six `releases*`
  modules), `narrow_lib/broad.py` and `shared_role_reach.py`. H7 re-homes nothing for them.
- **H7.2e repoints them:** `await_ci.py` and `landing_blockers.py`, as above.
- **H7.19b deletes them with their subject:** `deploy.sh`'s `deploy_lib/` (`playbook.py`,
  `tags.py`, `under_locks.py`), `gitops_state.py`, `narrow_setup.py`, `setup_routing.py`,
  `land_lib/land_reach.py`, `scripts/lib/deployer_park.py`, `probe_lib/gitops_view.py`,
  monitor-bridge's three generated copies of the state modules and
  `scripts/dev/gen_gitops_markers.py`, which writes them. monitor-bridge's
  `checks/gitops.py`, their reader, goes earlier, in H7.19a's first PR. Setup narrowing takes all seven
  `narrow_lib` modules D6 hands over: `setup_index`, `setup_playbook`, `task_walk`,
  `filters`, `foreign_keys`, `paths` and `templates`. `paths.py`'s lazy
  `deploy_logic.is_doc` import goes with it.
- **H7.19a deletes deploy_ui, if D6 kept it.** `deploy_ui.py` imports
  `gitops_markers.STATE_DIR`, `deploy_ui_reads.py` imports all three state modules, and
  `deploy_ui_writes.py` imports `gitops_hold.Hold` and `HOLD_PLANE_SEP`. Each action it
  offers, starting `deploy.sh` and clearing `hold_sha`, has no subject once H7.19a and
  H7.19b land. H7.11 states the one exception.
- **H7.19b re-homes or switches them, because they survive:**
    - `land_lib/tools.py` imports `deploy_locks.TREE_LOCK`. `TREE_LOCK` moves to
      `scripts/lib/tree_lock.py`, and `test_tree_lock_single_definition.py` holds that
      constant equal to `server_git_tree_lock` from then on.
    - `scripts/secrets_mgmt/consumers.py:_census_corpus` calls
      `deploy_cross_role.k8s_lookup_map`. That function imports only `ast`, `re` and `Path`.
      It moves to `scripts/lib/k8s_lookups.py`, with its test, before
      `deploy_cross_role.py` goes in H7.20a. This is D6's handoff ("H7 moves
      `k8s_lookup_map` out first").
    - `land_lib/land_shared.py` imports `deploy_logic.role_of`, and `reach.py`'s `role_for`
      and `is_role_test_path`. D6 hands H7 "the shared rest of `reach.py`". H7.19b greps
      its base for callers of `land_shared.py` that outlive land.sh's setup and Pi arms.
      If any exist, `role_of` with its `RolePath` and the two `reach.py` functions move to
      `scripts/lib/role_paths.py`, and `reach.py` is deleted. If none exist,
      `land_shared.py` is deleted with `reach.py`, and the PR body quotes the empty grep.
    - `land_lib/classify.py` loses its `deploy_cross_role` arm, which reads the setup
      routing tables. `land_tags.py` loses its setup-plane imports from `deploy_logic`,
      and `land_platform.py` loses its `reach` import.
    - `land_lib/landing.py`, `options.py`, `outcome.py` and `tools.py` read
      `gitops_ledger` and `gitops_markers` (receipts, `STATE_DIR`, `HOLD_CLEAR_CMD`,
      `DeployerSnapshot`). Each read is deleted with the arm that used it, or switched to
      C2's `DEPLOY_HOLD`.
    - `runbook_gates_lib/k3s_upgrade.py`, `probe_lib/landing_blockers.py` and
      `scripts/docs/reference/state.py` read `gitops_hold` and `gitops_markers`. Each
      switches to `DEPLOY_HOLD` or drops the read.
    - renovate_agent's `agent_toolbox.py`, `agent_logic.py` and `renovate_agent.py` read
      `gitops_hold` and `gitops_markers`. renovate_agent survives, because H7.11 packages
      it and its lander runs `land.sh`, so its reads switch to `DEPLOY_HOLD` or are
      dropped.

**Path readers.** An import census cannot see a module that reads a deployer file by path.
H7.2e runs a fifth derivation grep, `git grep -n -E
'/(etc|var/lib|opt)/gitops-deploy' -- ':!ansible/roles/setup/gitops_deploy/' ':!docs/'`,
and assigns each hit outside tests:

- `scripts/docs/gen_doc_fragments.py:_gitops` reads `deploy_changes.py` and
  `gitops_deploy.py` with `module_constant`. H7.19b retires that fragment and the
  transclusion that includes it, or H7.20a would turn
  `test_every_committed_fragment_matches_what_the_generator_writes_now` red.
  `repo_paths.GITOPS_DEPLOY_FILES` goes in H7.19b with its last reader.
- monitor-bridge's `monitor-bridge-gitops-state` volume and mount, `GITOPS_STATE_DIR` and
  `config_service.py`'s read of it go in H7.19a's first PR, with `checks/gitops.py`'s two
  checks, their `check_table.py` rows and Kuma tiles. Their two push-token keys stay in
  `secrets.yml` as orphans, because removing a key needs a `sops` edit; the PR body names
  their removal as an **operator-only** cleanup. They must go before the
  timer does, because `check_gitops_alive` pages on a stale `last_run`. The mount is `type:
  Directory`, so a pod that kept it would fail to start once the operator removed the
  directory, and would never schedule on a rebuilt daniel-box. The pod keeps its daniel-box
  `nodeSelector` for the etcd drill's stamp directory, and the role's `CLAUDE.md` restates
  that as the pin's reason. If M6 has already turned the two checks into exporter series,
  the series go instead.
- `land_renovate_pr.py:DEPLOYER_CONFIG` no longer exists by then: D6.13 repoints the
  lander's denylist at `k8s_autodeploy_counts.py --denylist`. The PR body quotes the empty
  grep that confirms it.
- `fanout_lib/brief.py:APPLY_OWED` drops its `cat` of `hold_sha` and `owed.jsonl` in
  H7.19a, and names `DEPLOY_HOLD` and C6's host wait in its place.
- The grants in `renovate_agent/tasks/main.yml` and `claude_code/tasks/agent_access.yml`
  stay out of the components that H7.11 and H7.12 build. The ACLs Ansible set stay on the
  directory until the operator's cleanup removes it, and the readers that use them switch
  in H7.19b.

**The deployer-import census lands in H7.19b,** after the deletions it checks, as a row in
`ansible/tests/repo/test_census_rows_python.py`. It walks the AST of every tracked `.py`
file outside `ansible/roles/setup/gitops_deploy/`, the `hosts/` tree included, and reads
every `Import` and `ImportFrom` node, function-level ones too. It fails on any import of
`deployer_lib`, `gitops_hold`, `gitops_markers` or `gitops_ledger`, and on any use of
`GITOPS_DEPLOY_FILES`. It also reads every string constant in those `.py` files and fails
on one naming `/etc/gitops-deploy`, `/var/lib/gitops-deploy`, `/opt/gitops-deploy`,
`gitops_tick.sh`, `gitops_deploy_ci_contexts` or `gitops_deploy_ci_ancestor_walk_max`. The
last two keep a later path reader of the deleted role defaults from returning, which the key
grep in *Shared modules leave the setup tree* finds only at H7.2e. It reads only Python, so the legacy entries in `hosts/*/package.yml` that
retire those paths are not hits. The deployer's code stays on disk until H7.20a, and so do the tests
of that code, under `ansible/tests/deploy/` and the role's own `tests/`. The census's
allowlist names each of those test modules, H7.20a deletes them with their subject, and the
allowlist is empty once H7.20a merges. Its named member is
`scripts/deploy_tools/await_ci.py`, which the walk must read. Three red fixtures are
flagged: a module holding `from deployer_lib.deploy_locks import TREE_LOCK`, a module with
`import gitops_markers` inside a function, a module whose string constant is
`/var/lib/gitops-deploy/hold_sha`, and a module that indexes a loaded YAML mapping with
`"gitops_deploy_ci_contexts"`. The census and H7.20a's `pytest --collect-only` check different things.
Collection imports only the modules a test reaches, and the census reads every tracked
module without importing it.

Each move updates `pyproject.toml`'s `pythonpath` and `extra-paths` in the same PR. A
cluster render proves the move changed no manifest: `homelab-render cluster` runs at the PR's
base and head, and `diff -r` over the two trees prints nothing. That is R4's exit 3 method.
`shadow_diff.py` is gone after R4 PR 9, so it is not used.

### The deployer retires last, and checkout-sync replaces its fast-forward

The deployer applies daniel-box's setup plane until every daniel-box role is packaged, which
is the end of H7.14. H7.19a's fourth PR ships `homelab-checkout-sync` beside the running
deployer, the fifth PR stops the deployer ticking, and its last units and paths go in
H7.19b.

- `homelab-checkout-sync.timer` fires every 10 minutes, as the tick does, with
  `OnBootSec=10min` and `OnUnitActiveSec=10min`. A monotonic timer has no calendar instant,
  so H6's window test fails it unless it is on that test's exemption list (H6 *Open
  questions* 10). The fourth PR adds it there, with its exclusion: checkout-sync
  fast-forwards a git checkout and calls GitHub's check-runs API, it neither restarts nor
  reconfigures k3s, and it reads no Longhorn volume. An `OnCalendar` fire every 10 minutes
  was rejected, because the test would then pin each restart window between two of its
  fires, for a unit that a k3s restart cannot harm. The window test reads schedules through
  the sources `gen_role_glance.py` reads, which H7.18a points at the packages, so the
  fourth PR merges after H7.18a. Its PR body quotes the window test naming
  `homelab-checkout-sync.timer` among the timers it read.
  `homelab-checkout-sync.service` runs as `sys_user` in the primary checkout. It takes the
  git-tree lock (`server_git_tree_lock`) and runs `git fetch origin master`. It refuses a
  dirty tree and a branch other than `master`, as the deployer does.
- **The CI gate stays.** The deployer's contract never applies "a red or unfinished CI
  verdict". The checkout timers that H7.10 packages run the tree's code as `sys_user`, so
  checkout-sync keeps that gate, and it keeps the deployer's choice of commit too. The
  docstring of `deploy_git.py:ci_walk_candidates` records it as "DECIDED the gate chooses
  the NEWEST GREEN ANCESTOR, the walk is bounded, and a red ancestor is never chosen".
  checkout-sync grades the fetched tip over `ci_required_contexts` with `ci_verdict` from
  `hosts/lib/ci_verdict.py` (H7.2e). If the tip is not green, it lists `git rev-list
  --first-parent HEAD..origin/master`, passes that list to `ci_walk_candidates` with no held
  SHA and the bound `ci_ancestor_walk_max`, and grades each candidate newest first. It stops
  at the first green one.
- **The walk bound has its own source.** At `70591c307`, `deploy_config.py` reads
  `CI_ANCESTOR_WALK_MAX` into its `ci_ancestor_walk_max` field from the deployer's
  `config.env`, which
  `gitops_deploy/templates/config.env.j2` renders from the role default
  `gitops_deploy_ci_ancestor_walk_max: 10`. H7.19b retires that config and H7.20a deletes
  the default. H7.2e therefore moves the value to `ansible/inventory/group_vars/all.yml` as
  `ci_ancestor_walk_max`, beside `ci_required_contexts`, and the role default becomes a
  reference to it. checkout-sync's unit renders it into `Environment=`. checkout-sync
  refuses a missing, non-integer or non-positive bound with `refused{reason="ci"}` before
  it grades anything, because a bound of 0 disarms the walk and brings back tip-only
  grading.
- **The token is the builder's, delivered as a credential.** The anonymous API quota is 60
  requests an hour per source IP, which the builder and `await_ci.py` share, and one run
  may spend up to the bound's 10 requests. checkout-sync's component therefore declares
  `LoadCredential=github_token:/run/credstore/homelab_builder_github_token`, the form H4
  gives, and adds no SOPS key. Its wrapper exports the file as `GH_TOKEN` and calls
  `host_lib.github_token` with a `run` that refuses the `gh auth token` fallback. The
  fallback matters here: `sys_user`'s gh is logged in as the repo owner, so without that
  `run` the call would never return None and would spend the operator's personal token.
  With no credential file, checkout-sync sets `refused{reason="token"}` and makes no
  request, as H3's builder exits before any request.
- **An empty context set is refused.** The docstring of `deploy_git.py:ci_verdict` says
  "An empty set means the gate is disarmed, which returns `pass`", and
  `await_ci.py:DisarmedGateError` refuses that case for the same reason. checkout-sync
  refuses it too, before it grades anything: an empty or missing `ci_required_contexts`
  sets `homelab_checkout_sync_refused{reason="ci"}` and merges nothing. The refusal lives
  in checkout-sync, not in `ci_verdict`, because the deployer relies on that disarm switch
  until H7.19a. A red or pending verdict, or one of `_CI_NO_VERDICT_CONCLUSIONS`, is never
  chosen. Only the chosen commit is merged, with `git merge --ff-only <sha>`, so a busy or
  red master tip does not freeze the checkout behind an older green commit. With no green
  candidate, the checkout stays where it is, and the next run tries again.
- **No `DEPLOY_HOLD` read.** C2's hold stops cluster promotion, and `promote` already resets
  `prod`. The checkout's readers publish docs and rotate secrets, and a cluster hold makes
  neither unsafe. The deployer's `hold_sha` existed because the deployer applied what it
  merged, and checkout-sync applies nothing. The H7 ADR records this narrowing as a decision.
- It writes `homelab_checkout_sync_success_timestamp_seconds`, `homelab_checkout_dirty`, and
  `homelab_checkout_sync_refused{reason}` with the reasons `dirty`, `branch`, `ci` and
  `token`. Its
  journal line names the merged commit and its distance below the tip, as the deployer's
  does.
- Its component carries an `## Autonomous-role contract` header: scope, a fast-forward of
  `master` only; mode, the timer's enable link; abort valves, the lock, the dirty and branch
  gates and the CI gate; evidence, the metrics and the journal.
- **Overlap.** Both timers run from H7.19a's fourth PR until reconcile removes the tick's
  timer after the fifth. Both actors take the same tree lock, and a fast-forward-only merge
  to a commit `HEAD` already contains does nothing, so the overlap is harmless. The
  deployer's range may then be empty because checkout-sync moved `HEAD` first, and that is
  safe only because nothing remains for the deployer to apply: every daniel-box role is
  packaged by the end of H7.14, and D6 has removed its cluster planes. The fourth PR
  therefore merges only after H7.14 and D6. Retiring the deployer therefore stops both, and no setup change lands in a
  window with no applier.
- **Retirement, across two daniel-box versions.** H7.19a's version lists only
  `gitops-deploy.timer` as a legacy entry. Reconcile stops and removes the timer, which
  starts no new tick and does not stop a running one. H7.19b's deleting PR lists the rest:
  `gitops-deploy.service`, `/opt/gitops-deploy`, `/etc/gitops-deploy` and
  `/etc/polkit-1/rules.d/50-gitops-deploy.rules`. By then nothing has started the service
  since H7.19a: the timer is gone, land.sh starts no tick (*land.sh stops running the
  tick*), and H7.19a retires the `gitops-tick` skill. Reconcile retires the service only
  while it is inactive. If someone started it by hand, the entry is drift (*Which outcomes
  hold the host*), and the next reconcile run retires it. Splitting the retirement removes
  the wait inside reconcile, and with it a timeout that would have held daniel-box.
- **H6's deployer-coupled checks go with the timer.** H6 *Open questions* 10 hands H7.19a
  seven items that read the deployer's lock or state, and the fifth PR, which stops the
  tick, removes all seven:
    - the hold check in `k3s_gates.py`'s server mode, which reads `hold_sha` under `/host`;
    - the `all` deploy-lock check in the same mode;
    - step 5 of `homelab-k3s-restart`, which takes the `all` lock on daniel-box and refuses
      on a non-empty `hold_sha`;
    - daniel-box's `tmpfiles.d` entry for `/run/lock/server-deploy-all.lock` in the
      `k3s-restart` component;
    - the test that holds that lock path equal to
      `deploy_locks.py:lock_path(SERVICE_LOCK_ALL)`;
    - the `held_sha` case of H6's gate parity test;
    - the window test's exemption for `gitops-deploy.timer`.

  None of the seven can outlive the deployer. The hold check refuses an absent state
  directory, as `k3s_upgrade.py:held_sha` does (H6 *Open questions* 10), so once the operator
  removes `/var/lib/gitops-deploy` after H7.19b, every server-mode `prepare` and every
  daniel-box k3s restart would refuse. The equality test imports `deploy_locks.py`, which
  H7.20a deletes. The exemption names a timer this PR removes. The removal therefore lands
  before H7.19b and H7.20a. It lands no earlier than the fifth PR, because until the timer
  stops a tick can still hold the `all` lock. `k3s_gates.py` ships in both hosts'
  `k3s-restart` component (H6), so the PR publishes new versions of both host packages,
  and daniel-box gets the gate change in the same version that removes the timer. The PR
  body quotes H6's gate, parity and window tests passing at its head, and `git grep -n -E
  'server-deploy-all|SERVICE_LOCK_ALL' -- hosts/` printing nothing.
- **The deployer's state.** `/var/lib/gitops-deploy/` is state, and H7.19b's PR body names
  it as an operator cleanup. Before that cleanup, nothing that survives may read the
  directory. H7.19a's first PR removes monitor-bridge's mount, and *Surviving importers of
  deployer modules* assigns every other reader.

#### land.sh stops running the tick

land.sh reaches the deployer through `gitops_tick.sh`, a subprocess, so neither the
deployer-import census nor `pytest --collect-only` sees the dependency (Baseline). After
H7.19a's version removes the timer, a land.sh tick would still start the retired service.
After H7.19b's version removes the service, every tagless landing would end `deploy-failed`.
H7.19a's third PR therefore removes every land.sh path that starts or reads a tick, before
the fifth PR retires the timer. Between the two, the timer still fast-forwards the primary
checkout within 10 minutes, and no landing step reads the checkout after the merge:

- `pipeline.py:_step_tick` and its row in `_STEPS`. After D6 a tagless landing needs only
  H3.9b's "waiting for hosts to apply" step, which is C6's covered wait on
  `homelab_host_applied_info`. checkout-sync's timer converges the primary checkout within
  10 minutes, and no later step reads it.
- `tick.py`, with `run_tick`, `kick_tick` and `rearm_tick`, and `tools.py:run_tick`. Their
  call sites go too: `pipeline.py:_step_tick`, `deploy.py:deploy_phase`'s `kick_tick` and
  the `run_tick` on its stale retry, and `health_verdict.py:health`'s `rearm_tick`.
- The part of land.sh's setup arm that reads the tick's markers. `classify.py` no longer
  sets `self_applied`, `self_applied_command` or `remaining_setup`. Every daniel-box setup
  role is empty by the end of H7.14 except `gitops_deploy`, and the only PR that changes
  that role before H7.19b is the fifth, which empties it. `deploy.py:no_tag_outcome`
  loses its `tick_state` and `tick_applied` arms, so a PR that reaches only an emptied
  setup role ends `nothing-to-deploy`. H7.19b deletes the rest of the setup arm.
- `gitops_tick.sh` itself, with its wrapper test and the land tests whose subject is the
  tick. The PR derives that list from `git grep -l -E
  'gitops_tick|run_tick|kick_tick|rearm_tick|tick_state|tick_applied'` at its base, and
  quotes the output with each hit marked deleted or rewritten.

`ci.py:preflight` and `probe_lib/landing_blockers.py` keep reading the deployer's state
until H7.19b switches them to `DEPLOY_HOLD`. H7.19a merges only while `hold_sha` is empty
and `owed.jsonl` holds no line, so those frozen reads return nothing in the way.

### Architecture-specific binaries

`homelab-tools` is an architecture-specific package that nfpm builds from pinned upstream
binaries, each checked against its sha256: sops and chezmoi, plus nfpm on daniel-box. The
pins move
from role defaults such as `sops_setup_version` into `hosts/packages/homelab-tools/`, where
`asset_pins.py` and Renovate already know the shape. C5 names the package, H3 builds it,
and the Pi is its `arm64` pilot. H7.1 adds nfpm to it for daniel-box.
The Pi's node exporter uses Ubuntu's
`prometheus-node-exporter` if its version is acceptable, and `homelab-tools` otherwise.

`bootstrap-host.sh` also carries pins, for nfpm and uv, each a version with a sha256 per
architecture, because `--local-build` needs both before any package exists. A test in
`hosts/lib/tests/` holds the script's nfpm pin equal to the one in
`hosts/packages/homelab-tools/`, and the uv pin equal to the one the builder uses, so the
Renovate manager that bumps one must bump both.

The script also embeds the three k3s pins: `k3s_version`, `k3s_install_script_url` and
`k3s_install_script_sha256`. On daniel-server it runs without `--local-build`, so no clone
is there to read them from. H6 moves the three keys into the `system-upgrade` role's
defaults, which drive the SUC Plans. The same test holds each embedded k3s pin equal to
H6's key, so a rebuilt node never installs an older k3s than the Plans target. The
Renovate manager that bumps `k3s_version` lists the script as a second file, and the test
refuses a PR that bumps only one side.

### The pinned endpoints that remove live reads

- `nut_host` on daniel-box reads the `nut` Service's ClusterIP with kubectl. H7.8 first
  pins `spec.clusterIP` in `ansible/roles/k8s/nut/` to the live value. That is a no-op on the
  live Service, because the value is unchanged. Then it renders the secondary from the pin.
- `deploy_ui`'s firewall sources and the builder's ufw rule already read inventory only.
- pi-peer-backup's rsync target reads `hostvars['daniel-pi'].server_ip` and a role default
  user in place of `ansible_host` and `ansible_user` (H7.18d).

### `bootstrap-host.sh`, complete

The script takes `bootstrap-host.sh <hostname> [--until <step>] [--local-build <clone>]`
and runs as root. It is idempotent, with the keyring fingerprint pinned in the script. Its
steps are:

1. Refuse if `hostname` differs from `<hostname>`.
2. Install the homelab keyring, verified against the pinned fingerprint. Without
   `--local-build`, also write `homelab.sources`.
   With `--local-build`, write only the keyring, because the repo is not running yet; step 5
   installs `homelab.sources` from the local build.
3. Create `/etc/homelab/age.key` if it is missing, and print the public key. If the key
   comes from the break-glass kit, the public key is unchanged, so no recipient commit is
   needed.
4. Run the one-time host steps, each behind a guard that reads the state first:
    - the LVM volumes, on daniel-server only;
    - the debloat purge, on the Pi only;
    - the k3s binary at `k3s_version`, from the release's `install.sh` checked against
      `k3s_install_script_sha256`, on the two cluster nodes. Both values, and
      `k3s_install_script_url`, are the script's embedded pins (*Architecture-specific
      binaries*). The script passes
      `INSTALL_K3S_EXEC=server` on daniel-box or `INSTALL_K3S_EXEC=agent` on daniel-server,
      with `INSTALL_K3S_SKIP_ENABLE=true` and `INSTALL_K3S_SKIP_START=true`. It never relies
      on `K3S_URL` to pick the mode. It then asserts that exactly one of `k3s.service` and
      `k3s-agent.service` exists, the one the host's role names. The package's enable link
      and H6's `ExecStart` drop-in enable and configure that unit, so the installer's
      default cannot start a second control plane.
    - On daniel-server, the installer also takes `K3S_URL` and `K3S_TOKEN`. The script
      reads both from its own environment, passed with `sudo --preserve-env`, and refuses
      when either is unset. `install.sh` writes them to
      `/etc/systemd/system/k3s-agent.service.env`, which H6 leaves as the agent's credential
      and which the etcd drill reads. The token is the one `docs/break-glass.md` item 2
      holds, so supplying it is **operator-only**. The guard skips the installer when the
      binary is at `k3s_version` and the unit exists, so a rerun never rewrites the env
      file.
5. Install `chezmoi` from `homelab-tools`, then run `apt-get install homelab-updater
   homelab-<hostname>`, then `systemd-tmpfiles --create`, as boot does. That creates each
   directory a package declares in `tmpfiles.d`, the Pi's textfile directory (H7.4) among
   them, before the updater's first run writes into it. daniel-box serves the repo it installs from, so it cannot bootstrap
   from `http://10.0.0.215:8791`, and it uses `--local-build <clone>` instead. That flag
   builds every package daniel-box needs from the clone:
    - It checks the clone's `HEAD` against C5's anchor, the rule the builder applies
      ([ADR-0021](../../adr/0021-the-host-package-builder-trusts-master-plus-green-ci.md)):
      `HEAD` must be on `origin/master`'s first-parent chain, and its CI verdict must be
      green. It verifies no signature, because the builder checks none. It reads both
      halves from GitHub, which daniel-box reaches because it already fetches `origin`. A
      `HEAD` that fails either half is refused, and the refusal names the half. The operator
      ruled this on 2026-10-15
      ([#5011](https://github.com/DanielH2018/server/issues/5011)).
    - Under `--offline`, GitHub is unreachable, so the script makes neither read. It prints
      that the clone is unchecked, and the operator who chose the clone is its trust root,
      as for any hand-run bring-up.
    - It fetches nfpm and uv at the versions pinned in the script and checks each sha256.
    - It renders and builds `homelab-tools`, `homelab-updater` and
      `homelab-daniel-box` from the clone, with `uv run --frozen` and
      `host_python_version`, as the builder does.
    - It installs the three in one `apt-get install ./<a>.deb ./<b>.deb ./<c>.deb`, so apt
      resolves their `Depends` among themselves and against Ubuntu's archive. The host
      package's own apt sources take effect from the next `apt-get update`.
    - The local `homelab-updater` ships `homelab.sources`, and `homelab-daniel-box` starts
      the signer, the builder and the repo. The next updater run moves the host onto the
      signed repo, at the same version or a newer one.
6. On the Pi only, start `homelab-docker-engine.service` once. The package shipped it in
   step 5 as a `manual` unit, so nothing else starts it. On a host with no `docker-ce` it
   installs the pinned versions, holds them, and starts `homelab-compose.service`, which
   failed in step 5 for want of an engine.

`--until <step>` stops after a step, so a runbook can restore data before step 5 starts any
service. The operator then adds the printed public key as a recipient (**operator-only**)
and merges. The next package carries ciphertext the host can read, and `homelab-secrets`
succeeds. The k3s agent on daniel-server needs no SOPS value, because it starts from the
env file the installer wrote in step 4. The script also absorbs `preflight.yml`'s checks
for a wrong hostname, a missing host var and a missing key.

### The Pi rebuild runbook

`docs/break-glass.md` gains the Pi runbook in H7.15b. Its steps are:

1. Flash stock Ubuntu 24.04 for Raspberry Pi. Before first boot, set the hostname to
   `daniel-pi` in the boot partition's cloud-init `user-data`, because the image boots as
   `ubuntu` and step 1 of the script refuses that.
2. Copy `hosts/bootstrap-host.sh` and `hosts/keys/homelab-archive-keyring.gpg` from a clone
   onto the boot partition. Both are public. The break-glass kit holds a copy as well.
3. Restore the Pi's SSH host keys and age key from the break-glass kit
   (**operator-only**). If the kit has no host keys, the operator commits the new host-key
   pin that R3 added, which is the second commit.
4. Run `sudo bootstrap-host.sh daniel-pi --until 4`.
5. Restore wg-easy's `config/` from the `pi-peer-backup-data` PVC into the Compose project's
   `project_directory`, which H5 kept at `/home/ubuntu/server/containers/wg-easy`. The files
   hold client private keys, so this step is **operator-only**.
6. Run `sudo bootstrap-host.sh daniel-pi`, which installs the package and starts the Compose
   project. The package also installs pi-peer-backup's forced-command wrapper and
   authorized key (H7.4).
7. If step 3 created a key, the operator adds it as a recipient **without removing the old
   one** (**operator-only**). The old key leaves the recipient list only after the rebuild
   is accepted.
8. An agent reads `homelab_host_applied_info{host="daniel-pi"}` and
   `node_textfile_scrape_error{job="node-pi"}` from Prometheus. The first must name the
   version that `dpkg-query -W homelab-daniel-pi` prints on the Pi, and the second must
   read 0. C6's host wait reads the first series, so a rebuilt Pi whose updater cannot
   write the textfile directory would leave every later host landing waiting. The rebuild
   is accepted only after this step passes.

Rollback is the old SD card. Because the old age key stays a recipient until acceptance,
the old card can still decrypt the next package. If step 3 committed a host-key pin, the
rollback reverts that commit too, or the nightly pi-peer-backup run fails against the old
card's key.

### The cluster-node bring-up

`docs/break-glass.md` gains the cluster-node procedure in H7.15c, and
`docs/longhorn-disaster-recovery.md` step 2 and `docs/k3s-etcd-restore.md` point at it in
the same PR. Its steps are:

1. Run `bootstrap-host.sh` with `--until 4`, which installs k3s in the declared mode. On
   daniel-server the operator supplies `K3S_URL` and `K3S_TOKEN`, the token from
   `docs/break-glass.md` item 2 (**operator-only**), and the installer writes the agent's
   env file.
2. On daniel-box, restore two things before step 3 starts any service, each
   **operator-only**:
    - the server token and, for a restore, etcd, as `docs/k3s-etcd-restore.md` describes.
      H6 keeps the server token out of the package, and this step is where it returns;
    - the archive signing key, from the break-glass kit into the signer's GnuPG home,
      because every host's keyring pins its fingerprint. H3.4b put the key in the kit. The
      builder's `ExecStartPre=+` script writes `/etc/homelab-builder/archive-key.fpr` from
      that key on the builder's first run (H7.14). Without the restore it writes nothing,
      and every build refuses with `reason="keyring"`, which is the intended state for a
      builder with no signing key.
3. Run the script to the end. On daniel-box use `--local-build <clone>`, with a clone from
   GitHub or from the kit's repo bundle (item 8). On daniel-server the agent starts from
   its env file and joins.
4. Bootstrap Flux by following *Bootstrapping Flux on a rebuilt cluster* in
   `docs/flux-deploy-plane.md`, which R4 PR 7b writes. H7 does not restate its steps or
   their order. That section applies the PriorityClasses Flux's own controllers need, then
   the components and sync objects, creates `sops-age` (**operator-only**), and applies the
   `OCIRepository` and the root Kustomization. `docs/break-glass.md` links the section and
   adds nothing to it.

**The offline branch.** When GitHub or GHCR cannot be reached, the procedure takes every
fetch from the break-glass kit instead, and each step stays **operator-only**:

- the clone, from the kit's repo bundle (item 8), as step 3 already allows;
- the k3s binary and `install.sh` at `k3s_version`, which step 1 otherwise fetches from
  GitHub;
- nfpm, uv and uv's package cache, which `--local-build` otherwise fetches;
- the Flux controller images, which GHCR serves (*Open questions* 14);
- the cluster artifact, through C2's break-glass promotion. The operator renders from the
  repo bundle, pushes the result to the in-cluster registry, and points the OCIRepository
  at it, verified against the break-glass cosign public key. D6 owns that runbook, and step
  4 links it rather than restating it.

`bootstrap-host.sh` takes `--offline <kit-dir>`. It reads each file from that directory,
checks it against the same sha256 pin the download path checks, and refuses on a missing
file or a mismatch, naming the file. `homelab-daniel-box` ships the GitHub CLI apt source (the `apt-github-cli` component), which
is a GitHub host, so in step 5 `--offline` runs `apt-get update` without that one source.
The next online updater run reads it again. `docs/break-glass.md` lists the kit's offline files,
and the *Bootstrap pins* test holds each listed pin equal to the script's.

Nobody creates `homelab-cluster-vars` by hand. In D3's design, the `cluster-vars`
Kustomization decrypts `cluster-vars.sops.env` with `sops-age`, and its `secretGenerator`
produces the Secret. A hand-made copy would be a second writer, which Flux then overwrites.
Every Kustomization that substitutes from it waits on `cluster-vars` through `dependsOn`.
The check is read-only: the `cluster-vars` Kustomization in `flux-system` reads Ready. A
cluster restored from an etcd snapshot already holds `sops-age` and the Flux objects, so a
restore skips step 4 and runs only the Ready check. The restored `sops-age` can hold a key
that a later D3.4 rotation replaced. If the Ready check fails after a restore, the operator
replaces `sops-age` with the current cluster key (**operator-only**).

**The agent keeps the server token.** This resolves H6 *Open questions* 4, which handed the
choice to H7. H7 adds no `agent-token` to the server's config, for three reasons:

- no k3s document describes an already-joined agent switching to an agent-scoped token, so
  the switch would need an operator-attended window and an untested rollback;
- the server token would still join agents, so an agent token in SOPS would add a
  credential without removing one;
- the rebuild path already has the operator present, supplying the token from break-glass
  item 2, as daniel-box's step 2 does.

The etcd drill (H7.13) keeps reading the agent's env file, and exit 3 and exit 4 name it. A
later slice may still add an agent token, inside an operator-attended window with the env
file as its rollback.

**The proofs.** H7.15c proves the procedure before H7.20a deletes `k3s-bringup.yml`, in two
halves (exit 4).

- **The Flux half, on kind.** R4 PR 7b's `flux-bootstrap-drill.yml` proves the section's
  steps on a throwaway kind cluster, with a fixture bundle and a throwaway key. That is C8's
  first substitute: the proof needs only the Kubernetes API, the pinned Flux controllers
  and a local-path volume. H7.15c's PR runs the workflow, by `workflow_dispatch` if its
  path filter skips the PR, and quotes both jobs.
- **The host half, in a drill guest.** This is C8's third substitute, an
  **operator-only** drill on production hardware, so the PR states its blast radius.
    - *Transience and the fence.* The guest is transient like the etcd drill's: `virsh
      create`, never `virsh define`, destroyed with its disk on every exit path. The drill
      reuses that drill's orchestrator, the `staging` network and `fence_check`, and the
      fence check passes before anything is copied into the guest.
    - *Identity.* The guest plays daniel-box. A lone guest can only be a server, because an
      agent could not reach daniel-box through the fence.
    - *What it runs.* `bootstrap-host.sh daniel-box --local-build <clone>`, with the clone
      fetched from GitHub over the guest's internet egress. No copy of the production repo
      is staged, so the drill takes the path a real rebuild takes.
    - *Two guest-local deviations.* The packaged k3s config sets `--node-ip` and
      `--advertise-address` to `server_ip`, which a fenced guest must never hold. The drill
      therefore writes `/etc/rancher/k3s/config.yaml.d/90-drill.yaml`, which sets both to
      the guest's own address. Before step 5 it also masks every timer and long-running
      service the rendered package ships, except `k3s.service`, `homelab-reconcile.service`
      and `homelab-secrets.service`, so nothing the package schedules runs. Reconcile then
      reports the masked units as refused starts, which the drill expects and does not
      grade. The package's netplan file is `activate: manual:systemd-networkd.service`
      (H7.9b), and the transient guest never reboots, so it never takes the LAN address.
    - *A third deviation, for the firewall.* Reconcile's ufw arm enables ufw in the guest,
      as on a real rebuild, and exit 4 checks that it did. The orchestrator reaches the
      guest over ssh from the `staging` bridge, which is outside the LAN, so only the SSH
      limit rule admits it, and that rule rejects a sixth connection within 30 seconds.
      The orchestrator polls over fresh ssh sessions. Before step 5 the drill therefore
      adds one guest-local rule, `ufw allow from <staging bridge address> to any port 22
      proto tcp`, with no comment. The ruleset is empty then, so the rule lands above the
      limit rule the arm appends later. The arm never deletes a rule without a `homelab:`
      comment, and the rule dies with the guest.
    - *No production credential.* The drill skips step 2, so k3s generates a guest-only
      server token and the guest holds no copy of the archive signing key. The guest's age
      key is fresh and is no SOPS recipient, so `homelab-secrets` decrypts nothing. k3s's
      etcd-s3 credential is a Secret that Flux applies from `cluster-base`, which the guest
      never receives. If k3s refuses to start with `etcd-s3` on and no Secret, the override
      file also sets `etcd-s3: false`, and the PR body records which happened.
    - *Blast radius.* The drill touches a transient domain and its disk on daniel-server,
      and the existing `staging` network and fence. It takes CPU and memory from the agent
      node for one run, sized like the etcd drill's guest. Every service keeps serving,
      because the guest reaches no LAN address and holds no production credential. The
      rollback is the drill's own teardown.
    - *The check a lone guest can pass.* The four packages are installed, `k3s.service` is
      the only k3s unit, and `k3s kubectl get node daniel-box` reads Ready.
- **In CI.** The systemd container proves the installer-mode assertion in both modes. It
  also runs `--local-build` on the CI checkout, as far as `apt-get install --simulate` of the
  four `.deb` files with no homelab repo configured.

A full-artifact proof, a guest that applies production's `prod`, is not planned. It would
write to the ACME resolver, B2 and R2, Healthchecks.io and Discord with real credentials,
and nothing here suppresses those writes. If the operator wants one, it is a separate
**operator-only** drill whose PR states that blast radius under C8.

### Ubuntu release upgrades

C5 gives this slice the release-upgrade procedure. No package depends on one Ubuntu
release, and H3's container suite runs on every release the fleet runs or is moving to.
One host moves at a time, in H3's canary order, and each step is **operator-only**:

1. The PR that adds the target release to the container suite's matrix merges first, with
   the suite green. The host's package at master's version is then one the suite tested on
   that release.
2. Hold the updater with H3's hold runbook in `docs/host-packages.md`, so no package
   installs during the upgrade.
3. On a cluster node, label it `homelab/k3s-upgrade-paused=true`, so no promoted k3s bump
   starts a SUC Job during the upgrade. Run H6's gate script in `host` mode, and pick a
   quiet slot outside `homelab-k3s-restart`'s window. Then run `do-release-upgrade` and
   reboot.
4. Verify against the matrix release. `lsb_release -rs` prints the target release, and
   `dpkg --verify` reports nothing for the `homelab-*` packages. `systemctl start
   homelab-reconcile` exits 0. Prometheus reads `homelab_host_reconcile_ok` at 1 and
   `homelab_host_applied_info` at the version the suite tested.
5. Release the hold with the same runbook, and on a cluster node remove the label. The
   next updater run installs whatever master published meanwhile, and SUC resumes on the
   node.

A host that fails step 4 stays held. Its rollback is a rebuild on the previous release with
`bootstrap-host.sh`, as the Pi runbook and the cluster-node procedure describe.

### Adding a host

The tree must be able to take another host before H7.20a deletes the bring-up playbooks,
which are the old way to add one. `docs/host-packages.md` gains the walkthrough, modelled
on H4.2's and H6.2's step lists. Steps 1 and 3 to 5 go in the host's first PR:

1. Add `hosts/<host>/package.yml` with the `reconcile` component, the updater's enable
   link and any `windowed` mark, and the host's nfpm spec in the form H3's builder takes. A
   host on another architecture first needs H3's `homelab-tools` build for it.
2. **Operator-only:** run `bootstrap-host.sh <host> --until 3`, which creates the age key
   and prints its public key.
3. Add the creation rule for `ansible/vars/hosts/<host>.sops.yaml` ahead of the general
   rule, with the host's printed key, daniel-box's key and the recovery key. Adding a
   recipient is **operator-only**.
4. **Operator-only:** run `host_secrets.py sync` for the host, and commit its file.
5. Add the host to every census row that names hosts: both CI scans' named sets of
   package roots, the hosts `host_secrets.py check` must find, and H3's canary order.
6. After the merge, run `bootstrap-host.sh <host>` to the end (**operator-only**).

A host census in `hosts/lib/tests/` fails when a directory under `hosts/` is missing from
any of those named sets or has no creation rule, and it names the set.

### What the deletion touches

- **Emptied in H7.18c:** D5's setup role `ansible/roles/setup/k8s_host_files/`. H7.4 and
  H7.10a have moved its rows by then, and H7.18c turns its absent arms into legacy entries
  and deletes D5's render test for it. Its directory goes in H7.20a.
- **Emptied in H7.19a:** the deployer role, whose directory goes in H7.20a.
- **Removed in H7.19a:** deploy_ui's Python modules and tests, if D6 kept it (H7.11);
  monitor-bridge's gitops checks and its `/var/lib/gitops-deploy` mount, in the first PR;
  `gitops-deploy.timer` as a legacy entry; land.sh's tick step, `land_lib/tick.py`,
  `tools.py:run_tick`, their call sites and `gitops_tick.sh`, with the setup arm's reads
  of the tick's markers.
- **Removed in H7.19b:** setup narrowing except `setup_gates.py`, with all seven
  `narrow_lib` modules D6 hands over; the rest of `land.sh`'s setup arm and its Pi arms,
  `land_reach.py`, the rest of `reach.py` and `land_lib/classify.py`'s setup arm; the
  deployer's service, code, config and polkit rule as legacy entries; `scripts/deploy.sh`
  with `deploy_cli.py` and `deploy_lib/`, `gitops_state.py`,
  `scripts/lib/deployer_park.py`, the deployer readers in `probe_lib`, `runbook_gates_lib`,
  `land_lib`, `scripts/docs/reference/state.py`, renovate_agent and monitor-bridge,
  `gen_gitops_markers.py`, `gen_doc_fragments.py`'s gitops fragment, and the session-health
  arms that read deployer state. The same PR first re-homes `TREE_LOCK`, `k8s_lookup_map`
  and, if a survivor needs them, `role_of`, `role_for` and `is_role_test_path` (*Surviving
  importers of deployer modules*).
- **Removed in H7.20a with the setup tree:** `deploy_cross_role.py` and the rest of
  `deployer_lib/`, after the deployer-import census shows no reader outside it.
- **Kept until H7.20a:** `scripts/deploy_tools/setup_lib/setup_gates.py`. Its guard reader evaluates
  the emptied roles' `end_role` guards and every `<role>_packaged_hosts` guard for H3.2's
  census, the ufw and line-arm censuses, H7.19c's strict setup census and
  `glance_facts.py:setup_glance_lines`. H7.20a deletes it with those censuses, whose subject
  is then gone, and with `glance_facts.py`'s setup-role branch.
- **Removed in H7.20:**
    - the playbooks listed in *Baseline*, `ansible/bring-up.sh`,
      `ansible/prune_backups/` and `ansible/pre_tasks/`, all in H7.20a together with every
      emptied role directory;
    - `ansible.cfg` and `.ansible-lint` at the repo root, with their `!` lines in
      `.gitignore`, and `ansible/requirements.yml`;
    - `ansible/roles/setup/` and `ansible/roles/containers/`;
    - R4's runbook roles' `tasks/`, after H7.16b replaces them;
    - the Ansible-only inventory keys `ansible_connection` and `ansible_user`, and
      `ansible_host` after H7.18d;
    - `ansible-core` from `pyproject.toml` and `uv.lock`;
    - the `ansible-lint` prek hook and `scripts/tests/test_ansible_lint_hook.py`;
    - in H7.20d, the Pi's empty `containers_list` key and `_example.yml`'s copy of it.
- **Converted to scripts in H7.16:** `prune_backups.yml` and `seed_volume_backup.yml` become
  `scripts/backup/` commands with dry-run defaults. `k3s-storage-smoke.yml` becomes a
  `scripts/diagnostics/` command. The volume-revert runbook becomes a task-free
  `scripts/backup/` command.
- **Kept as renderer data:** the keep-list in exit observation 1. Renaming the `ansible/`
  tree is not this slice's work.
    - The Pi's `compose_services` stays in `ansible/inventory/host_vars/daniel-pi.yml`.
      `scripts/lib/render_context.py` reads it for the Pi's host render and for the three
      cluster templates that name the Pi's services.
    - H1.9's `cluster/services.yml` holds the cluster's list, so after H7.20d no
      `host_vars` file carries `containers_list`.

### Tree diff

```diff
+ hosts/components/apt-{docker,azlux,github-cli}/              (the apt sources and keys)
+ hosts/packages/homelab-tools/
+ hosts/lib/{host_lib,kuma_push,ci_verdict}.py                (moved from roles/setup)
+ hosts/lib/homelab-reconcile                                  (+ activation, ufw, libvirt,
                                                                  users and line arms)
+ hosts/components/<one per moved subject>/                    (component.yml, CLAUDE.md
                                                                  where a timer changes state)
+ hosts/components/checkout-sync/
+ hosts/components/docker-engine/
+ hosts/components/pi-peer-backup-target/
  hosts/bootstrap-host.sh                                      (steps 3 and 4, --until,
                                                                  --local-build)
  hosts/{daniel-pi,daniel-server,daniel-box}/package.yml       (components, line-arm
                                                                  declarations, legacy
                                                                  entries)
  hosts/components/pi-compose/                                 (H5's unit: ExecStartPre
                                                                  creates the external
                                                                  networks, H7.5)
+ scripts/lib/longhorn_lib/                                    (unless H2 moved it)
+ scripts/backup/{prune_backups,seed_volume_backup,volume_revert}.py
+ scripts/diagnostics/storage_smoke.py
+ docs/adr/<next free>-one-package-per-host.md
- ansible/roles/setup/                                         (the 18 roles at 70591c307,
                                                                  plus H3's homelab_builder
                                                                  and D5's k8s_host_files)
- ansible/roles/containers/
- ansible/roles/k8s/{volume-revert,longhorn-api}/tasks/
- ansible/{initial_setup,bootstrap,k3s-bringup,deploy,preflight}.yml
- ansible/{prune_backups,seed_volume_backup,k3s-storage-smoke,requirements}.yml
- ansible/bring-up.sh  ansible/prune_backups/  ansible/pre_tasks/
- ansible.cfg  .ansible-lint
- scripts/deploy.sh  scripts/deploy_tools/{deploy_cli,narrow_setup,setup_routing,
    setup_role_chains,gitops_state}.py  scripts/deploy_tools/gitops_tick.sh
- scripts/deploy_tools/setup_lib/setup_gates.py                (H7.20a, after its last
                                                                  census reader)
- scripts/deploy_tools/deploy_lib/
- scripts/deploy_tools/narrow_lib/                             (the seven setup modules
                                                                  D6 hands over)
- scripts/deploy_tools/reach.py  scripts/deploy_tools/land_lib/land_reach.py
- scripts/deploy_tools/land_lib/tick.py                        (H7.19a, with _step_tick)
- scripts/dev/gen_gitops_markers.py
- ansible/roles/k8s/monitor-bridge/files/{gitops_hold,gitops_markers,gitops_ledger}.py
- ansible/roles/k8s/monitor-bridge/files/checks/gitops.py      (H7.19a's first PR, with
                                                                  the gitops-state mount)
- scripts/lib/deployer_park.py
+ scripts/lib/{tree_lock,k8s_lookups}.py                      (moved out of deployer_lib,
                                                                  H7.19b)
+ scripts/lib/role_paths.py                                    (only if a land_lib survivor
                                                                  calls role_of, H7.19b)
  scripts/deploy_tools/land_lib/                               (setup and Pi arms and the
                                                                  deployer-state reads
                                                                  removed)
  scripts/deploy_tools/await_ci.py                             (imports hosts/lib, H7.2e)
  scripts/secrets_mgmt/consumers.py                            (imports scripts/lib)
  ansible/inventory/group_vars/all.yml                         (+ ci_required_contexts,
                                                                  H7.2e)
  ansible/filter_plugins/claude_agents.py                      (deleted, or its ansible
                                                                  import removed, H7.17)
  ansible/inventory/hosts.ini                                  (host names only)
  ansible/inventory/host_vars/{daniel-pi,_example}.yml         (- containers_list, H7.20d;
                                                                  compose_services stays)
  ansible/roles/k8s/{artifacts,monitor-bridge,observability}/templates/
                                                               (Pi reads: compose_services
                                                                  only, H7.20d)
  scripts/lib/{render_context,ansible_inventory}.py            (Pi reads: compose_services
                                                                  only, H7.20d)
  pyproject.toml, uv.lock, prek.toml, .gitignore
```

## Steps, as PRs

Each per-role PR follows H3's handoff rule. It adds the component, deletes the role's tasks
for that component, lists the Ansible-era paths, units, crons and ufw rules as legacy
entries, and retargets that role's tests in `ansible/tests/setup/` at the rendered
component. It `git mv`s every template, file and default the component renders. The PR
that moves a role's last component empties the role, as *Roles are emptied before H7.20a*
describes, and never edits a bring-up playbook. H7.20a deletes the role directories.

**Canary order.** A subject that reaches every host lands as one PR per host, in the order
daniel-pi, daniel-server, daniel-box. Each host PR waits until the previous host shows the
version in `homelab_host_applied_info` and its live-state check passes. The first two host
PRs add their host to `<role>_packaged_hosts`, and the last one deletes the subject's tasks
(*Moving one host at a time*).

**daniel-box while the tick runs.** Until H7.19a the deployer applies setup-plane changes on
daniel-box. A daniel-box PR deletes or guards the role's tasks, or empties the role, and
never touches `initial_setup.yml`, `k3s-bringup.yml` or `bootstrap.yml`, so the range never
parks the tick. The tick then applies the changed role on daniel-box, which writes nothing
the package owns. *Rollback* gives the order for a revert.

**Clearing `manual_plane` lines.** A PR whose changed role the tick rejects records a
`manual_plane` line (Baseline): every PR that changes `optimize_pi` or `k3s`, and H7.2e,
whose `host_lib.py` move routes to `k3s`. The package replaces the deleted tasks, so no
apply is owed. Once the host's `homelab_host_applied_info` shows the PR's SHA or a
descendant, the PR's author clears it with `uv run python
scripts/deploy_tools/gitops_state.py clear-owed manual_plane <role> --applied <tags>`, the
form the `land-after-merge` skill gives, naming the tags the line lists. When the line also
holds tags from another session's change, that session applies and clears its own first.
This step lasts until H7.19b deletes the ledger.

**Before G2, every PR that moves or empties a role's `CLAUDE.md`** runs `uv run python
scripts/dev/fact_status.py forget '<old key>'` for each section key it removes and `verify`
for each new key, then commits `docs/facts.lock`. 57 rows key on setup-role docs (Baseline).

### The re-sliced PRs

The 65 planned PRs merge into the 37 below. The re-slicing changes no design. Each re-sliced
PR carries the full text of every old step it absorbs, from the collapsed list under *The old
step list*, with that step's verification, rollback note, operator-only gate and share of the
exit criterion. *Design*, *Verification*, *Rollback*, *Risks*, *Tests and docs* and the H3 to
H6 plans cite the old IDs, such as H7.2e or H7.19a's fifth PR, and the *Old steps* column
resolves each one to its re-sliced PR.

Four rules decide where a merge stops:

- **Canary hosts stay apart.** A per-host PR waits until the previous host reads the version
  in `homelab_host_applied_info` and passes its live-state check, so the Pi, daniel-server
  and daniel-box PRs of one subject never merge. Several subjects of one host do merge,
  because `<role>_packaged_hosts` maps each subject to its hosts separately.
- **A live wait stays a PR boundary.** H7.19a's third and fourth PRs precede its fifth. The
  fifth's daniel-box version precedes H7.19b's, and H7.19b's version precedes H7.20a.
- **Another repository is another PR.** H7.12a lands in `DanielH2018/dotfiles` first.
- **An operator drill that takes hosts down runs alone.** H7.8's `upsmon -c fsd` drill
  powers off both cluster nodes.

*Written ahead* says whether an agent can write the PR before its predecessors merge and
rebase it later (**yes**), or must wait for a predecessor's merged code or live result
(**no**, with the reason). A *live* entry in *Waits on* waits until that PR is deployed and
its live-state check passes, not only merged.

**Narrowed edges.** H7's slice edges are H2, H5, H6 and R4. Each re-sliced PR names the part
of a predecessor it reads. The edges that H7 reached only through those four become explicit:
H3 and H4 through H5 and H6, and D1, D5 and D6 through R4. Each host's package bounds its
PRs: H3 created `homelab-daniel-pi`, H4 creates `homelab-daniel-server`, and H6.2 creates
`homelab-daniel-box`. Nothing waits on H7, so no narrowing can form a cycle. Two cited H2 IDs
are absent from H2's step list. H2.6 appears only as a condition that H7.2e handles either
way, so it adds no edge. H2.10 names the S3 folder in old H7.13, so H7.34 waits on all of H2.

| PR | Old steps | Written ahead? | Waits on |
|---|---|---|---|
| H7.21 Activation, drift and the ufw arm | H7.2a, H7.2b | yes; B is measured at H3.2b's merge commit | H3.2b, H4.3b2 |
| H7.22 The libvirt, users and line arms | H7.2c, H7.2d | yes | H7.21 |
| H7.23 Shared modules leave the setup tree; the setup census | H7.2e, H7.2f | yes | H3's builder, D1's `promote.py`, D5.1h |
| H7.24a The apt-source components, and nfpm in `homelab-tools` | H7.1, first half | yes | H3, H4, H6.2 |
| H7.24b `sops_setup` retires | H7.1, second half | no: it waits for the callers to reach the packaged `sops` (item 4) | H7.24a, H7.42 (old step H7.15a) |
| H7.25 Pi: `optimize_pi`, `config_files` and the Pi rows of `k8s_host_files` | H7.3, H7.4 | yes; the owner measurement is a read-only call on the live Pi | H7.22, H3.6, D5.1h |
| H7.26 Pi: `docker_install` | H7.5 | yes, against H5's Compose unit | H7.22, H5.12 |
| H7.27 Pi: `initial_setup`, part 1 | H7.6a, H7.6d, H7.6g | yes | H7.22 |
| H7.28 daniel-server: `initial_setup`, part 1 | H7.6b, H7.6e, H7.6h | yes, with H7.27 | H7.27 live, H4 |
| H7.29 daniel-box: `initial_setup`, part 1 | H7.6c, H7.6f, H7.6i | yes, with H7.27 | H7.28 live, H6.2 |
| H7.30 Pi: `initial_setup`, part 2 | H7.7a, H7.7d, H7.7g | yes | H7.25, H7.26 |
| H7.31 daniel-server: `initial_setup`, part 2 | H7.7b, H7.7e, H7.7h | yes, with H7.30 | H7.30 live, H4 |
| H7.32 daniel-box: `initial_setup`, part 2 | H7.7c, H7.7f, H7.7i | yes, with H7.30 | H7.31 live, H6.2 |
| H7.33 daniel-box: NUT secondary | H7.8 | yes | H4, H6.2 |
| H7.34 daniel-server: the `k3s` agent half and `hypervisor` | H7.9a, H7.13 | yes | H7.31, H6.1b2, H6.5b, H2 |
| H7.35 daniel-box: the `k3s` host half | H7.9b | no: H6.4a, H6.4b and H2's `setup/k3s` PRs edit the same task files, so it starts once they merge | H7.32, H6.4a, H6.4b, H2, D5's media-volume row |
| H7.36 daniel-box: the checkout timers | H7.10a, H7.10b, H7.10c | yes | H6.2, D5's docs row |
| H7.37 daniel-box: Renovate and `fake_remux` | H7.11 | yes; D6.15's review picks the deploy_ui branch | H6.2, D6 |
| H7.38 Per-user tooling in `DanielH2018/dotfiles` | H7.12a | yes | none |
| H7.39 The agent users and daniel-server's `claude_code` | H7.12b, H7.12c's daniel-server PR | yes | H7.38, H7.22, H4, H6.2, the H6 PR that lands the window test |
| H7.40 daniel-box: `claude_code` | H7.12c's daniel-box PR | yes, with H7.39 | H7.39 live, D5's artifacts row |
| H7.41a daniel-box: the builder's tooling | H7.14, first half | yes | H3, H6.2, H7.24a |
| H7.41b daniel-box: the builder hosts itself | H7.14, second half | no: it waits for H7.41a live on daniel-box | H7.41a live, H3, H6.2, H7.24a |
| H7.42 `bootstrap-host.sh`, complete | H7.15a | yes | H7.24a, H6.1b, H6.2 |
| H7.43 The Pi runbook and the Pi rebuild | H7.15b | the runbook yes; the rebuild no, because it needs every Pi PR live | H7.25, H7.26, H7.27, H7.30, H7.42, H5, all live |
| H7.44 The cluster-node procedure, adding a host and release upgrades | H7.15c, H7.15d, H7.15e | yes; the guest drill needs H6 live | H7.42, R4 PR 7b, H6 |
| H7.45 Scripts for the operator playbooks | H7.16a, H7.16b | yes | R4 |
| H7.46 The last Ansible imports | H7.17 | yes | R4, D6.13 |
| H7.47 Closing the readers | H7.18a, H7.18b, H7.18d | yes | H7.36, H7.40, H5 |
| H7.48 Absent arms become legacy entries; `k8s_host_files` empties | H7.18c | no: its list of record is H7.23's report taken after every row has moved | H7.25, H7.35, H7.36, H7.40, H2, D5 |
| H7.49 monitor-bridge's gitops checks go, and land.sh stops running the tick | H7.19a's first and third PRs | yes | H7.41, D6 |
| H7.50 deploy_ui retires | H7.19a's second PR | yes | H7.37, H7.41, D6 |
| H7.51 `homelab-checkout-sync` beside the deployer | H7.19a's fourth PR | yes | H7.41, H7.47, D6, the H6 PR that lands the window test |
| H7.52 The deployer's timer retires | H7.19a's fifth PR | no: it deletes H6's seven deployer-coupled items, so it starts once they merge | H7.49, H7.51 live, H7.24a, H7.24b, H7.25, H7.29, H7.32, H7.33, H7.35, H7.36, H7.37, H7.40, H7.41, H7.48, H6 |
| H7.53 Re-home and delete the deployer; the setup census turns strict | H7.19b, H7.19c | the re-home half yes | H7.52 live on daniel-box, H7.24a to H7.41, H7.24b, H7.48, H7.50 |
| H7.54 The test env leaves ansible-core | H7.19d | no: its parity proof runs over the suite as H7.48 and H7.52 leave it | H7.46, H7.48, H7.52 |
| H7.55 Delete the playbooks and the role directories | H7.20a | no: it deletes what H7.53 leaves | H7.53 live on daniel-box, H7.43, H7.44, H7.45 |
| H7.56 ansible-core leaves, and so does the Pi's `containers_list` key | H7.20b, H7.20d | no: its two census greps run at its base | H7.54, H7.55, H5 |
| H7.57 The ADR | H7.20c | yes | H7.55 |

### What each re-sliced PR carries

Every per-role PR keeps the rules above the table: H3's handoff rule, the emptied-role stub,
the `manual_plane` clearing, the facts-lock `forget` and `verify`, and *Rollback*'s
**operator-only** hand re-apply and `--clear-hold`. The notes name only what a merge moves.

1. **H7.21** (about 580 lines, 270 of them reconcile source). The activation step, the drift
   metric and the ufw arm all extend reconcile. No host uses an arm until a later PR declares
   state for it, so nothing needs a live check between them. The reconcile-size census and
   its ceiling land here, ahead of any arm's source. It carries the activation, ufw-arm,
   ufw two-writer, per-host-guard and reconcile-ceiling proofs, and exit observation 7's
   census.
2. **H7.22** (about 600 lines, 300 of them reconcile source). The libvirt, users and line
   arms are the remaining arms, and the line arm's census extends the two-writer census that
   H7.21 extends. It carries the libvirt-arm, users-arm, way-out-of-a-hold, line-arm and
   line-arm two-writer proofs.
3. **H7.23.** Both steps change the tree and no host state, and both act on the setup tree's
   layout: H7.2e moves the shared modules out, and H7.2f's census reads what stays. It
   carries H7.2e's four derivation greps, the key grep, the routing test and the empty
   cluster `diff -r`, the moved-CI-key, shared-module-routing and setup-census proofs, the
   `manual_plane` line for `k3s` that the `host_lib.py` move records, and H7.2e's revert note.
4. **H7.24 splits in two, ruled on 2026-10-24.** H7.24a moves the apt sources and keys into
   per-host components and adds nfpm to `homelab-tools`. H7.24b retires `sops_setup`.
    - **The old step's premise about `sops` was false.** It reads the packaged `sops` as the
      binary callers reach through `PATH`. `hosts/packages/homelab-tools/package.yml:2`
      installs each tool at `/usr/lib/homelab/bin/<tool>`, "never on PATH", and only
      `hosts/lib/homelab-secrets:68` names that absolute path. `command -v sops` prints
      `/usr/local/bin/sops` on all three hosts. Retiring that binary now would remove `sops`
      from the deployer, from `deploy.sh`'s `community.sops` lookups, from the operator's edits
      and from the rotation cron.
    - **H7.24a** writes the components `apt-docker`, `apt-azlux` and `apt-github-cli`, with the
      sources and keys at the paths the roles wrote, so dpkg takes them over. It deletes the
      moved tasks of `docker_install`, `github_cli` and `optimize_pi`. daniel-pi's package lists
      the first two, and the packages of daniel-box and daniel-server list the third.
      daniel-box also depends on `homelab-tools` for nfpm. daniel-server's package retires the
      hand-installed `github-cli.list`, which names the repository that `github-cli.sources`
      now ships. `sops_setup` stays untouched.
    - **The operator ruled on 2026-10-24 that each host ships only the sources it uses.** An
      earlier draft shipped all three from one shared `homelab-repos` package, which gave every
      host two indexes it never reads and tied each updater run to three third-party hosts.
      The hosts that use each source are: Docker and Azlux on daniel-pi, which alone runs
      `docker_install` and `optimize_pi`; the GitHub CLI on daniel-box, which runs `github_cli`,
      and on daniel-server, where `dpkg-query -W gh` reports 2.103.2 from a hand install. No
      source is common to all three hosts, so there is no `homelab-repos` package.
      `scripts/renderer/tests/test_apt_source_components.py` fails a host package that ships a
      source its host does not use. A fresh host that runs `initial_setup.yml` before its
      packages are installed lacks these sources; the operator accepted that gap until H7.42.
    - **H7.24b** retires `sops_setup` and lists `/usr/local/bin/sops` as a legacy entry in all
      three host packages. It waits on H7.15a, which is H7.42 in the table. Its body quotes
      `command -v sops` on each host. It also carries the age-key decision in
      `docs/break-glass.md`. The removal of daniel-server's user-home age key waits for the
      operator's confirmation that nothing reads it (**operator-only**).
5. **H7.25.** Both steps move `optimize_pi`, and H7.4 empties the role that H7.3 starts.
   zram's keys and the Pi's other sysctl keys share one line arm on `/etc/sysctl.conf`. It
   carries H7.3's owner measurement and its divert contingency, H7.4's textfile-directory
   proof and `stat` read, and the `manual_plane` line for `optimize_pi`. Its rollback keeps
   H7.4's exception: the operator re-applies `k8s_host_files` ON daniel-box
   (**operator-only**).
6. **H7.26** stays alone, because it waits on H5's late PRs and the held engine (#1961) is
   its own risk. It carries the Compose measurement, the Compose-networks and Docker-engine
   proofs, and the six networks the operator removes or keeps. The operator rules on
   Open question 8, a windowed engine upgrade, in its review (**operator-only**).
7. **H7.27 to H7.29.** One host's apt, sshd and sysctl subjects land in one PR. Part 1 and
   part 2 stay apart, because sshd and ufw are the two subjects that can lock a host out.
   Each host's live check then proves one of them before the other lands, and a revert drops
   only one. Each carries the live-state probe, the host's `/etc/sysctl.conf` keys marked as
   declared or as host history for the operator, the config-validity proof and
   `docs/host-baseline-record.md`'s sshd sections. H7.29 deletes the subjects' tasks.
8. **H7.30 to H7.32.** One host's fail2ban group, maintenance timers and ufw land in one PR.
   Each carries the rules listed by exact spec and comment, the `Status: active` and rule-
   order reads, and the ufw rollback: removing a `keep` rule or disabling ufw is the
   operator's, at the host's console (**operator-only**). H7.30 waits on H7.25 and H7.26,
   because the Pi's ufw lists their rules by spec. H7.32 deletes `Enable UFW firewall`.
9. **H7.33** stays alone. Its `upsmon -c fsd` drill powers off both cluster nodes in a window
   the operator schedules, with the operator at both consoles and a clear Longhorn gate
   (**operator-only**).
10. **H7.34.** Both steps are daniel-server's last roles, and exit observation 3 reads both:
    the hypervisor's drill takes its token from the agent's installer env file. It carries
    the agent ufw rules declared `keep`, the `manual_plane` line for `k3s`, the S3 folder the
    drill reads after H2, and the drill run once by hand, then from its timer.
11. **H7.35** stays alone, because it edits the task files that H6.4a, H6.4b and H2 edit. It
    carries the netplan `manual:` activation, the media-volume `tmpfiles.d` lines and the
    `manual_plane` line for `k3s`.
12. **H7.36.** All three steps move daniel-box timers that read the primary checkout, and
    each keeps its `## Autonomous-role contract` in its component. It carries the docs site
    directory's `tmpfiles.d` line and the docs role's `CLAUDE.md` edit.
13. **H7.37** stays alone. Its three roles hold 61 files at `70591c307`.
14. **H7.38** stays alone. It lands in `DanielH2018/dotfiles` under that repo's `bin/gate`,
    before H7.39 deletes the tasks.
15. **H7.39.** The agent users and the units that run as them come up together on the first
    host. It carries the `meta: end_role` guard naming daniel-server. Old H7.12c names the
    `homelab-*` rename, the four window-test entries and the `claude-land@` callers once for
    both host PRs. Old H7.12c's text decides which of H7.39 and H7.40 carries them, and that
    PR's body quotes the sentence it followed. Restarting a host's `claude-rc.service` drops the phone's sessions, so H7.39 and
    H7.40 each land in a window the operator picks (**operator-only**).
16. **H7.40** stays alone as the second canary host. It carries the artifacts directories'
    `tmpfiles.d` lines and empties `claude_code`.
17. **H7.41 splits in two, ruled on 2026-10-24.** Wherever another row or entry names H7.41
    as a dependency, it means H7.41b. H7.41a is tooling and spec amendments and moves no
    builder unit or task. H7.41b is the builder move, and it waits for H7.41a live on
    daniel-box. The pair carries the fingerprint-file proof, the break-glass procedure
    for a broken builder, and the builder's three tools and its alert unit, which H7.14
    names (nfpm, uv, the interpreter and `homelab-alert@.service`). It waits on H7.24a
    because nfpm arrives in `homelab-tools` with H7.24a. A builder that rebuilds its own
    package can publish a broken `homelab-daniel-box`, so H7.41b's PR body states the
    recovery: `apt-get install homelab-daniel-box=<previous>`, or the by-hand build of one
    package (*Rollback*).
    - **H7.41a** carries rulings 2 and 3 below as code, with their tests, and rulings 1, 4 and
      5 as spec text. It needs no SOPS key.
    - **H7.41b** carries the builder move and the five facts listed after the rulings.
    - **Ruling 1, 2026-10-24: the `users:` arm declares uid only.**
      `scripts/renderer/renderer_lib/host_arms.py:USER_KEYS` is `{name, uid, home, shell,
      comment}` and `hosts/lib/reconcile_lib/users.py` has no gid. The package declares uid
      995 and 994. The install adopts the live accounts by uid, and a rebuilt host's groups
      get gid equal to uid. A grep for the live gids 983 and 982 under `ansible/`, `hosts/`
      and `scripts/` finds no reader of them.
    - **Ruling 2, 2026-10-24: uv's archive member is one directory down.** uv 0.13.4 ships
      tarballs only, with the binary at `uv-x86_64-unknown-linux-gnu/uv` and
      `uv-aarch64-unknown-linux-gnu/uv`. The member grammar of both twins
      (`ansible/roles/setup/homelab_builder/files/homelab_tool_fetch.py:_MEMBER` and
      `scripts/renderer/renderer_lib/host_tools.py:_MEMBER`) accepts exactly one directory
      segment. `homelab-tools` pins uv 0.13.4, the version of `initial_setup_uv_version`.
    - **Ruling 3, 2026-10-24: reconcile hands a shadowed unit to the package.** The builder's
      units are already named `homelab-*`, so the package ships the names of the Ansible
      copies in `/etc/systemd/system`, and those copies win over `/usr/lib/systemd/system`.
      `h3-pi-pilot.md`'s assumption that package units are renamed does not hold here. A
      `{path:}` legacy entry under `/etc/systemd/system/`, either a unit file or a `*.wants/`
      link, whose basename the package ships as a unit gets this handoff: reconcile removes
      the file, runs one `daemon-reload` after all such removals, and restarts each packaged
      unit whose file differed, in step 3's order. A `{unit:}` entry that names a packaged
      unit takes the same path and is never counted as running drift. A `{path:}` entry
      under that directory with no packaged unit of that name stays in place and counts as
      drift. A wants link carries no bytes, so removing one restarts nothing. H7.41b
      checks that the package's own enable link survives the removal.
    - **Ruling 4, 2026-10-24: credstore paths.** The builder unit writes
      `LoadCredential=github_token:/run/credstore/homelab_builder_github_token` and the alert
      unit writes `LoadCredential=discord_webhook:/run/credstore/gitops_deploy_discord_webhook`.
      The builder reads `$CREDENTIALS_DIRECTORY/github_token`, so `config.env` holds no
      secret. `hosts/lib/homelab-secrets` writes `/run/credstore/<sops key>`.
    - **Ruling 5, 2026-10-24: `UV_PYTHON_DOWNLOADS` is set on `ExecStart=` only.** The
      interpreter-install `ExecStartPre=` does not inherit `UV_PYTHON_DOWNLOADS=never`. The
      variable moves onto `ExecStart=` through `env`.
    - **Facts for H7.41b, measured on 2026-10-24 and not acted on in H7.41a:**
        - `ansible/roles/setup/homelab_builder/files/host_package_builder.py` is 596 of 600
          lines. The token change adds 4 lines at most, or moves code into a sibling module.
        - Both units need `StateDirectoryMode=0711`.
        - The alert can use `curl --variable 'webhook@%d/...' --expand-url '{{webhook:trim}}'`
          with no shell wrapper.
        - The `UFW_SPEC` grammar rejects quotes, so the commented ufw rules need legacy
          entries with `replaced_by`.
        - H7.41b needs `homelab_builder_github_token` and `gitops_deploy_discord_webhook` in
          daniel-box's host SOPS file, through an operator sync
          (**operator-only**: `host_secrets.py sync daniel-box`).
18. **H7.42** stays alone. It carries the bootstrap, offline and bootstrap-pins proofs.
19. **H7.43** stays alone, because the operator's rebuild is exit observation 2. The operator
    rebuilds at the Pi's console and keeps the old SD card as the rollback
    (**operator-only**). The cluster's wg-easy on 51820 is proven first.
20. **H7.44.** All three steps are procedures built on H7.42's script, in
    `docs/break-glass.md` and `docs/host-packages.md`, and H7.15d's host census is their one
    test. It carries R4 PR 7b's kind drill run on this PR, the guest drill with its blast
    radius (**operator-only**, exit observation 4), and the D6 case that Open question 14
    asks for. It merges before H7.55.
21. **H7.45.** Both steps replace an operator playbook or runbook role with a script. The
    operator drills the snapshot command once (**operator-only**).
22. **H7.46** stays alone. It carries the renders-without-ansible-core proof and lands the
    deletion census with its allowlist.
23. **H7.47.** Each step points a reader of the setup tree at the packages: the docs
    generators, the contract census, and the two scripts that name `ansible_connection`. It
    carries the generators' named members, the contract census and the empty cluster
    `diff -r`.
24. **H7.48** stays alone. It carries the retired-crons census and empties `k8s_host_files`.
25. **H7.49.** Both PRs remove a consumer of the running tick outside the deployer's role,
    and both must precede H7.52. The deployer runs unwatched from this merge until H7.52's.
    It carries the no-gitops-hostPath and land.sh-without-a-tick proofs. Its revert restores
    the checks, which need `/var/lib/gitops-deploy`.
26. **H7.50** stays alone, a deletion of about 2,300 lines. Its revert needs the operator's
    `--tags deploy_ui` re-apply ON daniel-box (**operator-only**).
27. **H7.51** stays alone, because it must be live and fast-forwarding before H7.52 removes
    the timer. It carries the checkout-sync proofs and the window-test exemption.
28. **H7.52** stays alone. It merges only while `hold_sha` is empty and `owed.jsonl` holds no
    line. It carries the proof for H6's deployer-coupled checks. Its revert needs the
    operator's `--tags gitops_deploy` re-apply ON daniel-box (**operator-only**).
29. **H7.53.** H7.19b deletes the deployer's code once its timer is gone, and H7.19c then fails
    the census on any setup task left. H7.19b may still split in two, the re-home first. It
    carries the deployer-import census and the legacy entries, and names
    `/var/lib/gitops-deploy/` as the operator's cleanup. Exit observation 5 holds once its
    version is live on daniel-box.
30. **H7.54** stays alone. Its parity proof covers 361 modules.
31. **H7.55** stays alone. It merges only after daniel-box shows H7.53's version and lists no
    `gitops-deploy` unit file. It carries the two-part proof, exit observation 1's listings
    and exit observation 7's census at its head.
32. **H7.56.** Both steps follow H7.55 and remove the last Ansible inputs: ansible-core with its
    config and hook, and the Pi's empty key. Neither moves a rendered byte. It carries exit
    observation 1's two counts, the Pi service-list proof and the two render pairs.
33. **H7.57** stays alone, so the deletions land while the operator reviews it. Approving the
    ADR is **operator-only** (exit observation 6), and so is deciding whether ADR-0001,
    ADR-0011 and ADR-0017 need an amendment.

### The old step list

<details markdown="1">
<summary>The 20 numbered items and 65 PRs that the re-sliced list replaces</summary>

1. **H7.1 `homelab-repos`, and `sops_setup` retires.**
    - Move the Docker, GitHub CLI and Log2Ram sources and keyrings out of their roles.
    - `homelab-tools` comes from H3, with sops and chezmoi (C5). H7.1 adds nfpm to it for
      daniel-box, if H3 has not.
    - `homelab-secrets` already reads the packaged `sops` (H4). Delete `sops_setup`'s
      install tasks. Its key generation moves to bootstrap step 3 in H7.15a. List
      `/usr/local/bin/sops` as a legacy entry in all three host packages, because that
      directory comes first on `PATH`, and a stale binary there would shadow every Renovate
      bump of the package pin. The PR body quotes `command -v sops` on each host reading
      the packaged path.
    - Record the age-key decision in `docs/break-glass.md`. daniel-box keeps the operator's
      `~/.config/sops/age/keys.txt`, because the operator's `sops` edits, the `add-secret`
      skill and the secret rotation cron (`when: has_gitops`, as `sys_user`) read it.
      daniel-server's user-home copy, which H4.2 duplicated, is removed only after the
      operator confirms that nothing on daniel-server reads it (**operator-only**).
2. **H7.2 Foundations.** Six PRs. No host uses an arm until a later PR declares state for it.
    - **H7.2a** (about 280 lines, 120 of them reconcile source) The `activate:`
      field, the validation commands, the pending metrics and
      `/run/homelab/validate-failed`. It also adds `homelab_host_reconcile_drift{host,arm}`
      and the drift rule of *Which outcomes hold the host*, and extends H3's
      `host_packages` check to name a non-zero drift count in its message without going
      down.
    - **H7.2b** (about 300 lines, 150 of them reconcile source) The ufw arm,
      with its floor, its order check and its default policies, and
      the ufw extension of H3.2's two-writer census. The census reads
      `<role>_packaged_hosts` guards through H4.3b2's helper, which this PR extends to a
      task-level and an import-level `when:`.
    - **H7.2c** (about 200 lines, 100 of them reconcile source) The libvirt arm, for
      networks and nwfilters.
    - **H7.2d** (about 400 lines, 200 of them reconcile source) The users arm and the
      line arm, with its line and block forms and its
      conffile rule, and the line-arm extension of H3.2's two-writer census.
    - **H7.2e** The shared modules leave the setup tree, as *Design* describes, with
      `ci_walk_candidates`, `ci_required_contexts` and `ci_ancestor_walk_max`, and
      `await_ci.py`, `landing_blockers.py`, D1's `promote.py decide` and H3's builder
      repointed. The PR body quotes the four derivation greps and the key grep, the
      routing test for `hosts/lib/host_lib.py`, the passing
      `test_gitops_deploy_ship_list.py` and `test_ci_contexts_match_workflows.py`, and the
      empty `diff -r` of the cluster render. The fourth grep's output is the list of
      record for H7.19b.
    - **H7.2f** The setup census, in report mode. It lives in
      `scripts/deploy_tools/setup_lib/setup_census.py` (`--host`, `--strict`), and the
      test is `ansible/tests/setup/test_setup_census.py`. The census does not evaluate a
      task's `when:`, so a `<role>_packaged_hosts` guard cannot hide an item: the item stays
      in the report, marked `[per-host guard: ...]`, until the host's package carries it.
    - H7.2a also adds the reconcile-size census with its ceiling constant, and measures B
      at H3.2b's merge commit (*Reconcile's line ceiling*).
3. **H7.3 Pi: `optimize_pi`, part 1.** zram, Log2Ram and the watchdog, each with `activate:
   reboot`. The PR first measures the owners of the Pi paths, `/etc/sysctl.conf` among
   them, in one batched read-only call (`dpkg -S` and `dpkg-query -W -f='${Conffiles}'`),
   and quotes the output. zram's two sysctl keys move to the line arm here. Each path then
   takes the mechanism *In-place edits* assigns: a conffile or an unowned file takes the
   line arm, and `/usr/local/bin/log2ram` takes the line arm or the homelab copy. If a path
   needs a divert after all, the divert lifecycle PR lands first.
4. **H7.4 Pi: `optimize_pi`, part 2, and `k8s_host_files`' Pi rows.** earlyoom, the
   apt-timer drop-ins, the node exporter unit, the resolver and hosts files, `config_files`,
   and the `pi-peer-backup-target` component. `optimize_pi` and `config_files` are then
   emptied, and their playbook lines stay until H7.20a.
    - **The textfile directory moves with the node exporter.** H3.6 made an `optimize_pi`
      task under `node-exporter-host` the only creator of `/var/lib/node-exporter-textfile`
      on the Pi, with M3's owner contract: owner root, group `sys_user`, mode 2775. The
      updater writes `homelab_host.prom` there, and it exits non-zero without creating the
      directory when it is missing. The package therefore ships a `tmpfiles.d` file with
      one `d` line for the directory at that owner and mode. The node exporter unit runs
      `systemd-tmpfiles --create` on that file in `ExecStartPre=` with the `+` prefix, as
      H7.10a's docs-refresh unit does, and `systemd-tmpfiles-setup.service` applies it at
      boot. The same PR deletes the H3.6 task, so H3's two-writer census still finds one
      writer, now `homelab-daniel-pi`, and that census keeps it as its named member.
    - The PR body quotes, from one batched read-only call on the Pi, `stat -c '%U %G %a'`
      on the directory printing `root <sys_user> 2775` after the updater installs the
      version, and the Prometheus read of `homelab_host_applied_info{host="daniel-pi"}` at
      the PR's version.
    - The component ships pi-peer-backup's forced-command wrapper as
      `/usr/local/bin/pi-peer-backup-shell`. It keeps the authorized key through the line
      arm, anchored on `pi-peer-backup-cronjob`, with the `restrict,command=` options the
      Ansible task sets.
    - The same file holds the operator's own login key, and sshd's `StrictModes` rejects
      every key in a file with the wrong owner or loose modes. So the arm keeps the file's
      owner and mode when it rewrites it. When the file is missing, it creates `~/.ssh` as
      0700 and the file as 0600, both owned by the Pi user.
    - Two-writer check: the same PR deletes both `delegate_to: daniel-pi` tasks from
      `k8s_host_files`. That is a setup-plane change, which the tick applies on daniel-box.
      If H7.2f's report still lists D5's crowdsec probe timer in `k8s_host_files`, it moves
      here as well.
    - The authorized key is the tracked public half, so the package carries no secret. A
      rotated `pi_peer_backup_ssh_key` then reaches the Pi as a new `homelab-daniel-pi`
      version. The PR repoints the redeploy that D5 names for that key, the
      `k8s_host_files` apply, at the Pi package.
5. **H7.5 Pi: `docker_install`.** The Docker source moves to the `apt-docker` component. The pins,
   `homelab-docker-engine.service`, `daemon.json`, the Go drop-ins, and the containerd
   config block through the line arm move to the package, all `manual`. The hold stays.
    - **The networks stay external.** The PR first measures, in the CI container, what
      `docker compose up` does with a project-managed network whose name an unlabelled
      network already holds, and the fixture records the result. Moving the networks into
      the project would have to recreate them, which stops every attached container on the
      Pi, wg-easy included. So `proxy` and `lifecycle` stay `external: true`, and H5's
      `homelab-compose.service` gains an `ExecStartPre=` that creates each external network
      the project names when it is missing. A fresh Pi gets them on first start, and the
      live Pi keeps its own. The `Create Docker networks` task goes, and the PR body lists
      the other six networks that loop created, for the operator to remove or keep.
    - A test asserts that the networks the `ExecStartPre=` creates equal the external
      networks the rendered project names. Its red fixture names a network the
      `ExecStartPre=` omits.
6. **H7.6 `initial_setup`, part 1.** Three subjects, each as three host PRs in canary order:
    - **H7.6a–c** apt and unattended-upgrades;
    - **H7.6d–f** sshd, through `10-homelab.conf` and the `Match` file, with
      `reload:ssh.service`;
    - **H7.6g–i** sysctl, journald and `limits.d`, with `/etc/ufw/sysctl.conf`,
      `login.defs` and each `initial_setup` sysctl key in `/etc/sysctl.conf` through the
      line arm (*In-place edits*). Each PR body quotes that host's uncommented
      `/etc/sysctl.conf` keys, read-only, and marks each one declared by the arm or left as
      host history for the operator. The live-state check reads `sysctl -n` for every
      declared key.
7. **H7.7 `initial_setup`, part 2.** Three subjects, each as three host PRs in canary order:
    - **H7.7a–c** fail2ban, rkhunter, AIDE, postfix and accounting;
    - **H7.7d–f** the maintenance timers: `homelab-reboot.timer`, the firmware timer,
      `Weekly apt autoremove`, `Weekly dpkg purge orphaned configs` and, on the Pi,
      `Clean unused Docker images`, each with explicit `Environment=`;
    - **H7.7g–i** ufw through its arm. Every Ansible-era rule on that host is listed by
      exact spec and comment, including the `docker_install` and `optimize_pi` rules on the
      Pi. The arm's enable step (step 6) replaces the `Enable UFW firewall` task, which the
      last host PR deletes with the rest of the subject.
8. **H7.8 daniel-box: NUT secondary.** Pin the `nut` ClusterIP, then move the secondary. Then
   run the `upsmon -c fsd` drill, as *Verification* states.
9. **H7.9 The `k3s` host halves.**
    - **H7.9a daniel-server:** `agent.yml`, `agent_verify.yml`, `static-address.yml`, the
      node files and the agent ufw rules, declared `keep`.
    - **H7.9b daniel-box:** node files, netplan, `coredns-host.service`, disk health, the
      etcd list drill, the kubeconfig oneshot and the join-firewall rules, declared `keep`.
      The netplan file declares `activate: manual:systemd-networkd.service`, because
      `netplan apply` reconfigures the link an SSH session rides on, as `static-address.yml`
      warns. `/etc/zsh/zshenv`'s block moves through the line arm.
    - H7.9b also ships `tmpfiles.d` lines for `k8s_host_files`' media-volume directories:
      `media_volume_host_path` and its `media` and `torrents` subdirectories, owned
      `puid`:`pgid`. The lines leave the mode unset, because the seeding rsync imposes
      daniel-server's modes, as the D5 task's comment says. No unit runs as a writer of
      these directories, so `systemd-tmpfiles-setup.service` creates them at boot and
      bootstrap step 5's `systemd-tmpfiles --create` creates them on a rebuild. The static
      local PV needs its path to exist before a mounter's pod starts. The same PR deletes
      media-volume's task file from `k8s_host_files`.
    - Neither PR deletes `k3s` or edits `k3s-bringup.yml`. The PR that moves the role's
      last task empties it: H7.15a, if the install tasks remain until bootstrap step 4
      replaces them. Each PR records a `manual_plane` line for `k3s`, which *Clearing
      `manual_plane` lines* clears.
10. **H7.10 daniel-box: the checkout timers.** Each keeps its `## Autonomous-role contract`,
    moved into its component's `CLAUDE.md`.
    - **H7.10a** docs refresh and infra map, plus `k8s_host_files`' docs site directory.
      The docs-refresh component ships one `d` line for `docs_host_site_dir`, owned by
      `sys_user` with mode 0755, in a `tmpfiles.d` file. Its service runs
      `systemd-tmpfiles --create` on that file in `ExecStartPre=`, as H5's Compose unit
      does, but with the `+` prefix. The `+` runs that one command as root, because the
      service itself runs as `sys_user`, as the cron does at `70591c307`.
      `systemd-tmpfiles-setup.service` also creates the directory at boot. dpkg never owns
      a path under `/home`. The docs pod's `hostPath` is `type: Directory`, so the
      directory must exist before the pod starts on a rebuilt daniel-box. The same PR deletes the docs task file from
      `k8s_host_files`.
    - **H7.10b** eval sweep and git repair.
    - **H7.10c** secret rotation, `kuma-check-secret-rotation-audit` and
      `kuma-check-loki-read-route`, each in the form M6 left it. `Clear ansible log file`
      and `kuma-check-setup-drift` are deleted.
11. **H7.11 daniel-box: Renovate and fake_remux.** `renovate_agent`, `renovate_notify`,
    `fake_remux`, and the two GitHub check timers from `gitops_deploy`. renovate_agent's
    reads of `gitops_hold` and `gitops_markers` move with it, and H7.19b switches them.
    - **deploy_ui is not packaged.** If D6 kept it, it reads and writes only the
      deployer's state and starts `deploy.sh`, and none of those outlive H7.19b. H7.19a
      retires it with the deployer: its units become legacy entries in
      `homelab-daniel-box`, and the PR deletes its tasks, its Python modules and their
      tests. The one exception is a deploy_ui that D6.15's operator review already moved
      onto C2's `DEPLOY_HOLD`. That one is packaged here, and H7.19b deletes its remaining
      deployer reads in `deploy_ui.py`, `deploy_ui_reads.py` and `deploy_ui_writes.py`.
      The deployer-import census covers it in both cases.
12. **H7.12 Claude Code.**
    - **H7.12a** The paired PR in `DanielH2018/dotfiles` adds the per-user tooling: uv, the
      pinned Python, Claude Code, `claude_guard`, `claude_worktree`, prek, `github_cli` and
      `chezmoi_setup`'s config. It merges first, under that repo's own rules.
      The operator ruled on four open questions on 2026-10-24, and
      [DanielH2018/dotfiles#997](https://github.com/DanielH2018/dotfiles/pull/997) follows them:
      1. **Pins.** The dotfiles repo moves to Ansible's pins: uv `0.13.4`
         (`initial_setup_uv_version`), prek `0.5.5` and Python `3.14.6` (`host_python_version`).
         Both installers write the same `~/.local/bin`, so a differing pin made each apply undo
         the other. Ansible does not change. Until H7.39 deletes its tasks, a bump of one of the
         three lands in both repos together.
      2. **Agent apply.** An `agent = true` apply installs uv, the pinned Python, prek and
         Claude Code as the agent user, so the dotfiles repo lifts the `is-agent` gate on its
         python-tools script. `claude_guard` and `claude_worktree` deploy to the agent from the
         dotfiles repo, not as copies of the operator's. Each installer leaves a tool alone
         when it is already at the pin, so it changes nothing on a home that
         `common/tasks/agent_user.yml` has filled, and that task keeps running until H7.39.
      3. **Hosts.** The sets mirror Ansible's `has_chezmoi`, `has_github_cli` and
         `dev_tooling_hosts`. An agent apply installs the tooling only on `dev_tooling_hosts`
         (daniel-box and daniel-server). The other two sets name daniel-box alone, and the
         dotfiles repo already installs `gh` and its credential helper on any Linux host, so
         it does not narrow them.
      4. **Seed.** Only `chezmoi_setup`'s three prompt answers need a non-interactive path.
         `chezmoi init` with `--promptBool` and `--promptString` for them runs with no tty and
         no config, and a dotfiles test runs that command. Bootstrap step 5 installs the
         chezmoi binary, and H7.12b's unit passes the three flags instead of a seed file.
    - **H7.12b** The agent users join `sysusers.d` with their live uids. A package unit that
      runs as each agent installs its tooling and its clone. `claude_agents.py`'s checks move
      to the renderer, if the host render still reads `claude_code_agents`.
    - **H7.12c** daniel-server, then daniel-box: the timers, `claude-rc.service`,
      `claude-land@.service`, the slice and fan-out caps, and the `claude-rc-alert` unit.
      The daniel-server PR adds a leading `meta: end_role` guard to `claude_code` that
      names daniel-server, H4's pattern, and the daniel-box PR empties the role.
      The daniel-box PR also ships `tmpfiles.d` lines for `k8s_host_files`' artifacts
      directories: `artifacts_host_dir`, one per entry in `artifacts_peer_sources` under
      `artifacts_peer_dir`, each owned by `sys_user` with mode 0750, and
      `artifacts_stars_dir`, owned by `sys_user` with mode 0755. The artifacts sync
      timer's service runs `systemd-tmpfiles --create` on that file in `ExecStartPre=`
      with the `+` prefix, as H7.10a's docs-refresh unit does, because the service runs
      as `sys_user` and two of the paths are under `/home`, which dpkg never owns. The
      artifacts pod's hostPaths need the directories before it starts on a rebuilt
      daniel-box. The same PR deletes artifacts' task file from `k8s_host_files`.
      The units are renamed `homelab-*` under H3's naming rule. The Ansible-era units are
      legacy entries. H6's window test exempts four of these monotonic timers by their
      Ansible-era names, `claude-cgroup-metrics.timer`, `claude-clone-sync.timer`,
      `claude-dotfiles-sync.timer` and `claude-memory-sync.timer`. The PR renames those
      four entries to the `homelab-*` names, each with its exclusion unchanged, so the test
      still exempts them once H7.18a points it at the packages. Restarting the renamed host drops the phone's sessions, so the PR
      lands in a window the operator picks. Every caller of `claude-land@` changes in the
      same PR. `git grep -l 'claude-land@'` finds them, among them
      `scripts/dev/fanout_lib/review/review_land.py`, `docs/landing.md`,
      `docs/claude-agent-user.md`, the polkit rule template `50-claude-land.rules.j2` and the
      agent's profile and `CLAUDE.md` templates.
13. **H7.13 daniel-server: `hypervisor`.**
    - Move the role, with the SUC rehearsal mode that H6.1b2 adds to the drill's
      orchestrator. The drill keeps reading `K3S_TOKEN` from the agent's installer env
      file, `/etc/systemd/system/k3s-agent.service.env`, which H6 leaves in place and the
      rebuild restores out of band (*The agent keeps the server token*). Its S3 env file
      becomes an H4 secret-bearing file. The PR states which S3 folder the drill reads
      after H2.10.
    - Declare the `staging` network and `staging-egress-fence` with their pinned UUIDs.
    - Run the drill once by hand, then from its timer.
14. **H7.14 daniel-box: the builder hosts itself.**
    - `homelab_builder` moves into `homelab-daniel-box`. Three things the role does
      outside its units move with it, each named in the PR body:
        - **The fingerprint file.** H3's builder refuses every build with
          `reason="keyring"` when `/etc/homelab-builder/archive-key.fpr` is absent or
          differs from the render's keyring, and the role rewrites that file from the
          signer's GnuPG home on every apply, because the builder cannot read that home.
          The package moves the write into the builder's unit: an `ExecStartPre=+` script
          reads the signing key's fingerprint from the signer's home as `homelab-signer`,
          and writes the file as root, mode 0644, with M3's write rule. When the home holds
          no signing key, the script removes the file and exits 0, so the builder then
          refuses with `reason="keyring"`. The break-glass key restore in *The cluster-node
          bring-up* therefore regenerates the file on the builder's next run, with no
          Ansible apply.
        - **`publish-textfile`.** The role's script moves to `/usr/lib/homelab/`, and both
          units keep their `ExecStopPost=-+` lines: the builder's with the
          `homelab_builder_` prefix and the signer's with `homelab_repo_`. The script still
          copies only series with the unit's own prefix.
        - **The once-per-condition failure record.** The builder's record of the last
          failing reason and tip (H3 builder step 8) stays under
          `/var/lib/homelab-builder/`, which the package's unit keeps as its state
          directory, so an upgrade does not reset it and `OnFailure=` still posts once per
          condition.
    - **The builder's three tools come from packages.** The role fetches nfpm with `get_url`,
      copies the operator's `~/.local/bin/uv` and runs `uv python install` as the builder
      (`homelab_builder/tasks/builder.yml`). The package replaces all three. Ruled
      2026-10-24:
        - **nfpm** comes from `homelab-tools`, which H7.24a (#6138) extends. The unit's
          `HOMELAB_NFPM` names its path under `/usr/lib/homelab/bin/`.
        - **uv** joins `homelab-tools`, pinned by version and sha256 as sops and chezmoi are
          (`hosts/packages/homelab-tools/package.yml`, one Renovate annotation per
          architecture block). The unit's `HOMELAB_UV` and `ExecStart=` name its fixed path
          under `/usr/lib/homelab/bin/`. The builder stops copying the operator's uv, so the
          role's assert against `initial_setup_uv_version` goes with the copy.
        - **The pinned interpreter** is installed by the builder unit itself. An
          `ExecStartPre=` runs the packaged uv's `uv python install --no-bin <version>` into
          a directory under the unit's `StateDirectory=`, which `UV_PYTHON_INSTALL_DIR`
          names. The version pin lives in the package, so rebuilding the host needs no
          Ansible apply. The run keeps `UV_PYTHON_DOWNLOADS=never` (on `ExecStart=` only, per
          item 17's ruling 5) and `--no-python-downloads`, so only the `ExecStartPre=` downloads, and it does nothing once the version is
          present.
    - **The failure alerts become one packaged template unit.** The role renders
      `homelab-builder-alert.service` and `homelab-repo-sign-alert.service` through
      `roles/setup/common/tasks/alert_unit.yml`, with an `alert-webhook.env` in
      `/etc/homelab-builder`. A new shared component, `failure-alert`, ships
      `homelab-alert@.service` instead. Ruled 2026-10-24:
        - The builder and repo-sign units name it with `OnFailure=homelab-alert@%n.service`.
          The page opens with the instance name, which replaces the per-service message that
          `alert_unit.yml` renders. That file's instanced form (`alert_unit_instanced`) is
          the model. The component keeps the retry-until-delivered behaviour and the
          once-per-failure `Failed at <UTC time>` record.
        - The webhook arrives through `LoadCredential=discord_webhook:/run/credstore/gitops_deploy_discord_webhook`,
          the form `k3s-restart` and checkout-sync use. The credential is the SOPS key
          `gitops_deploy_discord_webhook`, the key `alert_unit.yml` renders today.
          daniel-box's `package.yml` lists it under `credentials:`, and the operator runs
          `host_secrets.py sync daniel-box` to copy it into the host's SOPS file
          (**operator-only**). The role's teardown stops owning the env file.
        - Other roles' alert units can reuse `homelab-alert@.service` later. H7.41 leaves
          them out.
        - This reverses #3694's choice of per-service alert units over one `alert@.service`.
          The reasons in `alert_unit.yml`'s header (four renamed units, and a shared env
          file with no owner) do not apply once a package owns the credential. The PR moves
          the two builder entries out of `ansible/tests/setup/test_alert_unit.py`'s
          `KNOWN_ALERTS` and into the component's own test.
    - **The `users:` arm declares the live ids.** Measured with `id` on daniel-box on
      2026-10-24: `homelab-builder` uid 995 gid 983, and `homelab-signer` uid 994 gid 982.
      The package declares the uids only (item 17's ruling 1), so the install adopts the
      existing accounts and the files they own keep their owners.
    - The packaged builder unit keeps `SyslogLevel=notice`, which this host's journald needs
      (`MaxLevelStore=notice`) for a failed run's traceback to reach the journal, and the
      sandboxing directives that `homelab_builder/tests/test_builder_units.py` holds.
    - **Self-rebuild failure.** The builder builds the package that installs the builder, so
      a broken `homelab-daniel-box` breaks the repair path. The repository keeps the
      previous version, so `apt-get install homelab-daniel-box=<previous>` restores it.
      Otherwise the break-glass procedure below builds one package by hand.
    - Write a break-glass procedure for a broken builder: render, build and sign one
      package by hand as `homelab-builder`, then run `apt install ./pkg.deb`.
15. **H7.15 Bootstrap and the rebuild proofs.**
    - **H7.15a** `bootstrap-host.sh`, complete, with its container test: the mode
      assertion in both modes, the agent's refusal without `K3S_URL` or `K3S_TOKEN`, and
      `--local-build` up to `apt-get install --simulate` of the four packages. The script's
      nfpm, uv and k3s pins arrive with their equality test, and the k3s Renovate manager
      gains the script as a second file. It also adds `--offline <kit-dir>` and the kit's
      file list in `docs/break-glass.md`, with the offline container case, about 100 more
      lines (*The cluster-node bring-up*).
      **Amended 2026-10-25 (H7.42):** the script landed with fake-root tests, written against the
      live tree. Nine premises did not hold:
        - *No LVM step.* No task, role or doc creates daniel-server's LVM volumes. Only
          `ansible/README.md` records a one-time `lvextend` of the root volume. The script
          therefore writes no volume, and an inventor's layout would be destructive.
        - *No `homelab-repos` package.* H7.24a replaced it with the per-host `apt-docker`,
          `apt-azlux` and `apt-github-cli` components, so `--offline` leaves out the
          `github-cli` source file only.
        - *nfpm and uv pins.* Both are the blocks of `hosts/packages/homelab-tools/package.yml`
          (H7.24a, H7.41a), embedded byte for byte. The builder's own role no longer holds
          the uv pin.
        - *Three packages, not four.* `--local-build` builds `homelab-tools`,
          `homelab-updater` and `homelab-<host>`.
        - *Step 6.* `homelab-docker-engine.service` is not in the tree until H7.25 lands. The
          script starts it when the unit exists and prints that it starts nothing otherwise.
        - *The k3s Renovate manager.* The `k3s-io/k3s` rule is a package rule, not a manager.
          `renovate.json` gains a small custom manager for the script's `K3S_VERSION` line, and
          the `homelab-tools` manager gains the script as a second file.
        - *`preflight.yml`'s host-var checks.* They have no counterpart: the script embeds the
          three host names and refuses any other.
        - *The anchor check.* It runs as `hosts/lib/bootstrap_anchor.py` from the archived
          tree, not inside the script, so its decision has unit tests.
        - *Not in this PR.* The systemd container cases (`apt-get install --simulate` and the
          offline case with its two red fixtures) need the `host_packages` job's image, and the
          fake-root suite replaces them for this PR. H7.44's guest drill still owns the
          end-to-end proof.
      **Amended 2026-10-25 ([#6231](https://github.com/DanielH2018/server/issues/6231)):** the
      container cases landed in `scripts/validate/validate_lib/bootstrap_case.py`, which the
      `host_packages` job runs. Writing them found that `--local-build` looked for homelab-tools'
      `nfpm.yaml` above the per-architecture directory the renderer writes, so every local
      build died at step 5. Four premises changed:
        - *One case per host.* `--local-build` runs on a container named after each host, as
          far as `apt-get install --simulate`. The path that installs from the repository has no
          case, because a test repository cannot be signed with the archive key the script pins.
        - *The anchor.* Each case's scratch clone commits a stub in place of
          `hosts/lib/bootstrap_anchor.py`, because a pull request's head is on no first-parent
          chain of master. The anchor's unit tests hold its decision.
        - *Offline.* The offline container has no network at all, which is stricter than
          unresolvable GitHub and GHCR hostnames. Its kit's `k3s` is a stand-in that answers
          `--version`, because the script checks it against the kit's own checksum list.
        - *No local-build `Depends` red.* The red fixture that gives `homelab-daniel-box` a
          `Depends` only the repository carries is not in the cases.
    - **H7.15b** The Pi runbook, then the Pi rebuild, which is exit observation 2. The
      operator keeps the old SD card as the rollback. The cluster's wg-easy on 51820 is
      proven first, as in H5.5.
    - **H7.15c** The cluster-node procedure with its offline branch, and its two
      proofs, which are exit observation
      4: R4 PR 7b's kind drill, run on this PR, and the **operator-only** guest drill, whose
      blast radius the PR body states as *The proofs* gives it.
    - **H7.15d** The host-addition walkthrough in `docs/host-packages.md`, the host census and
      a fixture-host container case (about 150 lines). It merges before H7.20a (*Adding a
      host*).
    - **H7.15e** The release-upgrade procedure in `docs/host-packages.md` (docs only, about
      60 lines) (*Ubuntu release upgrades*).
16. **H7.16 Scripts for the operator playbooks.**
    - **H7.16a** Convert `prune_backups.yml`, `seed_volume_backup.yml` and
      `k3s-storage-smoke.yml`, and delete `preflight.yml`.
    - **H7.16b** Replace the `volume-revert` and `longhorn-api` runbook with a task-free
      command that reads the snapshot name D5 states. **Operator-only:** drill it once.
17. **H7.17 The last Ansible imports.** The PR body re-runs the Baseline's two greps and
    disposes of each of the 25 files, or names the later PR that does.
    - **`ansible/tests/_helpers.py` waits for H7.19d.** Until H7.19a and H7.18c empty
      `gitops_deploy` and `k8s_host_files`, tests of their live templates still render
      through it, so it stays on the deletion census's allowlist.
    - **The `Templar` and `DataLoader` tests.** Each of the ten, and each oracle test R1, R3
      and R4 added, is converted to the renderer's env or to a fixed expected output taken
      from its last green run, or is deleted with its subject.
    - **The `ansible.errors` tests** follow their plugins. R4 PR 10 converted `toposort.py`
      and `authelia_access.py`, R4 deleted `k8s_retire.py`, and D6.13 deleted
      `k8s_autodeploy.py`; the PR confirms all four. `claude_agents.py` is deleted with its
      last caller, or loses its `ansible.errors` import if the host render still uses it.
    - **Host and package modes without ansible-core.** R4's exit 4 proves cluster mode only.
      R4's subprocess test, which sets `sys.modules['ansible'] = None`, gains host mode for
      each of the three hosts and package mode for every package under `hosts/packages/`.
    - **The deletion census lands here**, ahead of H7.20b. It is a runtime check: a
      subprocess collects the whole suite with `sys.modules['ansible'] = None` and fails on
      any import error. Its named member is `ansible/tests/_helpers.py`, which the
      collection must import. Its allowlist names each file still importing `ansible` and
      the PR that removes the import: `ansible/tests/_helpers.py` (H7.19d),
      `scripts/deploy_tools/deploy_lib/flags.py` (H7.19b) and
      `test_pi_escalation_timeout.py` (H7.20a). An import error raised from an allowlisted
      file, including one a module meets by importing `_helpers`, is tolerated, and any
      other fails the census. The allowlist must be empty before H7.20b merges.
18. **H7.18 Closing the readers.**
    - **H7.18a** Each docs generator reads `hosts/*/package.yml`, the component units and
      the timers. Each gains a named-member assertion, such as `homelab-docs-refresh.timer`
      appearing in `docs/reference/crons.md`.
    - **H7.18b** The component contract census, described in *Verification*.
    - **H7.18c** Every absent-arm cron task becomes a legacy entry, by cron name or
      `cron_file`, in each host package its task applied to, with a census. H7.2f's census
      already reads each `ansible.builtin.cron` task, so it emits the ones whose `state:` is
      `absent`, a loop over `docker_install_stale_crons` included. That covers the three
      retirements and the absent halves of live crons (Baseline), and every absent arm left
      in `k8s_host_files`: traefik's `cloudflare-ip-drift` cron file and script, registry's
      three GC crons, and any retired-cron absent arm H2 placed there. H7.2f's report is the
      list of record. With its last rows gone, the PR empties `k8s_host_files` and deletes
      D5's render test for it.
    - **H7.18d** pi-peer-backup's template reads `server_ip` and a role default user.
      `scripts/docs/reference/hosts.py` and `.claude/hooks/block-footguns.py` stop naming
      `ansible_connection`. The cluster render's `diff -r` is empty.
19. **H7.19 Retire the deployer.**
    - **H7.19a** Five PRs. The second, third and fourth depend on H7.14 and D6, and the
      fourth on H7.18a as well, so the three may land in any order. The third must precede the fifth, because after the fifth
      a land.sh tick would start a service that no timer drives. The fourth must precede
      the fifth, so the checkout always has a fast-forward.
        - The first removes monitor-bridge's gitops checks, its
          `monitor-bridge-gitops-state` volume and mount, `GITOPS_STATE_DIR` and
          `config_service.py`'s read, and lands the hostPath row in
          `ansible/tests/k8s/test_rendered_properties.py` (*Path readers*). The role's
          `CLAUDE.md` restates its `nodeSelector` as the etcd drill's. The deployer runs
          unwatched from this merge until the fifth PR's.
        - The second retires deploy_ui as H7.11 states: its units become legacy entries in
          `homelab-daniel-box`, and the PR empties the role and deletes its Python modules
          and tests. It deletes about 2,300 of the 2,438 lines in the role's 16 files at
          `70591c307`, and adds under 50. It is a deletion sized by subject, as H7.20's are.
        - The third stops land.sh running the tick, as *land.sh stops running the tick*
          lists, with the red-half test, and `brief.py:APPLY_OWED` drops its reads. It
          deletes about 1,040 lines of whole files at `70591c307` (`gitops_tick.sh` 329,
          `tick.py` 135, and the three tick test modules 574), and changes or adds about 150
          more at the call sites and in the new test. `docs/landing.md` and the
          `land-after-merge` skill change with it, because they describe land.sh.
        - The fourth ships `homelab-checkout-sync` beside the running deployer, with its CI
          gate, its empty-set and bound refusals, its token credential and its ancestor
          walk. It adds about 350 lines, half of them the stub tests, which must land with
          the gates they prove. It adds `homelab-checkout-sync.timer` to H6's window-test
          exemption list with its exclusion, and merges after H7.18a, so the test reads the
          packaged timer. It changes no running behaviour: both actors only
          fast-forward (*Overlap*).
        - The fifth lists `gitops-deploy.timer` as a legacy entry and empties the
          `gitops_deploy` role, which removes about 530 lines of `tasks/` and turns the
          role's `CLAUDE.md` into a stub. Root `CLAUDE.md`'s *After a PR Merges* and *When to
          wait*, the `gitops-tick` skill's retirement and `docs/gitops-pipeline.md` change
          with it, because they describe the timer. It removes H6's seven deployer-coupled
          items, as *H6's deployer-coupled checks go with the timer* lists, from
          `k3s_gates.py`, the restart script, the component and H6's tests. Those files do
          not exist at `70591c307`, so the PR body states the deleted lines they add to the
          role's 530. The PR publishes new versions of both cluster-node packages. The role's line in `initial_setup.yml`
          stays until H7.20a, so the merge does not park the tick it retires. It merges only
          while `hold_sha` is empty and `owed.jsonl` holds no line.
    - **H7.19b** Re-home, then delete, as *Surviving importers of deployer modules* lists.
      It may land as two PRs, the re-home first.
        - Re-home `TREE_LOCK` to `scripts/lib/tree_lock.py` and `k8s_lookup_map` to
          `scripts/lib/k8s_lookups.py`, and repoint `land_lib/tools.py`, `consumers.py` and
          their tests. Move `role_of`, `role_for` and `is_role_test_path` to
          `scripts/lib/role_paths.py` only if the grep at the PR's base finds a caller that
          outlives land.sh's setup and Pi arms.
        - Delete setup narrowing except `setup_gates.py`, which the censuses read until
          H7.20a, with all seven `narrow_lib` modules. Delete the rest of `land.sh`'s
          setup arm and its Pi arms, `land_reach.py`, the rest of `reach.py` and
          `classify.py`'s setup arm, which D6 hands over. Delete `deploy.sh` and its
          library, `gitops_state.py`, `deployer_park.py`, monitor-bridge's copies of the
          state modules, `gen_gitops_markers.py`, `gen_doc_fragments.py`'s gitops
          fragment, and the session-health arms. H7.19a's third PR already deleted
          `gitops_tick.sh` with land.sh's tick step.
        - List `gitops-deploy.service`, `/opt/gitops-deploy`, `/etc/gitops-deploy` and the
          polkit rule as legacy entries in `homelab-daniel-box` (*Retirement, across two
          daniel-box versions*). The PR body names `/var/lib/gitops-deploy/` as the
          operator's cleanup.
        - Delete or switch to `DEPLOY_HOLD` every other reader of `gitops_hold`,
          `gitops_markers` and `gitops_ledger`: `land_lib`'s `landing.py`, `options.py`,
          `outcome.py` and `tools.py`, `runbook_gates_lib/k3s_upgrade.py`,
          `probe_lib/gitops_view.py` and `landing_blockers.py`,
          `scripts/docs/reference/state.py`, and renovate_agent's three modules.
        - Land the deployer-import census, whose allowlist holds only the tests of the
          deployer's own code.
        - Rewrite or retire the `deploy` skill. H7.19a already retired the `gitops-tick`
          skill and cut `land-after-merge` to C6's waits.
    - **H7.19c** Turn the setup census strict. It fails while any setup role has a task
      that applies to a host.
    - **H7.19d** The test env leaves ansible-core. `ansible/tests/_helpers.py`'s Jinja env
      is rebuilt on the renderer's filter set from R4's `renderer_lib/filters.py`, in place
      of ansible-core's three plugin modules, and leaves the deletion census's allowlist.
      The filter set shrinks for 361 modules, so the PR carries a parity proof in two
      parts. First, every module in the suite passes on the new env. A module that still
      tests a template no renderer mode reads retires in this PR, and the PR body lists
      each one. Second, a census lists every filter and test name used by the templates the
      renderer's three modes read, a set it derives from the renderer's inputs as C3's
      publish census does, and asserts that the renderer's env or Jinja's built-ins
      registers each. Its named member is `b64encode`, and its red fixture is a template
      that names an unregistered filter.
20. **H7.20 Delete Ansible.** Deletion PRs, each sized by subject as R4 and D6 size theirs,
    each stating its line count:
    - **H7.20a** the playbooks, `bring-up.sh`, every emptied role directory under
      `ansible/roles/setup/` and `ansible/roles/containers/`, and the runbook roles' tasks,
      with every test whose subject it deletes, `test_pi_escalation_timeout.py` and the
      remaining modules under `ansible/tests/setup/` that test only Ansible mechanics among
      them. It also deletes `scripts/deploy_tools/setup_lib/setup_gates.py` with the censuses that
      read it, H3.2's two-writer census and its ufw and line-arm rows and the setup census,
      whose subject the PR deletes. It merges only after daniel-box shows the version from
      H7.19b's deleting PR and
      `systemctl list-unit-files 'gitops-deploy*'` lists nothing there, so no tick sees its
      edits to the bring-up playbooks. Its proof has two parts. The first is an empty
      `diff -r` of `homelab-render host` for each of the three hosts and of
      `homelab-render package` for every package, at the PR's base and head. A render
      diff cannot see an import break, so the second is a full `uv run pytest
      --collect-only` at the head with no import error, and the deployer-import census
      green with its allowlist empty;
    - **H7.20b** ansible-core, the collections, `ansible.cfg`, `.ansible-lint`, the prek
      hook and the setup entries left in `pyproject.toml`. The deletion census's allowlist
      is empty by then;
    - **H7.20c** the ADR, `ansible/README.md`, the archived bring-up sections, and the
      setup half of `ansible/tests/repo/test_role_claude_md.py`;
    - **H7.20d** the Pi's `containers_list` key, after H7.20a. The key must outlive every
      Ansible reader: `preflight.yml` asserts it until H7.16a, `deploy.sh` and `land.sh`'s
      Pi arms read it until H7.19b, and `deploy.yml`'s Docker play and the restored tasks
      of a rollback re-apply read it until H7.20a. The PR does these things:
        - deletes `containers_list: []` from `ansible/inventory/host_vars/daniel-pi.yml`;
        - swaps `_example.yml`'s `containers_list: []` for `compose_services: []`, and
          rewrites its comment, which names `deploy.yml`;
        - points every reader that H5.1 pointed at both keys at `compose_services` alone:
          the three cluster templates, `render_context.py:_HOSTVARS_KEYS`, H5's helper in
          `scripts/lib/ansible_inventory.py` with `health_docker.py:declared_on_pi`, the
          recovery heartbeat's watch set, and H5's service-list census in
          `ansible/tests/setup/`. Two greps at the PR's base are the census of record:
          `git grep -n compose_services`, because H5 makes every reader of the union name
          both keys, and `git grep -n -F "hostvars['daniel-pi'].containers_list"`;
        - rewrites H5's census to assert that no `host_vars` file defines
          `containers_list` and that `compose_services` holds `docker-proxy` and `alloy`.
          A fixture `host_vars` with a Pi `containers_list` key is flagged. The census
          reads the inventory, not Ansible mechanics, so H7.20a keeps it.

      Proof: `homelab-render cluster` and `homelab-render host --host daniel-pi` at the
      PR's base and head, and `diff -r` over each pair prints nothing. The Pi's key was
      already empty, so no rendered byte moves.

</details>

## Verification

```bash
# Exit 1.
git ls-files ':(glob)ansible/*.yml' ':(glob)ansible/*.sh'
git ls-files ansible/roles/setup ansible/roles/containers ansible/pre_tasks \
  ansible/post_tasks ansible/tasks ansible/prune_backups ansible.cfg .ansible-lint \
  ansible/requirements.yml
git ls-files 'ansible/roles/*/*/tasks/*'
git grep -n '^containers_list:' -- ansible/inventory
uv tree | grep -c ansible-core
grep -c ansible-lint prek.toml

# After each per-host PR: one batched read-only call per host. The live-state probe
# matches the subject, for example `sshd -T | grep -i maxauthtries` for H7.6d-f.
ssh daniel-server 'dpkg-query -W homelab-daniel-server; systemctl --failed --no-legend "homelab-*"; dpkg --verify homelab-daniel-server; sudo ufw status numbered; sudo sshd -T | grep -iE "maxauthtries|allowgroups"; sysctl -n net.core.bpf_jit_harden; grep -c "^net.core.bpf_jit_harden=" /etc/sysctl.conf'
curl -s http://127.0.0.1:9090/api/v1/query --data-urlencode 'query=homelab_host_verify_mismatches or homelab_host_reboot_pending or homelab_host_activation_pending or homelab_host_reconcile_ok or homelab_host_reconcile_drift'
# Then, for a PR whose tick recorded a manual_plane line, once the version metric moved:
uv run python scripts/deploy_tools/gitops_state.py clear-owed manual_plane <role> --applied <tags>

# Exit 2: the Pi runbook, run by the operator at the Pi's console.
dpkg-query -W homelab-daniel-pi; docker compose ls; systemctl list-timers 'homelab-*' --no-pager
sudo ufw status | head -n 1
stat -c '%U %G %a' /var/lib/node-exporter-textfile
# Exit 2, then from daniel-box: runbook step 8.
curl -s http://127.0.0.1:9090/api/v1/query --data-urlencode 'query=homelab_host_applied_info{host="daniel-pi"} or node_textfile_scrape_error{job="node-pi"}'

# Exit 3: the drill. Its token source is the agent's installer env file.
ssh daniel-server 'sudo grep -c "^K3S_TOKEN=" /etc/systemd/system/k3s-agent.service.env; systemctl start homelab-etcd-restore-drill.service; journalctl -u homelab-etcd-restore-drill -n 20 --no-pager'

# Exit 4, host half, inside the drill guest, with the guest's own admin kubeconfig.
dpkg-query -W homelab-tools homelab-updater homelab-daniel-box
systemctl list-unit-files 'k3s*.service' --no-legend
k3s kubectl get node daniel-box
ufw status | head -n 1

# Exit 4, Flux half: R4 PR 7b's kind drill, as run on H7.15c's PR.
gh run list --workflow flux-bootstrap-drill.yml --limit 1

# Exit 5, on daniel-box.
systemctl list-unit-files 'gitops-deploy*'; systemctl is-active homelab-checkout-sync.timer
git -C ~/server rev-parse HEAD origin/master
journalctl -u homelab-checkout-sync.service -n 20 --no-pager

# Exit 5, in a worktree at master.
uv run pytest --collect-only -q | tail -n 3
uv run pytest ansible/tests/repo/test_census_rows_python.py -q
```

The expected results are:

- the first listing prints only `ansible/secret_rotation.yml`, and the other three print
  nothing;
- both counts print 0;
- no failed `homelab-*` unit, `dpkg --verify` prints nothing, and the live-state probe shows
  the declared values;
- `ufw status numbered` opens with `Status: active` on every host, and the LAN SSH allow
  rule sorts above the SSH limit rule;
- `sysctl -n` prints the declared value, and `/etc/sysctl.conf` holds exactly one line for
  the key;
- the mismatch, pending and drift metrics read 0 on every host, apart from a reboot-pending
  flag the PR body announced, `homelab_host_reconcile_ok` reads 1 on every host, and no
  `manual_plane` line that the PR recorded is left;
- the Pi's version matches the builder's `homelab_repo_published_info`, and its ufw reads
  `Status: active`;
- the textfile directory prints `root <sys_user> 2775`,
  `homelab_host_applied_info{host="daniel-pi"}` names the version `dpkg-query` printed, and
  `node_textfile_scrape_error{job="node-pi"}` reads 0;
- the env file holds one `K3S_TOKEN` line, and the drill's journal ends with its success
  line;
- in the guest, the four packages are listed, `k3s.service` is the only k3s unit file,
  `k3s --version` reports the script's embedded `k3s_version`, the node reads Ready, and
  ufw reads `Status: active`;
- the kind drill's run is green: its green job shows `cluster-vars` Ready, and its red job,
  which skips `sops-age`, passes only when `cluster-vars` fails. If the green job does not
  yet check that the `homelab-cluster-vars` Secret carries Flux's
  `kustomize.toolkit.fluxcd.io/name` label, H7.15c adds that check to the workflow;
- no `gitops-deploy` unit file is listed. The two SHAs match once master's CI verdict is
  green. While the tip is red or pending, `HEAD` is the commit the journal's last line
  names as the newest green ancestor;
- collection reports no error, and the census row passes.

**The NUT drill in H7.8.** `upsmon -c fsd` makes every armed host power off, which is both
cluster nodes. The control plane, Pi-hole, ingress and every workload go down. The drill is
**operator-only** and runs in a window the operator schedules. Its preconditions are the
operator at both consoles and a clear Longhorn gate: no volume degraded and no rebuild in
progress.

**Red proofs for the checks this slice adds.**

- *Activation.* One test per action kind: a changed file with `try-restart`, `reload`,
  `sysctl`, `modules-load` or `reboot` runs that action, and an unchanged file runs nothing.
  A `reload:ssh.service` whose `sshd -t` fails leaves sshd untouched, exits non-zero and
  writes the flag file, `homelab-reboot` then refuses, and the updater stub holds.
- *Config validity in CI.* The container test runs `sshd -t -f` on the rendered sshd
  config, `fail2ban-client -t`, `sysctl -p` on each rendered `sysctl.d` file and on a
  scratch `/etc/sysctl.conf` carrying the line arm's sysctl lines, and a journald
  parse. A fixture with a misspelled sshd keyword turns the first red.
- *Owned paths.* Shipping an owned path fails with "trying to overwrite". If a divert
  survives H7.3's measurement, its lifecycle PR adds the four upgrade and revert cases, each
  with a red fixture, as *In-place edits* lists them.
- *ufw arm.* Each against a stub `ufw`:
    - a declared rule missing from `ufw status` is added;
    - a `homelab:` rule no longer declared is deleted, at most two in one run, and a third
      is counted as drift while the run succeeds;
    - an empty declared set deletes nothing, counts each `homelab:` rule as drift, and the
      run succeeds;
    - a revert fixture, which drops every declaration, deletes no `homelab:keep:` rule.
      Driven through H3's updater stub, that run writes no hold file and
      `homelab_host_reconcile_ok` reads 1, and the updater's next run installs the
      fixture's next version. The red fixture treats a floor hit as a failure, and the
      updater then holds and installs nothing, so the test fails;
    - an allow rule placed after its `before:` limit rule is flagged, the run fails, and
      the updater stub writes the hold;
    - a legacy rule is deleted only after its replacement is present;
    - an uncommented rule that no legacy entry names is left alone;
    - a stub reporting `Status: inactive`, which replays the output H7.2b measured, is
      enabled with `ufw --force enable`, and the stub's call log shows the enable after
      every `keep` rule was added. Its red fixture drops step 6, the stub stays inactive,
      and the test fails. A second red fixture enables before the SSH rules are added,
      and the call-order assertion flags it;
    - a stub whose `ufw --force enable` exits non-zero fails the run, and the updater stub
      holds;
    - on an inactive stub, a `before:` violation in `ufw show added` fails the run. Its red
      fixture reads `ufw status numbered`, sees the empty inactive output, and passes the
      check, so the test fails.
- *ufw two-writer census.* A fixture where an Ansible task and a component declare one spec
  for one host is flagged.
- *The per-host guard.* In H3.2's census and in the ufw and line-arm censuses, a fixture
  where a component declares a subject for daniel-pi while that subject's task carries no
  `<role>_packaged_hosts` guard naming daniel-pi is flagged. The same fixture with
  daniel-pi in the map passes, and still flags daniel-server, whose package declares
  nothing. In the setup census, a host in the map whose package declares nothing for the
  subject is reported as missing.
- *libvirt arm.* An object the arm recorded and no longer declares is undefined. An object
  it never recorded, `default` among them, is never touched. A stub `virsh` that refuses
  the undefine leaves the object recorded, counts it as drift, and the run succeeds with no
  hold; the next run undefines it once the stub accepts. Its red fixture reports the
  refusal as a failure, and the updater stub holds, so the test fails.
- *Users arm.* A user leaving the list is expired, not deleted. A declared uid that differs
  from the live uid fails the run, and the updater stub holds.
- *Legacy service retirement.* A legacy service entry whose stub `systemctl is-active`
  reports `active` stays unretired, is counted as drift, and the run succeeds with no hold.
  The same entry reporting `inactive` is stopped, disabled and removed. Its red fixture
  retires the active unit, and the test fails.
- *The way out of a hold.* With the updater stub holding after a uid mismatch, installing
  a fixture version that declares the live uid and running `--clear-hold` removes the hold
  and writes `homelab_host_reconcile_ok 1`. Installing nothing and running `--clear-hold`
  keeps it.
- *Line arm.* A line in an unowned file is kept, and so is a line or block in a conffile.
  The arm's first run adopts a line or block that the Ansible task wrote, and adds no
  second copy. An owned path that is not a conffile is refused. The two owning-package
  upgrade cases in *In-place edits* keep the line.
- *Line-arm two-writer census.* A fixture where an Ansible `lineinfile` task and a host's
  line-arm declaration name one path for one host is flagged. A fixture
  `ansible.posix.sysctl` task with no `sysctl_file`, setting a key that host's line arm
  keeps in `/etc/sysctl.conf`, is flagged. One that sets a different key is not, so zram's
  keys can move before the hardening keys. The named member is `net.core.bpf_jit_harden`.
- *One mechanism per sysctl key.* A census in `hosts/lib/tests/` flags a component that
  ships a `sysctl.d` file naming a key a line arm keeps in `/etc/sysctl.conf`. Its red
  fixture is such a drop-in. In the CI container, a second test writes `/etc/sysctl.conf`
  with `net.ipv4.conf.all.accept_redirects=1` and a drop-in with `=0`, runs `sysctl
  --system`, and asserts that the live-state check, which reads `sysctl -n` against the
  declared value, fails for the drop-in's `0`. The same key declared through the line arm
  passes.
- *Reconcile ceiling (H7.2a).* A fixture ceiling one line below the measured count fails
  the census, which prints both numbers. A fixture module that only reconcile imports, and
  that carries reconcile over the ceiling, also fails it, so a split into helper modules
  cannot hide growth.
- *checkout-sync.* Each against a stub `git` and a stub verdict, each with a red fixture
  that skips the gate: a dirty tree refuses and sets `homelab_checkout_dirty` to 1; a
  branch other than `master` refuses; a red, pending or no-verdict tip is not merged; and
  the fast-forward runs only while the tree lock is held. The ancestor walk has two cases:
  a pending tip with a green first-parent parent advances to the parent, and a red
  ancestor between the tip and an older green one is never chosen. The red fixture grades
  the tip only, and the first case then leaves `HEAD` where it was, so the test fails. An
  empty `ci_required_contexts` merges nothing and sets `refused{reason="ci"}`; its red
  fixture passes the empty set straight to `ci_verdict`, which returns `pass`, and the
  fixture's tip is merged, so the test fails. A walk bound of 0 or a missing bound sets
  `refused{reason="ci"}` before any grading; its red fixture passes 0 to
  `ci_walk_candidates`, grades the pending tip only, and leaves `HEAD` where it was with no
  refusal, so the test fails. With no credential file and a stub `gh` that would print a
  token, checkout-sync sets `refused{reason="token"}` and the stub HTTP server records no
  request; its red fixture keeps the `gh auth token` fallback, and the request the stub
  records carries the operator's token, so the test fails.
- *The deployer-import census (H7.19b).* As *Surviving importers of deployer modules*
  gives it: the named member `scripts/deploy_tools/await_ci.py` is read, and a fixture
  holding `from deployer_lib.deploy_locks import TREE_LOCK`, one with a function-level
  `import gitops_markers`, one whose string constant is `/var/lib/gitops-deploy/hold_sha`
  and one that indexes a loaded YAML mapping with `"gitops_deploy_ci_contexts"` are each
  flagged.
- *land.sh without a tick (H7.19a).* A test in `scripts/deploy_tools/tests/` drives the
  pipeline for a tagless PR whose only target is a host, with the host wait stubbed
  settled and a stub `systemctl` on `PATH` that reports `gitops-deploy.service` absent. The
  landing ends `settled` and starts no tick. Its red half runs the same fixture against the
  base's `_STEPS`, where `_step_tick` runs `gitops_tick.sh`, and that run ends
  `deploy-failed`.
- *No gitops hostPath (H7.19a).* A row in `ansible/tests/k8s/test_rendered_properties.py`
  asserts that no rendered cluster manifest mounts a hostPath under
  `/var/lib/gitops-deploy`. Its named member is monitor-bridge's Deployment, which the row
  must find, and a fixture Deployment carrying that hostPath is flagged.
- *The moved CI key (H7.2e).* D1's `decide` test with an empty `ci_required_contexts`
  prints `error`. Its red fixture leaves `decide` reading the old role default, which is
  then a reference string, so the green case prints `error` where it expects `promote`,
  and the test fails. The key grep at the PR's head lists no reader outside the edited
  comments.
- *H6's deployer-coupled checks (H7.19a's fifth PR).* With `/host/var/lib/gitops-deploy`
  absent from the fixture root, `k3s_gates.py` server mode passes once the other checks
  pass. Its red fixture is the base's `k3s_gates.py`, which refuses that root. H6's window
  test reads `homelab-checkout-sync.timer` and passes through its exemption entry, and
  the existing red fixture, an unlisted timer with `OnUnitActiveSec=10min`, still fails.
- *The fingerprint file (H7.14).* Against a scratch signer home, the builder's
  `ExecStartPre=` script writes the key's fingerprint at mode 0644, and a home with no
  signing key leaves no file. A builder run with no file refuses with `reason="keyring"`.
  The red fixture drops the `ExecStartPre=` line, and the builder then refuses with a key
  present, so the test fails.
- *The Pi's textfile directory (H7.4).* In the CI container, `systemd-tmpfiles --create`
  on the rendered file creates the directory as root, group `sys_user`, mode 2775, and an
  updater stub writes `homelab_host.prom` there. The red fixture drops the `d` line, and
  the stub exits non-zero, so the test fails.
- *Shared-module routing.* A change to `hosts/lib/host_lib.py` routes to the Ansible
  consumers, and a change to `hosts/lib/ci_verdict.py` routes to `gitops_deploy`. The red
  fixture keeps only the old keys and routes nothing.
- *The Compose networks.* The networks the `ExecStartPre=` creates equal the external
  networks the rendered project names, and a fixture network the `ExecStartPre=` omits is
  flagged.
- *Docker engine.* In the CI container, a dummy `docker-ce` at version A is installed and
  held, and the package carries pins for version B. After an updater run, `docker-ce` is
  still at A, and `homelab_docker_engine_pending` reads 1. The red fixture adds `Depends:
  docker-ce (= B)`, and the test sees the version move and fails.
- *Bootstrap.* In a container, a hostname mismatch is refused. A second run on a matching
  host changes nothing, and does not rerun the installer. `INSTALL_K3S_EXEC=agent` yields
  only `k3s-agent.service`, and a stubbed installer that creates `k3s.service` for an agent
  host fails the assertion. An agent run without `K3S_URL` or `K3S_TOKEN` is refused.
  `--local-build` on the CI checkout builds the four packages, and `apt-get install
  --simulate` resolves them with no homelab repo configured. Its red fixture gives
  `homelab-daniel-box` a `Depends` on a package that only the homelab repo would carry and
  the local build does not produce, and the simulation fails. The container case stubs the
  anchor check's two GitHub reads. A `HEAD` off `origin/master`'s first-parent chain is
  refused, a `HEAD` with a red CI verdict is refused, and a green first-parent `HEAD`
  builds.
- *Offline bring-up (H7.15a).* The container runs `--local-build --offline <fixture kit>`
  with GitHub's and GHCR's hostnames unresolvable, as far as `apt-get install --simulate`,
  so a fetch the branch missed fails it. Its output says the clone is unchecked. A fixture kit with no k3s binary refuses and names the file, and a kit whose
  nfpm differs from its pin refuses.
- *Adding a host (H7.15d).* The container renders and builds a fixture host made from the
  walkthrough's file list. A fixture host with no creation rule, or missing from one named
  set, fails the host census, which names the set.
- *Bootstrap pins.* The script's nfpm and uv pins equal the ones in
  `hosts/packages/homelab-tools/` and the builder's. Its three k3s pins equal the keys in
  H6's `system-upgrade` defaults. A fixture that changes one side only is flagged, the k3s
  case included.
- *Renders without ansible-core (H7.17).* Host mode for each host and package mode for
  every package render in a subprocess where `import ansible` raises. As in R4, the red
  half runs the same subprocess with a shim that makes `renderer_lib.filters` import
  `ansible` first, and that run must fail.
- *The `_helpers.py` filter census (H7.17).* Every filter and test name a tracked `*.j2`
  uses is registered. Its named member is `b64encode`, and a fixture template naming an
  unregistered filter is flagged.
- *The setup census.* H7.2f's census names `Weekly apt autoremove`,
  `claude-land@.service.j2` and `k3s/tasks/static-address.yml` as members it must find, and
  a fixture role with one unassigned cron task is flagged in strict mode. A fixture
  `authorized_key` task with `delegate_to: daniel-pi`, in a role whose play runs on
  daniel-box, is reported under daniel-pi and flagged there, never under daniel-box. In
  report mode, a fixture `apt` task and a fixture `get_url` task with no assignment are
  each reported, and a `command` task with `changed_when: false` is not. The red fixture
  restores the old module list, and the `apt` and `get_url` tasks go unreported. An emptied
  role's `meta: end_role` stub is not flagged in strict mode.
- *The Pi's service list (H7.20d).* The rewritten census flags a fixture `host_vars` that
  defines `containers_list`, and asserts that `compose_services` holds `docker-proxy` and
  `alloy`. The two `homelab-render` pairs show an empty `diff -r`.
- *The deletion census.* Two rows, each with a named member:
    - in `ansible/tests/repo/test_census_rows_python.py`, landed by H7.17: a subprocess
      collects the whole suite with `sys.modules['ansible'] = None`, and any import error
      outside the allowlist fails it. The named member is `ansible/tests/_helpers.py`,
      which the collection must import, and a fixture module holding `import
      ansible.errors` is flagged. The allowlist is empty before H7.20b merges;
    - in `ansible/tests/repo/test_census_rows_text.py`, no tracked file names
      `ansible-playbook`, except under `docs/archive/`, `docs/adr/`,
      `docs/target-architecture/`, the generated `docs/reference/` and the census's own
      fixture; the named member is root `CLAUDE.md`, and a fixture naming
      `ansible-playbook` is flagged.
- *Component contracts.* A census over `hosts/components/*/component.yml` requires an
  `## Autonomous-role contract` heading in the component's `CLAUDE.md` for every component
  that ships a timer marked `changes_state: true`. Its named members are docs-refresh,
  secret-rotation, compose-adopt, checkout-sync and claude-memory-sync. A fixture component
  with such a timer and no heading is flagged.
  `ansible/tests/setup/test_setup_cron_roles_have_a_contract.py` and the setup half of
  `ansible/tests/repo/test_role_claude_md.py` retire in H7.20c, when their subject does.
- *Retired crons.* A census asserts that each absent-arm cron task that H7.2f's census
  emits, the entries of the `docker_install_stale_crons` loop included, appears as a legacy
  entry in each host package its task applied to. Its named members are the three
  retirements: `Retire the TLS cert-expiry watch`, an entry of
  `docker_install_stale_crons`, and the cron that `Remove the cron the kuma-check timer
  replaces` removes. A fixture absent-arm task with no legacy entry is flagged.
- *Generators.* Each generator's named member, such as `homelab-docs-refresh.timer` in the
  crons page, is asserted present, so an empty read fails.

## Rollback

- **Every hand re-apply below is operator-only.** Both playbooks import
  `pre_tasks/load_secrets.yml`, which decrypts `secrets.yml`, and H3's Baseline records that
  the `claude` user has no age key and no login on the Pi. An executing agent stops at each
  re-apply and at each `--clear-hold`, and hands the command to the operator.
- **H7.1 to H7.14, per PR.** Revert the PR. The builder publishes a version without the
  component, dpkg deletes its files, and reconcile stops its units. Then re-apply the role's
  restored tasks by hand, **after** `homelab_host_applied_info` shows the reverted version on
  that host. The order matters because both writers use the same paths: an Ansible apply
  that runs before dpkg removes the files is undone by dpkg.
    - **A held host never installs the reverted version.** A failed reconcile is the
      likeliest reason to revert, and it holds the host with the version metric at
      `last_ok` (*Which outcomes hold the host*). The operator first takes the way out of
      the hold: `sudo apt-get install homelab-<host>=<reverted version>`, then `sudo
      /usr/lib/homelab/homelab-updater --clear-hold` (**operator-only**). The version metric
      then moves, and the re-apply follows.
    - On daniel-server and daniel-box, which are `ansible_connection=local`, the operator
      runs the command ON that host (**operator-only**): `uv run ansible-playbook
      ansible/initial_setup.yml --tags <role>`, or `ansible/k3s-bringup.yml` for the k3s
      halves. Never pass `-e target=` for another host, because the header of
      `ansible/inventory/hosts.ini` records that it selects that host's variables and runs
      on the machine you typed it on.
    - For the Pi, the operator runs it from a node over ssh with `-e target=daniel-pi`, as
      today (**operator-only**).
    - **H7.4's `k8s_host_files` half is the exception.** Its two tasks carry `delegate_to:
      daniel-pi` inside a play that runs on daniel-box, so `-e target=daniel-pi` is wrong
      for them. The operator re-applies `k8s_host_files` ON daniel-box, through the playbook
      entry D5 gives it, with no `-e target=`, after the Pi shows the reverted version
      (**operator-only**). The line arm never removes a line, so the authorized key stays in
      force through the revert.
    - **daniel-box while the tick runs.** The revert also restores the role's tasks, and the
      tick may apply them before the updater installs the reverted package. dpkg then deletes
      the same-path files. The operator's re-apply after the version metric moves is
      therefore required on daniel-box too, not optional (**operator-only**).
    - **A per-host PR.** The revert removes that host from `<role>_packaged_hosts` and the
      component from that host's package, and leaves the other hosts as they were. The hand
      re-apply targets that host alone.
    - **An emptying PR.** The revert restores the role's tasks under the playbook line that
      never left, so the hand re-apply needs no playbook edit and the tick never parks.
    - Rollback by re-apply is possible until H7.20a deletes the playbooks.
- **The line-arm PRs.** The arm never removes a line or block, so a revert leaves the edit
  in force, which is the state Ansible left. The hand re-apply rewrites the same line with
  the same regexp or markers. A divert, if one survives H7.3's measurement, is
  forward-only: its revert is a later version that removes the file and the divert
  together, which the divert census enforces.
- **The ufw PRs (H7.7g–i, H7.9a and H7.9b).** A revert drops the declarations, and the arm's
  floor then deletes nothing: an empty set deletes nothing, and `homelab:keep:` rules are
  never deleted. Each kept rule is drift, so the run succeeds, the host does not hold, and
  the reverted version installs and reconciles. The SSH and inter-node rules therefore stay
  in force through a revert. If a
  revert must remove a `keep` rule, the operator re-adds the Ansible rule at the host's
  console before deleting the `keep` rule by hand (**operator-only**). An unwanted
  non-`keep` `homelab:` rule left by a revert is removed by hand.
- **H7.5.** A revert leaves the engine untouched, because the package never moved it. The
  hold stays.
- **H7.4's textfile directory and H7.14's fingerprint file.** Neither is a dpkg-owned path,
  so a revert removes neither. The Pi keeps the directory at the same owner and mode, and
  the restored H3.6 task, re-applied by hand, asserts the same contract. daniel-box keeps
  the last fingerprint file, and the restored role task rewrites it on the hand re-apply.
- **H7.2e.** A revert restores the literal list in the role defaults and the old reads in
  `decide` and the builder together, because one PR changed all of them.
- **H7.12c.** A revert restores the `claude-*` units through the hand re-apply and drops the
  phone's sessions once more.
- **H7.15b.** Put the old SD card back in the Pi. Revert the host-key pin commit, if step 3
  made one. The old age key stays a recipient until the rebuild is accepted, so the old card
  still decrypts.
- **H7.15c, H7.16, H7.17 and H7.18.** Revert. Each changes no live host state, apart from
  H7.18c, whose revert leaves the legacy entries' removals done, which is the intended
  state. That revert also restores `k8s_host_files`' tasks, whose absent arms then remove
  nothing. The guest drill in H7.15c leaves nothing behind, because its teardown destroys
  the guest and its disk on every exit path.
- **H7.19a, per PR.**
    - *The fifth.* Revert it, then the operator re-applies `gitops_deploy` by hand ON
      daniel-box with `uv run ansible-playbook ansible/initial_setup.yml --tags
      gitops_deploy`, after the reverted package shows on daniel-box (**operator-only**).
      The role's playbook line never left, so the revert restores its tasks without parking
      anything. checkout-sync keeps fast-forwarding meanwhile, so the checkout never stalls.
      The revert also restores H6's seven deployer-coupled items in the next versions of
      both cluster-node packages. Their hold check reads `/var/lib/gitops-deploy`, which
      still exists, because the operator removes it only after H7.19b.
    - *The fourth.* Revert it. The reverted package removes checkout-sync, and the
      deployer's timer, which the fourth PR never touched, keeps fast-forwarding. Reverting
      it after the fifth needs the fifth reverted first, or nothing fast-forwards the
      checkout, which `homelab_checkout_sync_success_timestamp_seconds` shows.
    - *The third.* Revert it to restore land.sh's tick step. Restoring it is optional after
      a revert of the fifth, because the timer fast-forwards on its own. The service was
      never removed, because only H7.19b's version lists it, so a landing starts a tick and
      does not end `deploy-failed`.
    - *The second.* Revert it, then the operator re-applies `--tags deploy_ui` ON daniel-box
      after the reverted package shows there (**operator-only**), as for H7.1 to H7.14.
    - *The first.* Reverting it restores the gitops checks and the hostPath mount, which
      needs `/var/lib/gitops-deploy` to exist.
- **H7.19b's legacy entries.** A revert of the deleting PR stops listing the service, the
  code, the config and the polkit rule, but reconcile never restores a retired entry, and
  H7.19a's fifth PR left the role empty. Restoring the deployer therefore reverts H7.19a's
  fifth PR as well, and the operator's `--tags gitops_deploy` re-apply then restores
  every unit and path (**operator-only**).
- **The ufw enable step.** A revert of H7.7g–i leaves ufw enabled, which is the state
  Ansible left. The arm never disables ufw. Disabling it is an operator decision at the
  host's console (**operator-only**).
- **The sysctl keys.** The line arm never removes a line, so a revert of H7.3, H7.6g–i or
  H7.9 leaves each key at its value in `/etc/sysctl.conf`, which the hand re-apply rewrites
  with the same line.
- **H7.19b, H7.19c and H7.20.** Revert. Code and files come back, but no host runs them,
  because every host is package-owned by then and the census proved it. A revert of H7.20
  restores the ability to run a playbook, not any running configuration. A revert of
  H7.20d restores the Pi's empty key and the readers over both keys, and both renders stay
  byte-identical.

**What a partial failure leaves live:**

- A per-role PR whose reconcile arm meets a fault leaves the package installed and the
  arm's state half-converged. H3's updater holds the host: `homelab_host_reconcile_ok`
  reads 0, `homelab_host_applied_info` stays at the record's `last_ok`, and the host
  installs no newer version. The landing ends `deploy-failed` and names the operator's
  `--clear-hold` command. The way out is operator-only: install the fixed or reverted
  version by hand, then run `--clear-hold` (*Which outcomes hold the host*).
- An arm that meets drift leaves the drifted items in place and the run green.
  `homelab_host_reconcile_drift` counts them, and the next reconcile run retries them.
- A validation failure leaves the old config running, the new file on disk, the reboot
  timer refusing, and the host held. The fix reaches the host only through the operator's
  way out of the hold, because a held host installs no next version.
- After H7.19a's fifth PR, a tick already running when reconcile removes the timer
  finishes on its own. No tick starts after it, and `gitops-deploy.service` stays installed
  and idle until H7.19b's version retires it. The same version drops the restart's lock
  step, so a daniel-box k3s restart window that meets that last tick no longer waits for
  it. The tick applies nothing by then (*Overlap*), so the two cannot conflict.
- A Pi rebuild that stops after step 4 leaves a Pi with the keyring and an age key, but no
  engine and no configuration. Re-running the script continues it.
- A checkout-sync that hits a dirty tree stops fast-forwarding, and `homelab_checkout_dirty`
  reads 1. One that sees a red or pending tip advances the checkout to the newest green
  first-parent ancestor within the walk bound. With none, the checkout stays where it is,
  and `homelab_checkout_sync_refused{reason="ci"}` says why. Nothing else depends on its
  success.
- From H7.19b until H7.20a, `deploy_locks.py` keeps its own `TREE_LOCK`, although no
  running code imports it after H7.19a. Until H7.20a deletes it,
  `test_tree_lock_single_definition.py` holds both constants equal to
  `server_git_tree_lock`, so the two copies cannot drift apart.

## Risks

- **The reconcilers are convergence engines this repo owns.** The design names this. Each arm
  keys only on what it labelled or recorded, and each deleting arm has a floor and the tests
  above.
- **A broken builder package breaks its own repair path.** H7.14's break-glass procedure
  builds and installs one package by hand. The repo keeps the previous version, so `apt-get
  install homelab-daniel-box=<previous>` is the other way out.
- **Losing the Pi during the rebuild.** The old SD card is the rollback, the old age key
  stays a recipient, and the cluster's WireGuard path is proven first.
- **An unattended engine swap repeats #1961.** On 2026-09-18 a dist-upgrade replaced the
  engine under running containers, autoheal's shim died, and ssh refused connections for
  about 20 minutes. Mitigation: no versioned `Depends` and no preferences pin, the hold
  stays, the engine moves only through the manual unit's stop, upgrade and recreate, and the
  CI test proves the updater cannot change the engine version.
- **A fleet-wide sshd change locks out a host.** Mitigation: the canary order, `sshd -t` in
  CI and before every reload, and the reboot refusal while validation is red. The Pi lands
  first and is reachable at its console.
- **Cross-repo drift.** The chezmoi half lands in another repository with its own gate
  (`bin/gate`). H7.12a merges the dotfiles PR first, and then this repo's PR, which removes
  the tasks.
- **Failure class 2, the check observes a proxy.** `dpkg --verify` proves files, not live
  config (H7-R1-07). Mitigation: the activation actions, and verification that reads
  `sshd -T`, `sysctl -n`, `zramctl` and `ufw status numbered`. A bring-up drill that staged
  the production repo would pass while a real daniel-box rebuild failed at its first apt
  call (H7-R10-06). Mitigation: the guest builds from a GitHub clone with `--local-build`,
  stages no repo, and its three deviations from a real rebuild are named and guest-local:
  two files and one ufw rule. A rebuilt host could pass every exit check with ufw installed
  but inactive (H7-C3-01). Mitigation: the ufw arm's enable step, and an `active` read in
  exit 2, exit 4's host half and each per-host check.
- **Failure class 3, empty is read as clean.** An empty ufw declared set could read as
  "delete everything" (H7-R1-03), and a generator that finds no tasks prints an empty page
  (H7-R1-16). An empty `ci_required_contexts` makes `ci_verdict` return `pass`, which would
  let checkout-sync merge any tip (H7-C2-04). A walk bound of 0 disarms the walk
  (H7-C3-04), and a token lookup that falls back to anything succeeds with the wrong
  identity (H7-C3-05). An inactive ufw prints an empty rule list, so an order check over it
  passes (H7-C3-01). Mitigation: the ufw floor, the generators' named members,
  checkout-sync's refusals of an empty context set, a non-positive bound and a missing
  credential, and the ufw arm's reads through `ufw show added`.
- **Failure class 2, a later writer overrides the declared one.** `/etc/sysctl.conf` is read
  after every `sysctl.d` file, so a drop-in for a key Ansible wrote there passes its move
  PR's `sysctl -n` check and is silently overridden by the first later change (H7-C3-02).
  Mitigation: each key keeps one mechanism, the line arm on `/etc/sysctl.conf`, and a
  census flags a drop-in that names such a key.
- **A hold on a reverted host.** H3's updater holds a host on any failed reconcile, and a
  held host installs no newer version, the revert included (H7-C2-02). Holding on the ufw
  floor would have wedged a ufw revert until someone edited rules at the console.
  Mitigation: the floor and a refused libvirt undefine are drift, which never holds, and
  the way out of a real hold is a named operator-only procedure.
- **Failure class 4, selector drift.** The setup census, the deletion census, the contract
  census and every reconcile arm name the members they must find. The setup census reads
  every module outside a named control and read-only set, because a fixed list of modules
  missed `systemd_service`, `apt`, `get_url`, `ansible.posix.sysctl` and others
  (H7-C3-03). It counts a `delegate_to:` task against its delegate host. Without the two modules, the strict census could pass while
  `k8s_host_files` still wrote the Pi. Without the attribution, a path in
  `homelab-daniel-box` could resolve an item that lives on the Pi. A per-host guard could
  hide an unmoved item on a host, so the setup census reports a host in the map whose
  package declares nothing. The retired-crons census derives its set from the absent-arm
  tasks, because the list it once named, `retired_host_crons`, does not exist (H7-R10-10).
  The deletion census runs the import rather than grepping for it (H7-R10-04).
- **Failure class 5, sequences treated as transactions.** The ufw replacement adds before it
  deletes (H7-R1-08). A daniel-box revert and the tick can apply in either order, so the
  hand re-apply waits for the version metric (H7-R1-01). The deployer retires across two
  versions, timer first, so reconcile never waits on an in-flight tick (H7-C2-02). land.sh
  stops running the tick in the same PR that removes the timer, because a subprocess
  dependency is invisible to both import checks (H7-C2-01). A role deletion before the deployer retires would edit a bring-up
  playbook, which parks the tick with no fast-forward and holds every other session's
  landing until a hand pulls the primary checkout (H7-R10-02). Mitigation: roles are
  emptied, never deleted, until H7.20a, which merges only after daniel-box lists no
  `gitops-deploy` unit. A `manual_plane` line the tick records is cleared once the version
  metric moves, so no PR leaves a *When to wait* stop behind (H7-R10-08).
- **A move or retirement that another slice's code reads.** Three cases surfaced in the
  integration-2 review, each with its mitigation:
    - a path reader of a moved key: D1's `decide` and H3's builder read the CI contexts
      from the role defaults H7.2e rewrites (X2-02). H7.2e repoints both in the same PR, the
      key grep lists every reader by key name, and the deployer-import census keeps the
      old key names out after H7.19b;
    - checks in H6 that read the deployer's lock and state directory (X2-04). The fifth
      H7.19a PR removes all seven before H7.19b names the directory for cleanup and before
      H7.20a deletes `deploy_locks.py`, and checkout-sync's monotonic timer joins the window
      test's exemption list;
    - files that a role writes outside its units: the builder's fingerprint file and
      `publish-textfile`, and the Pi's textfile directory (X2-05). H7.4 and H7.14 move each
      one into the package with a container test, and the Pi runbook's step 8 reads the
      metric that C6's host wait reads.
- **Failure class 6, restart is not recreate.** The engine's shim swap (H7-R1-02) and config
  that no reload applies (H7-R1-07). Mitigation: the manual engine unit recreates the
  project, and each file declares its activation.
- **Failure class 7, execution context.** Push mode removes the `-e target=` trap once the
  playbooks go. Until then, *Rollback* says where each command runs. Each host installs only
  its own package, and the `preinst` refuses a hostname mismatch.
- **Failure class 8, prose that nothing re-derives.** Every setup role's `CLAUDE.md` holds
  ENFORCED citations into `ansible/tests/setup/`. Each per-role PR rewrites or archives that
  doc. Until G2 retires `docs/facts.lock`, the same commit runs `fact_status.py forget` for
  each section key it removes and `verify` for each new key, then commits the lock. A moved
  heading reads as `section-gone`, so `verify` alone leaves the old row behind. After G2,
  the PR reads G2's notice instead.

## Tests and docs that change in the same PR

- **Every PR** runs `git grep -l -E 'initial_setup\.yml|bootstrap\.yml|k3s-bringup\.yml|ansible-playbook'`
  over `CLAUDE.md`, `.claude/`, every role `CLAUDE.md` and `docs/`, and lists each hit its
  subject touches as edited, archived or exempt in the PR body. A generated page under
  `docs/reference/` is never hand-edited; its generator changes in H7.18a.
- **Every per-role PR:**
    - that role's modules in `ansible/tests/setup/` retarget the rendered component or are
      deleted, if they test only Ansible mechanics;
    - its `CLAUDE.md` moves into the component's `CLAUDE.md` or into `docs/`, and an
      emptied role keeps a stub `CLAUDE.md` that points at the component;
    - `gen_role_glance.py` stops listing it;
    - its `pyproject.toml` `extra-paths` entry goes;
    - until G2 retires `docs/facts.lock`, `uv run python scripts/dev/fact_status.py forget
      '<old key>'` for each section key the move removes, `verify '<new key>'` for each new
      section, and a commit of the lock.
- **H7.1:** `docs/break-glass.md` records the age-key decision.
- **H7.2e:** `pyproject.toml`, root `CLAUDE.md`'s `_CI_NO_VERDICT_CONCLUSIONS` citation,
  `ansible/tests/deploy/test_ci_cancelled_is_not_a_verdict.py`,
  `ansible/tests/repo/test_ci_contexts_match_workflows.py` and
  `ansible/tests/repo/test_every_ci_job_is_gated.py` for `ci_required_contexts`,
  `await_ci.py`'s comment, the comments in `.github/workflows/ci.yml` and
  `.github/workflows/renovate-config-canary.yml` that name the old key, D1's `decide` tests
  in `scripts/deploy_tools/tests/`, whose empty-list fixture empties `ci_required_contexts`,
  the builder's tests for its context list and walk bound, the deployer's own
  `tests/test_deploy_git_ci.py`,
  `ansible/tests/setup/test_host_lib_sibling_copies.py`'s `HOST_LIB_SRC`,
  `ansible/tests/deploy/test_gitops_deploy_ship_list.py` for the new `ci_verdict.py` entry,
  and `ansible/roles/setup/common/CLAUDE.md`'s table rows for the two modules and its
  routing paragraph. Until G2 retires `docs/facts.lock`, run `uv run python
  scripts/dev/fact_status.py verify '<doc>#<heading>'` for each edited section, `forget` for
  each removed one, and commit the lock.
- **H7.4:**
    - `k8s_host_files`' `CLAUDE.md` loses the two Pi rows;
    - `ansible/roles/k8s/pi-peer-backup/CLAUDE.md`'s *Auth* line, which says the role
      authorizes the public half, names the `pi-peer-backup-target` component;
    - `ansible/tests/services/test_pi_peer_backup_forced_command.py` follows the wrapper's
      path;
    - the line-arm tests in `hosts/lib/tests/` gain a case where a rotated key replaces the
      line anchored on `pi-peer-backup-cronjob`, and a case where the rewrite keeps the
      file's owner and mode and a missing file is created 0600 under a 0700 `~/.ssh`;
    - the container test for the Pi's textfile directory, and `optimize_pi/CLAUDE.md`'s
      `node-exporter-host` rows, which move into the component's `CLAUDE.md` with the
      directory's owner contract.
- **H7.5:** `docs/docker-engine-pins-and-runtime.md` replaces the `docker-engine-upgrade`
  play with `homelab-docker-engine.service` and keeps the hold.
  `scripts/tests/test_renovate_docker_engine_pins.py` follows the pins' path. H5's
  `pi-compose` component `CLAUDE.md` names the `ExecStartPre=` that creates the external
  networks, and the CI fixture records Compose's behaviour with an unlabelled network.
- **H7.6d–f:** `docs/host-baseline-record.md`'s sshd sections.
- **H7.8:** `ansible/roles/k8s/nut/CLAUDE.md` records the pinned ClusterIP.
- **H7.10a:** `ansible/roles/k8s/docs/CLAUDE.md`, which says "The role's own task creates
  the directory", names the docs-refresh component's `tmpfiles.d` line.
- **H7.12c:** `docs/claude-agent-user.md`, `docs/claude-code-rc-caps.md` and
  `docs/landing.md` for the `claude-land@` rename, and the tests that name the unit, such
  as `scripts/deploy_tools/tests/test_land_handoff.py`.
- **H7.13:** `docs/hypervisor-libvirt-internals.md`, which keeps the agent's env file as the
  drill's token source.
- **H7.14:** `docs/break-glass.md` gains the builder procedure. The builder's tests follow
  it into the component: the keyring check, `publish-textfile`'s prefix filter and the
  once-per-condition record, plus the fingerprint script's two cases. `docs/host-packages.md`
  names the `ExecStartPre=` script as the fingerprint file's writer.
- **H7.15b and H7.15c:**
    - `docs/break-glass.md` gains the Pi runbook and the cluster-node procedure, and item 2
      names the agent's env file as where the token goes on a daniel-server rebuild;
    - the cluster-node procedure links R4 PR 7b's *Bootstrapping Flux on a rebuilt
      cluster* and restates none of its steps;
    - `docs/longhorn-disaster-recovery.md` step 2 and `docs/k3s-etcd-restore.md` point at
      the cluster-node procedure;
    - `ansible/README.md`'s bring-up section points at `bootstrap-host.sh`;
    - `CLAUDE.md`'s host list loses `-e target=daniel-pi`;
    - the `add-secret` skill's bootstrap section and root `CLAUDE.md`'s *Secrets
      Management* line about `bootstrap.yml` point at `bootstrap-host.sh`;
    - `.claude/rules/` entries that name playbooks change with it.
- **H7.18a:** the generators under `scripts/docs/` and `scripts/secrets_mgmt/secret_rotation.py`,
  with their tests in `scripts/docs/tests/`. ADR-0001 is listed below.
- **H7.17:** the tests the Baseline lists as importing ansible, converted or deleted; the
  `_helpers.py` filter census and the deletion census in `ansible/tests/repo/`; and R4's
  ansible-free render test, extended to host and package modes.
- **H7.18c:** `k8s_host_files`' `CLAUDE.md` becomes a stub, and D5's render test leaves.
- **H7.19a**, by PR:
    - the first edits monitor-bridge's `CLAUDE.md`, whose *Host* line names the gitops state
      as the pin's reason, and `docs/monitor-bridge-checks.md`'s rows for the two checks;
    - the second turns deploy_ui's `CLAUDE.md` into a stub and deletes its tests;
    - the third cuts `land-after-merge` to C6's waits, updates `docs/landing.md`, and
      deletes or rewrites the land tests whose subject is the tick, among them
      `test_gitops_tick_wrapper.py` and `test_land_abandoned_tick_watch.py`;
    - the fourth adds the checkout-sync component's `CLAUDE.md`, which carries its
      contract, the CI gate, the empty-set, bound and token refusals, and the decision not
      to read `DEPLOY_HOLD`, which the H7 ADR records;
    - the fourth also adds `homelab-checkout-sync.timer` to the exemption list of H6's
      window test, with its exclusion;
    - the fifth moves the gitops_deploy role's `CLAUDE.md` to `docs/archive/`, leaving a
      stub, with `docs/gitops-pipeline.md`'s remaining host-plane sections, rewrites root
      `CLAUDE.md`'s *After a PR Merges* and *When to wait*, and retires the `gitops-tick`
      skill. It also removes H6's deployer-coupled tests (the lock-path equality test, the
      `held_sha` parity case and the `gitops-deploy.timer` exemption), and edits the
      `k3s-restart` component's `CLAUDE.md` wherever it names the lock step or the
      server-mode hold check.

  Until G2 retires `docs/facts.lock`, each PR runs `fact_status.py verify` for each edited
  root `CLAUDE.md` section and commits the lock.
- **H7.2e**, for the walk bound: `config.env.j2` keeps rendering the role default, which
  now refers to `ci_ancestor_walk_max`, so `gitops_deploy/tests/test_deploy_config.py`
  passes unchanged, and the PR body quotes it passing.
- **H7.19b:** `ansible/tests/deploy/test_tree_lock_single_definition.py` and
  `test_k8s_lookup_readers.py` follow their modules to `scripts/lib/`; the land_lib tests
  that import `deploy_cross_role` or the state modules, such as `test_land_classify.py` and
  `_land_fakes.py`, follow their subjects; `test_gitops_markers_copies.py` goes with the
  copies; the deployer-import census row in `ansible/tests/repo/test_census_rows_python.py`;
  the doc that transcludes the gitops fragment, and the fragment file;
  `.claude/hooks/session-health.py` and its tests in `.claude/hooks/tests/`; the `deploy`
  skill; `docs/landing.md` again, for the setup and Pi arms; `docs/deploying.md`; root
  `CLAUDE.md`'s *Common Commands*. Until G2
  retires `docs/facts.lock`, run `uv run python scripts/dev/fact_status.py verify
  '<doc>#<heading>'` for each edited root `CLAUDE.md` section and commit the lock.
- **H7.20:**
    - the ADR, which records the package-per-host design, checkout-sync's narrowing to the
      CI gate without `DEPLOY_HOLD`, and the decision to keep the agent on the server
      token, and amends ADR-0003 and ADR-0013 where it touches them;
    - `docs/claude-shell-permissions.md`'s ansible-playbook rows;
    - `pyproject.toml`, `uv.lock`, `prek.toml` and `.gitignore`;
    - in H7.20d, H5's service-list census, `_example.yml`'s comment, the `pi-heartbeats`
      component header whose contract scope names both keys, and each doc that the two
      census greps list.
- **ADRs, each operator-only.** Approving the H7 ADR is **operator-only**. The operator also
  decides, **operator-only**, whether each of these needs an amendment:
    - ADR-0001, because its generators read the Ansible tree until H7.18a;
    - ADR-0011 and ADR-0017, because `gitops-deploy.service` and `deploy.sh` leave the lock
      holders in H7.19, while `homelab-checkout-sync` and the checkout timers keep the tree
      lock.
- **Added by the pillar review fold:**
    - H7.2a: the reconcile-size census and its ceiling constant, in `hosts/lib/tests/`;
    - H7.13: `docs/hypervisor-libvirt-internals.md` names the rehearsal mode that moves
      with the role;
    - H7.15a: the offline container case and its two red fixtures, and the kit's offline
      file list in `docs/break-glass.md`;
    - H7.15c: the cluster-node procedure in `docs/break-glass.md` gains the offline branch;
    - H7.15d: `docs/host-packages.md` gains the host-addition walkthrough, and the host census
      joins `hosts/lib/tests/`;
    - H7.15e: `docs/host-packages.md` gains the release-upgrade procedure.

## Complexity delta

- **Removed:**
    - ansible-core and four collections;
    - the 408 tracked files under `ansible/roles/setup/` and `ansible/roles/containers/`
      that remain after H1 to H6;
    - about 290 lines of bring-up playbooks, plus `bring-up.sh`;
    - the GitOps deployer, setup narrowing, `deploy.sh` and `land.sh`'s setup and Pi arms;
    - land.sh's tick step with `tick.py` and `gitops_tick.sh`, and monitor-bridge's two
      gitops checks with their hostPath mount;
    - H6's seven deployer-coupled items: two server-mode checks, the restart's lock step,
      the lock's `tmpfiles.d` entry, two test cases and one window-test exemption;
    - the local and ssh inventory model;
    - every `has_X` teardown arm;
    - `setup-drift-check.sh` and its timer, which the version metric and `dpkg --verify`
      replace;
    - the absent-arm cron tasks and `docker_install_stale_crons`, as legacy entries;
    - ansible-core's filter set in the test env, which `_helpers.py` registered for 361
      modules;
    - D5's interim setup role `k8s_host_files`, and its `delegate_to` writes into the Pi;
    - the Pi's `containers_list` key and the readers' two-key union.
- **Added:**
    - four reconcile arms (ufw, libvirt, users and lines), the activation step and the
      kubeconfig oneshot. The ufw arm's enable step replaces one Ansible task, and the
      line arm takes the seven `ansible.posix.sysctl` tasks with no new form;
    - the builder's fingerprint script in place of the role's fingerprint task, one
      `tmpfiles.d` line for the Pi's textfile directory in place of H3.6's task, and one
      `systemd-tmpfiles --create` call in bootstrap step 5;
    - one key grep at H7.2e, and two old key names among the deployer-import census's
      string constants;
    - one sysctl-key census and two checkout-sync refusals, for a non-positive walk bound
      and a missing credential, with one more `group_vars` key and one `LoadCredential=`
      line, and no new SOPS key;
    - one guest-local ufw rule in the bring-up drill, its third deviation;
    - one drift metric and one rule that sorts each arm outcome into a fault, which holds
      the host, or drift, which does not. H3's updater is unchanged. The alternative, a
      third "pending" result that makes H3's updater rerun reconcile on every fire, would
      amend H3's record and updater for the one case the two-version retirement removes;
    - the apt-source components, and nfpm in H3's `homelab-tools`;
    - the reconcile-size census and its ceiling, and the host census;
    - `--offline` in `bootstrap-host.sh`, with the kit's offline file list;
    - two docs-only procedures: adding a host and upgrading a host's Ubuntu release;
    - `homelab-checkout-sync` and `homelab-docker-engine.service`;
    - `bootstrap-host.sh`, complete, with the Pi runbook and the cluster-node procedure;
    - four operator commands in place of three playbooks and one runbook role;
    - the setup census, the deletion census, the deployer-import census, the component
      contract census and the retired crons census, plus the `_helpers.py` filter census;
    - three small `scripts/lib/` modules moved out of `deployer_lib` (`tree_lock`,
      `k8s_lookups`, and `role_paths` only if a survivor needs it), and the ancestor walk
      in checkout-sync, which reuses the deployer's `ci_walk_candidates` rather than adding
      logic;
    - one `<role>_packaged_hosts` default per role that moves per host, until its last host
      PR deletes it;
    - nfpm, uv and k3s pins in `bootstrap-host.sh`, held equal to the package pins and to
      H6's `system-upgrade` keys by a test;
    - one ADR.
- **Kept:** the primary checkout on daniel-box and `homelab-checkout-sync`, which *Open
  questions* 12 proposes to retire, and the `ansible/` tree name, which *Open questions* 13
  proposes to change.
- **Not added:** no `dpkg-divert` lifecycle and no change to H3's maintainer-script
  whitelist, because every owned path measured so far is a conffile, which the line arm
  edits as Ansible does. No agent-scoped k3s token, and no new SOPS key for it.
- **Reboot activation.** Reusing the weekly reboot timer adds one refusal check and one
  metric. A separate windowed reboot unit would add a second timer, its own window and its
  own Healthchecks.io check, for a reboot that the weekly timer already performs within a
  week.
- The design's net for Area 3 holds with two changes: a fourth reconciler, the line arm, and
  `homelab-tools`, which C5 assigns to H3.
  The rest stays as the design lists it: one CI workflow, 4 package specs,
  5 shared units, a version monitor, SUC with 2 Plans, `bootstrap-host.sh`,
  a LAN builder and its key, and the CI package tests.

## Open questions

1. **C5 lacks a package for architecture-specific tools.** Resolved by the C5 amendment
   that the pillar review fold applied. `homelab-tools` is C5's one architecture-specific
   package, H3 builds it, and the Pi is its `arm64` pilot. H7.1 adds nfpm to it and
   retires `sops_setup`.
2. **The window between D6 and the end of H7.** Resolved, with one condition. D6 keeps the
   deployer, so the tick keeps applying daniel-box's setup plane until H7.19a retires it,
   after the last daniel-box role is packaged. No window of hand applies on daniel-box
   opens, provided no PR before H7.20a edits a bring-up playbook. A range that edits one
   parks the tick with no fast-forward (Baseline), so roles are emptied rather than
   deleted, and H7.20a, which deletes the playbooks, merges only after the deployer is gone.
3. **Who owns the checkout-sync timer?** Resolved. H7.19a ships it in `homelab-daniel-box`
   in the same PR that retires the deployer, and both take the tree lock. It keeps the
   deployer's CI gate and does not read `DEPLOY_HOLD`, a narrowing the H7 ADR records.
4. **The renderer's ansible-core imports.** Resolved for cluster mode by R4: PR 10
   reimplements the filters, and R4's exit check 4 proves the cluster render runs with
   ansible-core unimportable. Host and package modes, and the test env in
   `ansible/tests/_helpers.py`, are H7.17's, which proves both with the same subprocess
   technique and the filter census.
5. **ADR numbering.** D6, G2 and H7 each add an ADR. Resolution: each takes the next free
   number at merge time, and none reserves one.
6. **`deploy_ui`.** The design leaves open whether it retires with the deployer. Resolution:
   D6 decides, and H7.11 packages it only if it stays.
7. **A fourth reconciler.** The design names three reconcilers, and this plan adds the line
   arm for files that maintainer scripts generate and for conffiles another package owns.
   Resolution: the design's count changes to four when the ADR is written. The alternative,
   a divert, does not stop a maintainer script from rewriting an unowned file, and on a
   conffile it is what H4-R1-04 rejected.
8. **Should the engine upgrade become windowed?** The manual unit keeps today's rule that an
   operator moves the engine. Resolution: the operator decides in H7.5's review
   (**operator-only**). A windowed timer could later start the same unit, because the unit
   already stops and recreates the project.
9. **Diverts and H3's maintainer-script whitelist.** H3 leaves the divert lifecycle to H7
   and admits no `dpkg-divert` line. Resolution: H7 ships no divert, so H3's `preinst` rule
   and whitelist stay as they are. If H7.3 or H7.5 finds an owned path that is not a
   conffile and that no unit drop-in can redirect, the lifecycle in *In-place edits* lands
   in its own PR before that move, and the same PR amends H3's `preinst` rule and whitelist
   lint. H3's plan text then needs the matching note.
10. **The agent's join token (H6 Open question 4).** Resolved: the agent stays on the
    server token, held in the installer's env file and restored out of band on a rebuild
    (*The agent keeps the server token*). design.md's sentence that H7's fresh join decides
    needs to name this outcome.
11. **Is the bring-up guest a C8 substitute?** Resolved without amending C8. The Flux half
    runs on kind, C8's first substitute. The host half is C8's third, an **operator-only**
    drill on production hardware, whose PR states the blast radius *The proofs* gives. It
    runs no homelab workload and no Flux, and no production credential reaches it.
12. **The primary checkout outlives the deployer.** `homelab-checkout-sync` keeps a git
    checkout on daniel-box current: a 10-minute timer, the tree lock, a dirty-tree refusal
    and the CI ancestor walk. That is the pillar review's failure class E, a host whose
    behaviour depends on mutable working-tree state. Recommendation: a later slice runs
    the checkout-reading crons from a package or an image built at the applied commit. A
    cron that commits, such as docs-refresh, keeps a private clone under its own state
    directory, not the operator's checkout. checkout-sync, its lock, its refusal and its
    walk then retire. Resolution: the H7 ADR lists the primary checkout under what
    remains, with this recommendation, and the operator rules on the later slice
    (**operator-only**).
13. **The tree still reads as Ansible after H7.** The keep-list leaves `ansible/vars/`,
    `ansible/inventory/`, `ansible/filter_plugins/`, `ansible/templates/`,
    `ansible/tests/`, and each k8s role's `templates/`, `defaults/` and `files/` under
    `ansible/`. A reader then looks for playbooks that no longer exist. Recommendation: one
    move PR after H7.20 renames the tree to a neutral root that the operator picks
    (**operator-only**), with `git mv` only and every path constant changed through
    `scripts/lib/repo_paths.py`. The same PR moves `ansible/.sops.yaml` beside `vars/`,
    because SOPS searches upward from the file, and updates `.gitleaks.toml`, the Renovate
    rules, the prek hooks, the CI path filters and the `docs/facts.lock` rows. Resolution:
    the H7 ADR records the follow-on. The PR runs when no other slice has a branch open,
    because every open branch conflicts with it.
14. **Two offline items need more than a kit file.** The offline branch takes every fetch
    from the kit, but two items are not files the script installs.
    - The Flux controller images come from GHCR. Recommendation: the kit carries them as
      image tarballs at D1's pinned digests, and the procedure places them in k3s's image
      import directory before k3s starts, so the node imports them at start.
    - C2's break-glass promotion pushes to the in-cluster registry. On a fresh cluster that
      registry is a Flux workload that is not running yet. Recommendation: D6's runbook
      applies the registry's rendered manifests from the repo bundle first, with its image
      from the kit, and pushes the artifact after.

    Resolution: D6's break-glass drill on kind adds a fresh-cluster case with no registry
    and no GHCR, and H7.15c's PR quotes it. This is a cross-slice request to D6.

## Review log

### Round 1 (per-slice adversarial review)

25 findings: 3 critical, 10 high and 12 medium. All 25 are fixed, and none is refuted. Four
fixes use a different mechanism from the one the finding proposed, and each says why.

- **H7-R1-01 (critical) False premise about D6; checkout-sync has no owner.** Fixed.
  *Baseline* now quotes D6's *Goal*, table row and exit 2. H7.19a retires the deployer
  through legacy entries and ships `homelab-checkout-sync` in the same PR, and H7.19b
  deletes setup narrowing, `land.sh`'s setup and Pi arms, `deploy.sh` and the session-health
  arms. H7.2e re-homes `ci_verdict`, and the `gitops_hold` and `gitops_markers` readers are
  deleted or switched to `DEPLOY_HOLD` in H7.19b. *Rollback* states the daniel-box order
  while the tick runs. ADR-0011 and ADR-0017 are listed as **operator-only** decisions. Open
  questions 2 and 3 are resolved. Changed mechanism: "exactly one writer at every commit"
  cannot hold, because the updater and the tick run on independent schedules. The overlap
  is made harmless instead: both take the tree lock, a fast-forward-only merge is
  idempotent, and retirement waits for an in-flight tick.
- **H7-R1-02 (critical) H7.5 makes engine upgrades unattended.** Fixed with option (b),
  adjusted. No versioned `Depends` and no preferences pin, because a pin above 1000 forces
  a version change. The hold stays, and `homelab-docker-engine.service` is a `manual` unit
  that stops, upgrades and recreates. #1961 is named in *Risks*, and a CI test with a red
  fixture shows that the updater cannot change the installed `docker-ce` version.
- **H7-R1-03 (critical) The ufw arm deletes every undeclared rule.** Fixed. The arm has a
  floor: no deletes on an empty set, at most two per run, never a `homelab:keep:` rule, and
  legacy deletes only after the replacement is present. Red proofs cover the empty set and
  the revert fixture. *Rollback* for the ufw PRs keeps the SSH and inter-node rules, and
  removing a `keep` rule is an **operator-only** console step.
- **H7-R1-04 (high) The exit pathspec matches the whole tree.** Fixed. Measured: the plain
  pathspec prints 288 files and `':(glob)ansible/*.yml'` prints 10. Exit 1 uses the glob
  form, names every playbook-only directory, and states the keep-list.
- **H7-R1-05 (high) The wholesale delete removes imported modules.** Fixed. *What goes
  where* has a `common` row, and H7.2e re-homes the modules from a derived list and updates
  `pythonpath` and `extra-paths`. Added from the tree: `longhorn_lib`, which
  `longhorn_groups.py` imports from `roles/setup/k3s/files`. Changed mechanism: the render
  proof uses `homelab-render cluster` at base and head with `diff -r`, because R4 PR 9
  deletes `shadow_diff.py`.
- **H7-R1-06 (high) In-place edits on owned files.** Fixed. *Design* assigns each of the 29
  edits a mechanism: 6 drop-ins, 14 diverts, 8 line-arm edits and 1 chezmoi edit, using
  `dpkg -S` and `dpkg-query` output from daniel-box. No `login.defs.d` is assumed, so
  `login.defs` is diverted. Pi owners are measured in H7.3 and H7.5. The CI container test
  shows the "trying to overwrite" failure and the divert's success.
- **H7-R1-07 (high) Moved config is never activated.** Fixed. Each file declares an
  `activate:` action, and every kind has a red proof. Verification reads live state.
  Changed mechanism: a reboot-class change waits for the weekly reboot timer, which refuses
  while validation is red, rather than for a second windowed unit. *Complexity delta*
  compares the two.
- **H7-R1-08 (high) The ufw arm ignores order and the DECIDED marker.** Fixed. Rules carry
  `position` and `before:`, the design cites the marker, the arm manages the incoming and
  outgoing defaults, every Ansible-era rule is listed by spec and comment (corrected for
  `deploy_ui`, `docker_install` and `optimize_pi`), and the two-writer census covers ufw
  specs. A red proof flags an allow after its limit rule.
- **H7-R1-09 (high) The Pi rebuild needs more than the script.** Fixed. Exit 2 is now the
  runbook: hostname preset, files onto the boot partition, host keys and age key from the
  kit, wg-easy's `config/` restored before step 5 with `--until 4`, and the old key kept as
  a recipient (**operator-only**). The rollback names the commits it reverts. pi-peer-backup's
  Pi-side wrapper and key, which no slice moved, join the Pi package in H7.4.
- **H7-R1-10 (high) No proven bring-up for the cluster nodes.** Fixed. Bootstrap passes
  `INSTALL_K3S_EXEC` explicitly with `INSTALL_K3S_SKIP_ENABLE` and asserts the unit set.
  daniel-box uses `--local-build`. The procedure restores the server token and creates
  `sops-age` and `homelab-cluster-vars` (**operator-only**), and H7.15c proves it in a drill
  guest with a staged repo copy, because the guest's nwfilter drops the LAN. The two DR
  docs change in the same PR.
- **H7-R1-11 (high) Roles and components with no PR.** Fixed. H7.2f's census derives the
  checklist from the glance blocks and the tasks, and H7.19c makes it strict. H7.9a moves
  daniel-server's k3s half. H7.1 moves `sops_setup`, H7.2e and the table move `common`, and
  H7.12a moves `github_cli` and `chezmoi_setup`. The three named crons and the two kuma-check
  timers have PRs, and H7.12c lists every `claude_code` unit.
- **H7-R1-12 (high) Agent users cannot move to chezmoi.** Fixed. The agent users join
  `sysusers.d` with their live uids, because sysusers creates only missing users. A package
  unit that runs as the agent installs its tooling. Bootstrap step 5 installs chezmoi. H7.12c
  names the unit rename, the session drop and every `claude-land@` caller.
- **H7-R1-13 (medium) The drill's inputs move.** Fixed in H7.13: `LoadCredential=`, the S3
  env file through `homelab-secrets`, the nwfilter in the libvirt arm, one naming rule
  (recorded names only, with `staging` kept), and the S3 folder stated.
- **H7-R1-14 (medium) H7.17's filter list and skip condition.** Fixed. H7.17 deletes or
  converts R4's parity tests, removes `claude_agents.py` or its import, and confirms
  `k8s_retire.py` and `k8s_autodeploy.py` are gone. *Baseline* carries the measured filter
  list, and Open question 4 points at R4's exit check 4.
- **H7-R1-15 (medium) The deletion census is unscoped.** Fixed. Two rows with named members,
  one in `ansible/tests/repo/test_census_rows_python.py` and one in
  `ansible/tests/repo/test_census_rows_text.py` with the stated exemptions.
- **H7-R1-16 (medium) Generators come back empty.** Fixed by H7.18a, with named members.
  ADR-0001 is an **operator-only** decision.
- **H7-R1-17 (medium) Docs, skills and hooks are missing.** Fixed. Every PR runs the grep and
  lists each hit. The skills, the session-health hook and root `CLAUDE.md` have PRs.
- **H7-R1-18 (medium) PR size and no canary.** Fixed. H7.6, H7.7 and H7.10 are split per
  subject and per host in the order Pi, daniel-server, daniel-box. CI parses sshd, sysctl,
  journald and fail2ban config, each with a red proof. H7.20 is sized by subject.
- **H7-R1-19 (medium) The user-home age key.** Fixed in H7.1. daniel-box's key stays, because
  the rotation cron runs there as `sys_user` (`when: has_gitops`). daniel-server's copy goes
  only after an **operator-only** check.
- **H7-R1-20 (medium) Retired crons are deleted without legacy entries.** Fixed by H7.18c,
  with a census.
- **H7-R1-21 (medium) The `-e target=` trap in the rollback.** Fixed. Commands run ON
  daniel-server and daniel-box, the Pi keeps ssh, and the rollback ends at H7.20a.
- **H7-R1-22 (medium) `ansible_host` is read by a cluster template.** Fixed by H7.18d, with
  the render diff in place of `shadow_diff.py`.
- **H7-R1-23 (medium) Contract checks become vacuous.** Fixed. The component contract census
  has named members and a red fixture, and the old tests retire in H7.20c.
- **H7-R1-24 (medium) The fsd drill powers off both nodes.** Fixed. *Verification* states the
  blast radius, the window, the **operator-only** mark and the preconditions.
- **H7-R1-25 (medium) Unnamed failure classes.** Fixed. *Risks* names classes 2, 3, 5 and 6
  with mitigations and finding links.

Found while fixing, and fixed here: R4 leaves the `volume-revert` and `longhorn-api`
runbook tasks to H7 (H7.16b, and exit 1 now lists `ansible/roles/*/*/tasks/*`). The lint is
the `ansible-lint` prek hook, not a CI job, so exit 1 greps `prek.toml`. Root `CLAUDE.md`
cites `deploy_git.py:_CI_NO_VERDICT_CONCLUSIONS`, which H7.2e moves.

### Integration round (cross-slice review)

Four findings that name H7, all medium. All four are fixed, and none is refuted. Each claim
was checked against the tree and the other slice plans before the fix.

- **X-14 (medium) No slice retires D5's `k8s_host_files`.** Fixed. Measured: `git ls-files
  ansible/roles/setup/k8s_host_files | wc -l` prints 0 at `70591c307`, and H7 named the role
  nowhere. D5's *Host-side rows* hands H7 the docs site directory and the absent arms. H5's
  *Open questions* 3 leaves pi-peer-backup's two Pi writes to H7.
    - *Baseline* gains a bullet listing the role's rows and who takes each. The crowdsec
      probe timer and the Alloy stage are H5's or D5's, and H7 depends on both.
    - *What goes where* replaces the `k8s/pi-peer-backup` row with a `k8s_host_files` row.
    - H7.4 takes the wrapper, and takes the authorized key through the line arm, anchored
      on its comment, `pi-peer-backup-cronjob`. The same PR deletes the `delegate_to` tasks
      and repoints the key's rotation redeploy.
    - H7.10a ships the docs site directory as a `tmpfiles.d` line. The docs-refresh service
      runs as `sys_user`, so its `systemd-tmpfiles` call carries the `+` prefix.
    - The line arm keeps the authorized-keys file's owner and mode, because the file also
      holds the operator's login key.
    - H7.18c turns the absent arms into legacy entries and deletes the role.
    - The deletion list and the tree diff name the role. *Rollback* says to re-apply it ON
      daniel-box with no `-e target=`.
    - Found while fixing: the setup census read no `file` or `authorized_key` task and
      counted a task against its play's host. It now reads both modules and counts a
      `delegate_to:` task against its delegate host. A red fixture covers the attribution,
      and *Risks* names the gap under failure class 4.
- **X-15 (medium) Rebuild runbooks create `homelab-cluster-vars` by hand.** Fixed for H7.
  D3's design has the `cluster-vars` Kustomization's `secretGenerator` produce the Secret
  from `cluster-vars.sops.env` with `sops-age`. The cluster-node procedure now applies
  `gotk-components`, creates only `sops-age` (**operator-only**) and applies the sync
  objects. It forbids a hand-made `homelab-cluster-vars` and follows R4's bootstrap section.
  Exit 4 and *Verification* add a read-only check: `cluster-vars` reads Ready, and the
  Secret carries Flux's `kustomize.toolkit.fluxcd.io/name` label, which shows Flux is its
  writer. A restore whose `sops-age` predates a key rotation fails the Ready check, and the
  operator then replaces `sops-age` (**operator-only**). H7-R1-10's wording above is
  history, and this round corrects it. Rotating the cluster keypair belongs to D3.4.
- **X-19 (medium) Nobody removes the Pi's `containers_list`.** Fixed.
    - New step H7.20d deletes the key from `daniel-pi.yml` and `_example.yml`. It points
      every two-key reader at `compose_services` alone and rewrites H5's census. It lands
      after H7.20a, because `preflight.yml`, `deploy.sh`, `land.sh`'s Pi arms, `deploy.yml`
      and a rollback re-apply read the key until then.
    - The proof is an empty `diff -r` over the cluster render and the Pi's host render.
    - Exit 1, *What the deletion touches* and the tree diff state that `compose_services`
      stays in the inventory as renderer data. Exit 1 gains `git grep -n
      '^containers_list:' -- ansible/inventory`, which must print nothing.
    - The C4 amendment goes to the contracts as a note.
- **X-21 (medium) `facts.lock` steps ran unconditionally.** Fixed at four sites: the
  shared-modules design, H7.2e's and H7.19b's docs rows, and failure class 8 in *Risks*.
  Each now starts "Until G2 retires `docs/facts.lock`", in R4's wording. After G2, the
  H7.2e citation repoint still stands, because G2 keeps `fact_status.py lint`, which fails
  a citation that does not resolve.

### Round 10 (per-slice adversarial review)

15 findings: 6 high and 9 medium. All 15 are fixed, and none is refuted. Each claim was
checked against the tree at `70591c307` and the sibling plans first. Five fixes take a
mechanism the finding offered as its alternative, or a narrower one, and each says why.

- **H7-R10-01 (high) H7 relied on an agent join token that H6 no longer moves, and never
  made H6's agent-token decision.** Fixed with the finding's second option. Confirmed: H6
  *Design* says "The agent keeps its token where it is", and `grep -rn
  k3s_agent_join_token docs/target-architecture` matched only H7 and H6's R1-02 row.
  *Baseline* now quotes H6. The drill keeps reading
  `/etc/systemd/system/k3s-agent.service.env` (H7.13, exit 3). Bootstrap step 4 passes
  the operator's `K3S_URL` and `K3S_TOKEN` to `install.sh`, which writes that file, and
  marks that **operator-only**. Every `token-file`, credstore and `k3s_agent_join_token`
  mention is gone. *The agent keeps the server token* closes H6 Open question 4 with three
  reasons, Open question 10 records it, and exit 4 names each token source. Found while
  fixing: the agent also needs `K3S_URL`, and `install.sh` rewrites the env file, so the
  guard skips the installer on a rerun.
- **H7-R10-02 (high) Role deletions before H7.19a edit a bring-up playbook, which parks
  the deployer.** Fixed. Confirmed: `_BROAD_MANUAL_PREFIXES` holds all three bring-up
  playbooks, and the `deploy_defer.py` docstring says "`cs.broad_manual` still parks". The
  new *Roles are emptied before H7.20a* reduces a role to an unconditional `meta:
  end_role`, H4's pattern, keeps its playbook line and a stub `CLAUDE.md`, and `git mv`s
  what a component renders. H7.4, H7.18c and H7.19a now empty their roles. H7.20a deletes
  every role directory and the playbooks together, only after daniel-box lists no
  `gitops-deploy` unit, and proves it with host and package `diff -r`. *daniel-box while
  the tick runs*, Open question 2 and failure class 5 are corrected.
- **H7-R10-03 (high) The divert design cited an H4 pattern that no longer exists, and
  broke H3's maintainer-script rules.** Fixed with a narrower mechanism. The H4 citation is
  gone. All six measured divert targets are conffiles that Ansible already edits in place
  (Baseline), and diverting a conffile is what H4-R1-04 rejected. So those edits move to the
  line arm, whose refusal narrows from any owned path to an owned path that is not a
  conffile. The arm gains a block form for `zshenv` and the containerd config, and two
  owning-package upgrade cases with a red fixture. `/usr/local/bin/log2ram` takes the line
  arm or a homelab copy behind a unit drop-in. The full divert lifecycle the finding asked
  for is written as the fallback, with `$1` gating, forward-only removal, a census against
  a plain revert, four CI cases, and the H3 amendment in the same PR. H7 ships no divert at
  `70591c307`, so H3's whitelist stays closed (Open question 9).
- **H7-R10-04 (high) ansible-core removal missed `ansible/tests/_helpers.py`, which 361
  test modules import.** Fixed. Confirmed: the import grep lists 25 files, and the
  `_helpers` grep prints 361. *Baseline* lists all 25 by group. H7.17 rebuilds
  `_helpers.py` on R4's filter set with a two-part parity proof, converts or deletes the ten
  `Templar` and `DataLoader` tests and R1's, R3's and R4's oracle tests, and extends R4's
  subprocess test to host and package modes. The deletion census becomes a runtime
  collection with `ansible` blocked, lands in H7.17 with `_helpers.py` as its named member,
  and its allowlist empties before H7.20b. Found while fixing: H7.20b removed ansible-core
  before H7.20c deleted `test_pi_escalation_timeout.py`, so that deletion moved to H7.20a.
  R4's exit 4 covers cluster mode only, and Open question 4 now says so.
- **H7-R10-05 (high) H7.15c's drill guest was not a C8 substitute, and it would have
  reconciled production config with the real cluster key.** Fixed. Exit 4 has two proofs.
  The Flux half is R4 PR 7b's kind drill, C8's first substitute. The host half is a
  transient, fenced guest that plays daniel-box, classified as C8's third substitute, an
  **operator-only** drill with a stated blast radius. It holds no production credential:
  k3s generates its own token, its age key is no recipient, and etcd-s3's credential is a
  Flux Secret it never receives. "The node joins" became `k3s kubectl get node daniel-box`
  reading Ready. A full-artifact proof is declined, with the writes it would make. Found
  while fixing: `k3s_server_args` pins `--node-ip` to `server_ip`, so the guest writes a
  named, guest-local override, and H7.9b's netplan file is `activate: manual`.
- **H7-R10-06 (high) daniel-box could not bootstrap itself.** Fixed with the finding's
  first option. `--local-build` builds all four packages daniel-box needs from the clone,
  after the builder's trust check on `HEAD`, with nfpm and uv fetched against sha256s
  pinned in the script. It installs `homelab-repos` first, then the rest in one `apt-get
  install`. A test holds the script's pins equal to the package pins. The drill builds from
  a GitHub clone and stages no repo, and CI simulates the install with no homelab repo
  configured, with a red fixture. Found while fixing: the rebuild must restore the archive
  signing key from the kit (**operator-only**), and step 2 skips `homelab.sources` under
  `--local-build`.
- **H7-R10-07 (medium) H7.2e's derivation missed in-tree copiers and the deployer's copy.**
  Fixed. Confirmed: `install_host_lib.yml` copies from `common/files`, `HOST_LIB_SRC` is
  hardcoded, and `tasks/code.yml` fills `/opt/gitops-deploy`. H7.2e adds a third grep for
  `src:` and `lookup('file'`, repoints every `src`, `HOST_LIB_SRC` and the two routing keys,
  and ships `ci_verdict.py` through the copy loop with its stamp pair and a routing test.
  The "four items" claim is gone. Found while fixing: `kuma-push-lib.sh` and
  `setup-drift-lib.sh` have 15 outside references, all tests, test helpers or an eval
  record, so neither moves.
- **H7-R10-08 (medium) `manual_plane` lines were never cleared.** Fixed. Derived rather
  than listed: `tick_applies_setup_role` rejects `optimize_pi`, `k3s` and `common`, and a
  `host_lib.py` change routes to `k3s`. The finding's list was wider than the tree:
  `initial_setup`, `docker_install`, `hypervisor` and `claude_code` admit daniel-box, so H7.5
  to H7.7 and H7.13 record no line. *Clearing `manual_plane` lines* gives the `--applied`
  form after the version metric moves, and *Verification* runs it.
- **H7-R10-09 (medium) The cluster-node procedure dropped R4's PriorityClass step,
  reordered it, and cited the wrong R4 PR.** Fixed. Step 4 points at R4 PR 7b's
  *Bootstrapping Flux on a rebuilt cluster* and restates nothing, and "in this order" is
  gone. *Baseline* lists R4's four steps, PriorityClasses first.
- **H7-R10-10 (medium) `retired_host_crons` does not exist.** Fixed. Confirmed: `git grep`
  prints nothing. H7.18c and the census derive the set from the absent-arm cron tasks H7.2f
  already reads, with the three true retirements as named members. *Baseline* and
  *Complexity delta* are corrected.
- **H7-R10-11 (medium) The facts.lock steps covered `verify` only.** Fixed. Confirmed: 57
  rows key on setup-role docs. Every PR that moves or empties a role's `CLAUDE.md` runs
  `forget` for each old key and `verify` for each new one, in *Steps*, *Tests and docs* and
  failure class 8.
- **H7-R10-12 (medium) checkout-sync dropped the deployer's CI gate and had no red
  proof.** Fixed. checkout-sync grades the tip with `ci_verdict` from `hosts/lib/`, through
  the authenticated token, and merges only a green tip. Not reading `DEPLOY_HOLD` is
  recorded as a decision for the ADR, with the reason. Four stub tests, each with a red
  fixture, cover the dirty tree, the branch, the verdict and the lock.
- **H7-R10-13 (medium) The C5 amendment was missing from contracts.md and named no gate.**
  Fixed here and handed on. Open question 1 names the gate, before H7.1 merges. This slice
  does not edit contracts.md, so the row goes to the contracts as a cross-slice note.
- **H7-R10-14 (medium) Moving the networks into the Compose project would collide with
  the live external networks.** Fixed with the finding's first option. The networks stay
  `external: true`, and the Compose unit's `ExecStartPre=` creates any that are missing. H7.5
  first measures Compose's label behaviour in CI. A test holds the created set equal to the
  project's external networks. The install loop creates eight networks, not two, and the
  PR body lists the other six.
- **H7-R10-15 (medium) A per-host canary PR could not remove a shared task for one host.**
  Fixed. *Moving one host at a time* adds `<role>_packaged_hosts` in the role's own
  defaults, never in `host_vars`, because an inventory edit is a deploy-plane change. Moved
  tasks carry a `when:` that reads it. H3.2's, the ufw and the line-arm censuses evaluate
  it through H4.3b2's helper, and the setup census reports a guarded host whose package
  declares nothing. Red fixtures cover both. *Rollback* says what a single-host revert
  restores, and `claude_code` uses H4's `end_role` guard.

### Round c1 (per-slice adversarial review)

Four findings. Every one was checked against the tree at `70591c307` before it was fixed.

- **H7-C1-01 (high) Surviving code imports deployer_lib modules that H7.20a deletes, and
  D6's handoff of `land_reach.py`, `reach.py` and `deploy_cross_role.py` is not picked
  up.** Fixed. An import grep for `deployer_lib`, `gitops_hold`, `gitops_markers` and
  `gitops_ledger` outside the deployer's role confirmed every importer the finding names.
  It also found four more that the finding missed:
    - renovate_agent's `agent_toolbox.py`, `agent_logic.py` and `renovate_agent.py`, which
      survive H7;
    - `deploy_ui_writes.py`;
    - `scripts/docs/reference/state.py`;
    - `land_lib`'s `landing.py`, `options.py` and `outcome.py`, which read
      `gitops_ledger`.

  A path grep found `gen_doc_fragments.py:_gitops`, which reads deployer files by path,
  and `gitops_deploy_ci_contexts`, which lives in role defaults that H7.20a deletes. Three
  sub-claims are refuted, because D6 deletes those importers first:
    - `land_lib/land_rerolls.py`'s `deploy_locks` import (D6's *What D6 deletes*:
      "`land_lib`'s k8s deploy arm, `land_rerolls.py`");
    - `probe_lib/releases.py`'s `_is_test_only_path` import ("the six `releases*`
      modules");
    - `narrow_lib/broad.py`.

  So `_is_test_only_path` needs no new home. The slice now carries the following:
    - a fourth derivation grep in H7.2e;
    - *Surviving importers of deployer modules*, which assigns every importer a PR;
    - H7.2e repoints `await_ci.py` and `landing_blockers.py`, and moves
      `ci_required_contexts`;
    - H7.19b re-homes `TREE_LOCK` and `k8s_lookup_map`, and also `role_of`, `role_for` and
      `is_role_test_path` if a survivor needs them;
    - H7.19b deletes what D6 hands over: `land_reach.py`, the rest of `reach.py`,
      `classify.py`'s setup arm and all seven `narrow_lib` modules, where the tree diff
      had listed three;
    - a deployer-import census with an AST walk, function-level imports included, whose
      named member is `await_ci.py`. It lands in H7.19b, and its allowlist empties in
      H7.20a;
    - a `pytest --collect-only` part in H7.20a's proof and in exit 5.

  `deploy_cross_role.py` leaves with the setup tree in H7.20a, after `k8s_lookup_map`
  moves out.
- **H7-C1-02 (medium) checkout-sync grades only the tip, which narrows the deployer's
  newest-green-ancestor gate without recording it.** Fixed with option A. Confirmed:
  `deploy_git.py:ci_walk_candidates` carries the `DECIDED` marker, and `deploy_git.py`
  imports nothing. H7.2e moves `ci_walk_candidates` with `ci_verdict`, and checkout-sync
  merges `--ff-only` to the newest green first-parent ancestor. A red proof covers a
  pending tip with a green parent, and a red ancestor is never chosen. Exit 5, its expected
  result and *What a partial failure leaves live* are reworded to "within one timer
  interval of master's CI verdict turning green".
- **H7-C1-03 (medium) `bootstrap-host.sh` step 4 has no stated source for the k3s pins
  after H6 moves them.** Fixed with option A. Confirmed: H6 moves the three keys into the
  `system-upgrade` role's defaults, which do not exist at `70591c307`. The script embeds
  the three pins. The *Bootstrap pins* test and its red fixture hold them equal to H6's
  keys, and the k3s Renovate manager lists the script as a second file. Exit 4's host half
  checks `k3s --version`.
- **H7-C1-04 (medium) deploy_ui imports the deployer state modules, but H7.19b's reader
  list omits it.** Fixed. Confirmed: `deploy_ui.py`, `deploy_ui_reads.py` and
  `deploy_ui_writes.py` import the three state modules. H7.11 no longer packages
  deploy_ui, and H7.19a retires it with the deployer. The one exception is a deploy_ui
  that D6.15's operator review already moved onto `DEPLOY_HOLD`, whose remaining reads
  H7.19b deletes. The census covers all three files.

### Round c2 (per-slice adversarial review)

Six findings: three high and three medium. All six are fixed, and none is refuted. Each was
confirmed against the tree at `70591c307` before it was fixed.

- **H7-C2-01 (high) land.sh's tick step has no owner: every tagless landing dies
  `deploy-failed` from H7.19a, and H7.19b deletes `gitops_tick.sh` under its callers.**
  Fixed. Confirmed: `pipeline.py:_STEPS` runs `_step_tick`, `tick.py:run_tick` ends any
  exit but `TICK_OK`, `TICK_STILL_RUNNING` and lock contention as `DEPLOY_FAILED`, and
  `gitops_tick.sh` calls `tick_verdict 2 not-installed` when the unit is missing.
  `deploy.py:deploy_phase` holds a second fatal `run_tick` call on its stale retry, which
  the finding did not name.
    - The new *land.sh stops running the tick* section moves every land.sh path that starts
      or reads a tick into H7.19a. That covers `_step_tick`, `tick.py`,
      `tools.py:run_tick`, the three call sites, `gitops_tick.sh`, and the setup arm's
      reads of the tick's markers in `classify.py` and `no_tag_outcome`. This fix put them in
      H7.19a's second PR. H7-C3-06 later split H7.19a into five PRs, and the third PR owns
      them (*Steps*, #4609).
    - Root `CLAUDE.md`'s *After a PR Merges* and *When to wait*, the `gitops-tick` and
      `land-after-merge` skills and `docs/landing.md` move from H7.19b into H7.19a. H7-C3-06
      puts the first two with the fifth PR and the last two with the third.
    - The start-checkout-sync option was rejected. `tick.py:rearm_tick`'s `DECIDED` note
      records that the polkit rule admits `start` on `gitops-deploy.service` alone, so that
      option needs a new polkit rule, and C6's host wait already covers a tagless host PR.
    - *Verification* gains a test with a red half. The red half runs a tagless host PR,
      with a stub `systemctl` that reports the unit absent, against the base's `_STEPS`,
      and that run ends `deploy-failed`.
- **H7-C2-02 (high) H3's per-host updater hold contradicts H7's Rollback, its
  partial-failure section and the deployer-retirement timeout, and a ufw revert wedges the
  host.** Fixed. Confirmed in H3: the updater's failure arm writes the hold and
  `applied_info` from `last_ok`, a held host installs nothing newer, and H3's builder
  publishes a host package only when its render changed.
    - The new *Which outcomes hold the host* section sorts each arm outcome into one of two
      classes. A fault holds the host: a violated `before:`, a uid mismatch, a validation
      failure. Drift is reported through `homelab_host_reconcile_drift` and the run
      succeeds: a rule kept by the ufw floor, and a refused libvirt undefine.
    - The section gives the operator-only way out: install the fixed or reverted version by
      hand, then `--clear-hold`.
    - The deployer now retires across two daniel-box versions, the timer in H7.19a and the
      service and paths in H7.19b, so reconcile no longer waits on a tick and has no timeout
      to hold on.
    - *Rollback*, *What a partial failure leaves live*, *Risks* and *Verification* are
      updated to match. The revert fixture must leave the updater installing the next
      version. A per-arm test proves each class, and a test proves the way out.
    - H3 needs no amendment. A third "pending" result was considered, and *Complexity
      delta* records why it was not taken.
- **H7-C2-03 (high) monitor-bridge keeps a `type: Directory` hostPath mount of
  `/var/lib/gitops-deploy`.** Fixed. Confirmed: `deployment.yaml.j2` declares
  `monitor-bridge-gitops-state`, `config_service.py` reads `GITOPS_STATE_DIR`, and
  `checks/gitops.py:check_gitops_alive` reads `last_run`.
    - Retiring the timer would have made that check page, so H7.19a's first PR removes the
      two checks, the volume, the mount, the env key and the read.
    - The pin stays for the etcd drill's stamp directory, and the role's `CLAUDE.md`
      restates that as its reason.
    - The operator cleanup of the directory moves to H7.19b's PR body. The two checks'
      push-token keys stay in `secrets.yml` as orphans, and their removal is named as an
      **operator-only** cleanup, because it needs a `sops` edit.
    - A `test_rendered_properties.py` row with a named member and a red fixture asserts
      that no rendered manifest mounts that hostPath.
- **H7-C2-04 (medium) checkout-sync reuses `ci_verdict`, which returns `pass` for an empty
  required set.** Fixed. Confirmed in the docstring of `deploy_git.py:ci_verdict` and in
  `await_ci.py:DisarmedGateError`. checkout-sync refuses an empty or missing
  `ci_required_contexts` with `refused{reason="ci"}`. The refusal sits in checkout-sync, not
  in `ci_verdict`. A red-fixture case joins the checkout-sync proofs, and failure class 3 in
  *Risks* names it.
- **H7-C2-05 (medium) The path-reader census is incomplete.** Fixed. Confirmed:
  `brief.py:APPLY_OWED` holds the `cat`, and the two task files grant access to the
  directory.
    - `land_renovate_pr.py:DEPLOYER_CONFIG` was confirmed at `70591c307`, but D6.13 already
      repoints the lander's denylist at `k8s_autodeploy_counts.py --denylist`. H7 therefore
      only quotes the empty grep.
    - H7.2e runs a fifth derivation grep for the three path prefixes, and *Path readers*
      assigns each hit to a PR.
    - The deployer-import census also flags those path literals and `gitops_tick.sh` in
      Python string constants, with a third red fixture.
- **H7-C2-06 (medium) Rollback's hand re-applies decrypt secrets and are not marked
  operator-only.** Fixed. Confirmed: `initial_setup.yml` and `k3s-bringup.yml` both import
  `pre_tasks/load_secrets.yml`. *Rollback* opens with a rule that every hand re-apply and
  every `--clear-hold` is **operator-only**, and that an executing agent stops and hands the
  step over. Each re-apply command carries the mark, H7.19a's included.

### Round c3 (per-slice adversarial review)

Six findings: two high and four medium. All six are fixed, and none is refuted. Each was
confirmed against the tree at `70591c307` before it was fixed.

- **H7-C3-01 (high) The ufw arm never enables ufw, so a rebuilt host's declared rules are
  not enforced and exits 2 and 4 still pass.** Fixed. Confirmed:
  `initial_setup/tasks/network.yml` holds `Enable UFW firewall` (`state: enabled`), and no
  arm step or bootstrap step replaced it.
    - The ufw arm gains step 6, `ufw --force enable` only while `ufw status` reads
      inactive, after every `keep` rule exists and step 3 passes. It reruns the `before:`
      check after the enable, and a failed enable is a fault that holds the host.
    - The finding did not name a second gap: `ufw status numbered` prints no rules while
      ufw is inactive, so step 3's order check would pass vacuously on a rebuilt host.
      Steps 2 to 5 now read `ufw show added`, and H7.2b measures both outputs for the stub.
    - Reconcile runs the line arm before the ufw arm, so the enable applies the edited
      `/etc/ufw/sysctl.conf` (#2977).
    - The drill guest gains a third deviation, a guest-local SSH allow for the `staging`
      bridge, because the enabled limit rule would reject the orchestrator's polling.
    - Exit 2, exit 4's host half and the per-host checks read `Status: active`. The setup
      census names `Enable UFW firewall`. Red proofs cover the enable, its order and its
      failure.
- **H7-C3-02 (high) Seven `ansible.posix.sysctl` tasks write `/etc/sysctl.conf`, which
  overrides the packaged `sysctl.d` drop-ins.** Fixed by Option A. Confirmed: `git grep -c
  'ansible.posix.sysctl:'` prints the seven tasks, `git grep -n sysctl_file` prints
  nothing, `dpkg -S /etc/sysctl.conf` prints `procps`, procps' `${Conffiles}` lists the
  file, and `man 8 sysctl` reads it after every `sysctl.d` directory.
    - Every key those tasks set stays in `/etc/sysctl.conf` through the line arm's conffile
      rule, with `activate: sysctl`. No drop-in declares such a key, and a census holds
      that. Option B was rejected because it adds a deleting arm, which needs its own
      floor and red proof.
    - The line-arm two-writer census keys `/etc/sysctl.conf` on the path and the sysctl
      key, so zram's keys can move in H7.3 before the hardening keys in H7.6g–i.
    - Baseline counts the seven tasks. H7.6g–i's PR bodies quote each host's
      `/etc/sysctl.conf` keys. The CI container test proves a drop-in key loses to
      `/etc/sysctl.conf`.
- **H7-C3-03 (medium) The setup census's module list misses whole module types.** Fixed.
  Confirmed by a module count over the setup tasks: `systemd_service` 14, `apt` 34,
  `get_url` 14, `unarchive` 3, `user` 9, `group` 4, `ansible.posix.sysctl` 7,
  `ansible.posix.acl` 3 and `dpkg_selections` 5, none of them read.
    - The census now reads every module outside a named control and read-only set. That
      set adds `meta`, `fail`, `wait_for`, `service_facts`, `docker_host_info` and
      `include_tasks` to the finding's list, so strict mode never flags an emptied role's
      `end_role` stub.
    - A `command` or `shell` task counts as a read only with `changed_when: false` and no
      `creates:` or `removes:`.
    - H7.1 lists `/usr/local/bin/sops` as a legacy entry on all three hosts. vale's
      `unarchive` into `/usr/local/bin` gets the same treatment.
    - Named members: `/usr/local/bin/sops` and `Enable and start the host upsmon service`.
      Red fixtures: an unassigned `apt` task and an unassigned `get_url` task.
- **H7-C3-04 (medium) checkout-sync's ancestor-walk bound has no source once the
  deployer's config goes.** Fixed. Confirmed: `deploy_config.py` reads
  `CI_ANCESTOR_WALK_MAX` into `ci_ancestor_walk_max`, the role's `config.env` template
  renders it from `gitops_deploy_ci_ancestor_walk_max: 10`, and H7.20a deletes both.
    - H7.2e moves the bound to `group_vars/all.yml` as `ci_ancestor_walk_max`, beside
      `ci_required_contexts`, so it adds no new plane.
    - checkout-sync refuses a missing or non-positive bound with `refused{reason="ci"}`. A
      red fixture with a bound of 0 leaves a pending tip with a green parent unmerged.
- **H7-C3-05 (medium) checkout-sync's GitHub token delivery is unstated.** Fixed.
  Confirmed in the docstring of `host_lib.py:github_token`, which falls back to `gh auth
  token`.
    - The refusal the finding asked for would have been vacuous. `sys_user`'s gh is logged
      in as the repo owner, so the fallback never returns None and spends the operator's
      personal token. The builder's own refusal works only because its user has no gh
      login.
    - checkout-sync declares
      `LoadCredential=github_token:/run/credstore/homelab_builder_github_token` and exports
      it as `GH_TOKEN`. It calls `github_token` with a `run` that refuses the gh fallback,
      and sets `refused{reason="token"}` before any request when the credential is missing.
    - A stub test's red fixture keeps the fallback, and the stub records the operator's
      token on a request.
- **H7-C3-06 (medium) H7.19a's second PR bundles about six separable changes and states no
  line count.** Fixed.
    - H7.19a is now five PRs, each with a line count from the tree and its own rollback:
        1. monitor-bridge;
        2. deploy_ui's retirement, about 2,300 deleted lines;
        3. land.sh's tick removal, about 1,040 deleted lines and 150 changed;
        4. checkout-sync beside the deployer, about 350 lines;
        5. the timer's retirement with the emptied role, about 530 deleted lines.
    - The third and fourth precede the fifth. The *Overlap* bullet now states why running
      both actors is safe after H7.14 and D6.
    - Each doc moves with the PR whose behaviour it describes. `land-after-merge` and
      `docs/landing.md` go with the third, and root `CLAUDE.md` and the `gitops-tick` skill
      go with the fifth.

### Round integration-2 (cross-slice review)

Three findings that name H7, all high. All three are fixed, and none is refuted. Each claim
was checked against the tree at `70591c307` and against the D1, H3 and H6 plans before the
fix.

- **X2-02 (high) H7 moves and then deletes the CI-context list that D1's `promote.py
  decide` reads by path, and never repoints promote.** Fixed. Confirmed: D1's *Design*
  has `decide` read `gitops_deploy/defaults/main.yml:gitops_deploy_ci_contexts` from its
  own checkout and refuse an empty or missing list. H3's builder grades over the same key
  and walks `gitops_deploy_ci_ancestor_walk_max` commits. `git grep -l -E
  'gitops_deploy_ci_(contexts|ancestor_walk_max)'` outside the role and `docs/` lists two
  workflow comments, `await_ci.py` and two tests under `ansible/tests/repo/`, and no
  promote or builder, because neither exists at `70591c307`. Both exist at H7.2e's base:
  H7 depends on H6, H6 on D2 and D2 on D1, and H7 on H5, which depends on H3.
    - The break comes at H7.2e, not at H7.20a. A role default rewritten as a reference is
      a string to a path reader, so `decide` would print `error` from H7.2e's merge on.
    - *Shared modules leave the setup tree* now lists four surviving readers, and H7.2e
      repoints `decide` and the builder, the walk bound included, in the same PR.
    - A key grep by key name joins the derivation greps, because an import grep cannot see
      a YAML read. The deployer-import census also fails on either old key name, with a red
      fixture.
    - *Verification*, *Rollback*, *Risks*, *Tests and docs* and *Complexity delta* changed
      with it.
- **X2-04 (high) H7 removes the deployer without the seven deployer-coupled H6 checks.**
  Fixed. Confirmed: H6 *Open questions* 10 asks H7.19a's tick-stopping PR to remove seven
  items, and H7 named none of them. `k3s_upgrade.py:held_sha` returns a refusal when the
  state directory is not a directory. `deploy_locks.py:lock_path` and `SERVICE_LOCK_ALL`
  are what the equality test reads. `gitops_deploy/templates/gitops-deploy.timer.j2` sets
  `OnBootSec=10min` and `OnUnitActiveSec`, so the tick is monotonic.
    - The fifth H7.19a PR removes the seven items, before H7.19b names the state directory
      for cleanup and before H7.20a deletes `deploy_locks.py`. *The deployer retires last*
      gains a bullet that lists them and says why they go then and not earlier.
    - checkout-sync's timer stays monotonic and joins H6's exemption list with its own
      exclusion. `OnCalendar` was rejected because the window test would then pin each
      restart window between two of its fires. The fourth PR merges after H7.18a, so the
      window test reads the packaged timer.
    - Found while fixing: H7.12c renames the four `claude-*` timers that H6's exemption
      list names. H7.12c now renames those entries too.
    - *Verification*, *Rollback* (the fifth PR and the last tick), *Tests and docs* and
      *Complexity delta* changed with it.
- **X2-05 (high) H7's move of the builder and the Pi node exporter drops H3's fingerprint
  file, publish-textfile hook and Pi textfile directory.** Fixed. Confirmed: H7 named
  `archive-key.fpr`, `publish-textfile` and `node-exporter-textfile` nowhere. H3's role
  writes the fingerprint file on every apply, and H3.6's `optimize_pi` task is the Pi's
  only creator of the textfile directory.
    - H7.14 moves the fingerprint write into an `ExecStartPre=+` script on the builder
      unit, so the break-glass key restore regenerates the file on the builder's next run.
      It also moves `publish-textfile` with both `ExecStopPost=-+` lines, and it keeps the
      builder's once-per-condition record in its state directory.
    - H7.4 ships the textfile directory as a `tmpfiles.d` line at root, group `sys_user`,
      mode 2775, and deletes H3.6's task. Bootstrap step 5 runs `systemd-tmpfiles
      --create`, so the directory exists before the updater's first run.
    - The Pi runbook gains step 8, which reads `homelab_host_applied_info{host="daniel-pi"}`
      and `node_textfile_scrape_error`. Exit observation 2 and *Verification* read the same
      series.
    - Container tests with red fixtures cover the fingerprint script and the directory.
      *Rollback*, *Risks*, *Tests and docs* and *Complexity delta* changed with it.

### Cross-slice follow-up (2026-10-12)

- **#4609 (medium): two owners for the deletion of `gitops_tick.sh`.** H7-C2-01's row said
  that H7.19a's second PR deletes it, and the finding's title still reads that H7.19b
  deletes it. *Steps* gives it to H7.19a's third PR. Confirmed: H7-C3-06 renumbered the
  PRs and left H7-C2-01's row behind. **Fixed in the row.** H7.19a's third PR is the one
  owner, as *land.sh stops running the tick*, *What the deletion touches* and *Steps*
  state. H7.19b's step now names the third PR.

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review round covers it.

- **F-03.** *The cluster-node bring-up* gains an offline branch: each GitHub or GHCR fetch
  takes a kit source, and the artifact goes through C2's break-glass promotion, which D6
  owns. `bootstrap-host.sh` gains `--offline` in H7.15a, with a red-proofed container case.
  The Flux images and the registry's bring-up order on a fresh cluster went to *Open
  questions* 14, with a cross-slice request to D6.
- **F-15.** *Design* gains *Ubuntu release upgrades*: the updater hold, the upgrade, the
  check against the matrix release, and the release of the hold. A cluster node also
  takes H6's pause label for the duration. PR H7.15e writes it. C5
  assigns `homelab-tools` to H3, so H7.1, *Architecture-specific binaries* and *Complexity
  delta* re-attribute it, and H7.1 keeps only nfpm and the `sops_setup` retirement.
  *Open questions* 1 is resolved by the C5 amendment.
- **F-05 / F7.** Exit criterion 7 adds the reconcile line ceiling, and *Design* gains
  *Reconcile's line ceiling*. H3.2b states no size, so the ceiling is the measured base
  plus the stated sizes, with H7.2a to H7.2d's sizes stated here, times 1.3. Crossing it is
  design.md's ansible-pull trigger, per its *Open questions* 6 and 7, and raising it is
  **operator-only**. H7.2a adds the census, with a red proof.
- **F-05 / failure class E.** *Goal* and *Complexity delta* name the primary checkout and
  checkout-sync as kept. *Open questions* 12 recommends running the checkout-reading
  crons from a package or an image.
- **F-12 / P7.** *Design* gains *Adding a host*, modelled on H4.2 and H6.2, and PR H7.15d
  adds it with a host census and a fixture-host case before H7.20a.
- **F-10.** *Open questions* 13 recommends one move PR after H7.20 that renames the
  `ansible/` tree.
- **F-11 (from H6).** H7.13 moves H6.1b2's rehearsal mode with the `hypervisor` role.
