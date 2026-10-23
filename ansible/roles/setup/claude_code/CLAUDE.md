# `setup/claude_code` — Claude Code install + the Remote Control host

Four things live here: the **install** (native installer, per-user, auto-updating),
**`claude-rc.service`**, the Remote Control host that lets sessions be created from a phone,
**`claude-memory-sync.timer`**, which copies daniel-box's Claude memory store to daniel-server,
and **the artifacts node's two crons**, the peer artifact sync and the OTLP-door heartbeat.

Runs on every host with `has_claude_code: true` (daniel-box and daniel-server, with per-host cap
numbers in host_vars). The unit is enabled only where `claude_code_rc_enabled` is also true
(daniel-box). Invoked from `initial_setup.yml`, **not** `deploy.yml` — the role is not in
`containers_list`, so `./scripts/deploy.sh --tags claude_code` exits 2 on an unmatched tag:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags claude_code
```

`docs/claude-code-rc-caps.md` holds the record behind the rules below.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "claude_code"` when `has_claude_code`
- **Crons (2):**
  - `Sync peer Claude artifacts` — `*/5 * * * *`
  - `Claude Code telemetry health` — `*/10 * * * *`
- **Timers (7):** `claude-cgroup-metrics.timer` (`OnBootSec=30s`, `OnUnitActiveSec=30s`),
  `claude-clone-sync.timer` (`OnBootSec=10min`, `OnUnitActiveSec=15min`),
  `claude-dotfiles-sync.timer` (`OnBootSec=10min`, `OnUnitActiveSec=15min`),
  `claude-fanout-server-lander.timer` (`OnBootSec=5min`, `OnUnitActiveSec=10min`),
  `claude-memory-sync.timer` (`OnBootSec=5min`, `OnUnitActiveSec=15min`),
  `claude-rc-restart.timer` (`OnCalendar=weekly`), `claude-triage-poll.timer`
  (`OnBootSec=5min`, `OnUnitActiveSec=2min`)
<!-- /generated_from -->

## The two Remote Control modes are different features

`/remote-control` **inside a session** publishes that session to the phone. `claude rc` **from a
shell** spawns sessions **on demand** up to `--capacity`, so only it lets the phone create one.

## Activating it

`claude_code_rc_enabled: true` enables and starts the host; back to `false` stops **and** disables
it and its restart timer, which is the rollback. `ansible/tests/setup/test_claude_rc_unit.py` pins both directions.

One prerequisite Ansible cannot check, the trust dialog, is in `docs/claude-code-rc-caps.md`. **Re-run that check after a Claude Code upgrade or spawn-mode change.**

**Stop any hand-run host before deploying**: it and the service compete for one account and
directory.

## What monitoring does and does not cover

`OnFailure=claude-rc-alert.service` pages Discord when the host **crashes**, reusing the shared
`gitops_deploy_discord_webhook`.

- **That value reaches the alert unit through an `EnvironmentFile=`, never the unit body.**
  `ansible/roles/setup/gitops_deploy/tests/test_systemd_unit_secrets.py` holds the whole repo to
  that shape. `no_log: true` hides an undefined-variable failure, so if the task fails
  opaquely, check `gitops_deploy_discord_webhook` is in scope.
