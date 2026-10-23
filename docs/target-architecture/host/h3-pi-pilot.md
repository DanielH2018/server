# H3 — Pi pilot: builder, updater, versioning

Status: IN PROGRESS

PR H3.1, the renderer's host and package modes: [#4911](https://github.com/DanielH2018/server/pull/4911).
PR H3.2a, the updater package: [#4917](https://github.com/DanielH2018/server/pull/4917).
PR H3.2b, the reconcile unit: [#4927](https://github.com/DanielH2018/server/pull/4927).
PR H3.2c, the censuses and the unit validator: [#4935](https://github.com/DanielH2018/server/pull/4935).
PR H3.2d, the manifest emitter: [#4944](https://github.com/DanielH2018/server/pull/4944).
PR H3.3a, the `host_packages` CI job skeleton: [#4974](https://github.com/DanielH2018/server/pull/4974).
PR H3.3b, the updater cases in systemd containers: [#5009](https://github.com/DanielH2018/server/pull/5009).
PR H3.4a, the `homelab_builder` role behind a disarmed flag: [#5016](https://github.com/DanielH2018/server/pull/5016).
The operator cleared its park on 2026-10-14, and the apply changed nothing.
PR H3.4b, the repository half: [#5029](https://github.com/DanielH2018/server/pull/5029).
The tick applied it on 2026-10-15, and daniel-server reads `10.0.0.215:8791`.
PR H3.4c, the archive key in `homelab-updater`: [#5032](https://github.com/DanielH2018/server/pull/5032).
The key's fingerprint is `83B57416B342DDE1CBA0752D435D139ADFD6F016`.
PR H3.5a, the builder half, staging only: [#5046](https://github.com/DanielH2018/server/pull/5046).
The operator landed it on 2026-10-15, and the tick applied it at `b7d8f0ef5`. The builder's
first run ended at 12:39:05 UTC with `reason="ok"` for `a594ad662e68` and nothing unbuilt.
PR H3.5b, signer and publish: [#5149](https://github.com/DanielH2018/server/pull/5149). It
landed in an attended window on 2026-10-15, and the tick applied it at `9b063197a`. The
signer's first run ended at 21:51:16 UTC with `Result=success`.
`http://10.0.0.215:8791/dists/homelab/InRelease` carries a `Valid-Until` seven days after its
`Date`, and `gpgv` against `hosts/keys/homelab-archive-keyring.gpg` reports a good
signature from `83B57416B342DDE1CBA0752D435D139ADFD6F016`. `homelab_repo_published_info`
names `homelab-updater` at `9b063197a50a`. With the operator's `/etc/homelab-builder/hold` in
place, the builder's next run wrote `reason="hold"` at 21:53:13, and the signer it triggered
exited 0 and left `InRelease`'s `Date` at 21:51:16.
PR H3.5c, page on the builder, in the row form: [#5159](https://github.com/DanielH2018/server/pull/5159).
M6's A1 shadow PR had not merged. The operator generated
`monitor_bridge_host_packages_push_token`. The landing deployed monitor-bridge and
uptime-kuma, both healthy, and `kuma-drift` showed the new "Host Packages — Status" tile live
and not down.
PR H3.5d, the host rules: [#5112](https://github.com/DanielH2018/server/pull/5112). It
deployed `observability` at `de380735e`. Prometheus loaded the `host-checks` group with 7
rules, and neither `HostReconcileFailed` nor `HostUpdateStale` has a series, because the
expected-host map is empty until H3.9c.
PR H3.6, the Pi package and bring-up: [#5210](https://github.com/DanielH2018/server/pull/5210).
Its landing at `6eb63d2a4` read `needs-manual-apply` with an `optimize_pi` line for the
whole role, so the narrowed `clear-owed` in step 2 refused. The operator applied
`node-exporter-host`, then the whole role (`ok=76 changed=0`), and cleared the line. On the
Pi, `homelab-updater.timer` read `inactive` and `homelab_host.prom` had mode 0644.
Prometheus read `homelab_host_reconcile_ok{host="daniel-pi"}` = 1 and
`node_textfile_scrape_error{job="node-pi"}` = 0, and `homelab_host_applied_info` named
version `0.5641+g6eb63d2a4157`.
PR H3.7, the Depends gate: [#5237](https://github.com/DanielH2018/server/pull/5237). The gate
passed on 2026-10-16. The operator started the updater by hand, and it installed
`homelab-daniel-pi 0.5658+g70bdf5e85e1b` with `Result=success`. apt pulled `tree` in and marked
it automatic, `homelab-updater.timer` stayed `inactive`, and `dpkg --audit` was clean, so the
package plan stands and Open question 2's fallback is not taken. The follow-up,
[#5246](https://github.com/DanielH2018/server/pull/5246), drops the `Depends`. On the operator's
second hand-started run, the Pi upgraded to `homelab-daniel-pi 0.5666+gd3a4f5ccc005` at
16:16:43 UTC with `Result=success`, and the installed `Depends` reads `python3 (>= 3.12)`.
`tree` stays marked automatic, and `apt-get -s autoremove` lists it, so the weekly autoremove
removes it.
PR H3.8a, interim credentials: [#5322](https://github.com/DanielH2018/server/pull/5322). It merged
at `8920ca2d0` and stages two files, not three, because `domain` left the SOPS store in #5087; the
PR corrected this plan's text to match. The operator's `optimize_pi` apply read `ok=80 changed=3
failed=0`: `/etc/homelab/interim` at `root:root 700` and the two push tokens at `root:root 600`
([readings](https://github.com/DanielH2018/server/pull/5322#issuecomment-6224823184)).
PR H3.8b, the heartbeats move: [#5348](https://github.com/DanielH2018/server/pull/5348). It merged
at `4c01b38c0`. The operator's hand-started updater installed `homelab-daniel-pi
0.5709+g252cd470cc5b` with `Result=success`. The three timers are active, `ubuntu`'s crontab
holds no `pi-` line (3 before), and the three `/usr/local/bin/pi-*.sh` are gone. Each heartbeat's
first timer run exited 0 between 22:42:59 and 22:45:11 UTC, and no monitor dropped
([readings](https://github.com/DanielH2018/server/pull/5348#issuecomment-6226115060)). The owed
`optimize_pi` line was cleared with no Ansible apply.
PR H3.9a, the host-wait module, unwired: [#5383](https://github.com/DanielH2018/server/pull/5383).
It merged at `340f20772` and deployed nothing. Nothing called `land_lib/host_wait.py` until
H3.9b. Its `HOST_INPUTS` audit caught D5.3b2's soak imports reaching a host render before
D5.3b2 merged.
PR H3.9b, wire the host wait and arm the Pi: [#6020](https://github.com/DanielH2018/server/pull/6020).
It merged at `b0c40b65`, and the Pi package ships the enable link for `homelab-updater.timer`.
The Pi package repo published `b0c40b65` (`homelab_repo_published_info{package="homelab-daniel-pi"}`
read sha `b0c40b65693c`). On 2026-10-23 at about 17:05Z the operator started
`homelab-updater.service` on the Pi by hand (item 9 of H3.9b), and `systemctl is-active
homelab-updater.timer` printed `active`.
PR H3.9c, the Pi joins `HostUpdateStale`: the PR that carries this line. It adds daniel-pi
(job `node-pi`, 11400 seconds) to `observability_expected_hosts` and its rule to
`host-checks.yaml`.
PR H3.10a, arm the canary: [#6033](https://github.com/DanielH2018/server/pull/6033). It merged at
`3f8e28fde`. It ships `homelab-pi-canary.service`,
`homelab-pi-canary.timer` and the timer's enable link in the Pi package. The operator check passed:
`systemctl is-active homelab-pi-canary.timer` printed `active` and the `timers.target.wants`
count was 1.
PR H3.10b, drop the canary: the PR that carries this line. It removes both units and the link.

Depends on: R1
Unblocks: H4, H5

This slice builds the package pipeline end to end and pilots it on daniel-pi. It also pins
the names, paths and rules that H4 to H7 build on. Those slices cite the section *Shared
names for the host slices* instead of restating it.

## Goal and exit criterion

Afterwards, a signed apt repository on daniel-box serves `homelab-updater` and
`homelab-daniel-pi`. The Pi installs them through `homelab-updater.timer`. The three Pi
heartbeats run as package-owned systemd timers, and their Ansible-era crons are gone.

The exit criterion is four observations, in this order, all on the Pi:

1. A merge that changes the Pi package shows its SHA in `dpkg-query -W homelab-daniel-pi`.
   Prometheus shows the same SHA in `homelab_host_applied_info{host="daniel-pi"}`, with
   `homelab_host_reconcile_ok{host="daniel-pi"}` at 1.
2. A commit that drops a timer from the package leaves that timer stopped, with no unit
   file and no enable link.
3. An updater run killed while dpkg unpacks, so that `dpkg --audit` lists the package, is
   followed by a clean next run, and `dpkg --audit` then prints nothing.
4. `land.sh` on a PR that touches only the Pi package reports `settled` only after the Pi's
   reported SHA reaches the merge SHA. It never reports `nothing-to-deploy` for that PR.
   Under the daily fallback schedule (*Risks*, apt memory), the operator starts the updater
   by hand once the builder has published, then re-runs the same `land.sh` command. The host
   wait queries once before it applies its 45-minute rule, so that query settles it.

The operator reads the on-host half of observations 1 to 3, because no agent has a login on
the Pi (`docs/claude-agent-user.md`, *What the `claude` user cannot do*). An agent reads the
Prometheus half.

Before any of those, one gate: a later package version adds a `Depends`, and the updater
installs it. Until that gate passes, the Pi's updater runs only when the operator starts it.

## Baseline at `70591c307`

- **Nothing packages host configuration.** `git ls-files | grep -c nfpm` prints 0, and no
  `hosts/` directory exists. `.gitignore` denies every root path with `/*` and allows each
  tracked root entry by name, so a root `hosts/` directory needs a `!/hosts` line.
- **The three heartbeats are Ansible crons.** `ansible/roles/setup/optimize_pi/tasks/main.yml`
  installs `Pi SD-card health heartbeat` (`*/5`), `Pi rotated-log integrity sweep` (`40 1`) and
  `Pi container-recovery heartbeat` (`*/5`). Its `CLAUDE.md` glance block lists the same three.
- **The two token scripts read two SOPS keys.** `pi-sd-health.sh.j2` renders
  `pi_sd_health_push_token` and `pi-recovery-health.sh.j2` renders `pi_recovery_push_token`
  inline. `grep -c '^<key>:' ansible/vars/secrets.yml` prints 1 for each of the two. A join of
  every name the three templates read against the `secrets.yml` key names finds no third. The
  integrity sweep reads none. Both token scripts install at mode 0700 with `no_log: true`.
- **Both token scripts also render `domain` into `KUMA_HOST`, from inventory.** At
  `70591c307` `domain` was a third SOPS key. #5087 deleted it from `secrets.yml`, so
  `grep -c '^domain:' ansible/vars/secrets.yml` prints 0, and the value lives in
  `ansible/inventory/group_vars/all.yml`.
- **The moved templates read role defaults and inventory.** `pi-gz-integrity.sh.j2` and
  `pi-sd-health.sh.j2` read `optimize_pi_gz_integrity_roots`, `_min_files`, `_state_file` and
  `_max_age_h`. The recovery cron's `state:` reads `optimize_pi_recovery_restart_enabled`. All
  five live in `ansible/roles/setup/optimize_pi/defaults/main.yml`. The recovery script also
  reads `containers_list` and `has_code_server`, and both token scripts read
  `k3s_metallb_ingress_vip` from `group_vars/all.yml`.
- **The heartbeats run as `sys_user` and share two directories.** The crons run as
  `sys_user` (`ubuntu`, uid 1000). `docker_install`'s `docker-group` task adds that user to
  `docker`. `optimize_pi` creates `/var/log/pi-health` and `/var/lib/pi-health` owned by
  `sys_user`, and writes `/etc/logrotate.d/pi-health` with `su {{ sys_user }}`. The Pi's
  promtail reads the log as uid 1000.
- **Three checks find the templates by their role path.**
  `scripts/validate/shell_templates.py:discover_templates` globs `*.sh.j2` under
  `ansible/roles/`. `ansible/tests/setup/test_cron_scripts_publish_via_pr.py:push_corpus` and
  `ansible/tests/repo/test_secret_rendering_host_scripts_have_no_log.py:KNOWN_SECRET_SCRIPTS`
  name the two token scripts by role path.
- **The two template validators read `ansible/roles/` only.**
  `scripts/validate/unit_templates.py:discover_templates` globs `*.service.j2`, `*.timer.j2`
  and `*.socket.j2` under `ansible/roles/`. Its module docstring says it exists because a
  typo'd directive key otherwise reaches a live host. In `prek.toml`, the `files` selectors of
  `validate-unit-templates` and `validate-shell-templates` both start `^(ansible/roles/`.
- **The Pi decrypts nothing.** `docs/break-glass.md` records that the Pi's age key was removed
  from `ansible/.sops.yaml` on 2026-10-03, because the Pi is driven over ssh and never decrypts.
  `ansible/.sops.yaml` lists three recipients: daniel-box, daniel-server and an off-box
  recovery key.
- **The Pi's node exporter ignores textfiles.** `ansible/roles/setup/optimize_pi/templates/node_exporter.service.j2`
  passes `--no-collector.textfile`. The cluster nodes' DaemonSet reads
  `/var/lib/node-exporter-textfile` (`ansible/roles/k8s/node-exporter/templates/daemonset.yaml.j2`).
  That hostPath has `type: DirectoryOrCreate`, so the kubelet creates the directory, root
  0755, on the k3s nodes. Nothing creates it on the Pi: `git grep node-exporter-textfile --
  ansible scripts` matches only that DaemonSet and
  `ansible/roles/setup/claude_code/files/claude-cgroup-metrics.sh`, which exits 0 when the
  directory is missing.
  Prometheus scrapes the Pi as a static target on port 9100
  (`ansible/roles/k8s/observability/templates/prometheus.yaml.j2`, the `node-pi` job, with
  `instance: daniel-pi`). The same file scrapes uptime-kuma, so Kuma's `monitor_status` series
  are in Prometheus.
- **apt on the Pi is heavy, and the night is full.** `optimize_pi`'s `Pin the apt timers to
  the quiet hour` task records one `apt-get -d dist-upgrade` at 57 MB RSS and memory PSI
  `full avg10` 45%. That figure is a process RSS, not a cgroup peak. It moves `apt-daily` to 04:20 and `apt-daily-upgrade` to 05:20. The same
  comment lists the midnight cluster (23:55 to 00:10), man-db (01:00 to 03:00) and the 06:00
  hour. The integrity sweep runs at 01:40. The Pi had 173 MB available under load on
  2026-10-11.
- **Master commits carry GitHub's signature, not the operator's.** `git cat-file -p 70591c307`
  shows `committer GitHub` and a PGP `gpgsig`. `git log --format=%G?` prints `E` for the last
  8 master commits, and `gh api repos/DanielH2018/server/commits/70591c307` reports
  `verified: true, reason: valid`. `scripts/deploy_tools/land_lib/merge.py` squash-merges
  through the REST endpoint. Renovate's commits are authored by `renovate[bot]`.
- **A bring-up playbook edit parks the deployer.**
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_changes.py:_BROAD_MANUAL_PREFIXES`
  holds `ansible/initial_setup.yml`. `deploy_changes.py:services_from_changed_paths` sets
  `cs.broad_manual` for it, and the `deploy_defer.py` module docstring says that
  `cs.broad_manual` still parks. `docs/gitops-pipeline.md` (*When a tick parks*) gives the
  clear: `git merge --ff-only origin/master` in the primary checkout, then the playbook. One
  such change holds back every later merge. The same section starts the diagnosis with
  `git diff --name-only <local-HEAD>..origin/master`. A hand ff-merge strands every other
  change in that range: the comment in `scripts/deploy_tools/land_lib/health_verdict.py:health`
  says that once local equals origin the tick returns `noop` forever.
- **A setup role the tick cannot apply records a `manual_plane` line.**
  `deploy_defer.py:unapplyable_setup_roles` returns every changed setup role that
  `deploy_setup_roles.py:tick_applies_setup_role` rejects. That function accepts a role only
  when its `initial_setup.yml` entry admits the tick's host. `optimize_pi`'s entry is
  `when: inventory_hostname == optimize_pi_host`, so every `optimize_pi` change records one.
- **The deployer passes an unknown path silently.**
  `deploy_changes.py:services_from_changed_paths` maps a path outside its prefixes to an empty
  change set, and the tick fast-forwards it with no deploy. A change under `hosts/` therefore
  neither parks nor deploys anything.
- **Setup-role changes reach the Pi only by hand, and only the operator can apply them.**
  `has_gitops: false` in `ansible/inventory/host_vars/daniel-pi.yml`, and the `optimize_pi`
  docs give `uv run ansible-playbook ansible/initial_setup.yml --tags optimize_pi -e
  target=daniel-pi`. The play's `load_secrets` pre_task decrypts. The `claude` user has no age
  key and no login on the Pi (`docs/claude-agent-user.md`).
- **`land.sh` finishes a PR that reaches no tag before it waits on anything.**
  `scripts/deploy_tools/land_lib/pipeline.py:_step_resolve` calls
  `classify.py:shortcut_if_nothing`. That function finishes `nothing-to-deploy` when the PR has
  no resolved tag, no plane, nothing self-applied and no `needs_diff`. Later,
  `deploy.py:deploy_phase` calls `deploy.py:no_tag_outcome`, which is `NoReturn`, whenever
  `resolved_tags` is empty. The last entry of `pipeline.py:_STEPS` is `health_verdict.health`,
  also `NoReturn`.
    - `_STEPS` runs `_step_resolve` first and `_step_ci` ("waiting for master CI") third.
      `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py:tables_in`
      carries `DECIDED parse the copy rather than exec it`, because `land.sh` reads the
      merge commit's copy before it waits on master CI.
    - `no_tag_outcome` ends `needs-manual-apply` when the PR carries a plane. Otherwise it
      ends `nothing-to-deploy` when nothing is self-applied, and reads the tick's state when
      something is. Every verdict line in `health_verdict.health` says "services deployed".
    - `scripts/deploy_tools/land_lib/tools.py:gate_snapshot` makes a detached worktree of a
      SHA and points `UV_PROJECT_ENVIRONMENT` at the primary's `.venv`, because a snapshot
      carries no `.venv` and `uv run` would otherwise build a fresh environment.
- **Prometheus answers on loopback only on its own node.** The comment on
  `observability_prometheus_cluster_ip` in `ansible/roles/k8s/observability/defaults/main.yml`
  says the query backends publish a hostPort on 127.0.0.1 of whichever node runs them. `otelq`
  tries loopback, then the pinned ClusterIP, which works from either node.
  `scripts/diagnostics/probe_lib/core.py:observability_loki_ip` reads Loki's pinned address from
  that file. `probe_lib/core.py:prom_endpoint` and `core.py:k8s_endpoint` go through
  `core.py:sops_extract('domain')`, which needs an age key. `probe.py metric` and
  `probe.py monitors` use them (`probe_lib/monitors.py:run_monitors`,
  `probe_lib/kuma_live.py`), so neither runs as the `claude` user. The operator ruled on
  2026-10-14 that `domain` is published. Domain PR 1 of the *Publish `domain`* item in R2's
  *Steps, as PRs* makes both functions read the inventory value, so this age-key blocker
  goes away once domain PR 1 lands. Domain PR 1 has landed: `core.py:domain` reads
  `ansible/inventory/group_vars/all.yml`, and #5087 deleted `domain` from `secrets.yml`.
  Neither `probe.py metric` nor `probe.py monitors` reads `secrets.yml` any more; only
  `probe.py kuma-drift` does, through `probe_lib/monitors.py:gate_var_state`.
- **The CI gate is one context.** `ci.yml`'s `prek` job carries the required context
  `prek (lint + validate + tests + secrets)`, and the deployer reads the same name from
  `gitops_deploy_ci_contexts`. `ansible/tests/repo/test_every_ci_job_is_gated.py` fails a
  `ci.yml` job that the gate's step does not read. A job in another workflow gates nothing.
  `ci.yml`'s concurrency group keys a push run on its SHA.
- **The CI grading and the token lookup are reusable, and the lookup falls back.**
  `deploy_git.py:ci_verdict` grades check runs. `ansible/roles/setup/common/files/host_lib.py:github_token`
  with `github_get` does every authenticated host read of the GitHub API. `github_token`
  returns None when no variable is set and `gh auth token` fails. Its docstring records that
  anonymous reads share 60 requests per hour per source IP with the tick's own gate.
- **The deployer walks back to the newest green commit.** The comment on
  `gitops_deploy_ci_ancestor_walk_max` (10) in `ansible/roles/setup/gitops_deploy/defaults/main.yml`
  records about 124 merges a day, a median gap of 315s and a p25 gap of 88s, against a
  ~103s CI sweep. Gating on the tip alone parks a green commit behind every later merge's
  sweep, so `deploy_phases.py:_newest_green_ancestor` walks back from the tip.
  `deploy_git.py:ci_walk_candidates` lists the first-parent commits that walk grades. A
  commit on `origin/master` with a green CI verdict is the deployer's whole anchor.
- **Setup roles that share `common` files are listed in one table.**
  `deploy_cross_role.py:SETUP_FILES_SHIPPED_BY_OTHER_ROLES` maps `alert_unit.yml` to
  `_ALERT_UNIT` and `install_host_lib.yml` to `_HOST_LIB`. `_STAMPED`, the importers of
  `stamp_deployed.yml` and `stamp_render.yml`, is `_HOST_LIB` plus three roles.
  `ansible/tests/setup/test_setup_cross_role_files.py:test_the_deployers_table_equals_the_trees_cross_role_files`
  asserts that the table equals the tree's imports.
- **monitor-bridge already reads the Pi through Prometheus.** The `pi_pressure` row of
  `ansible/roles/k8s/monitor-bridge/files/check_table.py:CHECKS` carries `gate="prometheus"`
  and reads daniel-pi's node exporter. Nothing pages on a host-package metric, because none
  exists.
- **The setup-role task walkers live in the test helpers.**
  `ansible/tests/_role_census.py:task_files_by_role` lists every plane's task files, setup
  included. `ansible/tests/setup/test_cron_output_goes_to_the_journal.py` pairs it with
  `_helpers.walk_tasks` to read cron tasks. `ansible/tests/_role_tasks.py` reads only
  `ansible/roles/k8s` (`_K8S_ROLES`).
- **Port 8791 is free.** `git grep -n 8791` matches nothing. `deploy_ui_port` is 8790.
- **A host address has one definition site.** `ansible/tests/repo/test_lan_facts_have_one_source.py`
  fails a `.j2`, task file or Python module under `ansible/`, `scripts/` or `docs/` that types a
  host's address instead of reading `server_ip`. Prose in `.md` files is exempt. Its
  `SCAN_ROOTS` holds no `hosts` and its `SCAN_SUFFIXES` holds no `.sh`. `git grep -lE` for
  the seven guarded addresses over `*.sh` and `*.sh.j2` printed nothing at `70591c307`.
- **The Pi's disarm walkthrough is written for the cron.** `docs/pi-host-tuning-record.md`,
  *Disarming it*, applies `--tags recovery-health` and reads the result with
  `crontab -l -u <sys_user>`. The same page calls each heartbeat a cron.
- **No check holds a host script to Python 3.12.** Ubuntu 24.04's `/usr/bin/python3` is 3.12.
  `host_python_version` is `3.14.6`, and
  `ansible/tests/setup/test_host_python_pin.py:test_host_python_pin_tracks_the_repo_minor` ties
  it to `.python-version` (`3.14`). CI and `uv run` run 3.14.

## Design

### Shared names for the host slices

H4 to H7 use these names unchanged. A slice that needs a different value says so in its own
*Open questions*.

- **Source tree.** `hosts/` at the repo root, admitted by `!/hosts` in `.gitignore`.
    - `hosts/<hostname>/package.yml` is a host manifest. It lists the components, `Depends`,
      the SOPS file the host decrypts (H4), and which units are enabled.
    - `hosts/components/<component>/` holds one component: its templates, its static files,
      and a `component.yml`. That file maps each file to its installed path, lists its units,
      its legacy entries (below) and the secret keys its units load at run time, and carries a
      `defaults:` map for its own variables.
    - `hosts/packages/<name>/` holds a shared package: `homelab-updater` (H3) and
      `homelab-repos` (H7).
    - `hosts/lib/` holds the scripts several packages ship, with tests in `hosts/lib/tests/`.
    - `hosts/bootstrap-host.sh` is the bring-up script.
    - `hosts/keys/` holds the public keys hosts trust. PR H3.4c commits the first,
      `homelab-archive-keyring.gpg`. The builder's anchor reads no key ([ADR-0021](../../adr/0021-the-host-package-builder-trusts-master-plus-green-ci.md)), so
      nothing a candidate commit puts there can move it.
    - A package ships a key with a `key:` file entry, which names `hosts/keys/<name>.gpg`
      without its suffix, the way a `lib:` entry names `hosts/lib/<name>`. Both are copied
      byte for byte, and they are the only sources outside a component's own directory.
- **Packages and the slice that creates each.** `homelab-updater`, `homelab-daniel-pi` and
  `homelab-tools` in H3, `homelab-daniel-server` in H4, `homelab-daniel-box` in H6, and
  `homelab-repos` in H7. `homelab-tools` is the one architecture-specific package (C5,
  *`homelab-tools`* below).
- **Installed paths.**
    - Scripts go in `/usr/lib/homelab/`.
    - Units go in `/usr/lib/systemd/system/`, and enable links in
      `/usr/lib/systemd/system/<target>.wants/`. An enable link is a symlink to `../<unit>`,
      as `systemctl enable` writes it: systemd adds a `Wants=` only for a symlinked unit.
    - A drop-in for another package's unit goes in `/usr/lib/systemd/system/<unit>.d/50-homelab.conf`.
    - The rendered manifest goes in `/usr/lib/homelab/manifest.json`.
- **Unit naming.** Every unit a homelab package ships is named `homelab-*`. The reconcile
  sweep keys on that prefix, so a census test enforces it.
- **Enable links are per host.** Each host package ships its own enable links, including the
  one for `homelab-updater.timer`. A host arms its updater by shipping that link.
- **Shared units.**
    - `homelab-updater.service` and `.timer` ship in `homelab-updater`. A host package
      overrides the timer's schedule with the drop-in
      `/usr/lib/systemd/system/homelab-updater.timer.d/50-homelab.conf`.
    - `homelab-reconcile.service` ships in every host package.
    - `homelab-secrets.service` arrives in H4, `homelab-compose.service` in H5, and
      `homelab-k3s-restart.service` and `.timer` in H6.
    - On daniel-box, the Ansible setup role `homelab_builder` installs
      `homelab-builder.service`, `homelab-builder.timer`, `homelab-repo-sign.service` and
      `homelab-apt-repo.service` until H7 moves them into `homelab-daniel-box`. The role
      holds more than the units, and *What H7 inherits from this slice* lists what the
      move must carry.
- **Repository.**
    - URL `http://10.0.0.215:8791`, which is daniel-box's `server_ip`.
    - Suite `homelab`, component `main`, and `Origin` and `Label` both `homelab`.
    - `Architectures: amd64 arm64`. Every package but `homelab-tools` is `Architecture:
      all` and appears in both indexes.
    - The signed `Release` carries `Valid-Until` seven days after its `Date`, and the
      signer re-signs it daily (C5, *The builder and the repository*).
    - The deb822 source is `/etc/apt/sources.list.d/homelab.sources`, with
      `Signed-By: /usr/share/keyrings/homelab-archive-keyring.gpg`.
    - `homelab-updater` ships both files. It ships the keyring only from PR H3.4c, the PR
      that commits the key, so no render or build before then names a file that does not
      exist. `bootstrap-host.sh` writes both files first, and dpkg takes them over on the
      first install.
    - The builder publishes no `homelab-updater` whose keyring is absent or carries a
      fingerprint other than the signing key's (builder step 6). A wrong keyring would
      otherwise replace the right one on every host at its next install.
    - `hosts/bootstrap-host.sh` is the one file under `hosts/` that types the URL, because it
      runs on a host before anything is installed. A test holds that URL equal to
      daniel-box's `server_ip` (PR H3.6).
    - The script types an address, not a name, because the LAN's DNS runs in the
      cluster. A host that is being bootstrapped, or whose cluster is down, still reaches
      the repository.
- **Builder state.** Two system users split building from signing.
    - `homelab-builder` owns `/var/lib/homelab-builder/`: its own clone (`src/`), scratch
      worktrees, the uv cache (`uv-cache/`), the CI verdict cache, the last published render
      of each package, and `staged/`.
    - `homelab-signer` owns `/var/lib/homelab-signer/`: the GnuPG home (`gnupg/`, 0700) and
      the published tree (`repo/`).
    - `/etc/homelab-builder/` is root-owned. It holds `config.env` (the GitHub token, the CI
      context list, the walk bound and the Prometheus addresses, 0640, group
      `homelab-builder`), `archive-key.fpr`, and the operator's `hold` and `release`
      files (*Staged rollout*). `archive-key.fpr` holds the signing key's fingerprint,
      mode 0644. The role writes it from the signer's GnuPG home on every apply, because
      the builder cannot read that home.
- **Metrics.**
    - The textfile directory is `/var/lib/node-exporter-textfile/` on all three hosts. No
      package in this slice ships it.
        - On the Pi, an `optimize_pi` task under the `node-exporter-host` tag creates it (PR
          H3.6). The task uses M3's owner contract: owner root, group `{{ sys_user }}`, mode
          2775. That task is the Pi's only creator, so the H7 PR that retires it names the
          next one (*What H7 inherits from this slice*).
        - On the k3s nodes, the node-exporter DaemonSet's `DirectoryOrCreate` hostPath
          creates it, and M3 sets the same mode on daniel-box.
        - Every writer uses M3's write rule: `mktemp` in the directory, `chmod 0644`, then
          `mv`. A bare `mktemp` file is 0600, which the exporter's user cannot read.
        - A homelab writer that finds the directory missing exits non-zero. It never creates
          the directory, so a host whose exporter is not set up fails loudly.
    - The updater writes `homelab_host.prom`, with four series. It derives the first three
      from reconcile's record in `applied.json` (*The reconcile unit*), never from dpkg state
      alone:
        - `homelab_host_applied_info{host,sha,version} 1`, the C6 metric. It names the host
          package version of reconcile's last successful run.
        - `homelab_host_update_success_timestamp_seconds{host}`, the end of the last updater
          run that found the installed versions reconciled, the common path included. It is
          the updater's freshness signal.
        - `homelab_host_reconcile_ok{host}`, 1 only when the record names the installed
          versions and says the run succeeded. At 0, the updater holds that host (see
          *Host-plane holds and stop levers*).
        - `homelab_host_verify_mismatches{host}`, the line count of
          `dpkg --verify homelab-updater homelab-<host>`.
    - The signer writes `homelab_repo.prom` with `homelab_repo_published_info{package,sha,version} 1`.
    - The builder writes `homelab_builder.prom` with two series:
        - `homelab_builder_fetch_timestamp_seconds`, the end of the last run whose
          `git fetch` succeeded;
        - `homelab_builder_unbuilt_since_timestamp_seconds{reason,sha}`, the committer time
          of the oldest first-parent commit after the last built commit. It is the tip's
          committer time when the tip does not descend from the last built commit, and 0
          when the tip is the last built commit. `sha` names that oldest unbuilt commit, or
          the tip. `reason` says why the run built nothing newer: `hold`, `ancestry`,
          `keyring`, `ci`, `no-green`, `token`, `tools`, `canary`, or `ok` when it built
          or had nothing to build. `token` is a builder token that `config.env` lacks or that
          the check-runs read refused with 401 or 403, so a revoked token never reads as
          `no-green`. `tools` is a pinned binary whose digest differs
          (*`homelab-tools`*), and `canary` is an open rollout gate (*Staged rollout*).
    - Neither `homelab-builder` nor `homelab-signer` can write the textfile directory. Each
      writes its file under its own state directory. Its unit then runs the role's
      `publish-textfile` script through an `ExecStopPost=-+` line. The `+` runs it as root,
      and the `-` keeps a failed copy from failing the unit. The script copies only series
      whose names start with that unit's prefix, `homelab_builder_` or `homelab_repo_`, and
      writes them with M3's write rule.
    - **Series budget (C7).** Against M1's fleet budget of 140,000 head series, H3 adds
      about 16. They are the Pi's four `homelab_host_*` series, one
      `homelab_repo_published_info` series for each of `homelab-updater`,
      `homelab-daniel-pi` and `homelab-tools`, and the builder's two series. The Pi's
      exporter gains two textfile-collector series, `node_textfile_scrape_error` and one
      `node_textfile_mtime_seconds`. About five more exist while a `sha`, `version` or
      `reason` label changes inside the head block. Each host that H4 and H6 add brings
      four `homelab_host_*` series and one published-info series, so the host plane
      stays under 40.
- **Ordering on the secrets unit (H4).** A unit that reads a credential or a
  secret-bearing file declares `Wants=homelab-secrets.service` and
  `After=homelab-secrets.service`, never `Requires=`. systemd restarts every unit that
  `Requires=` another whenever that other unit restarts. Reconcile restarts
  `homelab-secrets` whenever its inputs hash changes, which includes every package version
  that changes the ciphertext.
    - Reconcile is the only process that restarts a dependent. `homelab-secrets` calls no
      `systemctl`. H4's section *The restart contract* owns this rule, and reconcile restarts
      only the dependents whose outputs changed.
    - Order comes from reconcile's sequence: its call that restarts `homelab-secrets`
      returns before it restarts any dependent. `After=` orders only the boot path and a
      start that pulls `homelab-secrets` in through `Wants=`.
    - At boot the consumer still fails closed, because its credential or symlink target does
      not exist until `homelab-secrets` has run.
- **Credentials (H4).**
    - A single secret value goes to `/run/credstore/<key>`, root 0600, and units read it with
      `LoadCredential=<key>:/run/credstore/<key>`.
    - A secret-bearing file renders to `/run/homelab-secrets/<absolute path>`. The package
      ships a symlink at the real path.
    - The host's age key is `/etc/homelab/age.key`, root 0600.

### Host mode renders no secret

C1's placeholder and escape rules exist for Flux's `postBuild` substitution, and only cluster
mode has a substitution step. Host mode applies neither rule.

- **No secret value, stub or placeholder.** Host mode removes every top-level key name of
  `ansible/vars/secrets.yml` from the context, together with any inventory value that cannot
  expand without one. It renders with strict undefined. A template that reads a secret, directly
  or through an inventory value aliasing one, therefore fails the render, and the error names
  the key.
- **No rewriting of `${`.** A literal `${` in a host template is shell, and host mode emits it
  byte for byte. The three moved scripts and `kuma-push-lib.sh` hold 57 of them between them,
  and all three scripts run under `set -u`.
- **No shared code with R2.** Host mode imports nothing from R2's `placeholders.py`, so H3
  depends on R1 only.
- **A host script reads a secret at run time** from `$CREDENTIALS_DIRECTORY`. In H3 the
  credential source is an interim file written by Ansible (PR H3.8a). H4 replaces it with
  `/run/credstore/`.

### Component variables

A component's `component.yml` carries a `defaults:` map. Host mode layers it beneath
`group_vars/all.yml` and the host's `host_vars`. That is the position a role's
`defaults/main.yml` takes in `scripts/lib/render_context.py:render_context`, and PR H3.1 adds
it to that function as a keyword. Host mode never passes component defaults as `overrides`,
which sit above `host_vars`: there they would beat the `optimize_pi_recovery_restart_enabled`
disarm that `host_vars` carries.

### The host handoff rule (extends C4 to the host plane)

C4 governs the move from Ansible to Flux. This rule governs the move from Ansible to dpkg,
and every host slice applies it.

1. **One path, one owner.** No commit leaves a path written both by an Ansible task that
   applies to a host and by that host's package. The two-writer census in PR H3.2c fails any
   commit that does.
2. **Same PR.** The PR that adds an item to a package deletes the Ansible task that wrote it.
    - If the package ships the same path, dpkg replaces the unowned file on unpack, and
      nothing else is needed.
    - If the path differs, the component lists the Ansible-era path as a legacy entry. Units
      always differ, because package units are renamed `homelab-*`.
3. **The package retires what it replaces.** `homelab-reconcile` removes each legacy entry
   after the package that replaces it has unpacked and its units have started. Ordering is
   therefore package first by construction. No hand-run Ansible absent arm is needed, which
   matters on daniel-server and the Pi, where `has_gitops: false`.
4. **Legacy entries are explicit.** A legacy entry names one of four things:
    - a file path;
    - a cron name, which removes the `#Ansible: <name>` line and the job line after it from
      the named user's crontab;
    - a `cron_file` under `/etc/cron.d/`;
    - an Ansible-era unit, which is disabled and stopped, then its file is removed.
   Reconcile never removes a path that a package owns (`dpkg -S` finds it).

### Package rules (C5, made checkable)

- **Plain files only.** No nfpm content has `type: config*`. The renderer emits no such
  type, and the CI lint fails the build if one appears. A file under `/etc`, such as
  `/etc/logrotate.d/pi-health`, is a plain file too. An enable link is the one entry that is
  not a file: it ships as `type: symlink`, for the reason *Installed paths* gives.
- **Version schema.** Every `nfpm.yaml` sets `version_schema: none`, so the built package
  carries the *Version* string below unchanged. nfpm's default, `semver`, would build
  `0.<count>+g<sha>` as `0.<count>.0+g<sha>`.
- **Maintainer scripts.**
    - `postinst` holds only `systemctl daemon-reload || true`,
      `systemctl --no-block start homelab-reconcile.service || true` and `exit 0`.
    - `prerm` stops `homelab-*` units only when `$1` is `remove`.
    - `preinst` refuses a hostname mismatch and does nothing else.
    - A lint, `lint_script` in `scripts/validate/host_packages.py`, holds every script to a
      whitelist of line shapes. CI runs it over each build, and
      `hosts/lib/tests/test_maintainer_scripts.py` runs it over every shipped render. The
      whitelist admits no `dpkg-divert` line. H4 ships no diversion: its round-1 review
      replaced the two it planned with a `NUT_CONFPATH` drop-in. H7's *In-place edits* is the
      first slice that diverts a file. That slice defines the `preinst` and `postrm`
      lifecycle and adds the `dpkg-divert` line shapes to this whitelist in the same PR.
- **Depends.** Every host package carries `Depends: python3 (>= 3.12)`, because reconcile
  runs under `/usr/bin/python3`.
- **Version.** `0.<git rev-list --count --first-parent <chosen>>+g<12-hex sha of <chosen>>`,
  with `Architecture: all`. `<chosen>` is the commit the builder builds (builder step 4),
  never `origin/master`. The builder can build a green parent while its tip is pending. A
  count taken from the tip would then give the parent and the tip equal numbers, and dpkg
  would order the two by their SHA's hex digits. Step 4 builds only a strict first-parent
  descendant of the last built commit, so each built version's count exceeds the one
  before. C5 still names `origin/master` (*Open questions* 7). The version and SHA stay out
  of `manifest.json`, so a render changes only when the package's content does.
- **Publish on change.** The builder publishes a package only when its rendered tree differs
  from the last published one. A host's applied SHA is therefore the last merge that changed
  that host's package, and a reader compares by ancestry, not by equality.

### The updater

`/usr/lib/homelab/homelab-updater` is a POSIX shell script, run as root by
`homelab-updater.service` (`Type=oneshot`, `Nice=10`, `IOSchedulingClass=idle`,
`TimeoutStartSec=50min`). The limit is derived from reconcile's: reconcile's 30 minutes, step
5's 10-minute lock wait, and 10 minutes for the fetch and the unpack. A test in
`hosts/lib/tests/` reads both rendered units and fails when the updater's limit does not
exceed reconcile's limit plus the lock timeout.

Every verdict the updater writes comes from reconcile's record (*The reconcile unit*). The
installed versions are *reconciled* when the record's `result` names exactly the versions
`dpkg-query` reports for both packages and says the run succeeded. Each run does these steps:

1. If the host holds (`/var/lib/homelab-updater/hold` exists), write the metrics from the
   record with `homelab_host_reconcile_ok 0`, and exit 0 without installing anything.
2. If `dpkg --audit` lists a package, run `dpkg --configure -a`, which finishes a run killed
   after unpacking. dpkg takes its frontend lock without waiting. If `apt-daily` or
   `unattended-upgrades` holds it, the run exits 75 and changes nothing, and the next fire
   retries. Reconcile holds that lock only while it reads, so it never causes this exit.
3. Run `apt-get update --error-on=any` restricted to the homelab source, with
   `-o Dir::Etc::sourcelist=sources.list.d/homelab.sources -o Dir::Etc::sourceparts=- -o
   APT::Get::List-Cleanup=0`. The Ubuntu lists stay as `apt-daily` last left them.
    - The restricted calls, this fetch and step 4's `apt-get indextargets`, also carry
      `-o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache=`, and the fetch carries
      `-o pkgCacheFile::Generate=false`. Without them, `apt-get update` ends by rebuilding
      `pkgcache.bin` and `indextargets` builds one too, each from the homelab source
      alone, so every run, the common path included, rewrites the apt cache. The
      container's common-path case caught both against real apt (H3.3b).
    - Without `--error-on=any`, apt treats a failed fetch as transient: it warns, keeps the
      list the last good fetch left, and can exit 0. Step 4 would then read that old list,
      which still holds `Version:` lines, as "nothing newer". The flag applies to this
      restricted fetch and to bootstrap step 5, never to step 6's full update.
    - A non-zero exit ends the run non-zero. It writes no metric and no hold, and leaves
      `last_success` alone. An unreachable repository, a stopped `homelab-apt-repo.service`,
      a wrong firewall rule and a keyring mismatch therefore all let the success timestamp
      go stale, and `HostUpdateStale` fires once the host's limit passes. One failed fetch
      does not page, because the limit exceeds the longest gap between two fires. A
      `Release` past its `Valid-Until` fails the fetch the same way.
4. Read `dpkg-query -W -f '${db:Status-Abbrev} ${Version}'` for both packages. Read each
   candidate from the homelab `Packages` list that step 3 fetched. `apt-get indextargets`,
   run with step 3's options, names that file, and awk takes the highest `Version:` of each
   package, compared with `dpkg --compare-versions`. This lookup loads no apt cache.
    - If the scan finds no `Version:` for `homelab-updater` or for `homelab-<host>`, because
      the list is missing, renamed or reshaped, the run exits non-zero and writes no metric.
      "Nothing found" never reads as "nothing newer", so the success timestamp goes stale
      and `HostUpdateStale` fires.
    - A package whose status is not `ii `, such as one half-installed by a kill during
      unpack, is reinstalled with `apt-get install --reinstall`. The run continues at step 7.
    - A candidate newer than the installed version continues at step 5.
    - Otherwise this is the common path. If step 2 configured anything, or the installed
      versions are not reconciled, the run continues at step 7. If they are, it continues at
      step 8. The common path costs one small HTTP fetch and one text scan.
5. `apt-get install -y --no-install-recommends -o DPkg::Lock::Timeout=600 homelab-updater
   homelab-$(hostname)`.
6. If that install fails to resolve or fetch a dependency, run one full `apt-get update` and
   retry once. A `Depends` on an Ubuntu package can need fresher Ubuntu lists than step 3
   fetched. The full update runs at most once a day, recorded in a stamp file under
   `/var/lib/homelab-updater/`, and never between 04:00 and 05:59. Otherwise the run exits
   non-zero and the next run tries again. Any other install failure, such as a lock wait
   that timed out, ends the run with no full update. The retry runs with
   `DPkg::Lock::Timeout=0`, so one run waits on the lock at most once, and
   `TimeoutStartSec` budgets that one wait.
7. Run `systemctl start homelab-reconcile.service` synchronously. Its exit status is not the
   verdict. Step 8 reads the record that the run wrote.
8. Read the record, then write `homelab_host.prom` with M3's write rule (*Shared names*,
   Metrics). If the textfile directory is missing, the run exits non-zero and writes
   nothing.
    - If the installed versions are reconciled: `homelab_host_applied_info` from the
      record, the success timestamp now (also kept in `/var/lib/homelab-updater/last_success`),
      `homelab_host_reconcile_ok 1`, and the `dpkg --verify` count for the two homelab
      packages only. Exit 0.
    - Otherwise: write the hold file naming the installed versions, write
      `homelab_host_applied_info` from the record's `last_ok`, keep the last success
      timestamp, write `homelab_host_reconcile_ok 0`, and exit non-zero. This arm covers a
      record that says the run failed. It also covers a record that still names older
      versions after step 7, because reconcile was killed or a concurrent transaction moved
      the versions.

An updater killed anywhere between the install and step 8, by its timeout, the OOM killer or
a test, leaves the record naming older versions. The next run's step 4 therefore runs
reconcile before it writes any metric, and a failure there holds the host as usual.

The shared timer fires 5 minutes after boot and then every 5 minutes, with
`RandomizedDelaySec=60`. That is the schedule on daniel-box and daniel-server.

The Pi package overrides it with its drop-in. The drop-in's first line, an empty
`OnCalendar=`, resets every inherited trigger. Its second line is
`OnCalendar=*-*-* 01..03,06..22:05/15:00`. So the Pi checks every 15 minutes and skips 23:00
to 00:59, which holds the midnight cluster, and 04:00 to 05:59, which holds its two apt
timers. Until PR H3.9b, the Pi package ships no enable link for `homelab-updater.timer`, so
the updater runs only when the operator starts it. Reconcile's step 3 never starts that timer
while it is neither enabled nor active, whatever its file or drop-in hash does. H3.9b's link
reaches the Pi only through an updater run, so the operator starts the first one by hand
after the builder publishes H3.9b. That run's reconcile then starts the timer (step 2).

### The reconcile unit

`homelab-reconcile.service` (`Type=oneshot`, `TimeoutStartSec=30min`) runs
`/usr/lib/homelab/homelab-reconcile`. That is a stdlib script under `/usr/bin/python3`, which is
3.12 on Ubuntu 24.04.

It first takes dpkg's frontend lock, `/var/lib/dpkg/lock-frontend`, with a blocking
`fcntl.lockf`. That is the lock type apt and dpkg use. A start from a postinst therefore waits
until the whole apt transaction has finished. Every start inside one transaction merges into
that one job, which reads the manifest only after the lock is free. The updater's synchronous
start in step 7 joins the same job, or starts a fresh one.

It holds the lock only while it reads. It reads `/usr/lib/homelab/manifest.json`, its own
last state in `/var/lib/homelab-reconcile/applied.json`, and the status and version of both
packages from `dpkg-query`. If either package's status is not `ii `, it records a failed
result for the versions it read and exits non-zero without acting, because a half-installed
package's manifest may belong to either version. Otherwise it releases the lock before step 1.
No restart therefore holds up `apt-daily`, `unattended-upgrades` or the updater's own
`dpkg --configure -a`. A transaction that starts while reconcile acts can change files under
it. Reconcile still records the versions it read, which then differ from the installed ones,
so the updater's next run starts reconcile again (*The updater*, step 4).

`applied.json` holds two things:

- **Per-entry state:** the manifest's unit list, each unit's file and drop-in hashes, each
  shipped enable link, and each legacy entry already retired.
- **The record:** `result`, with the two package versions this run read under the lock,
  whether every step succeeded, and the finish time; and `last_ok`, the same fields from the
  newest run that succeeded.

Reconcile advances a per-entry item only when its step succeeded on that item, or when the
step's rule left the item alone. A unit whose stop, start or restart failed keeps its old
hashes. A unit whose stop failed stays in the recorded unit list. A legacy entry stays
unretired until its removal succeeds. The next run, including the one `--clear-hold` starts,
therefore retries exactly the actions that failed, and repeats none that succeeded. A first
run, with no `applied.json`, treats every hash as changed.

After releasing the lock, it does these steps in order:

1. It stops and `reset-failed`s three sets of units:
    - every `homelab-*` unit that the previous manifest shipped and the current one lacks;
    - every loaded `homelab-*` unit whose `LoadState` is `not-found`;
    - every `homelab-*` timer whose enable link the previous manifest shipped and the current
      one lacks. This rule covers timers only, because a timer-activated service has no
      enable link and must not be stopped mid-run.
   It never stops a unit another package ships. When a homelab drop-in on such a unit
   (`nut-monitor`, `k3s`, `k3s-agent`) disappears, the unit's drop-in hash changes, and
   step 3 applies that unit's restart policy instead.
2. It starts every enabled unit that is not active. A `homelab-*` unit is enabled when the
   manifest ships its enable link. It never starts a masked unit.
3. It walks every unit whose file or drop-in hash differs from `applied.json`. It skips a
   unit the manifest marks `windowed` (k3s, H6) or `manual`. `homelab-updater.service` and
   `homelab-reconcile.service` are always `manual`, so an upgrade never restarts the run that
   installed it. For each other unit:
    - It reads `systemctl show -p LoadState --value` first, and never touches a unit that
      reads `masked`. A masked unit may still have an enable link in the manifest, so this
      test comes before the next one.
    - It acts only when the unit is enabled or active. For a `homelab-*` unit, *enabled*
      means the manifest ships its enable link, the definition step 2 uses, and reconcile
      never asks `is-enabled` about it. For another package's unit, it means
      `systemctl is-enabled` prints `enabled`, as H4 defines. *Active* means
      `systemctl is-active` prints `active`. A service gets `try-restart`. A timer gets
      `restart`, so a schedule change takes effect.
    - A unit the rule leaves alone takes its new file at its next start. This rule keeps the
      H3.6 Pi package's `homelab-updater.timer` inactive on the first run, which treats every
      hash as changed, and on every later drop-in change before H3.9b. It also keeps a
      recovery timer that H3.8b disarmed stopped when its unit file changes later.
    - **H4.1b replaces this step's service verb.** H4's section *The restart contract*
      builds one restart set and, for each unit in it that is enabled or active, runs
      `systemctl reset-failed` and then `systemctl restart`. That is this step's predicate.
      `try-restart` does nothing to a failed unit, so a failed `nut-monitor` (H4) or
      `homelab-compose` (H5) would stay failed under this step's verb.
    - For services the two verbs act alike on every unit H3 ships. Each H3 service is either
      `manual` or timer-activated with no enable link of its own. Running, it is restarted by
      both. Between runs, or failed, it is neither enabled nor active, so neither touches it,
      and its next timer fire runs the new file.
4. It retires legacy entries, as the handoff rule above describes.
5. It runs the per-kind reconcilers the manifest lists. H4 adds the udev reload, and H7 adds
   ufw and libvirt.
6. It writes `applied.json` atomically, with the per-entry state advanced as described above
   and the record, and exits non-zero if any step failed. A run killed before this step
   writes nothing, so the record still names the versions of the run before it.

The package stays configured when reconcile fails, because the postinst's `--no-block` start
returns at once and the postinst ignores its result. The updater reads the record instead
(updater step 8), and a failure holds that host.

### Host-plane holds and stop levers

Each lever stops promotion at a different scope. `docs/host-packages.md` is the runbook for
all three. PR H3.4b creates it with the deployer-hold procedure, and H3.5b completes it.

- **Per host, automatic.** A failed reconcile makes the updater write
  `/var/lib/homelab-updater/hold` and report `homelab_host_reconcile_ok 0`. That host installs
  nothing newer until the operator runs `sudo /usr/lib/homelab/homelab-updater --clear-hold`.
  That command reruns reconcile, which retries the actions that failed, and removes the hold
  only if the record then shows the installed versions reconciled. A fix merged after the
  failure reaches that host only after the clear. That is the trade `hold_sha` makes for the
  cluster.
- **Per host, when the installed package is the cause.** `--clear-hold` cannot pass while
  the installed version itself makes reconcile fail. `sudo /usr/lib/homelab/homelab-updater
  --clear-hold --install-newest` removes the hold, then runs steps 3 to 8 as a timer run
  would. It fetches, installs the newest published versions, runs reconcile and writes the
  verdict, and a failure writes the hold again. The operator runs it once the builder has
  published the fix or the revert. With nothing newer published, it acts as `--clear-hold`.
- **Per host, by hand.** `sudo systemctl mask --now homelab-updater.timer` stops updates on
  one host. Reconcile never starts or restarts a masked unit, so a later package cannot undo
  it.
- **Every host, by hand.** A root-owned `/etc/homelab-builder/hold` on daniel-box stops the
  builder publishing anything. The builder user cannot remove it.
- **Every host but the canary, by hand.** A root-owned `/etc/homelab-builder/release`
  that names a SHA closes an open rollout gate before the canary reports (*Staged
  rollout*). The builder user cannot create it.

Two alert paths report the host plane, so a hold or a stall is never silent:

- **Host rules (PR H3.5d, C7).** Two rules in M1's rules directory page through
  Alertmanager. Each carries C7's labels: `service` is the host the series names, and
  `severity` is `critical`. Both survive M6, and H5 relies on them.
    - `HostReconcileFailed` fires when `homelab_host_reconcile_ok` reads 0 for 15 minutes.
      Its annotation names the host and both `--clear-hold` commands.
    - `HostUpdateStale` fires when an expected host's
      `homelab_host_update_success_timestamp_seconds` is older than that host's limit, or
      absent while that host's exporter `up` reads 1. The expected hosts and their limits
      are a default of the observability role, empty in H3.5d. H3.9c adds daniel-pi.
- **Builder arms (PR H3.5c).** monitor-bridge's `host_packages` row pushes `down` in two
  cases. If H3.5c merges after M6's A1 shadow PR, two alert rules carry the same two cases
  instead (*Steps*, H3.5c). The cases:
    - `homelab_builder_fetch_timestamp_seconds` is older than
      `monitor_bridge_builder_max_fetch_age_s`, 1800 seconds by default, or absent. That
      covers a dead builder timer, a missing token and a fetch that keeps failing;
    - `homelab_builder_unbuilt_since_timestamp_seconds` is non-zero and older than
      `monitor_bridge_builder_max_lag_s`, 2 hours by default. That covers a trust refusal,
      a keyring refusal, a tools refusal, a rewritten master, an empty CI context list, a
      CI context that no longer matches, a builder hold, a rollout gate the canary never
      closes, and a master that stays red for 2 hours. In each of these the builder runs
      and exits 0 while no host receives a change. The bound exceeds the ~103s master CI
      run many times over.
- The builder and the signer each carry `OnFailure=` through
  `ansible/roles/setup/common/tasks/alert_unit.yml`, which posts to Discord once per
  failure. The builder fires every 2 minutes, so it exits non-zero only once for each
  condition (builder step 8). The bridge row carries a condition that persists.

### The builder and the repository (daniel-box)

`homelab-builder.timer` fires every 2 minutes. `homelab-builder.service` runs as
`homelab-builder`, with `OnSuccess=homelab-repo-sign.service` and
`OnFailure=homelab-builder-alert.service` from `ansible/roles/setup/common/tasks/alert_unit.yml`.
Each run does these steps:

1. If `host_lib.py:github_token` returns None, exit 1 before any request. The builder never
   polls GitHub anonymously, because anonymous reads share one quota with the tick's gate.
2. `git fetch origin master` into `/var/lib/homelab-builder/src`. The builder never touches
   the primary checkout, so an untracked file there cannot park it.
3. If `/etc/homelab-builder/hold` exists, exit 0 without building. The hold comes after the
   fetch, so a held builder still reports how far master has moved past it.
4. Choose the commit to build. The fetched tip must descend, by first parent, from the last
   commit the builder built, which it reads from its own state. Otherwise nothing is built,
   and the run fails (step 8). A rewritten master is the usual cause, and
   `docs/host-packages.md` has the reset. The builder then walks first-parent from the tip
   back to that last built commit, at most the walk bound in `config.env`. The role renders
   that bound from `gitops_deploy_ci_ancestor_walk_max`, the bound
   `deploy_phases.py:_newest_green_ancestor` uses. It takes the newest commit in the walk
   whose CI verdict is green.
    - It grades each verdict as
      `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_git.py:ci_verdict` does,
      over the contexts in `CI_CONTEXTS` in `/etc/homelab-builder/config.env`. That list
      names the `prek` gate, which PR H3.3a makes depend on the host-package job.
    - The builder reads the context list and the walk bound from `config.env` only, never
      from a path in its clone. The role renders `CI_CONTEXTS` from
      `gitops_deploy_ci_contexts` with the expression
      `ansible/roles/setup/gitops_deploy/templates/config.env.j2` uses for the deployer, and
      the bound from `gitops_deploy_ci_ancestor_walk_max`. Both are `gitops_deploy`
      defaults that another role reads. H3.5a therefore adds the row
      `ansible/roles/setup/gitops_deploy/defaults/main.yml` naming `homelab_builder` to
      `deploy_cross_role.py:SETUP_FILES_SHIPPED_BY_OTHER_ROLES`, as the `claude_code` and
      `k3s` defaults rows do. The defaults-read census in
      `ansible/tests/setup/test_setup_cross_role_files.py` fails the PR without that row.
      With it, a change to either key re-applies the builder's config. That route ends when
      H7.2e moves both keys into inventory, and H7.2e owes its replacement (*What H7
      inherits from this slice*).
    - An empty or missing `CI_CONTEXTS`, or a walk bound that is not a positive integer,
      builds nothing, and the run fails (step 8) with `reason="ci"`. The docstring of
      `ci_verdict` says an empty set disarms the gate and returns `pass`, and
      `scripts/deploy_tools/await_ci.py:DisarmedGateError` refuses that case for the same
      reason. The builder refuses it before it grades anything.
    - It caches each green verdict per SHA, and polls GitHub again for a commit still pending
      or red. A red verdict is never cached, because `gh run rerun --failed` can turn the
      same SHA green, and the deployer's gate reads it afresh (#5786). Step 1
      guarantees a token, so the walk never runs anonymously.
    - A pending or red tip over a green parent builds the parent. When no commit in the walk
      is green, it exits 0 and walks again next tick. That exit stays 0, because a pending
      tip is normal. The unbuilt-since metric reports a walk that stays empty.
5. The anchor is step 4's result: a commit on `origin/master`'s first-parent chain with a
   green CI verdict. `deploy_git.py:ci_walk_candidates` gives the GitOps deployer the same
   anchor (C5). The builder checks no commit signature, because master commits carry
   GitHub's signature, not the operator's (*Baseline*).
    - The builder adds no other check. The operator chose option (a) of *Open questions*
      1, and [ADR-0021](../../adr/0021-the-host-package-builder-trusts-master-plus-green-ci.md) records it.
6. Check out the chosen commit in a scratch worktree, then run the renderer there with
   `uv run --frozen`: `host` mode for every `hosts/*/package.yml`, and `package` mode for every
   package under `hosts/packages/`. The uv cache is `/var/lib/homelab-builder/uv-cache`, and the
   interpreter is `host_python_version`.
    - Then it reads the fingerprint of `/usr/share/keyrings/homelab-archive-keyring.gpg` in
      `homelab-updater`'s render with `gpg --show-keys --with-colons`, and compares it with
      `/etc/homelab-builder/archive-key.fpr`. When the keyring is absent from the render, or
      its fingerprint differs, the run builds nothing and fails with `reason="keyring"`
      (step 8). A placeholder key, a revert of H3.4c or a key rotated on one side only
      therefore never reaches a host.
7. Run `nfpm package` for each package whose rendered tree differs from its last published
   render, with the version from *Package rules*. Write `staged/chosen`, which names the
   chosen commit's 12-hex SHA, and stage only the packages that *Staged rollout* admits.
   Record the chosen commit as the last built one. Exit 0.
8. Every run that passed step 2 writes `homelab_builder.prom` before it exits, whichever
   step ends it, and `publish-textfile` copies it (*Shared names*, Metrics). Any failing
   run, step 1's included, records its reason and the tip it met, if any. A later run that fails for the same reason on
   the same tip exits 0 instead. `OnFailure=` therefore posts once for each condition, and
   the bridge row's two builder arms report a condition that persists.

`homelab-repo-sign.service` runs as `homelab-signer`, with
`OnFailure=homelab-repo-sign-alert.service` from `alert_unit.yml`. It runs a script the role
installs from its own `files/`, never code from the builder's clone at run time. The role
itself reaches daniel-box from master through the tick, like every setup role.

When `staged/` is empty, the signer exits 0, unless the published `Release` carries a
`Date` more than 24 hours old. Then it runs steps 3 and 4 alone, which re-signs the
unchanged tree with a fresh `Valid-Until`. The builder's `OnSuccess=` starts the signer
after every builder run that exits 0, a held run included, so the re-sign happens daily
while the builder timer runs. Otherwise the signer does these steps:

1. Check every entry of `staged/` before it copies anything. It accepts `staged/chosen`
   and `.deb` files whose control `Package` starts with `homelab-` and whose `Version`
   ends in `+g` and the SHA that `staged/chosen` names. Any other entry, another package
   name, or a version that names another commit is a refusal. The signer then moves the
   whole of `staged/` into `refused/` under its own state directory, publishes nothing,
   and fails the run, so `OnFailure=` posts once.
2. Copy the accepted packages into `repo/pool/`.
3. Regenerate the `amd64` and `arm64` `Packages` indexes and `Release` with
   `apt-ftparchive`. `Release` carries `Valid-Until` seven days after its `Date` (C5).
4. Sign `InRelease` and `Release.gpg` with its key, and swap `dists/` in atomically.
5. Write `homelab_repo.prom` under its own state directory, then empty `staged/`. That
   directory is group-owned by `homelab-signer` with mode 2770. `publish-textfile` copies
   the metric file into the textfile directory (*Shared names*, Metrics).

**Sandboxing.** The builder and signer units run under systemd sandboxing, and a test
holds each directive (*Verification*, Builder):

- Both units set `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`,
  `PrivateTmp=yes`, `PrivateDevices=yes`, `ProtectKernelTunables=yes`,
  `ProtectKernelModules=yes`, `ProtectControlGroups=yes`, `RestrictSUIDSGID=yes` and an
  empty `CapabilityBoundingSet=`.
- `homelab-builder.service` sets `ReadWritePaths=/var/lib/homelab-builder`. That directory
  holds the clone, the scratch worktrees, `staged/` and the uv cache, and the role points
  `UV_PYTHON_INSTALL_DIR` into it as well.
- `homelab-repo-sign.service` sets `ReadWritePaths=/var/lib/homelab-signer` and
  `/var/lib/homelab-builder/staged`, and `PrivateNetwork=yes`, because signing needs no
  network.
- Each unit's `ExecStopPost=-+` line runs `publish-textfile` outside the sandbox, because
  systemd's `+` prefix exempts that one command. The script is the role's own, and it
  copies only the unit's prefixed series.

Sandboxing bounds what a builder run can touch on daniel-box. It does not bound what the
builder hands the signer, and *Risks* says so.

`homelab-apt-repo.service` runs `python3 -m http.server --bind 10.0.0.215 8791 --directory
/var/lib/homelab-signer/repo` with `DynamicUser=yes` and `ProtectSystem=strict`. apt trusts
the signature on `InRelease`, not the transport.

A ufw rule admits 8791/tcp from daniel-server and daniel-pi only. When the repo is
unreachable, a host keeps its installed version, and its updater fails at step 3 without
advancing its success timestamp. The role's templates and the
`homelab.sources` template read daniel-box's `server_ip` and the peers' `server_ip`, and never
type an address. `hosts/bootstrap-host.sh` is the one exception, held equal to `server_ip` by
a test (*Shared names*, Repository).

The builder, the signer and the repository all run on daniel-box. *Risks* accepts that
concentration.

**Recovering from a revoked archive key.** `Valid-Until` bounds how long a host accepts an
old index signed by a working key. It does not help once the key itself is revoked,
because whoever holds the revoked key can sign a fresh `Release`. `docs/host-packages.md`
carries this procedure from H3.5b. Each host step runs from a LAN session and is
**operator-only**:

1. On each host, `sudo systemctl mask --now homelab-updater.timer`, so no host installs
   anything while it still trusts the revoked key. `HostUpdateStale` fires for each
   masked host once its limit passes, which is expected.
2. On daniel-box, set the builder hold and generate the replacement key in the signer's
   GnuPG home. Export its public half with H3.4c's command, and commit it to `hosts/keys/`
   with the matching pin in `hosts/bootstrap-host.sh`, in one PR. The role's next apply
   rewrites `archive-key.fpr`, and builder step 6 then accepts the render.
3. Release the builder hold. The signer re-signs the tree with the replacement key.
4. On each host, run `sudo ./bootstrap-host.sh <hostname>` from the commit that carries
   the replacement pin. Its step 2 verifies the keyring against that pin, and its step 5
   installs the newest packages, whose `homelab-updater` ships the same keyring. Then
   `sudo systemctl unmask homelab-updater.timer` and `sudo systemctl start
   homelab-updater.timer`.

A host whose `HostUpdateStale` still fires after step 4 has not recovered.

### Staged rollout (C5)

A run whose changed set holds more than one host package stages only the canary's package.
The others publish once the canary reports `homelab_host_reconcile_ok 1` at that version,
or when the operator overrides. PR H3.12 adds the gate, which changes nothing until H4
publishes a second host package.

- **The canary is daniel-pi.** Each host costs something different when a package breaks
  it:
    - daniel-box runs the builder, the signer and the repository, so a package that breaks
      it can stop the publish of its own fix. It always comes last.
    - daniel-server runs half the cluster's workloads and the UPS shutdown chain.
    - daniel-pi runs four LAN utilities and the WireGuard endpoint. A package that breaks
      it costs off-LAN access until a LAN session repairs it (*Risks*). It is also the
      pilot, and the hostname every container case runs under.

  The order is daniel-pi, daniel-server, daniel-box. When the Pi's package is not in a
  run's changed set, the first host in that order whose package changed is that run's
  canary.
- **The gate.** A run that stages the canary's package at version V records V's SHA as an
  open gate in the builder's state. While a gate is open, the builder builds the canary's
  package as usual, and moves the gate to each newer canary version. It builds no other
  host package, so their renders keep differing from the last published ones.
- **Closing the gate.** Each run reads `homelab_host_applied_info` and
  `homelab_host_reconcile_ok` for the canary through the loopback-then-ClusterIP transport
  of the land host wait (H3.9a item 3). The gate closes when the canary's applied SHA
  equals or descends from V's SHA with `homelab_host_reconcile_ok` at 1. The next run
  builds the deferred host packages at its own chosen commit, so their versions name the
  commit that `staged/chosen` names, as the signer requires.
- **The override.** A root-owned `/etc/homelab-builder/release` that names V's SHA, or a
  descendant, closes the gate at once. The operator removes the file after use.
- **An open gate pages.** While a gate is open, the unbuilt-since series carries
  `reason="canary"`, its `sha` names V's commit, and its value is that commit's committer
  time. H3.5c's lag arm fires once the gate stays open past 2 hours. A held canary fires
  `HostReconcileFailed` first. An unreachable Prometheus keeps the gate open, which fails
  closed.
- **Shared packages are outside the gate.** `homelab-updater` and `homelab-tools` publish
  in the run that builds them, and every host installs them at its next fire (*Open
  questions* 10).
- **Under the Pi's daily fallback schedule** (*Risks*, apt memory), daniel-server is the
  canary. A daily canary would hold every other host for up to a day and trip the lag arm.

### CI (builds, never signs)

The host-package checks are one job, `host_packages`, in `.github/workflows/ci.yml`, not a
workflow of their own. The `prek` gate lists it in `needs` and reads `needs.host_packages.result`
in its step, so the required context covers it. The job inherits `ci.yml`'s SHA-keyed
concurrency group on push. It needs no secrets, and it does these steps:

1. It renders every package at the PR head and at its merge base, on every run. A path filter
   would miss a `group_vars` change that alters a render, such as a `containers_list` edit that
   changes the Pi's recovery script.
2. If no rendered tree differs, the job ends green here, unless the PR changes an input of
   the container cases. Those inputs change no render: the test image's build context
   `hosts/lib/tests/container/`, which holds the fixture specs, and the cases' own scripts.
   `CASE_INPUTS` on the render step lists them, and
   `ansible/tests/repo/test_host_package_case_inputs_census.py` holds that list to
   every `validate` module the cases script imports (#5004). An edit to the
   `host_packages` job itself counts too, because its steps invoke the cases.
   `host_packages.py job-changed` compares the job as parsed YAML, so an edit to a comment
   alone does not count. A renderer change that alters only the cases' fixture counts too,
   because the fixture exercises renderer paths no real package does until H3.6.
   `host_packages.py fixture-changed` renders the fixture in each tree with that tree's own
   renderer and compares the output, so an edit to the renderer's files alone does not count
   (#5066). Such a PR runs steps 3 to 5. Each of these verdicts fails the step when it cannot
   be computed, rather than reading as no change (#5071).
3. It builds both renders with nfpm pinned by version and sha256, and runs the
   maintainer-script lint and the `type: config` lint.
4. It builds the test image from `hosts/lib/tests/container/Dockerfile`. The job does not
   rely on what the stock image carries, which is a minimal root filesystem. The job is a
   matrix over `ubuntu:24.04` and `ubuntu:26.04`, the releases the fleet runs or is moving
   to (C5). Each leg's image starts from its release pinned by digest and installs
   `systemd`, `systemd-sysv` and `python3` from Ubuntu's snapshot archive
   (`snapshot.ubuntu.com`) at a pinned timestamp, so a rebuild resolves the same versions.
   The build step writes `dpkg-query -W systemd python3` to the job summary. The image also
   carries these three, so every case, the cold install included, starts with them:
    - the textfile directory, with M3's owner and mode, which `optimize_pi` creates on the Pi;
    - a local diversion, made before the test's own source is written:
      `dpkg-divert --local --no-rename --divert /var/lib/homelab-test/homelab.sources.shipped
      --add /etc/apt/sources.list.d/homelab.sources`. dpkg then unpacks the shipped
      `homelab.sources` at the diverted path, outside `sources.list.d/`, and leaves the
      test's file in place. Without it, the first install would replace the test's source
      with the shipped one, and every later case would fetch from daniel-box's address,
      which a runner cannot reach. The diversion lives in the test image, not in a shipped
      maintainer script, so the maintainer-script lint, which reads shipped scripts only,
      still admits no `dpkg-divert`.
    - The test's source is deb822, because updater step 3 reads only `homelab.sources`:
      `URIs: http://127.0.0.1:8791`, `Suites: homelab`, `Components: main` and
      `Signed-By: /srv/repo/homelab-test-keyring.gpg`. That keyring is the public half of a
      throwaway key `host_fixtures.publish` generates and signs `InRelease` with, so apt
      checks the signature and the `Valid-Until` as it does on a host (#5147). `Trusted: yes`
      would accept an index signed by any key or by none. The keyring the package ships from
      H3.4c is installed but unused, and needs no diversion.
5. It boots that image with systemd as PID 1 (`--privileged --cgroupns=host`) and
   `--hostname daniel-pi`, because the Pi package's `preinst` refuses any other hostname.
   Before any case it asserts that `python3 --version` prints the minor its matrix leg
   names, `Python 3.12` on 24.04, so reconcile runs under the interpreter each release
   carries. Every case below runs on both legs. It serves both builds from a local signed
   repository with `python3 -m http.server` on `127.0.0.1:8791`, as daniel-box serves the
   real one, and runs the updater against it. Every case but two asserts that the server's
   log holds that case's `GET` of `dists/homelab/InRelease` or `dists/homelab/Release`. The stopped-server case has no
   server, and the lock case exits at step 2, before the fetch, so it asserts the log holds
   no `GET`. Every case asserts that `/var/lib/apt/lists/` holds a list for the test
   repository and none named after daniel-box's address. Until H3.6 ships the Pi package,
   every case installs a fixture `homelab-daniel-pi` of the same shape, rendered with the
   real reconcile component from `hosts/lib/tests/container/fixture/`. The cases are:
    - after the upgrade to the head version, stop the local server, run the head version's
      updater, and assert that it exits non-zero and leaves
      `/var/lib/homelab-updater/last_success` and the success timestamp in
      `homelab_host.prom` unchanged. The case goes red when step 3 lets a failed fetch
      through, whether apt exits 0 or the updater ignores its exit. It runs after the
      upgrade because the base version's updater is master's, which a change under review
      does not reach. Every other case is its green half, so `--error-on=any` must also
      pass a fetch from the signed local repository;
    - after the stopped-server case, serve the same packages from an index whose `Valid-Until`
      passed an hour ago, and assert that the head version's updater exits non-zero and
      leaves `last_success` and the success timestamp unchanged. It is the container half
      of *Verification*'s *Valid-Until* item;
    - install the base version, and assert that `homelab_host.prom` exists in the textfile
      directory with mode 0644 after that first run;
    - upgrade to the head version, and assert that every dropped file is gone and every
      dropped unit is stopped;
    - remove the package;
    - cold-install the head version on a fresh container;
    - kill the updater during unpack, at a deterministic point. A fixture head version
      carries a 64 MiB payload file. The test waits until that file's `.dpkg-new` path
      exists, which dpkg writes only while it unpacks the package, then kills the updater's
      cgroup. It asserts that `dpkg --audit` lists the package before the next run, so a kill
      that missed the unpack fails the case. The next run must leave `dpkg --audit` empty;
    - upgrade a package that changes `homelab-updater.service`, and assert the running
      updater finishes with no restart;
    - install the H3.6 Pi package by hand, with no enable link for `homelab-updater.timer`,
      and assert that the timer stays inactive, then change its drop-in and assert the same;
    - hold dpkg's frontend lock from the test, start the updater with a package `dpkg --audit`
      lists, and assert that it exits 75 and writes nothing;
    - run the common path and assert that `/var/cache/apt/pkgcache.bin` is unchanged.
6. It writes the file-level package diff to the job summary.

The job has no `name:` key. GitHub names each matrix leg after the job id and the leg's
values, such as `host_packages (24.04, 3.12)`. The gate census in
`ansible/tests/repo/test_every_ci_job_is_gated.py` falls back to the job id, and the gate's
`needs.host_packages.result` fails when either leg fails. Later host slices extend this job
and add no workflow of their own. They read each leg's result from the `ci.yml` runs,
filtered by the job name's prefix:

```bash
gh run list --workflow ci.yml --branch master --limit 3 --json databaseId,headSha,conclusion
gh run view <run-id> --json jobs \
  --jq '.jobs[] | select(.name | startswith("host_packages")) | [.name, .conclusion]'
```

### `homelab-tools` (C5)

`homelab-tools` is the one architecture-specific package. nfpm builds it for `amd64` and
`arm64` from pinned upstream release binaries, `sops` and `chezmoi` among them. PR H3.11
adds it, and the Pi is its `arm64` pilot.

- `hosts/packages/homelab-tools/package.yml` lists each tool with its version and, for each
  architecture, its upstream URL and its sha256. A Renovate manager bumps each pin, as the
  nfpm pin's manager does (H3.3a).
- Each binary installs at `/usr/lib/homelab/bin/<tool>`, never on `PATH`. `sops` is
  `/usr/lib/homelab/bin/sops`, the absolute path H4's `homelab-secrets` calls, and
  `chezmoi` is `/usr/lib/homelab/bin/chezmoi`. A test of the rendered package holds
  both paths as named members, and a fixture that moves `sops` fails it.
- The builder downloads each binary into a cache under its state directory, keyed by
  sha256. A file whose digest differs from its pin builds nothing, writes
  `reason="tools"`, and fails the run (step 8).
- Its version follows *Package rules*, and publish-on-change republishes it only when a
  pin changes.
- A host takes it through a `Depends` in its host package. An unversioned `Depends` that
  is already satisfied never upgrades the package, so from H3.11 updater steps 4 and 5
  cover every installed package whose name starts `homelab-`, not only the two.
- CI builds both architectures. A job on an `arm64` runner installs the `arm64` build in
  the test image and runs each tool's `--version` (*Open questions* 9).

### What H7 inherits from this slice

This slice leaves three things in Ansible that a reader outside Ansible depends on: two CI
keys in `gitops_deploy`'s defaults, the `homelab_builder` role, and the `optimize_pi` task
that creates the Pi's textfile directory. H7 deletes Ansible, so each one needs a new home
in the H7 PR that retires its old one. A move that drops one raises no error at the move.
The reader fails later, often on a host the move did not touch.

- **The CI context list and the walk bound.** The builder reads both from `config.env`,
  which the role renders from `gitops_deploy_ci_contexts` and
  `gitops_deploy_ci_ancestor_walk_max` (builder step 4). H7.20a deletes those defaults.
    - H7.2e moves the two keys to `ansible/inventory/group_vars/all.yml`, as
      `ci_required_contexts` and `ci_ancestor_walk_max`. The same PR repoints the builder's
      `config.env` template and its render test to the new keys. It also drops the
      `gitops_deploy/defaults/main.yml` row naming `homelab_builder`, because the census then
      finds no read of that file.
    - A word grep for `gitops_deploy_ci_contexts` and `gitops_deploy_ci_ancestor_walk_max`
      over `ansible/roles/setup/homelab_builder/` lists the builder's reads. That grep
      prints nothing once the move is complete.
    - A builder config that reads a deleted key fails closed. The role's apply fails on the
      undefined variable, and the builder refuses an empty list with `reason="ci"`. Neither
      path grades a commit against an empty context set.
    - H7.14 moves the builder into a package whose config the host renderer writes. That
      config reads the moved keys, so the repoint lands before H7.14.
    - The move ends the route that re-applies the builder's config. The row naming
      `homelab_builder` routes a change to the `gitops_deploy` defaults file only.
      `deploy_changes.py:_BROAD_DEPLOY_PREFIXES` lists `ansible/inventory/`, so the deployer
      applies a change to either key in `group_vars/all.yml` as `deploy.yml`, never as the
      `homelab_builder` setup role. The walk in `scripts/deploy_tools/narrow_lib/foreign_keys.py`
      follows a changed role default outward to the inventory names that carry it. It does
      not route an inventory edit to a setup role.
    - From H7.2e to H7.14, a changed key therefore leaves the builder's `config.env` stale.
      The failure is closed. With a renamed context the builder finds no green commit,
      writes `reason="no-green"` and exits 0, and the bridge's unbuilt-since arm pages once
      the 2-hour bound passes. With an added context the builder grades fewer contexts than
      the deployer. From H7.14 the route returns, because a key change alters the host
      render, and the builder publishes a new package.
    - H7.2e therefore ships a replacement for the route and a red/green test of it. One
      option is a land or tick output that lists `homelab_builder` as owed whenever either
      key changes in `group_vars/all.yml`. Its test feeds a fixture diff that changes the
      key and asserts `homelab_builder` in the output, and a diff that leaves the key alone
      and asserts its absence. The fallback is a runbook step, in the same PR, that
      hand-applies `initial_setup.yml --tags homelab_builder` after any change to either
      key. That step is **operator-only**, because the apply decrypts.
    - The same PR repoints the runbook procedure in `docs/host-packages.md` that matches
      the context list to a renamed CI job (H3.5b) to `ci_required_contexts`, and names
      the replacement step there.
- **The builder role's other pieces.** `homelab_builder` holds these until H7.14 moves the
  builder into `homelab-daniel-box`:
    - the four units, the builder and signer scripts, the two system users and their state
      directories, the ufw rule for 8791/tcp, and the two `alert_unit.yml` imports;
    - the `publish-textfile` script and the two `ExecStopPost=-+` lines that run it. Without
      them neither builder file reaches the textfile directory, and the bridge row's builder
      arms read their series as absent;
    - the builder's once-per-condition failure record (builder step 8). Without it,
      `OnFailure=` posts once every 2 minutes for a persistent condition;
    - `config.env`, the two units' sandboxing directives, the staged-rollout gate and the
      `release` override file;
    - the writer of `/etc/homelab-builder/archive-key.fpr`. The role writes it from the
      signer's GnuPG home on every apply. With the file absent, builder step 6 refuses
      every `homelab-updater` build with `reason="keyring"`, so a rebuilt daniel-box whose
      package lacks the writer publishes nothing. A break-glass restore of the signing key
      (`docs/break-glass.md`, H3.4b) ends with a run of that writer, so the file names the
      restored key.
- **The Pi's textfile directory.** H3.6's `optimize_pi` task is its only creator on the Pi,
  at owner root, group `sys_user`, mode 2775. The updater exits non-zero without it and
  never creates it (updater step 8).
    - The H7 PR that retires that task gives the directory a new creator with the same
      owner, group and mode. The creator belongs in the package that takes over the Pi's
      node exporter unit.
    - Without a creator, a fresh Pi installs its package and every updater run fails.
      `homelab_host_applied_info{host="daniel-pi"}` then never appears, so C6's host wait
      never sees the Pi settle.
- **A fresh-Pi check.** H7's rebuild proof for the Pi reads
  `homelab_host_applied_info{host="daniel-pi"}` and `homelab_host_reconcile_ok{host="daniel-pi"}`
  at 1, as H3.6's operator step 6 does. A rebuild that only installs the packages proves
  neither the directory nor the exporter's textfile flag.

### Data flow

```text
master tip ─▶ homelab-builder (daniel-box, every 2 min, user homelab-builder)
               token? ─▶ fetch ─▶ hold? ─▶ newest green commit (the anchor) ─▶ render ─▶ keyring check ─▶ nfpm ─▶ canary gate ─▶ staged/
               └─▶ every run past the fetch: homelab_builder.prom ─▶ Prometheus ─▶ monitor-bridge
                                                                                   │ OnSuccess
              homelab-repo-sign (user homelab-signer) ─▶ refusal rule ─▶ apt-ftparchive ─▶ sign ─▶ repo/
                                                                                   │
daniel-pi: homelab-updater.timer ─▶ apt-get install homelab-updater homelab-daniel-pi ┘
             └─▶ homelab-reconcile (waits for the dpkg lock, reads, releases) ─▶ units, legacy entries
                   └─▶ applied.json: per-entry state + record {versions, ok}
             └─▶ ok:   /var/lib/node-exporter-textfile/homelab_host.prom ─▶ Prometheus ─▶ land.sh
             └─▶ fail: /var/lib/homelab-updater/hold, homelab_host_reconcile_ok 0 ─▶ HostReconcileFailed
```

### File tree this slice adds

```diff
+ hosts/bootstrap-host.sh
+ hosts/keys/homelab-archive-keyring.gpg              (H3.4c, the operator's export of the signing key)
+ hosts/lib/homelab-updater
+ hosts/lib/homelab-reconcile
+ hosts/lib/tests/
+ hosts/lib/tests/container/Dockerfile
+ hosts/lib/tests/container/fixture/                (H3.3b: the cases' homelab-daniel-pi until H3.6)
+ hosts/packages/homelab-updater/package.yml
+ hosts/packages/homelab-updater/units/
+ hosts/packages/homelab-tools/package.yml            (H3.11)
+ hosts/components/reconcile/component.yml
+ hosts/components/pi-heartbeats/component.yml        (defaults moved from optimize_pi)
+ hosts/components/pi-heartbeats/templates/
+ hosts/components/pi-heartbeats/files/pi-health.logrotate
+ hosts/daniel-pi/package.yml
+ ansible/roles/setup/homelab_builder/                 (incl. files/publish-textfile and the signer's refusal rule)
+ scripts/deploy_tools/land_lib/host_wait.py
  ansible/initial_setup.yml                            (+ homelab_builder entry; parks the deployer)
  .github/workflows/ci.yml                             (+ host_packages job, read by the prek gate)
  ansible/roles/setup/optimize_pi/tasks/main.yml       (eleven heartbeat tasks removed; + the textfile directory task)
  ansible/roles/setup/optimize_pi/defaults/main.yml    (five heartbeat defaults moved out)
- ansible/roles/setup/optimize_pi/templates/pi-sd-health.sh.j2        (moved to hosts/components/pi-heartbeats/templates/)
- ansible/roles/setup/optimize_pi/templates/pi-recovery-health.sh.j2  (moved)
- ansible/roles/setup/optimize_pi/templates/pi-gz-integrity.sh.j2     (moved)
  ansible/roles/setup/optimize_pi/templates/node_exporter.service.j2  (textfile collector on)
  scripts/lib/render_context.py                        (component defaults layer)
  scripts/renderer/homelab_render.py                   (R1's path; + HOST_INPUTS)
+ scripts/renderer/renderer_lib/host_manifest.py       (units, legacy and manifest.json, H3.2d)
  scripts/validate/shell_templates.py                  (+ hosts/components/*/templates/)
  scripts/validate/unit_templates.py                   (+ every unit and drop-in the host renderer emits)
+ scripts/validate/host_packages.py                    (H3.3a: render, diff and both lints the job runs)
+ scripts/validate/tests/test_validate_host_packages.py
+ scripts/validate/host_package_cases.py              (H3.3b: the container cases the job runs)
+ scripts/validate/validate_lib/host_fixtures.py      (H3.3b: the fixture builds and the local repository)
+ scripts/validate/validate_lib/host_container.py     (H3.3b: the systemd container the cases drive)
+ scripts/validate/tests/test_validate_host_package_cases.py
+ scripts/validate/tests/fixtures/host_packages/red/   (the lints' red fixture, built in CI too)
  prek.toml                                            (unit and shell selectors gain hosts/)
  scripts/deploy_tools/land_lib/pipeline.py            (host candidate in resolve, render after CI, host wait step)
  scripts/deploy_tools/land_lib/classify.py            (shortcut reads the host candidate)
  scripts/deploy_tools/land_lib/deploy.py              (no_tag_outcome yields to host targets)
  scripts/deploy_tools/land_lib/health_verdict.py      (verdict wording names hosts)
  ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py  (+ homelab_builder)
  ansible/roles/k8s/monitor-bridge/files/check_table.py                     (+ host_packages row, builder arms)
  the observability role's rules directory, which M1 creates   (+ HostReconcileFailed, HostUpdateStale, H3.5d)
  ansible/roles/k8s/monitor-bridge/files/checks/gitops.py                   (+ the row's check body)
  ansible/roles/k8s/monitor-bridge/files/gates.py                           (host_packages in EXPORTER_DEPENDENT)
  ansible/tests/repo/test_lan_facts_have_one_source.py (+ hosts, .sh, the bootstrap row)
  ansible/tests/setup/test_setup_cron_roles_have_a_contract.py  (optimize_pi named member retired, H3.8b)
+ docs/host-packages.md                                (the runbook, H3.4b and H3.5b)
  mkdocs.yml                                           (+ the runbook's nav entry, H3.4b)
  docs/facts.lock                                      (optimize_pi sections forgotten or re-verified, H3.8b)
  docs/pi-host-tuning-record.md                        (heartbeats and disarm on the package path)
  .gitignore                                           (+ !/hosts)
```

## Steps, as PRs

Each PR is about 100 to 200 lines and reverts on its own, except where its *Rollback* entry
says otherwise.

1. **H3.1 Renderer host mode.**
    - Add `homelab-render host --host NAME --out DIR` to R1's renderer
      (`scripts/renderer/homelab_render.py`, as R1 places it), plus a shared-package
      mode for `hosts/packages/` (see *Open questions* 3).
    - The host context comes from the inventory layers that
      `scripts/lib/render_context.py:render_context` builds for that host. Any `ansible_facts`
      read becomes an inventory variable.
    - Add the component defaults layer to `render_context` as a keyword, beneath
      `group_vars/all.yml` (*Component variables*).
    - Host mode renders no secret and never rewrites `${` (*Host mode renders no secret*).
    - Add `!/hosts` to `.gitignore`, and add `hosts/lib/tests` to `testpaths` in
      `pyproject.toml`.
    - Two-writer check: none, because nothing ships yet.
2. **H3.2 Shared scripts and the censuses.** This step is four PRs, in order. "H3.2"
   names them together, and other slices' "H3.2 census" is H3.2c's.
    - **H3.2a, the updater.** Add `hosts/lib/homelab-updater`,
      `hosts/packages/homelab-updater/` (its units and `homelab.sources`),
      the maintainer-script templates and the maintainer-script lint. The keyring entry
      joins the package in H3.4c, with the key itself, so no render or nfpm build before
      then names a missing file. Its tests in
      `hosts/lib/tests/` drive the updater against a stub `dpkg-query`, `apt-get`, `systemctl`
      and record (*Verification*, Updater), and hold its `TimeoutStartSec` above reconcile's.
      The updater carries both `--clear-hold` levers (*Host-plane holds and stop levers*).
    - **H3.2b, reconcile.** Add `hosts/lib/homelab-reconcile` and the `reconcile` component.
      Its tests drive reconcile against a stub `systemctl` and a stub dpkg lock, and parse it
      with the 3.12 grammar (*Verification*, Reconcile and Python 3.12 syntax).
    - **H3.2c, the censuses and the unit validator.**
        - Add the `homelab-*` naming census. Its named members at H3.2c are
          `homelab-updater.service`, `homelab-updater.timer` and `homelab-reconcile.service`.
          PR H3.8b adds the three Pi timers.
        - Extend `scripts/validate/unit_templates.py` to render every host and shared package
          with the host renderer and run `systemd_verify` on every unit it emits, each with
          its drop-ins laid out beside it, the Pi's updater drop-in over the shared timer
          included. A `.j2` glob would miss a static unit file. The validator asserts
          `homelab-updater.timer` as a named member, so an empty render fails. The prek
          `validate-unit-templates` selector gains `^hosts/`.
        - Add the two-writer census, a test in `ansible/tests/setup/`, because it needs a
          render context and a join that a `Census` row cannot hold.
            - It walks every setup role's tasks through
              `ansible/tests/_role_census.py:task_files_by_role` and `_helpers.walk_tasks`.
            - From each task it extracts `dest:`, `path:`, a cron's `name:` with its `user:`,
              and `cron_file:`.
            - It renders every host package plus the shared packages, and fails when a
              rendered path or legacy entry equals what a task in a role applying to that host
              still writes.
            - Its named members at H3.2c are the three `optimize_pi` cron names on the Ansible
              side and `homelab-updater` on the package side. PR H3.6 adds
              `homelab-daniel-pi`.
            - At H3.2c the package side holds installed paths only. H3.2d adds legacy
              entries.
    - **H3.2d, the manifest emitter.** It lands before H3.6, whose first Pi install
      runs reconcile, and before H3.8b, whose legacy entries need it.
        - The renderer gains two spec keys, in `package.yml` and `component.yml`:
          `units:` maps a unit to its `manual` or `windowed` policy, and `legacy:` lists
          the four entry kinds of *The host handoff rule*. Each legacy value renders with
          the host's context, so H3.8b's `user: "{{ sys_user }}"` reaches the manifest as a
          user name.
        - `render_host` writes `/usr/lib/homelab/manifest.json` into every host package,
          in the schema `hosts/lib/homelab-reconcile` documents. `units` lists the units
          the host package and `homelab-updater` install, and each unit the host package
          drops in on. `enable_links` lists each unit the package installs under a
          `<target>.wants/` directory. `reconcilers` stays empty until H4. A shared package
          ships no manifest and refuses a legacy entry.
        - A fixture host package's manifest is parsed by reconcile's own `parse_manifest`.
        - The two-writer census adds each legacy entry to the package side, keyed as the
          write it retires: a `path` and a `cron_file` as paths, a `unit` as its file under
          `/etc/systemd/system/`, and a `cron` as its `(user, name)`. A fixture whose legacy
          cron a role still installs fails it.
3. **H3.3 CI job.** This step is two PRs, in order.
    - **H3.3a, the job skeleton.** Add the `host_packages` job to `ci.yml` with *CI* steps 1
      to 3 and 6: the render diff, the nfpm build, the two lints and the job summary. Add the
      `needs` entry and result check in the `prek` gate, the nfpm pin, and the pin's Renovate
      manager.
    - **H3.3b, the container cases.** Add `hosts/lib/tests/container/Dockerfile` and *CI*
      steps 4 and 5, as a matrix over `ubuntu:24.04` and `ubuntu:26.04`. The cases include the Depends case: fixture version N has no `Depends`,
      fixture N+1 adds `Depends: tree`, and the updater must leave `tree` installed.
4. **H3.4a Place the `homelab_builder` role. This PR parks the GitOps deployer.**
    - Add the `initial_setup.yml` entry `{ role: homelab_builder, tags: ["homelab_builder"],
      when: has_gitops }` after `gitops_deploy`. `has_gitops` is already resolvable by
      `scripts/deploy_tools/setup_lib/setup_gates.py`, so no inventory change is needed.
    - Add the role skeleton:
        - `defaults/main.yml` with `homelab_builder_enabled: false`;
        - `tasks/main.yml` dispatching to `install.yml` or `teardown.yml` on that flag, both
          empty of effects in this PR;
        - the role `CLAUDE.md` and its generated glance block.
    - The PR is about 60 lines. It parks the deployer, because `initial_setup.yml` is in
      `_BROAD_MANUAL_PREFIXES`. Until the clear below, every later merge waits behind it, and
      every other session's landing stops.
    - **Operator-only**, in an attended window right after the merge, in the primary
      checkout:
        1. Run `git log --oneline HEAD..origin/master` and
           `git diff --name-only HEAD..origin/master`. If the range holds only H3.4a, go on.
           If it holds other merges, the hand ff-merge would strand them, because the tick
           returns `noop` once local equals origin. Either wait for a window in which H3.4a
           is the only commit in the range, or list each other change with the apply it owes,
           such as a service deploy through `deploy.sh` or a setup role's
           `initial_setup.yml --tags <role>`, and run each one after step 3.
        2. `git merge --ff-only origin/master`.
        3. `uv run ansible-playbook ansible/initial_setup.yml --tags homelab_builder`. The
           play's `load_secrets` pre_task decrypts. The apply runs the empty teardown arm, so
           it changes nothing on daniel-box.
    - After the clear, `deploy_setup_roles.py:tick_applies_setup_role` accepts
      `homelab_builder`, because its gate admits daniel-box. Every later PR to the role lands
      through the setup-plane tick.
    - Reverting this entry, or deleting it in H7, parks the deployer the same way.
5. **H3.4b and H3.4c: the repository half, then its key.** Two PRs, in order.
    - **H3.4b, the repository half.** Merge it only after H3.4a's park is cleared, in an
      attended window (*Risks*, a failed tick apply).
        - Set `homelab_builder_enabled: true`. The tick applies the role on daniel-box.
        - Add the system users `homelab-builder` and `homelab-signer`, their directories,
          `homelab-apt-repo.service`, the ufw rule, and the signing key in the signer's
          GnuPG home, generated by `gpg --quick-generate-key` behind a `creates:` guard.
        - Add the teardown arm. It stops and removes the role's units, the ufw rule and
          `/var/lib/homelab-builder/`. It never touches `/var/lib/homelab-signer/`, so the
          key every host trusts survives a disarm.
        - Create `docs/host-packages.md` with its first procedure: clearing a deployer hold
          that a failed `homelab_builder` or `gitops_deploy` apply wrote (*Rollback*, H3.4b
          to H3.5b). Add its `mkdocs.yml` nav entry beside `The GitOps pipeline:
          gitops-pipeline.md`.
          `scripts/tests/test_mkdocs_config.py:test_every_doc_is_either_in_the_nav_or_explicitly_excluded`
          fails on a page that is in neither the nav nor `exclude_docs`, and no
          `exclude_docs` pattern matches this one.
        - **Operator-only:**
            - copy the private key into the break-glass kit;
            - create `homelab_builder_github_token` with no repository permissions, and add
              it with the `add-secret` skill.
    - **H3.4c, commit the archive key.** Merge it after H3.4b's apply has generated the key
      and before H3.5a, so the builder never renders `homelab-updater` without it.
        - **Operator-only**, because the signer's GnuPG home is 0700: export the public key
          with `sudo -u homelab-signer gpg --homedir /var/lib/homelab-signer/gnupg --export`
          into `hosts/keys/homelab-archive-keyring.gpg` in a worktree, and commit it. Then
          stop the gpg-agent the export starts:
          `sudo -u homelab-signer gpgconf --homedir /var/lib/homelab-signer/gnupg --kill gpg-agent`.
          On 2026-10-15 the agent outlived the export, and the worktree tools then read
          every worktree as in use.
        - Add the keyring to `hosts/packages/homelab-updater/package.yml`, installed at
          `/usr/share/keyrings/homelab-archive-keyring.gpg`.
        - The shared-package render test names that path as a member of `homelab-updater`'s
          render, so a dropped entry fails CI.
        - Nothing is published yet, so no host changes. The fingerprint pin in
          `bootstrap-host.sh` is H3.6's, because H3.6 creates the script.
6. **H3.5a Builder half, staging only.** Merge it after H3.4c, in an attended window. It
   also waits on three gates:
    - the operator's ADR for *Open questions* 1 is merged: [ADR-0021](../../adr/0021-the-host-package-builder-trusts-master-plus-green-ci.md), accepted
      2026-10-14;
    - the C5 *Version* row (*Open questions* 7) is amended: contracts.md's *Applied requests
      (2026-10-15)* records it;
    - **operator-only:** the signing-key reach check in *Verification* has run for `claude`
      and for `homelab-builder`, and its output is in the PR body. A check that finds any
      route for either account stops the PR until that route is closed. `ubuntu` is out of
      scope by the operator's ruling of 2026-10-15 (*Risks*, the `ubuntu` bullet).
    - Add `homelab-builder.service` and `homelab-builder.timer`, the builder script, the
      pinned nfpm and `apt-utils`, and `/etc/homelab-builder/config.env` carrying the token,
      `CI_CONTEXTS` and the walk bound (builder step 4).
    - Add the row `ansible/roles/setup/gitops_deploy/defaults/main.yml` naming
      `homelab_builder` to `deploy_cross_role.py:SETUP_FILES_SHIPPED_BY_OTHER_ROLES`. The
      defaults-read census in `ansible/tests/setup/test_setup_cross_role_files.py` requires
      it once the role's template reads `gitops_deploy_ci_contexts`.
    - Give `homelab-builder.service` its sandboxing directives (*The builder and the
      repository*, Sandboxing), with the test that holds them.
    - Give `homelab-builder` uv, the pinned `host_python_version` and a frozen environment.
      Follow the uv and Python steps of `ansible/roles/setup/common/tasks/agent_user.yml`, not
      the whole file, which also installs Claude Code.
    - Import `alert_unit.yml` for the builder's `OnFailure=`, and
      `ansible/roles/setup/common/tasks/install_host_lib.yml` so the builder script can
      import `host_lib`.
    - Add the builder's failure exits and `homelab_builder.prom` (builder steps 4, 5 and 8),
      and the role's `publish-textfile` script with the builder's `ExecStopPost=-+` line.
    - Add the task that writes `/etc/homelab-builder/archive-key.fpr` from the signer's
      key, and the builder's keyring check (builder step 6).
    - Add `homelab_builder` to `_ALERT_UNIT` and `_HOST_LIB` in
      `deploy_cross_role.py:SETUP_FILES_SHIPPED_BY_OTHER_ROLES`, or
      `test_the_deployers_table_equals_the_trees_cross_role_files` fails. `_STAMPED` is
      `_HOST_LIB` plus three roles, so the role also imports `stamp_deployed.yml` and
      `stamp_render.yml`: a `stamp_deployed_pairs` entry for the builder script and a render
      group for its units, as every other `_HOST_LIB` role does.
    - That table is a `gitops_deploy` file, so this PR's tick applies `gitops_deploy` as well
      as `homelab_builder`.
    - Add the `## Autonomous-role contract` section to the role `CLAUDE.md`, because the
      timer acts unattended.
    - The builder stages packages but publishes nothing until H3.5b.
7. **H3.5b to H3.5d: publish, and page on the host plane.** Three PRs. H3.5c follows
   H3.5b. H3.5d waits on M1 instead (contracts.md index), and H3.9b waits on H3.5d.
    - **H3.5b, signer and publish.** Merge it in an attended window.
        - Add `homelab-repo-sign.service`, its script, the `OnSuccess=` link,
          `homelab_repo.prom` with the signer's `ExecStopPost=-+` line, and the
          `/etc/homelab-builder/hold` lever.
        - Give the signer `OnFailure=homelab-repo-sign-alert.service` through a second
          import of `alert_unit.yml`.
        - Add the signer's refusal rule, `Valid-Until`, the daily re-sign arm, and the
          signer unit's sandboxing directives, `PrivateNetwork=yes` included.
        - Extend the autonomous-role contract with the signer and the hold.
        - Complete the runbook `docs/host-packages.md` (*Tests and docs*, H3.4b to H3.5c).
    - **H3.5c, page on the builder.** It carries the two builder arms. The host arms are
      H3.5d's rules. It takes one of two forms. The form depends on
      whether M6's A1 shadow PR is on master when H3.5c merges (*Open questions* 8). A
      row-form H3.5c that meets A1's shadow PR on master at its rebase switches to the
      rule form before it merges.
        - **Row form, while A1's shadow PR has not merged.** M6's A1 batch then moves the
          row with the other host rows. M6's planned bijection test is the backstop for a
          row that A1 misses. It fails while a row of `CHECKS` has no rule, so A6-7a cannot
          delete the push path under the row.
            - Add a monitor-bridge row, `host_packages`, to `check_table.py:CHECKS` with
              `gate="prometheus"`, as the `pi_pressure` row reads the Pi's node exporter. It is a
              new row rather than an arm folded into `gitops_status`, because a row carries one
              `gate` value, and `gitops_status` reads the deployer's state directory outside the
              Prometheus gate. A Prometheus-sourced arm there would page on every Prometheus
              outage. Its check body joins `checks/gitops.py`, and its Kuma tile is "Host
              Packages — Status"
              in "Automation & Drift". It pushes `down` in the two builder cases
              *Host-plane holds and stop levers* lists. Each message names the series'
              `reason` and `sha` labels, so a hold, a refusal, a rewritten master, an open
              rollout gate and a walk with no green commit read differently.
            - Add `host_packages` to `gates.py:EXPORTER_DEPENDENT["node"]`, because the builder
              arms read daniel-box's node exporter through the `node` job. A dead exporter then
              pages once, through its own row, rather than also as an absent builder series.
              The bridge defaults `monitor_bridge_builder_max_lag_s` (7200) and
              `monitor_bridge_builder_max_fetch_age_s` (1800) render into
              `ansible/roles/k8s/monitor-bridge/templates/env-secret.yaml.j2`, as every bridge
              threshold does.
            - **Operator-only:** create `monitor_bridge_host_packages_push_token` with the
              `add-secret` skill.
        - **Rule form, once A1's shadow PR has merged.** No row, no Kuma tile and no push
          token. Two alert rules carry the two builder arms, because each arm is already a
          PromQL condition. They join the observability role's rules directory, which M1
          creates.
            - Each rule carries C7's labels. `severity` is `critical`, because the row
              pages, and `service` is `daniel-box`.
            - A rule that reads a series as absent fires only while daniel-box's `node`
              exporter `up` reads 1. That does for each rule what the `EXPORTER_DEPENDENT`
              entry does for the row, without waiting for A1's inhibit rule.
            - The annotations carry the row's messages: the builder series' `reason` and
              `sha` labels. The two thresholds become defaults of the observability role.
            - Each rule's red and green pair is a `promtool test rules` case beside M2's.
    - **H3.5d, the host rules.** About 120 lines. It waits on M1, which creates the rules
      directory, and on none of H3.5a to H3.5c.
        - Add `HostReconcileFailed` and `HostUpdateStale` to the observability role's rules
          directory, with C7's labels (*Host-plane holds and stop levers*).
        - `HostReconcileFailed` is `homelab_host_reconcile_ok == 0` held for 15 minutes.
          Its annotation names the host and both `--clear-hold` commands.
        - `HostUpdateStale` renders one clause per entry of the observability role's
          expected-host map, which is empty in this PR. A clause fires when the host's
          success timestamp is older than its limit, or absent while that host's exporter
          `up` reads 1.
        - A test beside the renderer's host-mode tests renders each listed host's updater
          timer with its drop-in. It fails when a limit is shorter than the longest gap
          between two fires plus the updater's `TimeoutStartSec`, so a schedule change
          that outgrows the limit fails CI.
        - Each rule's red and green pair is a `promtool test rules` case beside M1's own.
        - M1's convention 5 bars a rule that is not a shadow from paging `up == 0` for a
          scrape job without an `origin` label before M6's batch A3. It also requires
          every `absent()` arm over such a job to be conjoined with `up == 1`. The `node`
          and `node-pi` jobs in `ansible/roles/k8s/observability/templates/prometheus.yaml.j2`
          both carry `origin`, so the convention does not bind these rules. They meet it
          anyway: neither rule pages on `up`, and every absent arm is conjoined with
          `up == 1`. The builder rules of H3.5c's rule form follow the same shape.
8. **H3.6 The Pi package, empty, plus bring-up.**
    - Add `hosts/daniel-pi/package.yml` carrying only the `reconcile` component and the Pi's
      updater-schedule drop-in, with no enable link for `homelab-updater.timer`.
    - Run the container case that installs the Pi package by hand against this PR's real
      render, not H3.3b's fixture `homelab-daniel-pi`. The other cases keep the fixture,
      because they need a unit, a file and a payload that the real package does not ship.
    - Add `hosts/bootstrap-host.sh`. It takes `bootstrap-host.sh <hostname>` and runs as
      root. It carries H7's step numbers, so H7 extends it in place:
        - Step 1 refuses if `hostname` differs from `<hostname>`.
        - Step 2 installs the homelab keyring, verified against the fingerprint pinned in
          the script, and writes `homelab.sources`. This PR pins that fingerprint, read
          from H3.4c's `hosts/keys/homelab-archive-keyring.gpg`. A test beside the
          LAN-facts URL test holds the pin equal to that file's fingerprint, and a fixture
          pin that differs fails it.
        - Step 5 runs `apt-get update --error-on=any` restricted to the homelab source,
          then `apt-get install homelab-updater homelab-<hostname>`.
      Steps 3, 4 and 6 are H7's. H7 also adds `homelab-repos` to step 2, because H7.1
      creates it, and `chezmoi` from `homelab-tools` to step 5. H3.11 builds
      `homelab-tools`.
    - The script types the repository URL (*Shared names*, Repository). Add `hosts` to
      `test_lan_facts_have_one_source.py:SCAN_ROOTS` and `.sh` to its `SCAN_SUFFIXES`, with
      an `ALLOWLIST` row for `hosts/bootstrap-host.sh` whose reason names the test below. Add
      a test to that file that the script's URL equals `http://<server_ip>:8791` for
      daniel-box's `server_ip`. No `.sh` file typed a guarded address at `70591c307`, so the
      wider scan starts green.
    - Flip the Pi's node exporter to `--collector.textfile.directory=/var/lib/node-exporter-textfile`.
      In the same `node-exporter-host` tag, add a task that creates that directory with M3's
      owner contract: owner root, group `{{ sys_user }}`, mode 2775. Nothing else creates
      it on the Pi, and the updater fails without it (step 8). No package ships the
      directory, so the two-writer census finds one writer. This changes `optimize_pi`, so
      the tick records a `manual_plane` line for it.
    - The two-writer census gains `homelab-daniel-pi` as a named member.
    - **Operator-only**, in order, from a LAN session:
        1. `uv run ansible-playbook ansible/initial_setup.yml --tags node-exporter-host -e
           target=daniel-pi`, which decrypts.
        2. `uv run python scripts/deploy_tools/gitops_state.py clear-owed manual_plane
           optimize_pi`. The tick records the line for the whole role, with no narrowed
           tags, so `clear-owed` refuses `--applied node-exporter-host`. Step 1's narrowed
           run therefore does not discharge it: apply the whole role with `--tags
           optimize_pi -e target=daniel-pi`, as the operator did on 2026-10-16, then clear.
        3. Copy `hosts/bootstrap-host.sh` and `hosts/keys/homelab-archive-keyring.gpg` to the
           Pi, then run `sudo ./bootstrap-host.sh daniel-pi` there.
        4. `sudo systemctl start homelab-updater.service` on the Pi.
        5. Confirm on the Pi that `systemctl is-active homelab-updater.timer` prints
           `inactive`, and that `/var/lib/node-exporter-textfile/homelab_host.prom` exists
           with mode 0644.
        6. An agent then runs the Prometheus query in *Verification*. It must return
           `homelab_host_reconcile_ok{host="daniel-pi"}` and
           `node_textfile_scrape_error{job="node-pi"}` at 0, which shows the Pi's node
           exporter reads the directory and exposes the series.
9. **H3.7 The Depends gate.**
    - Add `Depends: tree` to `hosts/daniel-pi/package.yml`. Pick any package that
      `apt-cache policy` on the Pi shows as not installed.
    - The container case that installs the real Pi package by hand runs `apt-get install`
      on the debs, as `bootstrap-host.sh` does. With `dpkg -i`, as H3.6 wrote it, a new
      `Depends` leaves the package unconfigured and the case fails.
    - After merge, the operator starts the updater on the Pi and confirms that
      `dpkg-query -W tree` succeeds.
    - A follow-up commit drops the `Depends`. apt marks `tree` automatic, so the weekly
      `Weekly apt autoremove` cron removes it.
    - If the gate fails, stop the slice and follow *Open questions* 2.
10. **H3.8 The three heartbeats.** This PR comes in two parts, in order.
    - **H3.8a, interim credentials.**
        - `optimize_pi` writes `/etc/homelab/interim/pi_sd_health_push_token` and
          `pi_recovery_push_token`, root 0600, from SOPS on the controller, as every
          Ansible-rendered secret is at `70591c307`. It stages no `domain` file: #5087
          deleted `domain` from `secrets.yml`, and H3.8b renders it from inventory.
        - **Operator-only:** apply it with `--tags sd-health,recovery-health -e
          target=daniel-pi`, then run `gitops_state.py clear-owed manual_plane optimize_pi
          --applied sd-health,recovery-health`. If the line names no narrowed tags, as
          H3.6's did, `clear-owed` refuses `--applied`: apply `--tags optimize_pi` and
          clear without it.
        - Two-writer check: these files are Ansible-owned, and the package only reads them.
    - **H3.8b, move.**
        - `git mv` the three templates into `hosts/components/pi-heartbeats/templates/`.
          Each token script reads its own push token from `$CREDENTIALS_DIRECTORY/<key>`,
          two keys in all, and its unit declares
          `LoadCredential=<key>:/etc/homelab/interim/<key>`. Both scripts render `domain`
          from inventory, because #5087 made it a plain inventory value. The Jinja comments
          about `domain` in `pi-sd-health.sh.j2` and `pi-recovery-health.sh.j2` are deleted
          in the same edit, not moved.
        - Move the four `optimize_pi_gz_integrity_*` defaults and
          `optimize_pi_recovery_restart_enabled` into the component's `defaults:` map.
        - Add six units: `homelab-pi-sd-health`, `homelab-pi-gz-integrity` and
          `homelab-pi-recovery`, each a service and a timer. The three services set
          `User={{ sys_user }}`. That user reaches `docker` through the supplementary group
          `docker_install` grants, as the cron did.
        - `LogsDirectory=pi-health` and `StateDirectory=pi-health` keep the paths at
          `70591c307`. The package ships `/etc/logrotate.d/pi-health` at the same path, as a
          plain file, so dpkg takes it over on unpack.
        - Ship `/usr/lib/homelab/kuma-push-lib.sh`, a package-owned copy of
          `ansible/roles/setup/initial_setup/files/kuma-push-lib.sh`. The source file stays
          single.
        - The gz-integrity timer adds `OnActiveSec=1min`, which primes the verdict the
          Ansible task used to prime.
        - The recovery timer's enable link ships only when
          `optimize_pi_recovery_restart_enabled` is true. Reconcile step 1 stops a timer whose
          enable link was dropped, so a disarm takes effect on the next install with no
          reboot. Step 3 leaves the stopped timer alone when its file changes later, because
          it is neither enabled nor active. The script stays installed, as the cron disarm
          left it.
        - The prek `validate-shell-templates` selector gains `^hosts/components/`, beside
          `shell_templates.py:discover_templates` gaining `hosts/components/*/templates/`.
        - Delete these eleven `optimize_pi` tasks, and keep every other task, including
          `Remove the superseded Pi-only Kuma-push helper`:
            - `Create the directory both health crons write their verdict to`;
            - `Cap the health log so it cannot grow into the log2ram tmpfs`;
            - `Deploy the SD-card health heartbeat script`;
            - `Schedule the SD-card health heartbeat`;
            - `Create the directory the rotated-log integrity verdict lives in`;
            - `Deploy the rotated-log integrity sweep`;
            - `Check whether a rotated-log integrity verdict exists yet`;
            - `Prime the rotated-log integrity verdict before the first daily sweep`;
            - `Schedule the rotated-log integrity sweep`;
            - `Deploy the container-recovery heartbeat script`;
            - `Schedule the container-recovery heartbeat`.
        - Legacy entries: the three cron names (user `sys_user`) and the three
          `/usr/local/bin/pi-*.sh` paths.
        - The naming census gains the three Pi timers as named members.
        - The tick records a `manual_plane` line for `optimize_pi`. It needs no apply, because
          the package replaces the deleted tasks. Once the Pi reports this SHA, the operator
          clears it with `--applied` naming the tags the row lists, if the row holds only this
          PR's. Otherwise the operator applies the rest first.
11. **H3.9 Land on facts, and arm the Pi timer.** This step is three PRs, in order. Other
    slices' "H3.9 host-wait module" is H3.9a's, and "H3.9 arms the timer" is H3.9b. H3.9c
    adds the Pi to `HostUpdateStale` once its timer runs.
    - **H3.9a, the host-wait module, unwired.** Add
      `scripts/deploy_tools/land_lib/host_wait.py`. Nothing calls it yet, so no landing
      changes.
        1. `HOST_INPUTS` is a constant in the renderer: every path prefix a host render reads.
           It holds `hosts/`, `ansible/inventory/`, `ansible/filter_plugins/`, `scripts/lib/`,
           `scripts/renderer/`, `pyproject.toml`, `uv.lock`, `ansible/vars/secrets.yml`,
           because host mode removes its key names, and
           `ansible/roles/setup/initial_setup/files/kuma-push-lib.sh`, which H3.8b copies. A
           test runs a host render under `sys.addaudithook` and asserts that every file it
           opens in the repo falls under a member. It also asserts that the render opened
           `hosts/daniel-pi/package.yml`, and that `HOST_INPUTS` names the last two members
           above.
        2. `host_targets` renders every host package at the merge commit and at its first
           parent, each in a worktree from `tools.py:gate_snapshot`, which points
           `UV_PROJECT_ENVIRONMENT` at the primary's `.venv`. It keeps the hosts whose trees
           differ. When a snapshot or a render fails, every host with a
           `hosts/<host>/package.yml` at the merge commit counts as a target, so a failure
           waits rather than skips.
        3. `wait_for_hosts` queries Prometheus on `127.0.0.1:9090`. When that is refused or
           times out, it queries `observability_prometheus_cluster_ip` on 9090, read from the
           role default the way `probe_lib/core.py:observability_loki_ip` reads Loki's. It
           never uses `prom_endpoint`, which decrypts. Both refused is "not settled", with the
           message "Prometheus unreachable on loopback and ClusterIP".
        4. A host settles when the SHA in `homelab_host_applied_info` equals or descends from
           the merge SHA and `homelab_host_reconcile_ok` is 1. An absent series is not
           settled. `homelab_host_reconcile_ok` at 0 ends the landing `deploy-failed`, naming
           the host and the operator's `--clear-hold` command.
        5. It queries once before anything else, so a host that already settled never waits.
           Then the deadline comes from the host's own schedule, read from the rendered
           drop-in: the second updater fire after the merge, plus 10 minutes. The first fire
           can precede the builder's publish. In two cases the step does not wait: when that
           deadline is more than 45 minutes away, and when the host's package rendered at the
           first parent ships no enable link for `homelab-updater.timer`. Before this merge
           is published, a host holds at best the first parent's package. A link that
           arrives in this merge therefore arms nothing until an updater run installs it, and
           the merge commit's render cannot tell the step that. It ends `deferred`, naming the
           next fire, or that no timer is armed, and the operator's command to start the
           updater. Past the deadline, it ends `deferred` too, with the host's last success
           timestamp.
    - **H3.9b, wire it and arm the Pi.** Merge it only after H3.5d is on master, so a hold
      on the armed Pi pages through `HostReconcileFailed`.
        1. `pipeline.py:_step_resolve` sets `ln.host_candidate` when a changed path falls
           under a member of `HOST_INPUTS`. It runs no renderer. Resolve reads the merge
           commit before master CI, and `deploy_cross_role.py:tables_in` records, in
           `DECIDED parse the copy rather than exec it`, why nothing at that point may run
           the merge commit's code.
        2. `shortcut_if_nothing` treats `ln.host_candidate` like a resolved tag, so a
           hosts-only PR never finishes `nothing-to-deploy` before CI.
        3. `_step_ci` calls `host_wait.host_targets` after `ci.wait_master_ci` returns green,
           and only when `ln.host_candidate` is set. The merge commit has then passed master
           CI, and its first parent is an earlier master commit. When no host differs, it
           clears the candidate and calls `shortcut_if_nothing` again, so a PR whose paths
           only looked like host inputs still ends `nothing-to-deploy`.
        4. `deploy.py:deploy_phase` calls `deploy.py:no_tag_outcome` when the PR has no tag
           and either no host target or a non-empty plane. A PR with a plane therefore still
           ends `needs-manual-apply` with "reaches no service tag, but is not done", and
           `no_tag_outcome` prints a line naming each host target it did not wait on. A PR
           with host targets, no tag and no plane returns from the phase without deploying.
           It awaited the tick in `_step_tick`, because it has no tag, and that tick
           fast-forwards its empty change set.
        5. A step in `pipeline.py:_STEPS`, "waiting for hosts to apply", runs
           `host_wait.wait_for_hosts` after `deploy.deploy_phase` and before
           `health_verdict.health`, which is `NoReturn`.
        6. `health_verdict.health` builds its verdict wording from what the landing applied:
           "services deployed", "hosts applied", or both. A hosts-only landing whose tick
           applied a setup role of its own therefore never reads "services deployed".
        7. The reads/writes table in `pipeline.py`'s module docstring gains the step,
           `host_candidate` and `host_targets`.
        8. Ship the enable link for `homelab-updater.timer` in the Pi package.
        9. **Operator-only**, after the merge. The first parent's Pi package ships no link,
           so the landing ends `deferred` (H3.9a item 5). Wait until the agent's Prometheus
           query shows `homelab_repo_published_info{package="homelab-daniel-pi"}` at the
           merge SHA. Then run `sudo systemctl start homelab-updater.service` on the Pi.
           Confirm that `systemctl is-active homelab-updater.timer` prints `active`, then
           re-run the same `land.sh` command. Its first query finds the Pi settled.
    - **H3.9c, the Pi joins `HostUpdateStale`.** Merge it only after H3.9b's operator step
      reads `active`. Before then, the Pi's last success timestamp is the last hand-started
      run's, which can be days old, so the rule would fire as soon as the observability
      role redeployed.
        1. Add daniel-pi, with its limit, to the observability role's expected-host map
           (H3.5d). The limit test then covers the Pi's rendered schedule. The rule file is
           plain YAML, so also add the `HostUpdateStale` rule for daniel-pi to
           `ansible/roles/k8s/observability/files/rules/host-checks.yaml`.
           `ansible/tests/services/test_host_plane_rule_literals.py` fails until the rule
           is there, and its message prints the rule.
        2. The Pi's absent clause reads `up{job="node-pi"}`. A failed scrape marks the
           Pi's series stale, so the success timestamp reads as absent. The `node-pi`
           job's comment in `ansible/roles/k8s/observability/templates/prometheus.yaml.j2`
           records 88 minutes of `up == 0` in 7 days. The clause keeps a failed scrape
           from reading as a stale host.
    - The `outcome.py:Verdict.SETTLED` path runs only after every target host settles.
12. **H3.10 The drop proof.** This PR comes in two parts, in order. Both are needed for exit
    observation 2, and the first must leave the timer running, or the drop proves nothing.
    - **H3.10a, arm the canary.**
        - Add `homelab-pi-canary.service`, which runs `true`, and
          `homelab-pi-canary.timer`, which fires it hourly.
        - Ship the timer's enable link under `timers.target.wants/`. Reconcile step 2 then
          starts the timer on the install, because it is enabled and not active.
        - The naming census gains `homelab-pi-canary.service` and `homelab-pi-canary.timer`
          as named members.
        - **Operator check before H3.10b merges:** the H3.10a landing ends `settled` for
          daniel-pi, so Prometheus shows the Pi's applied SHA at or past the H3.10a merge
          with `homelab_host_reconcile_ok` at 1. On the Pi, `systemctl is-active
          homelab-pi-canary.timer` prints `active`.
    - **H3.10b, drop it.** Merge it only after that check passes.
        - Remove both units and the enable link from the Pi package.
        - Remove the canary pair from the naming census's named members in the same PR, or
          the census asserts members that no longer exist.
        - Reconcile step 1 stops the timer, and dpkg deletes its files on the upgrade.
13. **H3.11 `homelab-tools`, with the Pi as its `arm64` pilot.** About 200 lines. Merge it
    after H3.9b, so the Pi installs it through its armed updater. This PR produces both
    builds: H4.2 waits on its `amd64` build, and H4.5 on its `arm64` build.
    - Add `hosts/packages/homelab-tools/package.yml` with `sops` and `chezmoi`, and the
      pins' Renovate manager (*`homelab-tools`*).
    - The builder downloads and verifies each binary, then runs nfpm once per
      architecture.
    - Updater steps 4 and 5 cover every installed `homelab-*` package.
    - Add `Depends: homelab-tools` to the Pi package. The two-writer census then checks the
      tools' installed paths against every Ansible task that applies to the Pi.
    - Add the `arm64` CI job. The `prek` gate lists it in `needs`, and `KNOWN_JOBS` in
      `ansible/tests/repo/test_every_ci_job_is_gated.py` names it (*Open questions* 9).
    - **Operator check after the merge:** the Pi lists `homelab-tools:arm64` at the merge
      SHA or a descendant, and `dpkg --verify homelab-tools` prints nothing.
14. **H3.12 Staged rollout.** About 150 lines, in `homelab_builder`. It changes nothing
    while the Pi is the only host with a package. H4's first publish of
    `homelab-daniel-server` waits on it.
    - Add the gate, its state, the canary order and the `reason="canary"` arm of the
      unbuilt-since series (*Staged rollout*).
    - Render the Prometheus loopback address and `observability_prometheus_cluster_ip`
      into `config.env`. The builder reads both from there, never from its clone. The PR
      adds whatever row the defaults-read census in
      `ansible/tests/setup/test_setup_cross_role_files.py` requires for that read.
    - Add the root-owned `release` override and its runbook step.

## Verification

An agent runs the Prometheus block, through the same loopback-then-ClusterIP transport as the
land host wait. `probe.py metric` and `probe.py monitors` read `domain` from inventory since
#5087 and decrypt nothing, so an agent may run them too. The operator runs every `ssh daniel-pi` block and every command on the Pi, because
the `claude` user has no login there. Each ssh call is one batched, read-only command, which
keeps it under the ssh rate limiter.

```bash
# Agent, H3.6 onward: what the builder published for the Pi, what the Pi applied, and (H3.8b)
# both heartbeat tiles. The fallback address is observability_prometheus_cluster_ip.
Q='{__name__=~"homelab_repo_published_info|homelab_builder_.*|homelab_host_(applied_info|reconcile_ok|verify_mismatches)"} or node_textfile_scrape_error{job="node-pi"} or monitor_status{job="uptime-kuma",monitor_name=~"Daniel Pi (SD Health|Recovery)"}'
curl -s --max-time 5 http://127.0.0.1:9090/api/v1/query --data-urlencode "query=$Q" \
  || curl -s --max-time 5 http://10.43.39.218:9090/api/v1/query --data-urlencode "query=$Q"
```

```bash
# Operator, H3.6 onward.
ssh daniel-pi 'dpkg-query -W homelab-updater homelab-daniel-pi; dpkg --audit; ls /var/lib/homelab-updater/; cat /var/lib/node-exporter-textfile/homelab_host.prom'

# Operator, H3.7: the Depends gate.
ssh daniel-pi 'dpkg-query -W tree; apt-mark showauto | grep -x tree'

# Operator, H3.8b: timers from the package, crons gone, verdicts fresh, units run as ubuntu.
ssh daniel-pi 'systemctl list-timers "homelab-pi-*" --no-pager; systemctl show -p User homelab-pi-recovery.service; crontab -l | grep -c "^#Ansible: Pi "; ls -l /usr/local/bin/pi-*.sh 2>&1; tail -n 3 /var/log/pi-health/health.log'

# Operator, H3.10a: the canary runs before anything drops it.
ssh daniel-pi 'systemctl is-active homelab-pi-canary.timer; ls /usr/lib/systemd/system/timers.target.wants/ | grep -c canary'

# Operator, H3.10b: the dropped timer is gone.
ssh daniel-pi 'systemctl show homelab-pi-canary.timer -p LoadState -p ActiveState; ls /usr/lib/systemd/system/timers.target.wants/ | grep -c canary'

# Operator, H3.11: the arm64 pilot.
ssh daniel-pi 'dpkg-query -W homelab-tools:arm64; dpkg --verify homelab-tools'
```

```bash
# Operator, on daniel-box, before H3.5a merges: the signing-key reach check. Run it once
# for claude, the agent user, and once for homelab-builder. ubuntu is out of scope (Risks).
sudo -l -U <account>
id <account>
sudo -u <account> -- test -r /var/lib/homelab-signer/gnupg && echo READABLE
sudo -u <account> -- gpg --homedir /var/lib/homelab-signer/gnupg --list-secret-keys
```

The expected results are:

- `homelab_host_applied_info{host="daniel-pi"}` carries the same SHA as
  `homelab_repo_published_info{package="homelab-daniel-pi"}`, and
  `homelab_host_reconcile_ok{host="daniel-pi"}` is 1;
- `homelab_builder_unbuilt_since_timestamp_seconds` is 0 once the builder has caught up,
  and `node_textfile_scrape_error{job="node-pi"}` is 0;
- `dpkg --audit` prints nothing, and `/var/lib/homelab-updater/` holds no `hold` file;
- both `monitor_status` series for the Pi heartbeats read 1;
- the recovery service reads `User=ubuntu`;
- the crontab count is 0;
- at H3.10a, the canary timer reads `active` and its link count is 1;
- at H3.10b, the canary reads `LoadState=not-found` and `ActiveState=inactive`, and the link
  count is 0. Only the H3.10a reading makes this one a proof that reconcile stopped a
  running timer;
- at H3.11, `dpkg-query` prints one `arm64` line and `dpkg --verify` prints nothing;
- the reach check passes for an account when `sudo -l` lists no rule that runs a command
  as root or as `homelab-signer`, `id` names none of `homelab-signer`, `sudo`, `docker`,
  `lxd` or `disk`, the `test` line prints nothing, and `gpg` fails. For the two accounts it
  runs for, the check covers every route that skips master. A route through a merged change
  is the anchor itself (*Open questions* 1).

**The recovery disarm (H3.8b).** The container suite proves it, so production never loses
its recovery timer for a test. It installs the Pi package rendered with
`optimize_pi_recovery_restart_enabled` true, upgrades to one rendered with it false, and
asserts that `homelab-pi-recovery.timer` is inactive with no reboot.

**Killed pull (exit observation 3).** The CI container case runs first, where a kill costs
nothing and its kill point is fixed by the payload's `.dpkg-new` file (*CI*). On the Pi the
packages are small, so the operator kills on the dpkg process rather than on a clock. The
sequence runs from a LAN session, never over the WireGuard path, on a hand-started install of
a newly published version:

```bash
sudo systemctl start --no-block homelab-updater.service
# Wait for apt's `dpkg ... --unpack` call. The character class keeps the pattern from
# matching the waiting shell's own command line.
until pgrep -f 'dpkg .*-[-]unpack' >/dev/null; do sleep 0.05; done
sudo systemctl kill -s KILL homelab-updater.service
sudo dpkg --audit    # must list homelab-daniel-pi or homelab-updater
sudo systemctl start homelab-updater.service
sudo dpkg --audit    # prints nothing
```

If the first `dpkg --audit` prints nothing, the kill landed after the unpack, and the attempt
does not count. The operator repeats it on the next hand-started install. H3.7, its
follow-up and H3.8b each publish a new Pi version before H3.9b arms the timer.

**Red proofs for the checks this slice adds.** Each check is a `..._is_clean` and
`..._is_flagged` pair, and each census asserts named members.

- *Maintainer-script lint.* A fixture postinst carrying `rm -rf /etc/foo` is flagged, and the
  shipped template is clean.
- *`type: config` lint.* A fixture `nfpm.yaml` with `type: config|noreplace` is flagged.
- *Naming census.* A fixture component shipping `pi-sd-health.timer` is flagged. The census
  asserts the named members its PR adds, so an empty glob cannot pass.
- *Two-writer census.* A fixture where `optimize_pi` still writes `/usr/lib/homelab/pi-sd-health`
  is flagged. The census asserts the named members its PR adds.
- *Host-mode secret refusal.* A fixture template reading `{{ pi_sd_health_push_token }}` fails
  the host render, and the error names the key. One reading
  `$CREDENTIALS_DIRECTORY/pi_sd_health_push_token` renders. A fixture holding `${HOME}` renders
  byte for byte.
- *Component variables.* A host-mode render of `pi-heartbeats` for daniel-pi resolves the five
  moved defaults, `containers_list`, `has_code_server`, `k3s_metallb_ingress_vip` and
  `sys_user`. A `host_vars` value for `optimize_pi_recovery_restart_enabled` beats the
  component default.
- *Python 3.12 syntax.* A test in `hosts/lib/tests/` parses `homelab-reconcile` with
  `ast.parse(..., feature_version=(3, 12))`, and a fixture using newer syntax is flagged. That
  check covers syntax only. The container suite, which runs reconcile under 3.12, covers the
  standard library.
- *Reconcile.* Unit tests drive the script against a stub `systemctl` and a stub dpkg lock.
    - A unit dropped from the manifest is stopped.
    - A timer whose enable link was dropped is stopped, and a timer-activated service is not.
    - A changed service hash is try-restarted when the service is active, and a changed
      timer hash is restarted when the timer is enabled or active. H4.1b changes the service
      case to expect `reset-failed` and then `restart`, as H4's tests list states.
    - A timer with no enable link that is inactive, whose drop-in hash changed, stays
      inactive. A first run with no `applied.json` leaves it inactive too.
    - A recovery timer disarmed by a dropped enable link stays stopped when its unit file
      changes in a later run.
    - A masked timer whose hash changed is never touched, and the run succeeds.
    - A `manual` service is never restarted, and a masked unit is never started.
    - A legacy cron line is removed, and a path `dpkg -S` reports as owned is never removed.
    - Started while the lock is held, reconcile waits, then reads the manifest written before
      the lock was released. It releases the lock before its first `systemctl` call.
    - A package whose status is not `ii ` makes reconcile record a failed result for the
      versions it read, and act on nothing.
    - A restart that fails keeps that unit's old hash in `applied.json`, and the record says
      the run failed. The next run re-attempts that restart and does not repeat a restart
      that succeeded.
- *Updater.*
    - A stub reconcile that records a failure leaves `homelab_host_applied_info` and the
      success timestamp unadvanced, writes `homelab_host_reconcile_ok 0` and the hold file,
      and the next run installs nothing.
    - An updater killed after the install, before step 7, is followed by a run on the common
      path whose stub reconcile records a failure. That run writes `reconcile_ok 0` and the
      hold, never `reconcile_ok 1`.
    - On the common path, a record that names other versions makes the run start reconcile
      before it writes a metric. So does a failed record for the installed versions with no
      hold file, and a second failure then writes the hold.
    - `--clear-hold` after a failed restart re-attempts that restart, through the stub
      `systemctl`, rather than passing on an empty diff. With a passing reconcile it removes
      the hold.
    - With a held host whose stub reconcile fails on the installed version, `--clear-hold`
      keeps the hold and installs nothing. That is the red half. `--clear-hold
      --install-newest` over a fixture list with a newer version installs it, runs
      reconcile, and removes the hold when that run passes. When it fails, the hold is
      written again.
    - A package whose status is not `ii ` is reinstalled.
    - A fixture with no homelab `Packages` list, and one whose list lacks the host
      package's stanza, each exit non-zero and leave the success timestamp unadvanced.
    - A stub `apt-get update` that exits non-zero, over a list left by an earlier good
      fetch, makes the run exit non-zero with no metric written, no hold, and
      `last_success` unadvanced. The stub records `--error-on=any` on the restricted fetch
      and its absence on the full update. The container's stopped-server case proves the
      same against real apt.
    - A second dependency failure inside one day runs no second full `apt-get update`.
    - With the textfile directory missing, the run exits non-zero and creates no directory.
      With it present, `homelab_host.prom` is written with mode 0644.
    - The rendered `homelab-updater.service`'s `TimeoutStartSec` exceeds the rendered
      `homelab-reconcile.service`'s plus step 5's `DPkg::Lock::Timeout`. A fixture updater
      unit at 20 minutes is flagged.
- *Unit validator.* A fixture host component whose unit carries a typo'd directive key is
  flagged, and the rendered `homelab-updater.timer` with the Pi's drop-in is clean. The
  validator asserts `homelab-updater.timer` as a named member.
- *LAN-facts guard.* A fixture `hosts/<name>.sh` typing daniel-box's address outside the
  allowlist is flagged. The URL in `hosts/bootstrap-host.sh` equals
  `http://<server_ip>:8791` for daniel-box, and a fixture with another address fails that
  test.
- *Builder page (H3.5c).* For the `host_packages` check body, each arm is a red and green
  pair:
    - A builder fetch timestamp 31 minutes old is red, and one 1 minute old is green. An
      absent builder series is red.
    - An unbuilt-since value older than the lag bound is red, and one newer than the bound,
      or 0, is green.
    - With `up{job="node"}` at 0, the row is suppressed. With it at 1 and the builder
      series absent, the row is red.
    - *Rule form.* The same pairs are `promtool test rules` cases. The `up` pairs assert
      that each absent-series rule stays silent while its exporter's `up` reads 0, and
      fires once it reads 1 with the series still absent.
- *Host rules (H3.5d).* Each pair is a `promtool test rules` case:
    - `homelab_host_reconcile_ok` at 0 for 16 minutes fires `HostReconcileFailed`, and at
      0 for 14 minutes, or at 1, does not.
    - A listed host's success timestamp past its limit fires `HostUpdateStale`, and so
      does an absent series while that host's `up` reads 1. With `up` at 0, the absent
      series stays silent.
    - The limit test flags a fixture limit shorter than the longest gap of the Pi's
      rendered schedule.
    - *H3.9c.* With `up{job="node-pi"}` at 1 and the Pi's success timestamp present but
      past its limit, `HostUpdateStale` fires for daniel-pi.
- *Builder.*
    - A missing token makes no request and exits 1.
    - A cached green verdict makes no second request.
    - A red verdict is asked again, so a re-run that turns it green is built, and a `fail`
      an older run left in `ci-cache.json` blocks nothing
      (`ansible/roles/setup/homelab_builder/tests/test_host_package_builder_ci_cache.py`).
    - A tip that does not descend from the last built commit is refused. It exits non-zero
      on the first run that meets that tip, and 0 on a repeat. It writes
      `homelab_builder.prom`, whose unbuilt-since value is then non-zero, with `reason`
      `ancestry` and `sha` naming the commit.
    - A green fixture commit that is not on `origin/master`'s first-parent chain is never
      chosen.
    - A held builder fetches, writes its metrics with `reason="hold"` and builds nothing.
    - A walk with no green commit exits 0 and writes `reason="no-green"`. A run that builds
      writes `reason="ok"` with the value 0.
    - A check-runs read refused with 401 or 403 builds nothing, exits non-zero on the first
      run and 0 on a repeat, and writes `reason="token"`.
    - A pending tip over a green parent builds the parent, and so does a red tip. The walk
      stops at the last built commit and at the walk bound in `config.env`.
    - A `config.env` with an empty `CI_CONTEXTS`, and one with a walk bound of 0, each make
      no CI request, build nothing, exit non-zero on the first run and 0 on a repeat, and
      write `reason="ci"`. The same fixture history with the `prek` context builds.
    - The role's `config.env` render holds `CI_CONTEXTS` equal to
      `gitops_deploy_ci_contexts`, read from the defaults file. A fixture list that differs
      fails it.
    - Built over a pending tip, the parent's version carries the parent's own first-parent
      count, not the tip's. Over a fixture history built commit by commit, each published
      version compares strictly greater, by `dpkg --compare-versions`, than the one before.
    - `publish-textfile` drops a line whose series name lacks the unit's prefix, and writes
      the file with mode 0644.
    - *Keyring check.* A render whose `homelab-updater` keyring has a fingerprint other than
      `archive-key.fpr`'s, and one with no keyring, each stage nothing, exit non-zero on
      the first run and 0 on a repeat, and write `reason="keyring"`. A render whose keyring
      matches builds.
    - *Sandboxing.* A test reads the rendered builder and signer units and asserts each
      directive *Sandboxing* lists, with `ReadWritePaths` equal to the listed paths. A
      fixture builder unit without `NoNewPrivileges=yes`, and a fixture signer unit
      without `PrivateNetwork=yes`, are each flagged.
    - *Signer refusal.* The signer script runs against a fixture `staged/`. A `.deb`
      named `other-tool`, a `homelab-daniel-pi` whose version names another SHA, and a
      stray file each make it publish nothing, move `staged/` into `refused/`, and exit
      non-zero. One `homelab-daniel-pi` whose version names `staged/chosen`'s SHA is
      published.
    - *Valid-Until.* A signed fixture `Release` carries `Valid-Until` seven days after
      its `Date`. With `staged/` empty, a `Release` dated 25 hours ago is re-signed, and
      one dated 1 hour ago is left alone. In the container suite, a repository whose
      `Release` is past its `Valid-Until` makes updater step 3 exit non-zero with
      `last_success` unchanged.
    - *Staged rollout (H3.12).* A fixture run that changes two host packages stages the
      canary's alone and writes `reason="canary"`. A stub Prometheus that reports the
      canary at V with `reconcile_ok` 1 lets the next run build the other. One that
      reports V with `reconcile_ok` 0, an older SHA, or no answer on either address
      builds nothing more. A `release` file naming V's SHA builds the other at once. A
      change that skips the Pi's package makes daniel-server the canary.
    - *Tools (H3.11).* A fixture binary whose sha256 differs from its pin builds nothing
      and writes `reason="tools"`. The `arm64` CI job installs the `arm64` build and
      runs each tool's `--version`. A fixture host with `homelab-tools` installed and a
      newer one published makes updater step 5 name it.
- *Bootstrap pin (H3.6).* The fingerprint pinned in `hosts/bootstrap-host.sh` equals the
  fingerprint of `hosts/keys/homelab-archive-keyring.gpg`, and a fixture pin that differs
  is flagged.
- *CI gate.* `ansible/tests/repo/test_every_ci_job_is_gated.py` already flags a job the gate
  never reads. `host_packages` joins its `KNOWN_JOBS`.
- *Land host wait.*
    - A PR touching only `hosts/daniel-pi/package.yml`, with a stub Prometheus reporting the
      merge SHA and `reconcile_ok` 1, runs every step and ends `settled`. Neither
      `shortcut_if_nothing` nor `no_tag_outcome` finishes it.
    - A hosts-only PR whose CI is still pending runs no renderer before the CI step. A stub
      renderer records every call, and the stub CI wait records when it returned.
    - A PR whose paths fall under `HOST_INPUTS` but whose renders match ends
      `nothing-to-deploy` after the CI step.
    - A PR touching `hosts/` and a manual-plane path ends `needs-manual-apply` with "reaches
      no service tag, but is not done", and prints the host it did not wait on.
    - A failed snapshot makes every host with a package a target.
    - A stub Prometheus returning an older SHA ends `deferred` at the deadline, and a
      descendant SHA with `reconcile_ok` 1 settles.
    - `reconcile_ok` 0 ends `deploy-failed`.
    - With loopback refused and the ClusterIP answering, the host settles. With both refused,
      it does not settle, and the message names the transport.
    - A host whose next fire is beyond 45 minutes ends `deferred` with no wait, unless the
      first query already shows it settled, which ends `settled`.
    - A merge that adds the updater timer's enable link, over a first parent whose render
      ships none, ends `deferred` at once, naming the start command. A re-run whose first
      query shows the host settled ends `settled`.
    - The `HOST_INPUTS` audit test flags a fixture render that opens a repo file outside
      every member.
- *Renderer determinism.* Two renders of one commit are byte-identical.

## Rollback

- **H3.1, H3.2a to H3.2d, H3.3a and H3.3b:** revert each on its own, in reverse order. They
  change no host. Reverting H3.2c drops the host units from the unit validator and the
  censuses, which nothing ships yet. Reverting H3.3b leaves H3.3a's render diff and lints in
  the gate. It also takes back the updater's cache options, so every run, the common path
  included, rewrites `pkgcache.bin` again.
- **H3.4a:** reverting it parks the deployer again, and the clear is the same operator-only
  sequence, range check included. Never revert it while H3.4b to H3.5b are armed. Disarm
  first.
- **H3.4c:** revert it only while H3.5a is unmerged. Once the builder runs, a revert drops
  the keyring from `homelab-updater`'s render, and builder step 6 refuses every build with
  `reason="keyring"`. That fails closed: nothing is published, `OnFailure=` posts once, and
  the builder arm pages once the lag bound passes. Replace the key through the rotation in
  `docs/break-glass.md` instead.
- **A failed tick apply of `homelab_builder` or `gitops_deploy` (H3.4b to H3.5b).** Every
  broad arm of the deployer is forward-only, so the failure writes `hold_sha` and a
  `hold_plane` entry and leaves the tree merged. `land.sh` then reports `deploy-failed` for
  every session's landing until the hold clears. The clear path, which
  `docs/host-packages.md` carries from H3.4b:
    1. Merge a fix, or the disarm below, as its own PR. The tick applies that role, and an
       apply covering every held plane clears `hold_sha` with no further step.
    2. If that apply fails too, read `hold_plane` and the host's `owed.jsonl` for every plane
       the hold names. H3.5a's tick applies `gitops_deploy` as well as `homelab_builder`, so
       the list can hold both.
    3. **Operator-only**, because the play's `load_secrets` pre_task decrypts: apply each
       named plane by hand, for example `uv run ansible-playbook ansible/initial_setup.yml
       --tags homelab_builder,gitops_deploy`.
    4. A hand `ansible-playbook` run is not the deployer, so it clears nothing on its own.
       Once every held plane is applied, run `uv run python
       scripts/deploy_tools/gitops_state.py clear-hold <full hold_sha>`, as
       `docs/gitops-pipeline.md`'s *Which apply clears a hold* states.
- **H3.4b to H3.5b:** disarm by setting `homelab_builder_enabled: false` in the role's
  defaults. `deploy_changes.py:setup_tags_for` maps that path to `homelab_builder`, so the
  tick applies the teardown arm on daniel-box.
  The teardown stops and removes the four units, the ufw rule and `/var/lib/homelab-builder/`.
  It keeps `/var/lib/homelab-signer/`, so the signing key every host trusts survives. Revert
  the role body in a later PR. Deleting the role and its entry is H7's, and that PR parks the
  deployer once more. Hosts keep their installed packages, and apt reports the source as
  unreachable. Each host's updater then fails at step 3, so after H3.9c a disarm fires
  `HostUpdateStale` for the Pi once its limit passes. That page is the signal that no
  host is updating, and it stays until the builder is re-armed. Rotating the key is a separate procedure in `docs/break-glass.md`.
  Reverting H3.5a's `deploy_cross_role.py` rows goes in the PR that removes the role's
  imports and its `config.env` read of the `gitops_deploy` defaults, so the table and the
  tree stay equal.
- **H3.5c:** revert, and land the revert so monitor-bridge and uptime-kuma redeploy without
  the row and its tile. The push-token secret stays until the operator removes it. In the
  rule form, land the revert to the observability role, which removes the two rules.
  Nothing else carries state.
- **H3.5d:** revert H3.9c first, then H3.5d, and land each to the observability role.
  While the Pi's timer is armed, nothing then pages on a held Pi, so revert H3.9b first
  as well when the revert is to last.
- **H3.6 and H3.7:** the operator runs `sudo apt-get remove homelab-daniel-pi homelab-updater`
  on the Pi. Plain files mean `remove` deletes everything the packages installed. Then the
  operator deletes `/etc/apt/sources.list.d/homelab.sources` and the keyring.
- **H3.8:** revert on master.
    - The builder publishes a higher version without the component, and dpkg deletes the
      timers. If the Pi holds, the operator clears the hold first.
    - The revert puts the five defaults and the eleven tasks back in `optimize_pi`, on master
      but not on the Pi. **Operator-only:** re-apply `--tags sd-health,recovery-health -e
      target=daniel-pi`, then clear the `manual_plane` line.
    - Between the two steps no heartbeat runs, and the two Kuma push tiles go DOWN. That gap
      is visible, not silent.
- **H3.9a:** revert. Nothing calls the module.
- **H3.9b:** revert H3.9c first, or `HostUpdateStale` fires once the hand-started runs age
  past the Pi's limit. Then revert H3.9b. Reconcile stops the Pi's updater timer, because
  its enable link was dropped, and the Pi returns to hand-started updates. land.sh stops
  waiting on hosts, and a hosts-only PR again finishes `nothing-to-deploy`.
- **H3.9c:** revert. The expected-host map loses daniel-pi, so `HostUpdateStale` stops for
  the Pi. `HostReconcileFailed` still covers it.
- **H3.10:** no rollback is needed.
- **H3.11:** revert. The Pi package drops its `Depends`, and apt marks `homelab-tools`
  automatic, so the weekly autoremove removes it. The updater then names only its two
  packages again.
- **H3.12:** revert. Every changed host package publishes in the run that builds it. Any
  open gate state is ignored, and the role's next apply deletes it.

**What a partial failure leaves live:**

- A builder failure leaves the last published repo, and every host keeps its version.
- A signer failure leaves the packages in `staged/`. The next successful builder run starts
  the signer again.
- An updater killed mid-install leaves a half-configured or half-installed package. The next
  run's step 2 or step 4 repairs it, then runs reconcile before it writes any metric.
- A reconcile failure leaves the package configured with some units unstarted. The updater
  holds that host, `homelab_host_reconcile_ok` reads 0, `HostReconcileFailed` fires after
  15 minutes, and `land.sh` reports `deploy-failed` for a PR that touches that host.
- A signer refusal leaves the last published repo and the refused files under
  `refused/`. The builder's record still names those renders as published, so the runbook
  resets that record after the cause is found.

## Risks

- **H3.4a parks every session's deploys.** The entry is its own PR of about 60 lines, the
  attended clear applies an empty arm, and every later role change lands through the tick.
  The clear's range check keeps the hand ff-merge from stranding another session's merge.
- **A failed tick apply holds every session's landings.** H3.4b, H3.5a and H3.5b each land
  through a setup-plane tick on daniel-box whose tasks can fail: the key generation as a
  system user, the uv and pinned-Python install, and the nfpm and `apt-utils` installs.
  H3.5a's tick applies `gitops_deploy` too. A failure writes `hold_sha`, and `land.sh` then
  reports `deploy-failed` for everyone until an apply covering the held plane, or a clear.
  Each of the three merges in an attended window, and *Rollback* gives the clear path,
  whose last resort is operator-only.
- **H7's Ansible deletion can drop a piece this slice leaves in Ansible.** The CI keys, the
  `archive-key.fpr` writer, `publish-textfile` and the Pi's textfile directory each have a
  reader that keeps running after its Ansible home is gone: the builder, the updater and
  C6's host wait. Each fails later, on a rebuilt host, rather than at the move. *What H7
  inherits from this slice* lists every piece, the grep that lists the builder's reads of
  the CI keys, and the fresh-Pi check.
- **A wedged apt cuts remote access to the WireGuard endpoint.**
    - The postinst cannot fail, and the updater runs by hand until H3.9b.
    - The killed-pull test runs in CI before it runs on the Pi.
    - Every Pi step in this slice runs from a LAN session.
- **apt memory on the Pi.** The common path fetches one `InRelease` and scans the homelab
  `Packages` list with awk. It builds no apt cache and installs nothing.
    - H3.6 measures four runs with one tool, `systemd-run --wait -p MemoryAccounting=yes`,
      whose summary prints the cgroup's memory peak: `apt-get -d dist-upgrade`, which
      `apt-daily` already runs once a day; the updater's common path; its install path; and
      its full `apt-get update` path.
    - The baseline is re-measured this way because the recorded 57 MB is a process RSS. A
      cgroup peak can also count page cache, so the two figures are not comparable.
    - The fallback applies when the install or full-update path peaks above the re-measured
      `apt-get -d dist-upgrade`, or when the common path peaks above a quarter of it. The
      common path runs up to 80 times a day, where the baseline runs once.
    - The fallback moves the Pi's drop-in to once a day at 00:30. That slot follows the
      midnight cluster and precedes man-db, the 01:40 sweep and the apt timers. The land host
      wait then ends `deferred` for the Pi unless its first query finds the Pi settled. The
      operator meets exit observation 4 by starting the updater by hand after the builder
      publishes, then re-running the same `land.sh` command. The limit test then requires
      the Pi's limit to exceed a day, and daniel-server becomes the rollout canary.
- **H3 adds the first unattended path from master to root on the Pi.** The Pi carries the
  WireGuard endpoint. `daniel-pi.yml` sets `has_gitops: false`, so no deploy reaches the
  Pi unattended at `70591c307`. From H3.9b, a merged commit with a green CI verdict
  reaches root there within one updater fire. That anchor is the one the deployer gives
  the two k3s nodes. [ADR-0021](../../adr/0021-the-host-package-builder-trusts-master-plus-green-ci.md) keeps that anchor for the Pi, and option (c) of *Open
  questions* 1 stays available to take the Pi off the unattended path.
- **The signing key is root on every host that trusts it.** It lives only in
  `homelab-signer`'s GnuPG home, mode 0700, plus the break-glass copy.
    - The signer runs only the script the role installs, and the role arrives from master
      through the tick. A merged change to the role reaches the key by design (*Open
      questions* 1).
    - Code that runs as the builder still decides what the signer is handed. The refusal
      rule limits that to `homelab-*` packages at the chosen commit's version, and the
      sandbox limits what a builder run can write. A builder run that renders a malicious
      package for the chosen commit still gets it signed. The anchor, not the split
      between the two users, is what keeps that commit honest.
    - H3.5a's reach check is the gate on every other route to the key. The operator runs
      it before the builder first runs, and the PR body carries its output.
    - **`ubuntu` reaches the key, and the check does not cover it.** The operator ruled on
      2026-10-15 that the check runs for `claude` and `homelab-builder` only. `ubuntu` is the
      operator's administrative account, and it already holds root on all three hosts
      without going through master, so the signing key adds nothing to its reach. While
      agent sessions still run as `ubuntu`, only the session hooks keep them from the key,
      as they do from root. Retiring those sessions is slice 6 of the move to the `claude`
      user (`docs/claude-agent-user.md`).
- **daniel-box holds the builder, the signer and the repository.** A daniel-box outage
  stops publishing, every host keeps its installed version, and `HostUpdateStale` fires
  for each host once its limit passes. The break-glass copy of the key is the off-box
  recovery. Splitting the three across hosts would put the signing key on a second host or
  the repository behind a second failure domain, which costs more than the outage. This
  slice accepts the concentration. H7 keeps it, moving the units into
  `homelab-daniel-box`.
- **The canary is the WireGuard endpoint.** A shared change that breaks daniel-pi costs
  off-LAN access until a LAN session repairs it. The alternatives cost more (*Staged
  rollout*), every Pi step already runs from a LAN session, and a reconcile failure holds
  the Pi rather than retrying.
- **Failure class 2, the check observes a proxy.** A configured package is not an applied
  one. Every updater path derives `homelab_host_applied_info` and
  `homelab_host_reconcile_ok` from reconcile's per-version record, never from dpkg state, so
  an updater killed between the install and its verdict cannot report success. Land also
  requires `homelab_host_reconcile_ok` at 1.
- **Failure class 3, empty read as clean.** A missing `homelab_host.prom` must read as stale,
  never as up to date. The land host wait treats an absent series as not settled, and an
  unreachable Prometheus as not settled with its own message. `HostReconcileFailed` and
  `HostUpdateStale` page on a held host and on a stale or absent success timestamp, and
  the signer pages through `OnFailure=`, so a hold no landing touches still reaches the
  operator. The
  updater's candidate scan treats a missing `Version:` as a failure, not as "nothing newer".
    - A builder that runs and builds nothing reads as clean to every host metric, because
      the updater's common path advances the success timestamp whether or not anything new
      was published. The builder's own two series cover it: the fetch timestamp catches a
      builder that stopped, and the unbuilt-since value catches one that runs while master
      moves past it.
    - A missing textfile directory fails the updater rather than dropping its metric, and
      the H3.6 operator check reads the series through Prometheus.
    - A failed homelab fetch leaves the list from the last good fetch, which still holds
      `Version:` lines, so step 4's guard cannot see it. Step 3 therefore runs with
      `--error-on=any` and ends the run non-zero without advancing the success timestamp.
      An unreachable repository, a stopped `homelab-apt-repo.service` or a keyring
      mismatch then reads as stale, never as "nothing newer". The container's
      stopped-server case is the red proof.
    - A placeholder or mismatched archive keyring never ships: the builder refuses it with
      `reason="keyring"`, and H3.6's test holds the bootstrap pin equal to the committed
      key.
- **Page cadence.** The builder fires every 2 minutes, and `alert_unit.yml` posts once per
  failure, so a builder failing on every run would post 30 times an hour. The builder exits
  non-zero once for each reason and tip, and the bridge row carries what persists.
- **`EXPORTER_DEPENDENT` suppresses the whole row.** With `host_packages` under `node`, a
  dead exporter on daniel-box hides both builder arms until that exporter answers. The
  host rules carry their own `up` clauses, so a dead exporter silences only its host's. The exporter's own page fires meanwhile. That is
  the bridge's rule for every exporter-dependent row (the `DECIDED` marker at the probe in
  `ansible/roles/k8s/monitor-bridge/files/check.py`). In the rule form, a dead exporter
  silences only the rules that read its series.
- **H3.5c and M6 can merge in either order.** H3 waits only on R1, and no slice-index edge
  links H3 and M6. M6's baseline counts 44 rows of `CHECKS` and 44 bridge push tokens, and
  neither count includes `host_packages`. A row merged after M6's A1 shadow PR would miss
  A1's migration, and a Kuma tile merged after M6's Phase C would meet a Kuma that is gone.
  Mitigation: H3.5c's form follows A1's shadow PR (*Steps*, H3.5c), and the form check runs
  again at rebase. A row that misses A1 anyway fails M6's planned bijection test at A6-7a,
  so the push path cannot be deleted under it. This round's cross-slice note asks M6 to
  list the row, its token and its one `EXPORTER_DEPENDENT` entry in A1. H3.5d's host
  rules are M1-native, so M6 has nothing to move for them. Failure class 8
  applies: M6's counts are prose that nothing re-derives, so they go stale without an
  error when the row merges first.
- **Failure class 4, selector drift.**
    - The reconcile sweep keys on `homelab-*`. The naming census holds every shipped unit to
      that prefix and asserts named members.
    - Three selectors find the moved templates by role path. H3.8b moves each to the
      component, with a named member.
    - Two validators found files only under `ansible/roles/`: `unit_templates.py` and the
      prek selectors of both template hooks. H3.2c verifies every unit and drop-in the host
      renderer emits, with `homelab-updater.timer` as a named member. H3.8b widens the shell
      hook's selector.
    - Land's `HOST_INPUTS` path test is a selector over the renderer's reads. The audit test
      holds it to every file a host render opens, with named members.
    - The CI job renders on every run instead of filtering on `hosts/`, so a `group_vars`
      change that alters a render still runs the container tests.
- **Failure class 5, sequences treated as transactions.**
    - The handoff rule makes the ordering structural: the package retires the Ansible-era
      item only after its own units start.
    - Reconcile waits for the dpkg frontend lock before it reads, so it never reads half a
      transaction. It records the versions it read, so a transaction that lands while it acts
      shows as a version mismatch, and the updater runs it again.
    - Reconcile advances each entry only after its action succeeded, so a failed run is
      retried rather than recorded as done.
- **Failure class 7, execution context.**
    - The heartbeats move from cron to systemd units with explicit `Environment=` and
      `User=`, which removes the cron `PATH` trap and keeps the directory owner.
    - Reconcile runs under the host's Python 3.12 while CI runs 3.14. The container suite
      runs it under 3.12.
    - The Pi package refuses to install on any other host.
    - `land.sh` reaches Prometheus from either node, through loopback or the pinned ClusterIP.

## Tests and docs that change in the same PR

- **H3.1:**
    - renderer host-mode tests beside R1's renderer tests: the secret-refusal pair, the `${`
      literal, and the component defaults layer beneath `host_vars`;
    - a test of the `render_context` keyword in `scripts/lib/tests/`;
    - the `testpaths` entry;
    - `docs/homelab-render.md` gains a host-mode section.
- **H3.2a:** `hosts/lib/tests/` covers the updater, its fetch-failure arm, both
  `--clear-hold` levers, the maintainer-script lint and the timeout inequality.
- **H3.2b:** `hosts/lib/tests/` covers reconcile and the 3.12 syntax check.
- **H3.2c:**
    - the two-writer census joins `ansible/tests/setup/`;
    - the naming census is a `Census` row of `ansible/tests/repo/test_census_rows_text.py`,
      as `.claude/rules/python-layout.md` asks of a textual census;
    - `scripts/validate/unit_templates.py`'s test gains the host-render pair and the named
      member, and its module docstring names the host renders beside `ansible/roles/`;
    - the `validate-unit-templates` selector in `prek.toml`.
- **H3.2d:**
    - `scripts/renderer/tests/test_renderer_host_manifest.py` holds the manifest cases, one
      parsed by reconcile's `parse_manifest`;
    - the two-writer census gains the legacy join and its red fixture;
    - `docs/homelab-render.md`'s host-mode section documents `units:`, `legacy:` and the
      manifest.
- **H3.3a:**
    - `KNOWN_JOBS` in `ansible/tests/repo/test_every_ci_job_is_gated.py` gains
      `host_packages`;
    - `scripts/validate/tests/test_validate_host_packages.py` holds the `type: config` pair,
      the `.deb` lint pair and the render diff, and `test_maintainer_scripts.py` imports the
      whitelist from `scripts/validate/host_packages.py`.
- **H3.3b:** the job, whose cases are the tests on both release legs, the stopped-server
  case included. `scripts/validate/tests/test_validate_host_package_cases.py` covers the
  fixtures, the local repository and the two log checks without a container.
  `hosts/lib/tests/test_homelab_updater_fetch.py` asserts the cache options the common-path
  case made the updater carry.
- **H3.4a:** the `homelab_builder` role `CLAUDE.md`, whose glance block `gen_role_glance.py`
  writes.
- **H3.4b to H3.5c:**
    - the role `CLAUDE.md`, including its autonomous-role contract (H3.5a);
    - a row in `ansible/tests/setup/test_setup_cron_roles_have_a_contract.py`'s census
      if that census does not derive the role on its own;
    - the `KNOWN_ALERTS` entries in `ansible/tests/setup/test_alert_unit.py`, the builder's
      (H3.5a) and the signer's (H3.5b);
    - `homelab_builder` in `EXPECTED_CONSUMERS` of
      `ansible/tests/setup/test_host_lib_sibling_copies.py`, which compares with `==` (H3.5a);
    - `homelab_builder` in `_ALERT_UNIT` and `_HOST_LIB` of
      `deploy_cross_role.py:SETUP_FILES_SHIPPED_BY_OTHER_ROLES`, which
      `ansible/tests/setup/test_setup_cross_role_files.py` holds equal to the tree, and the
      stamp imports that `_STAMPED` then requires (H3.5a);
    - the `gitops_deploy/defaults/main.yml` row naming `homelab_builder` in the same table,
      which that file's defaults-read census requires, and the `config.env` render test
      that holds `CI_CONTEXTS` equal to `gitops_deploy_ci_contexts` (H3.5a);
    - `docs/break-glass.md` gains where the signing key lives, how to restore it, and how to
      rotate it (H3.4b);
    - `docs/host-packages.md`, the runbook, and its `mkdocs.yml` nav entry, both created in
      H3.4b with the clear path for a deployer hold that a failed `homelab_builder` or
      `gitops_deploy` apply wrote (*Rollback*);
    - the shared-package render test names the keyring path as a member of
      `homelab-updater`'s render (H3.4c);
    - the runbook's remaining procedures (H3.5b): clear a host hold with either lever, mask
      a host's timer, set and clear the builder hold, roll a host back, rotate the key,
      recover from a revoked key, and clear a park caused by the `homelab_builder` entry
      with H3.4a's range check. It also covers the five builder pages: reset the
      builder's last-built commit after a rewritten master, reset its published record
      after a signer refusal, match
      `gitops_deploy_ci_contexts` to a renamed CI job, resolve a `keyring` refusal, and
      resolve a `ci` refusal by restoring the context list or the walk bound and re-applying
      `homelab_builder`;
    - the builder's tests beside the role's script: the failure exits, the `ci` refusal,
      the version count, the keyring check, `publish-textfile` and the sandboxing test
      (H3.5a), and the signer refusal and `Valid-Until` tests (H3.5b);
    - [ADR-0021](../../adr/0021-the-host-package-builder-trusts-master-plus-green-ci.md) for *Open questions* 1, merged before H3.5a;
    - `docs/gitops-pipeline.md` gains a note that the builder, not the tick, applies `hosts/`
      (H3.5b);
    - the `host_packages` row's red and green pairs in
      `ansible/roles/k8s/monitor-bridge/tests/`, the builder arms and the `node` suppression
      pair included, the limit test beside the renderer's tests, the row's entry in
      `docs/monitor-bridge-checks.md`, and `host_packages` under `node` in the
      `EXPORTER_DEPENDENT` membership on `docs/monitor-bridge-internals.md` (H3.5c). In the
      rule form, the two rules' `promtool test rules` cases. The runbook's builder page
      section names the two alerts instead of the tile;
    - the two host rules' `promtool test rules` cases and the limit test, and the
      runbook's host-plane page section naming `HostReconcileFailed` and
      `HostUpdateStale` (H3.5d).
- **H3.6:**
    - `ansible/tests/setup/test_pi_node_exporter_host_unit.py` changes with the textfile
      flag, and asserts the directory task's owner, group and mode;
    - `ansible/tests/repo/test_lan_facts_have_one_source.py` gains `hosts`, `.sh`, the
      bootstrap allowlist row and its URL test;
    - the bootstrap fingerprint-pin test, beside the URL test;
    - `docs/pi-host-tuning-record.md` records the updater's measured memory peaks.
- **H3.8b:**
    - `ansible/tests/setup/test_pi_recovery_restarts_and_reports.py`,
      `ansible/tests/setup/test_pi_gz_integrity_sweep.py`,
      `ansible/tests/setup/test_pi_health_log_line_shape.py` and `ansible/tests/setup/_pi_health.py`
      retarget the rendered component and its `defaults:` map instead of the role;
    - `ansible/tests/setup/test_cron_scripts_publish_via_pr.py:push_corpus` names the two
      scripts at their component path;
    - `ansible/tests/repo/test_secret_rendering_host_scripts_have_no_log.py:KNOWN_SECRET_SCRIPTS`
      drops both, because neither renders a secret any more;
    - `scripts/validate/shell_templates.py:discover_templates` also globs
      `hosts/components/*/templates/`, and its test names `pi-sd-health.sh.j2` there as a
      member;
    - `ansible/roles/setup/optimize_pi/CLAUDE.md` loses the heartbeat sections and points at
      the component;
    - its `## Autonomous-role contract` moves into a header comment in
      `hosts/components/pi-heartbeats/component.yml`;
    - `docs/facts.lock` follows those edits, or CI's
      `test_every_recorded_atom_hashes_as_recorded` goes red. The lock records five
      sections of that `CLAUDE.md`. Run `uv run python scripts/dev/fact_status.py forget`
      for the removed *Autonomous-role contract* section's key, and `fact_status.py verify`
      for *Where it runs*, *What it does* and any other section whose citations the PR
      changes. *What it does* cites a test node in
      `ansible/tests/setup/test_pi_health_log_line_shape.py`, which this PR retargets;
    - the `# fact:` marker in
      `ansible/tests/setup/test_pi_recovery_restarts_and_reports.py:test_a_dead_container_is_restarted`
      names the removed section, so it goes in the same edit;
    - the role's `## At a glance` block loses its three crons. The `regen-role-glance` hook
      rewrites it, and the rewritten block is committed;
    - `ansible/tests/setup/test_setup_cron_roles_have_a_contract.py:test_optimize_pi_is_not_exempt`
      would pass vacuously once the role installs no cron. It is replaced by a test beside
      the component tests that `hosts/components/pi-heartbeats/component.yml` carries the
      contract header;
    - `docs/pi-host-tuning-record.md`: *Disarming it* is rewritten for the package path. Set
      `optimize_pi_recovery_restart_enabled: false` in `inventory/host_vars/daniel-pi.yml`,
      merge, let the updater install the version without the timer's enable link (or start
      it by hand before H3.9b), then check that
      `systemctl list-timers homelab-pi-recovery.timer` lists nothing. The heartbeat
      descriptions that say "cron, every 5 minutes" and "cron, daily 01:40 UTC" name the
      package timers instead;
    - the `validate-shell-templates` selector in `prek.toml`.
- **H3.9a:** land tests in `scripts/deploy_tools/tests/` for `host_targets`, the transport,
  the settle rule and the deadline, and the `HOST_INPUTS` audit test beside the renderer's
  tests.
- **H3.9b:**
    - land tests in `scripts/deploy_tools/tests/` for the candidate at resolve, the render
      after CI, the plane and self-applied routes through `deploy_phase`, and the verdict
      wording;
    - `docs/landing.md` gains the host-wait paragraph and when it ends `deferred`;
    - `.claude/skills/land-after-merge/SKILL.md` and root `CLAUDE.md` (*After a PR Merges*)
      gain one line each: a PR touching a host package waits for that host;
    - `docs/facts.lock` records `CLAUDE.md#After a PR Merges — Pull, Deploy, Verify`. When
      the new line cites a path or symbol, run `fact_status.py verify` on that key in the
      same commit, or the citation reads as `unrecorded-atom`;
    - a land test for the first-parent enable-link rule, and `docs/landing.md` names the
      operator's hand start for a PR that arms a host's timer.
- **H3.9c:** the daniel-pi `promtool test rules` cases for `HostUpdateStale`, its
  `node-pi` `up` pair included, and the limit test's daniel-pi member.
- **H3.10a:** the naming census gains the canary pair as named members.
- **H3.10b:** the naming census drops the canary pair from its named members.
- **H3.11:** the tools tests beside the builder's, the updater test for a third
  `homelab-*` package, `KNOWN_JOBS` gains the `arm64` job, and the runbook names the
  `tools` refusal.
- **H3.12:** the staged-rollout tests beside the builder's, and the runbook's override
  step.

## Complexity delta

- **Added:**
    - the renderer's host mode and the component defaults layer;
    - two shared scripts (updater, reconcile), and reconcile's per-version record;
    - one CI job and its test image on two release legs, and one `arm64` CI job;
    - one Ansible setup role (`homelab_builder`, temporary until H7) with two system users and
      four units;
    - a signing key, the signer's refusal rule, `Valid-Until` and its daily re-sign arm;
    - sandboxing directives on two units, and the test that holds them;
    - one operator-only reach check, run once before H3.5a;
    - `bootstrap-host.sh` (three steps);
    - two package manifests;
    - four lints or censuses, the timeout inequality test, and the unit validator's host
      renders;
    - one land step, a post-CI render and the `HOST_INPUTS` path test;
    - one monitor-bridge row with its tile and push token, two builder arms, and one
      `EXPORTER_DEPENDENT` entry. In H3.5c's rule form, two alert rules with their
      `promtool` cases replace all of it, and no token or tile is added;
    - two host alert rules (H3.5d) with their `promtool` cases, and one observability
      default, the expected-host map;
    - one updater flag, `--install-newest`;
    - one architecture-specific package, `homelab-tools`, with its pins, their Renovate
      manager and the builder's download cache;
    - the staged-rollout gate, one root-owned override file and two `reason` values;
    - the builder's two series, its once-per-condition failure record, and the role's
      `publish-textfile` script;
    - about 16 head series at H3's end, against M1's budget of 140,000 (*Shared names*,
      Metrics);
    - the committed archive keyring (H3.4c), `archive-key.fpr`, the builder's keyring check
      and the bootstrap pin test;
    - the builder's `ci` refusal, its `config.env` render test, and one cross-role table
      row for the `gitops_deploy` defaults it reads;
    - the updater's fetch-failure arm and its container case;
    - a canary service and timer, added in H3.10a and removed in H3.10b;
    - one `optimize_pi` task that creates the Pi's textfile directory;
    - four `optimize_pi` tasks that stage the two interim push-token files (H3.8a), which
      H4's last PR retires;
    - one runbook page.
- **Removed:** three crons and eleven tasks from `optimize_pi`, and the inline push tokens in
  two rendered scripts. Five defaults move rather than go.
- **Net:** this slice adds more than it removes. Its value is the pipeline that H4 to H7
  reuse.

## Open questions

1. **Does the builder require anything beyond the master-plus-green-CI anchor?** C5 makes a
   commit on `origin/master` with a green CI verdict the builder's anchor. That is the
   anchor `deploy_git.py:ci_walk_candidates` gives the GitOps deployer. Master commits
   carry GitHub's signature, not the operator's (*Baseline*), so no operator-signature
   check can pass on them. The options:
    - **(a) Master plus green CI, and nothing more.** The two k3s nodes already take setup
      roles from master on this anchor. The cost lands on the Pi, which gains its first
      unattended path from master to root (*Risks*).
    - **(b) A signed landing tag.** `land.sh` pushes a signed tag for each merge SHA, and
      the builder verifies it against a root-owned allowed-signers file. The tag adds
      protection only if its signing key is out of reach of every account that runs
      `land.sh`. In practice the operator then signs each landing, and every host change
      waits on the operator.
    - **(c) A Pi-only manual approval gate.** The Pi package ships no enable link for
      `homelab-updater.timer`, so the Pi installs only when the operator starts its
      updater from a LAN session. The other hosts take (a). Every Pi change, Renovate's
      included, then waits on the operator, and `land.sh` ends `deferred` for the Pi.

   Recommendation: (a). The signer's key lives on daniel-box, and the tick applies
   `homelab_builder` there from master on the same anchor (H3.4a). A merged change to that
   role therefore reaches the key whatever the builder checks, so (b) adds a landing step
   without narrowing what a merged commit reaches. (c) is the only option that narrows it,
   and only for the Pi. It stays available at no design cost: H3.9b's enable link is the
   switch, and dropping it moves the Pi to (c).

   Resolution: the operator chose (a) on 2026-10-14, and [ADR-0021](../../adr/0021-the-host-package-builder-trusts-master-plus-green-ci.md) records it. Builder step 5
   adds no check. Moving the Pi to (c) later drops H3.9b's link and meets exit observation
   4 with a hand-started run.
2. **What if the Depends gate fails?** If `apt-get install homelab-daniel-pi` does not
   install an added `Depends`, the design's fallback applies: ansible-pull becomes the plan,
   and H4 to H7 are re-planned. The slice stops at H3.7. H3.1 to H3.6 stay, because the
   renderer and the version metric serve either path.
3. **C1 does not name the shared packages.** C1's host mode emits only `homelab-<hostname>`.
   This plan adds `homelab-render package --name <pkg> --out DIR`, which writes
   `DIR/packages/<pkg>/root/` and `nfpm.yaml`. Resolution: amend C1. Applied: C1's command
   list names `homelab-render package --name PKG --out DIR`.
4. **C6 equality, and what `land.sh` waits for.** C6 says land.sh waits for
   `homelab_host_applied_info` showing the merge SHA. With publish-on-change, a later merge
   can supersede it before the host updates. This plan accepts the merge SHA or any
   descendant, and also requires `homelab_host_reconcile_ok` at 1. It takes "the hosts the PR
   touched" from a render diff that runs only after master CI passes on the merge commit, and
   ends `deferred` for a host whose next updater fire is past its wait. Resolution: amend C6.
   Applied: C6 accepts M or a descendant of M with `homelab_host_reconcile_ok` at 1.
5. **C1's placeholder and escape rules cover cluster mode only.** C1 states them for every
   render. Host mode renders no secret and keeps `${` literal (*Host mode renders no secret*).
   Resolution: amend the C1 *Secret references* row to say "cluster mode". Applied: the row
   reads *Secret references, cluster mode only*.
6. **Who runs the Pi's interim Ansible step?** H3.8a needs one apply, which decrypts, so the
   operator runs it. H4's last PR retires the three interim files. Resolution: none needed
   beyond the PR text.
7. **C5's version counts `origin/master`.** The builder builds the newest green commit,
   which can be older than the tip. A count taken from the tip gives a parent and its tip
   equal numbers, and dpkg then orders them by SHA. This plan counts the built commit:
   `0.<git rev-list --count --first-parent <chosen>>+g<12-hex sha of <chosen>>`
   (*Package rules*). Resolution: applied on 2026-10-15. The C5 *Version* row counts the
   built commit, and contracts.md's *Applied requests (2026-10-15)* records it, which clears
   H3.5a's gate.
8. **Which form does H3.5c take, and how does M6 learn of it?** H3 waits only on R1, and M6
   on M2 to M5, so the slice index orders neither against the other. H3.5c's row, tile and
   token are outside M6's baseline counts. Two fixes were weighed:
    - **(a) A sub-edge** that puts H3.5c before M6's A1 shadow PR. M6's inventory is then
      fixed. The cost is that M6's Phase A, and through A6-7a all of Phases B and C, would
      wait on H3.5a's **operator-only** trust choice (Open question 1).
    - **(b) Two forms, the default here.** H3.5c adds the bridge row while M6's A1 shadow
      PR has not merged, and two alert rules with C7's labels once it has (*Steps*,
      H3.5c). Neither slice waits on the other. M6's A1 batch moves the row when the row
      exists, and M6's planned bijection test fails A6-7a on a row that A1 missed.
   Resolution: (b), with the gate in H3.5c's step. No slice-index edge is requested. The
   round i1 cross-slice note asks M6 to name the row, its push token and its
   `EXPORTER_DEPENDENT` entry in A1's batch and in its counts, for the case where the row
   is on master when A1's shadow PR opens. H7.2a's drift-count message extends whichever
   form is on master. The host arms left H3.5c in the 2026-10-12 fold: H3.5d ships them as
   rules that wait on M1, so they are outside this question.
9. **Where does the `arm64` CI job run?** H3.11's install case needs an `arm64` userland.
   Two routes exist: GitHub's hosted `arm64` Linux runner, or QEMU user-mode emulation on
   the `amd64` runner. This plan cannot confirm from the tree that the hosted label is
   available to this repository. Recommendation: the hosted runner, because emulation
   slows the systemd container and can hide a fault that real `arm64` hardware shows.
   Resolution: H3.11's PR runs the job on the hosted label first. If GitHub refuses the
   label, the job uses QEMU, and the PR body records the run time.
10. **Should the canary gate cover the shared packages?** C5's staged rollout names host
    packages. `homelab-updater` and `homelab-tools` publish in one suite, so every host
    installs a published version at its next fire. Gating them would need a per-host pin
    on each shared version, which the updater's install of `homelab-updater` by name does
    not allow. Recommendation: leave them outside the gate. The container suite runs
    every `homelab-updater` change on both releases before it publishes, and a
    `homelab-tools` change is a pin bump with its own `arm64` and `amd64` install cases.
    Resolution: the H4 owner confirms before `homelab-daniel-server` first publishes,
    because a second host takes the shared packages only from then. **Ruled 2026-10-16:
    outside the gate.** The operator, who owns H4, confirmed the recommendation.

## Review log

### Round 1 (per-slice adversarial review)

Raised: 1 critical, 7 high, 17 medium. All 25 were checked against the tree at `70591c307`,
and all 25 held. All are fixed, and none was refuted. Where the fix differs from the
reviewer's suggested remedy, the reason is given.

- **H3-R1-01 (critical) H3.4 edits `initial_setup.yml`, which parks the deployer.** Fixed.
  Confirmed by `deploy_changes.py:_BROAD_MANUAL_PREFIXES` and the `deploy_defer.py` docstring.
  H3.4 is split. H3.4a carries only the entry and a disarmed skeleton, which parks the
  deployer. Its operator-only clear is the ff-merge plus a playbook run that applies an empty
  arm. Role-body PRs then land through the tick. The false sentence is gone.
- **H3-R1-02 (high) C1's placeholder and `$${` rules applied to host output.** Fixed. Host
  mode removes secret key names and renders strict, so a secret read fails the render. It
  keeps `${` literal and shares no R2 code. The C1 amendment is *Open questions* 5. The lint
  is part of the render rather than a text scan, because a key-name grep would flag `domain`
  in ordinary prose.
- **H3-R1-03 (high) `domain` left in both heartbeat scripts.** Fixed. A join of the
  templates' names against the `secrets.yml` key names finds exactly `domain`,
  `pi_sd_health_push_token` and `pi_recovery_push_token`. #5087 has since deleted `domain`
  from `secrets.yml`, so the join finds only the two push tokens. H3.8a stages those two, H3.8b
  reads them at run time, and H3.8b renders `domain` from inventory.
- **H3-R1-04 (high) land finishes a `hosts/`-only PR as `nothing-to-deploy`.** Fixed.
  Confirmed by `classify.py:shortcut_if_nothing`. A second early finish was found while
  fixing it: `deploy.py:deploy_phase` calls `deploy.py:no_tag_outcome`, which is `NoReturn`,
  whenever no tag resolved. `_step_resolve` computes host targets before the shortcut, both
  early finishes read them, and the wait is a `_STEPS` entry before `health_verdict.health`.
  A land test requires a hosts-only PR to run every step and end `settled`.
- **H3-R1-05 (high) Reconcile failure invisible to the applied-SHA metric.** Fixed. The
  updater runs reconcile synchronously and writes the applied metric only on success. It adds
  `homelab_host_reconcile_ok`, which land requires. Failure class 2 is in *Risks*.
- **H3-R1-06 (high) The recovery disarm never stops the running timer.** Fixed. Reconcile
  step 1 also stops a `homelab-*` timer whose enable link was dropped. The rule is scoped to
  timers so it never stops a timer-activated service mid-run. The same rule makes the H3.9
  rollback work. The container suite proves the disarm. Dropping the timer unit itself was
  the alternative, and it was not taken, because the enable-link rule was needed anyway.
- **H3-R1-07 (high) The land wait polls a loopback that answers on one node only.** Fixed.
  The wait queries loopback, then `observability_prometheus_cluster_ip`, the otelq order.
  `prom_endpoint`, the transport `probe.py metric` uses, was rejected because it calls
  `core.py:sops_extract('domain')`, and the agent user has no age key. The operator's ruling
  of 2026-10-14 removes that blocker once domain PR 1 of R2's *Publish `domain`* item lands.
  It has landed, and #5087 deleted `domain` from `secrets.yml`. A refused transport reads as
  not settled, with its own message and test.
- **H3-R1-08 (high) The rollback cannot run, and it deletes the signing key.** Fixed. The
  role gets its own arm flag, `homelab_builder_enabled`, in its defaults. A flip there is a
  path under `roles/setup/homelab_builder/`, and `deploy_changes.py:setup_tags_for` maps it to
  the role's tag once `tick_applies_setup_role` accepts the role, so the tick runs the
  teardown arm. A `host_vars` flag was rejected: an inventory change is deploy-plane, and its
  route to a setup role is unverified.
  The teardown keeps the signer's GnuPG home. Rotation is in `docs/break-glass.md`.
- **H3-R1-09 (medium) The host-package tests gate nothing.** Fixed. The checks are a `ci.yml`
  job that the `prek` gate reads. They inherit the SHA-keyed concurrency group and join
  `KNOWN_JOBS`. The job renders on every run instead of using a path filter.
- **H3-R1-10 (medium) `optimize_pi` changes leave `manual_plane` lines.** Fixed. H3.6 and
  H3.8a give the `clear-owed --applied` command after the apply. H3.8b states that it clears
  with no apply.
- **H3-R1-11 (medium) The deletion list is wrong, and `User=` is unstated.** Fixed. The
  eleven tasks are listed by name. The services run as `sys_user`, and `docker` comes from the
  supplementary group. The package takes over the directories and the logrotate file.
- **H3-R1-12 (medium) Component variable resolution is unstated.** Fixed. The five defaults
  move into `component.yml` `defaults:`, layered beneath `group_vars` by a `render_context`
  keyword, never as `overrides`. A render test resolves each variable.
- **H3-R1-13 (medium) `_role_tasks.py` is k8s-only.** Fixed. The census uses
  `_role_census.py:task_files_by_role` with `_helpers.walk_tasks`, and names the fields it
  extracts.
- **H3-R1-14 (medium) The censuses assert members that do not exist yet.** Fixed. Named
  members are staged by the PR that creates them: H3.2, H3.6 and H3.8b.
- **H3-R1-15 (medium) No test holds host scripts to Python 3.12.** Fixed. The baseline is
  corrected. The container suite runs reconcile under 3.12, and an `ast` syntax check with a
  red-proof pair backs it.
- **H3-R1-16 (medium) The builder needs the repo's uv environment.** Fixed in H3.5a: uv,
  `host_python_version`, a frozen environment and a named cache. H3.5 is split in two to stay
  near 200 lines.
- **H3-R1-17 (medium) No agent can run the Pi steps.** Fixed. Every Pi command is marked
  operator-run, and the agent checks go to Prometheus, the heartbeat tiles included through
  Kuma's `monitor_status`. The suggested `probe.py metric` was rejected for the transport
  reason in H3-R1-07. `probe.py monitors` moved to the operator for the same reason: it reads
  through `prom_endpoint` and `core.py:k8s_endpoint`.
- **H3-R1-18 (medium) No host-plane hold.** Fixed. A failed reconcile holds that host until
  the operator clears it. A masked timer and a builder hold file are the manual levers.
- **H3-R1-19 (medium) Reconcile runs inside the dpkg transaction.** Fixed. Reconcile takes
  the dpkg frontend lock before it reads the manifest, and the updater starts it
  synchronously. The updater and reconcile services are `manual`. The container suite covers
  the restart case. A postinst branch that skips the start under the updater was rejected,
  because it breaks the systemctl-only whitelist.
- **H3-R1-20 (medium) A half-installed package is not repaired.** Fixed. Updater step 4
  requires `ii ` for both packages, and otherwise reinstalls.
- **H3-R1-21 (medium) Unscoped `dpkg --verify` and an unbounded full update.** Fixed. The
  verify is scoped to the two packages. The full update runs at most daily and never between
  04:00 and 05:59. Both paths are measured in H3.6.
- **H3-R1-22 (medium) The Pi schedule conflicts with the quiet window and the land wait.**
  Fixed. The schedule is a host-package drop-in that skips 23:00 to 00:59 and 04:00 to 05:59.
  The daily fallback is 00:30. Land derives its deadline from the schedule, and ends
  `deferred` when the next fire is too far.
- **H3-R1-23 (medium) The trust anchor comes from the candidate, and the key holder runs
  master's code.** Fixed. The keyring ships in the role's `files/`. Signing moves to
  `homelab-signer`, which runs only the role's script. A red proof uses a tip signed by a key
  outside the keyring.
- **H3-R1-24 (medium) A missing token falls back to the anonymous quota.** Fixed. The builder
  exits 1 before any request without a token, and caches final verdicts. A test covers both.
- **H3-R1-25 (medium) Docs missing from the same-PR list.** Fixed. Added
  `docs/homelab-render.md` (H3.1), the land skill and root `CLAUDE.md` (H3.9), the
  `docs/host-packages.md` runbook (H3.5b) and the `docs/gitops-pipeline.md` note (H3.5b).

### Round 2 (integration review)

Raised: 1 high and 1 medium that name H3 among their targets. Both were checked against the
tree at `70591c307` and against the H4 and H5 plans. Both held for the slices they name.
H3's part of each is fixed, and none was refuted. The parts other slices own are their own.

- **X-09 (high) Unit-restart ownership contradicts itself across H3, H4 and H5.** Fixed in
  H3's text. Three statements in H3 disagreed with H4's section *The restart contract*:
    - *Ordering on the secrets unit* said `homelab-secrets` restarts its dependents. It now
      says reconcile is the only process that restarts one, that order comes from
      reconcile's sequence of calls, and that `After=` orders only the boot path and a
      `Wants=` pull-in. The `Requires=` argument is kept, because reconcile still restarts
      `homelab-secrets` on every change to its inputs hash.
    - Reconcile step 3 keeps `try-restart` for H3 and now states that H4.1b replaces it with
      `reset-failed` and then `restart` for an enabled or active unit. The two rules act
      alike on H3's own units, because each H3 service is `manual` or timer-activated with
      no enable link. The reconcile red proof notes the H4.1b change, which H4's tests list
      already carries. Step 2 now defines an enabled `homelab-*` unit as one whose enable
      link the manifest ships, which is H4's definition.
    - *Package rules* said `preinst` runs `dpkg-divert --add` lines from H4 on. H4's
      round-1 finding H4-R1-04 dropped both diversions, because `dpkg-query` lists both
      files as nut-client conffiles. H4's design reaches them through a `NUT_CONFPATH`
      drop-in instead. The whitelist now admits no `dpkg-divert` line. It names H7's
      *In-place edits* as the first slice that diverts a file, which must define the
      lifecycle and widen the lint. H4's and H7's sides are in this round's cross-slice
      notes.
- **X-18 (medium) H4 extends a `host-packages.yml` workflow that H3 does not create.** Fixed
  on H3's side. H3 already put the checks in a `host_packages` job in `ci.yml`. The tree at
  `70591c307` holds only `ci.yml`, `image-smoke.yml` and `renovate-config-canary.yml` under
  `.github/workflows/`. `gh run list --workflow host-packages.yml --limit 1` printed
  `HTTP 404: workflow host-packages.yml not found on the default branch`. *CI* now states
  the job's display name, `host_packages`. It also gives the `gh run list --workflow ci.yml`
  and `gh run view --json jobs` filter a later slice verifies with. The job does not exist
  yet, so the filter was proved on an existing job instead. On master's latest `ci.yml`
  run, `gh run view 38167395325 --json jobs --jq '.jobs[] | select(.name |
  test("ansible.lint")) | [.name, .conclusion] | @tsv'` printed `ansible-lint` and
  `success`, tab-separated. That shows a `.jobs[].name` filter works on a `ci.yml` run. The
  H4 edit is H4's.

### Round 10 (per-slice adversarial review)

Raised: 3 high and 14 medium. All 17 were checked against the tree at `70591c307` and held.
All are fixed, and none was refuted. Where the fix differs from the reviewer's remedy, the
reason is given.

- **H3-R10-01 (high) Reconcile step 3 restarted changed timers that were neither enabled
  nor active.** Fixed. Step 3 now reads `LoadState` first and never touches a masked unit,
  then acts on a changed unit only when it is enabled (the manifest ships its link) or
  active. That is H4's predicate. Services keep `try-restart`, so H4.1b changes only the
  verb. The "act alike" sentence is restricted to services. A first run with no
  `applied.json` treats every hash as changed, which is the case that keeps the H3.6 Pi
  package's timer inactive. New reconcile red proofs cover the inactive unlinked timer, the
  disarmed recovery timer and the masked timer. A container case and an H3.6 operator check
  cover the hand-installed Pi package.
- **H3-R10-02 (high) Reconcile's result was not recorded per version.** Fixed. `applied.json`
  now carries a record of the two package versions reconcile read under the lock, whether
  the run succeeded, and `last_ok`. Every updater path, the common path and the hold path
  included, derives `reconcile_ok`, `applied_info` and the hold from that record. The
  updater's `TimeoutStartSec` is 50 minutes, derived from reconcile's 30 plus the lock wait
  and the fetch, and a test holds the inequality.
    - The reviewer offered two options. The plan advances per-entry state only for actions
      that succeeded, rather than writing `applied.json` only on full success. A failed
      restart keeps its old hash, so `--clear-hold` retries it, and a restart that succeeded
      is not repeated. Writing only on full success would repeat every restart on each
      retry, including H5's 15-minute `homelab-compose` restart.
    - One nuance in the evidence: H4 does not assume the opposite throughout. H4's *The
      restart contract* says that H3's step 6 writes `applied.json` after a failed step, and
      builds `pending.json` on that. H4's "never writes `applied.json`" line is about a
      killed run. The hole the finding names was real all the same: before H4.1b, and on the
      updater's common path, which read no result.
    - The version and SHA stay out of `manifest.json`, because a field that changes on every
      commit would make every render differ and defeat publish-on-change.
- **H3-R10-03 (high) H3.9 rendered the merge commit's code before master CI.** Fixed, without
  overriding the marker. Confirmed: `pipeline.py:_STEPS` runs `_step_resolve` before
  `_step_ci`, and `deploy_cross_role.py:tables_in` carries `DECIDED parse the copy rather
  than exec it`. Resolve now sets `ln.host_candidate` by a path test against the renderer's
  `HOST_INPUTS` and runs nothing. `_step_ci` renders only after `ci.wait_master_ci` returns
  green, in `tools.py:gate_snapshot` worktrees that set `UV_PROJECT_ENVIRONMENT`.
    - `HOST_INPUTS` adds two inputs the finding's list missed: `ansible/vars/secrets.yml`,
      whose key names host mode removes, and `kuma-push-lib.sh`, which H3.8b copies. An audit
      test holds the constant to every file a host render opens, with named members.
    - When the render finds no host, `_step_ci` re-applies the shortcut, so a path-only false
      positive still ends `nothing-to-deploy`. A land test proves no renderer runs before the
      CI step.
- **H3-R10-04 (medium) The builder gated on the tip only.** Fixed. The builder walks
  first-parent from the tip back to the last built commit, bounded by
  `gitops_deploy_ci_ancestor_walk_max` as `deploy_phases.py:_newest_green_ancestor` is, and
  builds the newest green commit. The trust check applies to that commit. The walk is never
  anonymous, because the builder exits before any request without a token.
- **H3-R10-05 (medium) The memory fallback was likely to trigger, and observation 4 had no
  route under it.** Fixed. The threshold is now the re-measured `apt-get -d dist-upgrade`
  cgroup peak, taken with the same tool as the updater's paths, because the 57 MB baseline is
  a process RSS. H3.6 also measures the common path. The candidate lookup reads the homelab
  `Packages` list with awk and loads no apt cache, and a container case checks that
  `pkgcache.bin` is unchanged. A scan that finds no `Version:` for either package fails the
  run, so a missing list never reads as "nothing newer" behind an advancing timestamp. Exit observation 4 and *Risks* state the daily-schedule route:
  the host wait queries once first, so the operator starts the updater and re-runs `land.sh`.
- **H3-R10-06 (medium) The hand ff-merge that clears H3.4a's park could strand other
  merges.** Fixed. Confirmed by the comment in `health_verdict.py:health`. The operator step
  now starts with `git log` and `git diff --name-only` over `HEAD..origin/master`, and either
  waits for a window with H3.4a alone or lists and applies each other change.
  `docs/host-packages.md` carries the same check for a later park.
- **H3-R10-07 (medium) A held host, a failing signer and a stale updater paged nobody.**
  Fixed. PR H3.5c adds a monitor-bridge row, `host_packages`, gated on Prometheus as
  `pi_pressure` is. It pages on `reconcile_ok` 0 and on a stale or absent success timestamp.
  Its limits are tested against each host's rendered schedule. The signer gains `OnFailure=`
  through `alert_unit.yml`. The success timestamp is redefined as a freshness signal that the
  common path advances. The monitor-bridge `CLAUDE.md` makes folding into an existing monitor
  the default. The row is new because `gitops_status` carries no Prometheus gate, so a
  Prometheus-sourced arm there would page on every Prometheus outage.
- **H3-R10-08 (medium) The killed-pull kill point depended on timing.** Fixed. In CI, a
  64 MiB fixture payload's `.dpkg-new` file marks the unpack, and the case asserts that
  `dpkg --audit` lists the package before the next run. On the Pi, the operator kills when
  `pgrep -f 'dpkg .*-[-]unpack'` matches, and an attempt counts only when `dpkg --audit`
  lists the package. Exit observation 3 now says so.
- **H3-R10-09 (medium) The CI container assumed systemd, python3 and a hostname.** Fixed.
  The job builds its image from a digest-pinned base, installing `systemd`, `systemd-sysv`
  and `python3` from Ubuntu's snapshot archive at a pinned timestamp, and records the
  versions. It boots with `--hostname daniel-pi` and asserts `python3 --version` prints 3.12
  first. Every host package carries `Depends: python3 (>= 3.12)`.
- **H3-R10-10 (medium) H3.9 item 3 contradicted itself.** Fixed. Confirmed by
  `deploy.py:no_tag_outcome` and the "services deployed" wording in
  `health_verdict.py:health`. The rule is now: `no_tag_outcome` runs when there is no tag and
  either no host target or a non-empty plane. It names the hosts it did not wait on. `health`
  words its verdicts from what the landing applied. A land test covers a `hosts/` plus
  manual-plane PR.
- **H3-R10-11 (medium) The unit validator never saw a `homelab-*` unit.** Fixed. Confirmed by
  `unit_templates.py:discover_templates` and both prek selectors. H3.2c extends the validator
  to every unit and drop-in the host renderer emits, rather than a `.j2` glob, because a
  static unit file would escape a glob. It names `homelab-updater.timer` as a member. H3.8b
  widens the shell hook's selector. The failure-class-4 entry is corrected.
- **H3-R10-12 (medium) The disarm walkthrough in `docs/pi-host-tuning-record.md` went wrong
  at H3.8b.** Fixed. H3.8b's same-PR list gains that page: *Disarming it* moves to the
  package path and checks with `systemctl list-timers homelab-pi-recovery.timer`, and the
  heartbeat descriptions name the timers.
- **H3-R10-13 (medium) H3.5a omitted the deployer's cross-role tables.** Fixed. H3.5a adds
  `homelab_builder` to `_ALERT_UNIT` and `_HOST_LIB`, and states that its tick also applies
  `gitops_deploy`. One consequence the finding did not name: `_STAMPED` is `_HOST_LIB` plus
  three roles, so the role must also import `stamp_deployed.yml` and `stamp_render.yml`, or
  the equality test still fails. H3.5a says so.
- **H3-R10-14 (medium) `bootstrap-host.sh` types daniel-box's address outside the guard.**
  Fixed with the guard. The script must stay self-contained to run on a blank host, so H3.6
  adds `hosts` to `SCAN_ROOTS` with an allowlist row and a test that the URL equals
  daniel-box's `server_ip`. Adding `hosts` alone would scan nothing, because
  `SCAN_SUFFIXES` lacks `.sh`, so `.sh` joins it. `git grep` found no `.sh` or `.sh.j2` file
  typing a guarded address, so the wider scan starts green.
- **H3-R10-15 (medium) H3.6's bootstrap steps depended on H7's packages.** Fixed. H3.6 now
  spells out its three steps under H7's numbers: the hostname check; the keyring checked
  against the pinned fingerprint and `homelab.sources`; and the install of `homelab-updater`
  and `homelab-<hostname>`. H7 adds `homelab-repos` and `homelab-tools`. The C5 name for
  `homelab-tools` is a cross-slice note.
- **H3-R10-16 (medium) Several PRs exceeded the 100 to 200 line guide.** Fixed. H3.2 is
  H3.2a (updater), H3.2b (reconcile) and H3.2c (censuses and the unit validator). H3.3 is
  H3.3a (job skeleton) and H3.3b (container cases). H3.9 is H3.9a (the module, unwired) and
  H3.9b (the wiring and the arm). Each has its rollback line. The group names stay valid for
  the other slices that cite them.
- **H3-R10-17 (medium) Reconcile held dpkg's lock across every restart.** Fixed. Reconcile
  holds the lock only while it reads the manifest, its state and both packages' versions, and
  releases it before step 1. The record makes that safe: a transaction that lands while
  reconcile acts shows as a version mismatch, and the updater runs reconcile again. Updater
  step 2 runs only when `dpkg --audit` lists a package, and exits 75 when another apt process
  holds the lock. A container case covers that exit.

### Round c1 (per-slice adversarial review)

Raised: 1 high and 5 medium. All 6 were checked against the tree at `70591c307` and held.
All are fixed, and none was refuted. Where the fix differs from the reviewer's remedy, the
reason is given.

- **H3-C1-01 (high) The version took the tip's commit count while the builder built an older
  green commit.** Fixed. Confirmed: the comment on `gitops_deploy_ci_ancestor_walk_max` in
  `ansible/roles/setup/gitops_deploy/defaults/main.yml` records a p25 merge gap of 88s
  against a ~103s CI sweep, so a pending tip over a green parent is common. *Package rules*
  now counts the built commit with `git rev-list --count --first-parent <chosen>`, and
  builder step 7 builds with that version. Step 4 builds only a strict first-parent
  descendant of the last built commit, so counts increase strictly. A builder test covers
  the parent's own count and strict ordering under `dpkg --compare-versions`. *Open
  questions* 7 carries the C5 amendment, and the contracts.md change is a cross-slice note.
- **H3-C1-02 (medium) Nothing created the textfile directory on the Pi.** Fixed. `git grep
  node-exporter-textfile -- ansible scripts` matched only the DaemonSet hostPath, which is
  `DirectoryOrCreate`, and `claude-cgroup-metrics.sh`. The owner differs from the reviewer's
  suggestion. M3's *The textfile directory's owner* and M6's B5 already fix the mode at root,
  group `sys_user`, 2775, set by Ansible, which a package shipping it root 0755 would
  contradict. A package-owned directory would also become a second writer once H6 installs
  `homelab-updater` on daniel-box, where M3's task writes the same path. H3.6 therefore adds
  that task to `optimize_pi` under `node-exporter-host`, which the operator's H3.6 step 1
  already applies. *Shared names* states the owner per host and M3's write rule. The updater
  exits non-zero when the directory is missing, writes the file 0644, and never creates the
  directory. The container image creates it, a container case asserts `homelab_host.prom`
  exists after the first run, and an H3.6 step reads the series and
  `node_textfile_scrape_error` through Prometheus.
- **H3-C1-03 (medium) The host_packages row's absent arm was not exporter-dependent.** Fixed.
  Confirmed by `gates.py:EXPORTER_DEPENDENT` and the `node-pi` job comment in
  `ansible/roles/k8s/observability/templates/prometheus.yaml.j2`. The Pi entry goes in H3.9c, the PR that lists daniel-pi, not H3.9b,
  because the flapping starts there. The builder arms added for H3-C1-06 read daniel-box's
  exporter, so H3.5c adds `host_packages` under `node` too. A red/green pair covers each
  entry. *Risks* states that the suppression covers the whole row.
- **H3-C1-04 (medium) Nothing installed H3.9b's timer link on the Pi.** Fixed with both of
  the reviewer's options:
    - H3.9b gains an operator-only hand start after the builder publishes, with an `active`
      check and a re-run of the same `land.sh` command.
    - H3.9a item 5 now reads the enable link from the first parent's render, not the merge
      commit's. The landing therefore ends `deferred` at once, naming the start command,
      rather than waiting about 40 minutes for fires that cannot happen.
    - daniel-pi's limit moves to a new PR, H3.9c, merged after the timer reads `active`. The
      staleness arm then never compares against a days-old hand-started run.
- **H3-C1-05 (medium) The shipped homelab.sources replaced the test's local source.** Fixed.
  The test image adds a local `dpkg-divert --no-rename` of `homelab.sources` to a path
  outside `sources.list.d/`, before it writes its own deb822 source with `Trusted: yes`.
  The source must be deb822, because updater step 3 reads only `homelab.sources`. The
  keyring needs no diversion, because the test source names no `Signed-By:`. The diversion
  sits in the image, outside the shipped maintainer scripts the lint reads. Every case
  asserts that the local server logged its `Release` fetch and that no list names
  daniel-box's address.
- **H3-C1-06 (medium) A stalled builder paged nobody.** Fixed.
    - The signal differs from the reviewer's "considered tip lags `origin/master`". With a
      renamed CI context, the builder considers the tip and builds nothing, so that lag
      would read 0. The builder now reports the age of the oldest unbuilt first-parent
      commit, and the time of its last successful fetch.
    - The `host_packages` row pages when either passes its bound.
    - A trust refusal and a non-descending tip now exit non-zero. A walk with no green
      commit stays at exit 0, because a pending tip is normal.
    - `alert_unit.yml` posts once per failure, and the builder fires every 2 minutes. It
      therefore exits non-zero once for each reason and tip, and the row carries a
      condition that persists.
    - The hold moved after the fetch, so a held builder still reports its lag.
    - Neither builder user can write the textfile directory: the kubelet's root 0755, or
      M3's root:`sys_user` 2775. The signer's `homelab_repo.prom` had the same gap. Both
      units now copy their file through the role's `publish-textfile` script, run as root
      by `ExecStopPost=-+`, which keeps only that unit's series prefix.
    - The runbook gains three builder procedures: the last-built reset, the keyring
      replacement and the CI context match.

### Round c2 (per-slice adversarial review)

Raised: 1 high and 5 medium. All 6 were checked against the tree at `70591c307` and held.
All are fixed, and none was refuted. Where the fix differs from the reviewer's remedy, the
reason is given.

- **H3-C2-01 (high) Several PRs depended on the archive keyring before any step created or
  committed it.** Fixed. Confirmed in this plan: H3.2a's package named the keyring, the key
  existed only after H3.4b's apply, and no PR committed its export.
    - The fix differs from the reviewer's CI-generated throwaway. A throwaway in the CI job
      would not cover the other renders before the key exists: the prek unit validator
      (H3.2c) and the shared-package tests also render `homelab-updater`. Instead the
      keyring entry and the key land together in a new PR, H3.4c, so no render or build
      before it names a missing file, and no placeholder ever exists.
    - H3.4c commits the operator's export of the public key. It merges after H3.4b's apply
      and before H3.5a, the first PR whose builder renders the package.
    - The role writes the signing key's fingerprint to `/etc/homelab-builder/archive-key.fpr`
      (H3.5a), because the builder cannot read the signer's 0700 GnuPG home. Builder step
      6 refuses a render whose keyring is absent or carries another fingerprint, with
      `reason="keyring"`. The check sits in the builder rather than the signer, because
      the signer runs on every 2-minute `OnSuccess=` and would post about 30 alerts an hour.
      The builder's once-per-condition exit and the bridge's unbuilt-since arm already
      carry a persistent refusal. A red/green pair covers it.
    - The fingerprint pin moved to H3.6, which creates `bootstrap-host.sh`, with a test that
      holds the pin equal to the committed key.
    - *File tree*, *Rollback* (H3.4c fails closed once H3.5a is in), *Risks* and the
      same-PR lists match.
- **H3-C2-02 (medium) A failed homelab fetch left a stale list that read as "nothing
  newer".** Fixed. Confirmed: updater step 3 had no failure arm, and step 4's guard fires
  only on a list with no `Version:` at all. Checking the exit status alone would not fix
  it, because apt treats a failed fetch as transient. Step 3, and bootstrap step 5, now run
  `apt-get update --error-on=any`, which `man apt-get` for apt 2.8.3 documents as "Fail the
  update command if any error occured, even a transient one". A non-zero exit ends the run
  with no metric, no hold and `last_success` unadvanced. A stub test (H3.2a) and a
  container case that stops the local server (H3.3b) are the red proofs. The other
  container cases are the green half. *Risks* names the path under failure class 3.
- **H3-C2-03 (medium) `docs/host-packages.md` had no mkdocs nav entry.** Fixed. Confirmed
  by the `ENFORCED by scripts/tests/test_mkdocs_config.py` comment in `mkdocs.yml` and
  `scripts/tests/test_mkdocs_config.py:test_every_doc_is_either_in_the_nav_or_explicitly_excluded`.
  No `exclude_docs` pattern matches the page. The runbook is now created in H3.4b (see
  H3-C2-05), so the nav entry goes there, beside `The GitOps pipeline:
  gitops-pipeline.md`. *File tree* lists `mkdocs.yml` and the page.
- **H3-C2-04 (medium) The H3.10 canary was never armed.** Fixed. H3.10a ships
  `homelab-pi-canary.service`, the timer and its `timers.target.wants/` link. An operator
  check reads the timer `active` and the H3.10a landing `settled` before H3.10b merges. The
  naming census gains the pair in H3.10a and drops it in H3.10b, or it would assert
  members that no longer exist.
- **H3-C2-05 (medium) A failed tick apply of `homelab_builder` held every landing.** Fixed.
  Confirmed by the FORWARD-ONLY rule in `ansible/roles/setup/gitops_deploy/CLAUDE.md` and
  *Which apply clears a hold* in `docs/gitops-pipeline.md`. *Risks* gains the entry.
  *Rollback* gives the clear path: a covering apply from a fix or disarm PR, or an
  operator-only hand apply of every plane `hold_plane` names, followed by `clear-hold`.
  The clear path does not hard-code `homelab_builder`, because H3.5a's tick also applies
  `gitops_deploy`. The runbook moved from H3.5b to H3.4b, the first PR whose tick apply
  can fail, so the procedure exists before it is needed. H3.4b, H3.5a and H3.5b merge in
  attended windows.
- **H3-C2-06 (medium) H3.8b removed `optimize_pi` sections that `docs/facts.lock`
  records.** Fixed. `docs/facts.lock` records five sections of that `CLAUDE.md`. H3.8b's
  same-PR list now runs `fact_status.py forget` for the removed contract section and
  `verify` for each changed one. It also covers three more breaks of the same kind:
    - the `# fact:` marker in
      `ansible/tests/setup/test_pi_recovery_restarts_and_reports.py:test_a_dead_container_is_restarted`
      that names the removed section;
    - the regenerated `## At a glance` block;
    - `ansible/tests/setup/test_setup_cron_roles_have_a_contract.py:test_optimize_pi_is_not_exempt`,
      which would pass vacuously once the role installs no cron.

  H3.9b re-verifies `CLAUDE.md#After a PR Merges — Pull, Deploy, Verify` when its new line
  cites anything.

### Round integration-2 (integration review)

Raised against H3: 2 high and 1 medium. Each was checked against the tree at `70591c307`
and against the H7, D1 and contracts.md texts. All three held for H3's part. H3's part of
each is fixed, and none was refuted. The parts H7, D1 and contracts.md own are in this
round's cross-slice notes.

- **X2-02 (high) H7 moves and then deletes the CI-context list that D1's `promote.py
  decide` reads by path, and never repoints promote.** Fixed on H3's side. Confirmed:
  `gitops_deploy_ci_contexts` exists only in
  `ansible/roles/setup/gitops_deploy/defaults/main.yml`, and H7's *Design* deletes those
  defaults at H7.20a. H3's builder named the list but never said how it read it.
    - The builder now reads `CI_CONTEXTS` and the walk bound from its own `config.env`,
      which the role renders from the two `gitops_deploy` defaults. That is the deployer's
      own pattern, and it is not a path read of the old file.
    - A role that reads another role's default is a row of
      `deploy_cross_role.py:SETUP_FILES_SHIPPED_BY_OTHER_ROLES`. The docstring of
      `ansible/tests/setup/test_setup_cross_role_files.py` says its own census holds those
      rows (#4303, #4357), and the `claude_code` and `k3s` defaults rows are the
      precedent. H3.5a adds the `gitops_deploy` defaults row naming `homelab_builder`, so a
      change to either key re-applies the builder's config.
    - `deploy_git.py:ci_verdict` returns `pass` for an empty set. The builder now refuses
      an empty list or a non-positive bound with `reason="ci"`, as
      `await_ci.py:DisarmedGateError` does. A red/green pair covers it. *Metrics*, the
      bridge arms, the runbook pages, *Tests and docs*, *Risks* and *Complexity delta*
      match.
    - The new section *What H7 inherits from this slice* states that H7's key move repoints
      the builder's template in the same PR and drops the cross-role row, and gives the
      grep that lists the builder's reads. The repoint must land before H7.14, whose
      package config cannot read a role default.
- **X2-05 (high) H7's move of the builder and the Pi node exporter drops H3's fingerprint
  file, publish-textfile hook and Pi textfile directory.** Fixed on H3's side. Confirmed:
  `grep -c` printed 0 for `archive-key.fpr`, `publish-textfile` and
  `node-exporter-textfile` in `h7-delete-ansible.md`. H3's *Shared names* said only that
  H7 moves the units. It also said that no package ships the textfile directory, and named
  no successor for the `optimize_pi` task that H7 retires.
    - *What H7 inherits from this slice* lists every role-held piece the H7.14 move must
      carry: the `archive-key.fpr` writer, which a break-glass key restore also runs;
      `publish-textfile` with both `ExecStopPost=-+` lines; the once-per-condition failure
      record; the keyring, `config.env`, users, ufw rule and alert imports.
    - It also requires a successor creator for the Pi's textfile directory at root,
      `sys_user`, 2775, and a fresh-Pi check that reads
      `homelab_host_applied_info{host="daniel-pi"}` and `homelab_host_reconcile_ok` at 1.
    - *Shared names* points both the builder units and the directory at that section.
- **X2-11 (medium) The register and slice index lack about 20 requested rows.** Fixed on
  H3's side. Confirmed: contracts.md's *Requested amendments not yet applied* lists the C5
  trust row but not H3 Open question 7's C5 *Version* row, and C5 still reads
  `0.<git rev-list --count origin/master>`. H3.5a's step now names both C5 rows as merge
  gates. Open questions 3, 4 and 5 were checked against contracts.md and are applied: C1
  names `homelab-render package`, the *Secret references* row says cluster mode only, and
  C6 accepts a descendant of M. Each now says so. The register row itself is contracts.md's.

### Round i1 (per-slice adversarial review)

Raised: 1 high and 1 medium. Both were checked against the tree at `70591c307` and against
the M6, H4 and H7 texts. Both held, and both are fixed. None was refuted.

- **H3-I1-01 (high) H3.5c adds a monitor-bridge row, a Kuma tile and a push token, with no
  dependency edge to M6, which retires Kuma and counts the bridge rows at the baseline.**
  Fixed. Confirmed: `check_table.py:CHECKS` has 44 rows at `70591c307`, and
  `gates.py:EXPORTER_DEPENDENT` keys only `node` and `node-pi`. M6's *What M2 to M5 leave*
  counts 44 rows and 44 bridge tokens, and its header says it unblocks nothing.
  contracts.md's slice index gives H3 only R1 and M6 only M2 to M5, so either order can
  happen. One part of the evidence did not hold: no H4 text adds daniel-server to the
  expected-host map. A grep of `host/` outside H3 for `host_update`, `update_max` and
  `EXPORTER_DEP` printed nothing. H7.2a does extend the check's message with a drift count.
    - The fix takes the finding's option (b). H3.5c takes the row form while M6's A1 shadow
      PR has not merged, and the rule form once it has. The rule form is four alert rules
      with C7's `service` and `severity` labels and an `up` clause in place of the two
      `EXPORTER_DEPENDENT` entries, with no tile or token. A row-form PR that meets A1's
      shadow PR at its rebase switches form. M6's planned bijection test fails A6-7a on a
      row that A1 missed.
    - Option (a), a sub-edge that puts H3.5c before M6's A1, was not taken. Through A6-7a it
      would make every later M6 phase wait on H3.5a's **operator-only** trust choice.
      *Open questions* 8 records both options.
    - H3.9c extends whichever form is on master. *Host-plane holds and stop levers*,
      *Verification*, *Rollback*, *Risks*, *Tests and docs* and *Complexity delta* name the
      rule form. The cross-slice notes ask M6 to list the row, its token and its two
      `EXPORTER_DEPENDENT` entries in A1, and ask H7 to write H7.2a's drift count for
      either form.
- **H3-I1-02 (medium) After H7.2e moves the CI keys into `group_vars`, nothing re-applies
  the builder's `config.env`, and *What H7 inherits* does not say so.** Fixed. Confirmed:
  `deploy_changes.py:_BROAD_DEPLOY_PREFIXES` lists `ansible/inventory/`, so the deployer
  applies a `group_vars/all.yml` change as `deploy.yml`. The module docstring of
  `scripts/deploy_tools/narrow_lib/foreign_keys.py` follows a changed role default outward
  to the inventory keys that carry it. Nothing routes an inventory edit to a setup role.
    - *What H7 inherits from this slice* now says the route ends at H7.2e and returns at
      H7.14, when a key change alters the host render. Between those two PRs, a stale
      `config.env` fails closed.
    - H7.2e must ship a replacement with a red/green test. One option is a land or tick
      output that lists `homelab_builder` as owed on a change to either key. The fallback
      is an **operator-only** runbook step that hand-applies `homelab_builder`. The same PR
      repoints the CI-context procedure in `docs/host-packages.md` to
      `ci_required_contexts`. Builder step 4 now says the route lasts only until H7.2e.

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review
round covers it.

- **F-04, the trust anchor.** Builder step 5 states the C5 anchor, a commit on
  `origin/master` with a green CI verdict, and checks no signature. *Open questions* 1 is
  an **operator-only** ADR before H3.5a, with options (a) master plus green CI, (b) a
  signed landing tag and (c) a Pi-only approval gate, and recommends (a). *Risks* records
  the Pi's first unattended path from master to root. The trusted merge-signing keyring
  left *Shared names*, H3.5a, the file tree, *What H7 inherits*, *Verification* and the
  runbook list.
- **F-07, signing.** *The builder and the repository* gains *Sandboxing* with named
  directives and the signer's refusal rule. *Verification* gains red proofs for both.
  *Risks* states that builder code decides what the signer is handed, and drops the
  wording that overstated the isolation. H3.5a gains the **operator-only** reach check,
  whose commands are in *Verification*.
- **S11, `Valid-Until`.** The signer writes it seven days ahead and re-signs daily,
  triggered through the builder's `OnSuccess=`. *The builder and the repository* gains
  the per-host procedure for a revoked archive key, and the H3.5b runbook lists it.
- **F-14, staged rollout and holds.** *Staged rollout* names daniel-pi as the canary and
  records the reasoning. PR H3.12 adds the gate and the `release` override. The updater
  gains `--clear-hold --install-newest`. PR H3.5d ships `HostReconcileFailed` and
  `HostUpdateStale` and waits on M1. H3.5c keeps only the builder arms, and every
  bridge page for `reconcile_ok` or host staleness names the rules instead. H3.9b waits on
  H3.5d. *Open questions* 10 defers gating the shared packages.
- **F-15, releases and architectures.** The container suite is a matrix over
  `ubuntu:24.04` and `ubuntu:26.04`. PR H3.11 builds `homelab-tools` for both
  architectures, with the Pi as the `arm64` pilot. The `arm64` runner went to *Open
  questions* 9.
- **P7.** *Shared names* records why `bootstrap-host.sh` types the address. *Risks*
  accepts that daniel-box holds the builder, the signer and the repository.
- **Note from the M1 owner.** H3.5d states how the host rules meet M1's convention 5: the
  `node` and `node-pi` jobs carry `origin`, no rule pages on `up`, and every absent arm
  is conjoined with `up == 1`. *Shared names*, Metrics, states H3's series delta against
  the 140,000 budget, and *Complexity delta* carries the figure.
- **Note from the H4, H6 and H7 owner.** H3.6's bootstrap text credits H3.11, not H7.1,
  with `homelab-tools`. *`homelab-tools`* names `/usr/lib/homelab/bin/sops`, and H3.11
  names itself as the PR that H4.2 (`amd64`) and H4.5 (`arm64`) wait on.
