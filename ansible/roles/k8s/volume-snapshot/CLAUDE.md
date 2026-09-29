# k8s/volume-snapshot — a pre-apply Longhorn snapshot for a `Recreate` + RWO role

This role deploys nothing. `roles/k8s/manifests` includes it immediately **before** the apply, and
it takes a Longhorn snapshot of each of the caller's RWO volumes, waits until the snapshot is
usable, then prunes that service's older snapshots of the same volume. It gives you a recovery
point; `k8s/volume-revert`, included between it and the apply, consumes one.

**Why a `Recreate` + RWO role needs it:** such a role attaches its volume to a freshly-created pod
on an image bump, and the application may migrate its on-disk format before anything observes a
fault, after which redeploying the previous image is not a rollback. Neither `rollout status` nor
the stabilisation soak sees that, because a migration looks exactly like a healthy start.

**This file carries the rules; `docs/volume-snapshot-drills.md` carries the measurements** — the
drill records, the traps the `kubectl` calls are written around, the variable-precedence
measurement and the 13 over-long CRs Longhorn refused. Read it before editing `tasks/claim.yml`.

**No standalone deploy tag.** The include fires for an opted-in caller, so
`deploy.sh --tags volume-snapshot` deploys most of the fleet (#2704).

## How a role opts in

A `Recreate` + RWO role does **not** include this role. It declares
`k8s_autodeploy_snapshot_pvcs` in its own `defaults/main.yml`, and the shared include in
`roles/k8s/manifests/tasks/main.yml` picks it up, because role defaults are in scope for an
included role:

```yaml
# roles/k8s/widget/defaults/main.yml
k8s_autodeploy_snapshot_pvcs:
  - widget-config   # PVC name(s) in k8s_namespace
```

A role that declares nothing gets `length == 0` and the include never runs. Fourteen roles are
opted in, of the 31 carrying this shape.

- **A caller cannot change `volume_snapshot_retain` or `volume_snapshot_timeout` from its own
  `defaults/main.yml`** — this role's own default wins that collision, measured. `group_vars`,
  `host_vars` and `-e` do override them; a caller needing another value adds it to the `vars:` of
  the `Snapshot the stateful volumes` task.
- **`volume_snapshot_retain` is clamped to a floor of 2**, whatever is passed. Two, not one,
  because a rollback redeploy takes its own snapshot and prunes BEFORE `k8s/volume-revert` reads
  the chain, so both must survive one run's own prune.

## The snapshot name is deterministic, and the revert depends on that

```
autodeploy-<service>-<sha8>-<claim>-<token>
```

`<sha8>` is `git rev-parse --short=8 HEAD`'s output **used verbatim** — `--short=8` is a minimum
width, not a fixed one, and truncating a longer abbreviation back to 8 would build a prefix that no
longer matches. `<token>` is a UTC timestamp. Both are resolved once in `tasks/main.yml` before the
per-claim loop, so every claim in one run shares them and the wait
polls for the name the apply just created. `autodeploy-<service>-<sha8>-<claim>` survives as a **prefix**,
which `k8s/volume-revert` reconstructs and matches on.

- **The token keeps a rollback deploy from being refused by its own protection step**, and makes
  the prefix a non-unique lookup — so the revert picks the NEWEST match. Two runs in one second
  are the case it does not separate.
- **Renaming a service strands its old snapshots permanently**: the prune selects on
  `autodeploy-<service>-`, and the cluster-wide reaper skips a snapshot with no `RecurringJob`
  label — which no `autodeploy-*` snapshot carries.
- **A name must stay under 63 bytes.** Past that, Longhorn's webhook denies every DELETE through
  every route and nothing removes the CR; `claim.yml`'s length assert is what keeps a new snapshot
  deletable. Thirteen needed a webhook bypass by hand (#2734).

## Pruning is asynchronous, and a lingering CR is normal

A Snapshot CR carries a `longhorn.io` finalizer, so a delete sets `markRemoved: true` and waits for
the volume to coalesce. Hence: the delete always passes `--wait=false` (it once hung a drill for
twelve minutes); the CR persists afterwards and the name stays taken, so "the CR is gone" is never
the success condition; and `markRemoved` snapshots are dropped from the retention window rather
than counted in it, because counting dead CRs as the retained three deletes live snapshots to make
room. **The window holds the 3 most recent deploy RUNS, not commits** — the accepted cost of the
per-run token.

## What fails the deploy, and what does not

The whole role runs **before** the apply, so every failure below stops the deploy without having
changed the workload: a claim missing or still `Pending`, an empty `git rev-parse`, a snapshot that
never reports `readyToUse`, a listing that does not contain this run's own snapshot, or a non-zero
delete in the prune. The last is deliberate — swallowing it means unbounded snapshot growth, and a
retained snapshot pins every block beneath it against `filesystem trim`.

**`git rev-parse` runs without `become`**, because git refuses a checkout read as root as "dubious
ownership", so a hand-run deploy from someone else's checkout fails every opted-in role there.
**This role also runs on a no-op deploy**, creating a real CR and a real prune.

## A detached volume is snapshotted on the ordinary path

Longhorn v1.12.1 snapshots a detached volume with healthy replicas, so `claim.yml` treats a
detached claim as any other: apply, one readiness wait, prune (#2740 retired the maintenance-mode
attach two drills made dead code). **A detached claim that cannot be snapshotted fails the
deploy**, and `claim.yml` names the volume's `status.state` in that failure — which is what
distinguishes Longhorn refusing a detached snapshot from a wedged engine.

## The guard is `k8s_no_mutate`, and neither `--check` nor `--dry-run` exercises this role

Every mutating task carries `when: not (k8s_no_mutate | bool)` —
`ansible_check_mode or (k8s_dry_run | bool)`, one fact rather than two, because a role guarding
either alone is guarded against neither. **The call site skips the whole role, not just its
mutations**, so **a wrong claim name surfaces only on a real deploy**, as the "PVC has no
spec.volumeName" assert failing before the apply.

## Reverting: automated via k8s/volume-revert

A rollback is not a manual operation. `roles/k8s/manifests` includes `k8s/volume-revert` between
this role's snapshot and the apply, gated on `k8s_restore_snapshot_sha` — which gitops-deploy sets
only when it redeploys a failed auto-deploy's prior good commit, and which carries the FAILED
commit's SHA. The claim list comes from the rolled-back-to tree, so a claim the failed commit
renamed makes that role's "no snapshot matches this deploy" assert fire before anything moves.