- **It pages a login dead at start, not one that dies mid-run**, which leaves the host
  `active`. `docs/claude-code-rc-caps.md` has why (#4618).

## Traps already paid for

`ansible/tests/setup/test_claude_rc_unit.py` enforces every unit-shape rule here;
`ansible/tests/setup/test_claude_login_slice_caps.py` and
`ansible/tests/setup/test_claude_fleet_slice_cap.py` enforce the two slice planes.

- **`--spawn=` must be passed explicitly**, or Claude Code asks on stdin and the host hangs,
  never connected, while the unit reads `active`.
- **`PATH` must name `/usr/local/bin`**, as with cron: without it a spawned session loses `kubectl`
  and `uv` and reports an **empty cluster** rather than failing.
- **No `MemoryMax`**: systemd applies it to the whole cgroup, so one runaway session takes the
  OOM kill for every session.
- **`claude_code_rc_capacity` bounds session count, not memory**, and `MemoryHigh` throttles rather
  than caps. `claude_code_rc_memory_swap_max` is the ceiling, and **0 is wrong**.
- **`claude_code_rc_pytest_workers` caps one pytest run's fan-out, not the number of runs**, via
  `PYTEST_XDIST_AUTO_NUM_WORKERS` in the unit. **chezmoi's `settings.json` carries
  the same variable** for both users, so keep the two equal.
- **A session started with `claude agents` reads none of the unit's directives**: it lands in
  `user-<uid>.slice`. `login-slice-caps.conf.j2` and `pytest-fanout-cap.conf.j2` carry
  the unit's caps there for each uid in `claude_code_login_uids` and the agent's. A uid that
  leaves the list loses both; `claude_code_login_caps_enabled: false` removes all.
- **The background-shell pressure reaper is off** (`claude_code_rc_disable_bg_shell_pressure_reap`):
  Node reads the **cgroup**, so `free -m` reads clean while it kills backgrounded Bash tasks.
- **The weekly restart uses `try-restart`**, which never starts a host kept stopped on purpose.

## The fleet bound, and why it lives on `user.slice`

`claude_code_fleet_memory_high` / `claude_code_fleet_swap_max` bound both Claude cgroups on
`user.slice`, their shared parent; `claude_code_fleet_caps_enabled: false` removes both.
`docs/claude-code-rc-caps.md` has the derivation. **The `Slice=` line takes effect at the next
start**, so the deploy that changes it drops the RC host's sessions.

## The agent user

`tasks/agent.yml` builds one agent user per `claude_code_agents` entry; the include maps each
profile field (`ansible/filter_plugins/claude_agents.py`) onto the `claude_code_agent_*` name
the tasks read. `docs/claude-agent-user.md` has the rest. `claude` lands a PR through
`claude-land@<n>.service`.

`claude-triage@<episode>.service` runs one unattended, diagnose-only session as `claude` and
leaves a report (`tasks/triage.yml`, shipped off behind `claude_code_triage_enabled`).
`claude-triage-poll.timer` starts one when a monitor goes down, and ships in `shadow` mode,
which only logs. The task file, templates, runner and poller are on the lander's approval
list, and the caps and the poll mode are pinned block vars. `docs/claude-agent-user.md`,
*Alert-triggered triage*, has the design.

`claude_code_user` (default `sys_user`) is the account `claude-rc.service` runs as; its home and
`claude_code_rc_workdir` follow it, and the unit is sandboxed only when it is not `sys_user`.

## The artifacts node's two crons

`Sync peer Claude artifacts` and `Claude Code telemetry health` run only on
`claude_code_artifacts_node` (daniel-box); every task in `tasks/node_crons.yml` skips on
daniel-server. H2.7 moved them here from the k8s artifacts and observability roles under the
same cron file names and script paths. `ansible/roles/k8s/artifacts/CLAUDE.md` documents how
the sync behaves, and the heartbeat's template carries why it is a host cron at all.

- **Every key the k8s roles also read has a twin there.** `claude_code_artifacts_node`,
  `claude_code_artifacts_peer_sources`, `claude_code_artifacts_peer_dir`,
  `claude_code_artifacts_stars_file`, `claude_code_otlp_host_ip` and `claude_code_otlp_port`
  are each held equal to the k8s key by
  `ansible/tests/setup/test_claude_code_artifacts_node_crons.py`. A change edits both roles and
  needs both applies. One key in `group_vars/all.yml` would route as a deploy-plane change and
  never re-apply this role.
- **A push-token rotation is manual.** `artifacts_sync_push_token` and
  `claude_otel_push_token` are in `CROSS_HOST_PUSH_TOKENS`
  (`scripts/secrets_mgmt/consumers.py`), because no deploy tag re-renders these scripts.
- **No task here notifies `Restart claude-rc`**, so an apply that changes only these crons
  leaves every Remote Control session running.
- **Each cron also writes a check file** to `/var/lib/node-exporter-textfile/`,
  `sync-artifacts.prom` and `claude-otel-health.prom`, through `write_check_textfile` in
  `homelab-check-lib.sh` (M6 batch B6). The `ArtifactsSync*` and `OtlpDoor*` rules in
  observability's `host-checks.yaml` page them in shadow beside the Kuma tiles until their
  cutover. Each script renders its deadline from its own cron minute, so that minute must stay
  `*/N`, and `ansible/tests/services/test_b6a_rule_literals.py` pins the rules to it. The
  directory task in `tasks/node_crons.yml` sets the same owner, group and mode as the k3s
  role's disk-health task, so the crons do not wait on a hand apply of `k3s-bringup.yml`.

## Autonomous-role contract (`sync-artifacts`, a mirror of another host's tree)

- **Scope:** `rsync --delete` of each `claude_code_artifacts_peer_sources` entry's `path` into
  `claude_code_artifacts_peer_dir/<name>` on daniel-box, then a copy of
  `claude_code_artifacts_stars_file` to the peer's `stars_path`. It deletes on daniel-box only
  what the peer has already deleted.
- **Mode:** always on where it is installed; removing the entry from
  `claude_code_artifacts_peer_sources` (and its twin) stops that peer's sync.
- **Abort valve:** a `flock` holds runs to one at a time, and ssh runs in `BatchMode` with a
  10-second connect timeout, so an unreachable peer fails the run rather than hanging it.
- **Evidence:** `journalctl -t sync-artifacts`, the `Artifacts Peer Sync` Kuma tile, which
  goes DOWN at `claude_code_artifacts_sync_alert_after_failures` consecutive failures, and
  `sync-artifacts.prom`, which carries the same verdict for the shadow rules.

## Autonomous-role contract (`claude-memory-sync` overwrites a store on another host)

- **Scope:** `rsync --delete` of `claude_code_memory_sync_dir` (follows `claude_code_user`) to
  `claude_code_memory_sync_target_dir` (the operator's store)
  on `claude_code_memory_sync_target`, one-way. A memory a daniel-server session writes is lost
  at the next run, by the operator's decision (#3123); `docs/claude-memory-sync.md` has why.
- **Mode:** `claude_code_memory_sync_enabled`, true only in daniel-box's host_vars; false
  stops the timer and removes its units.
- **Abort valve:** the unit skips unless the source `MEMORY.md` is non-empty.
- **Evidence:** `journalctl -u claude-memory-sync` lists each file a run changed or deleted;
  a failure pages Discord.

## Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)

- **Scope:** one episode per run. Slice 1 diagnoses only: Edit, Write, a landing and a push
  are denied. The later slices widen this to a fix landed through `claude-land@` and a
  restart through an allowlisted broker, under the operator's 2026-10-15 rulings.
- **Mode:** `claude_code_triage_enabled`, false by default; false stops the poller, then removes
  the rule, the units, the code and the config, and keeps the records.
  `claude_code_triage_poll_mode` is `off`, `shadow` or `armed`, pinned at `shadow` in
  `tasks/triage.yml`, so arming the trigger needs the operator's approval.
- **Abort valves:** `$10`, 60 turns and a 30-minute wall clock per run, and six runs a UTC day,
  all pinned in `tasks/triage.yml`, plus a lock that holds runs to one at a time. The runner
  enforces the daily cap, because polkit lets `claude` start a run without the poller. The
  poller opens an episode only on a fall that holds for two polls, makes one storm episode of
  more than three falls at once, and opens nothing while `DeployHoldActive` fires or the weekly
  reboot's mute is open. It queues episodes and starts the next only when the lock is free,
  and never restarts one whose run crashed.
- **Evidence:** `/var/lib/claude/triage/runs/<episode>.json`, the report under
  `/a/<host>-claude/`, and a Discord page for every run; a crash pages separately, and a
  poller failure pages at most once an hour. In shadow
  mode, `/var/lib/claude/triage/shadow.jsonl` and `journalctl -u claude-triage-poll` list each
  run the poller would have started.
- **Next-run review:** before widening the scope or a cap, read the last week's records for
  what the runs diagnosed and what they cost.

## Autonomous-role contract (`claude-clone-sync`, `claude-dotfiles-sync`)

- **Scope:** per agent: a `--ff-only` pull of its clone, then the venv and
  collections if their locks moved; and a pull and `chezmoi apply` of the operator's dotfiles.
- **Mode:** the agent's `state` and `dotfiles`; off removes its `<name>-*-sync` units.
- **Abort valve:** the clone sync skips unless the clone is a clean `master`; the dotfiles sync
  refuses a source without the agent variant.
- **Evidence:** `journalctl -u <name>-{clone,dotfiles}-sync`; a failure pages Discord.

## Autonomous-role contract (`claude-fanout-server-lander`, a landing with no session behind it)

Issue #5496. A daniel-server fan-out batch stops at its PR, and this timer lands the PRs that
qualify. The operator approved it on 2026-10-18. `claude-land@` cannot do it: that unit admits
only the `claude` user and pins the author and branch prefix of the agent's PRs, while a server
batch runs as `ubuntu` on `worktree-fanout-<batch>`.

- **Scope:** every 10 minutes, `fanout.py land-server` reads the review records of the last 48
  hours from daniel-server in one ssh call, reads the repo's open PRs in one `gh` call, and
  starts `land.sh --pr <n> --arm-merge --await-merge --detach` for each PR that passes
  `scripts/dev/fanout_lib/autoland.py:problems`. That means a batch that ran its review phase,
  ended `pr`, passed its CI gate and left no finding, whose PR is open, not a draft, still at
  the head the review read, with no unchecked box under an operator or before-merge heading and
  no `needs input` or `Do not merge` text. A `--no-review` batch writes no record, so it never
  lands. Neither does a PR touching the landing path's code (`scripts/deploy_tools/`, `scripts/lib/`), the
  fan-out tooling, the deployer, `ansible/vars/`, `.github/`, `.claude/` or this role. Each landing runs from its own `origin/master` worktree under
  `~/.local/state/fanout-server-lander/checkouts/`, never from the primary checkout.
- **Primary:** the unit's working directory and `--primary` are `claude_code_operator_checkout`,
  which `ubuntu` owns. They are never `claude_code_rc_workdir`, the agent's clone on daniel-box:
  git refuses that clone as dubious ownership, and `ubuntu` cannot write to it (#5861).
- **Mode:** `claude_code_fanout_server_lander_enabled`, true only in daniel-box's host_vars.
  Setting it to false and applying the role stops the timer and removes the units, and every
  server batch stops at its PR again. `fanout.py land-server --dry-run` judges and logs without
  starting anything.
- **Abort valves:** at most 2 landings start per run (#5526: CI demand already exceeds the
  runner cap). A PR is started once: a log in `~/.local/state/fanout-server-lander/logs/`, or
  a live `land.py --pr <n>` process anywhere, skips it. `land.sh` holds no per-PR lock, so
  this check is the only guard against a second landing. The unit sets
  `LAND_REQUIRE_BRANCH_PREFIX`, which makes `land.sh` refuse while the deployer holds a SHA and
  pin the merge to the head it checked, and `LAND_REQUIRE_AUTHOR`.
- **Evidence:** `journalctl -u claude-fanout-server-lander` has one line per candidate PR,
  `eligible` or `ineligible` with the reason. Each landing's own `VERDICT:` is in its
  `land<pr>-*.log`, and `cc-wait land <pr> --log-dir ~/.local/state/fanout-server-lander/logs`
  waits on it. An unreadable daniel-server logs a line and exits 0, because the host is off for
  maintenance; an unreadable GitHub exits 1 and pages. A pass that read both also writes
  `homelab_awaiting_operator{reason}` and its `_oldest_seconds` twin to
  `/var/lib/node-exporter-textfile/fanout_operator_waits.prom`: the PRs and batches that wait
  on a person, and the owed manual-plane applies (`scripts/dev/fanout_lib/operator_waits.py`).
  Disarming removes the file.
- **Next-run review:** before raising the pace cap or widening the rule, read a week of
  `ineligible` reasons and the verdicts of the landings it started.

`KillMode=process` in the unit is load-bearing. The detached landing leaves a user-manager
service's cgroup only, so a system unit's default kill would end it when the oneshot exits.

## The takeover check

`/usr/local/sbin/homelab-takeover-check` runs a D5 flip PR's pre-merge dry runs as root, behind
one `NOPASSWD` line in `/etc/sudoers.d/claude-takeover-check`: `flux diff kustomization` or
the server-side `kubectl diff`, then the RBAC takeover census (issue #5494). The operator
granted it on 2026-10-17 ("Let's add those 3 to your allowed commands"). It exists on daniel-box
only, where `claude_code_takeover_check_enabled` is true.

- **What root runs:** `/usr/local/bin/k3s kubectl` and a root-owned `flux` copy at
  `/usr/local/lib/homelab-takeover-check/flux`, pinned to the operator's chezmoi release and
  checksum. Every flag is written in the script. The only caller input that reaches either binary
  is a validated tag, and a path to a root-owned copy of the user's render.
- **What root never runs:** anything the invoking user can write, and kustomize over the user's
  files. `kubectl kustomize` builds each tag as `SUDO_USER`, and root diffs only the built file.
  The census, which is the user's Python, runs as `SUDO_USER` through `runuser`.
- **Which diff a tag gets:** `flux diff` when the tag's own Kustomization carries `postBuild`,
  so Flux's substitution is exercised; `kubectl diff` otherwise. The wrapper finds that
  Kustomization by `metadata.name` in `flux/<tag>.yaml`. A `-snapshot`, `-verify` or `-deferred`
  directory has no flux file of its own, so its search runs over the base tag's file (#5575).
- **What it prints:** object names, field paths and exit codes. Full outputs stay in
  `/var/lib/homelab-takeover-check/<label>/`, mode 0700.
- **To take it back:** set `claude_code_takeover_check_enabled: false` and apply the role. The
  role removes the sudoers file, the wrapper, the flux copy and the state directory, in that
  order.
- **To widen who holds it,** add to `claude_code_takeover_check_users`; one line per user.
