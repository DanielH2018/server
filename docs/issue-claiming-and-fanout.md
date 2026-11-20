# Issue claiming and fan-out

Several Claude sessions work this repo at once. `findings.py` gives the backlog a status field
and an owner-of-record, but nothing says which *session* is working an issue right now, and
nothing marks an issue as yours alone. Two sessions can pick the same issue, and an issue you
want to keep for yourself gets picked up by the next session that reads the register.

This page describes three things in `scripts/dev/findings.py` and `.claude/skills/issue-fanout/`:
a claim protocol, a `manual` reservation, and a fan-out skill that dispatches agents at a
batch of issues after triage.

## What already exists

`scripts/dev/findings.py` files, re-observes, escalates and closes findings as GitHub Issues.
It is split four ways: the CLI, `findings_lib/issue_model.py` (vocabulary and pure reads),
`findings_lib/plans.py` (the `gh` argv every command plans) and `findings_lib/gh_calls.py` (the calls). Every
command plans a list of argv first, then runs it, so `--dry-run` writes nothing.

`scripts/lib/worktrees.py` decides whether a session worktree is done with: `is_merged`,
`classify` and `worktree_facts`. `scripts/dev/prune_worktrees.py` is only the CLI over it, and
nothing else imports the CLI. `.claude/hooks/session-health.py` imports `parse_worktree_list`
and `lock_is_live` from `lib.worktrees` to print the other-live-sessions banner, so a
`fanout-<batch>` lock counts only while its batch unit is active (#8576). Since #2133
`parse_worktree_list`, `session_is_alive`, `Worktree` and the cherry and merge-tree readers come from the
`claude_worktree` module the dotfiles repo deploys to `~/.local/share/claude-worktree`, and
`lib.worktrees` re-exports them. `scripts/lib/_claude_worktree.py` is the bootstrap, and it
raises rather than falls back, so a host without the deploy prunes and reaps nothing.

Every open issue except #3 carries the `claude` label. #3 is Renovate's Dependency Dashboard,
created 2026-06-06 with no labels. Scoping claims to `claude`-labelled issues therefore costs
nothing, and the claim protocol keeps that scope.

## Why the claim is not an assignee

`gh` authenticates as a single account. Assigning an issue would name the operator, not the
session, so every claim would be indistinguishable from every other. The claim has to carry a
session-level identity itself.

## Vocabulary

`LABELS` in `findings_lib/issue_model.py` holds every label that `sync-labels` creates. The table
below is generated from it. This spec added `manual`, a grey state marker. A claim carries no
label: the Claim comment is the record, and the Project board's Status shows it (see *The
Project board follows the issue*).

--8<-- "assets/generated/fragments/findings-labels.md"

`manual` is one word rather than a prefix. The Renovate convention already uses "manual — …"
inside group names, so the bare word is mildly overloaded; the label namespace is separate
enough that this has not been worth a longer name.

