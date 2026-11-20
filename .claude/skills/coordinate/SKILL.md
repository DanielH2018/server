---
name: coordinate
description: "Use when a brief names a project's milestone title, calls you the root coordinator or a sub-coordinator, or asks you to run several projects at once; also when a coordinator session restarts mid-project, a fan-out batch reads `parked`, or a recap for the session above you is due. One root session sets project order, and one sub-coordinator per project runs issue-fanout over that project's milestone."
allowed-tools: Bash, Read, Grep, Glob
---

# Coordinating projects

A coordinator tree has three levels. The root talks to the operator and edits nothing. Each
sub-coordinator owns one project, which is its project milestone, and runs `issue-fanout`
over that milestone's backlog. The workers are fan-out batches: headless units that
`fanout.py place launch` starts, never app sessions.

You are a **sub-coordinator** when your brief names a milestone title. You are the **root**
when the operator asks you to run projects. Read your role's section, then *Recap*.

`docs/issue-claiming-and-fanout.md`, *The coordinator tree*, records why each rule here
exists. The `issue-fanout` skill owns triage, launch, status and clean. The
`land-after-merge` skill owns the `land.sh` command. `findings.py <cmd> --help` owns every
flag. This skill adds only what a coordinator does differently.

Every coordinator runs on daniel-box as `ubuntu`, because the launch lock and the run
manifests cover one user on one host.

## Root

The root runs in the primary checkout and stays thin. It does seven things:

- **Start a sub-coordinator per project.** The brief is this skill's name and the project's
  milestone title, with the issue numbers when the operator named them. The app starts a
  session only on a turn the operator typed, so start sub-coordinators on that turn.
  `uv run python scripts/dev/findings.py next --milestone none` lists the issues no open
  milestone holds, which is the pool a project is picked from.
- **Carry the operator's launch approval in the brief, and never originate one.** When the
  operator told you to start the project, quote the words that named it, exactly as typed,
  and state the grouping rules (*Approval*). For a project you chose yourself, or grouping
  rules wider than the standing ones, quote nothing: tell the operator to approve in that
  sub-coordinator's chat.
- **Read recaps.** Each sub-coordinator's recap arrives when it ends a turn, one per
  project. Relay every line under `asks:` to the operator.
- **Set project order.** When two projects want the same file or the same landing, say
  which goes first, and message both sub-coordinators.
- **Sequence a serial PR across projects.** `land.sh` lands a serial PR alone and refuses
  every other merge meanwhile (`docs/landing.md`, *Rules that refuse a merge*). When a
  recap's `asks:` says a serial PR's landing has started, tell the other sub-coordinators
  to pause refills until it reports the verdict. A planned or in-review one pauses nothing.
- **Triage a park flagged `unattributed`.** The hold named no failed service, so no
  sub-coordinator can say whose it is. Read `uv run python scripts/diagnostics/probe.py
  landing` and the hold's `deploy-failed` line (`docs/landing.md`, *Reading a
  `deploy-failed`*). Tell the operator which PR the evidence points at. Clearing
  `DEPLOY_HOLD` is the operator's decision.
- **Archive a session you started once you have verified its result**, without asking, in
  the turn that verifies it. A result is verified when you read its landing verdict with
  `uv run python scripts/diagnostics/probe.py landing-verdicts <pr>`, saw its issue closed,
  or ended its project (*Ending a project*). A recap that says the work is done verifies
  nothing. Three cases stay open:
    - **Another live session uses its worktree.** Archiving cleans the worktree, so a
      stood-down sub-coordinator stays until its replacement's project ends.
    - **A landing or a background wait still runs in it.** Archive it after that ends.
    - **The app refuses the archive.** Report the session to the operator once, and do not
      retry.

To add an issue to a running project, message its sub-coordinator with the number. A
sub-coordinator's milestone wait also ends once `milestone add` files the issue.

