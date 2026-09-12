# The GitOps pipeline

How the homelab deploys itself, and what to do when it stops.

This is the operator's view. For the internals — the decision functions, their tests and the
incidents each one encodes — read
`ansible/roles/setup/gitops_deploy/CLAUDE.md`.

!!! warning "A merge is not a deploy"
    The deployer applies **only** an image-pin bump to a service that is not denylisted.
    Every other change — a manifest, a template, a task file — is fast-forwarded onto the
    primary checkout and left unapplied. Left alone it sits undeployed behind a green master
    until someone notices.

Each k8s role declares its own `k8s_autodeploy` stance
(`ansible/roles/k8s/<role>/defaults/main.yml`); `ansible/filter_plugins/k8s_autodeploy.py`
collects the ones declaring `false` into the denylist above.

<!-- Generated from k8s_autodeploy_counts.py, which reads every role's own declaration; edit
     the role's defaults/main.yml, not this table. -->
--8<-- "assets/generated/fragments/autodeploy-coverage.md"

## What a tick does

A systemd timer runs `gitops-deploy.service` on `daniel-box`
every 10 minutes (`gitops_deploy_tick_interval`). One tick, in order:

1. Fetch `origin`.
2. Read the check runs for the tip of `origin/master` and decide a CI verdict (`ci_verdict()`
   in `deploy_logic.py`).
3. Walk back to the newest green ancestor, when that verdict is pending or red: the tick asks
   GitHub about each earlier commit in the incoming range, newest first, and deploys the first
   one whose own CI is green (`ci_walk_candidates()` in `deploy_logic.py`, bounded by
   `CI_ANCESTOR_WALK_MAX`). A red commit is skipped, never chosen. Master takes about 124
   merges a day against a ~103s CI sweep, so the tip is pending on most ticks that would
   deploy; gating on it parked a green commit behind the CI sweep of every later merge.
4. Choose an action from the verdict for the chosen SHA and the changed paths (`next_action()` in
   `deploy_logic.py`).
5. Consult the staging cluster, on a k8s deploy where `gitops_deploy_staging_gate` is armed
   and the services intersect `STAGING_SUBSET`. Advisory: it returns no verdict and cannot
   block the deploy — worth knowing when a tick looks stuck.
6. Fast-forward the checkout to the chosen SHA, if the action allows it.
7. Deploy whatever is eligible.
8. Health-gate the result, and roll back on failure.

<!-- Generated from STAGING_GATE_TIMEOUT_S and STAGING_EXPECT_TIMEOUT_S in gitops_deploy.py;
     edit those. -->
--8<-- "assets/generated/fragments/staging-timeouts.md"

All of it runs while holding `/var/lock/server-git-tree.lock`, which is what stops a tick
rewriting the tree under the secret-rotation cron or under an operator's snapshot. The deploy
steps also take one `/var/lock/server-deploy-<tag>.lock` per service, inside that hold, which
is what stops a tick and an operator's deploy driving the same rollout. See
[ADR-0011](adr/0011-one-lock-serialises-every-deploy-path.md) and
[ADR-0017](adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md).

## Reading the deployer's state

`./scripts/deploy_tools/gitops_tick.sh` prints three values after it runs. Read them before concluding
anything.

| Field | Means |
|---|---|
| `last_run` | When a tick last completed. A stale value means the timer is not firing. |
| `hold_sha` | Non-empty: a previous SHA failed its health gate and is being held. Diagnose that before deploying anything else. |
| `behind_since` | Non-empty: the checkout is behind `origin`, naming the tip it has not reached. Its timestamp is when the deployer last FAST-FORWARDED, not when it first fell behind: any tick that moved the tree renews it, so a tick that landed at a green ancestor reads healthy while a tail that never goes green still pages at 6h. |

## Why a tick does nothing

Four reasons, and they look identical from outside — the unit succeeds and exits 0 in every
one of them.

**CI is pending or red on every commit in range.** `next_action()` returns `ci_pending`
*before* the fast-forward. A tick fired seconds after a merge therefore pulls nothing, because
GitHub has not finished creating the run yet. An empty or incomplete check-run list is
pending, never green. It takes the whole range: one green commit below a pending tip is enough
for the tick to fast-forward to that commit and deploy it.

**A held SHA.** `hold_sha` is set, so the deployer does not move forward until it is cleared.

**The tree is dirty.** Someone left uncommitted changes in the primary checkout.

**A broad change the deployer must not apply itself.** This is the one that surprises people,
so it has its own section.

## Broad changes

A change under a **broad prefix** is one that maps to no single service. The deployer splits
them three ways, by which playbook applies them and whether it may run that playbook at all.

