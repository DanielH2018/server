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

## What a tick does

A systemd timer runs `gitops-deploy.service` on `daniel-box` every 30 minutes. One tick, in
order:

1. Fetch `origin`.
2. Read the check runs for the origin SHA and decide a CI verdict (`ci_verdict()` in
   `deploy_logic.py`).
3. Choose an action from the verdict and the changed paths (`next_action()` in
   `deploy_logic.py`).
4. Consult the staging cluster, on a k8s deploy where `gitops_deploy_staging_gate` is armed
   and the services intersect `STAGING_SUBSET`. Advisory: it returns no verdict and cannot
   block the deploy, but it adds up to `STAGING_GATE_TIMEOUT_S` + `STAGING_EXPECT_TIMEOUT_S`
   (600s + 120s) to the tick — worth knowing when a tick looks stuck.
5. Fast-forward the checkout, if the action allows it.
6. Deploy whatever is eligible.
7. Health-gate the result, and roll back on failure.

All of it runs while holding `/var/lock/server-git-tree.lock`, which is what stops a tick
racing an operator's deploy or the secret-rotation cron. See
[ADR-0011](adr/0011-one-lock-serialises-every-deploy-path.md).

## Reading the deployer's state

`./scripts/deploy_tools/gitops_tick.sh` prints three values after it runs. Read them before concluding
anything.

| Field | Means |
|---|---|
| `last_run` | When a tick last completed. A stale value means the timer is not firing. |
| `hold_sha` | Non-empty: a previous SHA failed its health gate and is being held. Diagnose that before deploying anything else. |
| `behind_since` | Non-empty: the checkout is parked behind `origin` and naming the SHA it stopped at. |

## Why a tick does nothing

Four reasons, and they look identical from outside — the unit succeeds and exits 0 in every
one of them.

**CI is pending or red.** `next_action()` returns `ci_pending` *before* the fast-forward. A
tick fired seconds after a merge therefore pulls nothing, because
GitHub has not finished creating the run yet. An empty or incomplete check-run list is
pending, never green.

**A held SHA.** `hold_sha` is set, so the deployer will not move forward until it is cleared.

**The tree is dirty.** Someone left uncommitted changes in the primary checkout.

**A broad change the deployer must not apply itself.** This is the one that surprises people,
so it has its own section.

## Broad changes

A change under a **broad prefix** is one that maps to no single service. The deployer splits
them three ways, by which playbook applies them and whether it may run that playbook at all.

| Class | Prefixes | What the deployer does |
|---|---|---|
| Setup, scoped | `ansible/roles/setup/<name>/`, `ansible/requirements.yml` (`_BROAD_SETUP_PREFIXES`) | fast-forwards, then runs `initial_setup.yml --tags <name>` |
| Deploy plane | `ansible/templates/`, `ansible/inventory/`, `ansible/roles/containers/common/`, `ansible/deploy.yml`, `ansible/filter_plugins/`, `ansible.cfg` (`_BROAD_DEPLOY_PREFIXES`) | fast-forwards, then runs a full `ansible/deploy.yml` |
| Never applied here | `ansible/bootstrap.yml`, `ansible/k3s-bringup.yml`, `ansible/initial_setup.yml` (`_BROAD_MANUAL_PREFIXES`) | alerts and returns **without fast-forwarding** |

The setup/deploy split exists because `deploy.yml` is a `containers_list` loop and renders
nothing for the setup plane, so pointing an operator at it for a `roles/setup/` change is a
no-op that leaves the change unapplied.

The third class is the bring-up playbooks, which run by hand by construction. The deployer's
own role, `roles/setup/gitops_deploy/`, sat there until 2026-09-01 on the claim that applying
it restarts the unit executing the tick. It does not: the role's handler is `state: started`,
which Ansible skips for an `activating` unit, so a self-apply from inside a tick is a no-op on
the unit and the new code runs from the next tick. The park it imposed was real, though — every
other session's landing stopped behind it until an operator hand-ran the role and ff-merged,
three times that day. The role now applies itself as `initial_setup.yml --tags gitops_deploy`;
the `DECIDED:` marker above `_BROAD_MANUAL_PREFIXES` in `deploy_logic.py` carries the evidence.

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

### When a tick parks

Only the third class parks now, and the symptom is unchanged: **a tick that exits 0, logs
nothing, and writes `behind_since`.** The deferral is evaluated over the whole `local..origin`
range, so one such change anywhere in that range holds back everything behind it. Diagnose it
by diffing the range:

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
(`gitops-deploy.service.j2:64`). Treating contention as failure paged seven times in seven
days, because every long operator deploy holds the same lock.

Contention is still not silent starvation: the lock path never writes `last_run`, so
contention outlasting the maximum age pages through the GitOps-Alive monitor.
