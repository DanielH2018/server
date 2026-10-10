# Claude as its own UNIX user on daniel-box

On 2026-10-04 the operator approved a plan to run every Claude session on daniel-box as a
dedicated `claude` UNIX user instead of `ubuntu`. This page is the plan of record: the slices,
the decisions taken along the way, and what each slice still owes. It replaces the
`claude-unix-user-plan_2026-10-04.html` artifact as the durable copy, because
`prune-artifacts.sh` deletes an artifact 30 days after its last update (#3657).

One part of the plan stays out of this page. The repo is public, and the per-path inventory of
what a session reaches as `ubuntu` stays private. The operator decided this on 2026-10-09
(#3685), and kept it private when slice 6 landed. See
[What this page leaves out](#what-this-page-leaves-out).

## Status

| Slice | What it delivers | State |
|---|---|---|
| 1 | A read-only `claude` user an operator can open a session as | Done 2026-10-04 (#3504, #3505) |
| 2 | Claude opens PRs under its own GitHub identity | Done 2026-10-05 (checked on #3622) |
| 3 | Merged agent work lands and deploys through a lander unit | Built and deployed 2026-10-08 (#3633, #3650, #3651, #3652). The end-to-end check passed 2026-10-09, after #3999 fixed the approved-PR merge. Done. |
| 4 | The phone host `claude-rc.service` runs as `claude` | 4a to 4c deployed 2026-10-09 (#4035, #4030, #4036, #4039, #4044). 4d (#4054) switched the host over on 2026-10-09. The check passed the same day. Done. |
| 5 | Peer users, and the tools that decrypt SOPS | The homelab-ui login deployed 2026-10-09 (#4051). Peer users dropped. The agent's own browser and MCP registration are built and await an apply (#4058). |
| 6 | Retire Claude sessions as `ubuntu` | New sessions start as `claude` since 2026-10-09; caps render for both users. The uid 1000 metric and alert, and dropping uid 1000, wait for the operator. |
| 7 | A pre-merge dry run without secrets | Optional, planned |

Each slice ends in a check that can be run and a variable that turns it back off.

## Why a separate user

A Claude session acts on text it reads: issues, PR comments, web pages, logs and tool output.
Any of that text can carry an injected instruction. The plan assumes that some session
follows one, and limits what that session can do without leaving a record.

A session that runs as `ubuntu` has every right the operator's uid has. The split removes
three kinds of reach:

- **The operator's credentials and personal data.** Every file the operator's uid can read is
  readable by the session, and a session can send what it reads off the host.
- **The privilege the operator's session holds.** That includes sudo, so a session can reach
  root without a commit.
- **Code execution in the deployer.** The deployer runs from a git directory that `ubuntu`
  owns, and a session as `ubuntu` can change its `.git/config`. See the rules below.

Each of these lets an injected session take data or reach root and leave no commit behind.
[What the `claude` user cannot do](#what-the-claude-user-cannot-do) lists what replaces each
right the agent gives up.

### What the split cannot close

**A merged deployable change reaches root, whatever the agent's uid.** The GitOps deployer
applies the setup plane with `become`. Ansible applies k8s manifests with cluster-admin rights,
and a manifest can schedule a privileged pod that mounts the host's filesystem. An agent whose
change merges and deploys therefore runs code as root on the nodes.

Only a human approval on every merge closes that path. The operator rejected that gate on
2026-10-04, for the reasons in
[The decision: a visible trail, not a merge gate](#the-decision-a-visible-trail-not-a-merge-gate).
The split makes the path visible instead. Every change that reaches root needs a signed commit by
`DanielClaudeBot`, a CI run and a lander record.

The slice 3 gate has limits of its own. It requires the operator's approval only for the paths
on its list and the modules the landing process imports. Code that runs only after the checks,
such as the deploy itself, falls under the root chain above.

## The template: `renovate-agent`

The repo already ran one Claude agent as a separate user. `ansible/roles/setup/renovate_agent/`
creates `renovate-agent` with its own home under `/var/lib`, its own clone, its own copies of
`uv` and `claude_guard`, a hardened unit, and a GitHub token that cannot push. A polkit rule lets
it start one lander unit, which runs root-owned code as `ubuntu` and re-checks the PR.

The plan extracts that shape rather than copying it. The identity tasks became the shared
`ansible/roles/setup/common/tasks/agent_user.yml`, which both agents call, and the `claude`
agent gets a lander of the same shape.

## Rules the design follows

- **A broker never runs code from the agent's tree as `ubuntu`.** Broker code is root-owned and
  acts only on `origin/master` or the operator's checkout. This rules out a brokered `--check`
  or `--dry-run` over the agent's clone, because both decrypt SOPS.
- **The agent has its own clone.** git checks that the current uid owns the git directory,
  the worktree and the `.git` file (git 2.43, `setup.c` `ensure_valid_ownership`). Group ownership and
  the `setgid` bit do not pass that check, and a shared `.git/config` lets either user run code as the
  other.
- **The agent belongs to none of `ubuntu`, `sudo`, `adm`, `docker`, `lxd` or `kvm`.** Without
  `adm` or `systemd-journal`, `journalctl` shows the agent only its own units. The operator
  granted `systemd-journal` on 2026-10-09, after a secrets scan of the journal. Slice 4 adds it.
- **The agent has its own GitHub identity and signing key.** A PR author cannot approve their
  own PR, so any approval gate needs a second identity.
- **Remote Control needs one interactive step.** It requires a claude.ai `/login`, and without a
  TTY it exits with `Workspace not trusted`, so the agent user trusts its working directory once
  by hand.

## Slice 1: a read-only `claude` user

Deployed 2026-10-04 in #3504 and #3505. The operator logged in as `claude` the same day and
reported the checks working.

- The `claude` user has uid 996 and home `/var/lib/claude`. `claude_code_agent_user_enabled` in
  daniel-box's host_vars switches it on.
- It shipped differently from the plan in three ways. Entry is
  `sudo machinectl shell claude@ /bin/bash -l`, with no `ssh-users` membership and no authorized
  key. The dotfiles' user-level `~/.claude` is not applied. A `.profile` points the hook shim at
  the agent's clone and `uv`.
- The first tick failed on the Claude Code installer's sudo guard and held the deployer. #3505
  set `CLAUDE_INSTALL_ALLOW_SUDO=1`, and the hold was cleared after a hand apply.
- The first login needed a hand `uv sync` in `~/server`, because the role does not create the
  clone's `.venv` (#3513).
- The agent's `~/.claude` is `drwx------ claude:ubuntu`. The role sets that mode on every
  apply, in `ansible/roles/setup/claude_code/tasks/agent_github.yml`, because the directory
  holds the session's login. The group comes from the `setgid` home, but at `0700` it grants
  nothing. `ubuntu` cannot read the agent's memory or artifacts, so slice 4 needs a read grant.
  This page said `drwxr-s---` until 2026-10-09 (#3901). 3f15ead01 added the `0700` task on
  2026-10-05, one day after that check was written.

**Check:** as `claude`, `id` lists none of the denied groups, `ls /home/ubuntu` is refused,
`sudo -n true` fails, `kubectl get pods -A` works and `uv run pytest scripts` passes.

**Rollback:** turning the variable off locks the account and stops its units. It does not delete
the home, because the home holds the agent's clone and any unpushed work.

## Slice 2: Claude opens PRs under its own GitHub identity

Done 2026-10-05. The pieces are PRs #3541, #3542, #3598, #3613 and #3617.

Decisions, in the order they were taken:

- **A machine user account, not a GitHub App** (2026-10-04). The account is `DanielClaudeBot`
  (id 338220904), a write collaborator, because a personal-account repo has no lower role.
- **Its token is a classic `public_repo` token.** GitHub does not support fine-grained tokens
  for a collaborator on another personal account's repo. It is stored in SOPS as
  `claude_code_agent_gh_token`, so `ansible/secret_rotation.yml` tracks it.
- **The review requirement is its own ruleset.** "master CI gate" (id 20912512) has no bypass
  actors, and adding one there would also exempt the operator from CI. The operator created
  ruleset 24514824, named "master review gate," with repository admin and the Renovate app as bypass
  actors. It sets `require_last_push_approval`, because GitHub stops only a PR's author from
  approving it.
- **`land.sh` merges directly when a PR is blocked only by the missing review** (#3541). GitHub
  auto-merge ignores a ruleset bypass, while a direct merge honours it (github/docs#45265, which
  includes a repro). So `land.sh --arm-merge` waits for CI, then merges through the REST
  endpoint, pinned to the head SHA it checked. Slice 3's check found that an approved agent PR
  needs the same path, because the branch fence below also restricts updates to master (#3911).
  The fence applies whatever the review decision, so every PR takes that path and `land.sh`
  arms no auto-merge at all (#4001).
- **Renovate's `platformAutomerge` is off** (#3542), for the same reason. Renovate's own direct
  merge uses its bypass. Automerges therefore land on Renovate's next run rather than the moment
  CI passes.
- **Ruleset 24517167, named "agent branch fence," restricts branch writes.** It restricts creating,
  updating and deleting every branch except `refs/heads/worktree-claude+**`. The admin role and
  Renovate bypass it. The "Default" ruleset excludes that prefix too, so the agent can
  force-push and delete its own branches. `EnterWorktree` turns `/` in a worktree name into `+`,
  so a `claude/<slug>` worktree gets the branch `worktree-claude+<slug>`.
- **`github-ruleset-drift.sh` covers the review gate and the branch fence** (#3598, #3613), including their bypass
  lists.

Rejected along the way:

- A fork-based agent. The repo's interaction limit is `collaborators_only`, and the Actions
  policy holds every external contributor's CI run for approval.
- A deployer gate that refuses agent-merged ranges. A refused merge still sits on `master`,
  where the operator's sessions pull and run it.
- The `renovate-approve` app. It would give a third party approval rights on the repo.

**Check (passed 2026-10-05 on #3622, closed without merging):** as `claude`, a commit showed a good
signature locally and `verified: true` on GitHub. The push to `worktree-claude+slice2-check` was
accepted, and the PR was authored by `DanielClaudeBot`. The merge was refused at once by the CI gate, the
review gate and the branch fence. A push to `fence-probe` was refused with `Cannot create ref due to creations
being restricted`.

**A trap the check found:** the token has no `workflow` scope, and GitHub compares a pushed
branch with the default branch. A branch based on a `master` older than the latest
`.github/workflows/` change is refused as a workflow edit, even when its own commits touch no
workflow. Rebase onto the tip of `master` before pushing.

**Rollback:** delete the review gate and the branch fence, and revoke the token.

## Slice 3: merged work lands through the lander

The operator settled three design points on 2026-10-05:

- The agent gets its own `claude-land@.service`, not a template shared with Renovate's lander.
- The checks and the merge live in `land.sh`, not re-implemented in the lander.
- The approval list includes `scripts/deploy_tools/land*`, because the gate lives in that code.

It landed as four PRs, all deployed on daniel-box on 2026-10-08:

- **3a, #3633: the landing policy in `land.sh`.** The new settings change nothing when unset.
  With `LAND_REQUIRE_AUTHOR` set, every merge path is pinned to the head SHA `land.sh` checked,
  the head branch must carry `LAND_REQUIRE_BRANCH_PREFIX` and target `master`, a PR touching a
  path in `LAND_APPROVAL_PATHS` refuses, and a deployer hold refuses before the merge.
  `--verdict-file` writes `PENDING`, then the final `VERDICT:` or `REFUSED:` line.
- **3b, #3650: the unit.** `claude-land@.service` in `setup/claude_code` runs `land.sh` as
  `ubuntu` with the policy pinned to `DanielClaudeBot`, the `worktree-claude+` prefix and a
  root-owned approval-paths file. A polkit rule lets `claude` start only
  `claude-land@<n>.service`. `scripts/validate/unit_templates.py` node-checks every
  `*.rules.j2`. `claude_code_lander_enabled` switches both on.
- **3c, #3651: the approval exception.** A PR on the approval list lands when the operator's
  latest review approves its head SHA. An approval of an older commit refuses, and so does a
  later `CHANGES_REQUESTED`.
- **3d, #3652: the agent's side.** `land.sh` run as `claude` starts the unit and reports the
  unit's verdict as its own, so the `land-after-merge` skill works unchanged. The hand-off is a
  convenience, not a boundary: an agent that skips it has no privileges to land with. `cc-wait`
  is copied to the agent the way `claude_guard` is.

The approval list holds the paths that widen the agent's own authority.
`claude_code_lander_approval_paths` in `ansible/roles/setup/claude_code/defaults/main.yml` is the
source of truth for the rendered list, with a reason beside each entry. It holds:

- `ansible/roles/setup/claude_code/`, `ansible/roles/setup/renovate_agent/` and
  `ansible/roles/setup/gitops_deploy/`
- `ansible/roles/setup/common/tasks/agent_user.yml` and
  `ansible/roles/setup/common/files/host_lib.py`
- `ansible/roles/setup/initial_setup/tasks/access.yml`
- `ansible/inventory/`, `ansible/.sops.yaml` and `ansible/vars/secrets.yml`
- `.github/`
- `scripts/deploy_tools/land`, a prefix that covers `land.sh`, `land.py`, `land_*.py` and
  `land_lib/`
- `pyproject.toml`, `uv.lock`, `uv.toml` and `.python-version`, which set the interpreter and
  the packages `uv run` syncs before `land.py` starts

The list does not name the modules the landing process imports, such as `scripts/lib/` and the
deployer's helpers. The policy derives that set when it runs, from the modules it has loaded,
and refuses a PR that changes one or adds a file that would shadow one (#3888).
`gate_hits` in `scripts/deploy_tools/land_lib/policy.py` holds the rule.

After each of 3b to 3d, `land.sh` reported `needs-manual-apply` for daniel-server. That run was
skipped on purpose. Both `claude_code_agent_user_enabled` and `claude_code_lander_enabled` are
false there, so the role runs only its removal path, with nothing to remove.

**Check (passed 2026-10-09):** the operator logged in as `claude` and had a session
run `land.sh --pr <n> --arm-merge --await-merge --detach && cc-wait land <n>` on three agent PRs.
Only the operator can log in as `claude`, so only the operator can run this check.

- **A deployable change, #3904: passed.** A comment line in littlelink's deployment template
  landed with `VERDICT: settled`. `/var/lib/claude-land/3904.verdict` ends with the same line,
  and `probe.py health littlelink` exited 0.
- **An approval-list path, #3911: the gate worked, and the landing needed a hand merge.** The
  first run was refused with `need the operator's approval:
  ansible/roles/setup/claude_code/CLAUDE.md`. After the operator approved the head, `land.sh`
  armed auto-merge, because the PR's review decision was `APPROVED`. The branch fence restricts
  updates to master, and auto-merge never applies a bypass, so the PR stayed `BLOCKED` until the
  operator merged it by hand. #3999 merges an approved PR through the REST endpoint instead.
- **A module the gate imports, #3990: passed.** A comment line in `scripts/lib/gh.py` was
  refused with `need the operator's approval: scripts/lib/gh.py`, and the PR was closed without a merge.

- **The approval-list case again, #4025: passed.** After #3999 landed, the same kind of PR was
  refused with `DanielH2018 has not approved it`. Once the operator approved the head, the
  lander logged `merged directly through the ruleset bypass` and ended `VERDICT:
  nothing-to-deploy`, with no hand merge.

**Rollback:** `claude_code_lander_enabled: false` removes the polkit rule and the unit. `claude`
still opens PRs, and the operator lands them.

## Slice 4: the phone host runs as `claude` (done 2026-10-09)

**Decisions (operator, 2026-10-09):**

- **The agent gets a subset of the operator's user-level config.** That subset is the user
  `CLAUDE.md`, the output style, the rules, the skills and the `env` block of `settings.json`
  (operator, 2026-10-09). The `env` copy leaves out `SUDO_ASKPASS`, and the role sets the
  pytest cap from its own variable. The agent's telemetry carries the resource attribute
  `process.owner=claude`, so its metrics carry the label `process_owner="claude"` and the
  operator's carry none. The agent gets no user hooks until each one
  is free of `/home/ubuntu` and `SUDO_ASKPASS`. The role installs the subset root-owned and
  read-only to `claude`, copied from the operator's rendered `~/.claude` on each apply. Root
  ownership stops an injected session from editing those files in place. It does not stop the
  session from replacing them: `claude` owns `~/.claude`, so it can rename a root-owned
  directory and put its own in the place. The next apply restores the copy, and closing the
  gap would need Claude Code's writable state split from the home, which slice 4 does not do.
  The user `CLAUDE.md` is installed as `~/.claude/operator/CLAUDE.md` and imported by the
  agent's own `CLAUDE.md`. `claude_code_agent_operator_config` switches the subset off, and
  `tasks/agent_operator_config.yml` has the mechanics.
- **The memory store is copied once, at the switch-over.** The copy rewrites the
  `/home/ubuntu/server/...` links in `MEMORY.md`. The `ubuntu` store stays in place.
- **`claude` joins `systemd-journal`.** Before the grant, a `gitleaks` scan of a recent journal
  export looks for secrets. Any it finds are fixed at their source first. Read-only `kubectl`
  already exposes pod logs, so the grant adds no new kind of exposure.
- **The switch-over runs in a quiet window,** with no live phone sessions and the operator at
  a terminal for the `/login` and the one-time workspace trust.

**Build, in four PRs:**

- **4a** replaces the `/home/ubuntu` literals in repo tooling with the checkout root or
  `$HOME`. Nothing changes on the host.
- **4b** adds `claude_code_user` (default `sys_user`) and routes `claude-rc.service`'s `User=`,
  `Group=`, `HOME`, `PATH`, `KUBECONFIG`, `ExecStart` and `WorkingDirectory` through it, with
  `claude_code_memory_sync_dir`. `ProtectHome=yes`, `NoNewPrivileges=yes` and `PrivateTmp=yes`
  render only when `claude_code_user` is not `sys_user`. As `ubuntu`, `ProtectHome` would hide
  the unit's own home and checkout, and `NoNewPrivileges` would stop sudo in phone sessions.
  The agent's home sits under `/var/lib`, so after the switch-over `ProtectHome` hides every
  human home without hiding the agent's. `claude_code_login_uid` stays at 1000 until slice 6,
  so interactive sessions as `ubuntu` keep their caps.
- **4c** adds the read grant below, the second artifacts bind mount, the config subset and
  the `systemd-journal` membership. The read grant and the journal group land first, in
  `ansible/roles/setup/claude_code/tasks/agent_access.yml`, each behind its own switch.
- **4d** sets `claude_code_user: claude` in daniel-box's host_vars and copies the memory store.
  The copy is `tasks/agent_memory_seed.yml`. It runs as root while the agent's `MEMORY.md` does
  not exist, so a later apply never overwrites what the agent wrote. It gives the files to the
  agent with group `ubuntu` and mode `0640`, and it strips `/home/ubuntu/server/` from every
  copied `.md` so a link reads `docs/x.md` and resolves in any clone. The `ubuntu` store is not
  touched.
  - `claude-memory-sync` now reads the agent's store and still writes the operator's store on
    daniel-server, where sessions run as `ubuntu`. The target is its own variable,
    `claude_code_memory_sync_target_dir`.
  - The unit gains `UMask=0027`. systemd's default `0022` already leaves group read on a file
    created `0666`, so the line pins that and removes the "other" bits. It cannot widen a file
    Claude Code creates `0600`, which the check below tests.

**4d switch-over, after the merge:**

1. Wait for the deployer to apply `claude_code`. The change touches `ansible/inventory/` too, so
   the same tick then runs a full `deploy.yml`. The `claude_code` apply restarts `claude-rc.service` as
   `claude`, which drops live phone sessions. Until step 2 finishes, the agent has no login.
1. Log in once as the agent:

    ```bash
    sudo machinectl shell claude@ /bin/bash -l
    cd ~/server
    claude
    ```

    Run `/login`, trust the folder, then exit. The interactive session leaves a
    `claude daemon run --origin transient` process behind in the login scope. On 2026-10-09 the
    host spawned no phone session while that daemon ran. The service stayed `active` and logged
    nothing, so nothing reported the failure. The host started spawning once the daemon was
    killed, with the unit's hardening unchanged. A daemon is not a problem in itself: later
    that day a `claude-agents` session's daemon ran as `claude` beside the host, and the phone
    still connected. So the daemon `/login` leaves is the one to kill. The cause is unknown. Kill
    it, then restart the host:

    ```bash
    sudo pkill -u claude -f 'claude daemon run'
    sudo systemctl restart claude-rc.service
    ```

1. Run the check below.

**Read grant:** `ubuntu` reads the agent's artifacts and memory through one grant in the role.
`claude-memory-sync` runs as `sys_user`, and the artifacts tree is the operator's to read, so
both need it. The artifacts pod may not need it. Its template sets no `securityContext`, so the
pod runs as root with the default capability set of the container runtime, which includes
`CAP_DAC_OVERRIDE`. The slice 4 check tests that read anyway, because a later hardening of the
pod would drop the capability.

- The `~/.claude` item in `agent_github.yml` changes from `0700` to `0710`. Group `ubuntu`
  gets traverse but not list. The grant relies on `.credentials.json` staying `0600`.
- `~/.claude/projects` and `~/.claude/projects/<key>` get group `ubuntu` and `0710` too.
  Claude Code creates them at a mode nobody has checked, and the memory directory sits
  under both.
- A task creates `~/.claude/artifacts` and the memory directory as root, owned by the agent,
  `setgid`, with group `ubuntu`. It cannot run as the agent, because the agent belongs to no
  group and so cannot `chgrp` to `ubuntu`. The ACL task runs as root for the same reason. It gives each a default ACL of `g:ubuntu:rX`, so files Claude Code writes
  later inherit the read.
- The ACL task runs after every `file:` task that names those paths or their parents. A `mode:` there runs
  `chmod`, and `chmod` rewrites the ACL mask from the group bits. `0700` sets the mask to
  `---`, which disables every named entry while `getfacl` still lists it.
- A default ACL cannot widen a file's create mode. A file Claude Code creates `0600` gets
  a mask of `---`, so `ubuntu` cannot read it. The check below reads a new file as `ubuntu`
  for that reason, rather than reading `getfacl`.

**Couplings to change:**

| Coupling | Where |
|---|---|
| `HOME`, `PATH`, `KUBECONFIG`, `ExecStart`, `WorkingDirectory` through `sys_user` | `ansible/roles/setup/claude_code/templates/claude-rc.service.j2` |
| `claude_code_login_uid` and `claude_code_login_uids`, which drive the login-slice caps | `ansible/roles/setup/claude_code/defaults/main.yml` |
| `claude_code_memory_sync_dir` and the path-derived project key | `ansible/roles/setup/claude_code/defaults/main.yml` |
| `artifacts_host_dir` and its bind mount | `ansible/roles/k8s/artifacts/defaults/main.yml` |
| Hooks that hard-code `/home/ubuntu`, and `SUDO_ASKPASS` | the dotfiles repo's `~/.claude` sources |
| `/home/ubuntu` literals in repo tooling | `scripts/dev/fanout_lib/launch.py`, `scripts/deploy_tools/land_lib/options.py`, `scripts/deploy_tools/land_reach.py` and about 16 more non-test files |
| The SessionStart banner's other-session list | It reads `git worktree list`, so it shows only sessions in the same clone |

**Check:** create a session from the phone, and ask it to do three things.

1. Print `id`. It must show `claude`.
1. Say what `MEMORY.md` lists. A session that loads it names entries from the copied store.
1. Write an artifact and a new memory file. The artifact's link must render under
   `/a/daniel-box-claude/`, which proves the artifacts pod can traverse to the file.

Then, as `ubuntu`, `cat` the new artifact and the new memory file in
`/var/lib/claude/.claude/projects/-var-lib-claude-server/memory/`. A permission error on the
memory file means Claude Code created it `0600`, which `UMask=0027` cannot widen. In that case
`claude-memory-sync` cannot read it either, so file a finding with `findings.py open`. Last,
`journalctl -u claude-memory-sync` must show a run that copied to daniel-server, and the new
file must appear in `/home/ubuntu/.claude/projects/-home-ubuntu-server/memory/` there.

**Passed 2026-10-09.** A phone session printed `claude` for `id` and named the copied
`MEMORY.md` entries. Its artifact rendered at `/a/daniel-box-claude/`. It saved `rc-check.md`,
and Claude Code created that file `-rw-r----- claude:ubuntu`, so `ubuntu` read it. The 19:18 UTC
`claude-memory-sync` run copied the file and the new `MEMORY.md` line to daniel-server.

**Rollback:** `claude_code_user: ubuntu`. A `User=` change restarts the host and drops its live
sessions.

## Slice 5: peers, and the tools that decrypt SOPS (homelab-ui login built; daniel-server agent built for fan-out)

**Peer users: dropped (operator decision, 2026-10-09).** The plan was a no-sudo `claude` user
on daniel-server and daniel-pi for `probe.py`'s ssh paths. #4057 built it, and the build
showed the premise does not hold. `probe.py` has one ssh path, a Docker inspection on the
Pi. That path needs the `docker` group, which is root-equivalent, and it returns every
container's environment, secrets included. A no-sudo peer user would add an ssh login on two
hosts and unlock nothing, so #4057 was closed without a merge. The Pi container checks stay
operator-run. Peer users return only with a concrete need, such as a read-only container
listing that strips the environment.

**Built, 2026-10-09: an agent user on daniel-server, for fan-out placement (#4098).** Fan-out
is the concrete need. Run as `claude`, `scripts/dev/fanout_place.py` could place batches on
daniel-box only, because it had no login on daniel-server. The operator chose this over an ssh
login as `ubuntu`, whose shell has sudo. #4057's bare login does not serve fan-out: a batch
needs a clone, `uv`, Claude Code, a GitHub identity and a signing key. So daniel-server gets
the full agent user, and ssh access is added to it.

- **daniel-server.** `claude_code_agent_user_enabled: true` builds `claude` as on daniel-box,
  minus the lander, `claude-rc.service`, the memory copy and the homelab-ui browser and login.
  `claude_agent_ssh_login_enabled: true` adds `ssh-users` to the agent's exact group list
  (`agent_access.yml`) and writes a root-owned `authorized_keys` holding daniel-box's agent key
  with `restrict` (`agent_ssh_login.yml`). The play reads that key over ssh as the operator.
- **daniel-box.** `agent_peers.yml` generates the agent's peer key once, as the agent, at
  `claude_agent_ssh_key_path`, and renders `~/.ssh/config` with one stanza per peer whose
  switch is on: `User claude`, the peer's `server_ip`, `IdentitiesOnly yes` and
  `StrictHostKeyChecking accept-new`. Both PRs carry that `accept-new` decision.
- **Both hosts.** The agent user lingers, so a batch started with `systemd-run --user` over
  ssh outlives the call that started it.
- **The Pi** gets nothing. The `docker`-group reasoning above still holds for it.

The apply is the operator's, in this order:

1. On daniel-box, `uv run ansible-playbook ansible/initial_setup.yml --tags claude_code`. This
   generates the peer key.
2. On daniel-server, the same command. This builds the agent user and reads the key.
3. On daniel-server, one interactive `/login` as `claude`
   (`sudo machinectl shell claude@ /bin/bash -l`, then `claude`).
4. Register daniel-server's `~claude/.ssh/git_signing_ed25519.pub` on the agent's GitHub
   account as a Signing Key. `fanout_place.py launch` refuses a host whose key is not
   registered.

**Check:** as `claude` on daniel-box, `uv run python scripts/dev/fanout_place.py read` prints a
reading for both hosts, each with `signing=ok`.

**Rollback:** `claude_agent_ssh_login_enabled: false` in daniel-server's host_vars removes the
key and the group. `claude_code_agent_user_enabled: false` there also expires the account and
stops its linger.

`scripts/z2m/set_device_option.sh` and `ansible/roles/k8s/qbittorrent/files/apply_prefs.py`
stay operator-run.

**Built, 2026-10-09: the homelab-ui login.** The MCP server logs in as its own Authelia user,
`claude-agent`, instead of the operator.

- The password is `authelia_agent_password` in SOPS, so `secret_rotation.yml` tracks it (tier
  `assisted`, like `claude-ui`'s). The operator decided on 2026-10-09 that it stays in SOPS.
  The `authelia` role hashes it at deploy time, with the same username-keyed read-back as the
  other two users.
- The `claude_code` role renders `/var/lib/claude/.config/homelab-ui/credentials.json` with the
  username, the password and the domain. The file is `0600`, owned by `claude`, and written
  under `no_log`. Ansible decrypts the three values as the operator, so the agent still never
  holds the age key.
- The file carries the domain because `ui_mcp.sh` and `ui_login.py` read it from SOPS for the
  operator. The agent can already read the domain from the IngressRoutes its read-only
  kubeconfig covers.
- `ui_login.py` logs in from that file when it exists and from SOPS otherwise, so the
  operator's login is unchanged. `ui_mcp.sh` takes the domain from `ui_login.py --domain`.
- The account is in no group. Grafana maps `admins` to Admin and Headlamp binds `oidc:admins`
  to cluster read access. `claude-ui` copies the operator's groups on purpose and keeps both.
- The `access_control` rules are domain-scoped, never `subject:`-scoped, so a group cannot
  narrow what the account reaches. It reaches every `one_factor` `*.local.<domain>` route from
  an RFC1918 address, the set the operator's own UI session reaches. It has no TOTP
  registration, so it cannot open the four `two_factor` routes (code-server, n8n, longhorn and
  deploy-ui). It has no public name.
- To switch it off, set `claude_code_agent_user_enabled: false`, which removes the file. To
  revoke the account, delete the `claude-agent` block from the authelia role's
  `users_database.yml` template and redeploy. To rotate the password, run `sops` set, then
  `./scripts/deploy.sh --tags authelia -e authelia_k8s_rehash_passwords=true`, then apply
  `claude_code` to rewrite the file.

**Built, not yet applied: the agent's own browser (#4058).** The operator's Node,
`playwright-mcp` and Chromium sit under `/home/ubuntu`, which `claude` cannot read, and the
operator's homelab-ui registration is user-scope in their own `~/.claude.json`.
`claude_code_agent_homelab_ui_enabled` (true in daniel-box's host_vars) has the `claude_code`
role give the agent its own copies, in `tasks/agent_browser.yml`:

- The role installs a pinned Node tarball under `~/.local/share/node`, `@playwright/mcp` at the
  operator's release, and the Chromium that release's playwright pins. Each install runs as
  `claude`.
- The role registers `homelab-ui` at user scope in the agent's `~/.claude.json`. The entry
  launches `~/server/scripts/diagnostics/ui_mcp.sh` with `NODE_BIN` set to the agent's Node.
  The registration is not in the repo's `.mcp.json`: a project-scope server waits for an
  approval a phone session cannot give, and it would start a second Chromium in every
  operator session.
- To switch it off, set the switch false. The role then removes the registration, the Node
  tree and `~/.cache/ms-playwright`.

**Check:** homelab-ui renders a service page as `claude`. To check the login half alone, run
`uv run python scripts/diagnostics/ui_login.py && uv run python scripts/diagnostics/ui_login.py --verify homepage`
as `claude`.

**Rollback:** set `claude_code_agent_homelab_ui_enabled: false`, then switch the agent user off
and delete the `claude-agent` block.

## Slice 6: retire Claude sessions as `ubuntu` (done 2026-10-09)

**Decision, 2026-10-09:** new interactive sessions start as `claude` through the dotfiles
function `claude-agents`, which runs
`sudo machinectl shell claude@ /bin/bash -lc 'cd ~/server && exec claude agents "$@"'`. Current
sessions stay on `ubuntu` (uid 1000) until they end. A `claude-agents` session runs in
`user.slice/user-996.slice/session-<n>.scope` beside `claude-rc.service`, and the phone host
still connects.

**Built (caps for both users):** the login-slice caps and the pytest fan-out cap render for every
user ID in `claude_code_login_uids`, which defaults to `[claude_code_login_uid]` (1000). On a host
with `claude_code_agent_user_enabled`, `tasks/login_caps.yml` adds the agent's ID, looked up from
the user database and never written down. Sessions started as either account carry the same
`MemoryHigh`, `MemorySwapMax` and `PYTEST_XDIST_AUTO_NUM_WORKERS` as `claude-rc.service`, under
the fleet bound on `user.slice`. An ID that leaves the list has its slice drop-in and
`environment.d` file removed on the next apply, and `claude_code_login_caps_enabled: false`
removes every one.

**Built when the operator retired the `ubuntu` sessions (2026-10-09):**

- `claude-cgroup-metrics.sh` writes `claude_uid_processes{uid}` for each uid in
  `claude_code_watch_uids`, an explicit 0 included. It counts processes named `claude` by their
  real uid in `/proc/<pid>/status`. daniel-box watches uid 1000. The list defaults to empty, so
  daniel-server, where sessions still run as `ubuntu`, writes no series.
- monitor-bridge's Memory tile pages while any watched uid runs `claude`, after the cgroup
  arm's two-cycle streak. The message names the host and the uid.
- daniel-box sets `claude_code_login_uids: []`, which removes the caps from `user-1000.slice`.
  The fleet bound on `user.slice` still covers it.

A root-owned `/etc/claude-code/managed-settings.json` is deferred (operator decision,
2026-10-09). It binds every user on the host, `renovate-agent` included. Slice 4 already makes
the agent's user-level config root-owned, so revisit it after the switch-over, and only for
settings that should bind both agents.

**The inventory stays private (operator decision, 2026-10-09).** The plan was to publish it
here once this slice landed, on the premise that it would then describe history. That holds
only for Claude on daniel-box. Most of its rows describe the `ubuntu` account itself, which
keeps every one of them, and daniel-server still runs Claude as `ubuntu`.
[What this page leaves out](#what-this-page-leaves-out) has where it lives.

**Check:** starting `claude` as `ubuntu` shows up in the metric within one 30-second timer
interval. Passed on 2026-10-09: the session that built the slice and a leftover
`claude daemon run`, both uid 1000, read as `claude_uid_processes{uid="1000"} 2`. The first
deploy also read a false 0 on some runs, because gawk aborts on a status file whose process
has exited; #4096 reads the files through `cat` instead.

**Rollback:** delete the two lines in daniel-box's host_vars. `claude_code_login_uids` returns
to uid 1000, and an empty `claude_code_watch_uids` stops the series and the page with it.

## Slice 7: a pre-merge dry run without secrets (optional)

**Build:** add a `deploy.sh --dry-run` mode that renders with placeholder secret values and
applies with a dry-run-only ServiceAccount. A ValidatingAdmissionPolicy denies that
ServiceAccount every request whose `request.dryRun` is false.

**Unverified:** whether this k3s version's admission CEL exposes `request.dryRun`, and whether
the dry-run path's kubectl calls can run without `become`.

## What the `claude` user cannot do

| Capability | Why | Where it goes instead |
|---|---|---|
| `deploy.sh`, `--check` and `--dry-run` before a merge | All three decrypt SOPS, and the agent has no age key | Merged work deploys through the lander (slice 3). A pre-merge dry run returns with slice 7. |
| `sudo` | No sudo rights and no `SUDO_ASKPASS` helper | The operator, or the deployer after a merge |
| Adding or rotating a secret | `sops` needs the data key to write a value | The operator. Key names stay readable, because SOPS encrypts values only. |
| ssh as `ubuntu` to the peers | That shell has sudo | The operator. The agent reaches daniel-server as its own `claude` agent user, for fan-out only (slice 5). The Pi has no agent login, because the one `probe.py` ssh path needs its `docker` group. |
| The system journal, until slice 4 | No `adm` or `systemd-journal` membership | Its own units only. Verdict files from the lander replace the journal for landing. Slice 4 adds `systemd-journal`. |
| Edits to `.github/workflows/` | The token has no `workflow` scope | The operator, or Renovate's own app |

## The decision: a visible trail, not a merge gate

The operator decided on 2026-10-04. `claude` self-merges through the lander, as the root
`CLAUDE.md` expects of every session. The operator's approval is required only on the approval
list above, the paths that widen the agent's own authority. The re-check in the lander enforces that
gate and merges pinned to the head SHA it checked.

The rejected alternative was a human approval on every merge. Merged code deploys, so the
agent's uid does not bound what a merged change can do, and only a review of every merge would.
That gate would end unattended landing, including phone sessions that merge and deploy on their
own. The operator judged a trail of signed PRs by a distinct identity, CI runs and lander records
to be enough.

**How to apply:** an implementation follows the slices in order. It never gives `claude` the age
key, because SOPS holds the become password, and never gives it the operator's `gh` token.

## Alternatives rejected for the uid split itself

| Option | Why not |
|---|---|
| Stay as `ubuntu` and enable `/sandbox` | The sandbox covers Bash only. Read, Edit, hooks and MCP servers run with the session's full access. It can be layered on later; it does not replace a uid boundary. |
| A container or VM | The agent's work is host introspection: `journalctl`, the cgroup filesystem, `systemctl show`, `/run/lock`, `kubectl`. Bind-mounting those back in erodes the boundary at a higher maintenance cost. |
| Share the primary checkout through group permissions | git checks the owner uid, not the group. A shared `.git/config` lets either user run code as the other. |
| Give `claude` its own age recipient | SOPS holds the become password, so the key is root. |
| A `claude` user holding the operator's `gh` token | The operator cannot approve their own PRs, so no approval gate could bind that token. |

## What this page leaves out

The plan artifact also holds a per-path inventory of what a session reaches as `ubuntu`. This
page publishes the reasoning built on it, in [Why a separate user](#why-a-separate-user). The
inventory stays private, because the repo is public and the inventory describes the `ubuntu`
account as it still is. The operator decided this split on 2026-10-09 (#3685), and decided
the same day, when slice 6 landed, to keep it private rather than publish it.

The durable home of the inventory is the operator's break-glass kit in the password manager,
which holds a copy of `~/.claude/artifacts/pinned/claude-unix-user-plan_2026-10-04.html` since
2026-10-09. The pinned copy on daniel-box is a convenience only: `prune-artifacts.sh` skips
`pinned/`, but nothing backs it up.
