# `renovate_agent` — what bounds a run, and what its digest measures

Working-out moved off `hosts/components/renovate-agent/CLAUDE.md` (#2995), which a session
reads on every touch of the unattended agent. The role doc keeps the arming procedure and the
autonomous-role contract; this page keeps the modules `files/` ships, the lander, the session's
own identity and the arming checklist, the four bounds and why each is the var it is, the
worktree rules, what the Discord digest measures, and the exit codes behind the alive verdict. `docs/renovate-notify-internals.md` is the sibling page for the reporting half.

## The modules `files/` ships

- `agent_logic.py` — the pure half: the run gate, the before/after delta, the digest text.
- `agent_toolbox.py` — `AgentTools`, the four process boundaries a test replaces. It names
  the other two modules nowhere, which is what makes it the leaf they both import.
- `run_worktree.py` — the run worktree's lifecycle, and the only code here that talks to git.
- `renovate_agent.py` — the entry module: config, the `gh` census, the session, the digest,
  the crash report.
- `land_renovate_pr.py` — the lander, run by `renovate-agent-land@<n>.service` rather than by
  the session. *The lander* below says why it is separate.
- `renovate_floor.py` — the floor the lander reads: the paths that need the operator's approval, the
  digest-only exception, the branch-pusher check and the soak-status check.

`component.yml` names each module in `files:`. The list is not derived from the directory, so a
module in `files/` and not on the list is one the host never receives.
ENFORCED:
`hosts/tests/test_renovate_agent_modules_are_shipped.py::test_every_shipped_module_is_installed`.

## The lander

`renovate-agent-land@<n>.service` lands one Renovate PR for the agent. It runs
`files/land_renovate_pr.py` as the operator's user, because `land.sh` deploys. The
controller user, `renovate-agent-ctl`, may start it through
`templates/50-renovate-agent-land.rules.j2`, and the session's user may not. The runner starts
it and then reads `/var/lib/renovate-agent-land/<n>.verdict`.

The lander re-reads the PR from GitHub with the operator's token and lands it only when every
check in its module docstring passes. The denied roles come from
`uv run --frozen python scripts/dev/k8s_autodeploy_counts.py --denylist`, run in
`RENOVATE_AGENT_REPO_DIR`, the checkout from which it runs `land.sh`, so `renovate.json`'s rule
and the lander read one derivation. A non-zero exit, a timeout or output holding no list refuses
the landing. It never reads the PR title, because the session can edit a title. ENFORCED:
`hosts/components/renovate-agent/tests/test_land_renovate_pr.py`, which also holds the polkit
rule's unit-name pattern to the PR numbers the script accepts.

**The floor decides which landings need the operator's approval** (dotfiles#967 slice 1).
`files/t2-approval-floor`, installed at `/usr/lib/homelab/t2-approval-floor`, is the one list
every T2 lander reads (A5a). A lander that sets `LAND_APPROVAL_PATHS` to it reads it through
`land_lib/policy.py`, and a test holds the two matchers equal over every tracked path. A
missing or empty file refuses the landing. `homelab-claude-land@`, the T1 lander for the
phone host's sessions, keeps its own narrower list. The floor lists the paths a session, a host package or the landing itself runs:
`hosts/`, the secrets, the interpreter pins, `.github/`, `.claude/`, `scripts/`, every
`conftest.py` and `CLAUDE.md`, and each role's `tests/` and `files/`. A plain entry is a path
prefix, and an entry holding `*` is a glob over the whole path. A PR changing a floor path
needs approval, unless the change is a digest-only bump: a modified host component
`defaults/main.yml` whose every changed line is an `*_image:` line that differs only in its
`@sha256:` digest. A PR also needs approval when anyone but `renovate[bot]` pushed to its
branch since the branch's newest creation, as the repository activity API reports it. The
lander refuses outright a `renovate/stability-days` status that `renovate[bot]` did not post,
because DanielClaudeBot's write access can post a status.

The lander then runs `land.sh` with `LAND_REQUIRE_HEAD` set to the head it checked, and adds
`LAND_REQUIRE_APPROVAL` and `LAND_APPROVER` when the floor flagged the PR. land.sh refuses a
head that moved and, when asked, a head the operator has not approved. The test
`test_every_top_level_entry_is_on_the_floor_or_classified` in
`hosts/components/renovate-agent/tests/test_renovate_floor.py` fails when a new top-level entry
is neither on the floor nor listed with a reason.

**The session nominates and the runner lands** (#967's I9, guardrails plan B2r). The session
writes a `land` request for each PR it would land, and lands nothing itself. After the session
ends and the other requests are carried out, `files/run_requests.py` starts the lander for each
nomination in file order, one at a time, with a plain `systemctl start` that blocks until the
landing ends. It goes on after `VERDICT: settled`, `VERDICT: nothing-to-deploy` or a pre-merge
`REFUSED:` from the lander, and starts no further landing after anything else. It lands at most
`renovate_agent_max_prs`, skips a PR the run closed, and starts nothing once
`renovate_agent_run_timeout_s`, the deadline for the session and the landings together, has
passed. A wait cut short by that deadline leaves the landing running in its own unit. The
digest names each nomination the runner did not land, with why.

**The session runs in its own unit** (guardrails plan B2r-2a). The runner starts
`homelab-renovate-agent-session.service`, which polkit lets only the controller start
(`templates/50-renovate-agent-session.rules.j2`), and waits for it through
`agent_toolbox.py:start_unit`, the helper the landings use too. The unit runs
`files/renovate_session.py`: the worktree gate and its recreation, then the headless session.
Its `TimeoutStartSec` is `renovate_agent_session_timeout_s`, so systemd stops a session at its
wall clock and the runner reads `Result=timeout`. The script captures the session's output and
writes it to `session.out` under the exchange directory's `out/`, and a blocked worktree's
reason to `session.blocked`, so session text never reaches the journal. The runner's journal
line for each request carries fixed words only, for the same reason (#8676).

**Two users** (dotfiles#967's I1, guardrails plan B2r-2b). `templates/renovate-agent-pins.j2`
pins both, so no inventory value moves either:

- `renovate-agent` (uid 901) runs the session. It holds the read-only GitHub token and the
  Claude credential, from `/etc/renovate-agent/session.env`, and reads its settings from
  `/usr/lib/homelab/renovate-agent/session.conf`, which carries no secret.
- `renovate-agent-ctl` (uid 905) runs the runner. It holds the write token, from
  `/etc/renovate-agent/ctl.env`, reads `config.env` with the Discord webhook, and keeps the run
  record in its home, `/var/lib/renovate-agent-ctl`. Only it may start the session and lander
  units.

The two meet in `/var/lib/renovate-agent-exchange`, which the controller owns with the session
user's group, mode 2750. The runner recreates `requests/` and `out/` inside it on each run,
group-writable and set-group-ID, so the session can write there but cannot replace either
directory. The runner reads each file there without following a link, and treats a link or a
non-regular file as no output. The controller runs with the session user as a supplementary
group so it can read what the session wrote.

This runner and triage's `claude_triage_run.py` stay separate modules (decided 2026-11-14).
They share only the call that starts a session, and even that differs: the flags, how each
hands the session its token, and how each reads the output. `start_unit` is the part a shared
session start needs, and triage can adopt it when B2 moves its session into a unit.

The lander unit's `ExecStartPre` replaces the last run's line with a `VERDICT: none` line before
the lock wait, and the script writes `PENDING` once it holds the lock. A unit that systemd stops
mid-landing, by `TimeoutStartSec` or a kill, leaves `PENDING` behind. The runner therefore reads
the unit's `Result` first: only `success` and `exit-code` mean the script wrote its outcome, and
anything else is no verdict (#5205).

## The session's own identity

The session reads third-party text: release notes, changelogs and PR bodies. It runs as
`renovate-agent` so that text steering it reaches nothing able to change `master`.

- **Home and clone.** `renovate_agent_home` (`/var/lib/renovate-agent`) holds the session's
  `~/.claude` and its own clone at `renovate_agent_clone_dir`. The run record is in the
  controller's home, which carries the set-group-ID bit and group `sys_user`, so the docs cron
  reads it.
- **Toolchain.** The agent runs its own uv rather than `/usr/bin/uv`, the host's
  package wrapper. `renovate-agent-setup` copies the operator's uv into the agent's `~/.local/bin` and runs
  `uv python install` as the agent. Every project hook runs through `uv run`, and in an
  unattended session a hook that cannot start makes each Bash call an `ask`, which is a denial.
- **Project hooks.** `.claude/hooks/run-hook.sh` defaults to the operator's checkout and uv. The
  unit sets `RUN_HOOK_PROJECT_DIR` to the run worktree and `RUN_HOOK_UV` to the agent's uv, and
  `prepare_worktree` runs `uv sync --frozen` in that tree before the session starts. A guard
  that cannot import its dependencies raises, and the dispatcher drops its verdict without a
  word. `block-footguns` also needs the `claude_guard` package, which the dotfiles repo deploys
  only under the operator's home; without it the guard asks on every command naming `gh`. The
  `renovate-agent-setup` copies the operator's package into the agent's `~/.local/share/claude-guard` on each
  apply.
- **GitHub tokens.** Two fine-grained tokens for this repo alone (guardrails plan B1).
  `renovate_agent_gh_token` is the runner's: pull requests and actions read-write, contents,
  checks and metadata read, in `ctl.env`. `renovate_agent_gh_read_token` is the session's,
  with no write permission at all, in `session.env`. The session asks for a close, a comment
  or a re-run by writing a JSON request into `/var/lib/renovate-agent-exchange/requests/`,
  which `renovate_session.py` passes to Claude Code with `--add-dir` so a headless Write there
  needs no approval. The runner carries each request out with its own token
  after the session ends, through `files/run_requests.py`, and logs each outcome to
  `requests.jsonl` in the state directory. A request must be a regular file under 16 KiB,
  opened without following a link and without blocking, and must name an open Renovate PR the
  run counted. Anything else is refused and logged. The lander merges with the operator's
  token.
- **Claude credential.** `renovate_agent_claude_oauth_token`, from `claude setup-token`. It
  and the read token reach the session as environment variables through
  `/etc/renovate-agent/session.env` (root 0600), which systemd reads as root.
- **Confinement.** `ProtectHome=yes`, `ProtectProc=invisible`, `NoNewPrivileges=yes` and
  `PrivateTmp=yes`. A same-uid sandbox was ruled out first: systemd 255 has no `PrivatePIDs=`,
  so a session running as the operator could read the operator's other processes and user bus.
- **Its config is read-only to it.** `session.conf` is a root-owned packaged file, and
  `config.env` is readable only by root and the controller. A steered session could otherwise
  raise its budget or point `PROMPT_FILE` at a prompt of its own for the next run.
- **The run lock is in the controller's home.** `fs.protected_regular=2` refuses an `O_CREAT`
  open of another user's file in sticky `/run/lock`, and the old lock there belongs to the
  operator.
- **Deploy holds.** The wrapper reads no hold. `land.sh`, run by the lander as the operator,
  refuses a landing under `DEPLOY_HOLD`.

ENFORCED: `hosts/tests/test_renovate_agent_identity.py`, which also runs the prompt's
`systemctl` commands through the repo's own PreToolUse:Bash dispatcher.

## Arming it

`host_secrets.py check` fails a host file that lacks either credential. To arm the agent:

1. Create a fine-grained token for the repo with exactly the permissions above.
2. Run `claude setup-token`.
3. Add both as `renovate_agent_gh_token` and `renovate_agent_claude_oauth_token` with the
   `add-secret` skill, then run `secret_rotation.py sync`.
4. Set `renovate_agent_enabled: true` in `inventory/host_vars/daniel-box.yml`, and let the next
   package version arm the timer.
5. Start one run by hand and watch it. Auto mode's classifier decides whether the session may
   write its request files, and nothing short of a real run tests that.

## Exercising the wrapper without arming anything

`RENOVATE_AGENT_CONFIG` overrides the config path, so the I/O shell can run end-to-end against a
throwaway config before the timer exists. Point `REPO` at the real repo with the backlog empty and
the gate returns before it spends a session, which makes it a free pass through config parsing,
the `gh` census and the return path:

```bash
RENOVATE_AGENT_CONFIG=/tmp/agent-test.env \
  PYTHONPATH=hosts/lib \
  uv run python hosts/components/renovate-agent/files/renovate_agent.py
```

The `PYTHONPATH` stands in for the package's copies. `component.yml` installs
`host_lib.py` from `hosts/lib/` and the `gitops_*` modules from `gitops_deploy/files/` beside
the script in `/usr/lib/homelab/renovate-agent/`, so a run from the repo cannot import them without it.

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
parks `homelab-checkout-sync` silently, and a session that edits, renders and tests is guaranteed to
leave some. Each tick recreates `.claude/worktrees/renovate-auto` at `origin/master` inside the
agent's own clone and runs there. The lander runs `land.sh` in the primary checkout as the
operator.

Every rule in the rest of this section lives in `files/run_worktree.py`.

**A worktree holding work is not thrown away.** If the previous run left uncommitted changes or
commits whose content is not on `origin/master`, the run skips, posts the path and exits
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

When the result JSON carries a `session_id`, the digest prints a `resume this session:` line
above its cost line (#4603). The line holds a command that reopens the session that wrote a
`manual —` hand-off with its context intact. The harness stores a session under its owner's
home and keys it by the working directory it ran in, so a resume as another user or from
another path does not find it. The line sits outside the 900-character summary cut, so a long
summary cannot truncate it.

The command is `sudo sh -c 'cd <run worktree> && set -a && . <EnvironmentFile> && exec runuser
-u renovate-agent -- env <unit variables> claude --resume <id>'`. The session's GitHub
token and Claude credential exist only in the unit's root-only `EnvironmentFile`, so the
command reads that file as root before it switches user. `sudo` resets the environment, so the
command also sets again each variable the session unit sets: `PATH`, `HOME` and the hook
shim's `RUN_HOOK_*` paths among them. The runner reads their values with `systemctl show -p
Environment` on the session unit. `SESSION_ENV_KEYS` in `files/renovate_agent.py` lists them,
and `test_session_resume.py` fails when the unit sets a variable that list lacks.

The transcript lives under the owner's home rather than in the worktree, so it survives the
next tick's `prepare_worktree`. A resumed session works in the tree a tick reuses, though. Any
commit it leaves there unlanded blocks the next tick, so finish the work before 06:00.

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

Every tick appends one JSON line to `runs.jsonl` in `STATE_DIR` (`/var/lib/renovate-agent-ctl`), so
whether the agent is worth its cost is a count over one file rather than a read of 30 digests
(#2864). `run_record` in `files/agent_logic.py` builds the line, and each exit path writes one:

- `skipped` — the gate spent no session. `reason` says why, including the quiet empty-backlog
  skip that posts nothing to Discord.
- `blocked` — the run worktree still holds unlanded work.
- `ran` or `failed` — a session ran. The line carries `merged`, `closed`, `unread`,
  `left_open`, `touched` and `opened` from the measured delta, plus `cost_usd`, `turns` and the
  denial count, and `session_id` when the result JSON carried one (#4603). Lines written
  before #3421 also carry `handed_off`, which no reader uses.
- `crashed` — the wrapper threw. `report_crash` writes this line before its Discord post.

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
  /var/lib/renovate-agent-ctl/runs.jsonl
```

The file gains one line a day and is never rotated.

## The alive verdict

The unit's `ExecStartPost` writes the `renovate-agent` textfile verdict that
`RenovateAgentNotReporting` reads (`other-setup-checks.yaml`). It runs only when the wrapper
exited 0, so the rule reports silence and the `OnFailure` alert reports failure. M6 batch B4's
Cutover-1a deleted the `Renovate Agent — Alive` Kuma tile and the unit's beat to it, and B4's
token PR retired its push token a week later.

**A worktree-blocked skip exits non-zero, so it writes no verdict.** The verdict is
`ExecStartPost`, which runs after ANY exit 0, so a `down` written from inside the wrapper on a
`return 0` path would be overwritten by the `up` that follows it. That skip returns
`EXIT_WORKTREE_BLOCKED` instead: no verdict, `OnFailure` pages, and `RenovateAgentNotReporting`
fires by deadman if nobody clears the tree. Until 2026-09-18 that path returned 0, and the
monitor stayed green through five daily skips (#2014). The other skip, with no open PRs,
still exits 0, because it is the healthy steady state.
`test_agent_logic.py::TestSkipExitCodes` pins the blocked and the quiet case.

**A crash records its own reason** (`report_crash`, called from the `__main__` guard). Silence
plus an `OnFailure` page was not enough: the rule fired 28 hours later by deadman expiry with no
reason attached, which is what a host that is simply off looks like. For two days from 2026-09-08
that hid a one-line `prepare_worktree` failure, a directory at the run worktree's path that
`git worktree list` had no record of (#1477). The report writes a `crashed` line to the runs file
and posts the exception text to Discord. The deadman stays as the backstop for a host that is
genuinely off. The report is best effort and never re-raises, so a failed report cannot replace the
traceback that says what broke. ENFORCED:
`hosts/components/renovate-agent/tests/test_crash_report.py::test_a_crash_posts_the_exception_text_to_discord_and_the_runs_file`.

**The unit and `config.env` carry no Kuma push.** ENFORCED:
`hosts/tests/test_renovate_agent_unit.py::test_the_unit_and_its_config_make_no_kuma_push`.

The verdict's deadline is 28h, not the 25h the other daily checks use, because the verdict lands
at the end of the run: a fast run followed by one that draws the full jitter and runs to the
100-minute unit timeout spaces two verdicts 25h50m apart. `test_renovate_agent_unit.py` pins the
deadline between that gap and two periods, and `test_b4_rule_literals.py` pins the rule's literal
to it.

**An unregistered directory at the run worktree's path is reclaimed, not a crash.** That is what a
killed session leaves behind. It matters twice over, because **git searches UPWARD for a
repository**: `git -C <orphan dir> status` resolves to the PRIMARY CHECKOUT and answers about that
tree, so `worktree_is_reusable` was reading the wrong repo — a dirty primary would have read as
this run tree holding uncommitted changes. `is_registered_worktree` decides which case it is, and it
fails CLOSED — an unreadable `worktree list` reads as a directory git owns — so nothing removes a
tree that might be registered. Every case that holds real work is still refused by `worktree_is_reusable` before
this runs.

## The denylist marker's own history

The session never touches a PR whose title or branch carries `k8s_autodeploy: false`. Two rules
keep that marker reachable:

- **Read the branch as well as the title** (#2641). Renovate titles a single-dependency group
  `Update <dep> …` and drops the group name, so the marker can survive in the branch alone,
  slugified as `k8s_autodeploy-false`. The prompt therefore reads `headRefName` beside the title,
  and `test_the_prompt_leaves_a_denylisted_pr_to_a_person` pins both tells.
- **Every rule carrying the marker sets `groupSingleUpdates: true`**, which applies the group's
  `commitMessageTopic` to a one-dependency branch too and so puts the marker in its title.
  `tests/deploy/test_renovate_denylist_marker_reaches_the_pr_title.py` holds it.