A date is not a label. It is the Not Before issue field, a date field the account owns, which
`defer` and `open --not-before` set. It means "workable on that date, not earlier" and it
expires by comparison: `next` and `claim` read it against today's UTC date, so nobody clears
it. That is the property `manual` lacks. #1288 could be re-derived only once seven days of a
metric existed, said so in its body, and was claimed, read and released unchanged six times in
four days because the only withholding label was permanent and meant the wrong thing (#1739).
Severity is the Severity issue field, `High`, `Medium` or `Low`, which `open --severity` sets.
A kind is the issue's type: `open --kind gap` sets Gap, `improvement` sets Improvement and
`feature` sets GitHub's own Feature. The operator created Gap and Improvement under
Settings > Planning > Issue types.

All three replaced labels: one `not-before:<YYYY-MM-DD>` label per date, three `severity/*`
and three `kind/*`, where `kind/addition` was the name for `feature`. `gh issue list --json`
returns the type as `issueType`.
`gh issue list --json` cannot return an issue field, so every loader in
`findings_lib/gh_calls.py` adds them with `attach_fields`, one aliased GraphQL read per 100
issues (0.79s and one point for 113 issues, measured 2026-11-19). A failed read fails the
command, because an issue missing its date reads as not deferred, and `next` would offer it.
Until [#9120](https://github.com/DanielH2018/server/issues/9120) closes, the readers still
take a `not-before:`, `severity/` or `kind/` label as a fallback, the later date winning,
because a session on a pre-field checkout writes only labels.

Issue search filters a field directly, as `field."not before":>2026-12-01`, which a dated
label never allowed. The "Deferred, by date" table on the generated backlog page still lists
every deferred issue soonest first, marked *withheld* or *date passed*:
`scripts/docs/reference/backlog.py` reads the same `not_before`, so it never disagrees with
`next`.

A date is the wrong tool for work that waits on another issue. A guessed date either expires
before that work lands or holds the issue back after it has. On 2026-11-09, #7195 waited on
#7178, which had closed on 2026-11-06, while #7195's date kept it hidden until 2026-11-20. `findings.py block <n> --on <m>`
records GitHub's native "blocked by" relationship instead. `next` and `claim` read it from the
`blockedBy` field of the same `gh issue list` call that returns the labels, and withhold the
issue while any blocker is open. An issue frees itself when its last blocker closes, so nobody
clears the relationship. The issue page and the Project board show a "Blocked" badge from it,
and the backlog page lists it in its blocked section. Work that waits on a set of
changes with no issue needs one filed first, because only an issue can block. A block and a
date can apply together, as for work that waits on an issue and then on a retention period;
`next` offers the issue once both have passed.

## The claim record

A claim is a **comment**, following the machine-readable trailer convention
`issue_model.trailer()` already uses for fingerprints:

```
Claimed by `worktree-issue-1132` (session `cse_01ABC…`) at 2026-09-05T18:40Z

Claim: `worktree-issue-1132`
```

A release is the matching form:

```
Released by `worktree-issue-1132` at 2026-09-05T21:02Z — landed in #1270

Released: `worktree-issue-1132`
```

Reading who holds an issue means folding the comment list forward in `createdAt` order: a
`Claim:` line opens if no claim is open already, and a matching `Released:` line closes.

**The first writer wins, not the last.** gh returns comments in `createdAt` order, so the
earlier of two racing claims is the earlier comment. Letting a later claim overwrite a live
one would mean a session could take an issue out from under another by claiming it again —
and worse, the first claimant's own `release` would then be refused, so it could not clean up
after losing.

**Why a comment rather than a body edit.** Comments are append-only and carry `createdAt`.
Two sessions commenting concurrently both succeed and the ordering is total, so a read-back
settles which one holds the issue. Two sessions editing a body race, and the loser's write
disappears with no trace.

**Why no compare-and-swap.** GitHub offers none, and the fan-out does not need one: the
orchestrator claims every issue of a batch *before* that batch's agent starts, so a fan-out has
no internal race. `fanout.py place launch` takes those claims itself. The only residual race is between independent ad-hoc sessions, where the
cost of losing is a duplicated triage rather than corruption. The read-back handles it —
the session whose claim comment sorts first holds the issue, and the loser releases.

## The Project board follows the issue

The "Server Issue Tracking" Project shows every open finding. `findings_lib/board.py` derives
one field from the issue alone: **Status**, which is `Done` once the issue is closed, `In
Progress` while a worktree holds the issue's `Claim:` comment or an open pull request names it
as closing, and `Todo` otherwise.

The board shows Severity and Not Before as columns read from the issue fields, so nothing
keeps a copy of them in step. The account owns their definitions: the operator created both
under Settings > Planning > Issue fields, with Public visibility. The gh token cannot read or
create the account's definitions, but a repository's GraphQL `issueFields` connection lists
them, which is what `gh_calls.set_fields` reads; a missing field or option fails the write with a line naming
where to add it.

The board's Status is the one claim state a person reads. GitHub shows it in each issue's
sidebar and logs its changes on the issue's timeline. It is not the issue's own `status`
field, which is a preview this account cannot set through the API, so GitHub reserves the
name `Status` and an issue field cannot take it.

`findings.main` records every issue a subcommand writes to through `gh issue
edit|close|reopen|create|comment`. When the subcommand returns, `main` syncs those issues' board items
and adds an issue the board lacks. So `claim` moves an issue to `In Progress`, and `release`,
`reap`, `manual` and a stale-claim takeover move it back, with no per-command board code. A
dry run writes nothing, so it syncs nothing. Another repo's register (`--repo`) has no board,
so it is skipped.

The issue stays the record: `next`, `claim` and the fan-out never read the board. A board
write that fails therefore prints one warning naming the issues and leaves the exit code
alone. `findings.py sync-board [<n> ...]` repairs the board, and with no numbers it checks
every open issue. Two paths change an issue without findings.py: a label someone changed in
the GitHub UI, and a session whose gh token cannot reach the board, such as one run as the
`claude` user. To catch both, `homelab-board-sync.timer` on daniel-box runs `sync-board` over
every open issue hourly, as `ubuntu` (`hosts/components/board-sync/`). Writing the board needs
the `project` scope on the gh token, which the `ubuntu` user's token carries.

## Staleness: a claim expires with its worktree, not with a pid

A claim is reclaimable when either holds:

- the worktree it names is absent from `git worktree list`, or
- `lib.worktrees.classify` already judges that worktree REMOVABLE — merged into `origin/master`,
  clean, and holding no live session lock — and no live process works in it.

The last condition covers a session the Claude desktop app starts. The app creates the
session's worktree itself, unlocked, on a `claude/<name>` branch. Between a clean commit and
the next edit that tree reads merged, clean and unlocked. So `claim_is_live` in
`scripts/dev/findings_lib/claim.py` also holds a tree that a process this user can read has
as its cwd or `CLAUDE_PROJECT_DIR`, through `lib.worktrees.held_by_readable_process`. An
unreadable process counts for nothing there, unlike in `remove`, because a hold with no
evidence would stop `reap` releasing any claim. The main worktree is never held this way:
every session tree sits under it, sessions also run in it, and its branch is `master`, which
no claim may name.

A claim names a branch, in either session form: `worktree-<name>` from `EnterWorktree` or
`claude/<name>` from the desktop app (`scripts/lib/worktree_owner.py:SESSION_BRANCH_PREFIXES`).
The pruner's branch sweep deletes an orphan branch of either form once its work is on
`origin/master`. It keeps a branch that any open claim names, or that an open PR has as its
head, and it keeps every landed branch when it cannot read the claims or the open PRs
(`scripts/dev/prune_worktrees.py:judge_branches`). A stale claim therefore holds its branch
until `reap` releases the claim.

A claim is **not** reclaimable when the named worktree still exists with uncommitted changes,
even if its lock owner is dead. That case is the 2026-09-05 incident: the container restarted,
killed 14 agents mid-work, and every worktree kept its uncommitted edits so each session was
resumed in place. Expiring those claims would have handed half-finished issues to a second
agent, which is worse than leaving them held.

There is no heartbeat and no TTL. Both would have expired exactly those claims, because the
process was gone while the work was not.

Reusing `lib.worktrees` is what makes this self-healing without either. The staleness
question — is this worktree done with — is a question that module already answers for
`prune_worktrees.py`, and `session-health.py` already imports it from outside `scripts/`.

A claim on another repo's register is judged against that repo's worktrees. With
`--repo DanielH2018/dotfiles`, `findings.py` reads `git worktree list` in the chezmoi checkout
and merges against its `origin/main`, through the checkout table `REGISTER_CHECKOUTS` in
`scripts/dev/fanout_lib/target.py`. A repo missing from that table makes `claim`,
`claims`, `reap` and `next` exit 2. Against this repo's worktrees, every claim on it would
name a branch nothing here has checked out, so each would read as stale.

## Commands

`findings.py --help` lists the subcommands, and `findings.py <cmd> --help` owns each one's
flags. This page does not restate the flags, because a third copy drifts whenever a flag changes.
The table of subcommands is generated from the `add_parser` calls in `findings_lib/cli.py`.

--8<-- "assets/generated/fragments/findings-subcommands.md"

`docs/reference/scripts.md` maps the modules behind the CLI. The argv goes in
`findings_lib/plans.py`, the parsing in `findings_lib/issue_model.py`, and the `gh` calls in
`findings_lib/gh_calls.py`. Every write command has a dry-run mode that plans and writes nothing.

Four design rules hold across the subcommands, and no docstring states them:

- **The register is the operator's issues only.** Any collaborator can open a
  `claude`-labelled issue, and `next`, `show` and `claim` hand its body to a session as work
  (dotfiles#967 slice 1). Every list skips an issue another account opened, and names each
  skipped issue and its author on stderr. Reading one such issue by number exits with an
  error. `REGISTER_AUTHOR` in `findings_lib/issue_model.py` names the account. Issues that
  DanielClaudeBot files are skipped too, so a session running as the `claude` agent user
  files findings that `next` does not offer.

- **A claim that reads stale the moment it lands is worse than no claim.** It reads as
  protection while `next` re-offers the issue and `reap` releases it. So `claim` checks the
  worktree it is given before it writes. A name that matches no branch fails that check, and
  so does a branch git never locks, such as `master`.
- **No view hides a row.** `list` marks manual, deferred and claimed issues instead of dropping
  them, and `next` names each deferred issue after the free rows. A hidden row is how an issue
  like #1132 stops being visible to anyone, including the operator who reserved it. An issue
  that an open project reserves is counted by `next` and marked by `list` for the same reason.
- **Every path that closes or reopens an issue releases its claim.** `claims`, `reap` and
  `next` read only open issues, so a claim stranded on a closed issue leaves every view at once.
  A PR body's `Closes #<n>` posts no release, and the next re-observation that reopens the issue
  would otherwise bring the claim back live.

## Session milestones and project milestones

A session milestone is a GitHub milestone that groups one branch's focused work.
`findings.py milestone start "<title>" --worktree <branch>` opens it, and no other command
makes a branch one. `claim` files the branch's issues under it. The `session-milestone` workflow files the
branch's PR, and files a fan-out batch's PR under the milestone of the issue that PR closes.
The workflow closes the milestone once nothing in it is open, and a later claim or PR
reopens it. The milestone's description names its branch. The tools touch a milestone whose
description they can parse, and a group, which *A group is a backlog no branch owns*
describes. `scripts/dev/findings_lib/milestone.py` owns the scheme.

A session milestone holds only claimed issues: `release`, `reap` and `manual` take an issue
back out. So it cannot hold work nobody has started.

A project milestone is a session milestone that also holds a backlog.
`milestone start --project "<title>" --worktree <branch>` opens one, and its description
says `project` where a session milestone's says `session`. One sub-coordinator session owns
one project: the project is that session's milestone, and the session runs `issue-fanout`
over the project's backlog. A project differs from a session milestone in five ways:

- `milestone add <n...>` files unclaimed issues in the backlog. It refuses a closed issue,
  a claimed issue, and an issue that another milestone holds.
- `release`, `reap` and `manual` leave the issue in the project. A batch that fails and
  releases its issue returns that issue to the backlog.
- An open project reserves its backlog for its branch. *An open project reserves its
  backlog* has the rule.
- A claim by another branch leaves the issue in the project. Such a claim exists only when
  it predates the issue's filing in the project, or when the project had closed. The
  claimer's own session milestone does not take the issue out of the backlog.
- `next --milestone "<title>"` lists the project's free issues, and `list` and `new-issues`
  take the same flag. `--milestone none` selects the issues that no open milestone holds,
  which is the pool a root session assigns from. A title that no milestone has exits 64
  rather than printing an empty list. `cc-wait findings --milestone "<title>"` waits on one
  project. It records the project's free issues when the wait starts, and ends on a free
  issue outside that set, including an older issue that `milestone add` files later. It
  takes no `--after`.

Claim filing, PR joining, the close at zero open items and `reap` treat both forms alike. A
project with issues in its backlog has open items, so it stays open while its backlog lasts.

### An open project reserves its backlog

An issue filed in branch X's open project milestone is offered to branch X's runs only, and
only they can claim it. `findings_lib/milestone.py:reservations` is the one place that
decides who may take an issue. `next`, `list`, `new-issues` and `claim` all read it, and a
fan-out batch claims through `claim`.

- `next` without `--milestone` does not offer a reserved issue. It counts the reserved
  issues that are otherwise free on one line, by project:

    ```text
    2 reserved by a project (2 in `Alert noise`); --milestone "<title>" to list
    ```

    With `--json` the line goes to stderr, so the array stays the free set.
  `next --milestone "<title>"` lists the project's own backlog as before. `new-issues`
  reads the same free set, so a plain `cc-wait findings` does not end on a project's issue.
- `list` marks a reserved issue `[project:<title>]`.
- `claim --worktree <branch>` refuses a reserved issue with exit 3, and names the project
  and its branch. The project's own branch claims it. So does each of that branch's fan-out
  batches, because `fanout.py place launch` claims every batch under the orchestrator's
  branch. A batch from any other orchestrator is dropped from its launch with
  `dropped #<n>: claim refused (3): #<n> refused: reserved by project ...`.
- `claim` does not reap a stale claim on a reserved issue, because the reap would free
  nothing for the claimer. A claim that a branch already holds stands, and re-running it
  writes nothing.
- A closed project reserves nothing. A session milestone reserves nothing either: it holds
  only what its branch already claimed.

The way out is `milestone remove <n> --worktree <the project's branch>`, which the refusal
names. It takes the issue out of the backlog, and any branch can then claim it. `claim` has
no flag that overrides the reservation: the operator lifts it where it is recorded, as
`manual <n> --clear` lifts a `manual` label.

gh's issue JSON carries a milestone's title and description but not its state. So
`reservations` reads the open milestones, and only when an issue sits in another branch's
project. A register with no project costs no extra read.

The first two-project run showed why the rule exists. On 2026-11-19 the project "Validate
and CI cleanup" filed #9314 in its backlog at about 21:20 UTC. At 22:02 UTC an orchestrator
outside the tree ran a plain `next`, was offered #9314 and claimed it. The sub-coordinator's
own claim was then refused, and the milestone still listed an issue another session was
working.

Each way in has a way out:

- `milestone add` is reversed by `milestone remove <n...>`, which refuses a claimed issue.
  To take a claimed issue out, `release` it first. `milestone add` is also what reserves an
  issue, so `milestone remove` is what frees it for other branches.
- `milestone start` is reversed by `milestone close`, and `start` with the same title
  reopens the closed milestone. `milestone status` prints the form, then
  the milestone's open and closed issues, then its open and closed PRs as a separate pair
  (`scripts/dev/findings_lib/milestone.py:counts`).
- No command converts a milestone between the session and project forms. `start` refuses a
  title the branch already holds in the other form. To change form, close the milestone and
  start the other form under another title.
- `milestone group` is reversed by `milestone close --group "<title>"`, and `group` with
  the same title reopens it. `milestone status --group "<title>"` shows it.
- `milestone start --project` adopting a group or a project is reversed by `milestone
  disown --worktree <branch>`. `milestone status --worktree <branch>` shows the result.

The operator decided on 2026-11-19 that a project is the sub-coordinator's session
milestone, not a second kind of milestone. A milestone with no owning branch cannot take a
claim, close when its work finishes, or reopen, and a project gets all of that from the code
that serves a session milestone. That still holds for running a project. On 2026-11-20 the
operator ruled that preparing one is separate: a backlog is grouped ahead of time, and a
sub-coordinator takes it up later (#9535).

### A group is a backlog no branch owns

`findings.py milestone group "<title>" <n...>` makes a group and files the issues in it. It
takes no `--worktree`. A group differs from a project in three ways:

- It reserves nothing. `reservations` reads only a project, so a plain `next` offers a
  group's issues and any branch claims them.
- Nothing closes it on its own. The workflow and `reap` close only a milestone a branch
  owns, so a group stays open while it is empty.
- `status`, `close`, `add` and `remove` name it with `--group "<title>"` in place of
  `--worktree <branch>`. `milestone add` and `milestone remove` apply the same refusals as
  they do for a project.

`scripts/dev/findings_lib/milestone.py:is_group` reads a group by its description, which is
`GROUP_DESCRIPTION` or blank. A milestone made by hand with no description is therefore a
group. A milestone with a description of its own is not, and no command adopts or changes
it.

`milestone start --project "<title>" --worktree <branch>` adopts a milestone of that title
in two cases, and `scripts/dev/findings_lib/milestone.py:start` owns both:

- The milestone is a group. `start` writes the branch's project description and opens the
  milestone. The backlog stays in it and is reserved from then on.
- The milestone is another branch's project, and nothing holds that branch. The test is the
  one `reap` applies before it closes an idle milestone,
  `scripts/dev/findings_lib/claim.py:branch_is_held`: a worktree on either host holds the
  branch, or the other host cannot be read.

`start` refuses a project whose owner is live, and names the owner and the command that
hands the project on. It refuses a session milestone of another branch outright. A plain
`start` without `--project` adopts nothing and names the flag.

`milestone disown --worktree <branch>` returns the branch's open project to a group without
closing it. The backlog stays, the reservation ends, and another branch's `start --project`
adopts it. Issues the branch still claims stay claimed.

## What a fan-out agent may not do

A fanned-out agent may close an issue with `close --fixed --pr <n>` and nothing else.

`--refuted` and `--accepted` are terminal: `plan_open` returns early on both, so the fingerprint
can never be re-filed. An agent holding that authority could permanently bury a real finding,
and the burial would be invisible — the next review simply never re-files it. Both stay
operator-only.

This is a rule in the skill brief, not a flag guard, per the repo's escalation ladder: a check
is what a rule becomes after it has actually been violated.

## Attribution: the worktree name is the record

A worktree's name carries the issues it is working, so the mapping is derived rather than
recorded a second time:

| Case | Worktree | Branch |
|---|---|---|
| One issue | `issue-1132` | `worktree-issue-1132` |
| Several | `issue-1132+1140+1175` | `worktree-issue-1132+1140+1175` |

`EnterWorktree` allows letters, digits, dots, underscores and dashes, up to 64 characters,
which bounds a multi-issue name at roughly five issues.

What this buys:

- **Issue → session** is the `Claim:` comment.
- The `session-health.py` banner prints `worktree-issue-1132` where it prints
  `worktree-agent-a1f5b5e3cdf2f9684` today, so the other-live-sessions list becomes readable
  at a glance.
- `prune_worktrees.py` reports the same way.
- The PR body carries `Closes #1132`, so the merge closes the issue and the `land.sh` verdict
  attaches to it.

### Measured: a subagent cannot own a named worktree

The naming table above describes what an operator-driven session does. **A fanned-out subagent
cannot reach it.** Measured 2026-09-05 with one probe agent:

| Mechanism | Result |
|---|---|
| Agent calls `EnterWorktree` with `name:` | Refused. "EnterWorktree cannot create a worktree from a subagent with a cwd override (isolation: "worktree" or explicit cwd) — it would mutate the parent session's process-wide working directory." |
| Agent calls `EnterWorktree` with `path:` into a pre-created worktree | Accepted by the tool, then every subsequent command refused: "This agent is isolated in the worktree … Refusing to run it there." |

The refusal names a third mechanism — spawn the agent with `cwd` set to the worktree — but the
Agent tool as exposed here takes no `cwd` parameter. So a fanned-out agent gets the
auto-generated `agent-a0291ece…` name and keeps it.

**What this costs, and what it does not.** Issue-to-session attribution is unaffected: the
claim comment records whatever worktree name the agent reports, auto-generated or not, and
`claims` renders the mapping. Session-to-issue attribution loses the readable branch name, and
a `.claim` label restores it (see *Labels for worktrees Claude Code named* below).

**An orchestrator reads its own claims with `claims --worktree <its branch>`.** The filter keeps
the claims that branch holds and adds the branch of every standing batch a `fanout.py place
launch` run under it started in the same register, read from the run manifests. A batch in this
repo is claimed under the orchestrator's branch, so the branch alone covers it. A dotfiles batch
is claimed under its own branch, and only the manifest ties it back. The filter counts the claims
it drops rather than hiding them, so an empty result does not read as a register with nothing claimed.

A session an operator drives can still name its own worktree `issue-1132`, because it has no
cwd override. Only the fan-out is constrained.

### Labels for worktrees Claude Code named

Claude Code names a worktree itself on two paths, and neither takes a name from the caller.
An `Agent` call with `isolation: "worktree"` gets `agent-<agentId>`. A Remote Control session
gets `bridge-<sessionId>`, created before the phone sends its first prompt, so no description
exists to name it from. No `worktree.*` setting changes either name.

A session in such a tree runs `scripts/dev/label_worktree.py`, which writes a one-line `.claim`
at the worktree root. The root deny in `.gitignore` keeps the file out of every commit, and it
is removed with the tree. The script refuses a checkout outside `.claude/worktrees/`, because
one untracked file in the primary checkout parks `homelab-checkout-sync`. Three places print the label
beside the name, with no network call:

- `session-health.py`'s other-live-sessions list.
- `prune_worktrees.py`, in both `--brief` and the long report.
- `session-health.py`'s own-tree prompt. A session that starts in an unlabelled `agent-` or
  `bridge-` tree is told to label it once it knows its task, which is how a Remote Control
  session learns to do it.

The local fan-out brief makes the label the agent's first act, beside the `Worked by` comment.

`DECIDED:` the names stay generated, and a label sits beside them. A `WorktreeCreate` hook can
choose the directory, but it replaces worktree creation for every session in every repo. It
would have to redo what `worktree.baseRef: fresh` does, keep the `worktree-<name>` branch shape
that `lib/worktree_owner.py` and `prune_worktrees.py` parse, and delete its own branches,
which Claude Code never does for a hook-created worktree. Its input carries only `name`,
`session_id`, `transcript_path` and `cwd` (Claude Code 2.1.296), so it cannot tell which of
several parallel `Agent` calls a given `agent-<id>` belongs to.

## Placement across hosts

`scripts/dev/fanout.py place` names each worktree `fanout-<batch>` because it creates the
worktree itself rather than going through the Agent tool, which only ever produces the
`agent-<hash>` name above. Claims stay under the orchestrator's own worktree because
`findings.py` reads `git worktree list` on daniel-box only, so it cannot see a claim naming a
worktree that lives on daniel-server. A daniel-server agent stops at `gh pr create` and does
not land its PR, because only daniel-box runs deploys. The manifest under
`~/.claude/fanout/<run-id>.json` is what `status`, `clean`, and the SessionStart banner's
`remote_fanout_lines` (`.claude/hooks/hooklib/worktree_lines.py`) all read to find a fan-out's
live worktrees on the other host.

A reader keeps the batch keys it does not know. `Batch.from_json` in
`scripts/dev/fanout_lib/manifest.py` holds them in `Batch.extra`, and `Batch.to_json` writes
them back beside the fields, so a `clean` run by one build does not erase what another build
recorded. `to_json` never nests them under an `extra` key, because a reader that passes every
key to the `Batch` constructor skips the whole run on a key it has no field for.

### One launch at a time, and files held across runs

Several coordinators launch from one host as one user: a root session, and one
sub-coordinator per project. Each calls `fanout.py place launch`, and each reads the same
manifest directory. Two rules keep their launches apart.

**One launch runs at a time.** `launch` takes an exclusive `flock` on `launch.lock` in the
manifest directory before its first gate and holds it through its last manifest save
(`scripts/dev/fanout_lib/launch_lock.py:held`). Without the lock, two launches both read
the manifests before either saves, so both pass the live-batch cap. Refill waits make that
overlap the normal case, because every waiting coordinator wakes on the same freed slot. A
launch that waits `scripts/dev/fanout_lib/launch_lock.py:WAIT_S` seconds without the lock
exits `scripts/lib/exit_codes.py:FANOUT_PLACE_LOCK_TIMEOUT` and prints the holder's pid and
run id. The kernel releases the lock when its holder exits, so a killed launch leaves
nothing to clear. The `fanout-headroom` wait source reads `full` while a launch holds the
lock (`scripts/dev/fanout_lib/launch_lock.py:holder`). The lock also makes run ids unique:
`scripts/dev/fanout_lib/manifest.py:unused_run_id` picks the id once the lock is held.
Two launches started in one second once shared an id, and the second saved over the
first's manifest.

The lock covers the launches that share a manifest directory, which is one user on one
host. A launch from another host, or as another user, takes a different lock and reads
different manifests. Every coordinator runs on daniel-box as `ubuntu`, so one lock covers
them all.

**A live batch holds the files it cites and the files its open PR has edited.** `launch`
records on each batch the files its issues cite (`Batch.cited_paths`), resolved as the
within-launch check resolves them (`scripts/dev/fanout_lib/launch_gates.py:batch_paths`).
The files a batch has edited are the files its open PR changes, which
`scripts/dev/fanout_lib/transport.py:open_pr_files` reads from GitHub at each launch.
`scripts/dev/fanout_lib/launch_gates.py:refuse_live_shared_files` then refuses a launch
that cites a file a live batch in another run holds. The launch's own side is the files its
issues cite, because its agents have edited nothing yet. The refusal names the file, the
run and the orchestrator branch the run was launched under, and says whether the live batch
cites the file or has edited it
(`scripts/dev/tests/test_fanout_launch_gates.py::test_launch_refuses_a_file_the_open_pr_of_a_batch_in_another_run_has_edited`).
Ask that coordinator to finish or release its batch. Do not clean another coordinator's
run.

A batch is live for this rule while its unit is active or its branch has an open PR. An
uncleaned batch is not live for that reason alone: on 2026-11-19 `place runs` listed over
20 standing batches, the oldest from 2026-10-22. A parked batch has an open PR, so it keeps
its files. The gate reads the open PRs and the files each changes with one `gh pr list`
call, which costs the same GraphQL point as reading the branches alone. That call returns
at most 100 files of a PR, so a PR that changes more, on a standing batch's branch, costs
one paginated REST read for the rest. The gate reads a unit's state only for a batch with
no open PR that could collide. A read that fails refuses the launch. A batch saved before
`cited_paths` existed has its issue bodies fetched instead.

`--allow-shared-file <path>` excuses a path here as it does within one launch, and the
launch records the path on the new batch as `Batch.excused_paths`, where the holder's
coordinator can read it with `place runs --json`.
`scripts/dev/fanout_lib/launch_gates.py:excusable` rules out `secrets/secrets.yml` and
every `secrets/cluster-vars.` file: each is one encrypted document, and two branches that
edit it cannot be rebased over each other.

`scripts/dev/findings_lib/issue_model.py:cited_paths` is the one reading of an issue body
that both gates, the brief and the `paths` field of `next --json` share. It resolves each
citation against the files `git ls-files` lists. A path from the repo root counts as
written. A path from a subdirectory, such as `fanout_lib/clean.py`, counts as the one
tracked file that ends with it. A filename with no directory counts only when it stands
alone in an inline code span and exactly one tracked file has that name: `` `ci.yml` ``
holds `.github/workflows/ci.yml`.

The rule has four known limits:

- **A batch that has opened no PR holds only the files it cites.** An agent pushes its
  branch when it opens its PR, so GitHub has none of its edits before then. Two batches
  that run at once and cite different files can still edit one. A launch is compared by
  its cited files for the same reason, so a file it goes on to edit without citing is
  checked against nothing. In the run of 2026-11-19, one fix changed 11 files and its issue
  cited three of them (*The first two-project run*).
- **A change that lands on master is not a live batch.** The gate compares batches, so a
  file master changes under a running batch is held by nothing. That batch finds out at
  its landing, as a red CI run or a `merge-conflict` verdict, and the conflict resume
  below covers the second.
- **A filename that several tracked files share holds nothing.** `` `main.yml` `` and
  `` `CLAUDE.md` `` name dozens of files, so `cited_paths` drops them, and so it drops a
  filename written outside a code span. An issue that means one of those files must spell
  its path. In the other direction, a file an issue names only in passing is held like one
  it edits, as a full path always was; `--allow-shared-file` excuses it.
- **A batch that ended with no PR holds nothing, and a resumed one still holds nothing.**
  Someone who resumes such a batch by hand edits files a later launch was free to take,
  until the resumed work opens a PR.

The dispatcher scores a host on memory headroom, and an agent is throttled by two cgroup caps
rather than one: `user.slice`, the fleet, and `user-1000.slice`, the login plane it runs in as
a transient user service. It reads both and takes the smaller headroom, so the tighter cap
decides and a host the fleet number alone would allow can still be refused. Each placement
spends one 2 GiB reservation (`RESERVATION_BYTES` in `scripts/dev/fanout_lib/placement.py`),
and the host with the most left takes the next batch.

Each batch unit also carries its own runaway limits on memory, swap and PIDs, set in
`systemd_run_command` in `scripts/dev/fanout_lib/launch.py`. The memory limit is a multiple of
the reservation, so a batch that placement priced as normal never reaches it. A batch that
does reach it is OOM-killed alone, and `status` prints its line as `failed … result=oom-kill`.
[ADR-0027](adr/0027-fanout-batches-stay-on-host-cgroups.md) records why the limits sit on host
cgroups rather than on k8s pods.

`clean` records each removal in the manifest, because the act destroys its own evidence: the
remote leg reads the worktree it deletes, so a later pass has nothing left to ask. A batch
already removed is skipped without an ssh call, `status` reports it as `cleaned` rather than
reading a host whose report file went with the worktree. Once every batch is removed, the
manifest moves into `~/.claude/fanout/retired/`, out of every listing, and `status` still
reads the run as cleaned (#4907).

`clean` runs each batch's leg on the batch's host in one ssh call
(`scripts/dev/fanout_lib/clean.py:remote_clean_command`). The leg fetches master, archives
`origin/master`'s `scripts/` into a temporary directory, runs `fanout.py place clean-one` from
that snapshot and removes the directory. So the code that judges a batch is master's on both
hosts, whatever commit the host's primary checkout stands at. Only daniel-box has
`homelab-checkout-sync`, and daniel-server's primary checkout lags master by hundreds of
commits. The snapshot costs about 0.1 seconds and 30 MB of temporary space per leg, and adds
no ssh call. The sweep that the server-batch lander runs (`clean.sweep_runs`) uses the same leg.

### A batch whose landing conflicts with master

A review batch's landing ends `VERDICT: merge-conflict` when master moved under its PR. That
landing merged and deployed nothing. The pipeline then resumes the implementer once, as the
`conflict` phase (`scripts/dev/fanout_lib/review/conflict_resume.py:land_resolving`). It
decides on the verdict token, never on the sentence after it.

- **The resume.** The prompt names the conflicting files, which the pipeline reads with
  `git merge-tree` after a fetch of the base. The implementer rebases the branch onto
  `origin/master`, resolves each conflict with both changes' intent kept, runs its tests and
  pushes with `--force-with-lease`. It does not land.
- **The review of the resolution.** The rebase moves the head past the commit the reviewer
  read, and a resolution is code nobody reviewed. A fresh reviewer therefore reads what the
  rebase changed in the PR's own commits (`git range-diff`) before anything lands. The
  record's reviewed head moves to the rebased head only once that review has read it. A
  finding of medium severity or worse at confidence 0.8 or more holds the PR, as one left
  after the fix round does, and so does a review that fails.
- **The re-land.** The pipeline runs the same landing again from a fresh checkout of the
  base. On daniel-server the rebased head first passes
  `scripts/dev/fanout_lib/review/review_ci.py:server_lands` again.

The resume is bounded. A batch gets one, and only with
`scripts/dev/fanout_lib/review/review_land.py:LAND_MARGIN_S` of the unit's run time left,
checked before the resume and again before the re-land. A second conflict, too little run
time, or a resume that pushed no new head ends the batch `needs input: the landing of <PR>
stopped short on a merge conflict`, and the line names the conflicting files.

A daniel-box batch and a daniel-server batch that lands its own draft behave the same. A
rebased head that `server_lands` holds is not landed: the batch ends `needs input: the
conflict resolution of <PR> passed its review at head <sha>`, with the hold and the
`land.sh` command that lands it.

### A conflict found after the batch has ended

A PR the pipeline did not land is landed after the batch's unit has stopped: by the server
lander, or by a sub-coordinator's own `land.sh` for a PR that lander holds. A
`merge-conflict` there has no unit left to resume. `fanout.py place resume <run-id> <batch>`
starts one:

```bash
uv run python scripts/dev/fanout.py place resume <run-id> <batch>
```

The unit runs the resume, the review of the resolution and the re-land described above, and
no other phase. `scripts/dev/fanout_lib/review/review.py:Pipeline.run_conflict` and the
pipeline's own resume both call `scripts/dev/fanout_lib/review/conflict_resume.py:resolve`,
so the two share one prompt and one review.

- **What it resumes.** A batch of this repo whose unit has stopped, whose worktree still
  stands, whose PR is open, and whose newest landing ended `merge-conflict`. It refuses
  anything else with the reason and the command that fits: a running batch, a cleaned
  batch, a missing worktree, a merged or closed PR, a PR whose landing lock is held
  (`scripts/deploy_tools/land_lib/pr_lock.py`), and a newest verdict that is not a conflict.
- **Where it reads the verdict.** `land.sh --detach` writes `land<pr>-<stamp>.log`, and
  the newest stamp is the PR's last landing. The command reads the batch's own log in its
  worktree's `.fanout/`, the directory this session's `land.sh --detach` writes to (the
  one `cc-wait land` reads), and the logs of the server lander. So run it from the session whose
  `land.sh` found the conflict. `--log <path>` names a log anywhere else. The verdict is the
  `LAND-RESULT:` line's `verdict` field
  (`scripts/dev/fanout_lib/resume.py:LandingLog`).
- **How the unit starts.** On the batch's own host, under the launch lock from the first
  manifest read to the manifest save, and through the placement gates a launch passes: the
  signing check, the memory headroom and the live-batch cap. The unit runs master's copy
  of the pipeline from a fresh snapshot in the worktree's `.fanout/server`
  (`scripts/dev/fanout_lib/launch.py:resume_command`), because the worktree's own copy is
  as old as the batch's branch and holds what the implementer left.
- **Who lands it.** The pipeline lands the PR when it would have at first: a daniel-box
  batch, or a daniel-server PR that passes `server_lands`. It does not land a PR that check
  holds, which includes every PR the own-surface rule holds. That batch ends
  `needs input:` with the reviewed head and the `land.sh` command, and its sub-coordinator
  lands it.
- **The bound.** `scripts/dev/fanout_lib/resume.py:MAX_RESUMES` resumes per batch, counted
  in the run manifest as `conflict_resumes`. Past it the command refuses and names the
  conflicting files, and the branch is a person's to resolve or the batch is abandoned.

`status`, `place runs` and `cc-wait fanout` read the unit's state, so a resumed batch reads
`running` and then the state of its new report.

### A parked batch

A review batch lands its own PR with `land.sh`. Another PR's failed promotion sets
`DEPLOY_HOLD`, and that hold stops this batch's landing in one of two ways. The batch then
ends `parked`: it is neither resumed nor reported as `needs-input`, because the hold is not
its to fix and no person has a question to answer.
`scripts/dev/fanout_lib/land_result.py:park_line` decides from the landing's `LAND-RESULT:` line
(`docs/landing.md`, *The `LAND-RESULT:` line*), never from a sentence in the log.

- **Refused before merge.** `land.sh` exits 75 with `verdict=refused` and merges nothing. The
  batch's final line is `parked: refused before merge (<why>); re-land with land.sh --pr N
  --arm-merge --await-merge --detach && cc-wait land N`. Any rule in *Rules that refuse a
  merge* parks the same way, not only the hold.
- **Merged, then held.** The landing ends `deploy-failed`, cause `promote-held`, with
  `hold_owner=other`. The final line is `parked: merged, unverified behind DEPLOY_HOLD <sha>;
  re-land after cc-wait landing-clear`.
- **Merged, then held, with no owner.** `hold_owner=unattributed` parks with the same line
  and `unattributed, so the root coordinator triages the hold` after it. The hold names no
  failed service, or the PR has none, so nobody can say from the hold whose it is.

`hold_owner=own` does not park. The hold names one of the PR's own services, so the pipeline
resumes the implementer with the landing's log, as it does for `unhealthy`.

**A parked batch never waits in its unit for the hold to clear.** A wait there spends the
unit's run time, keeps its 2 GiB reservation and counts as a live batch against the width.
The unit ends, and the sub-coordinator that owns the batch re-lands it: it runs `cc-wait
landing-clear`, then the `land.sh` command for the PR. For a PR that merged, the re-run
skips the merge and waits on the next promotion (`docs/landing.md`, *A PR that merged behind
a hold*). Clearing the hold stays the operator's decision.

`place status`, `place runs` and `cc-wait fanout` count a `parked` batch and list it with its
reason, as a batch that needs attention (exit tier 1). The unit's stop does not page for it
(`fanout_lib/notify.py:PAGED_STATES`).

A daniel-server batch that stops at its PR is landed by the server lander, whose logs are on
daniel-box in `~/.local/state/fanout-server-lander/logs`. `status` reads the newest
`land<pr>-*.log` there and reports the batch `landed`, `parked` by the same rule, or
`needs-input` naming the verdict. That directory belongs to the user the lander runs as, so a
session of another user reads none and reports `done`.

A PR that merged with no lander result reads `landed`: a coordinator landed it by hand.
`status` takes the merged state from the answer GitHub already gave for the branch's PRs
(`scripts/dev/fanout_lib/status.py:with_own_pr`), so it costs no call.

With no result yet, `status` reads the ruling the lander made on the PR from the lander unit's
journal, in the same call to daniel-box. Each lander pass prints one line per candidate PR,
and `scripts/dev/fanout_lib/status.py:lander_rulings` takes the newest pass's line.

- A ruling the lander does not clear itself reads `needs-input: the server lander will not
  land #N`, followed by the next step and the ruling.
  `scripts/dev/fanout_lib/operator_waits.py:reason_of` decides which rulings those are. Four
  wait on a person: the surface the lander itself runs from, an operator step in the PR body,
  findings left after the fix round, and a batch that ended `needs-input`. Three wait on the
  batch's coordinator, because the lander judges the same review record on every pass: a
  head that moved since the review, a CI gate that did not pass, and a review that did not
  complete. `place runs` lists the batch the same way, and `cc-wait fanout` names it as a
  batch that needs a hand.
- Any other ruling, such as a draft, stays `done (awaiting the server lander: <ruling>)`.
- A PR the newest pass did not name reads `done (awaiting the server lander)`. The lander
  has not passed since the PR opened, or the PR has no review record the lander reads.

**The awaiting state ends.** A batch reads `awaiting the server lander` for at most
`scripts/dev/fanout_lib/status.py:AWAIT_LANDER_S`, 30 minutes from the last write of its
report, unless the lander has started a landing of the PR. Past that, `place status` reads
`needs-input: the server lander has not taken #N in 30 minutes`, and the coordinator runs
`probe.py lander` and lands the PR. A landing the lander started ends in its own
`LAND-RESULT:` line, which `status` reads. `place runs` does not apply the bound, because it
never asks GitHub whether the PR merged. `place status` does not apply it either on a poll
where GitHub could not be asked for the batch's PR. That batch keeps its state and the PR
URL its final text names, the count line names it, and its `status --json` row carries
`pr_unconfirmed`.

`cc-wait fanout` stays `running` while a batch awaits the lander, and its detail counts
those batches. The row `status --json` prints carries `awaiting_lander`, which is the field
the probe reads (`scripts/dev/fanout_lib/wait_probe.py:read`). So the wait ends only once
every PR is `landed` or named as needing a hand.

DECIDED: `status` reads the ruling the lander recorded and does not evaluate
`autoland.problems` itself. That function needs the review record and the PR's file list
from GitHub, which is one more GitHub call on every `status` and every `cc-wait fanout`
poll. The journal read adds no ssh connection and no GitHub call. Its cost is age: a ruling
is as old as the newest pass, ten minutes at most, so a batch reads `awaiting` until the
first pass after its PR opens.

**`clean` keeps a parked batch whose PR has not merged.** A claim expires with its worktree,
and the open PR still holds the edits, so removing the tree would free the issues for a
second batch to work. `clean_one` prints `kept: … parked with its PR unmerged` and names the
re-land. A batch parked after its merge is cleaned like any merged batch. To give up on a
parked batch, run `abandon`.

`launch` refuses daniel-server as a host until the operator registers its SSH key as a GitHub
signing key once (`gh ssh-key add ~/.ssh/id_ed25519.pub --type signing`): the repo's ruleset
requires a verified commit signature, and a PR signed with an unregistered key sits `BLOCKED`
with every check green (PR #1572 needed a hand re-sign), so the dispatcher checks the key
before it spends an agent.

That check compares each candidate host's `user.signingkey` against the account's live list,
read with `gh api /users/<login>/ssh_signing_keys`. `launch` exits 6 when no candidate host
passes, and also when the list could not be read at all — a `gh` outage refuses the launch
rather than placing a batch whose PR might not merge. `read` prints the same verdict per host
as `signing=ok|unverified|unknown`, where `unknown` is that unreadable list, so it answers
what exit 6 would refuse without spending an agent.

**HISTORY — the GitOps tick and the launch fast-forward.** `launch` used to fast-forward a
host's primary checkout to `origin/master` before it cut the worktree. `.claude/settings.json`
then named every hook by an absolute path into that checkout, so a worktree cut from a fresher
`origin/master` registered hook scripts the checkout lacked, and about 2,100 Bash calls on
daniel-server ran with no `PreToolUse:Bash` guard in September 2026 (issue #2675). The
fast-forward skipped daniel-box, whose checkout the GitOps deployer already pulled every 10
minutes. #3394 registered each hook through `$CLAUDE_PROJECT_DIR`, so a session runs the copy
its own checkout carries, and deleted the fast-forward. H7.53 deleted the GitOps deployer;
`homelab-checkout-sync` fast-forwards daniel-box's checkout.

## The coordinator tree

A coordinator tree runs several projects at once. One root session sets the order across
projects, and one sub-coordinator session per project runs `issue-fanout` over that
project's milestone. `.claude/skills/coordinate/SKILL.md` holds what each role does. This
section is the record of the design: the shape, what the pilot measured, what the first
two-project run measured, what the operator decided, who lands which PR, and what the
design does not cover. It was written from the
coordinator tree plan of 2026-11-15, revision 8 of 2026-11-19.

### Target shape

```text
daniel-box, user ubuntu, one SSH connection
root coordinator                 primary checkout, edits nothing, talks to the operator
├── sub-coordinator: project A   own locked worktree, holds the project's context
│   └── fanout.py place launch   places each batch on the host with headroom
│       ├── batch on daniel-box     → PR → its own land.sh
│       └── batch on daniel-server  → PR → its own land.sh, or the server lander
└── sub-coordinator: project B
    └── fanout.py place launch
        └── batch on either host
```

Three rules give the tree that shape:

- **The workers are fan-out batches, not app sessions.** `fanout.py place launch` starts
  each batch as a headless systemd unit, so the app's rule on starting sessions does not
  apply to it. App worker sessions remain for work the operator steers by hand: a
  design-heavy issue, or a solo-only issue that cites the fan-out tooling.
- **Only the coordinators are pinned to daniel-box.** `launch` runs there and places each
  batch on whichever host has headroom. A daniel-server batch is recorded in daniel-box's
  manifests like any other, so the launch lock and the cross-run file gate cover it.
- **A project is the sub-coordinator's project milestone** (*Session milestones and project
  milestones*). The claims and the milestone both belong to the sub-coordinator's branch,
  so a restarted sub-coordinator reuses the same worktree and branch. While the project is
  open, its backlog is reserved for that branch (*An open project reserves its backlog*):
  an orchestrator outside the tree is not offered a project's issue and cannot claim one.

Four changes on master carry the tree. Launches are serialised and gated across runs
(*One launch at a time, and files held across runs*,
[#9242](https://github.com/DanielH2018/server/pull/9242) and
[#9310](https://github.com/DanielH2018/server/pull/9310)). A batch that another project's
hold stopped parks (*A parked batch*,
[#9252](https://github.com/DanielH2018/server/pull/9252)). A change to
`cluster/services.yml` lands alone (`docs/landing.md`, *Rules that refuse a merge*,
[#9167](https://github.com/DanielH2018/server/pull/9167) and
[#9214](https://github.com/DanielH2018/server/pull/9214)). A milestone holds a project's
backlog ([#9217](https://github.com/DanielH2018/server/pull/9217)).

### What the pilot established

The pilot ran on 2026-11-15. The root started a read-only sub-coordinator, and that
sub-coordinator started one worker. It answered three questions:

- **A sub-coordinator cannot start a session from the root's brief.** The app refuses to
  start a session on a turn another session started. The operator's message in the
  sub-coordinator's chat is the step that unlocks it, and the fan-out's grouping approval
  is that message.
- **Recaps travel up one level at a time.** The worker woke the sub-coordinator, and the
  sub-coordinator woke the root. The root receives one recap per project, cut to about 400
  characters, which is why the skill fixes the recap's format.
- **The root lists only the sessions on its own SSH connection.** So the whole tree runs
  from one connection to daniel-box.

### The first two-project run

The tree ran two projects from 2026-11-19 21:26 UTC to 2026-11-20 06:52 UTC. Each
sub-coordinator launched from daniel-box under its own branch, and both projects closed
every issue in their backlogs.

**Validate and CI cleanup** was milestone 22, under `claude/coord-ci-cleanup-2151e1`. Five
batches closed seven issues:

- [#9373] closed [#9251], [#9294] and [#9347], and ended `nothing-to-deploy`.
- [#9391] closed [#9342], and ended `nothing-to-deploy`.
- [#9402] closed [#9246], and ended `nothing-to-deploy`.
- [#9382] closed [#9119], and ended `settled`.
- [#9436] closed [#9145], and ended `nothing-to-deploy`.

The project filed two more issues, [#9314] and [#9224]. A batch of an orchestrator outside the
tree closed both in [#9430], which ended `settled`. That loss is why an open project
reserves its backlog (*Decisions, with their dates*).

**Alert noise** was milestone 21, under `claude/coord-alert-noise-d7b708`. Four batches
closed five issues, and every PR ended `settled`:

- [#9386] closed [#9201] and [#9338]. It changes `cluster/services.yml`, so it landed as a
  serial PR.
- [#9450] closed [#9054].
- [#9461] closed [#9045].
- [#9475] closed [#9118].

The root coordinator recorded that the operator's drill of the `AirflowSchedulerDown` alert
passed. No log read for this section confirms it.

**How the run was read.** Three sources hold what this section states, and each row of the
table names its own.

- The run manifests are in `~/.claude/fanout/retired/` on daniel-box. Each names its
  orchestrator branch, its batches and each batch's host.
- The verdict log is `~/.local/state/homelab/landing-verdicts.jsonl`, one per host.
  On the day of the run, `probe.py landing-verdicts <pr>` read only the log of the host it
  ran on. A batch that
  lands its own draft on daniel-server writes there, so daniel-box's log holds no row for
  [#9450], [#9461] or [#9475].
- The review records are in `~/.local/state/fanout-review/` on the batch's host. Each
  lists the phases the batch ran, under `durations`.

A row marked *recap* comes from the coordinators' recaps, as the root coordinator wrote
them into the plan on 2026-11-20. Nothing on either host was read to confirm it.

| Measure | Result | Read from |
|---|---|---|
| Launches that waited for the launch lock | 1 | recap |
| Launches that timed out on the launch lock | 0 | recap |
| Launches the cross-run file gate refused | 0 | recap |
| Collisions that reached the gate in a form it does not read | 3 | recap; the second is also read from the issue and the PR |
| Batches of the two projects that parked | 0 | review records |
| PRs of other orchestrators that parked behind a serial landing | 2, [#9424] and [#9423] | verdict log, daniel-server |
| Time from a park to a verified re-land | 76 minutes for [#9423], 115 minutes for [#9424] | verdict logs, both hosts |
| Time two serial landings in a row kept other k8s landings out | between 55 and 84 minutes | verdict logs, both hosts |
| Landings of [#9386] before `settled` | 3: `merge-conflict`, `refused`, `pr-ci-red` | verdict logs, both hosts |
| Batches placed on daniel-server | 8 of 9 | manifests |
| Batches of the two projects that ran the conflict resume | 0 | review records |
| Operator steps per project | one approval, plus the drill for Alert noise | recap |

**The launch lock and the serial turnstile held when contended.** One launch waited for
the lock and none timed out. [#9386] landed alone, and [#9418] from another session landed
alone after it.

**The cross-run file gate refused nothing, and three real collisions reached it.** Each
arrived in a form the gate does not read:

- Issue [#9224] named its file by a bare filename. [#9370] made a filename alone in a code
  span count when one tracked file has that name.
- A fix edits files its issue does not cite. Issue [#9054] cites three paths that resolve to
  tracked files. Its fix, [#9450], changed 11 files, and one of the three is among them.
- Master changed a test file under a running batch, and [#9386]'s CI went red on 2026-11-19
  at 23:37 UTC. A landing on master is not a live batch, so the gate compares nothing for
  it.

A clean launch in that run therefore meant that no live batch cited a file the launch
cited. It did not mean that no live batch edited one. The collision the run planned, between [#9118] and
[#9119] in different projects, never reached the gate: [#9382] merged [#9119]'s fix on
2026-11-19 at 23:15 UTC, and [#9118] launched on 2026-11-20 at 04:41 UTC.
[#9479](https://github.com/DanielH2018/server/issues/9479) proposed comparing the files a
live batch has edited, and the gate reads them from each live batch's open PR (*One launch
at a time, and files held across runs*).

**The serial rule's cost was two landings in a row.** [#9386] and [#9418] both change
`cluster/services.yml`. `docs/landing.md`, *Rules that refuse a merge*, holds the timings.

**The conflict resume ran once, outside the two projects.**
[#9421](https://github.com/DanielH2018/server/pull/9421) merged on 2026-11-19 at 23:50
UTC. The batch for [#9377], launched by `worktree-simplification-audit-wave3` on 2026-11-20
at 02:02 UTC, ran the `conflict` and `review-conflict` phases, and its PR [#9454] ended
`settled`. No batch of the two projects ran either phase. [#9436] ended `merge-conflict`
twice after #9421 merged, and both landings ran on daniel-box, outside the batch's unit,
where the resume does not apply.

**Three paths were not exercised:**

- A stop-and-resume of a sub-coordinator. Neither sub-coordinator restarted.
- A gate refusal between two projects.
- A park caused by `DEPLOY_HOLD`. No promotion failed, so both parks were refusals behind
  a serial landing.

**Five planned measures were not read.** The plan asked how long a landing waits for the
landing turn, how often a serial PR reaches `slots.DRAIN_S`, how long a unit's land phase
runs, each session's GitHub quota, and whether the root's view goes stale. The run
recorded none of them.

The run changed the tooling six times, and each change edited the section it touched:
*One landing of a PR at a time* in `docs/landing.md`, the bare-filename rule and
*A batch whose landing conflicts with master* on this page, the lander rulings under
*A parked batch*, *An open project reserves its backlog*, and the last two entries of
*Decisions, with their dates*. It also filed six findings:
[#9367](https://github.com/DanielH2018/server/issues/9367),
[#9384](https://github.com/DanielH2018/server/issues/9384),
[#9400](https://github.com/DanielH2018/server/issues/9400),
[#9401](https://github.com/DanielH2018/server/issues/9401),
[#9431](https://github.com/DanielH2018/server/issues/9431) and
[#9434](https://github.com/DanielH2018/server/issues/9434).

[#9045]: https://github.com/DanielH2018/server/issues/9045
[#9054]: https://github.com/DanielH2018/server/issues/9054
[#9118]: https://github.com/DanielH2018/server/issues/9118
[#9119]: https://github.com/DanielH2018/server/issues/9119
[#9145]: https://github.com/DanielH2018/server/issues/9145
[#9201]: https://github.com/DanielH2018/server/issues/9201
[#9224]: https://github.com/DanielH2018/server/issues/9224
[#9246]: https://github.com/DanielH2018/server/issues/9246
[#9251]: https://github.com/DanielH2018/server/issues/9251
[#9294]: https://github.com/DanielH2018/server/issues/9294
[#9314]: https://github.com/DanielH2018/server/issues/9314
[#9338]: https://github.com/DanielH2018/server/issues/9338
[#9342]: https://github.com/DanielH2018/server/issues/9342
[#9347]: https://github.com/DanielH2018/server/issues/9347
[#9370]: https://github.com/DanielH2018/server/pull/9370
[#9373]: https://github.com/DanielH2018/server/pull/9373
[#9377]: https://github.com/DanielH2018/server/issues/9377
[#9382]: https://github.com/DanielH2018/server/pull/9382
[#9386]: https://github.com/DanielH2018/server/pull/9386
[#9391]: https://github.com/DanielH2018/server/pull/9391
[#9402]: https://github.com/DanielH2018/server/pull/9402
[#9418]: https://github.com/DanielH2018/server/pull/9418
[#9423]: https://github.com/DanielH2018/server/pull/9423
[#9424]: https://github.com/DanielH2018/server/pull/9424
[#9430]: https://github.com/DanielH2018/server/pull/9430
[#9436]: https://github.com/DanielH2018/server/pull/9436
[#9450]: https://github.com/DanielH2018/server/pull/9450
[#9454]: https://github.com/DanielH2018/server/pull/9454
[#9461]: https://github.com/DanielH2018/server/pull/9461
[#9475]: https://github.com/DanielH2018/server/pull/9475

### Decisions, with their dates

- **2026-11-15: a project is a milestone.** On 2026-11-19 the operator narrowed that to the
  sub-coordinator's session milestone in its project form, for the reason *Session
  milestones and project milestones* gives.
- **2026-11-15: two sub-coordinators for the first run, each capped at 2 running batches.**
  Nothing in the design limits the count of sub-coordinators. Memory is not the bound
  either, because each host takes `scripts/dev/fanout_lib/launch_gates.py:live_batch_cap`
  running batches. The bound is landing: one landing turn runs one PR's CI before its
  merge at a time. The cap rises once a real run shows the landing turn keeps up. The
  skill holds the cap, and no tool enforces it.
- **2026-11-15: a sub-coordinator lands without asking.** It lands and re-lands only the
  PRs of its own runs, after `probe.py lander`, inside root `CLAUDE.md`'s *When to wait*.
  On 2026-11-19 the operator dropped a second limit, under which a PR that changes code
  `land.py` imports waited for approval.
- **2026-11-15: one approval covers one milestone plus the grouping rules.** The operator
  types it once, in the sub-coordinator's chat. Refills stay inside that milestone, and a
  new milestone needs a new approval.
- **2026-11-15: build the serial turnstile, and defer a root status command.** The
  turnstile landed as a wait in the landing turn. The root reads recaps until a real run
  shows that it needs more.
- **2026-11-19: an open project reserves its backlog.** The first two-project run lost
  #9314 to an orchestrator outside the tree, 42 minutes after its project filed it. The
  way out is `milestone remove`, not a `claim` flag.
- **2026-11-19: a batch holds its files while its unit is active or its PR is open.** An
  earlier draft held them until `clean`, which the 21 standing batches of that day would
  have turned into refusals on files nobody was editing.
- **2026-11-20: a landing from a stale worktree re-runs itself from a fresh checkout.** A
  coordinator's locked worktree never follows master. In the first two-project run, on
  2026-11-19 and 2026-11-20, every `land.sh` a sub-coordinator ran from its worktree ended
  `refused` for landing code older than master's,
  [#9373](https://github.com/DanielH2018/server/pull/9373) and
  [#9386](https://github.com/DanielH2018/server/pull/9386) among them, and was run again
  with `--fresh`. `land.sh` makes that re-run itself, so a coordinator passes no extra flag
  (`docs/landing.md`, *Before the merge, a stale checkout re-runs the landing from a fresh
  one without asking*).
- **2026-11-20: the serial pause runs from the start of the serial PR's landing to its
  verdict.** The rule had the root pause the other projects when a recap named a serial
  PR. #9386 was named at triage, hours before it could land. The root paused the other
  project while #9386 was a draft in review, lost about half an hour of refills, lifted
  the pause, and applied it again once the landing was live. `land.sh` already keeps every
  other merge out while a serial PR drains (`scripts/deploy_tools/land_lib/slots.py`). The
  pause only stops the other project launching batches whose landings would be refused
  meanwhile, so a sub-coordinator reports a serial PR under `asks:` when its landing
  starts, and its verdict in the same place.

### Who lands a daniel-server batch's PR

A review batch lands its own draft on either host. On daniel-server it does so when the PR
passes the PR rule the server lander applies
(`scripts/dev/fanout_lib/review/review_ci.py:server_lands`), so the park outcomes reach the
sub-coordinator through one path for both hosts. The server lander
(`scripts/dev/fanout_lib/autoland.py`) keeps two cases: a `--no-review` batch, which stops
at its PR, and a review batch whose PR the rule holds.

| Server batch outcome | Who acts |
|---|---|
| A review batch whose PR passes the rule | The batch, through its own `land.sh`. A park reaches the sub-coordinator as it does for a daniel-box batch. |
| A review batch whose PR the rule holds | The batch marks the PR ready and stops, and its review comment says `Not landed by the pipeline:` with the reason. After the next lander pass, `place status` reads `needs-input: the server lander will not land #N` with the next step: the sub-coordinator lands the PR, or passes an operator step to the operator. |
| A PR the lander rules ineligible for a reason it clears itself (a draft) | Nobody yet. `place status` reads `done (awaiting the server lander: <ruling>)`, for 30 minutes at most. |
| A PR whose head moved since the review | The sub-coordinator. It reads the commits pushed since the review, then lands the PR with `land.sh`. |
| A PR whose CI gate did not pass | The sub-coordinator. It reads the PR's checks, then lands the PR with `land.sh` once they pass. |
| A PR whose review did not complete | The sub-coordinator. It reviews the diff, then lands the PR with `land.sh`. |
| A PR that awaited the lander for 30 minutes with no landing started | The sub-coordinator. `place status` reads `needs-input: the server lander has not taken #N in 30 minutes`; it runs `probe.py lander`, then lands the PR with `land.sh`. |
| A PR its coordinator landed by hand | Nobody. `place status` reads `landed`. |
| A `--no-review` batch that qualifies for the server lander | The server lander. The sub-coordinator never lands it. |
| A server lander landing refused before merge | The server lander, which starts the PR again once the rule clears (`scripts/dev/fanout_lib/autoland.py:refused_for_now`). |
| A server lander landing that merged, then ended `deploy-failed` on another project's hold | The sub-coordinator, after `cc-wait landing-clear`. The server lander leaves every other ended landing for a person. |

`place status` says whose the PR is: a `needs-input` line that names the server lander is
the sub-coordinator's, and `awaiting the server lander` is not. Before a sub-coordinator
runs `land.sh` on any server batch's PR, it still runs `probe.py lander`, which shows
whether a landing of the PR started since that ruling and how it ended. The probe is not a
guard against a second
landing: `land.sh` refuses one while a landing of the PR is live (`docs/landing.md`, *One
landing of a PR at a time*).

A re-land of a PR that merged behind a hold starts no promotion, and clearing the hold
starts none either (`docs/landing.md`, *A PR that merged behind a hold*). The skill states
that beside its re-land step, because a sub-coordinator that re-lands with nothing merged
since ends `deferred` after the whole promotion wait.

### A coordinator locks its own worktree

A coordinator edits nothing, so its worktree is clean and merged into `origin/master`.
`scripts/lib/worktrees.py:classify` calls such a tree removable once it holds no live
lock, and every claim under its branch then reads stale (*Staleness*). The project
milestone belongs to the same branch. So a sub-coordinator runs `git worktree lock` with a
reason that names its project.

`scripts/lib/worktrees.py:lock_is_live` reads a lock reason in three ways. A reason in
Claude Code's `(pid N start M)` form is live while that process runs. A reason that
`scripts/lib/fanout_lock.py:REASON` matches is live while its batch unit runs. Any other
reason counts as live until someone unlocks the tree. The coordinator's reason is the
third kind, so the lock outlives the session, and a restarted sub-coordinator finds its
claims still held. The way out is `git worktree unlock`, which the skill's last step runs.

The desktop app creates a session's worktree unlocked, and `EnterWorktree` creates one
locked with Claude Code's reason. The skill therefore locks first, and unlocks only when
git reports the tree as already locked.

### Known limits of the coordinator tree

- **The cross-run file gate has refused no real launch, and no landing has parked on a
  real `DEPLOY_HOLD`.** Tests cover both. In the first two-project run one launch waited
  for the launch lock, and two PRs parked behind a serial landing and re-landed (*The
  first two-project run*).
- **The restart drill covered one case: a cooperative stop while a batch was running.** It
  passed on 2026-11-20 for project `Coordinator tooling leftovers` (milestone 23, run
  `20261120T161457Z`). The root messaged the sub-coordinator to stand down, and a
  replacement in the same worktree recovered the project. The drill found five steps the
  skill's restart path left out
  ([#9521](https://github.com/DanielH2018/server/issues/9521)). A crash, a parked batch and
  a landing in flight were not covered.
- **A project lives as long as its sub-coordinator's branch.** `reap` closes only a
  milestone with nothing open, so a dead sub-coordinator's backlog stays filed, and
  reserved, until the sub-coordinator restarts or the root ends the project with
  `milestone close --worktree <branch>`.
- **A claim that predates the reservation stands.** An issue another branch claimed before
  the project filed it, or before this rule landed, stays with that branch until it
  releases.
- **A project's backlog cannot include an issue another open milestone holds**, until the
  session that holds it releases the issue.
- **The cap of 2 running batches and the refill pause under `DEPLOY_HOLD` are skill
  rules.** `launch` checks neither.
- **Only daniel-box's primary checkout follows master.** daniel-server's is never synced,
  and its code reads only that host's own manifest directory, so a coordinator started
  there would share no lock and no gate with the tree.
- **The cross-run file gate has the limits its own section lists** (*One launch at a time,
  and files held across runs*), and an issue that adds a secret without citing
  `secrets/secrets.yml` passes the SOPS gate.
- **The root's view can go stale.** The app holds recaps after many wake-ups in a row with
  no operator message. The first two-project run did not record whether that happened.
- **The cap of 2 running batches has no measurement behind a change.** The first
  two-project run did not record how long a landing waited for the landing turn, which is
  the reading the cap's decision waits on.

## The `/issue-fanout` skill

`.claude/skills/issue-fanout/SKILL.md` owns the five steps (triage, claim, spawn, land, report),
the brief every agent receives, and the width bound. This page owns the claim protocol the
steps rely on.

Three facts from that protocol shape the skill:

- **Every issue of a batch is claimed before that batch's agent starts.** `launch` takes the
  claims itself, under the orchestrator's worktree name, because a subagent's worktree name is
  unknown until it starts and a claim naming a worktree that does not exist yet reads as stale.
- **A landing takes no tree lock.** No local script applies a k8s workload: `promote` and Flux
  do, and `land.sh` only waits on them
  ([ADR-0017](adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md)). So two agents landing
  disjoint services proceed together. A `land.sh` exit 75 is a resume point to retry, not a
  failure to report.
- **Width follows memory headroom, not a count.** `scripts/dev/fanout.py place` reads the
  `user.slice` and `user-1000.slice` caps and places each batch on the host with the most left.
  `MemoryHigh` throttles rather than kills, so an over-wide fan-out stalls the host, as it did
  on 2026-09-05 (issue #1264).
