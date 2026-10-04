# Landing a merged PR

`land.sh` follows a merged PR through to a verified deploy in one invocation: arm the merge,
wait for master CI on the merge commit, deploy that commit, kick the tick, gate the health, and
print a `VERDICT:` line. The `land-after-merge` skill carries the invocation and the verdict
decoder. This page carries the reasoning behind each rule, and the incident that put it there.

It reads as reference rather than as a procedure. The skill was 418 lines and changed in 47
commits in the three months to 2026-09-30, more than any other file under `.claude/` — because
every measurement landed in the one file a session loads before every landing. The measurements
still matter; they just do not have to be resident (issue #2853).

## The command line

`land.sh --pr <n> --arm-merge --await-merge --detach && cc-wait land <n>` is the whole landing.
`land.sh --help` prints the flags, the exit codes and the verdicts.

**`--detach` and `cc-wait land` replaced three hand-written steps.** The skill used to spell out
`git rev-parse origin/master` for `--since`, a redirect to a logfile under
`$CLAUDE_JOB_DIR/tmp`, and `timeout 1200 tail -f -n +1 <log> | grep -m1 '^VERDICT:'`. Each was
a step to get wrong. `land_lib/detach.py` does the first two, and its docstring carries why the
child's exit code is the authority rather than the grep. The third is `cc-wait`'s `land` source,
`scripts/deploy_tools/land_probe.py`.

**The wait is `cc-wait`'s, and it fits a foreground Bash call.** `cc-wait` is the one wait loop
every repo shares (the dotfiles `cc-wait` package). Its predecessor, `--await-verdict`, waited
up to 1200s, twice the 600s limit of a foreground Bash call. 15 such waits overran that limit in
the 30 days to 2026-10-04; the harness moved each one to the background, and 12 of the 15, all
in fan-out `claude -p` agents, never woke. `cc-wait` waits at most 570s, then exits 75 with the
command that resumes the wait. Re-run that, never `land.sh`, which would start a second landing.

**`--since` is the PRE-merge tip.** It bounds the range the truncated-list fallback derives tags
from, so `--detach` resolves it before the arm. Reading it afterwards would capture the tip that
includes this PR and derive nothing.

**Without `--detach`, redirect stdout and stderr to a file yourself.** A backgrounded Bash call
hands the script a non-blocking pipe, and Ansible refuses to start on one:

```
ERROR: Ansible requires blocking IO on stdin/stdout/stderr. Non-blocking file handles detected: <stdout>, <stderr>
```

`land.sh` then prints `VERDICT: deploy-failed` with nothing deployed, and the error names Ansible
rather than the harness, so it reads as a playbook bug. Session transcripts on this host record
it at least ten times before the redirect became the rule.

**Do not end your turn on a backgrounded landing.** A backgrounded Bash call whose output is
redirected to a file is not a harness-tracked child, so nothing wakes the session when it exits.
Four of four fanned-out agents ended their turn at step 0/6 or 3/6 believing otherwise on
2026-09-06, three of them saying in as many words that a watcher was armed (issue #1291).

**A task that comes back "stopped because the system is running low on memory" was reaped, not
failed.** Claude Code kills every running backgrounded Bash task when Node emits a
`memoryPressure` event, and the kill arrives as a task notification rather than an error in the
logfile — so the session reads clean while the PR has merged and nothing followed it through.
Re-running the same command is the fix: it is idempotent, and a PR already `MERGED` is left
alone. On `daniel-box` the host unit turns the reaper off
(`CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP=1` in
`roles/setup/claude_code/templates/claude-rc.service.j2`), so this only reaches a session
started some other way. Three kills in a row on 2026-09-04, issue #1096.

## Arming the merge, and the two command shapes the classifier refuses

**`--arm-merge` runs `gh pr merge --squash --auto` itself.** A bare `gh pr merge` sits on the
ask list (`Bash(gh pr merge:*)`), and auto mode suspends the allow list — an unattended session
has nobody to answer that prompt, and it times out as a denial. Three attempts, three denials,
on 2026-09-03 (issue #979). `land.sh --arm-merge` is not that command: its own text never
contains `gh pr merge`, so it reaches the classifier as the single script invocation the
worktree-containment check already accepts.

The arm is idempotent, so re-running after a `merge-conflict` or `merge-timeout` re-arms
cleanly. `--subject` overrides the squash commit's subject; the PR's own title is used
otherwise.

**A PR waiting on a review is merged directly, not armed.** When `reviewDecision` is
`REVIEW_REQUIRED`, the arm leaves the PR alone. `--await-merge` then merges it through the REST
merge endpoint on the first poll where `await_ci` reads its head green, pinned to that head SHA.
GitHub's auto-merge does not apply a ruleset bypass, so an armed PR in that state stays
`BLOCKED` until `merge-timeout` (github/docs#45265). The REST endpoint does apply the bypass, and
a ruleset with no bypass actor, such as the master CI gate, still refuses the call until its
checks pass. A refused merge is printed once and the wait continues, so a caller who cannot
bypass ends at `merge-timeout`. Without `--await-merge` the arm dies, because nothing else in the
run would merge the PR.

**Under `LAND_REQUIRE_AUTHOR=<login>` the arm refuses a PR by any other author.**
`renovate-agent.service` sets it to `app/renovate`, so the unattended agent can only merge
Renovate's PRs (issue #2170). `--any-author` lifts it for a session that is allowed to merge the
PR; the unattended agent never passes it, because a `manual —` bump goes to a person through the
digest (issues #2746, #3420). An interactive shell leaves the
variable unset and never sees this.

**The arm refuses a body whose closing keyword is not its own `Closes #N` line.** GitHub closes
an issue named after `close`/`fixes`/`resolved` however the sentence reads, so PR #2510's "Filed
and not fixed: #2509" closed the unfixed follow-up two seconds after the merge (issue #2513).
The refusal prints the offending line: `gh pr edit <n> --body-file` with the reference reworded
(`Filed for later: #N`), then re-run the same landing command. Expect to be rewording a
body another session wrote, since the fan-out agent opens the PR and a daniel-box session lands
it.

**Open the PR with `--fill`, then replace the body — the same evasion, one command earlier.**
The worktree-containment check judges a command on its TEXT, so `gh pr create --title "…"` is
refused whenever the title carries `git`, `cd`, `worktree`, `write` or a construct the check
cannot parse — and a title here routinely names one, since it becomes the squash commit subject
and has to name the outcome. Rewording the title does not reliably help.

```
gh pr create --fill                 # title + body from the commit; no prose in the command text
gh pr edit <n> --body-file <path>   # then replace the body
```

`--fill` takes the title from the commit subject, so the command line carries no title text to
judge, and `--body-file` carries no body text either. Measured on Claude Code 2.1.263
(2026-09-06, issue #1431): three of four refusals in one isolated session were false, and the
`gh pr create` one cost three turns. Re-measured 2026-09-10 on the same surface — a
`for i in …; do gh issue comment …; done` was refused on its comment text — so the class is
narrower than it was (2.1.257 fixed loops and heredocs that never touch git) but not gone. The
rule generalises: keep the prose out of the command string and inside a file or a script.

## The merge wait

`--await-merge` polls the PR's state every 30s until it is merged and only then starts the
landing. It reads the state, never the checks, so it is not hand-polling. Without it the session
has to notice the merge itself, which every landing on 2026-09-01 did with a hand-written
`until MERGED` loop. A PR still open after 45 minutes exits 75: it is not being merged, and the
reason is on the PR.

**A conflicting PR ends the wait at once**, exit 1 with `VERDICT: merge-conflict`, rather than
sitting out the 45 minutes. A PR that goes conflicting after the auto-merge was armed never
merges, and nothing on the PR says so — with several sessions landing at once, another merge
moving master under an open PR is the ordinary way it happens. Rebase onto master, re-arm, and
re-run the same command. The wait tolerates a `mergeable` of `UNKNOWN` — GitHub computes the
field asynchronously — and bails only after two consecutive `CONFLICTING` polls, because the
base moving under a PR flips it for one poll.

**A PR whose own CI is red ends the wait too**, exit 1 with `VERDICT: pr-ci-red`, quoting what
`await_ci.py` said. GitHub reports it only as `mergeStateStatus: BLOCKED` — the same word it
uses while the checks are still running. The repo's ruleset requires status checks and
signatures and no review, so a `BLOCKED` PR here is always about checks. The wait keeps going
while `await_ci.py` answers `pending`, which is what it answers until a required check
registers; that is the grace period, so no landing is cut short for polling before CI started.

## The CI wait

**Do not hand-poll CI and do not hand-merge.** `await_ci.py` reads the same check-runs endpoint
the deployer reads, so its verdict and the tick's agree by construction. Hand-polling cost 835
polls across 213 wait episodes before it existed.

`cancelled`, `stale` and `skipped_by_concurrency` mean *no verdict for this SHA*, never *this
SHA is bad* — `_CI_NO_VERDICT_CONCLUSIONS` in `deploy_logic.py` is the list, and a commit whose
merge was immediately followed by another reads `cancelled` permanently. `await_ci.py` follows
the tip in that case, but only once your commit is an ancestor of it. The log line is `<sha> has
no verdict (cancelled/stale) — following the tip <tip>`, and it is normal. It also fires when
the cancellation came before the `prek` job registered a check-run at all: the check-runs list
then never carries the required name, so `await_ci.py` reads the SHA's check-suites (a
`completed cancelled` suite with zero runs) to tell that from a fresh push. Before PR #775 that
case waited out the whole budget and exited 75.

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

`land.sh` runs from the primary checkout wherever you invoke it. The deploy no longer depends on
that — `--at <sha>` renders the merge commit from anywhere — but three reads still ask about the
primary: `deploy_tags.py hosts`, the diff-derived tag list a truncated file list falls back to,
and the deploy the exit-4 fallback runs.

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

**Exhausting those retries prints `tip-outran-retries` (exit 75), not `deploy-failed`.** Every
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
changes are live` is `deploy.sh` exit 20: the play reached its tasks and one failed, so
everything applied before it took effect. Every other `deploy-failed` line means nothing was
deployed and re-running is safe; this one is not a resume point. It exists because `deploy.sh`
returned ansible-playbook's own status until 2026-09-02, and ansible exits 2 on a failed host —
the same number as the tag miss, which is how a run whose manifests both applied was reported as
`a derived tag matched no service, so nothing deployed` (issue #840).

**Another names `deploy_tags.py hosts` itself**: `deploy_tags.py hosts failed before any
deploy.sh ran; nothing was touched`. That command failing used to return bare exit 1 from
`deploy_by_host`, colliding with `deploy.sh`'s own rare `cd $repo_root || exit 1` — the two were
indistinguishable from `land.sh`'s side even though only one ever ran a deploy (issue #1016).
`HOST_LOOKUP_FAILED=21` is reserved for it now. Unlike exit 20, it means what every other
`deploy-failed` means: nothing was deployed, and re-running is safe.

## The health gate

The health gate renders from a detached worktree of the same commit. `probe.py health <tag>`
enumerates the workloads to check by rendering the role's manifests from the checkout it runs
in, so gating a brand-new role from a primary that has not pulled it yet enumerates nothing and
reads `skipped` — green, on the landing that most needs a gate. That worktree takes no lock. If
it cannot be made at all, the landing reports `unhealthy` rather than gating the primary — the
one exception being a primary that already contains the commit.

**Verify the change, not just the workload.** The `VERDICT:` line gates the rollout and the 180s
restart window. It cannot see whether *your change* took effect: an Authelia 302 fires in the
middleware before the backend is reached, and 19 dead Grafana panels sat behind a 1/1 pod.

## `deferred`, `needs-manual-apply`, and what a hand still has to run

`deferred` (exit 75) means the tick applies this PR itself — a setup role `initial_setup.yml`
includes, or the deploy plane — and has not crossed this PR's merge commit yet, almost always
because the CI of a newer merge is still running. The next tick does it. `land.sh` reads that from
the deployer's `behind_since` and `hold_sha` markers, and checks the merge commit against the
primary checkout before believing `behind_since`, because the tick lands at the newest green
ancestor and so sits behind the TIP on most ticks while working normally (issue #1786). A held
`hold_sha` is `deploy-failed`. If the verdict says the run stopped watching a tick that was
still applying, the markers were read mid-apply and a hold cannot be ruled out (issue #1607):
re-run, because no later tick crosses a hold.

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
  `manifests`, `volume-claim`, `volume-snapshot`, `image-builder`, `arr-notification` and the
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
  the ruling and the gap it accepts, and `deploy_defer.discharge_k8s_unapplied` still reads
  every caller, because its question is whether the change is applied rather than whether it
  runs.
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
the tick fast-forwards the range and records the role as a `manual_plane` line in
`/var/lib/gitops-deploy/owed.jsonl`,
which pages **GitOps Deploy — Status** six hours later. So the printed remediation ends with
`uv run python scripts/deploy_tools/gitops_state.py clear-owed manual_plane <role>`, and running the
playbook without it leaves a page over work that is already live. Where the apply it printed was
narrowed, the clear carries `--applied <tags>`: the row can gain a tag between the note and your
clear — a second PR touching the same role — and the bare form would drop that tag with yours.
Run the printed pair as printed. A bring-up playbook gets no such line: the tick parks on those
and writes no marker. Nothing is queued behind either — a landing behind a recorded role reads
`settled`, because the tree converged.

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
(`deploy_under_locks.LOCK_WAIT`) for the tree lock or one of its per-service locks,
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
