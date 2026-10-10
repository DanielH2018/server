# Claude as its own UNIX user on daniel-box

Claude sessions on daniel-box run as a dedicated `claude` UNIX user instead of `ubuntu`. The
operator approved the design on 2026-10-04. This page describes the design that runs, the rules
it follows, and the decisions behind it. It replaces the `claude-unix-user-plan_2026-10-04.html`
artifact as the durable copy, because `prune-artifacts.sh` deletes an artifact 30 days after its
last update (#3657).

One part of the design stays out of this page. The repo is public, and the per-path inventory of
what a session reaches as `ubuntu` stays private. See
[What this page leaves out](#what-this-page-leaves-out).

## Status

| Slice | What it delivers | State |
|---|---|---|
| 1 | A read-only `claude` user an operator can open a session as | Done |
| 2 | Claude opens PRs under its own GitHub identity | Done |
| 3 | Merged agent work lands and deploys through a lander unit | Done |
| 4 | The phone host `claude-rc.service` runs as `claude` | Done |
| 5 | The homelab-ui login, the agent's browser, and a fan-out agent on daniel-server | Built. Peer users were dropped. |
| 6 | Retire Claude sessions as `ubuntu` | New sessions start as `claude`, and the caps render for both users. The operator still owns dropping uid 1000. |
| 7 | A pre-merge dry run without secrets | Optional, planned |

Each slice ends in a check that can be run and a variable that turns it back off.
[Checking and rolling back](#checking-and-rolling-back) lists both.

## Why a separate user

A Claude session acts on text it reads: issues, PR comments, web pages, logs and tool output.
Any of that text can carry an injected instruction. The design assumes that some session
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

Only a human approval on every merge closes that path. The operator rejected that gate, for the
reasons in
[The decision: a visible trail, not a merge gate](#the-decision-a-visible-trail-not-a-merge-gate).
The split makes the path visible instead. Every change that reaches root needs a signed commit by
`DanielClaudeBot`, a CI run and a lander record.

The slice 3 gate has limits of its own. It requires the operator's approval only for the paths
on its list and the modules the landing process imports. Code that runs only after the checks,
such as the deploy itself, falls under the root chain above.

## The template: `renovate-agent`

The repo already ran one Claude agent as a separate user. The `renovate_agent` setup role
creates `renovate-agent` with its own home under `/var/lib`, its own clone, its own copies of
`uv` and `claude_guard`, a hardened unit, and a GitHub token that cannot push. A polkit rule lets
it start one lander unit, which runs root-owned code as `ubuntu` and re-checks the PR.

The `claude` agent reuses that shape. The identity tasks live in the shared
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
- **The agent belongs to none of `ubuntu`, `sudo`, `adm`, `docker`, `lxd` or `kvm`.** Its
  groups are `systemd-journal`, `worktree-holders` and, on a peer, `ssh-users`. Without
  `adm` or `systemd-journal`, `journalctl` shows the agent only its own units. The operator
  granted `systemd-journal` after a secrets scan of the journal (`claude_code_agent_journal_access`).
- **The agent has no sudo grant. Its one root service is the worktree holder scan.** The
  operator granted the scan on 2026-10-10 (#4021) and ruled the same day that a socket replaces
  the sudo grant it first used (#4297). Under `NoNewPrivileges=yes`, sudo cannot gain root, so
  no `claude-rc.service` session could use that grant. `worktree-holders.socket` listens on
  `/run/worktree-holders.sock`, mode 0660, group `worktree-holders`. For each connection
  systemd runs `/usr/local/libexec/worktree-holders` as root. The helper reads the caller's uid
  from `SO_PEERCRED` and reports only under that user's root in `/etc/worktree-holders.json`.
  For the agent, that root is its own clone, so a worktree removal in `/var/lib/claude/server`
  sees an `ubuntu` process inside the tree. initial_setup's `worktree-sweep` tag renders the
  map from every present entry of `claude_code_agents` (#4295), and the helper refuses a uid
  the map does not name. The `claude_code` role's `agent_access.yml` puts each present agent in
  the group. A session started before its user joined the group falls back to the
  unprivileged scan until it restarts. `sudo -n true` still fails.
- **The agent has its own GitHub identity and signing key.** A PR author cannot approve their
  own PR, so any approval gate needs a second identity.
- **Remote Control needs one interactive step.** It requires a claude.ai `/login`, and without a
  TTY it exits with `Workspace not trusted`, so the agent user trusts its working directory once
  by hand.

## Slice 1: a read-only `claude` user

The `claude` user has home `/var/lib/claude`, and `claude_code_agent_user_enabled` in
daniel-box's host_vars switches it on. The operator enters as `claude` with
`sudo machinectl shell claude@ /bin/bash -l`. The user has no `ssh-users` membership and no
authorized key.

- **The clone stays current.** The `claude_code` role fast-forwards `~/server` as `claude` on each
  apply, before its `uv sync`, and moves only a clean `master`. The deployer routes no change to
  `ui_mcp.sh`, `ui_login.py` or `.claude/hooks/` to `claude_code`, so `claude-clone-sync.timer`
  runs the same fast-forward every `claude_code_agent_clone_sync_interval` (15 minutes). The
  script is `ansible/roles/setup/claude_code/files/claude-clone-sync.sh`, and it re-syncs the
  `.venv` and the collections when a pull moves their lock files. The hook shim and the
  homelab-ui launcher run from that clone, so a stale clone breaks both.
- **The agent has commit hooks.** The shared `agent_user.yml` copies the operator's pinned `prek`
  and `uvx` beside `uv`. The `claude_code` role runs `prek install` in the clone, which covers
  every worktree made from it, so the agent's commits run gitleaks and the commit-time ratchets.
  The role installs the pinned Ansible collections into the clone, and the `.profile` sets
  `ANSIBLE_COLLECTIONS_PATH` so a worktree falls back to them. `renovate-agent` gets the binary
  but no hooks.
- **`~/.claude` is `drwx------ claude:ubuntu`.** The role sets that mode on every apply, in
  `tasks/agent_github.yml`, because the directory holds the session's login. Slice 4 widens it
  for the operator's group.
- **A detached deploy logs per user.** The log directory is `/tmp/homelab-deploy-logs-<user>`, so
  one user's directory never blocks the other's `deploy.sh --detach`.
- **The Claude Code installer runs with `CLAUDE_INSTALL_ALLOW_SUDO=1`**, because the installer
  refuses to run under a sudo guard otherwise.

To take the commit hooks off the agent, run `prek uninstall` as `claude` in `~/server`. Removing
the hook-install task does not remove the hook. A broken hook refuses every agent commit, and
`--no-verify` is denied at the permission layer, so `prek uninstall` is the only way past one.

## Slice 2: Claude opens PRs under its own GitHub identity

- **A machine user account, not a GitHub App.** The account is `DanielClaudeBot`, a write
  collaborator, because a personal-account repo has no lower role.
- **Its token is a classic `public_repo` token.** GitHub does not support fine-grained tokens
  for a collaborator on another personal account's repo. The token is stored in SOPS as
  `claude_code_agent_gh_token`, so `ansible/secret_rotation.yml` tracks it.
- **The review requirement is its own ruleset,** "master review gate," with repository admin and
  the Renovate app as bypass actors. The "master CI gate" ruleset has no bypass actors, and adding
  one there would also exempt the operator from CI. The review gate sets
  `require_last_push_approval`, because GitHub stops only a PR's author from approving it.
- **`land.sh` merges directly when a PR is blocked only by the missing review.** GitHub
  auto-merge ignores a ruleset bypass, while a direct merge honours it. So `land.sh --arm-merge`
  waits for CI, then merges through the REST endpoint, pinned to the head SHA it checked. The
  branch fence below also restricts updates to master whatever the review decision, so every
  agent PR takes that path and `land.sh` arms no auto-merge at all.
- **Renovate's `platformAutomerge` is off,** for the same reason. Renovate's own direct merge
  uses its bypass. Automerges therefore land on Renovate's next run rather than the moment CI
  passes.
- **The "agent branch fence" ruleset restricts branch writes.** It restricts creating, updating
  and deleting every branch except `refs/heads/worktree-claude+**`. The admin role and Renovate
  bypass it. The "Default" ruleset excludes that prefix too, so the agent can force-push and
  delete its own branches. `EnterWorktree` turns `/` in a worktree name into `+`, so a
  `claude/<slug>` worktree gets the branch `worktree-claude+<slug>`.
- **`github-ruleset-drift.sh` covers the review gate and the branch fence,** including their
  bypass lists.

Rejected alternatives:

- A fork-based agent. The repo's interaction limit is `collaborators_only`, and the Actions
  policy holds every external contributor's CI run for approval.
- A deployer gate that refuses agent-merged ranges. A refused merge still sits on `master`,
  where the operator's sessions pull and run it.
- The `renovate-approve` app. It would give a third party approval rights on the repo.

**A trap:** the token has no `workflow` scope, and GitHub compares a pushed branch with the
default branch. A branch based on a `master` older than the latest `.github/workflows/` change is
refused as a workflow edit, even when its own commits touch no workflow. Rebase onto the tip of
`master` before pushing.

## Slice 3: merged work lands through the lander

The agent has its own `claude-land@.service`, not a template shared with Renovate's lander. The
checks and the merge live in `land.sh`, and the lander does not re-implement them.

- **The landing policy lives in `land.sh`.** The settings change nothing when unset. With
  `LAND_REQUIRE_AUTHOR` set, every merge path is pinned to the head SHA `land.sh` checked, the
  head branch must carry `LAND_REQUIRE_BRANCH_PREFIX` and target `master`, a PR touching a path
  in `LAND_APPROVAL_PATHS` refuses, and a deployer hold refuses before the merge.
  `--verdict-file` writes `PENDING`, then the final `VERDICT:` or `REFUSED:` line.
- **The unit** `claude-land@.service` in `setup/claude_code` runs `land.sh` as `ubuntu` with the
  policy pinned to `DanielClaudeBot`, the `worktree-claude+` prefix and a root-owned
  approval-paths file. A polkit rule lets `claude` start only `claude-land@<n>.service`.
  `scripts/validate/unit_templates.py` node-checks every `*.rules.j2`.
  `claude_code_lander_enabled` switches the unit and the rule on.
- **An approval-list PR lands when the operator's latest review approves its head SHA.** An
  approval of an older commit refuses, and so does a later `CHANGES_REQUESTED`.
- **The agent's side.** `land.sh` run as `claude` starts the unit and reports the unit's verdict
  as its own, so the `land-after-merge` skill works unchanged. The hand-off is a convenience, not
  a boundary: an agent that skips it has no privileges to land with. `cc-wait` is copied to the
  agent the way `claude_guard` is.

### The approval floor

The approval list is a floor. It keeps the explicit grant points and the gate itself. The agent's
own roles (`claude_code`, `renovate_agent`), the deployer (`gitops_deploy`), `agent_user.yml`,
`host_lib.py` and `ansible/inventory/` are not on it, so a PR changing them lands without the
operator.

`ansible/roles/setup/claude_code/templates/claude-land-approval-paths.j2` is the source of
truth, with a reason beside each entry. It holds:

- the lander itself: `tasks/lander.yml`, the list template, the unit template and the polkit
  rule template, all under `ansible/roles/setup/claude_code/`
- `scripts/deploy_tools/land`, a prefix that covers `land.sh`, `land.py`, `land_*.py` and
  `land_lib/`
- `pyproject.toml`, `uv.lock`, `uv.toml` and `.python-version`, which set the interpreter and
  the packages `uv run` syncs before `land.py` starts
- `.github/`
- `ansible/roles/setup/initial_setup/tasks/access.yml`, which holds sudo, ssh and the
  operator's login
- `ansible/.sops.yaml` and `ansible/vars/secrets.yml`

Two choices keep the floor from being bypassed through the inventory:

- The list is literal in the template, not a variable.
- `tasks/lander.yml` pins the unit's checkout, branch prefix, required author and approver,
  and the Unix user the polkit rule admits, as block vars. Block vars outrank `host_vars` and
  `group_vars`. Each pin is a literal. A pin that templated the agent profile's variables
  resolved from the inventory and pinned nothing.

**The floor is not a barrier to root.** The deployer applies any setup-role change as root, so
a task added to an unlisted role reaches root without approval. A true root barrier would put
`ansible/roles/setup/` itself on the list.

The list does not name the modules the landing process imports, such as `scripts/lib/` and the
deployer's helpers. The policy derives that set when it runs, from the modules it has loaded,
and refuses a PR that changes one or adds a file that would shadow one. It also refuses any
compiled module file: a `__pycache__/` path, a `.pyc` or an extension module. Python loads a
`.pyc` file in place of unchanged source and an extension module before the `.py` beside it, and
a loaded module's `__file__` names the source either way, so the loaded set cannot see them.
`gate_hits` in `scripts/deploy_tools/land_lib/policy.py` holds the rule, and the
`no-compiled-module-file-is-tracked` row of `ansible/tests/repo/test_census_rows_text.py`
refuses such a file on any branch.

## Slice 4: the phone host runs as `claude`

`claude_code_user` (default `sys_user`) names the account `claude-rc.service` runs as, and
daniel-box sets it to the agent. The variable routes the unit's `User=`, `Group=`, `HOME`,
`PATH`, `KUBECONFIG`, `ExecStart` and `WorkingDirectory`, and `claude_code_memory_sync_dir`.

- **Hardening renders only for a non-operator user.** `ProtectHome=yes`, `NoNewPrivileges=yes`
  and `PrivateTmp=yes` render when `claude_code_user` is not `sys_user`. As `ubuntu`,
  `ProtectHome` would hide the unit's own home and checkout, and `NoNewPrivileges` would stop sudo
  in phone sessions. The agent's home sits under `/var/lib`, so `ProtectHome` hides every human
  home without hiding the agent's.
- **The agent takes the operator's user-level config through chezmoi.** [The operator's
  dotfiles](#the-operators-dotfiles) describes it. `claude_code_agent_operator_config` switches
  the older copied subset off, and `tasks/agent_operator_config.yml` has its mechanics. The
  agent's telemetry carries the resource attribute `process.owner=claude`, so its metrics carry
  the label `process_owner="claude"` and the operator's carry none.
- **The memory store is copied once.** `tasks/agent_memory_seed.yml` runs as root while the
  agent's `MEMORY.md` does not exist, so a later apply never overwrites what the agent wrote. It
  gives the files to the agent with group `ubuntu` and mode `0640`, and it strips
  `/home/ubuntu/server/` from every copied `.md` so a link reads `docs/x.md` and resolves in any
  clone. The `ubuntu` store is not touched.
- **`claude-memory-sync` reads the agent's store** and writes the operator's store on
  daniel-server, where sessions run as `ubuntu`. The target is
  `claude_code_memory_sync_target_dir`. The unit sets `UMask=0027`, which pins group read and
  removes the "other" bits. It cannot widen a file Claude Code creates `0600`.
- **`claude` joins `systemd-journal`,** per the rules above.
- **`claude_code_login_uid` stays at 1000** until slice 6 finishes, so interactive sessions as
  `ubuntu` keep their caps.

**The first login.** A host switch-over drops live phone sessions and leaves the agent without a
login. Log in once as the agent, in a quiet window:

```bash
sudo machinectl shell claude@ /bin/bash -l
cd ~/server
claude
```

Run `/login`, trust the folder, then exit. The interactive session leaves a
`claude daemon run --origin transient` process behind in the login scope. While that daemon
runs, the host can spawn no phone session, yet the service stays `active` and logs nothing. Kill
the daemon, then restart the host:

```bash
sudo pkill -u claude -f 'claude daemon run'
sudo systemctl restart claude-rc.service
```

A daemon is not a problem in itself: a `claude-agents` session's daemon runs as `claude` beside
the host, and the phone still connects. The cause of the one `/login` leaves is unknown.

### The read grant

`ubuntu` reads the agent's artifacts and memory through one grant in the role. `claude-memory-sync`
runs as `sys_user`, and the artifacts tree is the operator's to read, so both need it. The
artifacts pod may not need it, because its template sets no `securityContext` and the pod runs as
root with `CAP_DAC_OVERRIDE`. The slice 4 check tests that read anyway, because a later hardening
of the pod would drop the capability.

- The `~/.claude` item in `agent_github.yml` is mode `0710`. Group `ubuntu` gets traverse but not
  list. The grant relies on `.credentials.json` staying `0600`.
- `~/.claude/projects` and `~/.claude/projects/<key>` get group `ubuntu` and `0710` too. The
  memory directory sits under both.
- A task creates `~/.claude/artifacts` and the memory directory as root, owned by the agent,
  `setgid`, with group `ubuntu`. It cannot run as the agent, because the agent belongs to no
  group and so cannot `chgrp` to `ubuntu`. The ACL task runs as root for the same reason. It gives
  each a default ACL of `g:ubuntu:rX`, so files Claude Code writes later inherit the read.
- The ACL task runs after every `file:` task that names those paths or their parents. A `mode:`
  there runs `chmod`, and `chmod` rewrites the ACL mask from the group bits. `0700` sets the mask
  to `---`, which disables every named entry while `getfacl` still lists it.
- A default ACL cannot widen a file's create mode. A file Claude Code creates `0600` gets a mask
  of `---`, so `ubuntu` cannot read it. The check reads a new file as `ubuntu` for that reason,
  rather than reading `getfacl`.

### Couplings

| Coupling | Where |
|---|---|
| `HOME`, `PATH`, `KUBECONFIG`, `ExecStart`, `WorkingDirectory` through `sys_user` | `ansible/roles/setup/claude_code/templates/claude-rc.service.j2` |
| `claude_code_login_uid` and `claude_code_login_uids`, which drive the login-slice caps | `ansible/roles/setup/claude_code/defaults/main.yml` |
| `claude_code_memory_sync_dir` and the path-derived project key | `ansible/roles/setup/claude_code/defaults/main.yml` |
| `artifacts_host_dir` and its bind mount | `ansible/roles/k8s/artifacts/defaults/main.yml` |
| Hooks that hard-code `/home/ubuntu`, and `SUDO_ASKPASS` | the dotfiles repo's `~/.claude` sources |
| `/home/ubuntu` literals in repo tooling | the non-test files `git grep -l /home/ubuntu -- '*.py' '*.sh' '*.j2'` lists outside `tests/` (8 on 2026-10-10) |
| The SessionStart banner's other-session list | It reads `git worktree list`, so it shows only sessions in the same clone |

## Slice 5: the homelab-ui login, the agent's browser, and a fan-out agent

**Peer users are dropped.** The first design was a no-sudo `claude` user on daniel-server and
daniel-pi for `probe.py`'s ssh paths. `probe.py` has one ssh path, a Docker inspection on the Pi.
That path needs the `docker` group, which is root-equivalent, and it returns every container's
environment, secrets included. A no-sudo peer user would add an ssh login on two hosts and unlock
nothing, so the operator dropped it. The Pi container checks stay operator-run. Peer users return
only with a concrete need, such as a read-only container listing that strips the environment.

**A fan-out agent on daniel-server.** Run as `claude`, `scripts/dev/fanout_place.py` places
batches on daniel-box only unless the agent has a login on daniel-server. The operator chose a
full agent user there over an ssh login as `ubuntu`, whose shell has sudo, because a batch needs
a clone, `uv`, Claude Code, a GitHub identity and a signing key.

- **daniel-server.** `claude_code_agent_user_enabled: true` builds `claude` as on daniel-box,
  minus the lander, `claude-rc.service`, the memory copy and the homelab-ui browser and login.
  `claude_agent_ssh_login_enabled: true` adds `ssh-users` to the agent's exact group list
  (`agent_access.yml`) and writes a root-owned `authorized_keys` holding daniel-box's agent key
  with `restrict` (`agent_ssh_login.yml`). The play reads that key over ssh as the operator.
- **daniel-box.** `agent_peers.yml` generates the agent's peer key once, as the agent, at
  `claude_agent_ssh_key_path`, and renders `~/.ssh/config` with one stanza per peer whose switch
  is on: `User claude`, the peer's `server_ip`, `IdentitiesOnly yes` and
  `StrictHostKeyChecking accept-new`.
- **Both hosts.** The agent user lingers, so a batch started with `systemd-run --user` over ssh
  outlives the call that started it.
- **The Pi** gets nothing. The `docker`-group reasoning above holds for it.

Bringing a peer up is the operator's job, in this order: apply `claude_code` on daniel-box to
generate the peer key, apply it on daniel-server, run one interactive `/login` as `claude` there,
and register daniel-server's `~claude/.ssh/git_signing_ed25519.pub` on the agent's GitHub account
as a Signing Key. `fanout_place.py launch` refuses a host whose key is not registered.

`scripts/z2m/set_device_option.sh` and `ansible/roles/k8s/qbittorrent/files/apply_prefs.py` stay
operator-run.

**The homelab-ui login.** The MCP server logs in as its own Authelia user, `claude-agent`,
instead of the operator.

- The password is `authelia_agent_password` in SOPS, so `secret_rotation.yml` tracks it (tier
  `assisted`, like `claude-ui`'s). The `authelia` role hashes it at deploy time, with the same
  username-keyed read-back as the other two users.
- The `claude_code` role renders `/var/lib/claude/.config/homelab-ui/credentials.json` with the
  username, the password and the domain. The file is `0600`, owned by `claude`, and written
  under `no_log`. Ansible decrypts the three values as the operator, so the agent never holds
  the age key.
- The file carries the domain because `ui_mcp.sh` and `ui_login.py` read it from SOPS for the
  operator. The agent can already read the domain from the IngressRoutes its read-only
  kubeconfig covers.
- `ui_login.py` logs in from that file when it exists and from SOPS otherwise, so the operator's
  login is unchanged. `ui_mcp.sh` takes the domain from `ui_login.py --domain`.
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

**The agent's own browser.** The operator's Node, `playwright-mcp` and Chromium sit under
`/home/ubuntu`, which `claude` cannot read, and the operator's homelab-ui registration is
user-scope in their own `~/.claude.json`. `claude_code_agent_homelab_ui_enabled` (true in
daniel-box's host_vars) has the `claude_code` role give the agent its own copies, in
`tasks/agent_browser.yml`:

- The role installs a pinned Node tarball under `~/.local/share/node`, `@playwright/mcp` at the
  operator's release, and the Chromium that release's playwright pins. Each install runs as
  `claude`.
- The role registers `homelab-ui` at user scope in the agent's `~/.claude.json`. The entry
  launches `~/server/scripts/diagnostics/ui_mcp.sh` with `NODE_BIN` set to the agent's Node.
  The registration is not in the repo's `.mcp.json`: a project-scope server waits for an
  approval a phone session cannot give, and it would start a second Chromium in every operator
  session.
- Switching the variable off removes the registration, the Node tree and
  `~/.cache/ms-playwright`.

## Slice 6: retire Claude sessions as `ubuntu`

New interactive sessions start as `claude` through the dotfiles function `claude-agents`, which
runs `sudo machinectl shell claude@ /bin/bash -lc 'cd ~/server && exec claude agents "$@"'`. A
`claude-agents` session runs in `user.slice/user-996.slice/session-<n>.scope` beside
`claude-rc.service`, and the phone host still connects. daniel-server still runs sessions as
`ubuntu`.

- **Caps render for both users.** The login-slice caps and the pytest fan-out cap render for every
  user ID in `claude_code_login_uids`, which defaults to `[claude_code_login_uid]` (1000). On a
  host with `claude_code_agent_user_enabled`, `tasks/login_caps.yml` adds the agent's ID, looked up
  from the user database and never written down. Sessions started as either account carry the same
  `MemoryHigh`, `MemorySwapMax` and `PYTEST_XDIST_AUTO_NUM_WORKERS` as `claude-rc.service`, under
  the fleet bound on `user.slice`. An ID that leaves the list has its slice drop-in and
  `environment.d` file removed on the next apply, and `claude_code_login_caps_enabled: false`
  removes every one.
- **A metric and a page watch for a session as the wrong account.** `claude-cgroup-metrics.sh`
  writes `claude_uid_processes{uid}` for each uid in `claude_code_watch_uids`, an explicit 0
  included. It counts processes named `claude` by their real uid in `/proc/<pid>/status`.
  daniel-box watches uid 1000. The list defaults to empty, so daniel-server writes no series.
  monitor-bridge's Memory tile pages while any watched uid runs `claude`, after the cgroup arm's
  two-cycle streak, and the message names the host and the uid.
- **daniel-box sets `claude_code_login_uids: []`,** which removes the caps from `user-1000.slice`.
  The fleet bound on `user.slice` still covers it.

**Issue claims made from the other clone read as held.** `findings.py` judges whether a claim
is live from `git worktree list`, which lists only the reading user's own clone. A claim made
from the other clone therefore read as stale, with the reason `no worktree`, and `claim` or `reap`
released it while its session worked on. `lib/worktree_owner.py` reads the owner from the branch
name. A `worktree-<prefix>+…` branch belongs to the user whose `CLAUDE_WORKTREE_PREFIX` is
`<prefix>`, and a branch with no prefix belongs to the operator. A claim under the other user's
branch reads as held, with the reason `held by another user's clone`. To release a stale claim
from the other clone, run `findings.py reap` or `release` as that clone's user; neither user can
judge the other's worktrees.

A root-owned `/etc/claude-code/managed-settings.json` is deferred. It binds every user on the
host, `renovate-agent` included. Revisit it only for settings that should bind both agents.

**The inventory stays private.** Most of its rows describe the `ubuntu` account itself, which
keeps every one of them, and daniel-server still runs Claude as `ubuntu`. [What this page leaves
out](#what-this-page-leaves-out) has where it lives.

## Slice 7: a pre-merge dry run without secrets (optional)

**Build:** add a `deploy.sh --dry-run` mode that renders with placeholder secret values and
applies with a dry-run-only ServiceAccount. A ValidatingAdmissionPolicy denies that
ServiceAccount every request whose `request.dryRun` is false.

**Unverified:** whether this k3s version's admission CEL exposes `request.dryRun`, and whether
the dry-run path's kubectl calls can run without `become`.

## The operator's dotfiles

The agent takes the operator's dotfiles through chezmoi, with the dotfiles repo's `agent` flag on.
This replaced a hand-picked subset of the operator's config (the user `CLAUDE.md`, the output
style, the rules, the skills and the `env` block of `settings.json`). An audit found that the
subset left the agent with fewer safeguards than the operator: none of the operator's 32 user
hooks, 6 of the 37 top-level settings keys, and none of the user agents, commands or CLIs its
imported `CLAUDE.md` names (#4193, #4194, #4195). Each new operator tool reached the agent only
when someone remembered to copy it.

The dotfiles decide what an agent does not get (DanielH2018/dotfiles#809). Under the flag, they
leave alone what Ansible owns for the agent: its login profile, git identity and signers, ssh,
and its own `CLAUDE.md`. They render the user `CLAUDE.md` to `~/.claude/operator/CLAUDE.md`
instead, which the agent's own file imports. They also skip the scripts that need sudo or a
terminal, the uv and prek install that Ansible pins, and daniel-box's once-per-host timers. The
rendered `settings.json` drops `SUDO_ASKPASS`, names the `<host>-<user>` artifacts path and
labels telemetry `process.owner=<user>`. Every guard hook stays. `chezmoi-guard` and
`serve-artifacts` are off: the agent's source is a clone it only pulls, and the artifact port is
the operator's.

`tasks/agent_dotfiles.yml` installs it, and `claude_code_agent_dotfiles` switches it. The
steps:

1. Copy the operator's pinned chezmoi.
1. Seed the three prompt answers.
1. Clone the dotfiles as the agent's chezmoi source.
1. Run `files/claude-dotfiles-sync.sh`. `<name>-dotfiles-sync.timer` runs the same script every
   15 minutes, since a dotfiles merge applies no role here.
1. Delete the subset tree the script moved aside.

The script has three guards:

- It refuses a source that does not render `is-agent` as true. Applied anyway, such a source
  would write the operator's `.gitconfig` over the agent's identity. The refusal is not fatal
  to the apply, so a host can switch the flag on before the dotfiles change lands.
- After that check, and only then, it renames each root-owned subset tree to
  `~/.claude/.subset-copy-<name>`. chezmoi, running as the agent, cannot write into a root-owned
  directory, and the agent cannot delete one. It owns `~/.claude`, though, so it can rename one
  in place, and the role deletes the renamed tree on its next apply. A refused source therefore
  leaves the subset where it was, and the agent always has one of the two.
- It applies in two passes, every directory but `~/.claude` and then everything but
  directories. The source names the directory `private_dot_claude`, and a plain apply sets its mode
  to 0700. That would cut the operator off from the agent's memory store and artifacts, which
  `agent_github.yml` opens at 0710 for the operator's group.

**What the agent loses:** root ownership of its copied config. The subset was root-owned, which
stopped an injected session from editing it in place. chezmoi writes as the agent, so the agent
owns the files, and an edit lasts until the next sync, at most 15 minutes. The root ownership
never stopped a rename of the whole directory, so the gap it closed was narrow. The hooks come
from the dotfiles repo, where DanielClaudeBot can merge its own PRs. They guard against mistakes,
not against a hostile session, as the copied skills already did.

## More than one agent

The `claude_code` role builds every agent user in `claude_code_agents`, one run of
`tasks/agent.yml` per entry (#4196). `claude` stays the primary agent: the
`claude_code_agent_*` scalars configure it, and its entry carries only its name. A further
agent is a full entry in the host's `host_vars`:

```yaml
claude_code_agents:
  - name: "{{ claude_code_agent_user }}"
  - name: claude-ops
    github_login: SomeOtherBot
    github_id: 123456
    github_token_var: claude_ops_gh_token
```

`filter_plugins/claude_agents.py` refuses a further agent without its own GitHub login, id
and token variable, so it never pushes as `DanielClaudeBot` by omission. It also refuses two
agents that share a name, home or worktree prefix. A further agent gets a home under
`/var/lib/<name>`, its own clone, the shared tools, a GitHub identity, the operator's config
and the login caps. It gets no journal access unless its entry sets `journal_access: true`.

Only the primary agent gets the lander, the peer ssh login, the homelab-ui browser and login,
the artifacts mount and the memory seed. Each of those is a host-wide object named after
`claude` or a single Authelia account, and the lander is on the approval floor, so widening any
of them is its own change. The browser also stays outside the loop so that a Node or playwright
pin bump narrows to its own tag (#4189).

To add a further agent:

1. Create its GitHub machine account and make it a write collaborator.
1. Store its classic `public_repo` token in SOPS under the name its entry gives
   `github_token_var`.
1. Add `refs/heads/worktree-<prefix>+**` to the "agent branch fence" ruleset's excludes, and
   the same pattern to `github-ruleset-drift.sh`'s declaration. A ruleset cannot tell two bot
   accounts apart, so each can push to the other's prefix. The primary agent's lander lands
   only `worktree-claude+` branches, so it never lands another agent's work.
1. Add the entry, apply `initial_setup.yml --tags claude_code`, then log in as the agent once
   with `sudo machinectl shell <name>@ /bin/bash -l` and run `/login`.
1. Add its signing key's `.pub` to its account as a Signing Key.

To retire one, set `state: absent` rather than deleting the entry. Ansible removes nothing it
no longer declares. Absent expires the account, keeps the home, and removes the GitHub token
and the clone sync units.

## Checking and rolling back

Each slice has a check that proves it works and a switch that turns it back off. Only the
operator can log in as `claude`, so only the operator can run the checks that start there.

| Slice | Check | Rollback |
|---|---|---|
| 1 | As `claude`, `id` lists none of the denied groups, `ls /home/ubuntu` is refused, `sudo -n true` fails, `kubectl get pods -A` works and `uv run pytest` passes. `prek --version` runs, and `~/server/.git/hooks/pre-commit` names `/var/lib/claude/.local/bin/prek`. | Turn `claude_code_agent_user_enabled` off. That locks the account and stops its units. It keeps the home, which holds the agent's clone and any unpushed work. |
| 2 | As `claude`, a commit shows a good signature locally and `verified: true` on GitHub. A push to a `worktree-claude+` branch is accepted, and the PR is authored by `DanielClaudeBot`. The merge is refused at once by the CI gate, the review gate and the branch fence. A push to a branch outside the prefix is refused with `Cannot create ref due to creations being restricted`. | Delete the review gate and the branch fence, and revoke the token. |
| 3 | A session as `claude` runs `land.sh --pr <n> --arm-merge --await-merge --detach && cc-wait land <n>` on three PRs. A deployable change ends in `VERDICT: settled` and the verdict file in `/var/lib/claude-land/<n>.verdict` ends with the same line. An approval-list path or a gate-imported module refuses with `need the operator's approval: <path>`. After the operator approves the head, the lander logs `merged directly through the ruleset bypass`. | `claude_code_lander_enabled: false` removes the polkit rule and the unit. `claude` still opens PRs, and the operator lands them. |
| 4 | From the phone, a session prints `id` (it must show `claude`), names the entries in the copied `MEMORY.md`, and writes an artifact and a new memory file. The artifact renders under `/a/daniel-box-claude/`, which proves the artifacts pod can traverse to the file. As `ubuntu`, `cat` of both new files succeeds. A permission error on the memory file means Claude Code created it `0600`, which `UMask=0027` cannot widen, so file a finding with `findings.py open`. `journalctl -u claude-memory-sync` shows a run that copied the file to daniel-server. | `claude_code_user: ubuntu`. A `User=` change restarts the host and drops its live sessions. |
| 5 | As `claude` on daniel-box, `uv run python scripts/dev/fanout_place.py read` prints a reading for both hosts, each with `signing=ok`. For the login alone, `uv run python scripts/diagnostics/ui_login.py && uv run python scripts/diagnostics/ui_login.py --verify homepage` succeeds, and homelab-ui renders a service page. | `claude_agent_ssh_login_enabled: false` in daniel-server's host_vars removes the key and the group. `claude_code_agent_user_enabled: false` there also expires the account and stops its linger. `claude_code_agent_homelab_ui_enabled: false` removes the browser, and deleting the `claude-agent` block from the authelia role revokes the login. |
| 6 | Starting `claude` as `ubuntu` shows up as `claude_uid_processes{uid="1000"}` within one 30-second timer interval. | Delete the two lines in daniel-box's host_vars. `claude_code_login_uids` returns to uid 1000, and an empty `claude_code_watch_uids` stops the series and the page with it. |
| dotfiles | A scratch run of the sync script as `claude` against the dotfiles branch applies the agent variant, leaves `~/.claude` at the mode it started with and keeps a pre-existing `.profile` and `.gitconfig`. The rendered `settings.json` carries every guard hook with `SUDO_ASKPASS` unset, `CLAUDE_ARTIFACTS_HOST=daniel-box-claude` and `OTEL_RESOURCE_ATTRIBUTES=process.owner=claude`. A second run is a no-op with an empty `chezmoi diff`. A source that predates the flag makes the script exit 1 and apply nothing. | Delete `claude_code_agent_dotfiles: true` from daniel-box's `host_vars`. The next apply stops the timer, removes its units, and installs the subset again. |

## What the `claude` user cannot do

| Capability | Why | Where it goes instead |
|---|---|---|
| `deploy.sh`, `--check` and `--dry-run` before a merge | All three decrypt SOPS, and the agent has no age key | Merged work deploys through the lander (slice 3). A pre-merge dry run returns with slice 7. |
| `sudo` | No sudo rights and no `SUDO_ASKPASS` helper | The operator, or the deployer after a merge |
| Adding or rotating a secret | `sops` needs the data key to write a value | The operator. Key names stay readable, because SOPS encrypts values only. |
| ssh as `ubuntu` to the peers | That shell has sudo | The operator. The agent reaches daniel-server as its own `claude` agent user, for fan-out only (slice 5). The Pi has no agent login, because the one `probe.py` ssh path needs its `docker` group. |
| The `adm` group's logs | The agent belongs to `systemd-journal` only, not `adm` | The journal through `systemd-journal`, and the verdict files from the lander for landing |
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
account as it still is. The operator decided on 2026-10-09 to keep it private rather than
publish it (#3685).

The durable home of the inventory is the operator's break-glass kit in the password manager,
which holds a copy of `~/.claude/artifacts/pinned/claude-unix-user-plan_2026-10-04.html`. The
pinned copy on daniel-box is a convenience only: `prune-artifacts.sh` skips `pinned/`, but
nothing backs it up.
