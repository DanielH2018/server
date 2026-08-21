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

A role that never declares `k8s_autodeploy_snapshot_pvcs` — everything except the thirteen roles
task 3 opted in — gets `| default([]) | length == 0`, and the include in `roles/k8s/manifests`
never runs: no extra kubectl call, no extra fact, nothing.

`roles/k8s/manifests` passes through only `volume_snapshot_claims` and `volume_snapshot_service`
as call-site `vars:` (`roles/k8s/manifests/tasks/main.yml:186`); `volume_snapshot_retain` and
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
something else, at which point the fix is adding them to the `vars:` block at
`roles/k8s/manifests/tasks/main.yml:186`, not a role default.

The one guarantee that holds regardless of what a caller passes: `volume_snapshot_retain` is
clamped to a floor of 1 at `claim.yml`'s prune (`[volume_snapshot_retain | int, 1] | max`). A
caller cannot delete the only recovery point by passing `retain: 0`, even once retain becomes
callable.

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
terraria-stats, traefik, uptime-kuma, valheim, valheim-stats, wg-easy) carry the same
manual-deploy migration risk today, unprotected by this role, exactly as they were before this
branch. Widening to all 31 was considered and deferred: the create path
here is entirely unexercised live (no Snapshot CR has been applied by this code), and widening an
unexercised new failure mode from 13 deploy paths to 31 before the first real deploy proves it
out was judged the worse risk. Follow-up, not done here.

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
clamped to a floor of 1 at the point of use. 7b reverts to the most recent snapshot, and a
retention pass that races a rollback would destroy the recovery point it exists to protect.

**The retention window holds the 3 most recent deploy runs, not the 3 most recent distinct
commits.** The per-run token (see "The snapshot name is deterministic" above) gives every run its
own snapshot, so redeploying the same commit three times fills the window with three snapshots of
that one commit and prunes out whatever came before it — including a pre-migration snapshot that
was the actual recovery point this role exists to hold. An operator who redeploys the same
commit after a migration, to pick up an unrelated config change, loses that recovery point on the
third redeploy without touching a different commit at all.

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

## What fails the deploy, and what does not

The whole role runs **before** the apply, so every failure below stops the deploy without having
changed the workload.

| condition | outcome |
|---|---|
| a claim is missing or still `Pending` | **fails** — named by claim, before anything is applied |
| `git rev-parse` returns nothing | **fails** — an undated name is one 7b cannot find |
| the volume is detached and the maintenance-mode attach also fails | **skips this claim and warns loudly** — see below |
| the volume is detached, the maintenance-mode attach succeeds, but the retaken snapshot never reports `readyToUse` | **fails** — this is now an attached, stuck engine, the same as the row below |
| the snapshot never reports `readyToUse` for any other reason (attached and stuck) | **fails** — this is the recovery point the deploy is about to need |
| the snapshot listing does not contain this run's own snapshot | **fails** — the read is broken, so the prune would be deleting from a set it cannot see |
| a delete in the prune returns non-zero | **fails** — see below |

The `markRemoved` case that used to sit in this table (a name collision with an earlier deploy's
CR) can't happen from a fresh commit any more — the per-run token makes each run's name distinct
— and is now only reachable if two runs land in the same wall-clock second; see "The snapshot
name is deterministic" above.

**`git rev-parse` is a hard prerequisite, and it is not reachable from the automated pipeline.**
It runs without `become` (a repo checkout read as root is "dubious ownership" and git refuses
it), which means it depends on the deploying user owning the checkout. `gitops-deploy.service`
runs `User=ubuntu` with `WorkingDirectory=/home/ubuntu/server`, so the refusal is not reachable
there — but a hand-run deploy as a different user, or from a checkout owned by someone else,
would fail every one of these 13 roles at this task before it fails anywhere more specific.

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
snapshot chain of all 13 protected volumes. Not worth gating on.

## A detached volume gets a maintenance-mode attach before it gets skipped

A Longhorn snapshot needs a running engine, and a workload scaled to zero has none. Two records
of the same constraint: `longhorn-reap-orphan-snapshots.sh.j2` refuses to reap on a detached
volume ("no engine to purge it", observed on `terraria-config` on 2026-08-16), and the slice 7
drill got a `500` reverting a plainly detached volume for the same reason.

**Ruling from slice 7a task 2: a detached claim does not fail the deploy.** Two realistic ways a
`Recreate` + RWO volume is detached at snapshot time, and failing the deploy would block a
legitimate action in both: an operator deliberately scaled the service to zero, or this is the
service's first-ever deploy (all 13 roles run `k8s/seed-volume` first, which deletes its seed pod
with `--wait=false`, and no Deployment has ever attached the volume yet — so a brand-new empty
volume is legitimately detached).

