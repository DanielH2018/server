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
- **Crons (13):**
  - `Weekly apt autoremove` — `0 2 * * 0`
  - `Weekly dpkg purge orphaned configs` — `15 2 * * 0`
  - `Weekly secret rotation (auto tier)` — `0 9 * * 0`
  - `Weekly system restart` — `30 7 * * 0`
  - `Weekly firmware update` — `0 7 * * 0`
  - `Clean unused Docker images` — `30 6 * * *`
  - `Clear ansible log file` — `0 6 * * 0`
  - `Weekly git object-store repair` — `20 4 * * 0`
  - `Refresh homelab infrastructure map` — `*/15 * * * *`
  - `Refresh generated docs` — `17 6,18 * * *`
  - `Homelab eval sweep` — `0 2 * * 0`
  - `Weekly rkhunter malware scan` — `0 2 * * 3`
  - `Weekly AIDE file integrity check` — `0 3 * * 1`
- **Timers (3):** `kuma-check-secret-rotation-audit.timer` (`OnCalendar=*-*-* 08:00:00`),
  `kuma-check-setup-drift.timer` (`OnCalendar=*-*-* 07:50:00`),
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
sit in `group_vars` because `scripts/deploy_tools/land_reach.py:_eval_when` resolves a gate
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
consumers' tags (the home-dir resolver is `[tooling, git-hooks]`), or a tag-scoped run dies on
an undefined variable. The GitOps deployer derives those tags and applies them unattended
since #3120, so the rule is checked rather than stated; the record page names the check.

## Rules the task files do not state
`tasks/` is the inventory — one file per subject, and the tag list above says which file a tag
runs. These three bite from outside the file you are editing; the rest, with every derivation,
are in the record page — including the one about `~` being `/root` under this play's `become`.

- **A sysctl this role sets that UFW's `/etc/ufw/sysctl.conf` also names must be set there
  too**, or the run ends with UFW's value live (#2977).
- **The `journald` tag's two files are one change** — the journal cap and the rsyslog info
  filter, gated `when: has_rsyslog` (false on the Pi). ENFORCED:
  `ansible/tests/setup/test_journald_syslog_forwarding.py::test_raising_the_forwarding_level_requires_the_syslog_filter`.
- **Handlers live in the playbook**, so a new `notify:` needs one added to `initial_setup.yml`;
  `Restart rsyslog` sits ABOVE `Restart systemd-journald` because handlers fire in definition
  order.

## Autonomous-role contract — Generated docs refresh (`crons` tag)
Twice daily (06:17 and 18:17, daniel-box only): `docs-refresh.sh.j2` regenerates the reference
pages and the infra map, weights any unweighted test module, rebuilds the site, and publishes
the diff through a PR that auto-merges.
- **Scope:** three derived paths and nothing else — `docs/reference/`,
  `docs/assets/generated/` and `scripts/dev/pytest_shard_weights.json`. Each is reproducible by
  a tested generator, which is what makes a review-free auto-merge acceptable; a write outside
  the three parks every deploy on the box.
- **Mode:** degrade, never abort. A failing generator sets `GENERATORS_OK=0`, publishes what
  succeeded and reports DOWN. A red test suite is not a degradation case — the script runs the
  suite before its commit and exits 1.
- **Abort valves:** the shared `/var/lock/server-git-tree.lock`, the dirty-tree gate, the
  unlanded-branch guard, and the commit-failure stamp under `/var/lib/homelab/docs-refresh.d`.
- **Required evidence:** `status=<up|down> <msg>` in the log and a push to "Docs Refresh".
  `PUSH_STATUS` defaults to `down`, so a later path that forgets to set it reports a failure.
- **Next-run review:** before adding a fourth staged path, check no prek hook matching it can
  fail on generator output — one that can wedges this cron. `vale` can, on purpose (#3008).

## Autonomous-role contract — Homelab eval sweep (`evals` tag)
Weekly (Sunday 02:00, daniel-box only): grades every case under `evals/cases/`, rolls the result
into `evals/history.json`, then publishes it.
- **Scope:** run the existing cases and commit the trended result. Never edits a case, never
  touches an agent or skill definition, never runs the `run-live.mjs` live-smoke path.
- **Mode:** binary, no dial. Either the API key exists and the sweep runs hermetic, or it is
  empty and the run is a no-op reporting UP with the reason logged.
- **Authoritative source:** the chezmoi eval engine's own grading, never a cached report.
- **Abort valves:** the shared git-tree lock, which throws a result away rather than record it
  against a tree it did not grade; the unlanded-branch guard; the empty-key gate above.
- **Required evidence:** `status=<up|down> <msg>` in the log and, when armed, a push to "Homelab
  Evals". A REGRESSED case is still committed — the data is real — and still reports DOWN.
- **Next-run review:** before widening scope, read the last few `evals-history/*` PRs this cron
  opened for what regressed or flaked.

## Notable
**Two daily host checks live here rather than in monitor-bridge**, both `kuma-check-*` timers
that rerun on failure: the setup-plane drift reader (`setup_drift`) and the Loki read-route
witness (`loki_route_witness`). Each pushes a per-host token in `CROSS_HOST_PUSH_TOKENS`.