**A sub-coordinator that died is restarted, not cleaned up.** To see which project has no
live session, run `uv run python .claude/hooks/session-health.py --sessions`. It prints one
line per such project with its branch, its worktree and the batches that wait on it
(`scripts/dev/fanout_lib/coordinators.py:projects`). The server lander pages the operator
for a project that also holds a waiting batch, so the page is the usual way a death is
noticed. Start a session in the same
worktree with the same milestone title, and quote in its brief the same operator words the
first brief quoted, so the replacement inherits the approval. *Resume* rebuilds its state.
The app starts a
session only on a turn the operator typed, and takes a named folder only on a start the
operator asked for. A recap or a peer message is no such turn, so ask the operator for one
that names the restart, and start the replacement on it.

**A sub-coordinator that is alive is stood down by message before its replacement
starts.** Message it to stop its background wait and do nothing further. Stopping the
session only interrupts its turn, so the next recap wakes it again. It is the first of
the archive duty's open cases: it stays until its replacement's project ends.

Only when the operator
ends the project does the root take over its runs: list them with `fanout.py place runs
--orchestrator <its branch>`, then follow *Ending a project* from the primary checkout,
naming that branch and that worktree's path. Never clean a run whose sub-coordinator is
alive.

Done when: every project has one live sub-coordinator, every quote in a brief is text the
operator typed to you, every `asks:` line has reached the operator, and every session
whose result you verified is archived or named with the case that keeps it open.

## Sub-coordinator

Run these in order on the first turn. `<branch>` is your worktree's branch, which
`git branch --show-current` prints.

1. **Lock the worktree.** A coordinator edits nothing, so its tree is clean and merged, and
   the weekly sweep would remove it and turn every claim under it stale.

    ```bash
    git worktree lock --reason "coordinator: project <title>" .
    ```

    When git answers `already locked`, run `git worktree unlock .` and lock again, so the
    reason is yours. Keep the reason's form: a reason Claude Code or a fan-out batch wrote
    stops counting once its process ends.

2. **Open the project.**

    ```bash
    uv run python scripts/dev/findings.py milestone start --project "<title>" --worktree <branch>
    ```

    A title the branch already holds is reopened. A group of that title, which `milestone
    group` prepared with no owner, is adopted with its backlog. So is another branch's
    project when nothing holds that branch. The command refuses a project whose owner is
    live and names the owner: report that to the session above you
    (`scripts/dev/tests/test_findings_milestone.py::test_start_refuses_a_title_another_branch_owns`).
    To hand a project on without closing it, run `milestone disown --worktree <branch>`.
    When `milestone status` shows the project exists with work in it, go to *Resume*.

3. **File the backlog.** Add the issues your brief or the root names with `milestone add
   <n...> --worktree <branch>`. When the brief names none, list `next --milestone none
   --json`, pick the issues the title covers, and put that list in the approval ask in
   place of adding it. A list you picked always goes to the gate, whatever the brief
   quotes.

4. **Triage the project.** Follow `issue-fanout` step 1 with `next --milestone "<title>"
   --json` in place of plain `next --json`. When your brief carries the launch approval
   (*Approval*), do not stop at its approval gate: go on to step 5. Otherwise stop at the
   gate with the grouping, each batch's rung, and the grouping rules you will refill
   under.

5. **Launch, watch and refill**, once the brief carries the approval or the operator has
   approved at the gate. `launch` reviews every batch unless told otherwise, so pass no
   review flag. Keep to these limits:

    - **At most 2 of your batches run at once.** Count the `running` batches in `place
      status` over your runs. The bound is the landing turn, not memory. A `parked` batch
      has ended and does not count.
    - **Refill only inside the approved milestone and grouping rules.** Refill from `next
      --milestone "<title>"`, after `cc-wait fanout-headroom` ends `room`. An issue outside
      the milestone, or a grouping the rules do not cover, goes under `asks:`.
    - **Pause refills while `DEPLOY_HOLD` is set or the root asks.** `probe.py landing`
      prints the hold. Batches that are running finish; launch nothing until `cc-wait
      landing-clear` ends 0 or the root says to go on.
    - **Report a serial PR when its landing starts, not at triage.** Put `root: serial #N
      landing` under `asks:` on that turn, and `root: serial #N <verdict>` when it ends.
    - **Exit 8 from `launch` is another coordinator's launch.** Run the same command again.
      A refusal that names another run's file is that coordinator's batch: hold the issue,
      and never clean its run.