<!-- The table is generated from deploy_changes.py's three prefix tuples; edit those. -->
--8<-- "assets/generated/fragments/broad-prefixes.md"

The bring-up playbooks appear in both the setup and the manual class: the manual check runs
first, so a change to one of them is never applied here.

The setup/deploy split exists because `deploy.yml` is a `containers_list` loop and renders
nothing for the setup plane, so pointing an operator at it for a `roles/setup/` change is a
no-op that leaves the change unapplied.

A setup role the deployer cannot apply is a fourth case that behaved like the third until
2026-09-11: `k3s` lives in `k3s-bringup.yml` and `common` in no playbook, so `setup_tags_for`
derives nothing for either. Such a range fast-forwards and records the role instead of
parking — see *A role only a hand can apply is recorded, not parked* below.

The third class is the bring-up playbooks, which run by hand by construction. The deployer's
own role, `roles/setup/gitops_deploy/`, sat there until 2026-09-01 on the claim that applying
it restarts the unit executing the tick. It does not: the role's handler is `state: started`,
which Ansible skips for an `activating` unit, so a self-apply from inside a tick is a no-op on
the unit and the new code runs from the next tick. The park it imposed was real, though — every
other session's landing stopped behind it until an operator hand-ran the role and ff-merged,
three times that day. The role now applies itself as `initial_setup.yml --tags gitops_deploy`;
the `DECIDED:` marker above `_BROAD_MANUAL_PREFIXES` in `deploy_logic.py` carries the evidence.

### A deploy-plane change is narrowed before it is applied

The deploy arm ran `ansible/deploy.yml` unscoped, over all 54 roles, for any change under
`ansible/templates/` or `ansible/inventory/`. That is about twenty minutes under the tree
lock, it was 47% of all lock-busy time in the week from 2026-09-04, and twice it failed on a
gate belonging to a service the change never touched and held the fleet.

`deploy_handlers.handle_broad` now asks `scripts/deploy_tools/deploy_tags.py narrow <local>
<origin>` which services the range actually reaches, before the fast-forward and at the two
refs the tick pinned. A subprocess rather than an import: the derivation parses YAML and the
unit runs under `uv run --no-project`.

Three outcomes, and the journal names which one it took on every tick:

- **tags** -- `ansible/deploy.yml --tags <tags>`, recorded in `broad_applied` with those tags.
- **no tags** -- the range moves no rendered output (a comment-only inventory edit, a
  variable nothing reads, a macro nothing imports). The fast-forward is the whole apply, and
  `broad_applied` records `narrowed-to-nothing` in the tag slot.
- **a refusal** -- the full `deploy.yml`, exactly as before. Anything the derivation cannot
  map lands here: a variable the play itself reads, `hosts.ini`, a removed `containers_list`
  entry, a tag list covering most of the fleet, or a crash in the derivation. A missed
  consumer would be a service left silently stale; a full run is only slow.

`narrow` is read-only and can be run by hand against any range. The rules it applies, and
what each one refuses, are in `scripts/deploy_tools/narrow_broad.py`.

A failed narrowed apply holds the plane it named, so `hold_plane` reads
`ansible/deploy.yml <tags>` and only an apply covering those tags clears it -- an untagged
full run does, and a narrowed run covering a different service does not.

### Both apply arms are forward-only

A failed apply writes `hold_sha` and `hold_plane`, alerts, and leaves the tree
fast-forwarded. Nothing is rolled back, and the alert says so.

A rollback re-run has to fit inside the unit's `TimeoutStartSec`, or it is killed partway —
worse than never starting one. The arm stays forward-only because proving it fits needs a
fresh `deploy.yml` measurement, not because of any particular timeout value:
`deploy_logic.broad_budget_ok` encodes the check and has no production caller. The role's own
`ansible/roles/setup/gitops_deploy/CLAUDE.md` carries the numbers and the date they were taken;
read `TimeoutStartSec` out of `gitops-deploy.service.j2` rather than from prose, since it moves
when the staging gate's budget changes.

It deliberately does not reset the tree either. Resetting without redeploying would leave the
tree claiming the old commit while live state is half-new — a tree that lies, over which every
repo-side check reads green.

### A role only a hand can apply is recorded, not parked

A range carrying `roles/setup/k3s/` or `roles/setup/common/` parked the whole tick until
2026-09-11. The wait bought nothing: the role needs `ansible-playbook
ansible/k3s-bringup.yml --tags k3s` whether or not the range is merged, while every other
session's landing behind it exits 4 from `deploy.sh` until a hand pulls the primary checkout.
Ten episodes over the seven days to then spanned 30 ticks, the longest about forty minutes.

