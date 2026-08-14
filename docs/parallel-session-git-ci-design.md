# Parallel Claude Sessions — Git & CI Design

**Date:** 2026-08-14
**Status:** Design, approved for planning
**Scope:** Making concurrent Claude Code sessions safe and cheap to run against this repo and cluster.

## Problem

Several Claude sessions now work this repo simultaneously, each in its own
`.claude/worktrees/<name>` checkout. The repo's safety and merge machinery was
built for one session at a time and has not caught up. Four concrete failures,
all evidenced below:

1. **A validation hook returns green for files it never read.** The parallel-session
   safety net has a hole in it.
2. **Every merge invalidates every other open PR**, producing a rebase treadmill and
   duplicate branches.
3. **Nothing prunes worktrees or branches**, so merged work accumulates on disk and on
   the remote.
4. **Agent deploys bypass the deploy mutex that already exists**, so a hand-run
   playbook can race the GitOps deployer.

## Evidence

### 1. `validate-compose.sh` is a false green in worktrees (live defect)

`.claude/settings.json:70` invokes the PostToolUse hook by absolute path:

```
"command": "~/server/.claude/hooks/validate-compose.sh"
```

and the script's first action is `cd /home/ubuntu/server` (`validate-compose.sh:17`).
It then runs `scripts/validate_compose_templates.py` — **which takes no file argument**;
it renders every service's compose template found in the current working directory.

So when a session working in `.claude/worktrees/workload-tiering` edits a
`docker-compose.yml.j2`, the hook renders the *primary checkout's* copy of that
template, sees it is fine, and prints:

```
validate-compose: render validation passed (compose) ✓
```

The edited file was never read. This is the exact failure mode the hook exists to
prevent — its own header comment says it "is the only thing that catches those bugs
before CI" — and it is silently inoperative for every worktree session. CI still
catches the bug on push, so the consequence is wasted round-trips and misplaced
confidence in-session, not a bad deploy. It is nevertheless a defect, not a risk.

**Reproduced, not inferred** (2026-08-14, from a worktree session). A deliberate Jinja
break was appended to `ansible/roles/containers/dozzle/templates/docker-compose.yml.j2`
inside `.claude/worktrees/parallel-sessions-design`, then the hook was invoked with that
file's path as its `tool_input.file_path`:

```
validate-compose: render validation passed (compose_templates) ✓
HOOK EXIT: 0
```

The same validator, pointed at the worktree's own copy, reports
`54 template(s) checked, 1 failure(s).` The break was real; the hook did not see it.

**The root cause is deeper than the `cd`.** `scripts/_render_guard.py:22` resolves the
repo from the script's own location:

```python
REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "ansible"
```

Because the hook `cd`s to the primary checkout and invokes `scripts/validate_compose_templates.py`
*relatively*, `__file__` is always the primary checkout's script, so `REPO` is always
`/home/ubuntu/server` — regardless of cwd. Changing the `cd` alone would therefore fix
nothing: the fix must invoke the *worktree's own copy* of the validator by absolute path.

`ansible-lint.sh` already solved the equivalent problem for its own tool. Lines 32–38
derive the owning checkout from the edited file's path:

```bash
repo_root="${file_path%%/ansible/*}"
cd "$repo_root" || exit 0
```

That fix was never applied to its sibling.

### 1b. Worktrees have no uv environment (surfaced while reproducing)

Running the validator from inside the worktree fails outright:

```
ModuleNotFoundError: No module named 'yaml'
```

The hooks pass `uv run --no-sync` to stay fast on the per-edit hot path, and a fresh
worktree has no synced env for that to reuse. So the naive fix for (1) — just run the
worktree's script from the worktree — trades a false green for a hard error. Slice 1 has
to resolve this: either invoke the worktree's script through the primary checkout's
environment (`cd` primary, absolute script path — `__file__` then correctly resolves to
the worktree), or sync each worktree's env once on creation. The former is cheaper and is
the recommended shape.

### 2. `block-protected-edits` misses worktree-local `containers/` (minor)

`block-protected-edits.py:99-101` computes `repo_root` as two levels up from the
hook script's own location. Because `settings.json` always invokes the primary
checkout's copy, `repo_root` is always `/home/ubuntu/server`, so the guard is
`/home/ubuntu/server/containers`. A `containers/` path inside a worktree does not
start with that prefix and sidesteps the guard.

Low severity — `containers/` is untracked and, post-migration, exists only on
`daniel-pi`. The SOPS half of the same hook is content-based and works correctly in
any checkout. Worth fixing for consistency with the same root cause as (1), not on
its own merit.

### 3. `strict: true` with no merge queue = a rebase treadmill

Branch protection on `master`:

```json
"required_status_checks": {
  "strict": true,
  "contexts": ["prek (lint + validate + tests + secrets)",
               "pull + boot changed images",
               "renovate config validator"]
}
```

