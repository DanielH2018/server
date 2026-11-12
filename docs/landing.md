# Landing a merged PR

`land.sh` follows a merged PR through to a verified deploy in one invocation: arm the merge,
wait for master CI on the merge commit, wait on the promotion, gate the health, and
print a `VERDICT:` line. The `land-after-merge` skill carries the invocation and the verdict
decoder. This page carries the reasoning behind each rule, and the incident that put it there.

## The command line

`land.sh --pr <n> --arm-merge --await-merge --detach && cc-wait land <n>` is the whole landing.
`land.sh --help` prints the flags, the exit codes and the verdicts.

**`--detach` and `cc-wait land` replace three hand-written steps.** `land_lib/detach.py`
resolves `--since` and redirects the log, and its docstring explains why the child's exit code
is the authority rather than a grep for `VERDICT:`. `cc-wait`'s `land` source
(`scripts/deploy_tools/land_lib/land_probe.py`) does the waiting.

**`cc-wait` waits at most 570s, which fits a foreground Bash call.** It then exits 75 with the
command that resumes the wait. Re-run that command, never `land.sh`, which would start a second
landing. The predecessor, `--await-verdict`, waited up to 1200s, past the 600s foreground limit,
so the harness moved it to the background, where fan-out `claude -p` agents never woke.

**`--since` is the PRE-merge tip.** It bounds the range the truncated-list fallback derives tags
from, so `--detach` resolves it before the arm. Reading it afterwards would capture the tip that
includes this PR and derive nothing.