6. **Wait for more work** when the backlog is empty, with `cc-wait findings --milestone
   "<title>"`. The wait records the project's free issues when it starts and ends on any
   other free issue the project gains, so it takes no `--after`.

Done when: the worktree's lock reason names the project, `milestone status` prints the
project form, and every launch ran under an approval the operator gave: words your brief
quotes, or an answer at the gate.

## Landing

Land and re-land **only the PRs of your own runs**, without asking, inside `CLAUDE.md`'s
*When to wait*. A review batch lands its own draft on either host, so most PRs need nothing
from you. `place status` names the ones that do.

- **Never land a PR the server lander qualifies** (`docs/issue-claiming-and-fanout.md`,
  *Who lands a daniel-server batch's PR*).
- **A PR the lander's rule held** reads `needs-input: the server lander will not land #N`
  in `place status`, with the next step and the lander's ruling. An operator step goes
  under `asks:`. Every other such PR is yours to land, by the step the line names: one held
  for the lander's own surface, one whose head moved since the review, one whose CI gate did
  not pass, and one whose review did not complete. `done (awaiting the server lander:
  <ruling>)` is the lander's to clear. After 30 minutes with no landing started, it reads
  `needs-input: the server lander has not taken #N`, and the PR is yours.
- **Re-land each `parked` PR.** Run `cc-wait landing-clear`, then the `land.sh` command the
  park line names. A park line that opens `another landing has it` is the exception: run
  the `cc-wait land` command it quotes and read that landing's verdict. A park line ending
  `unattributed` also goes under `asks:` for the root.
- **A PR that conflicts with master is `place resume`'s to resolve. Spawn no agent for
  it.** That covers a landing that ends `merge-conflict` and a batch that ends
  `needs-input: PR #N conflicts with master`, whose PR conflicted before CI ran and has no
  landing. Run `uv run python scripts/dev/fanout.py place resume <run-id> <batch>` from
  this session, then `cc-wait fanout <run-id>`. It restarts the batch's implementer to
  rebase, has a fresh reviewer read the resolution, and lands the PR. A PR the
  lander's rule holds ends `needs-input: the conflict resolution of <PR> passed its
  review`: land it with the `land.sh` command that line names. A refusal
  names its reason and the command that fits. Past
  `scripts/dev/fanout_lib/resume.py:MAX_RESUMES` resumes of one batch it names the
  conflicting files: put the PR under `asks:` (`docs/issue-claiming-and-fanout.md`, *A
  conflict found after the batch has ended*).
- **A re-land of a merged PR starts no promotion, and neither does clearing the hold.** The
  re-land settles only if master took a later merge. Otherwise it ends `deferred`, cause
  `promote-timeout`, unless the operator dispatched `promote` for the tip. When nothing
  has merged since the hold, ask for that dispatch under `asks:` before re-landing
  (`docs/landing.md`, *A PR that merged behind a hold*).

Done when: every PR of your runs is `landed`, waiting on the server lander, or named in the
recap with its reason.

## Placement

Let `launch` pick each batch's host. Pass `--host` only to exercise a path on purpose, or
to narrow a launch that was refused for headroom.

## Approval

The operator approves **one milestone plus its grouping rules, once**, by telling the root
to start the project and naming its milestone. That is the launch approval, and the root's
brief carries it to the sub-coordinator.

**The root never originates an approval.** The operator's quoted words are the approval,
and they are what a reader checks: a brief that only says the project is approved proves
nothing, because the root could have written that sentence on its own.

Your brief carries the launch approval when it has both of these:

- the operator's words that named the project, quoted;
- the grouping rules, stated in full or cited as "the standing grouping rules".

With both, do not stop at the approval gate, on the first launch or on any refill under
those rules.

The standing grouping rules are these four:

- one batch per issue;
- two issues that cite the same file share a batch or run in sequence;
- every batch gets the review phase;
- refill as headroom frees, without asking again.

Stop at the gate, and ask the operator in your own chat, when any of these holds:

- the brief quotes no operator words;
- the root chose the project itself;
- the brief's grouping rules are wider than the standing ones;
- you picked the backlog yourself (*Sub-coordinator*, step 3).

State the grouping rules in the ask, so the answer names what refills may do. Every later
launch inside that milestone and those rules needs no further ask.

No brief covers these three, whatever it quotes. Each needs the operator's answer in your
own chat: dropping the review phase, passing `--allow-shared-file`, and launching an issue
from outside the project's milestone. A new milestone is a new project and a new approval.

## Recap

End every turn with this block, first in whatever you write last. The session above you
receives only the first 400 characters. Keep the block under 300.

```text
state: running | blocked | parked N | needs you
PRs: <merged> merged, <open> open
last: #<n> <verdict>, #<n> <verdict>, #<n> <verdict>
asks: <one line per ask, or none>
```

- `state` is one of the four. `needs you` wins over `parked N`, which wins over `blocked`.
  `blocked` means refills are paused, by a hold or by the root.
- `last` holds the three newest PRs of your runs with each landing's verdict, or `open`.
- An ask names who it is for and the one thing needed: `operator: dispatch promote for
  <sha>`, or `root: serial #<n> landing`.

Done when: the block is the first thing in the turn's last message and is under 300
characters.

## Resume

A restarted sub-coordinator reuses the same worktree and branch, because the claims and the
milestone belong to that branch. `<branch>` is that branch, which `git branch
--show-current` prints. Rebuild the state from these reads before doing anything:

```bash
git worktree list --porcelain
uv run python scripts/dev/findings.py milestone status --worktree <branch>
uv run python scripts/dev/fanout.py place runs --orchestrator <branch>
uv run python scripts/dev/fanout.py place status <run-id>
uv run python scripts/dev/findings.py claims --worktree <branch>
```

The first shows whether the lock still carries the project's reason; lock again when it
does not. `milestone status` prints `issues N open, M closed; PRs N open, M closed`: compare
the issue pair with `claims` and `next --milestone`, never the PR pair
(`scripts/dev/tests/test_findings_milestone_cli.py::test_status_counts_the_project_issues_apart_from_its_pull_requests`). `place runs` gives the run ids, and no batch's state: a batch that is running,
done or landed reads `standing` there alike (`scripts/dev/fanout_lib/status_view.py:runs_lines`).
Run `place status <run-id>` once for each run id it lists. That read tells a `running`
batch from a `parked` one.

- **A run with a `running` batch is watched again.** The wait the dead session held died
  with it. Start `cc-wait fanout <run-id> [<run-id> …]` over those runs, the way
  `issue-fanout` step 3 describes.
- **A `parked` batch is re-landed**, as *Landing* describes.

Then read `next --milestone "<title>"` for what is left. Finish landing and re-landing what
earlier runs launched without asking. A restarted sub-coordinator inherits the launch
approval when its restart brief quotes the same operator words and states the grouping
rules (*Approval*). Before a new launch under any other brief, stop at the gate and ask
the operator to approve again.

Done when: every standing batch of the branch's runs has a state `place status` printed,
a wait covers every run with a `running` batch, and the recap states both.

## Ending a project

Each step undoes one from *Sub-coordinator*, in reverse.

1. Give back every batch no agent finished with `fanout.py place abandon <run-id>
   <batch>`, as `issue-fanout` step 5 describes.
2. Release what the branch still holds: `findings.py release --all --worktree <branch>
   --reason "project ended"`.
3. Run `fanout.py place clean <run-id>` for each run. A parked batch whose PR is open is
   kept: re-land it or abandon it first.
4. When `milestone status` still shows the project open, run `milestone close --worktree
   <branch>`. Its unstarted issues return to the pool `next --milestone none` lists.
5. Run `git worktree unlock .`, then retire the tree with the `worktree-cleanup` skill.

Done when: `place runs --orchestrator <branch>` lists no standing batch, `claims --worktree
<branch>` lists nothing, and the final recap says the project ended.
