# `hosts/components/claude-code` — Claude Code's host half (daniel-server and daniel-box)

This component ships Claude Code's host setup. Its `templates/`, `files/` and
`defaults/main.yml` sit beside this file (#6773). The renderer loads
`defaults/main.yml` beneath `component.yml`'s `defaults:`, and each host's `package.yml` reaches
the component's templates and defaults with `component: claude-code`. This file holds the rules
for what they render.

`docs/claude-code-rc-caps.md` holds the record behind the rules below.

## The host packages own it

`hosts/components/claude-code/` ships Claude Code's host half in `homelab-daniel-server` (H7.39)
and `homelab-daniel-box` (H7.40): the Remote Control host, the background resume, the cgroup
metrics, the caps, the two alerts, rootq, the agent user and its home, the agent's lander, the
server-batch lander, the pipeline timeline, the weekly runs summary, the memory store copy, the takeover check, triage,
the homelab-ui browser toolchain, and the artifacts node's peer sync and OTLP-door heartbeat.
The operator's own Claude Code install is the dotfiles repo's (H7.38).

- **The component renders its own `templates/` and copies its own `files/`.** An edit here
  changes both hosts' next package version.
- **The packaged units are named `homelab-*`**, such as `homelab-claude-rc.service` and
  `homelab-claude-clone-sync.timer`. A template that names a sibling unit reads the name from a
  variable. `defaults/main.yml` gives the packaged names of the Remote Control host and the
  cgroup metrics unit, and the two failure pages theirs:
  `claude_code_rc_alert_unit` is `homelab-claude-rc-alert.service`, and
  `claude_code_sync_alert_unit` is `homelab-alert@%n.service`. Two instanced alerts keep their names,
  `claude-stop-alert@` and `fanout-alert@`, because the dotfiles hook and the fan-out tooling
  start them by name.
- **The agent user is declared to the users arm** in each host's `package.yml`, at uid 900 on
  both hosts. The uid sits in the homelab band 900-949, below the uids Ubuntu hands out from
  999 down. The agent's `user-<uid>.slice.d` cap sits there too, because its path carries the uid.
  `homelab-claude-agent.service` runs `claude-agent-setup` at boot and on each change to its
  inputs. It sets the agent's groups, home directories, profile, git identity and signing key,
  and it clones the dotfiles and the repo when they are missing. The agent's gh token is a
  declared output of the host's SOPS file.
- **A toggle decides each subject a host can switch off.** That subject's files, outputs and
  enable links carry the toggle's `when:`, so setting it false in a host's host_vars drops the
  subject from the next package version. A subject every host listing the component ships, such
  as rootq, the caps, the two alerts and the background resume, carries no `when:`; taking one
  back means removing its entries from `component.yml`. The agent user is the one
  exception: the users arm takes no `when:`, so switching the agent off means deleting the
  host's `users:` entry, which the arm then expires. A test holds the two together.
- **A toggle takes a packaged subject back through the package, not an apply.** Set it false in
  the host's host_vars, and the next package version drops the subject.
- **The Remote Control host ships without an enable link** while `claude_code_rc_enabled` is
  false, and reconcile never restarts it (`policy: manual`).
- **The peer login follows `claude_agent_ssh_login_enabled`.** The package cannot read
  daniel-box's agent key, so the component's `claude_agent_peer_public_key` default carries its
  public half. `claude-agent-setup` installs exactly that one line as the agent's authorized_keys,
  so a rotation replaces the old key. Once the toggle is false, the setup script removes the file
  and the agent leaves `ssh-users`. A blank key leaves the file Ansible wrote.
- **The lander's name and settings are pinned in `templates/claude-land-pins.j2`.** The unit,
  the polkit rule and the agent's profile import it, so no inventory value repoints the
  lander. A block var would pin nothing here, because the host package renderer puts the
  inventory above a component's defaults. The pins file and `component.yml` are on the
  lander's approval list.
- **Triage's caps, users and paths are pinned in `templates/claude-triage-pins.j2`**, for the
  lander's reason. Its controller, `claude-triage-ctl`, and its session user, `claude-triage`,
  are declared in `hosts/daniel-box/package.yml`, because triage ships only there. Every triage template imports it. The poller
  is `homelab-claude-triage-poll`, and its page,
  `hosts/components/claude-code/units/homelab-claude-triage-poll-alert.service`, keeps the
  one-hour quiet window a poller failing every tick needs. `claude-triage@` and
  `claude-triage-report-alert@` keep their names, because the poller, the runner and the polkit
  rule start them by name.
- **Root writes nothing in the agent's home that the agent could have replaced with a link.**
  `claude-agent-setup` calls `claude-agent-home`, which writes the signing key, the
  `allowed_signers` file, the peer key on `claude_agent_ssh_key_host`, and the memory seed, all
  as the agent. The seed is the one step that reads as root: root copies the operator's store
  into a private staging directory, and the agent copies it from there. Where `claude_code_user`
  is the agent, it runs while the agent has no `MEMORY.md`, and on the first run after a run as
  the operator if the stores differ; that copy replaces the agent's store (#8074). A run as the
  operator sets `claude_code_agent_memory_seed_stale`, a root-owned flag outside the home. Root
  still has to set the operator's group and the setgid bit on the agent's directories and to
  place a few files, so
  `claude-agent-setup` opens each directory first (`pin`) and refuses unless the opened directory
  is the one its path names. It creates, owns, modes and writes through that open handle, and a
  link on any of those paths fails the run. `install -T` places each file, which replaces a link
  at the file rather than writing through it. `hosts/lib/tests/test_claude_agent_home.py` scans
  both rendered scripts for a root command that names such a path, and runs the setup over
  planted links. The homelab-ui login is
  an output of the host's SOPS file that renders under `/run/homelab-secrets`, outside the
  agent's home. The package ships no link into the home: `claude-agent-setup` links
  `~/.config/homelab-ui/credentials.json` to it with `point`, and `drop` removes the link while
  homelab-ui is off. The agent takes the operator's config from the dotfiles repo, and no host
  sets `claude_code_agent_dotfiles` false.
- **The homelab-ui browser is `homelab-claude-agent-browser.service`**, a oneshot run as the
  agent, which `homelab-claude-agent-browser.timer` starts 10 minutes after boot and every six
  hours. Its script installs the pinned Node, `@playwright/mcp` and Chromium and registers the
  MCP server, or removes all of it while `claude_code_agent_homelab_ui_enabled` is false. The
  timer ships wherever the agent exists, so the way out runs, and a pin bump or a failed run
  converges on the next run. The Node and `@playwright/mcp` pins are in the component's
  `defaults:`, where Renovate and `asset_pins.py --refresh node` read them.
- **Triage's token is a `LoadCredential=` file, `/etc/claude-triage/oauth-token`,** which only
  the session unit loads. The session script joins the whole file without whitespace, so a token
  pasted across `setup-token`'s line wrap still reaches the session whole.
- **Not packaged:** the operator's own Claude Code install, which the dotfiles repo owns since
  H7.38.
  ENFORCED: `hosts/lib/tests/test_claude_code_component.py`,
  `hosts/lib/tests/test_claude_code_on_daniel_box.py`,
  `hosts/lib/tests/test_claude_agent_browser.py`.

## The two Remote Control modes are different features

`/remote-control` **inside a session** publishes that session to the phone. `claude rc` **from a
shell** spawns sessions **on demand** up to `--capacity`, so only it lets the phone create one.

## Activating it

`claude_code_rc_enabled: true` enables and starts the host; back to `false` stops **and** disables
it and its restart timer, which is the rollback. `hosts/tests/test_claude_rc_unit.py` pins both directions.

One prerequisite Ansible cannot check, the trust dialog, is in `docs/claude-code-rc-caps.md`. **Re-run that check after a Claude Code upgrade or spawn-mode change.**

**Stop any hand-run host before deploying**: it and the service compete for one account and
directory.

## What monitoring does and does not cover

`OnFailure=homelab-claude-rc-alert.service` pages Discord when the host **crashes**, reusing the shared
`discord_alert_webhook`.

- **That value reaches the alert unit through an `EnvironmentFile=`, never the unit body.**
  `no_log: true` hides an undefined-variable failure, so if the task fails
  opaquely, check `discord_alert_webhook` is in scope.
- **It pages a login dead at start, not one that dies mid-run**, which leaves the host
  `active`. `docs/claude-code-rc-caps.md` has why (#4618).
- **A session that ends on an API error pages through `claude-stop-alert@<cause>.service`**
  (#6021), which the package ships with `templates/50-claude-stop-alert.rules.j2`, which
  lets the operator and each agent user start. The StopFailure hook in the dotfiles repo runs as
  a user that cannot read the webhook, so it starts the instance, named by the error cause in
  `[a-z0-9_]+`. ENFORCED: `hosts/tests/test_claude_stop_alert.py`.
- **A timer that stops firing pages through a textfile, because `OnFailure=` sees only a failed
  run** (#6165). The clone, dotfiles and memory syncs, the triage poller and the fan-out server
  lander write `<unit>.prom` from the `ExecStartPost=` in `templates/timer-textfile.j2`.
  Observability's `host-checks.yaml` pages a stale file as `Claude<Check>NotReporting` on each
  host that arms the timer. The deadline derives from the timer's period, which must stay
  `<N>min`. ENFORCED: `hosts/tests/test_claude_timer_rule_literals.py`. A timer's flag
  set false makes its rule fire, so the PR that disarms a timer deletes its rule.
  `claude-rc-restart` writes no file: a weekly try-restart whose miss only delays adopting an
  auto-update would page from the install until its first run. `claude-cgroup-metrics` writes
  none either, because a stopped sampler shows as a flat line on the dashboards.

## Traps already paid for

`hosts/tests/test_claude_rc_unit.py` enforces every unit-shape rule here;
`hosts/tests/test_claude_login_slice_caps.py` and
`hosts/tests/test_claude_fleet_slice_cap.py` enforce the two slice planes.

- **`--spawn=` must be passed explicitly**, or Claude Code asks on stdin and the host hangs,
  never connected, while the unit reads `active`.
- **`PATH` must name `/usr/local/bin`**, as with cron: without it a spawned session loses `kubectl`
  and `uv` and reports an **empty cluster** rather than failing. The operator's unit also names
  fnm's default alias, because `~/.zshrc` returns before fnm in the non-interactive shell a
  session snapshots, so a session has no `node` without it (#7972).
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
  leaves the list loses both.
- **The background-shell pressure reaper is off** (`claude_code_rc_disable_bg_shell_pressure_reap`):
  Node reads the **cgroup**, so `free -m` reads clean while it kills backgrounded Bash tasks.
- **The weekly restart uses `try-restart`**, which never starts a host kept stopped on purpose.
- **A Python timer writes its verdict through
  `hosts/lib/host_lib.py:write_check_textfile`.** node-exporter
  drops every `homelab_check_*` series whose file omits the HELP and TYPE lines the other
  check files declare. The file's mtime still reaches Prometheus, so the only symptom is the
  check's `NotReporting` alert. The `host-check-textfiles-go-through-host-lib` row of
  `tests/repo/test_census_rows_python.py` refuses a hand-written series.

## The fleet bound, and why it lives on `user.slice`

`claude_code_fleet_memory_high` / `claude_code_fleet_swap_max` bound both Claude cgroups on
`user.slice`, their shared parent.
The same drop-in sets
`hosts/components/claude-code/defaults/main.yml:claude_code_fleet_cpu_weight`, which ranks the
fleet below `system.slice`, where k3s runs, while the CPU is contended. A weight on
`user-1000.slice` would rank it only against its siblings under `user.slice`, never against k3s.
`docs/claude-code-rc-caps.md` has the derivation. **The `Slice=` line takes effect at the next
start**, so the deploy that changes it drops the RC host's sessions.

`claude_code_fleet_oom_score_adjust` (1000) makes a global OOM kill a fleet process before a
pod. A slice cannot carry `OOMScoreAdjust=`, so the two fleet units set it, a
`templates/fleet-user-manager-oom.conf.j2` drop-in sets it on each fleet uid's
`user@<uid>.service`, and the agent's login profile writes it to `/proc/self/oom_score_adj`.
A `lines:` block in `/etc/zsh/zshenv` writes it for every zsh of the operator's, which is why
the component depends on `zsh`. `zprofile` would miss the desktop app's sessions: they start
from `ssh host cmd`, which is no login shell. Keep the comment-only `zprofile` block declared:
the lines arm never removes a block, and aide-check explains only a declared one.
That drop-in must not be named `50-homelab.conf`: reconcile restarts a unit whose
`50-homelab.conf` changes, and restarting a user manager ends that user's sessions.
ENFORCED: `hosts/tests/test_claude_fleet_slice_cap.py`.

## Background sessions come back at boot

`claude-bg-resume.service` runs `files/bg_session_resume.py` once at boot as
`claude_code_bg_resume_user` (the operator). Claude Code's background supervisor runs on demand,
and nothing starts it at boot. After a reboot it shows each background session the shutdown
killed as failed until someone attaches, replies or runs `claude respawn`. Until then the
session has no process, so its Remote Control entry on the phone is dead. On 2026-10-25 the
sessions waited five hours, until the operator ran `claude agents`.

- **What it respawns:** each worker in `~/.claude/daemon/roster.json` that
  `claude agents --json --all` lists as a background session with no `pid`. The roster holds
  the workers the previous supervisor last recorded as running, so a session it had already
  stopped for idling stays stopped. A roster older than 48 hours is skipped, matching
  upstream's own prompt before it resumes a session that old.
- **The script reads every candidate before the first respawn.** The first `claude respawn`
  starts a supervisor, and that supervisor rewrites the roster with only its live workers.
- **A respawned session keeps its Remote Control entry.** Its `bridgeSessionId` is kept in
  the job's state, so the phone shows the same session rather than a new one.
- **An interrupted turn may continue on its own.** Upstream says a session restarted after a
  crash "is told it was restarted". Whether it then resumes mid-task work is not verified.
- **`KillMode=process` and `RemainAfterExit=yes` are load-bearing.** The supervisor the script
  starts lives in this unit's cgroup, under the fleet bound on `user.slice`, and hosts every
  later dispatch until it idles out. Under the default kill the unit would take every
  session down when it finished or stopped.
- **The role enables the unit and never starts it.** Its job is the next boot.
  ENFORCED: `hosts/tests/test_claude_bg_resume_unit.py`.
- **To see what it would do,** run `uv run --no-project --python <host_python_version>
  /usr/local/lib/claude-bg-resume/bg_session_resume.py --claude ~/.local/bin/claude --dry-run`. On a running box it prints 0 sessions. After a boot,
  `journalctl -b -u homelab-claude-bg-resume` shows what it respawned.

What it does not restore:

- **Sessions `claude-rc.service` spawned.** The host comes back by `Restart=always` and spawns
  new sessions. `claude rc --session-id` cannot be combined with `--spawn`.
- **Interactive sessions that ran `/remote-control`.** They end with their process. Resume one
  with `claude --resume <id>`, then `/remote-control`.
- **The login environment.** The unit sets `HOME`, `PATH`, `KUBECONFIG`, the pytest cap and
  the `session.kind=bg-resume` telemetry label.
  A respawned session has no `SSH_AUTH_SOCK`, D-Bus session or `XDG_RUNTIME_DIR`, because no
  one is logged in at boot. `gh` reads its token from `~/.config/gh/hosts.yml`, so it works.
- **Monitoring.** A failed run pages nothing; `systemctl status homelab-claude-bg-resume` shows it.
  `claude-cgroup-metrics` reads `user-<uid>.slice` and `claude-rc.service`, so it does not
  count the sessions in this unit's cgroup.

## The agent user

`homelab-claude-agent.service` builds the agent user each host's `package.yml` declares, from the
`claude_code_agent_*` values `defaults/main.yml` and the host's host_vars give.
`docs/claude-agent-user.md` has the rest. `claude` lands a PR through
`homelab-claude-land@<n>.service`.

`claude-triage@<episode>.service` runs as `claude-triage-ctl` and leaves a report of one
unattended, diagnose-only session that runs as `claude-triage` (shipped off behind `claude_code_triage_enabled`, and in daniel-box's package).
It starts the session in `homelab-claude-triage-session.service` and waits for it.
`homelab-claude-triage-poll.timer` starts one when an alert starts firing, and ships in `shadow`
mode, which only logs. The templates, runner and poller are on the lander's approval list, and the caps and the poll mode are pinned in `templates/claude-triage-pins.j2`. `docs/claude-agent-user.md`,
*Alert-triggered triage*, has the design.

`claude_code_user` (default `sys_user`) is the account `claude-rc.service` runs as; its home and
`claude_code_rc_workdir` follow it, and the unit is sandboxed only when it is not `sys_user`.
The sandbox hides `/home`, `/root` and `/run/user`, then binds back `/run/user/<uid>` and points
`XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` at it, so `systemd-run --user` works from a
phone session (#6764). The uid is `claude_code_user_uid`, which has no default: a host that
sets `claude_code_user` to the agent sets it in host_vars to the uid its `package.yml` declares.
The operator's unit carries no sandbox but sets the same two variables, at `/run/user/<uid>` for
`claude_code_login_uid` (#6868). It reads that var rather than `claude_code_user_uid`, which
daniel-box keeps at the agent's uid through a rollback to `ubuntu`. Only the agent's unit orders
on `user@<uid>.service`: the operator's binds nothing, so a late manager fails nothing, and a
`Wants=` would start the operator's manager on a host where the operator does not linger.

## The artifacts node's two timers

`homelab-sync-artifacts.timer` (the peer artifact sync) and `homelab-claude-otel-health.timer`
(the OTLP-door heartbeat) ship only on `claude_code_artifacts_node` (daniel-box), in its host
package. They were the role's crons `Sync peer Claude artifacts` and `Claude Code telemetry
health` until H7.40's fourth slice, which retired the cron files and the scripts that embedded the
push tokens. Each timer fires every N minutes of its `*/N` minute default, and its
unit's `SyslogIdentifier=` keeps the tag the cron's `logger -t` wrote, `sync-artifacts` or
`claude-otel-health`. Loki keeps each run's `status=` line under that tag.
`k8s/roles/artifacts/CLAUDE.md` documents how the sync behaves, and the heartbeat's
template carries why it is a host timer at all.

- **Every key the k8s roles also read has a twin there.** `claude_code_artifacts_node`,
  `claude_code_artifacts_peer_sources`, `claude_code_artifacts_host_dir`,
  `claude_code_artifacts_peer_dir`, `claude_code_artifacts_stars_file`,
  `claude_code_otlp_host_ip` and `claude_code_otlp_port` are each held equal to the k8s key by
  `hosts/lib/tests/test_claude_artifacts_node_timers.py`. A change edits both roles. One key in
  `group_vars/all.yml` would route as a deploy-plane change only.
- **The package creates the directories the artifacts pod mounts.**
  `hosts/components/claude-code/tmpfiles.d/homelab-artifacts.conf.j2` declares the local tree,
  each peer directory, the star list directory and the textfile directory, and the sync's
  `ExecStartPre=+` runs `systemd-tmpfiles --create` on it.
- **Each run writes a check file, and neither unit loads a credential.** Each run logs a `status=<up|down>` line under its own syslog tag and writes a check
  file to `/var/lib/node-exporter-textfile/`, `sync-artifacts.prom` or `claude-otel-health.prom`,
  through `write_check_textfile` in `homelab-check-lib.sh`. The `ArtifactsSync*` and `OtlpDoor*`
  rules in observability's `host-checks.yaml` page from those files. Each script renders its
  deadline from its own minute default, so that default must stay `*/N`, and
  `tests/services/alert_rules/test_b6a_rule_literals.py` pins the rules to it.

## Autonomous-role contract (`claude-guardrail-audit`, the audit watches on the guardrail paths)

server#8250, the host half of dotfiles#967's slice 2. `homelab-claude-guardrail-audit.timer`
runs `hosts/components/claude-code/templates/claude-guardrail-audit.sh.j2` as root on both hosts.

- **Scope:** auditd watches under the keys `claude-guardrail` and `claude-memory`, one per
  path in `claude_code_guardrail_audit_paths` and `claude_code_guardrail_audit_memory_dirs`
  that exists. Each run loads a missing watch and removes one the lists no longer name. It
  touches no rule under any other key, so the host-hardening watches stay as they are
  (`hosts/tests/test_claude_guardrail_audit.py::test_a_watch_the_list_dropped_is_removed_and_other_keys_are_kept`).
- **Why a timer, not a rules file:** `augenrules` stops at the first watch whose directory is
  missing, which would also drop the hardening rules. The agent's memory store exists only
  after the agent's first session in its clone, and the next run then watches it.
- **Mode:** `claude_code_guardrail_audit_enabled`. The timer ships either way, so false
  reaches a run that removes every watch under both keys and reports up
  (`hosts/tests/test_claude_guardrail_audit.py::test_disarming_removes_every_watch_under_both_keys`).
- **Abort valves:** a run stops at its first failed `auditctl` read and reports down, loading
  nothing. It removes a watch only under its own two keys, and only one the lists no longer
  name.
- **Evidence:** a `status=` line under `journalctl -t claude-guardrail-audit`, and
  `claude-guardrail-audit.prom`. `ClaudeGuardrailAuditFailed` pages a stopped auditd
  (`hosts/tests/test_claude_guardrail_audit.py::test_a_stopped_auditd_is_flagged`), kernel
  auditing switched off
  (`hosts/tests/test_claude_guardrail_audit.py::test_kernel_auditing_off_is_flagged`), or a
  present path with no watch
  (`hosts/tests/test_claude_guardrail_audit.py::test_a_watch_that_will_not_load_is_flagged_by_path`).
  `ClaudeGuardrailAuditNotReporting` pages a timer that stopped. Each watched write lands in
  `/var/log/audit/audit.log`, which loki-homelab's Alloy ships as `{job="audit"}`
  (`tests/k8s/test_alloy_ships_audit_log.py::test_the_source_tails_auditds_log_into_this_block`). Its SYSCALL line carries the key and the
  writing process, so a Bash write shows apart from the Write tool's.
- **Next-run review:** before adding a path, check that a write to it is rare. Every write to
  a watched path is a shipped record.

## Autonomous-role contract (`claude-guardrail-ack`, the post for each unexplained guardrail change)

server#8250 slice 2. `homelab-claude-guardrail-ack.timer` runs
`hosts/components/claude-code/files/claude_guardrail_ack.py` as root on both hosts, two minutes
after each audit run (`hosts/components/claude-code/units/homelab-claude-guardrail-ack.timer.j2`).

- **Scope:** every file under `claude_code_guardrail_audit_paths`, never the memory stores.
  A change is explained, and stays quiet, when the file matches the md5 recorded inside the
  `.deb` an apt source serves for the installed version
  (`hosts/components/claude-code/tests/test_claude_guardrail_ack.py::test_a_package_file_matching_its_md5_is_not_posted_and_a_drifted_one_is`), when it
  matches what chezmoi renders for its owner from the dotfiles repo's `main`
  (`hosts/components/claude-code/tests/test_claude_guardrail_ack.py::test_a_change_that_matches_the_dotfiles_main_render_is_not_posted`), or when a
  settings.json change touches only `claude_code_guardrail_ack_quiet_settings_keys`
  (`hosts/components/claude-code/tests/test_claude_guardrail_ack.py::test_claude_codes_own_model_write_is_quiet_and_a_permissions_change_is_posted`).
  The operator ruled on 2026-11-13 to post only the rest. The run fetches that `.deb` with
  `apt-get download`, which checks it against the signed index. A package installed with
  `dpkg -i` matches its own md5sums but not a published `.deb`, so its change posts with the
  installed version (#8415,
  `hosts/components/claude-code/tests/test_claude_guardrail_ack.py::test_a_hand_installed_package_change_is_posted_with_both_versions`). Reconcile's `applied.json`
  cannot make that split: the postinst starts reconcile, which records the hand-installed
  version as well. A posted diff masks any line naming a
  token, a secret, a password, an API key, a webhook, a credential, a cookie, a bearer or an
  authorization, and any long credential-shaped value
  (`hosts/components/claude-code/files/claude_guardrail_ack.py:mask_line`,
  `hosts/components/claude-code/tests/test_claude_guardrail_ack.py::test_a_hand_edit_to_a_hook_is_posted_with_its_secret_masked`).
- **Mode:** `claude_code_guardrail_ack_enabled`. False drops the timer, its units and the
  config from the next package version, and keeps the state under
  `/var/lib/homelab-claude-guardrail-ack`.
- **Abort valves:** a failed post leaves the kept copies where they were, so the next run
  posts the same changes again rather than losing them
  (`hosts/components/claude-code/tests/test_claude_guardrail_ack.py::test_a_failed_post_is_retried_on_the_next_run`).
  The first run on a host keeps a copy of every file and posts nothing
  (`hosts/components/claude-code/tests/test_claude_guardrail_ack.py::test_the_first_run_baselines_and_posts_nothing`).
- **Evidence:** a `status=` line under `journalctl -t claude-guardrail-ack`, and
  `claude-guardrail-ack.prom`, which `ClaudeGuardrailAckFailed` and
  `ClaudeGuardrailAckNotReporting` read. The auditd record of the same change names the
  writing process.
- **Next-run review:** after a week, read the posts. A post with an obvious source, such as a
  tool that rewrites settings.json, is a candidate for a new explanation rather than a mute.

## Autonomous-role contract (`claude-dotfiles-mirror`, the verified dotfiles tag for T2)

dotfiles#967 D13, the root half of the T2 config source. `homelab-claude-dotfiles-mirror.timer`
runs `hosts/components/claude-code/files/claude_dotfiles_mirror.py` as root on both hosts.

- **Scope:** a bare mirror of the dotfiles repo under `/var/lib/homelab-claude-dotfiles-mirror`,
  which only root writes. Each run fetches, takes the newest tag, and records its name and
  commit in `verified.json` only when its signature verifies against
  `/etc/claude-dotfiles/allowed_signers`
  (`hosts/components/claude-code/tests/test_claude_dotfiles_mirror.py::test_a_tag_without_the_operators_signature_is_refused`)
  and its commit descends from the last recorded one
  (`hosts/components/claude-code/tests/test_claude_dotfiles_mirror.py::test_a_signed_descendant_tag_moves_the_record_forward`).
  git runs with no system or global config, so nothing in the mirror changes how it checks
  (`hosts/components/claude-code/files/claude_dotfiles_mirror.py:git`).
- **The signer:** `hosts/components/claude-code/templates/claude-dotfiles-allowed-signers.j2` holds one key, the operator's
  "Daniel Box Key", as a literal the inventory cannot override. The operator chose that key on
  2026-11-13. The template and the script are on the lander's approval list
  (`hosts/tests/test_claude_lander.py::test_the_approval_list_land_sh_reads_is_the_floor`).
- **Mode:** `claude_code_dotfiles_mirror_enabled`. False drops the timer, its units, the config
  and the signers file from the next package version, and keeps the mirror and the record.
- **Abort valves:** a refused tag leaves the last record in place, so a bad or rolled-back tag
  never replaces a good one
  (`hosts/components/claude-code/tests/test_claude_dotfiles_mirror.py::test_a_newer_tag_on_an_older_commit_is_refused_and_the_last_is_kept`).
  A run that finds no tag records nothing and reports up
  (`hosts/components/claude-code/tests/test_claude_dotfiles_mirror.py::test_a_repo_with_no_tag_is_up_and_records_nothing`).
- **Evidence:** a `status=` line under `journalctl -t claude-dotfiles-mirror`, and
  `claude-dotfiles-mirror.prom`, which `ClaudeDotfilesMirrorFailed` and
  `ClaudeDotfilesMirrorNotReporting` read.
- **Next-run review:** after 2026-11-21, read the first week of `status=` lines and check that
  each tag the operator pushed was verified. `claude-config-render` renders T2's config from
  this record.

## Autonomous-role contract (`claude-config-render`, T2's Claude Code config from the verified tag)

dotfiles#967 D13 and I7. `homelab-claude-config-render.timer` runs
`hosts/components/claude-code/files/claude_config_render.py` on daniel-box every 15 minutes, in
two steps of one unit.

- **Scope:** `render` runs as `claude-triage-render`, which no session runs as, with no network
  and only its runtime directory writable. It clones the commit `verified.json` names from the
  mirror and renders the dotfiles repo's `settings.base.json` there with chezmoi and the
  root-owned answers in `hosts/components/claude-code/files/claude-config-render-chezmoi.toml`.
  A commit past the verified tag never reaches the stage
  (`hosts/components/claude-code/tests/test_claude_config_render.py::test_only_the_verified_commit_is_rendered`).
  `publish` runs as root. It keeps only `permissions.deny` and `autoMode`'s deny lists,
  environment and `classifyAllShell` (`hosts/components/claude-code/files/claude_config_render.py:floor`), and merges each overlay under
  `/usr/lib/homelab/claude-config-render/overlays/` over them. The triage overlay is
  `hosts/components/claude-code/files/claude-triage-settings-overlay.json`, and the Renovate
  overlay is `hosts/components/renovate-agent/files/claude-settings-overlay.json`. Each overlay
  carries its hooks, the `sudo` and self-edit denies, and bypass mode off. The result goes to
  `/var/lib/homelab-claude-config/current/<overlay>.json`, which each session passes with
  `--settings`. The `t2-1` render publishes exactly the settings the sessions ran with before
  (`hosts/components/claude-code/tests/test_claude_config_render.py::test_the_t2_1_render_publishes_the_settings_each_session_ran_before`).
- **Fail closed:** a session refuses to start without its published file
  (`hosts/components/claude-code/tests/test_claude_triage_session.py::test_no_session_starts_without_the_published_settings`,
  `hosts/lib/claude_config_lock.py:hold_settings`).
- **Mode:** `claude_code_config_render_enabled`, true only in daniel-box's host_vars. False
  drops the timer, the unit, the code and the chezmoi answers from the next package version,
  and keeps the last published version, which the sessions go on reading.
- **Abort valves:** `publish` refuses a stage that holds anything but its two files, a link,
  a commit other than the verified one, or a render without the keys above
  (`hosts/components/claude-code/tests/test_claude_config_render.py::test_a_tampered_or_incomplete_stage_publishes_nothing`).
  A refused publish keeps the last version
  (`hosts/components/claude-code/tests/test_claude_config_render.py::test_a_refused_publish_keeps_the_last_version`).
  A session holds `/var/lib/homelab-claude-config/lock` shared until it exits, and a publish
  that cannot take it exclusive defers to the next run
  (`hosts/components/claude-code/tests/test_claude_config_render.py::test_a_session_holding_the_config_defers_the_publish_and_keeps_its_file`).
- **Evidence:** a `status=` line for each step under `journalctl -t claude-config-render`.
  `/var/lib/homelab-claude-config/current/source.json` names the published commit. A failed
  run pages through `homelab-alert@`.
- **Next-run review:** after the first tag bump past `t2-1`, diff the published files against
  the previous version under `versions/`.

## Autonomous-role contract (`claude-weekly-summary`, one post a week about the unattended runs)

dotfiles#967 C7. `homelab-claude-weekly-summary.timer` runs
`hosts/components/claude-code/files/claude_weekly_summary.py` as root on daniel-box every Monday
at 09:00 UTC, and 10 minutes after each activation of the timer.

- **Scope:** it reads the run records the config lists and posts one message to the alerts
  webhook. It writes nothing but its own posted-week record and its check file. A run covers
  the ISO week before its own, Monday 00:00 to Sunday 23:59 UTC
  (`hosts/components/claude-code/tests/test_claude_weekly_summary.py::test_the_window_holds_sunday_2359_and_drops_the_next_monday_0000`),
  so a catch-up run on a later day covers the same week
  (`hosts/components/claude-code/tests/test_claude_weekly_summary.py::test_a_catch_up_run_later_in_the_week_covers_the_same_week`).
  The sources are triage's `runs/<episode>.json` and the Renovate agent's `runs.jsonl`, at
  the paths each controller's pins file names
  (`hosts/components/claude-code/tests/test_claude_weekly_summary.py::test_the_config_reads_the_record_paths_the_controllers_pin`).
  A new kind of record needs a reader in `READERS` and a row in
  `hosts/components/claude-code/templates/claude-weekly-summary.json.j2`.
- **What it posts:** one count line per source, then each triage run with the restart it
  asked for and the verdict
  (`hosts/components/claude-code/tests/test_claude_weekly_summary.py::test_a_run_that_asked_for_a_restart_shows_the_brokers_verdict`),
  each Renovate tick that
  merged, closed or left PRs open or did not finish, and every run with a denial
  (`hosts/components/claude-code/tests/test_claude_weekly_summary.py::test_a_drill_weeks_records_render_the_summary`).
  A quiet week still posts its count lines
  (`hosts/components/claude-code/tests/test_claude_weekly_summary.py::test_a_quiet_week_still_posts_its_count_lines`).
  Text a session or an alert wrote is put on one line, clamped, and stripped of Discord
  mentions
  (`hosts/components/claude-code/tests/test_claude_weekly_summary.py::test_a_mention_in_a_diagnosis_or_an_episode_is_defused`).
- **Mode:** `claude_code_weekly_summary_enabled`, true only in daniel-box's host_vars. False
  drops the timer, its units and the config from the next package version, and the toggle-gated
  `legacy:` entry removes the check file. `ClaudeWeeklySummaryNotReporting` then fires on the
  absent series until its rule is deleted.
- **Abort valves:** a week already posted posts nothing, so the activation run and a hand
  start repeat no post
  (`hosts/components/claude-code/tests/test_claude_weekly_summary.py::test_a_rerun_of_a_posted_week_posts_nothing`).
  A failed post records nothing and reports down, and the next run posts the week
  (`hosts/components/claude-code/tests/test_claude_weekly_summary.py::test_a_failed_post_reports_down_and_the_next_run_posts_again`).
  A missing source reads as zero runs, and a record that does not parse is counted as
  unreadable
  (`hosts/components/claude-code/tests/test_claude_weekly_summary.py::test_an_unparseable_record_is_counted_and_the_run_stays_up`).
- **Evidence:** the post itself, a `status=` line under `journalctl -t claude-weekly-summary`,
  and `claude-weekly-summary.prom`, which `ClaudeWeeklySummaryFailed` and
  `ClaudeWeeklySummaryNotReporting` read. The deadline is a week plus an hour, 608400 seconds
  (`tests/services/alert_rules/test_b6a_rule_literals.py::test_the_weekly_summary_rules_carry_the_deadline_its_config_renders_is_clean`).
- **Next-run review:** when C2's autopr-repair profile starts writing records, add its source
  and check the first post that carries it.

Applying it: a new version of daniel-box's host package, which its updater installs.

## Autonomous-role contract (`sync-artifacts`, a mirror of another host's tree)

- **Scope:** `rsync --delete` of each `claude_code_artifacts_peer_sources` entry's `path` into
  `claude_code_artifacts_peer_dir/<name>` on daniel-box, then a copy of
  `claude_code_artifacts_stars_file` to the peer's `stars_path`
  (`hosts/tests/test_artifacts_sync_alerts_once_per_outage.py::test_the_star_list_is_pushed_to_the_peer_after_the_pull`).
  It deletes on daniel-box only what the peer has already deleted.
- **Mode:** always on where it is installed, which is `claude_code_artifacts_node` alone
  (`hosts/lib/tests/test_claude_artifacts_node_timers.py::test_only_the_artifacts_node_ships_them`);
  removing the entry from `claude_code_artifacts_peer_sources` (and its twin) stops that peer's
  sync.
- **Abort valve:** a `flock` holds runs to one at a time, and ssh runs in `BatchMode` with a
  10-second connect timeout, so an unreachable peer fails the run rather than hanging it
  (`hosts/components/claude-code/templates/sync-artifacts.sh.j2`).
- **Evidence:** `journalctl -t sync-artifacts`, whose `status=down` line starts at
  `claude_code_artifacts_sync_alert_after_failures` consecutive failures
  (`hosts/tests/test_artifacts_sync_alerts_once_per_outage.py::test_the_verdict_goes_down_at_the_threshold_and_stays_down_past_it`), and
  `sync-artifacts.prom`, which carries the same verdict for the `ArtifactsSyncFailed` and
  `ArtifactsSyncNotReporting` rules.

## Autonomous-role contract (`claude-memory-sync` overwrites a store on another host)

- **Scope:** `rsync --delete` of `claude_code_memory_sync_dir` (follows `claude_code_user`) to
  `claude_code_memory_sync_target_dir` (the operator's store)
  on `claude_code_memory_sync_target`, one-way, by the operator's decision (#3123). The copy
  strips every write bit from the target (`--chmod=a-w`), so a memory a daniel-server session
  tries to write fails at once; `docs/claude-memory-sync.md` has why.
- **Mode:** `claude_code_memory_sync_enabled`, true only in daniel-box's host_vars; false
  drops the timer and its units from the next package version.
- **Abort valve:** the unit skips unless the source `MEMORY.md` is non-empty.
- **Evidence:** `journalctl -u homelab-claude-memory-sync` lists each file a run changed or
  deleted; a failure pages Discord, and `claude-memory-sync.prom` pages a timer that stopped. A run the
  empty-index condition skips writes no file either, so an empty store pages too.

## Autonomous-role contract (`claude-triage@`, a session started by a monitor episode)

- **Scope:** one episode per run. The session diagnoses: Edit, Write, a landing and a push
  are denied (`hosts/components/claude-code/files/claude_triage_session.py:DENIED_TOOLS`,
  `hosts/components/claude-code/tests/test_claude_triage_session.py::test_the_session_is_diagnose_only_and_capped`).
  Its one action is a request: the result's optional `restart` field names an allowlist key.
  The runner, as the controller, starts `homelab-claude-restart@<name>.service` for it, waits
  for the broker, and writes the broker's verdict line into the record and the report
  (`hosts/components/claude-code/files/claude_triage_run.py:request_restart`,
  `hosts/components/claude-code/tests/test_claude_triage_run.py::test_a_diagnosis_naming_a_listed_workload_starts_the_broker_and_records_its_verdict`).
  A name off the allowlist starts nothing
  (`hosts/components/claude-code/tests/test_claude_triage_run.py::test_a_name_off_the_allowlist_starts_nothing_and_is_escaped_in_the_report`),
  and neither does a request from a diagnosis of low confidence
  (`hosts/components/claude-code/tests/test_claude_triage_run.py::test_a_low_confidence_diagnosis_cannot_ask_for_a_restart`).
  The broker replaces its verdict file on every start, so the runner reads the file only when
  its inode or mtime changed during the request
  (`hosts/components/claude-code/tests/test_claude_triage_run.py::test_a_verdict_an_earlier_request_left_is_not_read_as_this_ones`).
  The broker's wait runs inside the run's wall clock, which adds the broker's pinned timeout
  (`hosts/tests/test_claude_triage.py::test_the_run_outlasts_the_session_and_the_restart_it_waits_for`).
  A run that asked for a restart keeps its state, so the page's closed list of states is
  unchanged; the broker posts each restart to Discord itself. A fix landed through
  `homelab-claude-land@` is a later slice, under the operator's 2026-10-15 rulings.
- **Identity:** the controller, `claude-triage-ctl`, runs the poller and the runner and alone may
  start the units (`hosts/tests/test_claude_triage.py::test_the_poller_runs_as_the_controller`).
  The session user, `claude-triage`, holds no GitHub token and no start right,
  and reads the clone and its toolchain read-only
  (`hosts/tests/test_claude_triage.py::test_the_session_shares_only_read_access_with_the_controller`).
  Neither is `claude` (dotfiles#967's I1).
  The session reaches the network only through the T2 egress proxy,
  `hosts/components/t2-egress/`, whose nftables fence refuses every other connection its uid
  opens, and whose squid admits only the hosts its config lists. Its kubeconfig names the API
  at the host's LAN address, so that request goes through the proxy too
  (`hosts/lib/tests/test_t2_egress_component.py::test_the_triage_kubeconfig_names_the_api_by_the_hosts_lan_address`).
  The session unit refuses to start unless the fence holds its uid's rules
  (`hosts/lib/tests/test_t2_egress_component.py::test_each_session_unit_checks_its_own_uid_is_fenced_before_it_starts`).
  The fence's one other opening is the loopback OTLP collector
  (`hosts/lib/tests/test_t2_egress_component.py::test_each_session_uid_reaches_the_otlp_collector_by_its_pre_dnat_address`).
  The overlay's `env` sends the session's telemetry there with `session.kind=triage` and no
  prompt or tool content. Prometheus keeps only the `claude_code_*` names from the collector,
  so a session cannot forge a series a routed alert reads
  (`hosts/tests/test_claude_triage_alert_inputs.py::test_no_routed_alert_reads_a_series_a_session_can_send_is_clean`).
- **Mode:** `hosts/components/claude-code/defaults/main.yml:claude_code_triage_enabled`, false
  by default; false stops the poller, then removes
  the rule, the units, the code and the config, and keeps the records.
  `claude_code_triage_poll_mode` is `off`, `shadow` or `armed`, pinned at `shadow` in
  `hosts/components/claude-code/templates/claude-triage-pins.j2`, so arming the trigger needs
  the operator's approval
  (`hosts/tests/test_claude_triage.py::test_the_template_pins_equal_the_defaults`).
- **Abort valves:** `$10`, 60 turns and a 30-minute wall clock per run, and six runs a UTC day,
  all pinned in `hosts/components/claude-code/templates/claude-triage-pins.j2`, plus a lock that holds runs to one at a time. The runner
  enforces the daily cap, because polkit lets the controller start a run without the poller
  (`hosts/components/claude-code/tests/test_claude_triage_run.py::test_the_daily_cap_stops_a_run_before_any_session_starts`). The
  poller opens an episode only on a fall that holds for two polls
  (`hosts/components/claude-code/tests/test_claude_triage_poll.py::test_a_fall_opens_one_episode_once_it_has_held_for_the_streak`)
  and that the pinned route table (`poll_routes`) names. It logs an unrouted fall as skipped
  (`hosts/components/claude-code/tests/test_claude_triage_poll.py::test_an_unrouted_fall_is_skipped_once_and_never_makes_a_storm`),
  makes one storm episode of more than three falls of one profile at once
  (`hosts/components/claude-code/tests/test_claude_triage_poll.py::test_more_falls_than_the_storm_size_make_one_storm_episode`),
  and opens nothing while `DeployHoldActive` fires or the weekly reboot's mute is open
  (`hosts/components/claude-code/tests/test_claude_triage_poll.py::test_a_hold_or_the_reboot_mute_defers_the_episode_until_it_lifts`).
  It queues episodes and starts the next only when the lock is free
  (`hosts/components/claude-code/tests/test_claude_triage_poll.py::test_queued_episodes_start_one_at_a_time_as_the_lock_frees`),
  and never restarts one whose run crashed
  (`hosts/components/claude-code/tests/test_claude_triage_poll.py::test_a_queued_episode_whose_run_crashed_is_not_started_again`).
- **Evidence:** `/var/lib/claude-triage-ctl/state/runs/<episode>.json` and the session's
  transcript beside it, `<episode>.transcript.jsonl`
  (`hosts/components/claude-code/tests/test_claude_triage_run.py::test_the_transcript_is_kept_beside_the_record_and_nothing_stays_in_out`),
  the report under `/a/<host>-claude-triage/`, and a Discord page for every run
  (`hosts/components/claude-code/tests/test_claude_triage_run.py::test_a_run_with_no_diagnosis_still_leaves_a_record_a_report_and_a_page`);
  a crash pages separately, and a poller failure pages at most once an hour
  (`hosts/components/claude-code/units/homelab-claude-triage-poll-alert.service`). In shadow
  mode, `/var/lib/claude-triage-ctl/state/shadow.jsonl` and `journalctl -u homelab-claude-triage-poll` list each
  run the poller would have started
  (`hosts/components/claude-code/tests/test_claude_triage_poll.py::test_armed_mode_starts_a_run_and_shadow_mode_only_logs_it`). `claude-triage-poll.prom` pages a poller that stopped.
- **Next-run review:** before widening the scope or a cap, read the last week's records for
  what the runs diagnosed and what they cost.

## Autonomous-role contract (`homelab-claude-restart@`, a restart of one allowlisted Deployment)

Triage slice 3 (#9387). `homelab-claude-restart@<name>.service` runs
`hosts/components/claude-code/files/claude_restart.py` as root on daniel-box.
`docs/claude-agent-user.md`, *The restart allowlist*, has the design and the operator's rulings.

- **Scope:** one `kubectl rollout restart` of the Deployment the allowlist entry `<name>`
  names, under the field manager `claude-restart`, which Flux's takeover leaves alone. The
  namespace and the Deployment come from the entry, never from the instance
  (`hosts/components/claude-code/tests/test_claude_restart.py::test_a_listed_workload_is_restarted_by_the_entrys_names_and_reported`).
  The allowlist is `restart_allowlist` in
  `hosts/components/claude-code/templates/claude-triage-pins.j2`, and it lists `docs`. A name
  off it is refused
  (`hosts/components/claude-code/tests/test_claude_restart.py::test_an_empty_allowlist_refuses_every_name_and_posts_nothing`).
  Each entry is a one-replica rolling Deployment that a failed restart leaves serving, alone
  in its role and outside the alert plane
  (`hosts/tests/test_claude_restart_allowlist.py::test_every_listed_workload_is_clean`,
  `hosts/tests/test_claude_restart_allowlist.py::test_a_deployment_a_restart_could_take_down_is_flagged`).
  The poller routes every fall of a listed service nowhere, and every fall labelled
  `unmapped` while a service is listed, so a restart opens no triage episode
  (`hosts/components/claude-code/tests/test_claude_triage_poll_restartable.py::test_a_fall_of_a_restartable_service_opens_nothing_whatever_the_table_says`).
  A poll that cannot read the listed services fails and pages rather than route them
  (`hosts/components/claude-code/tests/test_claude_triage_poll_restartable.py::test_a_poll_that_cannot_read_the_restartable_services_fails`),
  and a `poll_routes` key that names a listed service fails
  `hosts/tests/test_claude_restart_allowlist.py::test_a_route_naming_a_listed_service_is_flagged`.
- **Identity:** root, because only root reads the k3s admin kubeconfig. Besides root, only
  the triage controller, `claude-triage-ctl`, may start the unit, and it may only start it:
  `hosts/components/claude-code/templates/50-claude-restart.rules.j2` admits no other user and
  no other verb, by the operator's 2026-11-20 ruling
  (`hosts/tests/test_claude_restart_broker.py::test_only_the_triage_controller_may_start_the_broker_and_only_start_it`).
  Neither `claude` nor the triage session user can: a triage run asks through its result, and
  the runner starts the unit. The rule admits an instance only in the
  script's name shape
  (`hosts/tests/test_claude_restart_broker.py::test_the_rule_admits_only_the_broker_in_the_scripts_name_shape`).
  The unit passes the script its instance and nothing else
  (`hosts/tests/test_claude_restart_broker.py::test_the_unit_passes_only_its_instance_to_the_root_script`).
- **Mode:** `hosts/components/claude-code/defaults/main.yml:claude_code_restart_enabled`, true
  only in daniel-box's host_vars. False drops the unit, the script and the config from the next
  package version, and keeps `/var/lib/claude-restart`. It also empties the restart names
  the triage config and prompt carry, so no run asks for a restart
  (`hosts/tests/test_claude_triage.py::test_a_switched_off_broker_leaves_a_run_no_name_to_ask_for`). To stop the broker without a landing,
  run `sudo touch /etc/claude-restart/hold`; removing the file releases it
  (`hosts/components/claude-code/tests/test_claude_restart.py::test_the_hold_file_refuses_a_listed_workload`).
- **Abort valves:** one restart per workload per 24 hours
  (`hosts/components/claude-code/tests/test_claude_restart.py::test_a_second_restart_of_one_workload_waits_out_its_cooldown`)
  and three per UTC day
  (`hosts/components/claude-code/tests/test_claude_restart.py::test_the_daily_cap_counts_every_workloads_restarts`),
  both pins that no inventory value moves
  (`hosts/tests/test_claude_restart_broker.py::test_an_inventory_value_cannot_list_a_workload_or_raise_a_cap`).
  The script refuses while the weekly reboot's mute is open, because a failed restart would
  page nobody
  (`hosts/components/claude-code/tests/test_claude_restart.py::test_the_weekly_reboot_mute_refuses_because_a_failure_would_page_nobody`),
  and while the Deployment is mid-rollout or its Flux Kustomization is not Ready
  (`hosts/components/claude-code/tests/test_claude_restart.py::test_a_deployment_mid_rollout_or_mid_apply_is_refused_as_busy`).
  A `flock` holds restarts to one at a time.
- **Evidence:** every run ends in one `RESTART-RESULT:` line, under `journalctl -t
  claude-restart` and in `/var/lib/claude-restart/verdict`. Each restart, done or failed, is a
  line in `/var/lib/claude-restart/log.jsonl` and one Discord post. A failed restart exits 1
  and pages through `homelab-alert@`. A restart whose Deployment kept its generation reads as
  failed, because kubectl prints success for an identity that cannot write
  (`hosts/components/claude-code/tests/test_claude_restart.py::test_a_restart_that_changed_no_generation_fails_and_still_counts`).
  A refusal exits 0 and posts nothing.
- **Next-run review:** after the first real restart, wait one Flux reconcile and check that
  the Deployment holds one new ReplicaSet, not two. Before listing another workload, read
  `log.jsonl` for what the restarts of `docs` did.

Applying it: a new version of daniel-box's host package, which its updater installs.

## Autonomous-role contract (`claude-clone-sync`, `claude-dotfiles-sync`)

- **Scope:** per agent: a `--ff-only` pull of its clone
  (`hosts/tests/test_agent_clone_fast_forward.py::test_a_clean_master_advances_to_origin`),
  then the venv if its lock moved
  (`hosts/tests/test_agent_clone_fast_forward.py::test_a_moved_lock_resyncs_the_venv_and_installs_no_collections`);
  and a pull and `chezmoi apply` of the operator's dotfiles
  (`hosts/tests/test_agent_dotfiles.py::test_the_agent_variant_is_applied_in_two_passes_that_spare_the_role_owned_dirs`).
- **Mode:** `claude_code_agent_user_enabled` gates both, and `claude_code_agent_dotfiles` gates
  the dotfiles sync as well; false drops the `homelab-claude-{clone,dotfiles}-sync` units from
  the next package version.
- **Abort valve:** the clone sync skips unless the clone is a clean `master`
  (`hosts/tests/test_agent_clone_fast_forward.py::test_the_agents_work_in_progress_is_left_alone`);
  the dotfiles sync refuses a source without the agent variant
  (`hosts/tests/test_agent_dotfiles.py::test_a_source_without_the_agent_variant_is_refused`).
- **Evidence:** `journalctl -u homelab-claude-{clone,dotfiles}-sync`; a failure pages Discord, and
  `claude-{clone,dotfiles}-sync.prom` pages a timer that stopped
  (`hosts/lib/tests/test_claude_code_component.py::test_each_packaged_unit_names_its_packaged_sibling`).
  A clone left dirty or off
  `master` skips with exit 0 and still writes its file, so the file says the timer runs, not
  that the clone is current.

## Autonomous-role contract (`claude-fanout-server-lander`, a landing with no session behind it)

Issue #5496. A daniel-server fan-out batch stops at its PR, and this timer lands the PRs that
qualify. The operator approved it on 2026-10-18. Since option B of `docs/ci-throughput.md`
(2026-11-15), a daniel-server review batch lands its own draft when
`scripts/dev/fanout_lib/own_surface.py:pr_problems` passes it, so this timer sees only the PRs
that rule holds, which stay drafts with no CI gate and which it leaves for a person, and plain
batches. `homelab-claude-land@` cannot do it: that unit
admits only the `claude` user and pins the author and branch prefix of the agent's PRs, while a
server batch runs as `ubuntu` on `worktree-fanout-<batch>`.

- **Scope:** every 10 minutes, `fanout.py land-server` reads the review records of the last 48
  hours from daniel-server in one ssh call, reads the repo's open PRs in one `gh` call, and
  starts `land.sh --pr <n> --arm-merge --await-merge --detach` for each PR that passes
  `scripts/dev/fanout_lib/autoland.py:problems`. That means a batch that ran its review phase,
  ended `pr`, passed its CI gate and left no finding, whose PR is open, not a draft, still at
  the head the review read, with no unchecked box under an operator or before-merge heading and
  no `needs input` or `Do not merge` text. A `--no-review` batch writes no record, so it never
  lands. Neither does a PR touching the landing path's code (`scripts/deploy_tools/`, `scripts/lib/`), the
  fan-out tooling, `secrets/`, `.github/`, `.claude/` or this component. One narrowing
  applies (#5659). A PR passes when its only `pyproject.toml` change is to
  `[tool.pytest.ini_options].testpaths`, judged by parsing the file at the merge base and at the
  head, and a read or parse failure holds it. Each landing runs from its own `origin/master` worktree under
  `~/.local/state/fanout-server-lander/checkouts/`, never from the primary checkout.
- **Worktree sweep (#8575):** at most every 6 hours
  (`scripts/dev/fanout_lib/autoland.py:clean_landed`), a pass also runs `place clean`'s legs
  over every live run manifest in `ubuntu`'s `~/.claude/fanout/`, newest first, for up to 15
  minutes (`scripts/dev/fanout_lib/clean.py:sweep_runs`). It runs no leg for a batch whose
  branch heads an open PR. Each leg keeps `clean`'s refusals: an active unit, a dirty tree or
  an unmerged branch keeps the tree. A failed leg logs a line and never changes the exit status.
  A batch kept by `scripts/dev/fanout_lib/manifest.py:SWEEP_KEPT_LIMIT` cleans in a row with
  the same line gets no more legs: the sweep logs one line naming `place abandon`, and `place
  runs` lists the batch as `sweep-stopped`. A changed `kept:` line, an open PR on the branch or
  a merged PR newer than the one recorded at the stop restarts the count. A unit still active
  or a process still in the tree clears it, since either ends without anyone acting.
- **Coordinator check (#9533):** each pass ends with
  `scripts/dev/fanout_lib/coordinators.py:report`. It lists the worktrees locked for a
  coordinator project, and for each one no live process holds, the batches that read `parked`
  or that this lander will not land. A project with such a batch pages the operator once,
  on the second pass in a row that reads it so, through
  `fanout-alert@fanout-coordinator-<worktree>:no-coordinator.service`
  (`scripts/dev/tests/fanout/test_fanout_coordinators.py::test_an_orphaned_project_pages_once_on_its_second_pass`).
  It starts no session and changes no worktree, lock, claim or PR. The record of what it
  paged is `~/.local/state/fanout-server-lander/coordinator-pages.json`. A failed read logs a
  line and never changes the exit status. `--dry-run` logs the page it would send.
- **Primary:** the unit's working directory and `--primary` are `claude_code_operator_checkout`,
  which `ubuntu` owns. They are never `claude_code_rc_workdir`, the agent's clone on daniel-box:
  git refuses that clone as dubious ownership, and `ubuntu` cannot write to it (#5861).
- **Mode:** `claude_code_fanout_server_lander_enabled`, true only in daniel-box's host_vars.
  Setting it to false drops the timer and its units from the next package version, and every
  server batch stops at its PR again. `fanout.py land-server --dry-run` judges and logs
  without starting anything.
- **Abort valves:** a run starts every eligible PR, and the landing turn and the landing slots
  order and bound the landings (#8823). A PR is started once: a log in `~/.local/state/fanout-server-lander/logs/`, or
  a live `land.py --pr <n>` process on daniel-box, skips it. `land.sh` refuses a second
  landing of a PR while one is live on either host, and the lander logs that start as
  `another landing has it`. The unit sets
  `LAND_REQUIRE_BRANCH_PREFIX`, which makes `land.sh`
  pin the merge to the head it checked, and `LAND_REQUIRE_AUTHOR`.
- **Evidence:** `journalctl -u homelab-claude-fanout-server-lander` has one line per candidate PR,
  `eligible` or `ineligible` with the reason. Each landing's own `VERDICT:` is in its
  `land<pr>-*.log`, and `cc-wait land <pr> --log-dir ~/.local/state/fanout-server-lander/logs`
  waits on it. An unreadable daniel-server logs a line and exits 0, because the host is off for
  maintenance; an unreadable GitHub exits 1 and pages. A pass that read both also writes
  `homelab_awaiting_operator{reason}` and its `_oldest_seconds` twin to
  `/var/lib/node-exporter-textfile/fanout_operator_waits.prom`: the PRs and batches that wait
  on a person (`scripts/dev/fanout_lib/operator_waits.py`).
  Each clean run also writes `claude-fanout-server-lander.prom`, which pages a timer that
  stopped. Disarming adds both files to the component's toggle-gated `legacy:` entries, so the
  reconcile that drops the timer also removes them. The NotReporting rule still fires on the
  absent series until it is deleted.
- **Next-run review:** before raising the pace cap or widening the rule, read a week of
  `ineligible` reasons and the verdicts of the landings it started.

`KillMode=process` in the unit is load-bearing. The detached landing leaves a user-manager
service's cgroup only, so a system unit's default kill would end it when the oneshot exits.

Each pass also stamps every PR held for a counted reason into
`~/.local/state/fanout-server-lander/operator-waits.json`: `{"pr", "reason", "since"}` on the
first pass that sees the hold, and `until` on the first pass that does not
(`operator_waits.restamp`, #5682). `pipeline_timeline.py` joins the stamps into `waited_h`. The
gauge above counts the waits that are open now; the stamps say which PR's wait ended when. A
stamp has the pass's 10-minute resolution, and a closed one is kept 60 days.

## Autonomous-role contract (`claude-pipeline-timeline`, a daily snapshot of the PR timeline)

Issue #5661. `scripts/dev/pipeline_timeline.py` joins each merged PR's stamps into one row.
Two inputs are in daniel-box's journal, which keeps about ten days (`SystemMaxUse=1G`), so a day
nobody ran the joiner was lost. This timer runs it daily as `ubuntu`.

- **Scope:** every 1440 minutes, `pipeline_timeline.py --days 3 --pull-reviews daniel-server`
  reads GitHub, the landing ledger and the lander's journal, and appends its rows to
  `~/.local/state/pipeline-timeline/<UTC date>.jsonl`. It first copies daniel-server's review
  records with a read-only `rsync` into `~/.local/state/pipeline-timeline/reviews-daniel-server/`
  (one ssh connection, inside the `ufw limit ssh` budget). It writes nothing else, and changes
  no repository, cluster or GitHub state.
- **Mode:** `claude_code_pipeline_timeline_enabled`, true only in daniel-box's host_vars. False
  drops the timer and its units from the next package version, and keeps the snapshots. The
  same version's toggle-gated `legacy:` entry removes the heartbeat file, and
  `ClaudePipelineTimelineNotReporting` fires on the absent series until it is deleted.
  `--days` is `claude_code_pipeline_timeline_days`; a window of N days survives N-1 missed runs,
  because a later snapshot's rows supersede an earlier one's for the same PR.
- **Abort valves:** the unit's `TimeoutStartSec=30min`. A pull that fails warns and joins the
  copy the last pull left, so a server down for maintenance costs no snapshot. A failed `gh`
  read fails the unit and pages.
- **Evidence:** `journalctl -u homelab-claude-pipeline-timeline` holds the summary; a failure
  pages Discord through `homelab-alert@`, and `claude-pipeline-timeline.prom` pages a
  timer that stopped (`ClaudePipelineTimelineNotReporting`, a 174900-second deadline).
- **Next-run review:** after a week, check that the snapshot files hold one run a day, and that
  rows for daniel-server PRs carry `phases_min`. An empty `phases_min` means the pull failed
  on the first run and no copy exists yet.

Applying it: a new version of daniel-box's host package, which its updater installs.

## The root helpers: the takeover check and rootq

`docs/claude-code-root-helpers.md` holds both grants. The takeover check
(`claude_code_takeover_check_enabled`) runs a D5 flip PR's dry runs as root on daniel-box.
rootq reads root-only host state through a sudo line, and the
agent user reaches it through `homelab-rootq.socket`. Read that page before you change a
helper, its allowlist or its grant.