**A landing refuses when its own checkout is older than master's landing code.** `land.sh`
runs `land.py` from the checkout it lives in, so a worktree branched before a master change to
a reader runs that older reader against the merge commit. On 2026-11-09 such a reader read the
#7767 merge commit as declaring no k8s tags (#7820). The comparison covers every module the
landing has loaded from its checkout, and every module those import, function bodies included.
With `--arm-merge --await-merge`, the landing first compares them with `origin/master` before
it merges anything (#7830), and ends `refused` with nothing merged. After resolving the merge
commit, step 1 compares them with the merge commit's first parent. It ends `refused` when master
changed one the checkout lacks.
`land_lib/classify.py:refuse_stale_code` has the rule.

**Both refusals name `--fresh` as the re-run** (#7933). With `--fresh`, `land.sh` fetches
`origin/master` in the primary and adds a detached worktree of it under `/tmp`. It runs that
checkout's `land.sh` with the same arguments, then removes the worktree. A checkout made by
hand went stale again within minutes when master moved, and the `git -C <primary> worktree
add` that made it is a command a worktree-isolated session's guard refuses. The landing
still refuses rather than re-running on its own, so `land.sh` keeps running the release it
ships with unless the caller asks. With `--detach`, the fork runs the fresh checkout, so the
checkout lives until the landing ends. `land_lib/fresh.py` has the mechanics. A lander
unit's caller cannot pass `--fresh`, because the unit runs a fixed command line. Under a unit,
both refusals therefore name the re-run that caller can make: the same command, once
`homelab-checkout-sync` has fast-forwarded the primary, which it does every 10 minutes.

**Without `--detach`, redirect stdout and stderr to a file yourself.** A backgrounded Bash call
hands the script a non-blocking pipe, and Ansible refuses to start on one:

```
ERROR: Ansible requires blocking IO on stdin/stdout/stderr. Non-blocking file handles detected: <stdout>, <stderr>
```

`land.sh` then prints `VERDICT: deploy-failed` with nothing deployed. The error names Ansible
rather than the harness, so it reads as a playbook bug.

**Do not end your turn on a backgrounded landing.** A backgrounded Bash call whose output is
redirected to a file is not a harness-tracked child, so nothing wakes the session when it exits
(issue #1291).

**A task that comes back "stopped because the system is running low on memory" was reaped, not
failed.** Claude Code kills every running backgrounded Bash task on a Node `memoryPressure`
event. The kill arrives as a task notification rather than an error in the logfile, so the
session reads clean while the PR has merged and nothing followed it through. Re-running the same
command is the fix: it is idempotent, and a PR already `MERGED` is left alone. On `daniel-box`
the host unit turns the reaper off (`CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP=1` in
`hosts/components/claude-code/templates/claude-rc.service.j2`), so only a session started some other
way meets it.

### Exit codes and verdicts

A generated table lists the exit codes, and generated lists name the verdicts and causes. They
read `scripts/lib/exit_codes.py` and `scripts/deploy_tools/land_lib/outcome.py`, so a verdict
added there appears here without an edit.

--8<-- "assets/generated/fragments/land-verdicts.md"

## Arming the merge, and the two command shapes the classifier refuses

**`--arm-merge` readies the merge inside the script.** A bare `gh pr merge` once sat on the
ask list (`Bash(gh pr merge:*)`), and an unattended session has nobody to answer that prompt, so
it timed out as a denial. Three attempts, three denials, on 2026-09-03 (issue #979). Since
2026-10-11 `gh pr merge --auto` is allowed, so a session may arm GitHub's auto-merge; a plain
`gh pr merge` still asks. `land.sh --arm-merge` is not that command: its own text never
contains `gh pr merge`, so it reaches the classifier as the single script invocation the
worktree-containment check already accepts.

The arm is idempotent, so re-running after a `merge-conflict` or `merge-timeout` starts
cleanly. `--subject` overrides the squash commit's subject; the PR's own title is used
otherwise.

**`land.sh` merges every PR directly, never arming GitHub's auto-merge.** The direct merge is
pinned to the head SHA that `await_ci` read green and the landing policy checked. Auto-merge
merges whatever head is green when CI finishes. The arm path was removed in #4001, when the
agent branch fence still covered master and an armed PR sat `BLOCKED` because auto-merge never
applies a bypass (#3911). Since 2026-10-11 the fence excludes master and no rule needs a
review, so a PR armed for auto-merge by hand, from either account, merges once CI is green.

**A PR armed for auto-merge still needs a landing.** Auto-merge merges the PR and nothing
more. Run the usual `land.sh --pr <n> --arm-merge --await-merge` afterwards: the arm is a no-op
on a merged PR, and a PR still open is left for whichever merge fires first.

`--arm-merge` refuses a `CHANGES_REQUESTED` PR before any merge call, because someone asked for
it to change. `--await-merge` merges the PR through the REST merge endpoint on the first poll
where `await_ci` reads its head green, pinned to that head SHA. The master CI gate still refuses
the call until its checks pass. A refused merge is printed once and the wait continues, so a
merge GitHub keeps refusing ends at `merge-timeout`. The direct merge needs both flags in one
run: without `--await-merge` the arm dies, and `--await-merge` alone only polls.

**A master CI backlog holds a green PR's merge.** Before the direct merge, `land_lib/pace.py`
counts the master push runs of `ci.yml` that are not completed. At 2 or more it prints
`pacing the merge: …` and waits for the next poll. A full sweep is about 18 jobs against the
account's 20-job runner cap, so a second unfinished sweep already queues. On 2026-10-24, eight
concurrent landings left six master runs unfinished, their jobs waited 8-17 minutes for a
runner, and each landing waited 29-61 minutes for its merge commit's verdict (#5526).

A cap of 1 ran from 2026-10-25 to 2026-10-29 (#6308). It aimed to merge a re-tested PR into
the tree it tested, so that `prove` skips the push sweep. The share of push runs that skipped
the sweep did not rise, because the untested tree comes from master moving during the PR's
re-test rather than at merge time (#6309). The `DECIDED:` marker on `PACE_MASTER_RUNS` in
`land_lib/options.py` has the measurement. Batching the merges is the open route (#6521).

- The count is a read, not a lock. Two landings that read at the same poll can both merge.
  Pacing lowers the average queue depth; it does not close the race that makes the `prove`
  job's tested-tree skip miss.
- A run unfinished more than 2 hours after its creation is wedged and not counted.
- A held poll never ends the wait at `merge-timeout`. On 2026-10-28 eight landings raced
  for the one slot, and five ended `merge-timeout` while the queue was still draining
  (#6466).
- The cap rises by one for each 15 minutes a landing has been held, so the longest-held
  landings pass first, one at a time.
- Once the whole wait, held time included, reaches `merge_timeout`, the landing merges without
  pacing. The pre-merge wait therefore stays inside `merge_timeout`, or twice it when the
  landing updates the branch, which the `LAND_DEADLINE` sizing below counts on.
- A failed read merges without pacing. Pacing controls throughput; it is not a safety gate.
- `LAND_PACE_MASTER_RUNS` sets the cap, and `0` turns pacing off.
- A PR armed by hand with `gh pr merge --auto` merges without this check.

**A head whose only CI skipped as a draft is re-triggered once, then ends the wait if it skips again.** `ci.yml` runs
no job on a draft, so the push's run skips every job, the `prek` gate included, and `await_ci`
reads the head as pending. GitHub may start no run on a ready flip made seconds after the push.
PR #6069 was marked ready at 01:39:31 on 2026-10-24, its head's only run was created at
01:39:35 with every job skipped, and `land.sh` waited out its 1740s budget (#6077). No run was
cancelled, so `ci.yml`'s concurrency group did not cause it.

- The merge wait prints the cause on the first poll that reads such a head.
- Once the head has stayed so for `DRAFT_SKIPPED_GRACE_S` (180s), the wait reads the PR's
  draft state. A PR still a draft keeps waiting, because marking it ready starts its run.
- A ready PR is converted to draft and back by the landing itself (`gh pr ready --undo <n>`,
  then `gh pr ready <n>`), once per head, and the grace starts again. The landing prints a
  line saying it did, and the toggled run's verdict then arrives inside the existing
  `merge_timeout` budget (#6546). A re-run of the skipped run would replay its draft event and
  skip again, which is why the landing toggles instead.
- The landing does not toggle while the head has a `ci.yml` run that did not skip: one queued,
  in progress, or finished with any other conclusion. It prints why and restarts the grace.
  The ready run's `prek` gate gets its check run only once every job it needs has finished,
  so for those minutes `await_ci` reads only the draft run's skipped gate. A toggle then
  cancels the ready run: on PR 6443 it cancelled a run 32 seconds old, and on PR 6531 one 20
  minutes old (#6566). A failed read of the runs also stops the toggle.
- A second draft-skip on the same head after the toggle ends at `merge-timeout`, whose message
  says the toggle already ran. A toggle that fails also ends there, and names a PR left as a
  draft, because a draft PR never merges. The remedy is then a push, or the same two commands
  by hand.

**A check left pending under a completed workflow run ends the wait at `merge-timeout`.**
GitHub can leave a job `queued` with no runner after its workflow run reads `completed`. PR
#7975's required `pull + boot changed images` check did this on 2026-11-11. GitHub refused
every direct merge, and the landing waited about 75 minutes towards `merge-timeout` (#7996).

- `gh run rerun` refuses a completed run, and a `workflow_dispatch` run does not replace the
  PR's check. Only a new head commit gives the PR a fresh set of checks.
- Once the wait has sat on one head for `stuck_checks.AFTER_S` (600s), the merge wait reads
  that head's check runs and check suites on a poll where `await_ci` reads it green, and
  again every 600s after. A head that is not green is never read this way.
- A check still pending in a completed suite ends the wait at `merge-timeout`, exit 75. The
  message names the check and the remedy: push an empty commit to the PR branch, then re-run
  `land.sh`.
- A pending check in a suite still running is ordinary CI and keeps the wait. A failed read
  names nothing, so the wait goes on to its budget.

**Under `LAND_REQUIRE_AUTHOR=<login>` the arm refuses a PR by any other author.**
`renovate-agent-land@.service` sets it to `app/renovate`, so the unattended agent can only merge
Renovate's PRs (issue #2170). `--any-author` lifts it for a session that is allowed to merge the
PR; the unattended agent never passes it, because a `manual —` bump goes to a person through the
digest (issues #2746, #3420). An interactive shell leaves the
variable unset and never sees this.

**A lander unit can set a landing policy, which `--arm-merge` checks before any merge call.**
`land_lib/policy.py` owns the rules, and two settings switch them on.
`LAND_REQUIRE_BRANCH_PREFIX` requires a head branch in this repo, under the prefix, targeting
`master`. `LAND_APPROVAL_PATHS` names a file of path prefixes, and a PR changing a path under
one is refused, renamed files included. The same setting refuses a PR that changes a module the
landing process has imported. It also refuses a change that can run code on import with no import
line of its own: a non-empty `__init__.py`, an `__init__.py` leaving a directory that still holds
Python files, a symlink, an added `__main__.py`, a module beside a directory of the same name, and
a module at the top of one `sys.path` root that `land.py` inserts when it shadows a name in
another root.
Such code can enter `land.py`'s imports through a later approved PR whose diff never shows it.
`land_lib/policy.py:shape_hits` reads these from the file listing and from the tree at the pinned
head. `LAND_APPROVER` names a GitHub login whose latest
review, if it approves the head SHA, lifts that refusal. With either of the first two set, a non-empty `hold_sha` also refuses,
and every merge path is pinned to the head SHA the checks read. A push after the checks fails
the landing, and a re-run checks the new head. `--verdict-file <path>` writes `PENDING`, then
the landing's one result line, for a unit to hand on to the agent that started it. A landing
that sets neither setting is unchanged, and `--any-author` lifts neither.
`homelab-claude-land@<n>.service` (`roles/setup/claude_code`'s template, shipped in daniel-box's
host package) sets all four for the `claude` agent user's PRs, and a polkit rule lets that user
start it and do nothing else. That user's profile sets `LAND_HANDOFF_UNIT=homelab-claude-land`,
so its own `land.sh` starts the unit and reports the
unit's verdict line and exit code as its own (`land_lib/handoff.py`).

**The arm refuses a body whose closing keyword is not its own `Closes #N` line.** GitHub closes
an issue named after `close`/`fixes`/`resolved` however the sentence reads, so PR #2510's "Filed
and not fixed: #2509" closed the unfixed follow-up two seconds after the merge (issue #2513).
The refusal prints the offending line: `gh pr edit <n> --body-file` with the reference reworded
(`Filed for later: #N`), then re-run the same landing command. Expect to be rewording a
body another session wrote, since the fan-out agent opens the PR and a daniel-box session lands
it.

**The landing closes a closing reference GitHub left open.** PR #7295 merged with #7291 in
`closingIssuesReferences`, GitHub never closed #7291, and the backlog dispatched it twice more
(issue #7397). In step 1, `land_lib/closing_refs.py` reads each reference's state, waits 15
seconds for GitHub to catch up, and closes any issue still open. It leaves an issue reopened
after the merge and an issue in another repo open. A failed read or close keeps the verdict token
and the exit code, and its message and remedy end the `VERDICT:` line's detail. `cc-wait land`
prints that line, through a lander unit too. A PR merged without `land.sh` gets no such check.

**Open the PR with `--fill` and a body file — the same evasion, one command earlier.**
The worktree-containment check judges a command on its TEXT, so `gh pr create --title "…"` is
refused whenever the title carries `git`, `cd`, `worktree`, `write` or a construct the check
cannot parse — and a title here routinely names one, since it becomes the squash commit subject
and has to name the outcome. Rewording the title does not reliably help.

```
gh pr create --fill --body-file <path>   # title from the commit, body from the file
```

`--fill` takes the title from the commit subject, so the command line carries no title text to
judge, and `--body-file` carries no body text either. gh lets `--body-file` override the body
`--fill` would take from the commits. The file has to carry every `## ` heading of
`.github/pull_request_template.md`. The `block-footguns` hook denies a create or edit whose body
skips one, and a bare `--fill`, because rewriting the body after the PR opened was the
avoidable PR body rewrite in #3963. The class of false refusals narrowed after
Claude Code 2.1.257 (it fixed loops and heredocs that never touch git) and is not gone: a
`for … do gh issue comment …; done` was refused on its comment text on 2026-09-10 (issue #1431).
The rule generalises: keep the prose out of the command string and inside a file or a script.

## The merge wait

`--await-merge` polls the PR's state every 30s until it is merged and only then starts the
landing. It reads the state, never the checks, so it is not hand-polling. Without it the session
has to notice the merge itself, which every landing on 2026-09-01 did with a hand-written
`until MERGED` loop. A PR still open after 92 minutes exits `LAND_GAVE_UP`: it is not being merged, and the
reason is on the PR. A landing that updates the branch waits 92 minutes more from the update,
because the updated head's CI runs the whole chain again.

**A conflicting PR ends the wait at once**, exit `LAND_FAILED` with `VERDICT: merge-conflict`, rather than
sitting out the wait. A PR that goes conflicting during the wait never merges, and
nothing on the PR says so — with several sessions landing at once, another merge moving master
under an open PR is the ordinary way it happens. Rebase onto master, push, and re-run the same
command. The wait tolerates a `mergeable` of `UNKNOWN` — GitHub computes the
field asynchronously — and bails only after two consecutive `CONFLICTING` polls, because the
base moving under a PR flips it for one poll.

**A PR whose own CI is red ends the wait too**, exit `LAND_FAILED` with `VERDICT: pr-ci-red`, quoting what
`await_ci.py` said. GitHub reports it only as `mergeStateStatus: BLOCKED`, the same word it
uses while the checks are still running. No other rule blocks a PR into master, so the wait
treats `BLOCKED` as a question about checks. The wait keeps going
while `await_ci.py` answers `pending`, which is what it answers until a required check
registers; that is the grace period, so no landing is cut short for polling before CI started.

**A PR that bumps a forward-only pin merges only through the pin gate.** When the head moves
`k3s_longhorn_version` or `k3s_metallb_version`, `land_lib/pin_gate.py` runs before the direct
merge. It refuses on any host but daniel-box. It refuses while `DEPLOY_HOLD` reads anything
but `none`, and an empty or missing value refuses too. It also refuses while (`last-good`,
master tip] already changes the same pin. It then runs `runbook_gates.py longhorn-upgrade` or
`metallb-upgrade`, and a non-zero exit refuses. On a pass it posts the commit status
`homelab/upgrade-gates` on the head. `promote` requires that status, at most six hours old,
before it applies the bump, and holds without it (docs/flux-deploy-plane.md, *Forward-only
tags*). Every refusal ends the landing with `refused` and nothing merged. A merge made outside
`land.sh` bypasses the gate. For such a merge, `land.sh --regate <pr>` re-runs the gates for
the merged PR and re-posts the status without merging. It exits 0 when it posted and 1 when it
refused. The upgrade runbook's gate section, [docs/longhorn-upgrade.md](longhorn-upgrade.md),
has the operator-only fix-forward after a failed bump.

## The CI wait

**Do not hand-poll CI and do not hand-merge.** `await_ci.py` reads the same check-runs endpoint
the deployer reads, so its verdict and the tick's agree by construction. Hand-polling cost 835
polls across 213 wait episodes before it existed.

`cancelled`, `stale` and `skipped_by_concurrency` mean *no verdict for this SHA*, never *this
SHA is bad* — `_CI_NO_VERDICT_CONCLUSIONS` in `hosts/lib/ci_verdict.py` is the list, and a commit whose
merge was immediately followed by another reads `cancelled` permanently. The rule covers the
gate job too: ci.yml runs `prek` under `always()`, so a cancelled run still runs the gate, which
fails on its cancelled needs. `ci_verdict.ci_verdict` leaves that gate out unless a need in the
same run failed outright (#5931), and the deployer's tick reads it the same way (#6007).
`await_ci.py` follows the tip in that case, but only once your commit is an ancestor of it. The log line is `<sha> has
no verdict (cancelled/stale) — following the tip <tip>`, and it is normal. It also fires when
the cancellation came before the `prek` job registered a check-run at all: the check-runs list
then never carries the required name, so `await_ci.py` reads the SHA's check-suites (a
`completed cancelled` suite with zero runs) to tell that from a fresh push. Before PR #775 that
case waited out the whole budget and exited 75.

GitHub sometimes drops a master push event, and then the commit never gets a CI run at all.
`await_ci.py` reads a SHA with no GitHub Actions check suite after `DROPPED_PUSH_GRACE_S` as
that case. It then takes the verdict of the nearest descendant on master whose CI has
finished, which is the rule the tick already deploys by (#4621). The log line is
`<descendant>: CI green (GitHub never ran CI for <sha>; ...)`. A red nearest descendant ends
the wait red; a later green commit is not taken past it, since it may hold the fix your merge
commit lacks. The landing still deploys its own merge commit, whose tree no CI ran on by
itself; the `DECIDED:` marker on `descendant_verdict` records why that is accepted. When a
descendant's CI is still running, the wait exits 75 and says so, and a re-run converges once
that commit has a verdict. Past a red descendant, the wait skips a later commit that can never
get a verdict: one whose runs were all cancelled, or whose push GitHub dropped too. With
nothing left that can still settle, the wait ends red on its first poll after the grace period
rather than at its deadline (#4667).

**Three shapes end `ci-unproven` instead, because no re-run converges on them (#4641, #4649).**
`await_ci.py` exits `CI_UNPROVEN` and its line names the master tip and the way out:

`promote` refuses a commit without green CI (`.github/workflows/promote.yml`, *Refuse a dispatched
commit whose CI is not green*), so no route applies the tip without CI. A dispatched CI run starts
no promotion, because `promote` follows only a CI run from a push. The line therefore prints this
dispatch, with the tip's full SHA:

```bash
gh workflow run promote.yml -R DanielH2018/server --ref master -f sha=<master tip>
```

- No commit from the merge commit to the tip has a CI run, which includes the merge commit
  being the tip. To run CI on the tip, run `gh workflow run ci.yml --ref master`. Once that run
  is green, run the `promote` dispatch, then re-run land.sh.
- The nearest descendant is red and a later one is green. While a later one's CI is still
  running, the wait keeps polling for it rather than ending red. Neither verdict changes, so a
  re-run of land.sh reads the same state. The tip contains your merge commit. To apply your
  change, run the `promote` dispatch, once the tip is green when the line says so.
- The nearest descendant is red, and GitHub never ran CI for the tip after it. The wait keeps
  polling until its deadline, since a freshly merged tip has no CI for its first seconds
  either. To give the tip a verdict, run `gh workflow run ci.yml --ref master`, then re-run
  land.sh. A green tip then ends the landing on the shape above.

**It waits on your merge commit rather than on the tip**, because the deployer's timer fast-forwards to the
newest GREEN commit in its range rather than only to a green tip. A later merge whose CI is
still running no longer holds your PR. Waiting on the tip cost six landings in the 14 days to
2026-09-11 400-614s each, chasing a tip that moved again.

## The promotion wait

**No landing runs `deploy.sh`.** Every `containers_list` entry is `platform: k8s`, so `promote`
and Flux apply every tag, and a change to a host's package waits for that host
(`scripts/deploy_tools/land_lib/host_wait.py:wait_for_hosts`). A `--tags` value that names no k8s
entry at the merge commit ends the landing `deploy-failed` with the cause `tag-miss`, because
nothing would apply it.

**`land.sh` starts no GitOps tick and reads none of the deployer's markers** (H7.49a). The
deployer's own 10-minute timer still fast-forwards the primary checkout and applies a setup
role, and `land.sh` neither starts, awaits nor grades that. A landing waits on the promotion
for a PR's k8s tags, on the host wait for a host package, and on the health gate, and nothing
else.

**A PR that reaches only a setup role the deployer applies waits for master CI and the
promotion, then ends `nothing-to-deploy`.** Its author reads the outcome on the host: the changed
state itself, or a `manual_plane` line in
`owed.jsonl` for a role the deployer cannot apply (`k3s`). `probe.py gitops-state` prints every
marker, read-only. A change to a plane nobody applies on its own, which `plane` names, still
ends `needs-manual-apply` with the command that applies it.

**It scopes the derivation to the PR's own file list rather than a SHA range**, so another session's
merged work is not swept in. `gh` paginates that list at 100 files, so it falls back to
`--changed <since>` when the count disagrees — the only reason `--since` exists. That fallback
refuses a range wholesale as soon as one path is broad. Since D6.12 such a range derives no tag
and waits on the promotion, whose changed set names the k8s tags; a Pi Compose service it
reaches ships in the `homelab-daniel-pi` package. `--tags` overrides the scope entirely.

### Every landing waits on promotion

**A k8s tag never reaches `deploy.sh`.** Step 4 reads the k8s entries at the merge commit
(`land_lib/promotion.py:split_k8s`) and takes every such tag out of the list.

**The changed set names the k8s tags, not the PR's paths.** `split_k8s` drops a path-derived
k8s tag and keeps one `--tags` named. Every PR with a path other than a document or a test waits
on the promotion (`land_lib/promotion.py:moves_the_bundle`), and land-wait's changed set, joined
with any `--tags`, names the k8s tags the gate grades and the annotation names. A
shared template, an inventory edit or a `manifests` change therefore gates exactly the tags whose
render moved. A PR that moves no bundle file waits on `homelab` alone and gates nothing. The
gate renders the merge commit from a snapshot, because the primary checkout may not hold it yet.
A `cluster-vars` change moves the footprint of every substituting tag, so
D3.4's interim `--also cluster-vars` went in the same PR.

`land.sh` runs `promote.py land-wait <merge commit>`. The wait never follows one
`promote` run, because GitHub may evict a queued run and a later run may promote a newer commit.
Instead it reads the cluster every 30 seconds:

- **The changed set.** It waits on `homelab` and every Kustomization whose `footprint` differs
  between the merge commit's `digests.json` and the one on the newest published ancestor of its
  first parent (`scripts/deploy_tools/changed_set.py`). A file that is missing or unreadable
  widens the wait to every Kustomization, and the progress line says so. `sha-<merge commit>`
  is usually not published when the wait starts, so the wait widens until it is.
- **`settled`.** Every expected Kustomization is Ready, and its
  `status.lastAppliedOriginRevision` names the merge commit or a commit R that descends from
  it. When the changed set holds `soak`, as it does whenever it holds a tag, R must also carry
  a `flux/soak` `success`. The tags read Ready before the soak Job reaches a verdict, so
  `land-wait` reads R's combined status with `gh` on each poll until the soak has one. No
  status, or a `pending` one, keeps the wait going. `land-wait` then writes one
  `deploy-annotation` line with `source=promote`, and the landing goes on to the health gate
  for each k8s tag.
- **`deploy-failed`, cause `soak-failed`.** R carries a `flux/soak` `failure` or `error`.
  The wait ends at once rather than waiting for `verify` to write `DEPLOY_HOLD`. The line
  prints the read-only command that names the services the soak failed:
  `kubectl -n flux-system get pods -l app=soak -o jsonpath='{.items[*].status.containerStatuses[0].state.terminated.message}'`.
  `verify` holds the same revision, and for a revision with no snapshot role resets `prod`
  (`docs/volume-snapshot-drills.md`, *Choosing the branch*). A failure read once `homelab`
  has moved to a descendant of R does not end the wait: a later revision superseded R's
  soak, which exits once `homelab` leaves R (#5704), and the wait reads that revision's soak
  instead (D7.2).
- **`deploy-failed`, cause `promote-held`.** `DEPLOY_HOLD` holds a value other than `none`. The
  line names the held SHA and, for a `failed=` hold, the failed names, and says whether any
  of them is one of this PR's services. The held SHA is the newest commit the promotion
  carried, which may be another PR's, so a hold naming none of this PR's services was most
  likely caused by another merge in it, or by a run it superseded (D7, #6199). Clearing the hold is
  the operator's decision, approved through the permission prompt the agent's clear raises. `promote`'s `settle` job writes a `failed=` hold when a promotion fails
  its verification (`docs/flux-deploy-plane.md`, *Verification*).
- **`nothing-to-deploy`, from land-wait's `suspended`, exit 0.** Every tag Flux applies for
  the PR is `suspended` in the merge commit's `digests.json`, so Flux applies nothing for
  them until a PR removes their `flux_suspend`. The wait ends at the first poll that reads
  that file and a `none` `DEPLOY_HOLD`, rather than polling to `gave-up`. A hold still ends
  it `deploy-failed`. When only some of the PR's Flux tags are suspended, the wait leaves
  the suspended ones out and settles on the rest, and the gate leaves them out too. The
  suspend PR itself ends this way, before any promotion applies it: the suspend takes hold only once `last-good`
  carries it, so check that `probe.py landing` shows `last-good` at or past the merge commit.
  A change to a suspended tag cannot reach master through CI, because `render_diff_gates.py`
  refuses it. One that races past the gate does not end here: `promote.py decide` holds it
  as `suspended-changed`, and the wait ends `deploy-failed`, cause `promote-held`
  (`docs/flux-deploy-plane.md`, *Stopping one service: `flux_suspend`*).
- **`needs-manual-apply`, from land-wait's `suspended`, exit 1.** The same wait, on a PR that
  also owes a plane a hand applies or a setup role's other hosts. The output names what is
  owed and each host package the landing did not wait on, and the gate does not run. The
  suspend PR itself owes neither, so it keeps `nothing-to-deploy`. A PR that changes a host
  package and owes neither goes on to the host wait, and its verdict comes from that wait.
- **`deferred`, cause `promote-timeout`, exit 75.** No promotion covered the merge commit
  within twice the promote timeout (83 minutes,
  `scripts/deploy_tools/promote_lib/wait.py:LAND_WAIT_TIMEOUT_S`), or within twice
  `verify`'s wait on the listed `soak` Kustomization's `timeout`, whichever is later. The
  soak's `timeout` grows with every flux-owned Kustomization. Look first at the
  `promote` workflow's failed `publish` jobs: a publish refusal leaves no `sha-<commit>` tag
  to promote. The next promotion may still cover the merge commit.
  The 83 minutes count again from each poll that reads a verification of an earlier commit
  in flight, for up to `wait.py:IN_FLIGHT_WAIT_S`. M's `decide` waits on that verification
  before it promotes M, so two merges minutes apart would otherwise give up on a promotion
  that passes later (#5164).
  A lander that is killed at a fixed time sets `LAND_DEADLINE`, the seconds after `land.sh`
  starts by which the landing must print its verdict. The wait then gives up 10 minutes
  before that deadline, whatever is in flight, and the landing still ends `deferred` with a
  ledger line (#5199). So on a lander unit the wait can give up before twice `verify`'s wait.
  `homelab-claude-land@.service` and `renovate-agent-land@.service` each set 41460. After the merge
  and master CI budgets, that leaves the wait room for `verify`'s wait on the rendered soak,
  once, and the `promote` workflow's `render`, `images` and `publish` jobs at their
  timeouts. It also covers one measured rebuild of every built image by a commit queued ahead
  (#5431). `images` counts at its 20 minutes because published tags build images since D5.14
  handed ical-proxy and homelab-mcp to Flux (#5466). D5.16 raised the deadline from 18000,
  because code-server's `code-server-prepull` Kustomization, which covers its image's slow
  pull from GHCR at a 4500s `timeout`, put the soak's `timeout` at 14070s and left the wait
  3544 seconds short. D5.20 then published sonarr, radarr and mosquitto with two verify
  Kustomizations, which put the soak's `timeout` at 14934s, so D5.16 raised the deadline
  again, to 25200. n8n's flip added `n8n-prepull` at a 3600s `timeout` and put the soak's
  `timeout` at 15894s, and later flips took it to 17130s. The flip that handed karakeep,
  pi-peer-backup and media-volume to Flux added karakeep's 1200s budget, its snapshot and
  three verify Kustomizations. That put the soak's `timeout` at 18114s and left the wait 388
  seconds short, so the flip raised the deadline to 28800, which leaves 3212 seconds of
  headroom (#5512, #5566, #5546, #5652). The master CI budget then rose from 1200s to
  4620s, which is `ci.yml`'s longest `timeout-minutes` chain into the `prek` gate plus 10
  minutes of queueing (`land_lib/options.py:CI_TIMEOUT_S`). Every host-package landing had
  ended on `ci-timeout`, because the `host_packages` jobs alone run about 20 minutes. The
  deadline and every bound above it rose by the same 3420s, to 32220 (#6857). The
  `host_packages` timeout then rose from 45 to 60 minutes, because a slow Ubuntu snapshot
  mirror took a leg to about 56 minutes. That moved the master CI budget from 4620s to
  5520s, and the deadline and the Renovate bounds above it by the same 900s, to 33120
  (#7128). The merge budget then rose from 2700s to the same 5520s, because a PR's own CI
  runs that chain and a landing ended `merge-timeout` while its CI could still go green.
  A branch update starts that chain again and gives the wait one more budget, so the
  deadline counts two merge budgets. It and every bound above it rose by 8340s, to 41460
  (#7129).
  `ansible/tests/setup/test_lander_deadlines_cover_the_soak.py` renders the published bundle
  and fails once a newly published tag outgrows that headroom (#5427).
  Inside the no-promotion window around the weekly reboot, `decide` defers every promotion (D5.2b2),
  so a landing that merges as the window opens waits up to 22200 seconds before any
  promotion starts. The first poll inside the window moves the cap by the window's remaining
  length, up to the unit's `LAND_WINDOW_GRACE`. Both lander units set the window's full
  length, and every bound above `LAND_DEADLINE` covers it: homelab-claude-land's `TimeoutStartSec` of
  19h, and the 1071min the Renovate lander unit sets, with `land_renovate_pr.py`'s `LAND_TIMEOUT_S`. The
  Renovate lander needs it too: its unit runs on after the session ends, so a landing started
  late on a Saturday can still be waiting on Sunday's window (#5457).

Two holds coexist, and `probe.py landing` prints both on every run, each naming its source and
what it holds:

```text
hold_sha (GitOps deployer, setup plane): none
DEPLOY_HOLD (promote workflow, Flux-owned tags): 0123abcd... failed=bento-pdf
prod: 3cf44936 sha256:4e55da43005e   last-good: 9f1e2d3c sha256:0b7a51c2e9d4
```

`hold_sha` stops the deployer's setup-plane applies, and `land.sh` refuses to merge while one is
set. `DEPLOY_HOLD` stops every promotion, so it ends `land-wait` for a flux-owned tag.

`land-wait` reads the Kustomizations with the read-only `kubectl`, ancestry with
`git merge-base --is-ancestor` in the primary checkout, `DEPLOY_HOLD` with
`gh variable get`, and, once the tags have settled, R's `flux/soak` status. Each of the last
two is one authenticated GitHub call per poll, at most 240 an hour. A read that fails keeps
the wait going, and an unreadable Kustomization list never reads as `settled`.

`land.sh` runs from the primary checkout wherever you invoke it. One read asks about the primary:
the diff-derived tag list a truncated file list falls back to.

### A PR that adds an edge beside a change is refused before it merges

CI's **Render-diff gates** step, in the `pr_gates` job on pull requests only, runs
`scripts/validate/render_diff_gates.py --base HEAD^1`. It renders the cluster at the base and
at the merge commit with `homelab-render cluster`, each side with its own tree's renderer. A
tag counts as changed when its `cluster/<tag>/` output moved. The gate refuses five shapes,
and keeps the arm numbers its history gave them:

- **Arm 2, edge first.** A tag that is flux-owned at the base and at the head gains a
  `dependsOn` edge, and its own render moves in the same PR. A tag that becomes flux-owned in
  the PR is not judged, because its Kustomization is born with its edges. **To split an arm-2
  refusal, land the edge alone first**, then the change.
- **Arm 3, removals.** A flux-owned object dropped without a `Removes-Object:` trailer, a whole
  tag's objects included (`docs/flux-deploy-plane.md`, *Retiring a flux-owned service*).
- **Arm 5, suspended tags.** A change to the render of a tag `flux_suspend` holds.
- **Arm 6, the tag rule.** A tag leaves the render while its role directory stays, `flux` is
  missing, or a tag whose base render holds a PersistentVolume leaves at all. The publish job
  runs the same rule against `prod`, so a PR whose checks ran on an old base still fails there
  (`docs/flux-deploy-plane.md`, *The tag rule*).

The gate has no bypass, because a bypass would become the default route. A render that fails at
either end exits 2, and the step fails without a verdict.

**HISTORY — arm 1, plane order, went in D6.12.** Until every k8s role was flux-owned, arm 1
refused a PR that changed two roles on different planes along one dependency edge, because
`land.sh` applied the Ansible half and `promote` the Flux half in whichever order they
finished. The module was `cross_plane_order.py`. D5 left no Ansible-owned k8s role, so the arm
had no subject, and D6.12 deleted it with the replay script that measured its cost
(`docs/target-architecture/deploy/d6-delete-old-path.md`, *The render-diff gates after D6*).

### A cluster pin moves forward, one step, alone in its PR

Merging `k3s_longhorn_version` or `k3s_metallb_version` is the upgrade, and neither upgrade
runs backwards. CI's **Cluster pin checks** step, in the `pr_gates` job on pull requests only,
runs `scripts/validate/cluster_pins.py --base HEAD^1`. It reads each pin from whichever role
YAML file holds the key at the base and at the merge commit, and refuses five shapes:

- **A decrease.** A revert PR of a bump fails here. To undo a bump, fix forward in a PR that
  keeps the version.
- **A skipped Longhorn minor.** `v1.12.1` to `v1.14.0` is refused, and so is a major bump.
  Bump one minor release per PR.
- **A bump that shares its PR.** A PR that bumps a pin may change only the role directory
  that holds the pin, which carries its vendored file, and docs. A test, `renovate.json` or
  another role goes in a PR of its own. `land.sh`'s land gate relies on this refusal.
- **Two pins bumped together.** Bump each pin in its own PR.
- **A pin set outside the role file that holds it.** Inventory, play vars, `include_role`
  vars and `set_fact` override a role default, so a pin there would move the deployed version
  while the role file stays put. The check matches the key at any indent, as YAML `key:` or
  INI `key=`, in every YAML and INI file under `ansible/` and every file under `inventory/`,
  `group_vars/` or `host_vars/`. It skips a comment, which starts at a `#` that opens the line
  or follows whitespace outside a quoted value, or at a `;` that opens the line. A quoted
  `#`, as in `note="a #b"`, is part of the value. A `-e` passed at run time is outside
  its reach.

A PR that moves a key into another role at the same version is no bump, so none of the first
four applies to it. A key set in two role files at one commit, or a value that is not `vX.Y.Z`,
exits 2: the check cannot judge it. There is no bypass.

## Reading a `deploy-failed`

**`deploy-failed` does not say what went live.** `land.sh` runs no deploy itself, and `promote`
and Flux apply master whatever a landing's verdict was. The cause beside it says which stop this
was, and each one leaves a different state:

- `tag-miss`: no k8s entry declares a tag named on `--tags` at the merge commit, so nothing applies
  that tag. `promote` and Flux still apply the rest of the merge, so a tag named beside it can be
  live. To gate the rest, re-run with the tag corrected.
- `promote-held`: `DEPLOY_HOLD` stops every promotion, and the hold does not say whether this merge
  went live. `decide` writes a forward-only or `suspended-changed` hold before `prod` moves, and
  a hold set by hand moves nothing. A failed verification's hold follows a move of `prod`, so
  Flux may have applied this merge already. `settle` then resets `prod` to `last-good`, unless the
  held commit changes a forward-only tag or a snapshot role. An earlier promotion can also have
  carried this merge before the hold. To see where it stands, compare the merge commit with the
  `prod` and `last-good` commits `probe.py landing` prints.
- `soak-failed`: Flux applied revision R and R's `flux/soak` then read `failure`, so the change is
  live and `promote`'s verify holds it.

## The health gate

The health gate renders from a detached worktree of the same commit. `probe.py health <tag>`
enumerates the workloads to check by rendering the role's manifests from the checkout it runs
in, so gating a brand-new role from a primary that has not pulled it yet enumerates nothing and
reads `skipped` — green, on the landing that most needs a gate. That worktree takes no lock. If
it cannot be made at all, the landing reports `unhealthy` rather than gating the primary — the
one exception being a primary that already contains the commit.

A gate run by hand has the same exposure. An orchestrator worktree locked hours behind master
holds old role templates, owners and alert tables, and a `probe.py health` run from it grades
the live workload against them (#5912). The `block-footguns` hook denies `probe.py health` and
`probe.py takeover` from a linked worktree that holds an older copy of an `ansible/` or
`scripts/` file `origin/master` has changed. The denial names the same command run from the
primary checkout. Rebasing the worktree onto `origin/master` also clears it. `land.sh` runs the probe as a subprocess, so the
hook never sees the gate above.

A gate that could not read is not a failed gate. `probe.py health` exits 75
(`PROBE_UNREADABLE`) when it cannot reach the store it reads, such as Traefik answering `no
available server` while Prometheus rolls. The workload was not graded, healthy or not. When no
other tag reads unhealthy, `land.sh` asks the gate again every 30 seconds for up to 5
minutes (`health_verdict.UNREADABLE_RETRY_S`). The PRs that hit this roll the store
themselves, and the store's roll takes about 30s. Only a store still unreadable after that
ends the landing `deferred`, exit 75, cause `gate-unreadable`, with
`outcome.UNREADABLE_GATE_NOTE`. No tick gates
health, so `cc-wait land` reports this deferral as `gave-up`: re-run `land.sh` to grade the
deploy. PR #5533's landing read `unhealthy` this way on 2026-10-18 while its pod was `1/1
Running` (#5543). Before the retry, 29 landings in the 14 days to 2026-11-09 deferred on an
unreadable store, and 22 of them were never re-run.

The gate's worktree sits at `origin/master` whenever that contains the deployed commit, not at
the merge commit. `probe.py health` refuses a role whose files `origin/master` changed after
its tree's HEAD, so a gate pinned to the merge commit deferred on every re-run once a later
commit touched a landed role (#7816). The tip can carry a role change `promote` has not applied
yet. The gate then grades the live workload against that newer definition, which the probe
would otherwise refuse to grade at all. When master moves while the gate runs, the probe still
refuses, and the landing ends `deferred`, cause `gate-stale`, with `outcome.STALE_GATE_NOTE`
rather than the store wording. `cc-wait land` reports that deferral as `gave-up` too: re-run `land.sh`.

**Verify the change, not just the workload.** The `VERDICT:` line gates the rollout and the 180s
restart window. It cannot see whether *your change* took effect: an Authelia 302 fires in the
middleware before the backend is reached, and 19 dead Grafana panels sat behind a 1/1 pod.

## `deferred`, `needs-manual-apply`, and what a hand still has to run

`deferred` (`LAND_GAVE_UP`, exit 75) means the landing gave up waiting and a re-run of
`land.sh` completes it: no promotion covered the merge commit in time (cause `promote-timeout`),
a host's package is not yet applied, or the health gate could not read its store (cause
`gate-unreadable`) or found its tree older than `origin/master` (cause `gate-stale`; see *The
health gate*), or the owed ledger could not be read (cause `ledger-unreadable`).
`cc-wait land` ends the wait as `gave-up`, exit 3, for each of those four causes, read from
the `LAND-RESULT:` line rather than the note beside it. It ends the wait as `deferred`, exit
4, for any other.

`nothing-to-deploy` also covers a PR that reaches only a setup role the deployer applies.
`land.sh` starts no tick and reads none of the deployer's state, so it reports neither the
apply nor a failure of it, and it names no other host the role reaches. Read the host: the
changed state itself, or `probe.py gitops-state`, which prints `last_run`, `hold_sha` and
`owed.jsonl`. The deployer records a `manual_plane` line for a role it cannot apply, and for a
role it applied on daniel-box whose change reaches another host (#6510). No check pages on a
`hold_sha`. A failed apply shows in the deployer's Discord post, in the SessionStart banner and
as `deploy-failed` from every later `land.sh` run.

**The owed ledger decides which setup role a hand owes.** `land.sh` reads `owed.jsonl` in step 1.
A setup role the tick does not apply (`k3s`, `common`) ends `needs-manual-apply` only when the
ledger holds a `manual_plane` line for it, and the note then prints the apply command and the
`clear-owed manual_plane <role>` after it. The PR's paths only nominate roles: a ledger line for a
role the PR does not touch owes this PR nothing, and a modified role with no line owes nothing.
The deployer's timer is retired (H7.52), so nothing writes a new line; a path-derived verdict
named work no one owed (#7906). A bring-up playbook (`_BROAD_MANUAL_PREFIXES`) stays
path-derived, because the tick parked on it and never wrote a line.

**A deleted path owes nothing only when the PR deletes the role's whole task set.** `classify`
reads each file's `changeType` from `gh pr view` and the role's `tasks/` at the merge commit. It
leaves a `DELETED` path out of the plane note when it sits outside every setup role, or when no
task file of its role remains. The service tags still read it. A PR that deletes only a setup
role's files therefore ends `settled` or `nothing-to-deploy` (#7885). A PR that deletes one task
file and keeps another keeps the role, so the ledger still decides whether a hand owes it (#7906).

**An unreadable ledger ends `deferred`.** When `owed.jsonl` exists but cannot be read and the PR
changes a setup role the tick does not apply, the landing ends `deferred` (exit 75), cause
`ledger-unreadable`, and names the read error. Re-run `land.sh` once the ledger reads. A PR that nominates no such role never reads
the ledger, so a broken one cannot defer it.

**Read every remediation line, not the first one.** One PR can reach two of these at once, for
example a plane a hand applies and a host package, and the verdict then prints one line per
half.

Two things sit in the position of needing a hand:

- **A setup role `initial_setup.yml` does not include** (`k3s` is in `k3s-bringup.yml`; `common`
  is include-only, so the note names each consumer's playbook and tag), or a bring-up playbook.
  The deployer applies every other setup role itself, and `deploy.yml` is a `containers_list`
  loop.
- **A rotated secret** has no path to match: a secret's value lives in no role's template, so
  `ansible/vars/secrets.yml` derives zero tags however many roles consume it. Run
  `uv run python scripts/secrets_mgmt/secret_rotation.py consumers <secret>` for who holds a
  stale copy and the repair command per plane.

**A setup role the deployer cannot apply also needs its marker cleared.** For `k3s` and `common`
the tick fast-forwards the range and records a `manual_plane` line in the `owed` ledger.
`scripts/deploy_tools/owed_run.py <role>` runs the printed apply commands and the clear in one
step, and `--dry-run` shows them first. It refuses a line whose apply arms more than the change, a role
applied by hand, and a primary checkout behind `origin/master`, so run those as printed, including any
`--applied <tags>` on the clear. A bring-up
playbook gets no such line, because the tick parks on those and writes no marker. A landing reads
the line (see above), so a PR touching a recorded role ends `needs-manual-apply`. A role the tick DID apply
on daniel-box gets a line too when its change reaches another host (#6510): the line's `hosts`
key names the hosts the "also reaches" note names, and the banner and monitor-bridge print the
apply for each. Apply it there, then `clear-owed manual_plane <role>`. The clear, `--applied` and
the six-hour page are in
[A role only a hand can apply is recorded, not parked](archive/gitops-deployer.md#a-role-only-a-hand-can-apply-is-recorded-not-parked).

## Waiting for a host to apply its package

A PR that changes a path under a member of `HOST_INPUTS` (`hosts/`, the inventory, the
renderer and the other members `renderer_lib/host_inputs.py` lists) is a host candidate. Resolve
sets the flag from the file list and renders nothing, because it reads the merge commit before
master CI and must not run that commit's code. After master CI is green, `host_wait.host_targets`
renders every host package at the merge commit and at its first parent, and keeps the hosts whose
trees differ. It renders every shared package under `hosts/packages/` the same way. A host is a
target too when its rendered `nfpm.yaml` depends on a shared package whose render differs,
because that host's updater installs the new shared package (#6859). Before that rule, a
`homelab-tools` change landed `nothing-to-deploy` while every host was about to install it.
When no host differs, the candidate is cleared and the landing ends
`nothing-to-deploy`. A render or snapshot that fails makes every host a target, so a failure
waits rather than skips.

A PR with host targets and no service tag deploys nothing in step 4. A PR that also carries a plane a hand applies still ends
`needs-manual-apply`, and the output names each host it did not wait on.

Step 5, `host_wait.wait_for_hosts`, queries Prometheus on loopback and then on the ClusterIP.
A host settles when `homelab_host_applied_info` equals or descends from the merge commit and
`homelab_host_reconcile_ok` is 1. A `homelab_host_reconcile_ok` of 0 ends the landing
`deploy-failed` and names the host's `homelab-updater --clear-hold` command. The step queries
once before it waits, so a host that already settled never delays the landing.

It ends `deferred` in three cases:

- The host's package at the first parent ships no enable link for `homelab-updater.timer`. A host
  holds at best the first parent's package until the builder publishes this merge, so a link
  that arrives in this merge arms nothing until an updater run installs it. The operator starts
  the updater by hand with `systemctl start homelab-updater.service` as root on that host, once
  the agent's Prometheus shows `homelab_repo_published_info` at the merge commit. They then
  re-run the same `land.sh` command. daniel-box, daniel-server and the Pi ship the link, so
  this case reaches only the merge that first adds a link (#6218 added daniel-box's).
- The host's deadline is more than 45 minutes away. The deadline is the second updater fire
  after the wait's first query plus 10 minutes, read from the first parent's rendered
  drop-in. That query follows master CI going green at the merge, because no host can apply
  the merge before the builder publishes it. Counted from the merge instead, the deadline
  had passed before CI went green on 24 of 51 host deferrals in the 14 days to 2026-11-09.
- The host is still unsettled past its deadline. The line gives the host's last success
  timestamp.

Before it ends `deferred` in any of the three, it reads the builder's two series from
daniel-box's textfile exporter, `homelab_repo_published_info` and
`homelab_builder_unbuilt_since_timestamp_seconds`, the series `probe.py builder` prints. When
the builder's reason is not `ok`, `canary` or `no-green`, and no `homelab-<host>` version it publishes
is the merge commit or a descendant, no updater fire can apply the merge. The landing then ends
`unbuilt`, exit 75, naming the reason and the last version published for that host. Fix the
builder, then re-run the same `land.sh` command. During the builder outage of 2026-11-02, five
landings read `deferred` instead and hid it (#7024). An `ok`, `canary` or `no-green` builder that has
not reached the merge is lagging by design, so that landing stays `deferred`: the host wait runs
only after master CI is green at the merge, so a `no-green` predates it. A series Prometheus
does not report leaves it `deferred` too, because an unread source proves nothing.

A failing reason counts only from a builder run that fetched master after the wait's first
query, which follows master CI going green at the merge. An older run may have failed on an
ancestor that this merge fixes. Until a later run replaces that reason, the wait polls
`homelab_builder_fetch_timestamp_seconds` for up to `probe.py builder`'s fetch deadline, two
timer periods plus the unit's `TimeoutStartSec`. Past that deadline the reason stands.

## `nothing-to-deploy`

Decided from the PR's file list right after the merge, before any CI wait: a PR that reaches no
service tag, no plane a hand applies, no host package and no path outside documents and tests has
nothing to wait on. A PR that reaches a setup role is a path outside both, so it waits for master CI
and for a promotion of `homelab` alone, then ends `nothing-to-deploy` in step 4: the deployer's
own timer applies the role, and `land.sh` does not watch the apply. The one exception to the early
decision is a file list GitHub truncated, which is derived from the diff in step 4.

**A document under a role is not a manual apply.** A `.md` — a role's `CLAUDE.md`, a
`README.md`, one under `files/` — maps to no role and to no plane, because no playbook applies
prose. Landing PR #1696 ended `needs-manual-apply` for a role whose only changed file was its
CLAUDE.md, and asked for a full `ansible/deploy.yml` (issue #1701). `land_tags.role_for` now agrees with the deployer's own mapper, and
`land_tags.quiet_paths` drops a `.md` under the setup plane before any diff is read.

**A role the PR itself registers is not one either.** Which tags exist is read at the MERGE
COMMIT (`land_tags.service_tags_at`), not from a checkout — a `containers_list` entry added by
the same PR is absent from every tree until the tick fast-forwards, and `land.sh` used to report
the new role as unregistered and ask for a full `ansible/deploy.yml`. PR #1539 landed that way
and `./scripts/deploy.sh --tags pihole-exporter` deployed it a minute later (issue #1544).

## The annotation, and the Landings board

Every run writes one logfmt line to syslog on exit (`logger -t landing-annotation`): the PR, the
merge SHA, the verdict, and seconds spent in each phase — `wait_merge`, `wait_ci`, `deploy`,
`total`. `deploy` runs from master CI's answer to the end of the promotion wait. Lines written
before H7.49a also carry `tick`, `lock`, `holder` and `kick`, which described the GitOps tick
`land.sh` no longer runs. Loki keeps them until about 2026-12-07, and no panel reads them.

Promtail ships it to Loki and the **Landings** Grafana board (Infrastructure folder) plots it, so
"sessions wait too long" is answered by the phase medians there rather than by memory:

```bash
uv run python scripts/diagnostics/probe.py loki-query '{job="syslog"} |= "event=landing" | logfmt'
```

A usage error annotates nothing. argparse raises before a `Landing` or a `Ledger` exists, so the
only line it could write is `pr=unknown verdict=aborted` — a row meaning "you typed the command
wrong" in the stream the board counts. The bash original annotated it because its EXIT trap was
installed before the argument loop, and the Python port reproduced that until issue #1304 measured
592 such rows in Loki's 744h window.
