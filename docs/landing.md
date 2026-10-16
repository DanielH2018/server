# Landing a merged PR

`land.sh` follows a merged PR through to a verified deploy in one invocation: arm the merge,
wait for master CI on the merge commit, deploy that commit, kick the tick, gate the health, and
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
`roles/setup/claude_code/templates/claude-rc.service.j2`), so only a session started some other
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
`claude-land@<n>.service` (`roles/setup/claude_code`) sets all four for the `claude` agent
user's PRs, and a polkit rule lets that user start it and do nothing else. That user's profile
sets `LAND_HANDOFF_UNIT=claude-land`, so its own `land.sh` starts the unit and reports the
unit's verdict line and exit code as its own (`land_lib/handoff.py`).

**The arm refuses a body whose closing keyword is not its own `Closes #N` line.** GitHub closes
an issue named after `close`/`fixes`/`resolved` however the sentence reads, so PR #2510's "Filed
and not fixed: #2509" closed the unfixed follow-up two seconds after the merge (issue #2513).
The refusal prints the offending line: `gh pr edit <n> --body-file` with the reference reworded
(`Filed for later: #N`), then re-run the same landing command. Expect to be rewording a
body another session wrote, since the fan-out agent opens the PR and a daniel-box session lands
it.

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
`until MERGED` loop. A PR still open after 45 minutes exits `LAND_GAVE_UP`: it is not being merged, and the
reason is on the PR.

**A conflicting PR ends the wait at once**, exit `LAND_FAILED` with `VERDICT: merge-conflict`, rather than
sitting out the 45 minutes. A PR that goes conflicting during the wait never merges, and
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