The tick fast-forwards instead, and writes the role to
`/var/lib/gitops-deploy/manual_plane`, one line per role as
`"<origin_sha> <playbook-or-none> <role> <unix_ts>"`. A role already listed is not re-added,
so its first-seen stamp is the age everything else reads. Four consequences:

- the journal says `manual_plane pending: <roles> — apply by hand: <commands>` on **every**
  tick, not only the one that recorded it. The recording tick says
  `manual_plane recorded: <roles>` instead, naming only the roles it added, so no tick prints
  both lines;
- Discord pages once per SHA, with the same commands and the marker's path;
- **GitOps Deploy — Status** goes down once the oldest pending line is older than
  `GITOPS_BEHIND_MAX_S` (6 h), naming the roles and the clear command;
- the **SessionStart banner** names one line per pending role — the role, its playbook, how
  long it has waited and the clear command — from the moment the tick records it. Not
  age-gated, unlike the monitor: the monitor pages, where the banner is a passive notice, and
  the session reading it is usually not the session that landed the change;
- `broad_applied` is not written for that role — a role in the same range that DID apply
  still records its own, so a mixed push still proves the half it applied.

Applying the role by hand is half the job. The line stays until something clears it, and a
role left in the marker pages six hours later over work that is already live:

```bash
ansible-playbook ansible/k3s-bringup.yml --tags k3s
uv run python scripts/deploy_tools/gitops_state.py clear-manual-plane k3s
```

The deployer clears a line itself when a tick applies that role's own playbook and tag
(`DeployerState.clear_manual_plane_applied`). No role reaches that today, since the tick runs
neither `k3s-bringup.yml` nor a playbook for `common`; it is what a role promoted into
`initial_setup.yml` needs on the day it is.

### When a tick parks

Two shapes park: a bring-up playbook, and a setup path naming no role at all. The symptom
is unchanged — **a tick that exits 0, logs a park line, and writes `behind_since`** — and the
journal line says which of the two it was. The deferral is evaluated over the whole
`local..origin` range, so one such change anywhere in that range holds back everything behind
it. Diagnose it by diffing the range:

```bash
git diff --name-only <local-HEAD>..origin/master
```

The Discord alert names the playbook to run. **Fast-forward first, then run it** —
`git merge --ff-only origin/master` in the primary checkout, and only then the playbook.
Running the playbook first renders from the pre-merge tree and applies nothing.

**If the change is another session's, it is theirs to clear.** Say so and stop, rather than
applying a setup-plane change you did not write.

## Triggering a tick by hand

To run a tick without waiting for the timer, on `daniel-box`:

```bash
./scripts/deploy_tools/gitops_tick.sh
```

It runs the identical code path the timer runs. **There is no dry-run mode** — this deploys.

`--no-wait` starts the tick and returns at once, without watching it or printing its journal.
That is what a landing uses: `land.sh` deploys the merge commit of its own pull request
(`deploy.sh --at <sha>`), so it needs the tick only to converge the primary checkout, which
the timer does within ten minutes whether the request landed or not. It kicks it after the
deploy, not before — this unit holds the git-tree lock for its whole run, so a tick started
first is one `deploy.sh` then waits out to cut its snapshot.

## Gating on CI correctly

To wait for master CI on a merge commit, read the same endpoint the deployer reads:

```bash
gh api repos/DanielH2018/server/commits/<merge-sha>/check-runs \
  --jq '.check_runs[] | "\(.name) \(.status) \(.conclusion)"'
```

Do **not** gate on `gh run list --branch master --limit 1`. GitHub creates the run for a
freshly pushed merge commit a moment after the push, so that query returns the *previous*
master run — green — and the wait returns instantly having watched the wrong commit.

Reading the same endpoint as the deployer is what makes your verdict and the tick's agree by
construction.

## Contention is not failure

A tick that cannot take the lock exits **75**, and the systemd unit **succeeds**. That is a
resume point, not an error: the lock was busy and nothing was deployed.

This is deliberate and documented at the line that sets it
(`gitops-deploy.service.j2:87`). Treating contention as failure paged seven times in seven
days, because every long operator deploy held the same lock for its whole run. It no longer
does — `scripts/deploy.sh` holds the tree lock for a snapshot of `HEAD` and nothing more
([ADR-0017](adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md)) — so tree-lock
contention should now be rare. A tick can still queue behind an operator deploying one of the
same services, on that service's own lock.

Contention is still not silent starvation: the lock path never writes `last_run`, so
contention outlasting the maximum age pages through the GitOps-Alive monitor.