`strict: true` means a PR must be up to date with `master` to merge. The repo's only
ruleset is `deletion` + `non_fast_forward`; there is no merge queue. Meanwhile
`allow_update_branch` is **false**, so there is not even a one-click "Update branch"
path — each session must rebase and force-push by hand, which re-runs all three
required checks.

Measured today (2026-08-14), `ci.yml` runs by branch:

| Branch | CI runs |
|---|---|
| `master` | 33 |
| `ci/cache-prek-bootstrap` | 15 |
| everything else | 1–3 each |
| **total** | **80** |

33 pushes to `master` in one day, each one invalidating every open PR. The branch
names are the fossils: `workload-tiering-rebased`, `seed-affinity-fix-2`,
`docs-refresh-k3s-v2` — sessions abandoning a branch and re-cutting it rather than
untangling a rebase.

### 4. Nothing prunes worktrees or branches

`deleteBranchOnMerge` is `false`. Current state:

- `.claude/worktrees/containers-role-cleanup` — branch merged into `master`, worktree still on disk and **locked**
- `.claude/worktrees/workload-tiering` — tracks `seed-affinity-fix-2`, which is merged
- `.claude/worktrees/slice-polish` — untouched since 2026-08-09
- 8 local branches, of which 5 are unmerged; ~12 remote `worktree-*`/feature branches beyond the Renovate set

### 5. Agent deploys bypass the existing mutex

The repo already has a canonical lock. `gitops-deploy.service.j2:37`:

```
ExecStart=/usr/bin/flock -w 180 /var/lock/server-git-tree.lock /usr/bin/python …
```

`secret-rotate.sh.j2` takes the same lock — its comment states flock "serializes
against the weekly secret-rotate cron." The GitOps deployer runs on a 30-minute
timer from `master`.

An interactive `uv run ansible-playbook ansible/deploy.yml --tags <svc>` takes no
lock at all. Two Claude sessions deploying concurrently, or one session deploying
while the 30-minute GitOps timer fires, interleave writes to the same rendered tree
and the same cluster. The mutex exists; the agent path just doesn't participate.

## Design

Five vertical slices, each independently exercisable. Ordered by
blast-radius-reduction per unit of work — slice 1 fixes a live defect, slice 2 pays
back the largest recurring cost.

### Slice 1 — Make hooks validate the tree they were called about

Derive the checkout from the edited file's path rather than hardcoding
`/home/ubuntu/server`, using the pattern `ansible-lint.sh` already established.

- `validate-compose.sh`: derive `repo_root` from `$file_path`, then invoke the
  validators by **absolute path** into that checkout — `"$repo_root/scripts/<name>.py"` —
  while keeping the working directory at the primary checkout so `uv run --no-sync`
  finds a synced environment. `_render_guard.py` resolves `REPO` from `__file__`, so the
  absolute script path is what actually redirects the validation; the `cd` is not the
  lever. Keep `UV` absolute.
- `block-protected-edits.py`: resolve `repo_root` from the target file's enclosing
  checkout (walk up to the nearest directory containing `.git`) instead of from the
  hook script's location.
- Extend `.claude/hooks/test_auto_approve_readonly.py`'s sibling test files with cases
  asserting a worktree path routes to the worktree's own root. `test_block_protected_edits.py:29`
  already carries a comment that a hardcoded `/home/ubuntu/server` was wrong — this
  makes that assertion real for the worktree case too.

**Exercisable:** from a worktree session, introduce a deliberate Jinja indent bug in a
`docker-compose.yml.j2` and confirm the hook now exits 2 instead of printing `passed ✓`.

### Slice 2 — Drop `strict`, let auto-merge do the serialization

- Set `required_status_checks.strict = false` on `master`. The three required contexts
  still gate every PR; only the "must be up to date" clause goes away.
- Enable `allow_update_branch` so a genuinely stale PR has a one-click path.
- Use `gh pr merge --auto` as the standard session close-out: the PR merges itself when
  checks pass, without a session sitting and polling.

`allow_auto_merge` is already `true` and unused — this is turning on a mechanism that
is already provisioned.

**Risk, stated plainly:** without `strict`, two PRs that each pass CI independently can
merge into a broken `master` if they conflict semantically. The accepted judgement is
that this risk is low *here* — the required checks are lint, template rendering, unit
tests, and secret scanning, none of which are integration tests across PR boundaries —
and that the GitOps deployer's health-gate-and-rollback on `master` is the backstop.
If a semantic collision does land, the fallback is the merge queue — believed available
on this repo (public, user-owned), but that is a product-tier claim that was not tested
against the API. Verify before relying on it.

**Exercisable:** open two trivial PRs, merge the first, confirm the second is still
mergeable without a rebase.

### Slice 3 — Worktree and branch lifecycle

- Flip `deleteBranchOnMerge` to `true`. Remote branches then disappear on merge; this
  alone removes most of the accumulation.
