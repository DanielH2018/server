# k8s/volume-revert — put a service's volumes back to the pre-deploy snapshot

This role deploys nothing. It reverts each of a service's Longhorn volumes to the snapshot
`k8s/volume-snapshot` took immediately before the deploy that failed, so that the rollback's
apply restores manifests over data those manifests can actually read.

The drill timings, the timeout derivation, the longhorn-manager behaviour behind the attach and
detach, the hand-recovery steps and what no test covers are in
`docs/volume-revert-drill-and-sizing.md`.

**Read this first: the role stops the workload and does not start it again.** It scales the
Deployment to zero, reverts, and leaves it there. The apply that follows in the rollback
restores `replicas: 1` from the manifest — one rollout instead of two, and no race between
them. A caller that reverts without applying afterwards leaves the service down.

**No standalone deploy tag.** Callers reach `k8s/volume-revert` via `include_role`, keyed off
`volume_revert_sha`, not `--tags volume-revert`; see `defaults/main.yml`.

**This code path runs only during an incident**, and nothing exercises it on a good day. That is
why every step that can fail on its own account is checked before the scale-down, and why
`ansible/tests/longhorn/test_volume_revert.py` pins claim.yml's whole sequence. Two transpositions
that look harmless are outages: scaling down AFTER the maintenance-mode attach leaves the pod
holding the volume, and detaching BEFORE the revert gives the revert a volume with no engine.

**"Checked before the scale-down" is a per-claim guarantee, not a per-service one.** main.yml
includes claim.yml once per claim, so on a two-claim service the second claim's checks run after
the first already scaled the Deployment to zero. A failure there finds the service already DOWN,
and the failure message says so.

## What the caller passes

```yaml
- name: Revert the volumes for widget
  ansible.builtin.include_role:
    name: k8s/volume-revert
  vars:
    volume_revert_service: widget          # also the Deployment name
    volume_revert_claims: [widget-config]  # PVC names in k8s_namespace
    volume_revert_sha: 1a2b3c4d            # the deploy tag the snapshot was named with
```

`volume_revert_claims` must be a **list**, not a bare string: `"tdarr-configs" | length` is
13, so a string passes a length check and Ansible then loops its characters, looking up a PVC
named `t`. The input assert rejects it by type.

`volume_revert_sha` must be the **same string** `k8s/volume-snapshot` resolved for the deploy
being rolled back — `git rev-parse --short=8 HEAD`'s output at that commit, used verbatim.
`--short=8` is a minimum, not a width: git returns more characters when eight are ambiguous, so
this role checks the shape (`^[0-9a-f]{8,}$`) and transforms nothing, and truncating to eight
would fail to match a nine-character name. `gitops-deploy`'s automated rollback is the one caller
that passes a fixed 8-character slice instead; the docs page has why that stays fail-closed.

## The sequence, and why every step is there

Measured 2026-08-21 on `speedtest-config`, Longhorn v1.12.1. Two plausible shortcuts were
measured and both fail:

| volume state | revert result |
|---|---|
| attached, frontend enabled | `500 failed to revert snapshot for volume … with frontend enabled` |
| plainly detached | `500` — no engine is running, so nothing can perform the revert |
| attached with `disableFrontend: true` | works |

Per claim, in this order:

1. **Resolve the PVC's Longhorn volume.** Also the only proof the claim exists and is bound.
2. **Find the newest snapshot matching `autodeploy-<svc>-<sha>-<claim>-`, and fail when none
   does.** Newest by `creationTimestamp`, never by name — CR names are not chronologically
   sortable as strings. `markRemoved` snapshots are rejected, because longhorn-manager refuses
   one itself (`not revert to snapshot ... since it's marked as Removed`).
3. **Scale the Deployment to zero.** The volume cannot be attached in maintenance mode while a
   pod holds it.
4. **Wait for `state: detached`.**
5. **`POST ?action=attach {hostId, disableFrontend: true}`** — maintenance mode.
6. **Wait for `state: attached`, then assert `spec.disableFrontend` is true.** The assert is a
   precondition, not a formality: without it the revert fails at step 7, with the workload already
   at zero and the message naming the wrong thing.
7. **`POST ?action=snapshotRevert {name}`**, demanding HTTP 200.
8. **`POST ?action=detach {}`, then wait for `detached`.** That wait is the only check that a 200
   detach detached, so never suppress it with `failed_when: false`. Neither call sends an
   `attachmentID`, and adding one to the attach alone would leave the volume attached with its
   frontend disabled — the docs page reads longhorn-manager's own code for both.

There was a ninth step until 2026-09-01, stripping the PVC's
`homelab.daniel-hunter.com/seeded` annotation; it went when the seeding it reversed did. The docs
page has the reasoning.

## The guard is `k8s_no_mutate`, and neither `--check` nor `--dry-run` reaches this role

Steps 1 and 2 are reads and carry `check_mode: false`, but that only matters if the role starts at
all. `roles/k8s/manifests/tasks/main.yml` gates the whole `include_role: k8s/volume-revert` on
`when: not (k8s_no_mutate | bool)`, which is `ansible_check_mode or (k8s_dry_run | bool)`. So
under `--check` or `--dry-run` the role never starts — not the PVC lookup, not the snapshot
listing, not the `Fail when no snapshot matches this deploy` task. **Running a dry run and getting
silence proves nothing about whether a snapshot exists.** `k8s/volume-snapshot`'s CLAUDE.md
documents the identical trap for its own call site.

The role keeps its own per-task guards anyway, as defence in depth for a future call site that
includes it unconditionally. Guarding on either half of `k8s_no_mutate` alone is the bug the guard
exists to prevent, and `ansible/tests/longhorn/test_volume_revert.py` pins the whole census of
mutating tasks against it. The docs page has why the guard is per-task rather than tag-keyed, and
what the `k8s_no_mutate: true` branch's tests do and do not prove.

## A multi-claim revert is not atomic

`tdarr` (`tdarr-configs`, `tdarr-server`) and `code-server` (`code-server-config`,
`code-server-workspace`) each hold two claims. Each reverts separately, and a failure on the
second stops the play with the first already reverted — deliberate, because continuing past a
failed revert would be worse, but it leaves a service's volumes mutually inconsistent. The
hand-recovery steps are on the docs page; a successful revert leaves no maintenance-mode flag
behind, and a failed one can.
