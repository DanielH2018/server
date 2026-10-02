# volume-snapshot: the drill record

The measurements behind `k8s/volume-snapshot`, and the traps its
`kubectl` calls and Ansible registers are written around. The operating rules — what the role
does on each path, what fails a deploy, what a caller can and cannot override — are in
`ansible/roles/k8s/volume-snapshot/CLAUDE.md`, which is the file a session loads before touching
the role. This page is where the numbers came from, kept out of that file so the rule stays
short and the evidence stays findable (the same split `docs/monitor-bridge-checks.md` makes for
monitor-bridge, #2699).

Nothing tests this page. `ansible/roles/k8s/volume-snapshot/tasks/claim.yml` is the authority on
what the role does; where a paragraph here disagrees with it, the task file is right.

## The maintenance-mode attach was never reached by a deploy, and #2740 retired it

Between 2026-08 and 2026-09-27 a claim whose readiness wait timed out on a non-`attached` volume
was attached in maintenance mode, given a second snapshot attempt, then detached, and warned
"THIS DEPLOY IS UNPROTECTED" when that attach failed. The two drills below measured it unreachable on
Longhorn v1.12.1, on both ways a volume reaches the role detached, and #2740 removed it. The
measurements are kept because they are the evidence for the removal, and because they are what a
future session would otherwise re-derive before adding such a block back.

Measured by the task-6 drill, 2026-08-21, on `speedtest` / `speedtest-config` (1Gi,
`longhorn-nobackup`, Longhorn v1.12.1). The drill found two reasons. Only the second still
holds.

**1. (History, void since 2026-09-01.) `k8s/volume-claim` attached the volume first.** Its seed
pod mounted the claim before this role ran, so in the drill a volume scaled to zero was attached
again 11s before this role looked. volume-claim stopped seeding on 2026-09-01, so on a deploy
today a scaled-to-zero service's volume IS detached when this role reads it.

**2. Longhorn took a snapshot of the detached volume anyway.** Invoked directly against a genuinely
detached volume — the same include `k8s/manifests` makes, only without volume-claim ahead of it —
the ordinary `apply` + wait path SUCCEEDED in 10.8s. `volume_snapshot_detached` came out
`false`, so the maintenance-mode attach never fired. The resulting Snapshot CR is a real
recovery point, not an empty marker: `size=10436608`, `children={"volume-head":true}`, no error,
and it survived the volume's later re-attach still `readyToUse` and correctly positioned in the
chain.

So the premise this section's code rests on — "a Longhorn snapshot needs a running engine, and a
workload scaled to zero has none" — does not hold for a volume that is detached but still has
healthy replicas. Reason 2 alone keeps the maintenance-mode attach unreached for such a volume,
and the end of volume-claim's seeding does not change that.

**Established from the code path 2026-09-26, answering #2681: a detached volume is not enough to
reach the block.** `volume_snapshot_detached` is set from three conditions at once — this claim's
snapshot is not `readyToUse`, it is not `markRemoved`, and the volume's `status.state` is not
`attached`. Reason 2 measured the first of those false: Longhorn completed the snapshot of a
plainly detached volume, so the wait succeeded and the flag came out `false` however detached the
volume was. Reason 1's death moves which volumes arrive here detached; it does not make a
Longhorn-side snapshot failure any likelier. Reaching the block still needs the snapshot itself
to fail on a volume the state read also finds unattached.

**A service's first deploy was the one path where that was unestablished until #2698.** A
never-attached volume is a genuinely different state, the task-6 drill did not test it, and with seeding gone nothing attaches
the volume before this role reads it. The claim does still bind: both Longhorn StorageClasses
here set `volumeBindingMode: Immediate`
(`ansible/roles/setup/k3s/files/longhorn-storageclass.yaml`,
`ansible/roles/setup/k3s/files/longhorn-storageclass-nobackup.yaml`), so a PVC no pod has ever
consumed carries a `spec.volumeName` and passes this role's binding assert rather than stopping
the deploy there. Whether Longhorn then completes a snapshot of it is what decides reachability,
and that is untested.

**The Longhorn source explains reason 2, and predicts the never-attached answer.** Read from
`longhorn-manager` v1.12.1 on 2026-09-27. For a new Snapshot CR with `createSnapshot: true`, the
snapshot controller calls `handleAttachmentTicketCreation` before it checks the engine. That
call adds the controller's own attachment ticket, so Longhorn attaches a detached volume by
itself. The controller then waits for the engine to run, takes the snapshot, and deletes its
ticket once the snapshot exists. That is why task-6's detached volume got its snapshot on the
ordinary path. The code draws no line between a detached volume and a never-attached one, so the
prediction is that a first deploy also snapshots on the ordinary path and the block stays dead.
The #2698 drill below confirmed it.

**Measured by the #2698 drill, 2026-09-27: a never-attached volume snapshots on the ordinary
path too.** An operator-run scratch play created a 1Gi `longhorn-nobackup` PVC,
`drill-never-attached`, in `homelab`, and no pod ever mounted it. The play included
`k8s/volume-snapshot` the way `k8s/manifests` does, as service `drill`, whose prune can reach
only `autodeploy-drill-*`. What it recorded:

- **Before the snapshot, the volume had never been attached.** It read `state=detached`, with
  `currentNodeID` and `lastAttachedBy` both empty. Its one engine was `stopped` with no
  snapshots, and its `volumeattachments.longhorn.io` ticket map was empty.
- **The ordinary path succeeded.** The first wait returned `readyToUse=true`, with the snapshot
  13 s after the PVC was created. `volume_snapshot_detached` came out `False`, and the
  maintenance-mode attach was skipped.
- **Longhorn released its own attachment.** Straight after the role, the ticket map was empty
  again and the volume read `detached` on the first poll.
- **The snapshot was real but empty.** `readyToUse=true`, `size=0`,
  `children={"volume-head":true}`, no error. Nothing had been written to the volume.

So the block is unreachable on every deploy path on this Longhorn version: the 2026-08-21 drill
covered a detached volume that had been attached before, and this drill covers one that never
was. #2740 retired it.

**A never-attached volume's snapshot cannot be deleted until the volume attaches.** The drill's
cleanup found this. Its `kubectl delete` of the snapshot CR set the `deletionTimestamp` and then
waited indefinitely. Its `status.error` read as follows:

```text
snapshot deletion delayed: volume engine <volume>-e-0 is upgrading from image  to docker.io/longhornio/longhorn-engine:v1.12.1
```

The image before `to` is blank. An engine that has never run has an empty current image, so Longhorn reads it as mid-upgrade and
defers the delete. Deleting the PVC removed the volume, and the snapshot CR went with it. This
does not affect a real first deploy: the service's pod attaches the volume within the same run,
long before the prune could select that snapshot.

### The premise was never true on this Longhorn version

Settled from git history 2026-08-21, because it decides whether the block above is dead code to
retire or a version-compat guard worth keeping. **It is dead for a detached volume with healthy
replicas, and it was never a guard.** The #2698 drill measured it dead for a never-attached
volume too:

- The premise entered the tree in `410751a`, 2026-08-21. Its commit message derives it from two
  observations: `longhorn-reap-orphan-snapshots.sh` refusing to reap on a detached volume, and
  slice 7's drill getting a `500` reverting a plainly detached one. **Neither is snapshot
  creation** — one is a purge, the other a revert. The premise was generalised from adjacent
  operations, and snapshot creation on a detached volume was never actually tried.
- Longhorn has been pinned at **v1.12.1 since `ceb8723`, 2026-08-15** — six days before that
  commit, and unchanged since. So the premise was written against v1.12.1 and is false on
  v1.12.1. There is no older version here for it to have been true on.

Retiring the block is therefore a cleanup, not a compatibility decision. It is still a
behaviour change, so it belongs in its own change with its own justification, not folded into a
drill. The case that would have kept it honest, a volume that has never been attached, was
measured on 2026-09-27 and snapshots on the ordinary path.

**What this meant for the code.** The maintenance-attach block, its `longhorn-api` include, its
detach and both of their state waits were dead on this Longhorn version on every deploy path, as
was the "THIS DEPLOY IS UNPROTECTED" warning `claim.yml` fell through to. #2740 retired all of
it, so a detached claim now takes the ordinary path and an unready snapshot fails the deploy
whatever the volume's state.

## Things measured rather than assumed

- **A skipped task still sets the variable it registers to** — to a result carrying
  `skipped: true` and no `stdout` at all. Two tasks here may not share a register name unless
  both always run. The retake wait shared `volume_snapshot_ready` with the first wait, and on
  the ordinary attached path — where the retake is skipped — it erased the first wait's real
  reading, so the deploy failed on a snapshot that was healthy and `readyToUse`. Measured
  2026-08-21 on `speedtest-config`. #2740 removed the retake, so one wait now writes that
  register and nothing can clobber it; the rule stands for any register a future task adds.
  `test_the_attached_path_completes_against_a_ready_snapshot` runs the role end to end and pins
  the path that bug broke. Note that only a behavioural test can: the bug lived in when Ansible
  assigns a register, not in any expression's text, and a source-text test asserted the broken
  design and passed.
- `kubectl`'s `jsonpath` has **no `&&`** — `unrecognized character in action: U+0026`, verified
  2026-08-21. The snapshot listing therefore filters on `spec.volume` alone and does the rest in
  Jinja. Do not "simplify" it into a compound filter expression.
- The retention filter treats anything other than the literal `true` for `status.markRemoved` as
  not-removed — an absent or unpopulated field (a snapshot read moments after creation) counts as
  live, not as removed. Every live Snapshot CR measured on 2026-08-21 carried the field explicitly
  (28 `false` / 17 `true`), but the filter no longer depends on that being universal.
- `argv:`, not a folded `cmd:` string, on every `kubectl` call here. `ansible.builtin.command`
  shlex-splits a `cmd`, so a `jsonpath` is only safe there while it happens to contain no spaces,
  and the listing's `{range .items[?(...)]}` does not. Slice 4 shipped that bug and made a whole
  branch dead code for a round.
- `git rev-parse` uses `chdir`, **not** `git -C`. `git -C` does not override `GIT_DIR`, and a
  stray `GIT_DIR` has already made a check in this repo operate on the wrong repository. It also
  runs without `become`, because git refuses a repository it considers to have dubious ownership
  when a different user reads it.
- It carries `delegate_to: localhost`, added 2026-08-28. The SHA is a property of the
  **controller's** checkout — it names the commit whose templates the run is rendering — and
  reading it on the target only ever coincided with that, because `hosts.ini` pins both cluster
  nodes to `ansible_connection=local`. `daniel-stage` — the staging guest, retired 2026-09-29
  (#2941) — was the first genuinely remote target in this repo; it had no checkout, and the
  task failed there with "Unable to change directory before execution" for all thirteen
  callers. ENFORCED by
  `ansible/tests/longhorn/test_volume_snapshot_reads_the_controller_checkout.py`, whose rejecting half is
  the pre-fix task verbatim.
- Ansible role-defaults precedence for a same-named variable across an `include_role` chain: the
  innermost (most recently loaded) role's own default wins over an ancestor's, confirmed
  2026-08-21 with a throwaway three-level play matching this repo's real call structure. See "How
  a role opts in" in the role doc for what that means for
  `volume_snapshot_retain`/`volume_snapshot_timeout`.

## What is unverified

The create path and the prune have both run on real deploys. On 2026-09-26 the cluster held 61
`autodeploy-*` Snapshot CRs across 14 services, created from 2026-08-22 onward, 48 of them
`readyToUse`, and most claims sat at exactly `volume_snapshot_retain` (3). The 13 that were not
ready were the over-long CRs from 2026-08-22, which no supported route could delete (#2686). The
operator removed them on 2026-09-27 by bypassing Longhorn's webhook for exactly those objects
(#2734). The role doc's *The 13 over-long CRs the prune retries and Longhorn refuses, removed by
hand 2026-09-27* has the source reading and the method. Still unverified:

- **`readyToUse` timing** against the 120s ceiling has not been measured.

## Moved off the role doc by #2997

The derivations below left `ansible/roles/k8s/volume-snapshot/CLAUDE.md` when it went back under
the inject hook budget. The role doc keeps each one as a rule; this page keeps why it is that way.

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

### Why the opt-in census is 13 of 31

**Scope for this slice: 13 of 31.** Measured 2026-08-21, 31 roles in this repo carry the
`Recreate` + rendered-RWO-claim shape this role exists for; task 3 declared
`k8s_autodeploy_snapshot_pvcs` for 13 of them — the ones drawn from the auto-deploy promotion
criteria, not from a data-migration survey. The other 18 (`authelia`, `observability`, `crowdsec`,
`healthchecks`, `karakeep`, `loki-homelab`, `mosquitto`, `n8n`, `pihole`, `registry`, `scrutiny`,
`terraria`, `terraria-stats`, `traefik`, `uptime-kuma`, `valheim`, `valheim-stats`, `wg-easy`)
carried the same
manual-deploy migration risk; karakeep opted in later (`c49d5c4a4`), so 17 remain. Widening
was deferred on 2026-08-21 because the create path had not yet run live. It has since; see
*What is unverified* in `docs/volume-snapshot-drills.md`.

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
prune selects on `autodeploy-<service>-`, and `scripts/backup/longhorn_reap_orphan_snapshots.py` — the
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
snapshot info inside volume engine." Issue #2686 filed them as never pruned. The prune did
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

1. It asserts the targets by name, and asserts that `longhorn-webhook-validator` carries
   `objectSelector: {}`.
2. It labels only the targets, which is an UPDATE the validator allows.
3. It sets that `objectSelector` to skip that label. For a DELETE, the selector matches
   the old object, so every other object in the cluster stays validated.
4. It deletes the targets by exact name, then restores `objectSelector: {}` in an `always` block
   and asserts the restore.

The webhook configuration is applied by longhorn-manager through wrangler's `objectset` with the
`longhorn-webhook-ca` Secret as owner. longhorn-manager re-applies it on start or on a CA change,
not on a short loop, so nothing raced the patch and the restore was the play's job. Two runs, both
`failed=0` with nothing left afterwards: the eleven lost-track CRs, then the two `markRemoved`
ones.

### The two drills behind the ordinary detached path

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

### The internal `k8s_no_mutate` guards are defence in depth

The internal `k8s_no_mutate` guards on every task inside this role (the apply, the wait, the
prune, and the two reads' own `check_mode: false`) are defence in depth for a future call site
that includes this role unconditionally — they are not exercised by anything in this repo today,
because the one call site that exists already keeps this role from starting under `--check` or
`--dry-run`.

This role is reached as a dependency rather than named on the command line, so a tag-keyed
refusal could never have covered it — which is why it guards itself. `volume-claim`,
`image-builder` and `cronjob-gate` are all in the same position. The `k8s_dry_run_unsupported`
list that once held the alternative was deleted empty in #2876.
