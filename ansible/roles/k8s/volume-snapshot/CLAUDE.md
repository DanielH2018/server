# k8s/volume-snapshot — a pre-apply Longhorn snapshot for a `Recreate` + RWO role

This role deploys nothing. `roles/k8s/manifests` includes it immediately **before**
`Apply manifests for {{ manifests_service }}`, and it takes a Longhorn snapshot of each of the
caller's RWO volumes, waits until the snapshot is usable, then prunes that service's older
snapshots of the same volume.

**Read this first: the role gives you a recovery point and nothing that consumes it.** There is
no revert here, and no revert anywhere else in the repo. Reverting is slice 7b, and until that
lands it is a manual operation — the proven sequence is in the "Slice 7 drill, 2026-08-21"
section of `docs/superpowers/specs/2026-08-20-k8s-autodeploy-coverage-design.md`. Nothing in this
role detects a bad deploy, alerts on one, or rolls anything back. It makes the damage reversible
by hand; it does not reverse it.

## How a role opts in

A `Recreate` + RWO role does **not** include this role itself. It declares
`k8s_autodeploy_snapshot_pvcs` in its own `defaults/main.yml`, and the shared include in
`roles/k8s/manifests/tasks/main.yml` picks it up — role defaults are in scope for an included
role, so nothing needs plumbing through the call site:

```yaml
# roles/k8s/widget/defaults/main.yml
k8s_autodeploy_snapshot_pvcs:
  - widget-config   # PVC name(s) in k8s_namespace
```

A role that never declares `k8s_autodeploy_snapshot_pvcs` — every role until slice 7a task 3 opts
one in — gets `| default([]) | length == 0`, and the include in `roles/k8s/manifests` never runs:
no extra kubectl call, no extra fact, nothing.

`roles/k8s/manifests` passes through only `volume_snapshot_claims` and `volume_snapshot_service`;
`volume_snapshot_retain` and `volume_snapshot_timeout` are not wired to a caller override at that
call site. Whether a caller could still change them by declaring same-named defaults of its own
is unverified — two roles' `defaults/main.yml` setting the same variable name is not a pattern
tested here — so don't rely on it. They stay at this role's own defaults
(`volume_snapshot_retain: 3`, `volume_snapshot_timeout: 120`) until a caller genuinely needs
something else, at which point the call site is where that gets added.

## Why a `Recreate` + RWO role needs this

Thirteen roles here run `strategy: Recreate` against an RWO Longhorn volume. On an image bump the
old pod is deleted, the new one attaches the same volume, and the application may migrate its
on-disk format before anything observes a fault. After that, redeploying the previous image is
not a rollback: the old binary can no longer read its own data.

Neither existing gate sees it. `rollout status` and the stabilisation soak in
`roles/k8s/manifests` both watch for a pod that starts and stays up — and a schema migration is
exactly what a healthy start looks like. The failure surfaces later, as corrupt data or as a
failed downgrade, at which point there is nothing to go back to.

## The snapshot name is deterministic, and 7b depends on that

```
autodeploy-<service>-<sha8>-<claim>
```

`<sha8>` is `git rev-parse --short=8 HEAD` in the repo the deploy renders from. Slice 7b has to
find this snapshot without being told what it was called, so the name is derived rather than
recorded anywhere.

**The claim suffix is an extension of the design's `autodeploy-<svc>-<sha8>`, added because that
name collides.** `volume_snapshot_claims` is a list, and a service with two RWO claims — pihole
has exactly that — would produce two Snapshot CRs with one name. The design's string survives
verbatim as a **prefix**, so 7b reconstructs `autodeploy-<svc>-<sha8>` and matches on prefix
rather than equality.

**A multi-claim revert is not atomic.** This role snapshots each volume in turn, at slightly
different instants, and 7b will have to revert them one at a time with a scale-down between. A
partial failure leaves a service's volumes mutually inconsistent — which is a *different* and
possibly worse state than the bad deploy it was recovering from. Do not read N-claim support here
as N-claim support end to end.

**Two things weaken the tag's determinism, and both are worth knowing before trusting it:**

- **A dirty tree.** The SHA names the last commit, not the working tree. Deploy uncommitted
  changes twice from the same commit and both deploys claim the same tag while deploying
  different contents. The second `apply` finds the existing Snapshot CR, reports `unchanged`, and
  the second deploy's recovery point is the state before the *first* one.
- **Re-deploying an old tag after three newer deploys.** Retention keeps three, so the snapshot
  for that tag may already be `markRemoved` — and reverting to a `markRemoved` snapshot fails.
  The wait catches this and fails the deploy with a message naming it, rather than proceeding
  with a recovery point that cannot be used.

## Pruning is asynchronous, and a lingering CR is normal

A Snapshot CR carries a `longhorn.io` finalizer. Deleting it sets `markRemoved: true` and waits
for the volume to coalesce the data. Two consequences the prune is written around:

- **`kubectl delete` blocks by default** — measured hanging a drill run for twelve minutes on
  2026-08-21. The delete here always passes `--wait=false`.
- **The CR persists after a successful delete.** A snapshot whose only child is `volume-head`
  cannot be folded away while the volume is attached at all; it clears at the next detach.
  So "the CR is gone" is never the success condition, and a `markRemoved` CR sitting there is
  the expected state, not a failed prune.

`markRemoved` snapshots are dropped from the retention window rather than counted in it. Counting
three dead CRs as the retained three would make the next pass delete live snapshots to make room.

**The newest snapshot is never pruned, whatever `volume_snapshot_retain` says.** The value is
clamped to a floor of 1 at the point of use. 7b reverts to the most recent snapshot, and a
retention pass that races a rollback would destroy the recovery point it exists to protect.

## What fails the deploy, and what does not

