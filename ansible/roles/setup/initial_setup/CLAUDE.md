# initial_setup — host baseline + security hardening

The host-bring-up role: `ansible-core` and `ansible-lint`, the pinned collections, the absent arms
of retired crons, and the setup-drift check. The host packages own the whole baseline on every
host (sshd, fail2ban, unattended upgrades, sysctls, the journald cap and the rsyslog filter, the
sudo cache, auditd, the kernel modules, the banners, the base packages, the maintenance jobs, the
integrity scans and the firewall), with step 4 of `hosts/bootstrap-host.sh` for the swap, the
`ssh-users` group and the networkd-dispatcher mask (H7.58 to H7.60). The cluster hosts' packages
and the dotfiles repo own the dev tooling, Vale and the worktree holder scan (H7.61, H7.62). This
role keeps no task for any of them, and no setup apply upgrades a host's packages.
`files/worktree_holders.py` stays here as the component's input.
**Not a container role** — a host-setup role under `ansible/roles/setup/`, run by
`initial_setup.yml`, not `deploy.yml`. It is a large setup role, so **`--check` first** and scope
with `--tags` when iterating. `docs/host-baseline-record.md` carries the per-block traps, the two
host checks' reasoning, and the journald-cap forensics.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "initial_setup"`
- **Timer:** `kuma-check-setup-drift.timer` (`OnCalendar=*-*-* 08:20:00`)
- **Packaged timers:** `homelab-aide-check.timer` (`OnCalendar=Mon *-*-* 03:00:00`) on
  daniel-box, daniel-pi, daniel-server; `homelab-apt-autoremove.timer` (`OnCalendar=Sun *-*-*
  02:00:00`) on daniel-box, daniel-pi, daniel-server; `homelab-docker-prune.timer`
  (`OnCalendar=*-*-* 06:30:00`) on daniel-pi; `homelab-dpkg-purge.timer` (`OnCalendar=Sun *-*-*
  02:15:00`) on daniel-box, daniel-pi, daniel-server; `homelab-firmware-update.timer`
  (`OnCalendar=Sun *-*-* 07:00:00`) on daniel-box; `homelab-reboot.timer` (`OnCalendar=Sun
  *-*-* 07:30:00 UTC`) on daniel-box, daniel-pi, daniel-server; `homelab-rkhunter-check.timer`
  (`OnCalendar=Wed *-*-* 02:00:00`) on daniel-box, daniel-pi, daniel-server
<!-- /generated_from -->

## Where it runs
In `ansible/initial_setup.yml`, after [[config_files]] and before [[docker_install]], on
every host — Pi-specific tasks self-guard. Its `collections` tag installs the pinned Ansible
collections from `ansible/requirements.yml`, the tag the deployer applies a bump with.

