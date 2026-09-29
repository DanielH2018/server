# k8s/volume-snapshot — a pre-apply Longhorn snapshot for a `Recreate` + RWO role

This role deploys nothing. `roles/k8s/manifests` includes it immediately **before**
`Apply manifests for {{ manifests_service }}`, and it takes a Longhorn snapshot of each of the
caller's RWO volumes, waits until the snapshot is usable, then prunes that service's older
snapshots of the same volume.

**Read this first: the role gives you a recovery point, and `k8s/volume-revert` consumes it.**
`roles/k8s/manifests` includes `k8s/volume-revert` between this role and the apply, gated on
`k8s_restore_snapshot_sha` — an extra-var gitops-deploy sets only on a rollback redeploy of a
failed auto-deploy's prior commit. Nothing in THIS role decides to revert or detects a bad
deploy; it only makes the damage reversible. The drill-proven sequence, the manual recovery
steps for a partial multi-claim revert, and what is and is not covered by tests all live in
`k8s/volume-revert`'s own CLAUDE.md — see "Reverting: automated via k8s/volume-revert" below for
the trigger and the pointer.

**This file carries the rules; `docs/volume-snapshot-drills.md` carries the measurements.** Drill
records, the traps the `kubectl` calls are written around, and what stays unverified live there
(#2699); read that page before editing `tasks/claim.yml`.

**No standalone deploy tag.** `roles/k8s/manifests` includes `k8s/volume-snapshot`
automatically for an opted-in caller, so the role only ever runs with a caller's claims.
`deploy.sh --tags volume-snapshot` therefore deploys every service that includes `manifests`,
which is most of the fleet (#2704).

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

A role that never declares `k8s_autodeploy_snapshot_pvcs` — everything except the fourteen opted-in
roles (thirteen from task 3, plus karakeep in `c49d5c4a4`) — gets `| default([]) | length == 0`, and the include in `roles/k8s/manifests`
never runs: no extra kubectl call, no extra fact, nothing.

`roles/k8s/manifests` passes through only `volume_snapshot_claims` and `volume_snapshot_service`
as call-site `vars:` (the `vars:` of the `Snapshot the stateful volumes` task in
`ansible/roles/k8s/manifests/tasks/main.yml`); `volume_snapshot_retain` and
`volume_snapshot_timeout` are not wired there. **Measured, not assumed: a caller CANNOT change
them by declaring same-named defaults of its own.** `k8s/volume-snapshot`'s own
`defaults/main.yml` sets both, and it is the role actually executing when they're read — in a
three-level `include_role` chain (caller → `k8s/manifests` → `k8s/volume-snapshot`) built to
match this repo's real call structure, the innermost role's own default won a same-named
collision against an ancestor's, every time, confirmed 2026-08-21 with a throwaway play. This is
why `k8s_autodeploy_snapshot_pvcs` reaches the include cleanly — `k8s/volume-snapshot` never
declares that name itself, so there is no collision to lose — while `volume_snapshot_retain`
would silently stay at this role's own default even if a caller set it as its own role default.
`group_vars`, `host_vars` and `-e` all outrank a role default in Ansible's precedence order, so
each of those DOES override `volume_snapshot_retain`/`volume_snapshot_timeout` — the thing a
caller role's own `defaults/main.yml` cannot do is set them. The floor clamp described below holds
regardless of which of those set the value, including `-e volume_snapshot_retain=0`. The two knobs
stay at `volume_snapshot_retain: 3`, `volume_snapshot_timeout: 120` until a caller genuinely needs
something else, at which point the fix is adding them to the `vars:` block of that
`Snapshot the stateful volumes` task, not a role default.

The one guarantee that holds regardless of what a caller passes: `volume_snapshot_retain` is
clamped to a floor of 2 at `claim.yml`'s prune (`[volume_snapshot_retain | int, 2] | max`). A
caller cannot prune down to a single recovery point by passing `retain: 0` or `1`, even once
retain becomes callable — see "The revert needs two, not one" below for why the floor is 2, not
1.

## Why a `Recreate` + RWO role needs this

A `Recreate` + RWO role attaches its Longhorn volume to a freshly-created pod on an image bump —
the old pod is deleted first, the new one attaches the same volume — and the application may
migrate its on-disk format before anything observes a fault. After that, redeploying the
previous image is not a rollback: the old binary can no longer read its own data.

Neither existing gate sees it. `rollout status` and the stabilisation soak in
`roles/k8s/manifests` both watch for a pod that starts and stays up — and a schema migration is
exactly what a healthy start looks like. The failure surfaces later, as corrupt data or as a
failed downgrade, at which point there is nothing to go back to.

**Scope for this slice: 13 of 31.** Measured 2026-08-21, 31 roles in this repo carry the
`Recreate` + rendered-RWO-claim shape this role exists for; task 3 declared
`k8s_autodeploy_snapshot_pvcs` for 13 of them — the ones drawn from the auto-deploy promotion
criteria, not from a data-migration survey. The other 18 (authelia, claude-otel, crowdsec,
healthchecks, karakeep, loki-homelab, mosquitto, n8n, pihole, registry, scrutiny, terraria,
terraria-stats, traefik, uptime-kuma, valheim, valheim-stats, wg-easy) carried the same
manual-deploy migration risk; karakeep opted in later (`c49d5c4a4`), so 17 remain. Widening
was deferred on 2026-08-21 because the create path had not yet run live. It has since; see
*What is unverified* in `docs/volume-snapshot-drills.md`.

## The snapshot name is deterministic, and 7b depends on that

```
autodeploy-<service>-<sha8>-<claim>-<token>
```

`<sha8>` is `git rev-parse --short=8 HEAD` in the repo the deploy renders from, resolved once in
`tasks/main.yml`. `<token>` is `now(utc=true, fmt='%Y%m%d%H%M%S')`, also resolved once in
`tasks/main.yml` — before the per-claim loop, so every claim in one role run shares it, and the
wait a few tasks later in `claim.yml` polls for the exact name the apply task in the same pass
created.

Slice 7b has to find this snapshot without being told what it was called, so
`autodeploy-<service>-<sha8>-<claim>` — the design's original name, with the claim suffix added
because a service with two RWO claims (pihole has exactly that) would otherwise produce two
Snapshot CRs fighting over one name — survives verbatim as a **prefix**. 7b reconstructs that
string and matches on prefix, not equality.

**The token exists so a rollback deploy can't be refused by its own protection step.**
Redeploying an older commit is the manual rollback this slice exists to enable, and before the
token existed, its snapshot name was fully deterministic from service + tag + claim alone — so
redeploying a SHA a second time named the exact same CR the first deploy of that SHA already
created. That CR can be `markRemoved` but not yet gone (the finalizer only clears at the next
detach), and `apply` against it either silently reused the earlier deploy's stale recovery point
or failed against the `markRemoved` CR. The token fixes the collision by making every run's name
distinct.

**The consequence: `autodeploy-<service>-<sha8>-<claim>` is no longer a unique lookup.** One SHA
can now own several snapshots sharing that prefix — a genuine rollback redeploy is one way to get
there, a dirty tree deployed twice from the same commit is another (the SHA names the last
commit, not the working tree, so both deploys claim the same tag while the tree differs). **7b
must pick the newest of however many matches it finds, not assume the match is unique.**

The one case the token doesn't separate: two runs of this role landing in the same wall-clock
second get the identical token and therefore the identical name. `apply` there reports
"unchanged" and the earlier of the two stands as the recovery point for both — a retry a moment
later gets a fresh token and a fresh snapshot.

## Pruning is asynchronous, and a lingering CR is normal

A Snapshot CR carries a `longhorn.io` finalizer. Deleting it sets `markRemoved: true` and waits
for the volume to coalesce the data. Two consequences the prune is written around:

- **`kubectl delete` blocks by default** — measured hanging a drill run for twelve minutes on
  2026-08-21. The delete here always passes `--wait=false`.
- **The CR persists after a successful delete, so the name stays taken.** A snapshot whose
  only child is `volume-head` cannot be folded away while the volume is attached at all; it
  clears at the next detach. `kubectl delete snapshots.longhorn.io <name>` therefore does not
  free the name. So "the CR is gone" is never the success condition, and a `markRemoved` CR
  sitting there is the expected state, not a failed prune.

`markRemoved` snapshots are dropped from the retention window rather than counted in it. Counting
three dead CRs as the retained three would make the next pass delete live snapshots to make room.

**The newest snapshot is never pruned, whatever `volume_snapshot_retain` says.** The value is
clamped to a floor of 2, not 1. 7b reverts to the most recent snapshot, and a retention pass
that races a rollback would destroy the recovery point it exists to protect.

### The revert needs two, not one

A floor of 1 was the original design and was wrong: it protects against the wrong run. **A
rollback redeploy takes its OWN snapshot before it prunes, and prunes BEFORE `k8s/volume-revert`
reads the chain** — `roles/k8s/manifests/tasks/main.yml` runs "Snapshot the stateful volumes"
(which includes this role, prune and all) immediately before "Revert the stateful volumes"
(`k8s/volume-revert`), in that order, every time a claim is declared, whether or not
`k8s_restore_snapshot_sha` is set. So at the moment THIS run's prune executes, the chain holds
two snapshots that both matter: the one just taken of the current (already-migrated) data, and
the earlier pre-deploy snapshot the revert step right after it is about to need. A floor of 1
keeps only the newest — this run's own — and deletes the recovery point out from under the
revert that immediately follows it in the same play. The floor is 2 so both always survive one
run's own prune, regardless of what `volume_snapshot_retain` is set to.

**The retention window holds the 3 most recent deploy runs, not the 3 most recent distinct
commits.** The per-run token (see "The snapshot name is deterministic" above) gives every run its
own snapshot, so redeploying the same commit three times fills the window with three snapshots of
that one commit and prunes out whatever came before it — including a pre-migration snapshot that
was the actual recovery point this role exists to hold. An operator who redeploys the same
commit after a migration, to pick up an unrelated config change, loses that recovery point on the
third redeploy without touching a different commit at all.

**This is not hypothetical for a rollback specifically.** `gitops-deploy`'s hold only skips the
exact held SHA (`skip_hold` matches `origin_head == hold_sha`); redeploying that same failed
commit again — including a hand-run `./scripts/deploy.sh --tags <service>` while an operator
debugs a partial revert per `k8s/volume-revert/CLAUDE.md`'s recovery steps — takes another fresh
snapshot of it under the same commit's tag. A third such redeploy of the SAME failed SHA (at the
default `retain: 3`) prunes the ORIGINAL pre-deploy snapshot out of the window, and any further
automated rollback of that commit then finds no snapshot to revert to — the fail-closed "no
snapshot matches" error in `k8s/volume-revert` is correct in that case, not a bug, but it means
the recovery point is gone for good, not merely unreachable this run.

The trade was made deliberately, not overlooked: without the token, redeploying the same commit
named the exact same CR its own earlier deploy already created, and `apply` against a CR that
could be `markRemoved`-but-not-gone either silently reused a stale recovery point or failed the
deploy outright. The token fixes the collision, but a window counted in runs rather than commits
is the cost of fixing it this way.

**Known cost, not fixed here: renaming a service strands its old snapshots permanently.** The
prune selects on `autodeploy-<service>-`, and `longhorn-reap-orphan-snapshots.sh.j2` — the
cluster-wide orphan reaper — skips any snapshot carrying no `RecurringJob` label, which every
`autodeploy-*` snapshot does by construction. Rename a service and its snapshots under the old
name become invisible to both this role's own prefix filter and the reaper: nothing prunes them,
nothing reaps them, and they pin their blocks against `filesystem trim` forever. Not touched in
this slice.

### The 13 over-long CRs the prune retries and Longhorn refuses, removed by hand 2026-09-27

**They are gone.** The operator removed all thirteen on 2026-09-27 (#2734), bypassing the webhook
for exactly those objects; *How they were removed* below has the method. Nothing refused remains,
so the prune's tolerated-rejection branch and the `Report snapshots Longhorn refuses to delete`
task have no live target. The rest of this section records why they could not be deleted
through any supported route, which still holds for any future over-long name.

Thirteen `autodeploy-*` Snapshot CRs created on 2026-08-22 across four volumes
(`code-server-config`, `code-server-workspace`, `home-assistant-config`, `qbittorrent-config`)
could not be deleted through the Kubernetes API at all. On 2026-09-26 they read
`status.readyToUse: false`, most carrying `status.error` "lost track of the corresponding
snapshot info inside volume engine". Issue #2686 filed them as never pruned. The prune did
reach them; the delete was what failed. Eleven were `markRemoved: false` with the lost-track
error. The other two, both `code-server-workspace`, carried `markRemoved: true`.

**The cause is the 63-byte name ceiling, and these CRs predate its fix by hours.** `0c0317a77`
(2026-08-22) dropped the redundant `<service>-` from the claim segment. Before it those four
claims rendered names of 65, 68, 71 and 65 bytes; after it, 53, 56, 56 and 53.

**The prune selected them on every deploy.** It filters the live listing on `markRemoved` and
the `autodeploy-<service>-` prefix — never on `readyToUse` — so an error-state CR with
`markRemoved` unset is in `volume_snapshot_live`, sorts oldest under the newest-first order, and
lands in the slice past `volume_snapshot_retain`. The `kubectl delete` is issued, the webhook
denies it, and the prune's `failed_when` tolerates exactly that one message. Retention stayed
intact — the thirteen consumed no slot in the window — which is why this stayed invisible: the
only signal was the `Report snapshots Longhorn refuses to delete` debug line, and under the
GitOps deployer the play output reaches nobody. The two `markRemoved: true` CRs left the
candidate set instead, so the prune never retried those two.

**Teaching the orphan reaper the `autodeploy-` prefix would not have helped.** It skipped them
for the reason the rename bullet above gives, and it deletes through `kubectl` as well, so it
would have hit the same webhook.

**No supported route removes an over-long CR on Longhorn v1.12.1, the manager API included.**
Read from the `longhorn-manager` v1.12.1 source on 2026-09-27:

- Only a Kubernetes DELETE sets the `deletionTimestamp` that the snapshot controller's
  finalizer removal waits on.
- The validator's `Delete` hook calls `IsSnapshotLinkedCloneEntrypoint`, which builds a label
  selector from the raw snapshot name. That selector is invalid past 63 bytes, so the webhook
  denies every DELETE, whatever client sends it.
- The manager API's `snapshotDelete` action (`VolumeManager.DeleteSnapshot`) deletes from the
  engine's chain and never touches the CR. The engine no longer held these snapshots, so it had
  nothing to delete. The UI's per-volume delete makes the same call. The refused-delete report in
  `claim.yml` still names this route; #2733 corrects it.
- The manager API's `snapshotCRDelete` action goes through `DeleteSnapshotCR`, which is a
  Kubernetes DELETE and hits the same webhook.
- The controller sets the lost-track error and nothing else. It never removes such a CR itself.

Upstream master still builds the selector from the raw name, so an upgrade does not fix this
either. `claim.yml`'s name-length assert is what keeps new snapshots under the ceiling.

**They pinned no blocks.** Measured 2026-09-27, before the removal: none of the thirteen names
appeared in `status.snapshots` of any of the four volumes' running engines. The controller sets
the lost-track error exactly when a CR's name is missing from that map. One `markRemoved` CR
still reported `size: 344064`, which is a stale status field rather than blocks in the chain.

**How they were removed.** An operator-approved scratch play, run from a direct operator session
because the auto-mode classifier refuses a webhook bypass from an agent session:

1. It asserts the targets by name, and asserts the `longhorn-webhook-validator` webhook's
   `objectSelector` is `{}`.
2. It labels only the targets, which is an UPDATE the validator allows.
3. It sets the webhook's `objectSelector` to skip that label. For a DELETE, the selector matches
   the old object, so every other object in the cluster stays validated.
4. It deletes the targets by exact name, then restores `objectSelector: {}` in an `always` block
   and asserts the restore.

The webhook configuration is applied by longhorn-manager through wrangler's `objectset` with the
`longhorn-webhook-ca` Secret as owner. longhorn-manager re-applies it on start or on a CA change,
not on a short loop, so nothing raced the patch and the restore was the play's job. Two runs, both
`failed=0` with nothing left afterwards: the eleven lost-track CRs, then the two `markRemoved`
ones.

## What fails the deploy, and what does not

The whole role runs **before** the apply, so every failure below stops the deploy without having
changed the workload.

| condition | outcome |
|---|---|
| a claim is missing or still `Pending` | **fails** — named by claim, before anything is applied |
| `git rev-parse` returns nothing | **fails** — an undated name is one 7b cannot find |
| the snapshot never reports `readyToUse`, whatever the volume's state | **fails** — this is the recovery point the deploy is about to need |
| the snapshot listing does not contain this run's own snapshot | **fails** — the read is broken, so the prune would be deleting from a set it cannot see |
| a delete in the prune returns non-zero | **fails** — see below |

The `markRemoved` case that used to sit in this table (a name collision with an earlier deploy's
CR) can't happen from a fresh commit any more — the per-run token makes each run's name distinct
— and is now only reachable if two runs land in the same wall-clock second; see "The snapshot
name is deterministic" above.

**`git rev-parse` is a hard prerequisite, and it is not reachable from the automated pipeline.**
It runs without `become` (a repo checkout read as root is "dubious ownership" and git refuses
it), which means it depends on the deploying user owning the checkout — the **controller's**,
since the task is delegated to localhost. `gitops-deploy.service` runs `User=ubuntu` with
`WorkingDirectory=/home/ubuntu/server`, so the refusal is not reachable there — but a hand-run
deploy as a different user, or from a checkout owned by someone else, would fail every one of
these 14 roles at this task before it fails anywhere more specific.

The last table row is a deliberate choice rather than an oversight. A prune failure is not itself
dangerous, but swallowing it means unbounded snapshot growth, and a retained snapshot pins every
block beneath it against `filesystem trim` — the mechanism
`roles/setup/k3s/templates/longhorn-reap-orphan-snapshots.sh.j2` exists to clean up after. Since
the prune runs before the apply, failing costs a deploy that has not started rather than a
half-deployed service.

**Also accepted: this role runs on a no-op deploy too.** gitops-deploy only deploys services that
actually changed, so this cost is rare in the automated pipeline. A manual deploy is a different
story: each run gets its own token (see "The snapshot name is deterministic" above), so a no-op
manual deploy of an already-up-to-date service still creates a real Snapshot CR and runs a real
prune against it, not a skipped no-op. Running a full manual deploy twice in one day churns the
snapshot chain of every protected volume. Not worth gating on.

## A detached volume is snapshotted on the ordinary path

Longhorn v1.12.1 completes a snapshot of a detached volume with healthy replicas, so a detached
claim needs nothing special from this role. `tasks/claim.yml` treats it as any other claim: the
apply, the one readiness wait, then the prune.

**Two drills measured it, covering both ways a volume reaches this role detached.** The
2026-08-21 task-6 drill snapshotted a volume that had been attached before; the #2698 drill on
2026-09-27 snapshotted a throwaway PVC no pod had ever mounted, `readyToUse=true` about 13 s
after the PVC was created. `docs/volume-snapshot-drills.md` holds both records.

**#2740 retired the maintenance-mode attach those drills made dead code.** Between 2026-08 and
2026-09-27 a claim whose first wait timed out read the volume's `status.state`, and a state other
than `attached` triggered an attach through `k8s/longhorn-api` with `disableFrontend: true`, a
retake, a detach, and — when the attach itself failed — a warning that the deploy was proceeding
with no recovery point. No deploy ever reached it. The retirement also removed the `faulted` /
`attaching` / `detaching` ambiguity that block carried: those states were read as "detached" and
took the same unprotected path as a genuinely detached volume.

**A detached claim that cannot be snapshotted now fails the deploy**, at "did not report
readyToUse", like any other unready snapshot. `claim.yml` still reads `status.state` after a
failed wait and names it in that failure, which is what tells an operator that Longhorn stopped
snapshotting a detached volume rather than that an engine wedged.

## The guard is `k8s_no_mutate`, and neither `--check` nor `--dry-run` exercises this role at all

Every mutating task carries `when: not (k8s_no_mutate | bool)`.
`inventory/group_vars/all.yml` defines that as `ansible_check_mode or (k8s_dry_run | bool)`, and
the reason it is one fact rather than two is that a role guarding either alone is guarded against
neither.

**The call site skips the whole role, not just its mutations.** The include in
`roles/k8s/manifests/tasks/main.yml` is itself gated `when: not (k8s_no_mutate | bool)` — so under
`--check` or `--dry-run`, `k8s/volume-snapshot` never starts. Nothing here — not the deploy-tag
read, not the PVC lookup, not the assert that catches a typo'd claim name — runs under either
mode. A wrong claim name in `k8s_autodeploy_snapshot_pvcs` surfaces only on a real deploy, as the
"PVC has no spec.volumeName" assert failing before the apply.

The internal `k8s_no_mutate` guards on every task inside this role (the apply, the wait, the
prune, and the two reads' own `check_mode: false`) are defence in depth for a future call site
that includes this role unconditionally — they are not exercised by anything in this repo today,
because the one call site that exists already keeps this role from starting under `--check` or
`--dry-run`.

This role is reached as a dependency rather than named on the command line, so a tag-keyed
refusal could never have covered it — which is why it guards itself. `volume-claim`,
`image-builder` and `cronjob-gate` are all in the same position. The `k8s_dry_run_unsupported`
list that once held the alternative was deleted empty in #2876.

## Reverting: automated via k8s/volume-revert

A rollback is not a manual operation. `roles/k8s/manifests` includes `k8s/volume-revert`
between this role's snapshot and the apply, gated on `k8s_restore_snapshot_sha`
(the `Revert the stateful volumes` task in `ansible/roles/k8s/manifests/tasks/main.yml`) —
gitops-deploy sets that extra-var only when it
redeploys a failed auto-deploy's prior good commit, and it carries the FAILED commit's SHA, not
the tree's current one (the tree is already reset to the last good commit at that point). The
claim list comes from the current, rolled-back-to tree's `k8s_autodeploy_snapshot_pvcs`, not
from the failed commit — if the failed commit renamed or added a claim, `k8s/volume-revert`'s
own "no snapshot matches this deploy" assert fires loud and before anything moves, rather than
silently skipping.

`k8s/volume-revert`'s own CLAUDE.md is the source of truth for the sequence, the two drilled
facts that make it work (frontend must be disabled, a plainly detached volume also fails), the
recovery steps for a partial multi-claim revert, and what is and is not exercised by tests. Read
it there rather than a second copy here — this role only takes the snapshot the revert consumes.

If more than one snapshot matches the reconstructed `autodeploy-<service>-<sha8>-<claim>` prefix
(see "The snapshot name is deterministic" above), `k8s/volume-revert` reverts to the **newest**
one, never by assuming the match is unique.

## The drill record is in docs/, not here

`docs/volume-snapshot-drills.md`, split out 2026-09-26 (#2699) so this file stays under the
role-doc ceiling. Four sections: the detached-volume reasoning and why its premise is false on
Longhorn v1.12.1; the 2026-08-21 task-6 drill and which of its two reasons still holds (#2681,
and the 2026-09-27 #2698 drill that measured the never-attached case); *Things measured rather than assumed*, the six
traps to read **before editing the `kubectl` calls or the registers** here; and what is still
unverified.