- Add `scripts/prune_worktrees.py`: for each `.claude/worktrees/*`, report branch,
  merged-into-`origin/master` status, dirty/clean, lock state, and last-commit age.
  `--dry-run` by default; `--prune` removes worktrees that are merged **and** clean
  **and** unlocked. Never touches a dirty or locked tree — a lock is how a live session
  says "in use."
- Unit-test the classifier against a temp repo fixture, matching the existing
  `scripts/` suite style, and register it in `pyproject.toml` `testpaths`.
- One-time: run `--prune` against the current backlog and delete the merged local
  branches by hand.

**Exercisable:** `uv run python scripts/prune_worktrees.py` correctly classifies the
three worktrees currently on disk — `containers-role-cleanup` merged-but-locked,
`workload-tiering` merged, `slice-polish` stale.

### Slice 4 — Make agent deploys take the lock that already exists

Wrap the interactive deploy path in the same mutex the automated ones use, rather than
inventing a second one.

- Add `scripts/deploy.sh` (or extend the `/deploy` skill) to invoke
  `flock -w 180 /var/lock/server-git-tree.lock uv run ansible-playbook …`.
- On timeout, fail with a message naming the likely holder (the GitOps timer or another
  session) rather than a bare `flock` exit 1.
- Update the `/deploy` skill and `CLAUDE.md` → *Common Commands* so the locked form is
  the documented one. The bare `ansible-playbook` invocation stays working for the
  `--check` dry-run case, which takes no lock because it writes nothing.

**Consequence to design around, not a solved point.** `gitops-deploy.service.j2:34`
states that when its `flock -w 180` times out, "flock exits 1, failing the unit cleanly
(OnFailure alerts)" — and the service budgets its own deploy phase at up to ~1020s. An
agent deploy that holds the lock for more than 180 seconds therefore makes the next
30-minute GitOps firing fail its unit and raise a Discord alert. A full playbook run
plausibly exceeds 180s, so taking the lock naively trades a silent race for a noisy false
alarm. Three ways out, to be decided in the implementation plan:

1. Hold the lock only around the playbook invocation, not any surrounding health gate,
   keeping the held window as short as the work allows.
2. Give the agent path a **longer** wait than 180s, so an interactive deploy queues behind
   a running GitOps deploy rather than giving up.
3. Accept the alert as the correct signal (a deploy genuinely was in progress) and
   document it so it isn't chased as a fault.

**These are not interchangeable — they fix opposite directions.** Option 2 only helps when
GitOps holds the lock and the agent wants it; it does nothing about the case that produces
the alert, which is the agent *holding* the lock past 180s. Only option 1 shortens that
window, and it cannot shorten it below however long the playbook genuinely takes. So the
implemented shape is 1 + 2 + 3 together: hold the lock around the playbook and nothing
else, wait generously to acquire, and treat the resulting alert as true rather than
suppressing it — the timer retries 30 minutes later.

**Exercisable:** start a deploy, start a second concurrently, confirm the second blocks
and then proceeds rather than interleaving.

### Slice 5 — Session naming and collision visibility

**Naming.** One worktree per session, named for the work; the branch takes the same
slug. `EnterWorktree` prefixes branches with `worktree-`, which is noise on the remote —
the convention is to name the worktree the slug you actually want the branch to carry
(`containers-role-cleanup`, not `cleanup`), and to accept the prefix rather than fight
it. One PR per session; a session that needs a second piece of work opens a second
worktree.

**Collision visibility, derived not declared.** Rather than a claims file that agents
must remember to update — which drifts the moment one forgets — extend the existing
`session-health.sh` SessionStart hook to print, for every *other* worktree on disk, its
branch and its `git diff --name-only origin/master...HEAD`. A session then opens
knowing that another session is already touching `roles/k8s/traefik`, from ground truth
rather than from a file's honesty.

This follows the repo's own stated principle in *Review & Memory Hygiene*: prefer a
check a machine derives over a paragraph an agent has to remember.

**Exercisable:** open a session with two worktrees present and confirm the banner lists
the other's branch and changed paths.

## Out of scope

- Merge queue — held as the documented fallback for slice 2, not built now.
- A session-coordination service or lock daemon. The existing flock and the derived
  banner cover the real cases; a coordination layer would be new infrastructure to
  maintain for a solo operator.
- CI cost reduction beyond what the just-merged prek-cache work (#157) achieved. At
  80 runs/day and 1–3 minutes each this is not currently a constraint.

## Sequencing note

Slices 1 and 2 are independent and can land in either order; 1 is the live defect, 2 is
the largest recurring cost. Slice 3 depends on nothing. Slice 4 is independent. Slice 5's
banner work is easiest after slice 3, since both read the same worktree inventory —
factor the inventory helper once in slice 3 and reuse it in slice 5.
