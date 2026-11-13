# k8s/volume-snapshot — a pre-apply Longhorn snapshot for a `Recreate` + RWO role

This role deploys nothing. Its defaults feed the `<tag>-snapshot` Job that the renderer emits
for each opted-in caller. The Job takes a Longhorn snapshot of each of the caller's RWO volumes,
waits until the snapshot is usable, then prunes that service's older snapshots of the same
volume. It gives you a recovery point; `scripts/backup/volume_revert.py` consumes one.

**Why a `Recreate` + RWO role needs it:** such a role attaches its volume to a freshly-created pod
on an image bump, and the application may migrate its on-disk format before anything observes a
fault, after which redeploying the previous image is not a rollback. Neither `rollout status` nor
the stabilisation soak sees that, because a migration looks exactly like a healthy start.

**This file carries the rules; `docs/volume-snapshot-drills.md` carries the measurements.** Read
it before editing the snapshot Job, `scripts/renderer/job_files/flux_snapshot.py`.

**No standalone deploy tag.** `--tags volume-snapshot` selects nothing: the Job renders for each
opted-in role under that role's `<tag>-snapshot` Kustomization.

## How a role opts in

A `Recreate` + RWO role declares `k8s_autodeploy_snapshot_pvcs` in its own `defaults/main.yml`,
and the renderer reads that list to emit the `<tag>-snapshot` Job:

```yaml
# k8s/roles/widget/defaults/main.yml
k8s_autodeploy_snapshot_pvcs:
  - widget-config   # PVC name(s) in k8s_namespace
```

A role that declares nothing gets no Job.

- **`volume_snapshot_retain` is clamped to a floor of 2**, whatever is passed
  (`scripts/renderer/job_files/flux_snapshot.py:RETAIN_FLOOR`).

## The snapshot name is deterministic, and the revert depends on that

```
autodeploy-<tag>-<digest8>-<claim>-<token>
```

`<digest8>` is the role's spec digest. `<token>` is the UTC time the Job's pod starts, so every
claim in one run shares it. `autodeploy-<tag>-<digest8>-<claim>` survives as a **prefix**, which
`scripts/backup/volume_revert.py` reconstructs and matches on.

- **The token makes the prefix a non-unique lookup**, so the revert picks the NEWEST match.
- **Renaming a service strands its old snapshots permanently**: the prune selects on
  `autodeploy-<tag>-`, and the cluster-wide reaper skips a snapshot with no `RecurringJob`
  label, which no `autodeploy-*` snapshot carries.
- **A name must stay under 63 bytes.** Past that, Longhorn's webhook denies every DELETE through
  every route and nothing removes the CR. Thirteen needed a webhook bypass by hand (#2734).

## Pruning is asynchronous, and a lingering CR is normal

A Snapshot CR carries a `longhorn.io` finalizer, so a delete sets `markRemoved: true` and waits for
the volume to coalesce. The CR persists afterwards and the name stays taken, so "the CR is gone" is
never the success condition. `markRemoved` snapshots are dropped from the retention window rather
than counted in it, because counting dead CRs as retained deletes live snapshots to make room.

## What fails the Job

The Job runs **before** `<tag>` applies, because `<tag>` depends on `<tag>-snapshot`, so a failure
stops the revision without having changed the workload. Every claim is resolved, named and checked
against its snapshot-space cap before any snapshot is created. A claim whose PVC is absent live
and absent from `<tag>`'s inventory is new in this revision, and the Job skips it. A PVC the
inventory lists and the cluster lacks fails the Job, naming it.

Longhorn v1.12.1 snapshots a detached volume with healthy replicas, so a detached claim takes the
ordinary path (#2740).

## Reverting through volume_revert.py

**No automated rollback runs.** `scripts/backup/volume_revert.py` runs only from the operator's runbook.

**The revert is an operator-only runbook.** The `<tag>-snapshot` Job takes
the snapshot in this role's name shape, with the role's `digest8` in place of `<sha8>`. `promote`
runs in CI and cannot reach Longhorn, so a failed revision that changes a snapshot role holds as
`<T> stateful=<names>` and leaves `prod` on T. The hold's Discord post carries each changed
role's revert commands, and the operator reverts each changed snapshot role through
`scripts/backup/volume_revert.py` before the reset. The runbook is *The revert runbook* in
`docs/volume-snapshot-drills.md`, and `docs/flux-deploy-plane.md`'s *Snapshot roles hold without
a reset* has the rule.
