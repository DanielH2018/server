# Post-merge automation

A session that merges a PR here does not finish the job. It waits for master CI, triggers a
GitOps tick, deploys what the tick deferred, and verifies the result — roughly 60 lines of
hand-executed procedure in `CLAUDE.md`, five to fifteen minutes of foreground blocking, and a
hand `git merge --ff-only` whenever the tick parks a broad change.

This replaces that procedure with one backgrounded command, and teaches the deployer to apply
the broad changes it defers today.

Nothing here removes the CI gate. Master CI is the only check that sees the whole tree, because
PR CI is scoped to changed files, so a whole-tree failure can appear only after the merge.

## What this is worth

| Measure | Value | Source |
|---|---|---|
| Master CI on a merge commit | 260–300s | `gh run list --branch master`, 2026-08-29 — superseded, see below |
| Hand-polls spent waiting | 835 across 213 episodes | measured 2026-08-29 |
| Full `deploy.yml`, forward | 1212s, 1753 tasks ok | `ansible.log` pid 2004861, 2026-08-22 |

**Do not select the run for a merge commit by duration.** A merge SHA fires two workflow runs, `CI`
and `Push on master`, and only `CI` carries the required
`prek (lint + validate + tests + secrets)` check that gates a deploy. Select by workflow name,
never by which run took longest. The 260–300s above is a 2026-08-29 `gh run list` total and is
not the `CI` run the gate waits on. Taking the `CI` run of each merge SHA from `run_started_at`
to `updated_at` across the 15 landings measured in *The landing floor, measured* below gives
77–137s; the per-run durations are in that task's report, not on this page. What accounts for
the rest of the 2026-08-29 figure is not established here, so treat that row as history and
77–137s as the number to use.

## The two problems are separable

**Waiting on CI is session-side.** `next_action` returns `ci_pending` *before* the fast-forward
by design, so a tick fired seconds after a merge pulls nothing and every later step reads as
stale. That ordering is correct and stays. What is removable is the session sitting in the
foreground while it resolves.

That deferral is scoped to the range rather than to the tip: when the tip is pending or
red, `deploy_phases.assess` walks back to the newest commit whose own CI is green and
fast-forwards to that one (`deploy_git.ci_walk_candidates`). An unfinished sweep on a later
merge therefore does not defer an earlier green commit, and a landing waits only on its own
merge commit.

**Hand-merging is one arm of the deployer.** An ordinary k8s manifest change already
fast-forwards on its own — `deploy_handlers.handle_no_services` merges whenever `cs.services` is
empty. Only one arm returns *without* merging: the `_BROAD_MANUAL_PREFIXES` deferral inside
`deploy_handlers.handle_broad`, which parks a bring-up playbook change for a human. Every other
broad change ff-merges first and then applies itself. A docs-only commit sharing a
tick alongside another session's setup-plane change is therefore stranded too. The symptom is
a tick that exits 0, logs nothing, and writes `behind_since`.

### Why the obvious fix is wrong

Making the broad arm fast-forward clears `behind_since`. `DeployerState.record_behind`
recomputes from HEAD in `entrypoint()` after `main()` returns, so a converged tree reads as not-behind. That marker
is the only durable signal that an unapplied setup-plane change exists, and monitor-bridge pages
on its age. The naive fix converges the tree, silences the watchdog, and leaves the host running
a plane it never applied with every monitor green.

Any design that fast-forwards a broad change owes a replacement signal.

## Slice 1 — the session stops waiting

### What already exists

Most of the chain is built. The audit that matters:

| Tool | Owns | Change |
|---|---|---|
| `scripts/deploy.sh` | the lock, tag validation, staleness refusal, `--changed`, `--dry-run` | reuse |
| `scripts/deploy_tools/deploy_detach_notify.py` | backgrounds a deploy, gates on `probe.py health` per tag, posts one Discord verdict | edit |
| `scripts/deploy_tools/gitops_tick.sh` | triggering a tick, joining one in flight, reading `last_run` / `hold_sha` / `behind_since` | reuse |
| `scripts/deploy_tools/deploy_tags.py`, `deploy_staleness.py` | the two deploy refusals, each with its own exit code | reuse |
| `/deploy` skill | the platform split; it already knows the post-merge path skips its questions | reuse |
| `deploy_logic.ci_verdict()` | reading the check-runs for one SHA with the `cancelled`/`stale` semantics | expose |

