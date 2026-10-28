# initial_setup — host baseline + security hardening

The big host-bring-up role: base packages, per-user Python tooling, SSH/firewall/kernel
hardening, auditing, and file-integrity monitoring. **Not a container role** — a host-setup
role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`. It is the
largest and most fragile setup role, so **`--check` first** and scope with `--tags` when
iterating. `docs/host-baseline-record.md` carries the per-block traps, the two host checks in
full, and the journald-cap forensics.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "initial_setup"`
- **Crons (7):**
  - `Weekly apt autoremove` — `0 2 * * 0`
  - `Weekly dpkg purge orphaned configs` — `15 2 * * 0`
  - `Weekly system restart` — `30 7 * * 0`
  - `Weekly firmware update` — `0 7 * * 0`
  - `Clean unused Docker images` — `30 6 * * *`
  - `Weekly rkhunter malware scan` — `0 2 * * 3`
  - `Weekly AIDE file integrity check` — `0 3 * * 1`
- **Timers (2):** `kuma-check-setup-drift.timer` (`OnCalendar=*-*-* 08:20:00`),
  `kuma-check-loki-read-route.timer` (`OnCalendar=*-*-* *:23:00`)
<!-- /generated_from -->

## Where it runs
In `ansible/initial_setup.yml`, after [[config_files]] and before [[sops_setup]] /
[[docker_install]], on every host — Pi-specific tasks self-guard.

## Hardware gates are capability flags, not host names
A hardware gate names the FACT it reads, not the host that has it —
`ansible/inventory/group_vars/all.yml:has_low_memory_board`,
`ansible/inventory/group_vars/all.yml:has_raspi_kernel`,
`ansible/inventory/group_vars/all.yml:has_ample_ram`. Each defaults false there and is true in
every carrying host's `host_vars` (#2981), so replacing a machine is a `host_vars` edit. They
sit in `group_vars` because `scripts/deploy_tools/setup_lib/setup_gates.py:_eval_when` resolves a gate
against those two files only, and an unknown name reads as every host.

Three gates keep a host literal behind a `DECIDED:` comment, because they read HOST HISTORY
that no capability names, and they delete themselves once the host converges. ENFORCED:
`ansible/tests/setup/test_initial_setup_host_gates.py::test_a_surviving_host_literal_is_one_of_the_recorded_deliberate_ones`
refuses a new bare literal.

## Granular tags (run one block without the whole role)
Every task carries a block tag placed right under `name:`, so `--tags fail2ban` or
`--tags "ssh,firewall"` runs just that slice. The live list is the tree's, not a copy here:

```bash
grep -rho 'tags: \[.*\]' ansible/roles/setup/initial_setup/tasks/ | sort -u
```

The record page says what the non-obvious ones cover. No task is reachable only through
`crons`: each also has a subject tag.
**Fact-dependency rule:** a task whose `register:` feeds other blocks carries ALL its
consumers' tags, or a tag-scoped run dies on an undefined variable.
`docs/host-baseline-record.md`'s *Tooling, pins and the root HOME trap* has the home-dir
resolver example and the ENFORCED test.

## Rules the task files do not state
`tasks/` is the inventory — one file per subject, and the tag list above says which file a tag
runs. These three bite from outside the file you are editing; the rest, with every derivation,
are in the record page.

- **A sysctl this role sets that UFW's `/etc/ufw/sysctl.conf` also names must be set there
  too**, or the run ends with UFW's value live (#2977).
- **The `journald` tag's two files are one change** — the journal cap and the rsyslog info
  filter, gated `when: has_rsyslog` (false on the Pi). ENFORCED:
  `ansible/tests/setup/test_journald_syslog_forwarding.py::test_raising_the_forwarding_level_requires_the_syslog_filter`.
- **Handlers live in `handlers/main.yml`**; its header says why the order there matters. The
  two optimize_pi notifies went with that role's tasks in H7.25. ENFORCED:
  `ansible/tests/setup/test_setup_handlers_resolve.py::test_every_notify_in_initial_setup_resolves_to_a_handler`.

## Autonomous-role contract — the host crons that remain (`crons` tag)
The weekly reboot, the weekly firmware update, the Docker image prune, the weekly apt autoremove
and dpkg purge, and the rkhunter and AIDE scans run with no human in the loop. H7.36 moved the
docs refresh, the infrastructure-map refresh, the eval sweep, the object-store repair and the
secret-rotation timers into components under `hosts/components/` (`docs-refresh/`, `infra-map/`,
`eval-sweep/`, `worktree-sweep/`, `secret-rotation/`). Each component's `component.yml` carries
its own contract. This role keeps the Loki read-route witness for daniel-server
(`initial_setup_packaged_hosts` in `defaults/main.yml` excludes daniel-box) and the setup-drift
check.
- **Scope:** reboot the host, stage firmware, prune unused Docker images older than a week,
  remove orphaned packages and configs, and scan. None edits the repository or the cluster.
- **Mode:** the reboot, firmware and prune jobs follow `weekly_reboot_*` and `has_docker`; a
  host without the capability loses the job on the next apply.
- **Abort valves:** a failed prune leaves its Healthchecks.io ping unsent, and the reboot
  chain reboots even when its ping fails, by design.
- **Required evidence:** a `weekly-restart`, `fwupd-auto` or `docker-prune` line in the journal,
  the Healthchecks.io check for the reboot and the prune, and the `apt-autoremove`,
  `dpkg-purge` and `firmware-update` textfiles for the three maintenance jobs.
- **Next-run review:** before adding a state-changing cron here, check whether a component
  under `hosts/components/` should own it instead.

## Notable
**Two host checks live here rather than in monitor-bridge**, both `kuma-check-*` timers that
rerun on failure: the setup-plane drift reader (`setup_drift`) and the Loki read-route witness
(`loki_route_witness`).

**The five scripts of M6's batch B2 write a textfile beside their Kuma push.** They are
docs-refresh, the eval sweep, the secret-rotation audit, the setup-drift check and the witness.
Each calls `write_check_textfile` from `ansible/roles/setup/initial_setup/files/homelab-check-lib.sh`
with a `DEADLINE_S` that equals its Kuma tile's interval, and the rules in
`ansible/roles/k8s/observability/files/rules/initial-setup-checks.yaml` read the files in shadow.
ENFORCED: `ansible/tests/services/test_initial_setup_check_rule_literals.py` pins each rule's
deadline literals to the rendered `DEADLINE_S`. The weekly AIDE and rkhunter scans write a
textfile the same way on every host (#6168), and the `host-checks` rule group reads it.
ENFORCED: `ansible/tests/setup/test_integrity_scan_rule_literals.py`. The apt autoremove,
dpkg purge and firmware update crons run through `files/host-maintenance.sh`, which writes
each job's exit code as a textfile verdict, and the `host-checks` group reads it with a
`Failed` and a `NotReporting` rule per host that runs the job (#6179). ENFORCED:
`ansible/tests/setup/test_host_maintenance.py`. The witness gets its
textfile directory from a task tagged `loki_route_witness`, which gives the deploy user write access on daniel-server.