The whole role runs **before** the apply, so every failure below stops the deploy without having
changed the workload.

| condition | outcome |
|---|---|
| a claim is missing or still `Pending` | **fails** — named by claim, before anything is applied |
| `git rev-parse` returns nothing | **fails** — an undated name is one 7b cannot find |
| the volume is detached (not `attached`) when the snapshot never reports `readyToUse` | **skips this claim and warns loudly** — see below |
| the snapshot never reports `readyToUse` for any other reason (`markRemoved`, or attached and stuck) | **fails** — this is the recovery point the deploy is about to need |
| the snapshot listing does not contain this run's own snapshot | **fails** — the read is broken, so the prune would be deleting from a set it cannot see |
| a delete in the prune returns non-zero | **fails** — see below |

The last row is a deliberate choice rather than an oversight. A prune failure is not itself
dangerous, but swallowing it means unbounded snapshot growth, and a retained snapshot pins every
block beneath it against `filesystem trim` — the mechanism
`roles/setup/k3s/templates/longhorn-reap-orphan-snapshots.sh.j2` exists to clean up after. Since
the prune runs before the apply, failing costs a deploy that has not started rather than a
half-deployed service.

## A detached volume cannot be snapshotted at all, and that is not fatal

A Longhorn snapshot needs a running engine, and a workload scaled to zero has none. Two records
of the same constraint: `longhorn-reap-orphan-snapshots.sh.j2` refuses to reap on a detached
volume ("no engine to purge it", observed on `terraria-config` on 2026-08-16), and the slice 7
drill got a `500` reverting a plainly detached volume for the same reason.

**Ruling from slice 7a task 2: this skips the claim and warns, rather than failing the deploy.**
The realistic way a `Recreate` + RWO volume is detached at snapshot time is that an operator
deliberately scaled the service to zero — failing that deploy would block a legitimate action for
a service that is already down. After the wait times out, `claim.yml` reads the volume's own
`status.state` and skips only when it is not `attached`; any other unready cause (a `markRemoved`
name collision, or a stuck engine on a volume that *is* attached) still fails the deploy.

The gap this leaves is real and stays visible on purpose: the apply that follows may scale the
workload back up, the new pod can migrate the on-disk format, and there is then no recovery point
behind it. The skip task's `ansible.builtin.debug` warning names the service, the claim, and that
the deploy is proceeding unprotected — a silent skip would defeat the point of the slice. Slice 7b
closes the gap for real: it builds maintenance-mode attach for the revert anyway, and the same
machinery can attach a detached volume long enough to snapshot it first.

## The guard is `k8s_no_mutate`, and it covers the prune too

Every mutating task carries `when: not (k8s_no_mutate | bool)`.
`inventory/group_vars/all.yml` defines that as `ansible_check_mode or (k8s_dry_run | bool)`, and
the reason it is one fact rather than two is that a role guarding either alone is guarded against
neither.

The **whole prune** is guarded, not just the delete. Under a no-mutation run the snapshot above
it was never taken, so a retention window computed from the live list would be counting three
snapshots that do not include this run's — and would delete a real one during a dry run.

The wait is **skipped** rather than run, for `k8s/cronjob-gate`'s reason: it waits on something
the same guard skipped creating, and reading the previous deploy's snapshot would be a lie.

The two reads that *do* run under `--check` — the deploy tag and the PVC's `volumeName` — carry
`check_mode: false` deliberately. A `command` task is skipped under `--check` by default, and a
skipped read does not fail; it fails its consumer several tasks later with an undefined
attribute. Running them gives a dry run something real to check (does the claim exist, is it
bound) at no cost, since both are reads.

This role is **not** in `k8s_dry_run_unsupported`, and should not be added. That refusal keys on
`ansible_run_tags`, so it only reaches roles an operator names on the command line — a role
reached as a dependency is invisible to it. `seed-volume`, `image-builder` and `cronjob-gate` are
all in the same position and are guarded internally instead.

## Things measured rather than assumed

- `kubectl`'s jsonpath has **no `&&`** — `unrecognized character in action: U+0026`, verified
  2026-08-21. The snapshot listing therefore filters on `spec.volume` alone and does the rest in
  Jinja. Do not "simplify" it into a compound filter expression.
- Every live Snapshot CR carries `status.markRemoved` explicitly (28 `false` / 17 `true` on
  2026-08-21), so filtering on it in Jinja does not have to treat an absent field as a third
  state.
- `argv:`, not a folded `cmd:` string, on every `kubectl` call here. `ansible.builtin.command`
  shlex-splits a `cmd`, so a jsonpath is only safe there while it happens to contain no spaces,
  and the listing's `{range .items[?(...)]}` does not. Slice 4 shipped that bug and made a whole
  branch dead code for a round.
- `git rev-parse` uses `chdir`, **not** `git -C`. `git -C` does not override `GIT_DIR`, and a
  stray `GIT_DIR` has already made a check in this repo operate on the wrong repository. It also
  runs without `become`, because git refuses a repository it considers to have dubious ownership
  when a different user reads it.

## What is unverified

`kubectl` in this repo authenticates as a read-only ServiceAccount and Ansible is the only write
path to the cluster, so the create path here has **not** been exercised end to end. What was
checked live on 2026-08-21 is the shape it depends on: `longhorn.io/v1beta2` `Snapshot` is a
served resource; live CRs carry `spec.volume`, `spec.createSnapshot`, `status.readyToUse`,
`status.markRemoved` and a `longhorn.io` finalizer; and Longhorn's own snapshots use custom names
(`daily-ba-<uuid>`) rather than requiring a generated one. Whether a hand-applied Snapshot CR with
`createSnapshot: true` produces a snapshot was not observed here — the drill recorded in the spec
is the nearest evidence, and the first real deploy through this role is what confirms it.
