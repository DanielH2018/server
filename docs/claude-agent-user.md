# Claude as its own UNIX user on daniel-box

On 2026-10-04 the operator approved a plan to run every Claude session on daniel-box as a
dedicated `claude` UNIX user instead of `ubuntu`. This page is the plan of record: the slices,
the decisions taken along the way, and what each slice still owes. It replaces the
`claude-unix-user-plan_2026-10-04.html` artifact as the durable copy, because
`prune-artifacts.sh` deletes an artifact 30 days after its last update (#3657).

One part of the plan stays out of this page. The repo is public, and the per-path inventory of
what a session reaches as `ubuntu` stays private until slice 6 retires those sessions. The
operator decided this on 2026-10-09 (#3685). See
[What this page leaves out](#what-this-page-leaves-out).

## Status

| Slice | What it delivers | State |
|---|---|---|
| 1 | A read-only `claude` user an operator can open a session as | Done 2026-10-04 (#3504, #3505) |
| 2 | Claude opens PRs under its own GitHub identity | Done 2026-10-05 (checked on #3622) |
| 3 | Merged agent work lands and deploys through a lander unit | Built and deployed 2026-10-08 (#3633, #3650, #3651, #3652). The end-to-end check as `claude` is owed. |
| 4 | The phone host `claude-rc.service` runs as `claude` | Planned. Starts after slice 3's check. |
| 5 | Peer users, and the tools that decrypt SOPS | Planned |
| 6 | Retire Claude sessions as `ubuntu` | Planned |
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
  `adm` or `systemd-journal`, `journalctl` shows the agent only its own units. Granting
  `systemd-journal` is a separate decision for the operator, because logs can carry secrets.
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
  endpoint, pinned to the head SHA it checked.
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

**Check (owed):** a `claude` login runs
`land.sh --pr <n> --arm-merge --await-merge --detach && cc-wait land <n>` on a real agent PR.
The verdict file ends `VERDICT: settled`, and `probe.py health <svc>` passes. A PR touching
`setup/claude_code/` is refused without an approval and lands with one. Only the operator can
log in as `claude`, so only the operator can run this check. Slice 4 starts after it.

**Rollback:** `claude_code_lander_enabled: false` removes the polkit rule and the unit. `claude`
still opens PRs, and the operator lands them.

## Slice 4: the phone host runs as `claude` (planned)

**Build:** add `claude_code_user` (default `sys_user`) and route `claude-rc.service`'s `User=`,
`Group=`, `HOME`, `PATH`, `KUBECONFIG`, `ExecStart` and `WorkingDirectory` through it. Set it to
`claude` on daniel-box, and set `claude_code_login_uid` to `claude`'s uid. Add
`ProtectHome=yes`, `NoNewPrivileges=yes` and `PrivateTmp=yes`. The agent's home sits under
`/var/lib`, so `ProtectHome` hides every human home without hiding the agent's. Move the memory
store to the agent's project key, rewrite `MEMORY.md`'s `/home/ubuntu/server/...` links, and
update `claude_code_memory_sync_dir` and `artifacts_host_dir`.

**Read grant:** `ubuntu` reads the agent's artifacts and memory through one grant in the role.
`claude-memory-sync` runs as `sys_user`, and the artifacts tree is the operator's to read, so
both need it. The artifacts pod may not need it. Its template sets no `securityContext`, so the
pod runs as root with the default capability set of the container runtime, and those include `CAP_DAC_OVERRIDE`.
The slice 4 check tests that read anyway, because a later hardening of the pod would drop the
capability.

- The `~/.claude` item in `agent_github.yml` changes from `0700` to `0710`. Group `ubuntu`
  gets traverse but not list, and `.credentials.json` stays `0600`.
- A task creates `~/.claude/artifacts` and the memory directory as the agent, `setgid`, with
  group `ubuntu`. It gives each a default ACL of `g:ubuntu:rX`, so files Claude Code writes
  later inherit the read.
- The ACL task runs after every `file:` task that names those paths. A `mode:` there runs
  `chmod`, and `chmod` rewrites the ACL mask from the group bits. `0700` sets the mask to
  `---`, which disables every named entry while `getfacl` still lists it.
- A default ACL cannot widen a file's create mode. A file Claude Code creates `0600` gets
  a mask of `---`, so `ubuntu` cannot read it. The check below reads a new file as `ubuntu`
  for that reason, rather than reading `getfacl`.

**Couplings to change:**

| Coupling | Where |
|---|---|
| `HOME`, `PATH`, `KUBECONFIG`, `ExecStart`, `WorkingDirectory` through `sys_user` | `ansible/roles/setup/claude_code/templates/claude-rc.service.j2` |
| `claude_code_login_uid`, which drives the login-slice caps | `ansible/roles/setup/claude_code/defaults/main.yml` |
| `claude_code_memory_sync_dir` and the path-derived project key | `ansible/roles/setup/claude_code/defaults/main.yml` |
| `artifacts_host_dir` and its bind mount | `ansible/roles/k8s/artifacts/defaults/main.yml` |
| Hooks that hard-code `/home/ubuntu`, and `SUDO_ASKPASS` | the dotfiles repo's `~/.claude` sources |
| `/home/ubuntu` literals in repo tooling | `scripts/dev/fanout_lib/launch.py`, `scripts/deploy_tools/land_lib/options.py`, `scripts/deploy_tools/land_reach.py` and about 16 more non-test files |
| The SessionStart banner's other-session list | It reads `git worktree list`, so it shows only sessions in the same clone |

**Check:** a phone-created session prints `claude` for `id`, loads `MEMORY.md`, and writes an
artifact whose link renders. The artifacts pod can traverse to that file. As `ubuntu`, `cat`
reads that artifact and a memory file the session wrote, after a `claude_code` apply.

**Rollback:** `claude_code_user: ubuntu`. A `User=` change restarts the host and drops its live
sessions.

## Slice 5: peers, and the tools that decrypt SOPS (planned)

**Build:** create a no-sudo `claude` user on daniel-server and daniel-pi for `probe.py`'s ssh
paths, with a key per host. Give the homelab-ui MCP server a dedicated low-privilege Authelia
user whose credential lives in the agent's home rather than SOPS.
`scripts/z2m/set_device_option.sh` and `ansible/roles/k8s/qbittorrent/files/apply_prefs.py` stay operator-run, or
each gets a lander-style oneshot unit.

**Check:** a `probe.py` Pi-plane subcommand works as `claude`, and homelab-ui renders a service
page.

**Rollback:** remove the peer users.

## Slice 6: retire Claude sessions as `ubuntu` (planned)

**Build:** move the login-slice caps off `user-1000.slice`. Extend `claude-cgroup-metrics.sh` to
report any `claude` process running as uid 1000. Optionally ship a root-owned
`/etc/claude-code/managed-settings.json`. It binds every user on the host, `renovate-agent`
included, so it is written for both agents and ships only once no Claude session runs as
`ubuntu`.

Once the slice lands, publish the per-path inventory from the operator's break-glass kit in
this page, and delete [What this page leaves out](#what-this-page-leaves-out). From then on
the inventory describes history.

**Check:** starting `claude` as `ubuntu` shows up in the metric within one 30-second timer
interval.

**Rollback:** `claude_code_login_caps_enabled` and the metric are both variables.

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
| ssh as `ubuntu` to the peers | That shell has sudo | A no-sudo `claude` user per peer (slice 5) |
| The system journal | No `adm` membership | Its own units only. Verdict files from the lander replace the journal for landing. |
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
inventory stays private, because the repo is public and the inventory stays accurate until
slice 6 retires Claude sessions as `ubuntu`. The operator decided this split on 2026-10-09
(#3685).

The durable home of the inventory is the operator's offline break-glass kit. The operator
copies `~/.claude/artifacts/pinned/claude-unix-user-plan_2026-10-04.html` there. The pinned
copy on daniel-box is a convenience only: `prune-artifacts.sh` skips `pinned/`, but nothing
backs it up.

Slice 6 publishes the inventory in this page, once it describes history.
