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
was attached in maintenance mode, given a second snapshot attempt, then detached. #2740 removed
that block, because two drills measured it unreachable on Longhorn v1.12.1 for both ways a
volume reaches the role detached. The measurements stay as the evidence for the removal.

- **A volume that had been attached before (task-6 drill, 2026-08-21, `speedtest-config`).**
  Invoked against a genuinely detached volume, the ordinary apply and wait path succeeded in
  10.8s, and `volume_snapshot_detached` came out `false`. The Snapshot CR was a real recovery
  point (`size=10436608`, `children={"volume-head":true}`) and survived the re-attach still
  `readyToUse`. The seed pod of `k8s/volume-claim` had also attached the volume before this
  role looked. That cause ended on 2026-09-01, when volume-claim stopped seeding.
- **A volume never attached (#2698 drill, 2026-09-27).** A scratch 1Gi `longhorn-nobackup`
  PVC, `drill-never-attached`, was never mounted by any pod. It read `state=detached` with
  empty `currentNodeID` and `lastAttachedBy`, and its engine was `stopped`. The ordinary path
  returned `readyToUse=true` 13 s after the PVC was created, with `size=0`, and skipped the
  maintenance-mode attach. Longhorn then released its own attachment ticket.
- **Why Longhorn snapshots a detached volume.** In `longhorn-manager` v1.12.1, a new Snapshot
  CR with `createSnapshot: true` makes the snapshot controller call
  `handleAttachmentTicketCreation` before it checks the engine. Longhorn attaches the volume
  itself, takes the snapshot, and deletes its ticket. The code draws no line between a volume
  attached before and one never attached.
- **Why the premise existed.** It entered the tree in `410751a` (2026-08-21), generalised from
  two adjacent operations: the orphan reaper refusing a detached volume, and a revert that got
  a `500`. Neither is snapshot creation. Longhorn has been pinned at v1.12.1 since `ceb8723`
  (2026-08-15), so no older version existed for the premise to be true on.
- **A cleanup trap.** The snapshot of a never-attached volume cannot be deleted until the
  volume attaches. The delete waits on `snapshot deletion delayed: volume engine
  <volume>-e-0 is upgrading from image  to docker.io/longhornio/longhorn-engine:v1.12.1`,
  where the blank image means the engine never ran. Deleting the PVC removes the snapshot CR
  with it. A real first deploy is unaffected, because the pod attaches the volume within the
  run.

After #2740 a detached claim takes the ordinary path, and an unready snapshot fails the deploy
whatever the volume's state. The change also removed the `faulted` / `attaching` / `detaching`
ambiguity the old block carried, because it read those states as detached.

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

The create path and the prune have both run on real deploys: most claims sit at exactly
`volume_snapshot_retain` (3) Snapshot CRs. To count them and see which are ready, list the
`autodeploy-*` CRs:

```bash
kubectl -n longhorn-system get snapshots.longhorn.io \
  -o custom-columns=NAME:.metadata.name,READY:.status.readyToUse --no-headers | grep '^autodeploy-'
```

The only CRs that ever stayed unready were 13 over-long ones from 2026-08-22, which no
supported route could delete (#2686). The operator removed them on 2026-09-27 by bypassing
Longhorn's webhook for exactly those objects (#2734). *The 13 over-long CRs the prune retries and Longhorn refuses, removed by hand
2026-09-27* below has the source reading and the method. Still unverified:

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

### Which roles opt in

The set of roles that opt in is the table below. It is generated from each role's
`defaults/main.yml`, so it moves when a role declares or drops `k8s_autodeploy_snapshot_pvcs`.
A role that only mentions the name in a comment, as `observability` and `navidrome` do, is not
in it.

--8<-- "assets/generated/fragments/snapshot-optin.md"

The opt-in set follows the auto-deploy promotion criteria, not a data-migration survey. The
roles with the `Recreate` + rendered-RWO-claim shape that did not opt in carry the same
manual-deploy migration risk. Widening was deferred until the create path had run live, and
it has since; see *What is unverified* above.

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
commits.** The per-run token (see "The snapshot name is deterministic" in the role doc) gives every run its
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

### The internal `k8s_no_mutate` guards are defence in depth

The internal `k8s_no_mutate` guards on every task inside this role (the apply, the wait, the
prune, and the two reads' own `check_mode: false`) are defence in depth for a future call site
that includes this role unconditionally — they are not exercised by anything in this repo today,
because the one call site that exists already keeps this role from starting under `--check` or
`--dry-run`.

This role is reached as a dependency rather than named on the command line, so a tag-keyed
refusal could never have covered it — which is why it guards itself. `volume-claim`,
`image-builder`, `cronjob-gate` and `volume-revert` are all in the same position. The `k8s_dry_run_unsupported`
list that once held the alternative was deleted empty in #2876.
