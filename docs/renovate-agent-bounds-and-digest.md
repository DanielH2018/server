# `renovate_agent` — what bounds a run, and what its digest and tile measure

Working-out moved off `ansible/roles/setup/renovate_agent/CLAUDE.md` (#2995), which a session
reads on every touch of the unattended agent. The role doc keeps the arming procedure and the
autonomous-role contract; this page keeps the modules `files/` ships, the lander, the session's
own identity and the arming checklist, the four bounds and why each is the var it is, the
worktree rules, what the Discord digest measures, and the alive tile's exit-code and token
plumbing. `docs/renovate-notify-internals.md` is the sibling page for the reporting half.

## The modules `files/` ships

- `agent_logic.py` — the pure half: the run gate, the before/after delta, the digest text.
- `agent_toolbox.py` — `AgentTools`, the four process boundaries a test replaces. It names
  the other two modules nowhere, which is what makes it the leaf they both import.
- `run_worktree.py` — the run worktree's lifecycle, and the only code here that talks to git.
- `renovate_agent.py` — the entry module: config, the `gh` census, the session, the digest,
  the crash report.
- `land_renovate_pr.py` — the lander, run by `renovate-agent-land@<n>.service` rather than by
  the session. *The lander* below says why it is separate.

`run_worktree.py` and `agent_toolbox.py` came out of `renovate_agent.py` on 2026-09-30, which
had reached the 600-line cap `ansible/tests/repo/test_module_length_ratchet.py` enforces
(#3036). The seam was already there: the worktree half talks only to git and to the forge, it
had its own test module, and it shared nothing with the census and digest halves except the
toolbox. `agent_toolbox.py` is a third module rather than a name the other two pass between
them, because either of the two-module shapes makes the import a cycle.

The role names each module twice — once in the "Install agent Python files" copy loop, once in
`stamp_deployed_pairs` for manifest-prune-check.sh's stale-script arm. Neither list is derived
from the directory, so a module in `files/` and in neither list is one the host never receives.
ENFORCED:
`ansible/tests/setup/test_renovate_agent_modules_are_shipped.py::test_every_shipped_module_is_installed_and_stamped`.

## The lander

`renovate-agent-land@<n>.service` lands one Renovate PR for the agent. It runs
`files/land_renovate_pr.py` as the operator's user, because `land.sh` deploys. The agent's own
user, `renovate_agent_user`, may start it through `templates/50-renovate-agent-land.rules.j2`
and do nothing else with privilege. The session then reads
`/var/lib/renovate-agent-land/<n>.verdict`.

The lander re-reads the PR from GitHub with the operator's token and lands it only when every
check in its module docstring passes. The denied roles come from the deployer's rendered
`K8S_AUTODEPLOY_DENYLIST`, so the lander adds no parser of its own. It never reads the PR title,
because the session can edit a title. ENFORCED:
`ansible/roles/setup/renovate_agent/tests/test_land_renovate_pr.py`, which also holds the polkit
rule's unit-name pattern to the PR numbers the script accepts.

The session starts the lander with a plain `systemctl start`, which blocks until the landing
ends. The unit raises Claude Code's 10-minute Bash cap to `renovate_agent_bash_timeout_ms`
through `BASH_MAX_TIMEOUT_MS`, so the call outlasts the 60-minute `TimeoutStartSec` of the lander unit. A
call cut short leaves the landing running, and the verdict file still holds the last run's line
until the lander takes its lock and writes `PENDING`. The prompt therefore waits on the unit's
`ActiveState` before it reads the file.

## The session's own identity

The session reads third-party text: release notes, changelogs and PR bodies. It runs as
`renovate_agent_user` so that text steering it reaches nothing able to change `master`.

- **Home, state and clone.** `renovate_agent_home` (`/var/lib/renovate-agent`) holds the run
  record, the session's `~/.claude` and its own clone at `renovate_agent_clone_dir`. The
  directory carries the set-group-ID bit and group `sys_user`, so the docs cron still reads the run record.
- **Toolchain.** The agent runs its own uv rather than `/usr/local/bin/uv`, the operator's
  install that `initial_setup` publishes. The role copies the operator's pinned uv into the agent's `~/.local/bin` and runs
  `uv python install` as the agent. Every project hook runs through `uv run`, and in an
  unattended session a hook that cannot start makes each Bash call an `ask`, which is a denial.
- **Project hooks.** `.claude/hooks/run-hook.sh` defaults to the operator's checkout and uv. The
  unit sets `RUN_HOOK_PROJECT_DIR` to the run worktree and `RUN_HOOK_UV` to the agent's uv, and
  `prepare_worktree` runs `uv sync --frozen` in that tree before the session starts. A guard
  that cannot import its dependencies raises, and the dispatcher drops its verdict without a
  word. `block-footguns` also needs the `claude_guard` package, which the dotfiles repo deploys
  only under the operator's home; without it the guard asks on every command naming `gh`. The
  role copies the operator's package into the agent's `~/.local/share/claude-guard` on each
  apply.
- **GitHub token.** `renovate_agent_gh_token` is a fine-grained token for this repo alone:
  pull requests and actions read-write, contents, checks and metadata read. With no contents
  write it cannot push or merge, and with no issues write it cannot file a finding, so the
  digest is the session's only hand-off. The lander merges with the operator's token.
- **Claude credential.** `renovate_agent_claude_oauth_token`, from `claude setup-token`. Both
  reach the session as environment variables through `/etc/renovate-agent/session.env` (root
  0600), which systemd reads as root.
- **Confinement.** `ProtectHome=yes`, `ProtectProc=invisible`, `NoNewPrivileges=yes` and
  `PrivateTmp=yes`. A same-uid sandbox was ruled out first: systemd 255 has no `PrivatePIDs=`,
  so a session running as the operator could read the operator's other processes and user bus.
- **Its own config is read-only to it.** `/etc/renovate-agent/config.env` is root-owned and
  group-readable by the agent. A steered session could otherwise raise its budget or point
  `PROMPT_FILE` at a prompt of its own for the next run.
- **The run lock moved into the agent's home.** `fs.protected_regular=2` refuses an `O_CREAT`
  open of another user's file in sticky `/run/lock`, and the old lock there belongs to the
  operator.
- **Deployer holds.** An ACL gives the agent read on `/var/lib/gitops-deploy`, where the wrapper
  reads the hold markers through `gitops_hold.DeployerSnapshot`. A marker it cannot read fails
  the run with `EXIT_STATE_UNREADABLE` (3) and a Discord post naming the ACL, because an
  unreadable hold is not "no hold" (#3703). Until then `agent_toolbox.read_file` returned `""`
  on a read error, so a lost ACL read as no hold at all. `land.sh`, run by the lander as the
  operator, still refuses on a hold.

ENFORCED: `ansible/tests/setup/test_renovate_agent_identity.py`, which also runs the prompt's
`systemctl` commands through the repo's own PreToolUse:Bash dispatcher.

## Arming it

The role refuses to arm without both credentials. To arm the agent:

1. Create a fine-grained token for the repo with exactly the permissions above.
2. Run `claude setup-token`.
3. Add both as `renovate_agent_gh_token` and `renovate_agent_claude_oauth_token` with the
   `add-secret` skill, then run `secret_rotation.py sync`.
4. Set `renovate_agent_enabled: true` in `inventory/host_vars/daniel-box.yml` and apply the role.
5. Start one run by hand and watch it. Auto mode's classifier decides whether the session may
   run `systemctl start` on a system unit, and nothing short of a real run tests that.

## Exercising the wrapper without arming anything

`RENOVATE_AGENT_CONFIG` overrides the config path, so the I/O shell can run end-to-end against a
throwaway config before the timer exists. Point `REPO` at the real repo with the backlog empty and
the gate returns before it spends a session, which makes it a free pass through config parsing,
the `gh` census and the return path:

```bash
RENOVATE_AGENT_CONFIG=/tmp/agent-test.env \
  uv run python ansible/roles/setup/renovate_agent/files/renovate_agent.py
```

The override exists for exactly this. Without it the first armed tick would be the first time this
code ever ran.

## What bounds the run

The user boundary above bounds what a run can reach. These vars in `defaults/main.yml` bound
what it costs:

--8<-- "assets/generated/fragments/pr-agent-bounds.md"

Why each bound is the var it is:

- **`renovate_agent_max_prs`:** each landing is a CI wait plus a tick plus a deploy plus a
  health gate. The cap fits the wall clock, and the rest wait for tomorrow.
- **`renovate_agent_run_timeout_s`:** the wrapper's own kill, which still posts a digest naming
  the timeout.
- **`renovate_agent_unit_timeout`:** systemd's backstop. It kills the whole cgroup and posts
  only the `OnFailure` alert, so it must never trip first.
- **`renovate_agent_budget_usd`:** a runaway backstop, not a planned stop. See below.

**The budget must not be the binding constraint.** The PR cap is what bounds a normal run; the
budget only catches a loop. A landing in flight runs in the lander unit, so a session
killed at either limit leaves that landing to finish, but the digest then cannot report it.

**The session never runs in the primary checkout.** One untracked file in `/home/<user>/server`
parks the GitOps deployer silently, and a session that edits, renders and tests is guaranteed to
leave some. Each tick recreates `.claude/worktrees/renovate-auto` at `origin/master` inside the
agent's own clone and runs there. The lander runs `land.sh` in the primary checkout as the
operator.

Every rule in the rest of this section lives in `files/run_worktree.py`.

**A worktree holding work is not thrown away.** If the previous tick left uncommitted changes or
commits whose content is not on `origin/master`, the tick skips, posts the path and exits
`EXIT_WORKTREE_BLOCKED` (2). Removing the tree is how unlanded work is lost.

Content, not ancestry. The run branch is fixed (`worktree-renovate-auto`) and nothing resets it
after a landing, and a squash merge keeps a branch's content while discarding the commits that
carried it — so `rev-list origin/master..<branch>` counts a landed branch's commits forever. That
refused the tree every day from 2026-09-14 while its two commits sat on master as PR #1812
(#2014). `branch_content_is_on_master` settles it the way `scripts/dev/prune_worktrees.py` does:
`git merge-tree --write-tree origin/master <branch>` producing master's own tree means the branch
has nothing master lacks. A revert-only branch is still refused (merging it changes master's
tree), and no verdict — a conflict with master's drift, empty output — reads as not contained by
`merge-tree`. That conflict case is the pruner's fourth layer and is ported too
(`branch_tip_was_merged`): `gh pr list --state merged --head <branch> --json headRefOid` must name
the branch's exact tip. Ported rather than left to the page because the very tree #2014 found was
already past `merge-tree` — master had drifted into a conflict on `n8n/base-pin-history.tsv` — so
without it the fix would have paged daily and still needed the operator's `reset --hard`. The
match is on the head SHA, never the branch name: the name is reused every tick.

## The digest measures effect, not completion

`is_error: false` plus `terminal_reason: completed` means the process ended cleanly, not that any
PR moved — a session that achieved nothing still writes a confident closing paragraph. So the
wrapper censuses the open PRs authored by `app/renovate` **before and after**, and the digest's
headline is that delta:

- `resolved #a, #b` — those PR numbers left the open set and GitHub reports them `MERGED`.
- `ran and merged no Renovate PR` — PRs left the open set, but none merged. Each one is listed as
  `closed without merging` or `state unreadable`.
- `ran and no Renovate PR changed state` — the failure that would otherwise read green.
- `FAILED — <reason>` — timeout, non-zero exit, or `is_error`.

Leaving the open set is not landing. A session can close a Renovate PR without merging it,
for one it judges superseded or dropped (#2746). So the wrapper runs `gh pr view <n> --json
state` for every PR that left the open set, and only a `MERGED` one counts as resolved (#2755). A
failed lookup lands in `state unreadable`, never in `resolved`.

The session opens no PR of its own. Its token has no contents write, so a `manual —` work order
ends as a line in the session's summary for a person to finish (#3420). The census therefore
reads only PRs authored by `app/renovate`. A census of the session account's own PRs, which
counted the superseding PRs earlier sessions opened, was retired with that change (#3421).

**The census filters the author locally, never with `gh pr list --author`** (#2772). With
`--author`, gh runs a GraphQL `search(` query, and GitHub's search index is eventually consistent.
The after-census runs seconds after the session exits, so the index can still list a Renovate PR
just merged as open. Without the flag, gh reads `repository.pullRequests`, which is current.
`_open_pr_listing` in `files/renovate_agent.py` is the listing the census reads, and
`test_agent_logic.py::TestOpenPrs` refuses the flag.

`permission denials:` on a digest line is the one to act on. Headless auto mode approving the
session's writes is the assumption the whole design rests on; it was measured against Claude Code
2.1.258 (a bash file create, a `sed -i`, and an `Edit` tool call all landed with
`permission_denials: []`), but a Claude Code upgrade can change it, and the failure mode is a
session that reads green and does nothing.

## The run record

Every tick appends one JSON line to `runs.jsonl` in `STATE_DIR` (`/var/lib/renovate-agent`), so
whether the agent is worth its cost is a count over one file rather than a read of 30 digests
(#2864). `run_record` in `files/agent_logic.py` builds the line, and each exit path writes one:

- `skipped` — the gate spent no session. `reason` says why, including the quiet empty-backlog
  skip that posts nothing to Discord.
- `blocked` — the run worktree still holds unlanded work.
- `ran` or `failed` — a session ran. The line carries `merged`, `closed`, `unread`,
  `left_open`, `touched` and `opened` from the measured delta, plus `cost_usd`, `turns` and the
  denial count. Lines written before #3421 also carry `handed_off`, which no reader uses.
- `crashed` — the wrapper threw. `report_crash` writes this line before its Kuma push.

The PR fields come from the census, not from the session's summary. `left_open` is the PRs
still open after the run, whether the session skipped them for the `k8s_autodeploy: false`
denylist, for the PR cap, or after triaging them.

`touched` is the subset of `left_open` whose `updatedAt` moved between the before-census and
the after-census, and it is the only field a comment-only triage reaches (#3032): commenting
on or labelling a PR moves it out of no set. **Read it as evidence, not as proof.** A Renovate
rebase during the session moves `updatedAt` too, and so does anyone else who touches the PR
while the session runs. A PR whose `updatedAt` either census could not read counts as
untouched, because an unreadable timestamp is no evidence. The digest names the same PRs on a
`left open but updated during the run` line.

A 30-day count:

```bash
jq -s --argjson since "$(date -d '-30 days' +%s)" '[.[] | select(.ts >= $since)]
  | {ticks: length, sessions: map(select(.result == "ran" or .result == "failed")) | length,
     merged: map(.merged // [] | length) | add, closed: map(.closed // [] | length) | add,
     touched: map(.touched // [] | length) | add, cost_usd: map(.cost_usd // 0) | add}' \
  /var/lib/renovate-agent/runs.jsonl
```

The file gains one line a day and is never rotated.

## The alive monitor

`renovate_agent_kuma_push_token` (SOPS, tier auto) is the one token behind two halves: the unit's
`ExecStartPost` beat, and the `Renovate Agent — Alive` push tile in
`roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2`. The beat fires only when the wrapper
exited 0, so the tile reports silence and the `OnFailure` alert reports failure.

**A worktree-blocked skip exits non-zero, so it does not beat.** The beat is `ExecStartPost`,
which runs after ANY exit 0 — a `down` pushed from inside the wrapper on a `return 0` path is
overwritten by the `up` that follows it. So that skip returns `EXIT_WORKTREE_BLOCKED` instead: no
beat, `OnFailure` pages, and the tile expires by deadman if nobody clears the tree. Until
2026-09-18 that path returned 0, and the tile stayed green through five daily skips (#2014). A
deployer state the agent cannot read exits `EXIT_STATE_UNREADABLE` for the same reason (#3703).
The two other skips still exit 0: the quiet no-open-PRs skip is the healthy steady state, and the
GitOps-hold skip is alarmed by the deployer's own `GitOps Status` tile, which this one need not
duplicate. `test_agent_logic.py::TestSkipExitCodes` pins the blocked and the quiet case.

**A crash also pushes its own `down`, carrying the exception text** (`report_crash`, called from
the `__main__` guard). Silence plus an `OnFailure` page was not enough: the tile went down 28 hours
later by deadman expiry with no reason attached, which is what a host that is simply off looks
like. For two days from 2026-09-08 that hid a one-line `prepare_worktree` failure — a directory at
the run worktree's path that `git worktree list` had no record of (#1477). The deadman stays as the
backstop for a host that is genuinely off; it must not be the only signal for a run that started
and threw. The report is best effort and never re-raises, so a failed report cannot replace the
traceback that says what broke.

**An unregistered directory at the run worktree's path is reclaimed, not a crash.** That is what a
killed session leaves behind. It matters twice over, because **git searches UPWARD for a
repository**: `git -C <orphan dir> status` resolves to the PRIMARY CHECKOUT and answers about that
tree, so `worktree_is_reusable` was reading the wrong repo — a dirty primary would have read as
this run tree holding uncommitted changes. `is_registered_worktree` decides which case it is, and it
fails CLOSED — an unreadable `worktree list` reads as a directory git owns — so nothing removes a
tree that might be registered. Every case that holds real work is still refused by `worktree_is_reusable` before
this runs.

**The push URL lives in `/etc/renovate-agent/config.env` (0600), not in the unit.** A unit line is
public: `systemctl show <unit> -p ExecStartPost` serves it over the system bus to any local user,
and `/proc` here has no `hidepid`. The unit inlined the whole URL until 2026-09-09, which put a
rotation-tracked token in clear text behind a command the harness guard recommended as the SAFE
alternative to `systemctl cat` (issue #1489). `ExecStartPost` now sources `config.env` and passes
`$KUMA_PUSH_URL` to `curl` on stdin (`-K -`), the form `renovate-notify.service.j2` already used.
ENFORCED: `ansible/tests/setup/test_renovate_agent_unit.py`. `report_crash` sends its `down` the
same way. Until 2026-10-03 it passed the URL as curl's last argument, which put the token in
`/proc/<pid>/cmdline` for the length of the push. ENFORCED:
`ansible/roles/setup/renovate_agent/tests/test_crash_report.py::test_a_crash_pushes_a_down_carrying_the_exception_text`.

The tile's deadline is 28h, not the 25h the other daily tiles use, because the beat lands at the
end of the run: a fast run followed by one that draws the full jitter and runs to the 100-minute
unit timeout spaces two beats 25h50m apart. `test_renovate_agent_unit.py` pins the deadline
between that gap and two periods.

Rotating the token moves both halves, on two deploy paths: `deploy.sh --tags uptime-kuma` for the
tile and `initial_setup.yml --tags renovate_agent` for the unit. That is why it is in
`CROSS_HOST_PUSH_TOKENS` in `secret_rotation.py` and the unattended rotation skips it.

## The denylist marker's own history

The contract's rule is that the session never touches a PR whose title or branch carries
`k8s_autodeploy: false`. How the marker comes to be there took several rounds:

- The denylist rule in `renovate.json` leads its parenthetical with the marker. A per-package rule
  whose pin a denied role owns ends its own with it (the CrowdSec bouncer plugin in Traefik,
  Meilisearch and the time-tagger dependencies in Karakeep, n8n's Dockerfile pins, #1963), because
  those rules override the denylist rule's `groupName`.
- **Read the branch as well as the title** (#2641). Renovate titles a single-dependency group
  `Update <dep> …` and drops the group name, so the marker can survive in the branch alone,
  slugified as `k8s_autodeploy-false` — #2620 was `Update klutchell/unbound Docker tag to v1.26.1`
  against Pi-hole's defaults. The prompt therefore reads `headRefName` beside the title, and
  `test_the_prompt_leaves_a_denylisted_pr_to_a_person` pins both tells.
- Every rule carrying the marker sets `groupSingleUpdates: true`, which applies the group's
  `commitMessageTopic` to a one-dependency branch too and so puts the marker in their titles: the
  denylist and base-image rules since #2646, the per-package manual rules since #2654. A PR raised
  before that flag landed still arrives bare-titled.
- A third rule carries it for a denied role's base image — the `FROM` in
  `templates/Dockerfile*.j2`, which Renovate's built-in dockerfile manager finds and the denylist
  rule's `custom.regex` scope never reaches (#2117: nut's Debian digest bump #2115 arrived with a
  bare title). Its file list is the denylist restricted to the roles that carry a Dockerfile, and
  the same guard derives it.