**Slice 7b closes the gap 7a left open.** After the first wait times out, `claim.yml` reads the
volume's own `status.state`; when it is not `attached`, the claim reuses `k8s/longhorn-api` and
the maintenance-mode attach `k8s/volume-revert` proved (`POST ?action=attach
{hostId, disableFrontend: true}`, wait for `attached` with the frontend disabled) to attach the
volume long enough to retake the same-named snapshot, then detaches it again regardless of
whether the retake succeeded. **Both of 7a's cases — the deliberate scale-to-zero and the
service's first deploy — now get a real snapshot through this attach**, not the warning. Any
other unready cause on the FIRST attempt (a same-second name collision on a `markRemoved` CR, or
a stuck engine on a volume that *is* attached) still fails the deploy, unchanged from 7a.

**The warning survives, narrowed to one case: the maintenance-mode attach itself fails.** No
longhorn-manager reachable on this node, or the attach/wait sequence timing out, both fall
through to `ansible_debug` rather than failing the deploy — a detached volume is still not itself
an error, and this attempt is best-effort on top of 7a's ruling. If the attach succeeds but the
retaken snapshot still never becomes ready, that is no longer read as the detached case at all:
it folds into the ordinary "attached and stuck" row above and fails the deploy, because by then
the volume genuinely is attached.

The gap that remains is narrower than 7a's, but still real and still visible on purpose: if the
attach itself fails, the apply that follows may scale the workload back up, the new pod can
migrate the on-disk format, and there is then no recovery point behind it. The warning names the
service, the claim, and that the deploy is proceeding unprotected — a silent skip would defeat
the point of the slice.

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

This role is **not** in `k8s_dry_run_unsupported`, and should not be added. That refusal keys on
`ansible_run_tags`, so it only reaches roles an operator names on the command line — a role
reached as a dependency is invisible to it. `seed-volume`, `image-builder` and `cronjob-gate` are
all in the same position and are guarded internally instead.

## Reverting: automated via k8s/volume-revert

A rollback is not a manual operation. `roles/k8s/manifests` includes `k8s/volume-revert`
between this role's snapshot and the apply, gated on `k8s_restore_snapshot_sha`
(`roles/k8s/manifests/tasks/main.yml:212`) — gitops-deploy sets that extra-var only when it
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

## Things measured rather than assumed

- `kubectl`'s jsonpath has **no `&&`** — `unrecognized character in action: U+0026`, verified
  2026-08-21. The snapshot listing therefore filters on `spec.volume` alone and does the rest in
  Jinja. Do not "simplify" it into a compound filter expression.
- The retention filter treats anything other than the literal `true` for `status.markRemoved` as
  not-removed — an absent or unpopulated field (a snapshot read moments after creation) counts as
  live, not as removed. Every live Snapshot CR measured on 2026-08-21 carried the field explicitly
  (28 `false` / 17 `true`), but the filter no longer depends on that being universal.
- `argv:`, not a folded `cmd:` string, on every `kubectl` call here. `ansible.builtin.command`
  shlex-splits a `cmd`, so a jsonpath is only safe there while it happens to contain no spaces,
  and the listing's `{range .items[?(...)]}` does not. Slice 4 shipped that bug and made a whole
  branch dead code for a round.
- `git rev-parse` uses `chdir`, **not** `git -C`. `git -C` does not override `GIT_DIR`, and a
  stray `GIT_DIR` has already made a check in this repo operate on the wrong repository. It also
  runs without `become`, because git refuses a repository it considers to have dubious ownership
  when a different user reads it.
- Ansible role-defaults precedence for a same-named variable across an `include_role` chain: the
  innermost (most recently loaded) role's own default wins over an ancestor's, confirmed
  2026-08-21 with a throwaway three-level play matching this repo's real call structure. See "How
  a role opts in" above for what that means for `volume_snapshot_retain`/`volume_snapshot_timeout`.

## What is unverified

`kubectl` in this repo authenticates as a read-only ServiceAccount and Ansible is the only write
path to the cluster, so the create path here has **not** been exercised end to end. What was
checked live on 2026-08-21 is the shape it depends on: `longhorn.io/v1beta2` `Snapshot` is a
served resource; live CRs carry `spec.volume`, `spec.createSnapshot`, `status.readyToUse`,
`status.markRemoved` and a `longhorn.io` finalizer; and Longhorn's own snapshots use custom names
(`daily-ba-<uuid>`) rather than requiring a generated one. Whether a hand-applied Snapshot CR with
`createSnapshot: true` produces a snapshot was not observed here — the drill recorded in this
CLAUDE.md's revert sequence is the nearest evidence, and the first real deploy through this role
is what confirms it. The prune's live behaviour (`--wait=false` + `--ignore-not-found` against a
real finalizer) is likewise unexercised, and `readyToUse` timing against the 120s ceiling is
unmeasured.