**A PR that hands a tag back from Flux is refused before the merge while Flux still owns the
tag.** Before the direct merge, `land_lib/merge.py` compares each tag's `owner` at the PR's
merge base and at its head. For each tag that the PR makes Ansible-owned, it reads
`kubectl get kustomizations.kustomize.toolkit.fluxcd.io -n flux-system <tag>`. It merges only
on `NotFound`. A Kustomization that exists, or a read that fails, ends the landing with
`refused` and nothing merged. The order of the two PRs that hand a service back is in
[docs/flux-deploy-plane.md](flux-deploy-plane.md#handing-a-service-back). A merge made outside
`land.sh` bypasses this check.

## The CI wait

**Do not hand-poll CI and do not hand-merge.** `await_ci.py` reads the same check-runs endpoint
the deployer reads, so its verdict and the tick's agree by construction. Hand-polling cost 835
polls across 213 wait episodes before it existed.

`cancelled`, `stale` and `skipped_by_concurrency` mean *no verdict for this SHA*, never *this
SHA is bad* — `_CI_NO_VERDICT_CONCLUSIONS` in `deploy_git.py` is the list, and a commit whose
merge was immediately followed by another reads `cancelled` permanently. `await_ci.py` follows
the tip in that case, but only once your commit is an ancestor of it. The log line is `<sha> has
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

- No commit from the merge commit to the tip has a CI run, which includes the merge commit
  being the tip. To run CI on the tip, run `gh workflow run ci.yml --ref master`, then re-run
  land.sh. The fallback is to deploy the tip by hand without CI, with
  `./scripts/deploy.sh --tags <service> --at <tip>`.
- The nearest descendant is red and a later one is green. While a later one's CI is still
  running, the wait keeps polling for it rather than ending red. Neither verdict changes, and the
  tick has already fast-forwarded past your merge commit. To deploy your change, run the
  `deploy.sh --at <tip>` command the line prints. It deploys the tip, because `deploy.sh`
  refuses any tree behind `origin/master` with `DEPLOY_STALE`.
- The nearest descendant is red, and GitHub never ran CI for the tip after it. The wait keeps
  polling until its deadline, since a freshly merged tip has no CI for its first seconds
  either. To give the tip a verdict, run `gh workflow run ci.yml --ref master`, then re-run
  land.sh. A green tip then ends the landing on the shape above.

**It waits on your merge commit rather than on the tip**, because the tick fast-forwards to the
newest GREEN commit in its range rather than only to a green tip. A later merge whose CI is
still running no longer holds your PR. Waiting on the tip is what `tip-outran-retries` measured:
six landings in the 14 days to 2026-09-11 spent 400-614s each chasing a tip that moved again.

## The deploy, and the tick beside it

**A PR reaching service tags and nothing the tick applies itself deploys its own merge commit;
the tick is kicked afterwards, not awaited.** `deploy.sh --at <sha>` renders a snapshot of that
commit, so nothing needs the primary checkout fast-forwarded first. Step 4 waits for nothing,
the deploy runs, and only then does `land.sh` start the tick with `--no-wait` — the checkout
converges while the health gate runs, and the deployer's own 10-minute timer covers a kick that
failed. A kick that finds a tick already in flight starts nothing, so `land.sh` asks again after
the health gate. `kick=` on the Landings board says which happened: `started`, `rearmed` (the
second request started one), `joined` (both joined; the timer converges the checkout, this
landing did not), `failed`.

Kicking it BEFORE the deploy would not save the time: `gitops-deploy.service` holds the git-tree
lock for its whole unit run, so `deploy.sh` would queue behind it inside its own `flock` and the
seconds would move from `tick=` to `lock=`. `tick=0` on the board means step 4 awaited no tick.
A `lock=` wait beside it is one of two other waits: a tick the deployer's TIMER started, or
another deploy of the same service holding `server-deploy-<tag>.lock`. The second is the common
case, because the tree lock is held for the snapshot alone. `holder=` tells them apart — the
tree lock's wrapper line names its holder, the per-service line names none. Eleven landings in
the 14 days to 2026-09-11 spent the full 540s in step 4 watching a deployer busy with another
session's apply.

`tick=0` is about step 4 only. When the deploy exits 4 the landing falls back to the tick and
the primary checkout, and that tick IS awaited, so a fallback landing reads non-zero there.

**Two shapes keep waiting for the tick**, because there the tick is the apply and the verdict
reads the deployer's own markers straight afterwards. A PR with **no** service tag is one. A PR
reaching service tags **and** something the tick applies itself — the deploy plane, or a setup
role `initial_setup.yml` includes — is the other: it awaits the tick in step 4 and then deploys
from the primary checkout with no `--at`. Taking the fast path there would grade a tick that had
not run, and print `needs-manual-apply` (or `deferred`) for work the kicked tick applies a
minute later. `--tags` does not opt out: the derivation is skipped, the classification is not.

A change **no** tick can apply — the `plane` a hand must run, which `needs-manual-apply` names —
does not push a landing off the fast path. Nothing about awaiting a tick would settle it.

**A Pi role deploys on the Pi.** `land.sh` runs one `deploy.sh` per host that declares a derived
tag, read from `deploy_tags.py hosts`, and adds `-e target=daniel-pi` for the Pi's. The health
verdict probes a tag only the Pi declares with `--docker` alone. Until 2026-09-03 both halves
ran against the local node: PR #928 printed `settled` with the CLUSTER Alloy DaemonSet's 2/2
ready while the Pi ran the old container, because the play matched no service on daniel-box and
the gate guessed a same-named cluster workload (issue #929).

**It scopes the deploy to the PR's own file list rather than a SHA range**, so another session's
merged work is not swept in. `gh` paginates that list at 100 files, so it falls back to
`--changed <since>` when the count disagrees — the only reason `--since` exists. That fallback
refuses a range wholesale as soon as one path is broad, so since #2520 it then asks
`deploy_tags.py narrow` the same range: `narrow` maps a broad path to the services whose render
it reaches, and a list it returns is deployed rather than reported as broad. Only where `narrow`
refuses too does the landing grade from the deployer's markers — and it refuses on a tag list
covering more than half the fleet, so a `group_vars` key many roles read is graded from the
markers rather than deployed. `--tags` overrides the scope entirely.

### A tag Flux applies waits on promotion

**A tag whose `containers_list` entry sets `owner` never reaches `deploy.sh`.** Step 5 reads
ownership at the merge commit (`land_lib/promotion.py:split_owned`) and takes every such tag out
of the list before `deploy_by_host` sees it. `deploy.sh` runs only for the Ansible-owned tags
that remain. A PR whose only tag is flux-owned runs no `deploy.sh` at all. A deploy-plane PR
that derives no tag, such as an inventory edit setting `owner: flux`, finds its tags in the
deployer's narrowing of the PR's own range (`land_lib/promotion.py:owned_by_narrowing`) and
waits on promotion for the ones that are not Ansible-owned. The gate then renders
the merge commit from a snapshot, and `land.sh` kicks the tick to converge the primary checkout.

**A PR that changes a cluster file waits on promotion too.** A change to
`ansible/vars/cluster-vars.sops.env` or `ansible/vars/cluster-vars.source` touches no role,
so no tag derives from it. `land.sh` passes `--also cluster-vars` to the wait, which adds the
`cluster-vars` Kustomization and every Kustomization that depends on it, read from the live
`dependsOn` graph. A `.source`-only change moves no digest, so the changed set alone would
not see it. A rotation PR that also changes `secrets.yml` waits first, then ends
`needs-manual-apply` naming the Ansible half. The rule is
`scripts/deploy_tools/land_lib/land_tags.py:CLUSTER_VARS_PATHS`, and it retires when `land.sh`
reads its changed set from `digests.json` (slice D6).

For those tags `land.sh` runs `promote.py land-wait <merge commit>`. The wait never follows one
`promote` run, because GitHub may evict a queued run and a later run may promote a newer commit.
Instead it reads the cluster every 30 seconds:

- **The changed set.** It waits on `homelab` and every Kustomization whose `footprint` differs
  between the merge commit's `digests.json` and the one on the newest published ancestor of its
  first parent (`scripts/deploy_tools/changed_set.py`). A file that is missing or unreadable
  widens the wait to every Kustomization, and the progress line says so. `sha-<merge commit>`
  is usually not published when the wait starts, so the wait widens until it is.
- **`settled`.** Every expected Kustomization is Ready, and its
  `status.lastAppliedOriginRevision` names the merge commit or a commit that descends from it.
  `land-wait` then writes one `deploy-annotation` line with `source=promote`, and the landing
  goes on to the health gate for each `owner: flux` tag.
- **`deploy-failed`, cause `promote-held`.** `DEPLOY_HOLD` holds a value other than `none`. The
  line names the held SHA and, for a `failed=` hold, the failed names. Clearing the hold is
  **operator-only**. `promote`'s `settle` job writes a `failed=` hold when a promotion fails
  its verification (`docs/flux-deploy-plane.md`, *Verification*).
- **`deferred`, cause `promote-timeout`, exit 75.** No promotion covered the merge commit
  within twice the promote timeout (67 minutes,
  `scripts/deploy_tools/promote_lib/wait.py:LAND_WAIT_TIMEOUT_S`). Look first at the
  `promote` workflow's failed `publish` jobs: a publish refusal leaves no `sha-<commit>` tag
  to promote. The next promotion may still cover the merge commit.
  The 67 minutes count again from each poll that reads a verification of an earlier commit
  in flight, for up to `wait.py:IN_FLIGHT_WAIT_S`. M's `decide` waits on that verification
  before it promotes M, so two merges minutes apart would otherwise give up on a promotion
  that passes later (#5164).
  A lander that is killed at a fixed time sets `LAND_DEADLINE`, the seconds after `land.sh`
  starts by which the landing must print its verdict. The wait then gives up 10 minutes
  before that deadline, whatever is in flight, and the landing still ends `deferred` with a
  ledger line (#5199). `claude-land@.service` sets 5400 and `renovate-agent-land@.service`
  sets 3300.

Two holds coexist until slice D6 retires the deployer, and `probe.py landing` prints both on
every run, each naming its source and the tags it holds:

```text
hold_sha (GitOps deployer, Ansible-owned tags): none
DEPLOY_HOLD (promote workflow, Flux-owned tags): 0123abcd... failed=bento-pdf
prod: 3cf44936 sha256:4e55da43005e   last-good: 9f1e2d3c sha256:0b7a51c2e9d4
```

`hold_sha` stops the deployer and `land.sh`'s Ansible-owned tags. `DEPLOY_HOLD` stops every
promotion, so it ends `land-wait` for a flux-owned tag and leaves an Ansible-owned landing
alone.

`land-wait` reads the Kustomizations with the read-only `kubectl`, ancestry with
`git merge-base --is-ancestor` in the primary checkout, and `DEPLOY_HOLD` with
`gh variable get`. The last is one authenticated GitHub call per poll. A read that fails keeps
the wait going, and an unreadable Kustomization list never reads as `settled`.

`land.sh` runs from the primary checkout wherever you invoke it. The deploy no longer depends on
that — `--at <sha>` renders the merge commit from anywhere — but three reads still ask about the
primary: `deploy_tags.py hosts`, the diff-derived tag list a truncated file list falls back to,
and the deploy the exit-4 fallback runs.

### A PR that spans both planes is refused before it merges

Until slice D5 ends, nothing orders an edge between a flux-owned role and an Ansible-owned one.
`land.sh` applies the Ansible half after the merge, and `promote` applies the Flux half after
CI. A PR that changes both halves lands them in whichever order the two pipelines finish.
CI's **Cross-plane order gate** step, in the `hooks` job on pull requests only, refuses such a
PR. It runs `scripts/validate/cross_plane_order.py --base HEAD^1`, which renders the cluster
at the base and at the merge commit with `homelab-render cluster`. Each side renders with its
own tree's renderer. The gate refuses two shapes:

- **Arm 1, plane order.** Two changed roles sit on different planes, and one depends on the
  other, directly or through other roles, by `build_k8s_dep_map`. netpol-baseline depends on
  every workload (`K8S_APPLIES_LAST`). A role counts as changed when its `cluster/<tag>/`
  output moved, or when its `tasks/` changed and Ansible owns it at the head. A flux-owned
  role's tasks run nowhere, so only the render they feed counts. A role whose own diff is
  only an image pin does not count. A PR none of whose paths reaches an Ansible apply counts its
  flux-owned roles alone, because Ansible applies its own templates and never `homelab-render`
  output. A doc or a test reaches no apply. Any other path under `ansible/` does, and so do
  `scripts/lib/`, the `scripts/` files a task names through `playbook_dir`, and the root files
  that configure every apply: `ansible.cfg`, `pyproject.toml`, `uv.lock`, `uv.toml` and
  `.python-version`. An inventory field that another role reads counts through the render it moves:
  `auth_tier` on bento-pdf moves authelia's access rules alone, while `hostname` moves both.
- **Arm 2, edge first.** A tag that is flux-owned at the base and at the head gains a
  `dependsOn` edge, and its own render moves in the same PR. A tag that becomes flux-owned in
  the PR is not judged, because its Kustomization is born with its edges.

**To split an arm-1 refusal, land the dependency's half first.** Merge the dependent's half
only after the first PR's `land.sh` verdict reads `settled`. The same rule covers two PRs that
merge back to back, which the gate cannot see because each passes alone. **To split an arm-2
refusal, land the edge alone first**, then the change.

**A shared-template change exits by copy and migrate.** A macro under `ansible/templates/`
moves the render of every role that imports it, on both planes. bento-pdf and authelia both
import `workload-shell.yml.j2`, so the gate refuses most edits to it. Split the change in
three PRs:

1. Add the changed macro under a new name, and move the dependency's plane to it. Land that
   PR until `settled`.
2. Move the dependent's plane to the new macro.
3. Delete the old macro, which moves no render.

When a dependency set crosses the planes more than once, step 1 repeats once per plane, in
dependency order. Each refused macro change therefore costs about three PRs until D5 ends.

**The measured cost is two refusals in 30 days.** `scripts/dev/cross_plane_replay.py`
replayed the gate over the 1598 PRs merged in the 30 days to 2026-10-16, with bento-pdf the only
flux-owned tag. #1975 and #2002 changed the `ingressroute()` macro, which moves bento-pdf's
route beside traefik's or authelia's own change. The replay refused #1975, and #2002 was
judged by reading its diff.

The first version of the gate also counted a flux-owned role's `tasks/`, and refused three
more: #4291, #4292 and #4299, each a mechanical edit across many roles that included
bento-pdf's `tasks/`. That made about 1.2 refusals a week, over the plan's one-a-week bar. The
operator ruled on 2026-10-16 that a `tasks/` change counts only for an Ansible-owned role (D4
Open question 9), and those three no longer count.

The replay could not render 55 of the 645 commits that changed a render input. 24 of them
change a file bento-pdf's render reads, and so do the three above, so the true count can be
higher than two.

**An edit that one inventory field carries exits by handing the role back.** A `hostname` edit on bento-pdf
moves bento-pdf's route and authelia's access rules, and no macro copy splits it. Hand bento-pdf
back to Ansible (`docs/flux-deploy-plane.md`), make the change while the play orders both
roles, then flip bento-pdf again. While bento-pdf is handed back, its flux-owned dependents lose
their edge to it, and the second flip rolls the workload once more.

The gate has no bypass, because a bypass would become the default route. It also passes an
image bump that does need ordering. A render that fails at either end exits 2, and the step
fails without a verdict. Slice D6 deletes the gate once every role is flux-owned.

## The stale-tree retry

**If a PR that reaches YOUR OWN TAGS merges while this one is landing, the first `deploy.sh`
exits 4.** Under `--at` that is the staleness gate saying a commit in
`<your sha>..origin/master` renders something your tags render. That newer landing owns the
service, so this one falls back to the path it had before `--at` existed and retries, up to
three times: each pass sleeps a backoff that DOUBLES (60s, 120s, 240s), re-runs the blockers
check, waits for master CI on YOUR OWN merge commit, then ticks — with the wait this time — and
deploys from the primary checkout. That wait is booked under `wait_ci` and normally returns at
once, because step 3 already waited on the same SHA.

A tail that touches nothing your tags render no longer refuses the deploy at all: the gate is
scoped to the paths the requested tags reach.

**Exhausting those retries prints `tip-outran-retries` (`LAND_GAVE_UP`), not `deploy-failed`.** Every
attempt lost the same race: master merged faster than one tick-and-deploy cycle. Nothing was
deployed and re-running is safe — the opposite of what `deploy-failed` reads as, and a session
that did not read the log took it for a fault in its own change. PR #1460 ended that way twice
on 2026-09-09 with four other sessions holding worktrees; its checkout went from 7 to 9 commits
behind DURING the landing. The doubling backoff is the other half: three attempts at a fixed 60s
all fall inside ~4 minutes, against a measured merge rate of roughly one every 2 minutes, so
they could not converge (issue #1466). A static `behind_since` SHA distinguishes a parked
deployer from this; a SHA that advances under an unchanged timestamp is this.

## Reading a `deploy-failed`

**One variant means the opposite of the rest.** `a playbook task failed AFTER applying; some
changes are live` is `deploy.sh` exit 20 (`DEPLOY_PLAYBOOK_FAILED`): the play reached its tasks and one failed, so
everything applied before it took effect. Every other `deploy-failed` line means nothing was
deployed and re-running is safe; this one is not a resume point. It exists because `deploy.sh`
returned ansible-playbook's own status until 2026-09-02, and ansible exits 2 on a failed host —
the same number as the tag miss, which is how a run whose manifests both applied was reported as
`a derived tag matched no service, so nothing deployed` (issue #840).

**Another names `deploy_tags.py hosts` itself**: `deploy_tags.py hosts failed before any
deploy.sh ran; nothing was touched`. `land_lib/deploy.py:deploy_by_host` exits `LAND_FAILED` and
records the cause `host-lookup`. `deploy.sh`'s own rare `cd $repo_root || exit 1` records
`deploy-exit-1` instead. Before the causes existed, both returned a bare exit 1, and only one of
them had run a deploy (issue #1016). Unlike `DEPLOY_PLAYBOOK_FAILED`, `host-lookup` means what
every other `deploy-failed` means: nothing was deployed, and re-running is safe.

## The health gate

The health gate renders from a detached worktree of the same commit. `probe.py health <tag>`
enumerates the workloads to check by rendering the role's manifests from the checkout it runs
in, so gating a brand-new role from a primary that has not pulled it yet enumerates nothing and
reads `skipped` — green, on the landing that most needs a gate. That worktree takes no lock. If
it cannot be made at all, the landing reports `unhealthy` rather than gating the primary — the
one exception being a primary that already contains the commit.

A failed gate is asked once more whether a later deploy re-rolled the workload. The landing's
deploy releases the service lock before the gate runs, so another landing or a tick can deploy
the same service under the gate's one sample. A `Recreate` workload then reads `0/1 ready`
mid-roll (#3812). `land.sh` counts a tag as re-rolled on one of two signals, and neither counts
on its own (#4514). A held service lock (or `all`) counts only when one of the tag's workloads
changed `metadata.generation` while the lock was held. A release record stamped after step 5
ended counts only when it queued a restart or names manifests that differ from the record
before it. A deploy that rolled nothing leaves the failed gate standing. On a re-roll, `land.sh`
waits for that deploy to finish and gates again. The verdict line then reads `re-gated after a later deploy of <tags>`, and a change
that is still broken fails the second gate as well. A healthy second gate settles only when the
commit in that deploy's release record contains the merge commit. The tick's rollback
redeploys an older pin, and its healthy pods say nothing about this change.

**Verify the change, not just the workload.** The `VERDICT:` line gates the rollout and the 180s
restart window. It cannot see whether *your change* took effect: an Authelia 302 fires in the
middleware before the backend is reached, and 19 dead Grafana panels sat behind a 1/1 pod.

## `deferred`, `needs-manual-apply`, and what a hand still has to run

`deferred` (`LAND_GAVE_UP`) means the tick applies this PR itself — a setup role `initial_setup.yml`
includes, or the deploy plane — and has not crossed this PR's merge commit yet, almost always
because the CI of a newer merge is still running. The next tick does it. `land.sh` reads that from
the deployer's `behind_since` and `hold_sha` markers, and checks the merge commit against the
primary checkout before believing `behind_since`, because the tick lands at the newest green
ancestor and so sits behind the TIP on most ticks while working normally (issue #1786). A held
`hold_sha` is `deploy-failed`. If the verdict says the run stopped watching a tick that was
still applying, the markers were read mid-apply and a hold cannot be ruled out (issue #1607):
re-run, because no later tick crosses a hold.

`cc-wait land` tells the two apart for its caller (#3932). A plain `deferred` ends the wait as
state `deferred`, exit 4. A `deferred` that abandoned a mid-apply tick ends it as `gave-up`,
exit 3, the same as every other `LAND_GAVE_UP` that asks for a re-run of `land.sh`.

**Converging is not applying.** `behind_since` empty says local == origin, which any session's
`git merge --ff-only` produces too — and once it holds, `next_action()` returns `noop` for every
later tick, so a plane the tick never applied is stranded permanently. PR #1529 read `settled`
that way on 2026-09-10 while `/opt/renovate-agent/renovate_agent.py` was four days stale (issue
#1537). A self-applied landing also requires the receipt of the tick that crossed the PR's merge
commit to record an applied plane. `deploy_handlers.handle_broad` writes that half of the
receipt only after the apply returned. With no such receipt, the verdict is
`needs-manual-apply`. That includes a range another session's `git merge --ff-only` crossed,
which no tick ever applied (#3391).

**Read every remediation line, not the first one.** One PR can reach two of these at once — a
plane a hand applies, and a self-applied setup role the tick installed on its own host alone —
and the verdict then prints one line per half. PR #2568 carried exactly that pairing, and until
#2569 the plane's line suppressed the other: daniel-server and daniel-pi kept the old
`kuma-push-lib.sh` behind a printed remediation that named neither.

Four things sit in that position:

- **A setup role `initial_setup.yml` does not include** (`k3s` is in `k3s-bringup.yml`; `common`
  is include-only, so the note names each consumer's playbook and tag), or a bring-up playbook.
  The tick applies every other setup role itself, and `deploy.yml` is a `containers_list` loop.
- **A shared k8s role that no declared role runs** has no tag at all and still needs a full
  `ansible/deploy.yml`. Every other shared k8s role is deployed rather than reported (#2704):
  `manifests`, `volume-snapshot`, `image-builder`, `arr-notification` and the
  rest have no `containers_list` entry, but `deploy.yml` runs each under the tag of every role
  that includes it, so the landing adds the tags of every caller, followed through callers that
  are themselves shared. A `tasks/` change to `volume-snapshot` reaches about 58
  services. A shared-role change reaching no rendered manifest fans out to nothing.
  `deploy.sh --tags <shared role>` performs the same expansion by hand, for every caller, even
  where the landing narrows (#2717).
  **`manifests` is the one exception, and only for a change confined to `tasks/` or
  `handlers/`** (#3124). That role acts through the bytes it renders, so such a change moves no
  live object: the landing deploys ONE caller as a smoke test — the cheapest render target, as
  `shared_role_callers.smoke_caller` derives it — and the other 56 take the new task logic on
  their own next deploy. PR #3117 was the measured case: 57 tags, about 20 minutes, and 0 of 60
  release records showed the restart it was changing. The `# DECIDED:` at `smoke_caller` holds
  the ruling and the gap it accepts, and `deploy_k8s_owed.discharge_k8s_unapplied` still reads
  every caller, because its question is whether the change is applied rather than whether it
  runs.
  **A change confined to `manifests`' standalone-guard map deploys only the rows that
  moved** (#4932). The guard task reads one row of `files/standalone_secret_keys.json`,
  the row for its own service. `land_shared.guard_map_rows` therefore diffs the map over
  the PR's range, and the landing deploys only the callers whose row differs. It keeps
  every caller when the map is added or removed, or fails to parse on either side. It also
  keeps every caller when the range does not cover the map, or when another `manifests`
  path changed beside it.
- **A rotated secret** has no path to match: a secret's value lives in no role's template, so
  `ansible/vars/secrets.yml` derives zero tags however many roles consume it. Run
  `uv run python scripts/secrets_mgmt/secret_rotation.py consumers <secret>` for who holds a
  stale copy and the repair command per plane.
- **A self-applied setup role reaching a host beyond the tick's own** (issue #1009).
  `initial_setup.yml`'s `hosts:` is one target per run, and the tick runs it on whichever host
  `land.sh` executes on — so a role with no `when:` gate (`initial_setup` itself, plus
  `config_files`, `sops_setup`, `docker_install`, `hypervisor`) reaches all three hosts, and a
  role's own `when:` can reach more than one (`nut_host`: daniel-box and daniel-server). The
  tick converging says only that the LOCAL host is current; PR #1002 changed the shared Kuma
  push library, the tick converged on daniel-box, and `land.sh` read `settled` while
  daniel-server and daniel-pi kept the old library for three days. The line names each remaining
  host and its exact apply command. **This is not the failure PR #723 hit**: #723's self-applied
  roles (`gitops_deploy`, `renovate_notify`) are gated true only on daniel-box, so a role
  reaching just the tick's own host still reads `settled` — reporting every self-applied role as
  unfinished was tried and reverted for exactly that PR (`plane_note`'s docstring in
  `land_tags.py`).

**A setup role the deployer cannot apply also needs its marker cleared.** For `k3s` and `common`
the tick fast-forwards the range and records a `manual_plane` line in the `owed` ledger. Run the
printed apply and clear commands as printed, including any `--applied <tags>`. A bring-up
playbook gets no such line, because the tick parks on those and writes no marker. A landing
behind a recorded role reads `settled`, because the tree converged. The clear, `--applied` and
the six-hour page are in
[A role only a hand can apply is recorded, not parked](gitops-pipeline.md#a-role-only-a-hand-can-apply-is-recorded-not-parked).

## `nothing-to-deploy`

Decided from the PR's file list right after the merge, before any CI wait: a PR that reaches no
service tag, no plane a hand applies and nothing the tick applies itself has nothing to wait on,
and the deployer's own tick fast-forwards it. The one exception is a file list GitHub truncated,
which is derived from the diff after the tick as before.

**A document under a role is not a manual apply.** A `.md` — a role's `CLAUDE.md`, a
`README.md`, one under `files/` — maps to no role and to no plane, because no playbook applies
prose. Landing PR #1696 ended `needs-manual-apply` for `ansible/roles/k8s/manifests/`, whose
only changed file was that role's CLAUDE.md, and asked for a full `ansible/deploy.yml` (issue
#1701). `land_tags.role_for` now agrees with the deployer's own mapper, and
`land_tags.quiet_paths` drops a `.md` under the setup plane before any diff is read.

**A role the PR itself registers is not one either.** Which tags exist is read at the MERGE
COMMIT (`land_tags.service_tags_at`), not from a checkout — a `containers_list` entry added by
the same PR is absent from every tree until the tick fast-forwards, and `land.sh` used to report
the new role as unregistered and ask for a full `ansible/deploy.yml`. PR #1539 landed that way
and `./scripts/deploy.sh --tags pihole-exporter` deployed it a minute later (issue #1544).

## The annotation, and the Landings board

Every run writes one logfmt line to syslog on exit (`logger -t landing-annotation`): the PR, the
merge SHA, the verdict, and seconds spent in each phase — `wait_merge`, `wait_ci`, `tick`,
`deploy`, `total` — plus `lock`, the seconds spent waiting on a deploy lock (a sub-part of
`tick` and `deploy`, not a fifth phase), and `holder`, the command that held it.

`lock` books two kinds of wait: an attempt that LOST the lock and exited 75, and a wait a
wrapper rode out and then reported itself — `deploy.sh` queuing in a timed `flock(2)`
(`deploy_lib/under_locks.py:LOCK_WAIT`) for the tree lock or one of its per-service locks,
`gitops_tick.sh` watching a tick another actor had already started. Both exit 0, so before they
reported it the seconds landed in `deploy` and `tick` and every row read `lock=0`. Since
[ADR-0017](adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md) most of what a landing
waits for is a per-service lock: `deploy.sh` holds the tree lock only for a snapshot of `HEAD`. A
run that queued on several service locks prints one line each and `lock` is their sum.

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
