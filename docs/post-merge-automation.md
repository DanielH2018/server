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
| Master CI on a merge commit | 260–300s | `gh run list --branch master`, 2026-08-29 |
| Hand-polls spent waiting | 835 across 213 episodes | measured 2026-08-29 |
| Full `deploy.yml`, forward | 1212s, 1753 tasks ok | `ansible.log` pid 2004861, 2026-08-22 |

The ~100s runs in `gh run list` are PR pushes. Only the ~290s runs are merge commits, and only
those gate a deploy.

## The two problems are separable

**Waiting on CI is session-side.** `next_action` returns `ci_pending` *before* the fast-forward
by design (`deploy_logic.py:422`), so a tick fired seconds after a merge pulls nothing and every
later step reads as stale. That ordering is correct and stays. What is removable is the session
sitting in the foreground while it resolves.

**Hand-merging is one arm of the deployer.** An ordinary k8s manifest change already
fast-forwards on its own — `gitops_deploy.py:1221` merges whenever `cs.services` is empty. Only
`cs.broad` (`gitops_deploy.py:1136`) returns *without* merging. A docs-only commit sharing a
tick alongside another session's setup-plane change is therefore stranded too. The symptom is
a tick that exits 0, logs nothing, and writes `behind_since`.

### Why the obvious fix is wrong

Making the broad arm fast-forward clears `behind_since`. `_record_behind` (`gitops_deploy.py:1340`)
recomputes from HEAD after `main()` returns, so a converged tree reads as not-behind. That marker
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

### `scripts/deploy_tools/land.sh`

Pure glue, roughly 80 lines. It exists for one reason: the worktree containment check refuses
multi-step chains, so the sequence must be a single invocation.

```
land.sh --pr <n> [--since <pre-merge-sha>] [--tags <a,b>]
```

Sequence:

1. Resolve the merge SHA from the PR.
2. `await_ci.py <merge-sha>`.
3. `gitops_tick.sh` — fetch, CI-gate, ff-merge, deploy what is eligible.
4. `deploy.sh --tags <derived>` from `/home/ubuntu/server`, for what the tick deferred.
5. The health verdict, via `deploy_detach_notify.py --no-post`.
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
  it finishes. `deploy.sh` then acquires cleanly. The 30-minute timer can still slip in between —
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
| **Total** | **2604** | against `TimeoutStartSec=45min` (2700) |

A 96-second margin is 3.5%. A run four percent slower than measured is SIGTERMed mid-rollback,
which is precisely the failure the hold-before-reset comments throughout `gitops_deploy.py` exist
to prevent.

So the deploy-plane arm is **forward-only**. On failure it writes `hold_sha`, leaves the tree
fast-forwarded, and alerts.

It does not reset. Resetting without a redeploy leaves the tree claiming old while live state is
half-new — worse than either end state, and undiagnosable from the repo side. `hold_sha` is what
stops the retry loop, and it does that whether or not the tree was reset.

Raising `TimeoutStartSec` is the lever that would fund a rollback. Taking it requires a fresh
measurement of a full deploy, not the 2026-08-22 figure.

### The signal

A failed auto-apply leaves the tree converged and the plane unapplied — the same hole as a naive
broad fast-forward, reached by a different route. `behind_since` is clear by then and cannot carry
it.

`hold_sha` carries it instead. It already pages through **GitOps Deploy — Status**
(`monitor-bridge/files/check.py:1462`) with no new timer, which is the reuse the existing
behind-origin watchdog design already argues for.

Its message is service-shaped — `check.py:1463` reads `deploy held at %s — revert the offending PR`. A held *plane* needs a variant naming the playbook that failed, because reverting the PR is
not the remediation when the tree is already merged and the playbook is what broke.

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
| `ansible/roles/k8s/monitor-bridge/files/check.py` | plane-aware hold message |
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