## Hardware gates are capability flags, not host names
A hardware gate names the FACT it reads, not the host that has it, so replacing a machine is a
`host_vars` edit (#2981). No task here gates on hardware since H7.60. A new flag defaults false
in `group_vars/all.yml` and is true in every carrying host's `host_vars`, because
`scripts/deploy_tools/setup_routing.py` resolves a gate against those two files only, and an
unknown name there leaves the role unplaced.
`scripts/deploy_tools/setup_lib/setup_gates.py:_eval_when` also reads the role's defaults, but
only when its caller passes them.

A new gate that reads HOST HISTORY keeps a host literal behind a `DECIDED:` comment, because no
capability names it, and it deletes itself once the host converges. ENFORCED:
`ansible/tests/setup/test_initial_setup_host_gates.py::test_a_surviving_host_literal_is_one_of_the_recorded_deliberate_ones`
refuses a new bare literal.

## Granular tags (run one block without the whole role)
Every task carries a block tag placed right under `name:`, so `--tags setup_drift` or
`--tags "tooling,collections"` runs just that slice. The live list is the tree's, not a copy here:

```bash
grep -rho 'tags: \[.*\]' ansible/roles/setup/initial_setup/tasks/ | sort -u
```

The record page says what the non-obvious ones cover. No task is reachable only through
`crons`: each also has a subject tag.
**Fact-dependency rule:** a task whose `register:` feeds other blocks carries ALL its
consumers' tags, or a tag-scoped run dies on an undefined variable.
`docs/host-baseline-record.md`'s *Tooling, pins and the root HOME trap* has the ENFORCED test.

## Rules the task files do not state
`tasks/` is the inventory — one file per subject, and the tag list above says which file a tag
runs. These two bite from outside the file you are editing; the rest, with every derivation,
are in the record page.

- **The journal cap and the rsyslog info filter are one change.** The cap ships in
  `hosts/components/host-tuning/templates/50-homelab-journald.conf`. The filter is the
  `rsyslog-filter` component's, and every host whose `has_rsyslog` is true lists it. The flag is
  false on the Pi. ENFORCED:
  `ansible/tests/setup/test_journald_syslog_forwarding.py::test_raising_the_forwarding_level_requires_the_syslog_filter`,
  `ansible/tests/setup/test_journald_syslog_forwarding.py::test_the_filter_component_lists_exactly_on_hosts_with_rsyslog`.
- **Handlers live in `handlers/main.yml`**, and a `notify:` from any role in the play can reach
  them. Each handler needs a notifier. ENFORCED:
  `ansible/tests/setup/test_setup_handlers_resolve.py::test_every_notify_in_initial_setup_resolves_to_a_handler`,
  `ansible/tests/setup/test_setup_handlers_resolve.py::test_every_handler_in_the_play_has_a_notifier`.

## No state-changing job remains here
The weekly reboot, the firmware update, the Docker image prune, the apt autoremove and dpkg
purge, and the rkhunter and AIDE scans are timers of the `host-maintenance` and
`integrity-scans` components on all three hosts (H7.30 to H7.32). H7.36 moved the docs refresh,
the infrastructure-map refresh, the eval sweep, the object-store repair and the secret-rotation
timers into components under `hosts/components/`. Each component's `component.yml` carries its
own contract, and renders this role's templates and files in place. The fail2ban, sshd, sysctl,
unattended-upgrades and ufw tasks were deleted with the last host's move, so a template here
that no task names may still be a component's input: grep `hosts/` before you delete one.
This role keeps the setup-drift check, which is read-only. The `loki-route-witness` component is
the Loki read-route witness on both cluster nodes. Before you add a state-changing cron, put it
in a component.

## Notable
**The setup-plane drift reader (`setup_drift`) lives here rather than in monitor-bridge**, a
`kuma-check-*` timer that reruns on failure.
The reader runs on each host in `setup_drift_check_hosts`, and only daniel-server's verdict pages.
A change to that list routes to no setup role, so it takes a hand `--tags setup_drift` apply on
the host it adds or drops. That apply removes a dropped host's timer, units, script, `.prom` file
and render fragment.

**The five scripts of M6's batch B2 write a textfile and push to nothing.** They are
docs-refresh, the eval sweep, the secret-rotation audit, the setup-drift check and the witness.
Each calls `write_check_textfile` from `ansible/roles/setup/initial_setup/files/homelab-check-lib.sh`
with a `DEADLINE_S` sized to the check's schedule, and the rules in
`ansible/roles/k8s/observability/files/rules/initial-setup-checks.yaml` read the files and page.
B2's Cutover-2 removed the pushes and their credentials.
ENFORCED: `ansible/tests/services/test_initial_setup_check_rule_literals.py` pins each rule's
deadline literals to the rendered `DEADLINE_S`. The weekly AIDE and rkhunter scans write a
textfile the same way on every host (#6168), and the `host-checks` rule group reads it.
ENFORCED: `ansible/tests/setup/test_integrity_scan_rule_literals.py`. The apt autoremove,
dpkg purge and firmware update jobs run through `files/host-maintenance.sh`, which writes
each job's exit code as a textfile verdict, and the `host-checks` group reads it with a
`Failed` and a `NotReporting` rule per host that runs the job (#6179). ENFORCED:
`ansible/tests/setup/test_host_maintenance.py`. The witness gets its
textfile directory from a tmpfiles.d line of its component, which gives the deploy user write access.