Exactly one primitive is missing: **nothing can wait for master CI on a given SHA.** `ci_verdict()`
is the right logic and has no caller outside the deployer. The 835 hand-polls are what its absence
costs.

### `scripts/deploy_tools/await_ci.py`

A thin CLI over `deploy_logic.ci_verdict()`. It reads the same endpoint the deployer reads, so the
session's verdict and the tick's agree by construction rather than by convention.

```
await_ci.py <sha> [--timeout 900] [--interval 20]
```

| Exit | Meaning |
|---|---|
| 0 | the SHA is green |
| 1 | the SHA is red |
| 75 | the wait budget elapsed with no verdict (matches `deploy.sh`'s use of 75) |

`cancelled`, `stale` and `skipped_by_concurrency` mean *no verdict for this SHA*, not *this SHA is
bad* — `_CI_NO_VERDICT_CONCLUSIONS` already encodes that. When the polled SHA holds only
no-verdict conclusions and is an ancestor of the current tip, the verdict comes from the tip
instead. Without that fallback a commit whose merge was immediately followed by another polls
forever.

An empty or incomplete check-run list is pending, never green.

It imports `deploy_logic` from `ansible/roles/setup/gitops_deploy/files/` with the repo's own
`sys.path` bootstrap idiom, on the module that needs it. Verify it by running it, not by running
the suite — the suite is exactly the thing that cannot see a cross-directory import break.

### `scripts/deploy_tools/land.py` (invoked as `land.sh`)

The orchestrator: `land.py` is the entry point and `land_lib/` holds one module per phase
(`merge`, `classify`, `ci`, `tick`, `deploy`, `health_verdict`) plus the outcome vocabulary, the
options, the tool boundary and the ledger the Landings board reads. `land.sh` is a two-line
`exec` shim so every command in the skill, the hook's remedy text and the Renovate prompt
stays the same. It is one invocation because the worktree containment check refuses
multi-step chains; it is Python because its whole body is "spawn a helper, branch on the
exit code," and every one of those boundaries is a field on `Tools`, so each verdict has an
executing test.

```
land.sh --pr <n> [--since <pre-merge-sha>] [--tags <a,b>]
```

Sequence:

1. Resolve the merge SHA from the PR.
2. `await_ci.py <merge-sha>`.
3. `gitops_tick.sh` — fetch, CI-gate, ff-merge, deploy what is eligible. A PR reaching service
   tags and nothing the tick applies itself skips this step entirely: step 4 renders the merge
   commit itself, so nothing after it needs the primary checkout to have been fast-forwarded.
   Every other shape waits here, because the tick is the apply for part of its range: a PR with
   no service tag, and a PR reaching tags **and** a self-applied plane (the deploy plane, or a
   setup role `initial_setup.yml` includes) or a setup role reaching a host beyond this one.
   `land_lib/landing.py`'s `tick_is_the_apply` is the predicate, and `--tags` does not opt out
   of it. A change only a hand can apply is not one of these: no tick settles it, so it does
   not cost the fast path. A wait that finds a tick already in flight joins it, and once the
   joined run ends cleanly the wrapper starts a fresh run and grades that one: the joined run
   fetched before the merge, so its markers describe a tree without the PR, and a landing
   graded on them read `deferred` or `needs-manual-apply` for work the next timer tick
   applied (issue #1879).
4. `deploy.sh --tags <derived> --at <merge-sha>` from `/home/ubuntu/server`. `--at` snapshots
   that commit rather than the checkout's HEAD, and scopes the staleness gate and the tag
   check to it. A landing that waited for the tick at step 3 passes no `--at` and deploys the
   primary checkout the tick just fast-forwarded. Exit 4 under `--at` means a later commit
   reaches the same tags: that landing owns the
   service, and this one falls back to waiting for the tick and deploying from the checkout.
   On every other `--at` exit the tick is kicked here with `--no-wait`, once `deploy.sh` has
   returned, to converge the primary checkout. After rather than before, because
   `gitops-deploy.service` holds the git-tree lock for its whole unit run and `deploy.sh`
   would queue behind it to cut its snapshot — the same wait, booked under `lock=` instead of
   `tick=`. A kick that finds a tick already in flight joins it and starts nothing
   (`gitops_tick.sh` exit 4): that run fetched before the merge, so step 5 asks again after
   the health gate, and the landing's `kick=` field records `started`, `rearmed`, `joined`
   or `failed`. Under `joined` nothing the landing did converges the checkout; the deployer's
   timer does.
   Under `--at` the per-host split reads `containers_list` at the merge commit too, so a PR
   that adds a Pi role and its entry together deploys with `-e target=daniel-pi` on its first
   landing rather than after the tick has fast-forwarded the checkout (issue #1839).
   A tag the tick's own apply already deployed on this host is not deployed again. The step
   exists for what the tick deferred, and a deploy-plane PR whose tick ran `ansible/deploy.yml`
   at the merge commit deferred nothing: PR #2092's landing paid a 1203s lock wait for that
   second deploy, then failed its snapshot and read `deploy-failed` about live work (issue
   #2094). `Landing.tick_already_deployed` is the predicate: `broad_applied` must name
   `ansible/deploy.yml` at a commit containing the merge, the primary must carry the merge,
   and the marker's tag slot must cover the tag (empty is the whole play; `narrowed-to-nothing`
   covers none). It applies to the local host's tags only — the tick's apply names no
   `-e target=`, so a Pi-declared tag still deploys — and the health verdict still gates every
   tag.
5. The health verdict, via `deploy_detach_notify.py --no-post`, rendered from a detached
   worktree of the same merge commit: `probe.py health` enumerates the workloads to gate by
   rendering the role's manifests from the checkout it runs in, so a role the merge commit
   ADDS enumerates nothing in a primary that has not pulled it and the gate reads `skipped`.
   That worktree takes no lock, and when it cannot be made the landing does **not** fall back
   to the primary unless the primary already contains the commit — it reports `unhealthy`
   rather than a verdict from a tree that cannot see what was deployed.
6. Print a structured verdict block the session reads on re-invocation.

**It holds no check of its own.** No health logic, no tag validation, no staleness logic — each of
those has an owner above. A check appearing inside `land.sh` is a bug, not a feature.

The measured constraint that forces the single-invocation shape: a backgrounded script invocation
carrying an internal `for` loop and `sleep` is accepted, while the same loop written inline is
refused ("too complex to verify that it stays inside the worktree"). Verified 2026-08-29.

#### Tag derivation

Tags derive from the file list of the PR itself (`gh pr view --json files`), not from a SHA
range. A range covers another session's merged services; the PR cannot.

`gh pr view --json files` paginates at 100 files. `land.sh` asserts the returned count against the
`changedFiles` reported for the PR, and falls back to `deploy.sh --changed <since>` when they
disagree. Shipping a silently narrowed tag list is the failure mode this guards.

`--tags` overrides the derivation outright, for the case where the operator already knows the scope.

#### Two front doors, one rule

`deploy.sh --detach` is the human-at-a-terminal path — the verdict lands on Discord.
`land.sh` is the session path — the verdict returns to the session.

So `land.sh` calls `deploy.sh` **without** `--detach`. Nesting them splits one verdict across two
channels, and the session is re-invoked on a non-verdict.

`deploy_detach_notify.py` gains `--no-post`: it computes the same health verdict and prints it
instead of posting. One implementation of the settled check, two destinations.

#### Parallel sessions

- **A cross-worktree merge lock.** `land.sh` takes a flock on the git common dir before merging,
  the pattern `bin/land` uses in the chezmoi repo. This repo locks deploys
  (`/var/lock/server-git-tree.lock`) but has nothing on the merge, so two sessions can merge into
  a master the other is still moving.
- **Lock acquisitions are sequential, not nested.** `gitops_tick.sh` only starts the systemd unit;
  the unit's `ExecStart` holds `/var/lock/server-git-tree.lock` and `gitops_tick.sh` returns after
  it finishes. `deploy.sh` then acquires cleanly. The 10-minute timer can still slip in between —
  that is exit 75, and `land.sh` retries with backoff rather than failing.
- **Exit codes are resume points.** 75 = lock busy, retry. 4 = tree behind origin, pull again and
  never `--skip-staleness-check`. 2 = the tag matched nothing.

## Slice 2 — broad changes apply themselves

`cs.broad` splits three ways instead of one.

| Class | Paths | Behavior |
|---|---|---|
| Setup, scoped | `ansible/roles/setup/<name>/`, `ansible/requirements.yml` | ff-merge, then `initial_setup.yml --tags <name>` (`collections` for `requirements.yml`) |
| Deploy plane | `ansible/templates/`, `ansible/inventory/`, `ansible/roles/containers/common/`, `ansible/deploy.yml` and its task dirs | ff-merge, then full `ansible/deploy.yml` |
| Never auto-apply | `ansible/roles/setup/gitops_deploy/`, `ansible/bootstrap.yml`, `ansible/k3s-bringup.yml`, `ansible/initial_setup.yml` | today's defer-and-alert, no ff-merge |

The exclusion set is not caution. Applying `roles/setup/gitops_deploy/` runs a playbook whose
handler restarts the unit that is executing the tick — a self-modification defect, not a risk
trade-off. The bring-up playbooks run by hand by construction (`deploy_logic.py:112`).

The setup-plane tag derives from the path: `ansible/roles/setup/<name>/` yields `--tags <name>`.
`broad_remediation` emits a literal `<role>` placeholder (`deploy_logic.py:260`), which
is fine for a human reading an alert and useless for a machine. The derivation is new code, and
the alert text should use it too so the two never disagree.

### This overrides a standing rule

`CLAUDE.md` says a broad change another session wrote is theirs to clear, not yours. Auto-apply
moves that ownership to the deployer, which is coherent — the deployer is not a session and has no
half-finished landing to protect. The rule text is updated alongside the code, not left to
contradict it.

## Slice 3 — budget and the failure signal

### The setup arm fits the budget

Scoped `--tags <role>` runs are small. Forward run plus a rollback re-run both fit a bounded
timeout, mirroring the existing k8s path's `K8S_DEPLOY_TIMEOUT_S` / `K8S_ROLLBACK_TIMEOUT_S` split.

### The deploy-plane arm is not

| Component | Seconds | Source |
|---|---|---|
| Max flock wait | 180 | `gitops-deploy.service.j2` |
| Full `deploy.yml`, forward | 1212 | measured 2026-08-22 |
| Rollback re-run | 1212 | same playbook |
| **Total** | **2604** | against the 2700s ceiling the unit carried on 2026-08-22 |

A 96-second margin is 3.5%. A run four percent slower than measured is SIGTERMed mid-rollback,
which is precisely the failure the hold-before-reset comments throughout `gitops_deploy.py` exist
to prevent.

The unit sets `TimeoutStartSec=60min`, raised on 2026-08-29 to fund the staging gate, and
the same numbers fit under it. The arm stays forward-only regardless: funding a broad rollback
needs a fresh measurement, not a ceiling raised for an unrelated feature. The `# DECIDED:` marker
on `broad_budget_ok` in `deploy_remediation.py` carries that ruling, and
`test_docs_quote_current_values.py` pins the value quoted here to the unit template.

So the deploy-plane arm is **forward-only**. On failure it writes `hold_sha`, leaves the tree
fast-forwarded, and alerts.

It does not reset. Resetting without a redeploy leaves the tree claiming old while live state is
half-new — worse than either end state, and undiagnosable from the repo side. `hold_sha` is what
stops the retry loop, and it does that whether or not the tree was reset.

A larger `TimeoutStartSec` is only half of what funds a rollback. The other half is a fresh
measurement of a full deploy, not the 2026-08-22 figure.

### The signal

A failed auto-apply leaves the tree converged and the plane unapplied — the same hole as a naive
broad fast-forward, reached by a different route. `behind_since` is clear by then and cannot carry
it.

`hold_sha` carries it instead. It already pages through **GitOps Deploy — Status**
(`monitor-bridge/files/checks/gitops.py`, the GitOps check) with no new timer, which is the reuse the existing
behind-origin watchdog design already argues for.

Its message is service-shaped — `checks/service.py` reads `deploy held at %s — revert the offending PR`. A held *plane* needs a variant naming the playbook that failed, because reverting the PR is
not the remediation when the tree is already merged and the playbook is what broke.

## The landing floor, measured

The tick and the deploy have already been cut. What is left at the front of a landing is two
polls, and the ledger says neither is worth shortening. This section records the measurement so
nobody re-derives it.

The two phases are the Landings ledger's own timers, written by `land_lib/ledger.py`:
`wait_merge` runs from `t_start` to `t_merged`, `wait_ci` from `t_merged` to `t_ci`. The
`land-after-merge` skill describes both beside the rest of the board's vocabulary.

**Population.** 15 `verdict=settled` rows cover 2026-09-11 00:00 UTC through
2026-09-16 22:00 UTC. That window opens at PR #1796, the last change to these two phases.

```bash
uv run python scripts/diagnostics/probe.py \
    loki-query '{job="syslog"} |= "event=landing"' --since 7d --limit 500 > /tmp/landing_rows.txt

awk '$1 >= "2026-09-11T00:00" && $1 < "2026-09-16T22:00" && /verdict=settled/' /tmp/landing_rows.txt
```

The `awk` filter is what selects the population; the query only has to fetch a superset of it.
Two knobs decide whether it does, and both must be widened when the query runs later than the
window. `--since` is relative to the run time, so 7d stops reaching 2026-09-11 after
2026-09-18. `--limit` is the one that fails quietly: Loki reads backward, so the query returns
the **newest** N matching lines and says nothing when it truncates, and `--limit` defaults to
100. The 7d pull above matched 194 `event=landing` rows, so 500 was ample; a wider `--since`
needs a limit above the number of landing rows in the whole range, not in the window. A limit
that is too low drops the oldest rows first, which is exactly the window this page pins. Loki's
retention is the outer bound on both.

**Measure CI from the `CI` workflow run, selected by name.** A merge SHA fires two runs, `CI`
and `Push on master`, and only `CI` carries the required
`prek (lint + validate + tests + secrets)` check the gate waits on. `Push on master` is not a
workflow file in `.github/workflows/` — it arrives on the `dynamic` event and shows only in the
Actions API, so searching the tree for it finds nothing and proves nothing. Selecting by longest
duration picks it and inflates every master figure by up to 25s. That inflation *subtracts* from
the overhead past CI, so the error understates the overhead rather than manufacturing it: it can
hide a median that is over the bar, which is the direction that matters here, since the verdict
is to change nothing.

```bash
gh api "repos/DanielH2018/server/actions/runs?head_sha=<full sha>"   # PR head, then merge SHA
```

**The two medians**, each the overhead past CI — the seconds a phase spent beyond the CI it was
waiting on — against the 15s bar the plan set for changing an interval:

| Phase | Poll | Median overhead past CI | Verdict |
|---|---|---|---|
| `wait_merge` | `--await-merge`, 30s (`MERGE_POLL_S` in `land_lib/options.py`) | 14s | keep 30s |
| `wait_ci` | 20s, the literal in `await_ci_verdict` (`land_lib/tools.py`) | 10s | keep 20s |

**Change the `await_ci.wait(sha, timeout_s, 20)` call in `land_lib/tools.py` to move the
`wait_ci` poll, not `await_ci.py`'s `--interval` default.** A landing reaches `await_ci` through
`tools.await_ci_verdict`, which passes that interval as a literal. The CLI default governs only
a hand-run `await_ci.py`. Editing it alone leaves every `land.sh` polling at 20s while the suite
still passes — a silent no-op that reads as shipped. Move both together. (Cited by symbol, not
by line: the `options.py:15` this table used to carry was already one line off the
`MERGE_POLL_S` it pointed at.)

**What the `wait_merge` readings show.** Nine of the 15 landings are ones the split rule keeps
out of the headline median, because their `wait_merge` is shorter than the CI duration of their
own PR. Eight of the nine wait 4-7s in total and pay no poll cost. The ninth is PR #1817, which
waits 66s, two polls, and pays two: the rule buckets it here on the arithmetic alone. For the
eight, the ledger records no `--await-merge` flag, so a first check that found the PR already
merged and a landing run without the flag look identical — the data cannot separate them.

Three readings of the overhead, none of which reaches the bar:

| Reading | Value |
|---|---|
| The six rows armed before CI finished: `[0, 9, 14, 14, 15, 19]` | 14s |
| All 15 rows, an unpolled row contributing its own `wait_merge` | 6s |
| Absolute clock: `t_merged` against the merge run's `run_started_at` | 0s |

**Treat the 14s as consistent with the poll, not as a margin under the bar.** A remainder
against a 30s poll has an expected median near 15s by construction, and the plan set the bar at
half the interval, so a six-row median of 14s cannot discriminate — that test is close to a coin
flip at this sample size. The robust evidence is the other two readings: most landings pay
nothing, and anchoring on GitHub's own clock puts the overhead past CI at 0s because 10 of 15
detect the merge within a second. The six-row figure also assumes `land.sh` was armed exactly
when PR CI started, which the ledger cannot confirm.

The 30s poll does visibly quantize `wait_merge` — the values cluster at 4-7s, 66-67s, 97-99s and
136s, which is 0, 2, 3 and 4 polls. Only the five landings whose merge lands mid-sleep pay it,
and for those the overhead past CI is 5-28s on the absolute-clock reading. That set is not the
six armed before CI finished; the two counts answer different questions.

**Why shortening the interval would not touch the four longest waits.** On the same reading as
the 10s median, `wait_ci - master CI`, four rows sit far above the 20s interval: 435s, 80s, 76s
and 52s. `await_ci` follows a moved tip when the polled SHA holds only no-verdict conclusions,
so those rows span the CI of a later commit rather than their own, and no interval reaches them.
Excluding the four, the remaining 11 run from -26s to 16s, a spread a 20s poll covers. The
negative values are not a defect: `wait_ci` starts at `t_merged`, which itself lags the real
merge by the merge-poll delay, so this reading nets the two polls against each other and reads
low. Measuring the CI poll on its own clock, from `prek`'s `completed_at` to the green
observation, puts its median at 12s — still under the bar.

**Request cost is not the constraint.** `await_ci` authenticates through
`deploy_git.github_token`, as the deployer's own gate does, which is 5000 requests/hour. At the
measured median a landing spends about 5 polls, and about 45 in the worst case, riding the 900s
timeout to its end. Halving the interval doubles both, and neither approaches the budget. The
60/hour anonymous ceiling binds only if the token lookup returns `None` — a missing `gh`, an
expired login, a slow keyring — which is the fallback path, not the ordinary one.

**Re-measure when any of these changes**, because both medians sit within 5s of the bar and
neither the window nor the verdict announces its own staleness:

- The duration of PR CI itself moves by more than the poll interval it is measured against.
- Either poll interval changes.
- Steps 2-3 of a landing change, the way PR #1796 opened this window.

## Tests

Every new rule ships with a proof it can go red — one input it must accept, one it must reject.
Name them as `..._is_clean` / `..._is_flagged` pairs, following
`scripts/validate/tests/test_validate_compose_templates.py`.

| Rule | Accepts | Rejects |
|---|---|---|
| Broad classification | `roles/setup/renovate_notify/` auto-applies | `roles/setup/gitops_deploy/` is excluded |
| Setup-plane tag derivation | `ansible/roles/setup/foo/tasks/main.yml` → `foo` | `ansible/bootstrap.yml` → no tag |
| Budget check | a scoped setup run fits | a full `deploy.yml` plus rollback does not |
| PR tag derivation | a 3-file PR scopes to its services | a 120-file PR falls back to `--changed` |
| CI verdict | a genuine `failure` is red | a `cancelled` falls through to the tip |
| Hold message | a service hold names the PR | a plane hold names the playbook |

`await_ci.py` is verified by running it (`uv run python scripts/deploy_tools/await_ci.py --help`),
not only by the suite — pytest's `pythonpath` resolves cross-directory imports that a direct
invocation does not.

## Files

| File | Change |
|---|---|
| `scripts/deploy_tools/await_ci.py` | new |
| `scripts/deploy_tools/land.sh` | new |
| `scripts/deploy_tools/tests/test_await_ci.py` | new |
| `scripts/deploy_tools/deploy_detach_notify.py` | `--no-post` |
| `ansible/roles/setup/gitops_deploy/files/deploy_logic.py` | broad split, setup-tag derivation |
| `ansible/roles/setup/gitops_deploy/files/gitops_deploy.py` | the two apply arms |
| `ansible/roles/k8s/monitor-bridge/files/checks/service.py` | plane-aware hold message |
| `CLAUDE.md` | the post-merge section collapses to one command plus the exceptions |
| `ansible/roles/setup/gitops_deploy/CLAUDE.md` | broad-change behaviour, and the branch-protection correction below |

## Out of scope, reported

`ansible/roles/setup/gitops_deploy/CLAUDE.md` states "This gates the DEPLOY; branch protection
gates the MERGE" as the reason the CI gate is not redundant. **There is no branch protection on
`master`** — `gh api repos/DanielH2018/server/branches/master/protection` returns 404 (checked
2026-08-29).

The CI gate's value is unchanged, because it turns out to be the only gate. The stated rationale is
false, and the doc line is corrected as part of this work. Adding branch protection is a separate
decision and is not made here.
