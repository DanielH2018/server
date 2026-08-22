# k8s/volume-revert — put a service's volumes back to the pre-deploy snapshot

This role deploys nothing. It reverts each of a service's Longhorn volumes to the snapshot
`k8s/volume-snapshot` took immediately before the deploy that failed, so that the rollback's
apply restores manifests over data those manifests can actually read.

**Read this first: the role stops the workload and does not start it again.** It scales the
Deployment to zero, reverts, and leaves it there. The apply that follows in the rollback
restores `replicas: 1` from the manifest — one rollout instead of two, and no race between
them. A caller that reverts without applying afterwards leaves the service down.

**This code path runs only during an incident.** Nothing exercises it on a good day. That is
why every step that can fail on its own account is checked before the scale-down, and why
`ansible/tests/test_volume_revert.py` pins claim.yml's whole sequence rather than a few pairs.
Two transpositions that look harmless are outages: scaling down AFTER the maintenance-mode
attach leaves the pod holding the volume, and detaching BEFORE the revert gives the revert a
volume with no engine. Read the table below before reordering anything.

**"Checked before the scale-down" is a per-claim guarantee, not a per-service one.** `tdarr` and
`code-server` each hold two claims (see "A multi-claim revert is not atomic" below), and
main.yml includes this file once per claim. On the second claim, its own checks — including the
"no snapshot matches" fail below — run AFTER the first claim already scaled the Deployment to
zero. So a failure on claim 2 finds the service already DOWN, not still running; the failure
message says so rather than claiming otherwise.

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
being rolled back — the output of `git rev-parse --short=8 HEAD` at that commit, used verbatim.
`--short=8` is a minimum, not a width: git returns more characters when eight are ambiguous, and
the snapshot carries whatever it returned. So this role checks the shape (`^[0-9a-f]{8,}$`) and
transforms nothing. Truncating to eight would fail to match a nine-character name.

**The one caller that does not follow this rule: `gitops-deploy`'s automated rollback.**
`gitops_deploy.py` passes `restore_sha=origin[:8]` — a fixed 8-character slice of the FULL
40-char SHA, not `git rev-parse --short=8`'s own (possibly longer) output. The two agree
whenever 8 characters is already unambiguous, which is the overwhelmingly common case, and
diverge only on an 8-hex-char collision in the repo's history — negligibly likely, and safe when
it happens: the prefix this role builds then fails to match, and "no snapshot matches this
deploy" fires before anything moves, the same fail-closed outcome as every other unmatched
prefix. See `ansible/roles/setup/gitops_deploy/CLAUDE.md`'s "Logic tests" section for the full
reconciliation; not changed here because the failure mode it falls into is already correct.

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
   sortable as strings. `markRemoved` snapshots are rejected: a snapshot already marked removed
   cannot be reverted to, and longhorn-manager refuses one itself
   (`not revert to snapshot ... since it's marked as Removed`).
3. **Scale the Deployment to zero.** The volume cannot be attached in maintenance mode while a
   pod holds it.
4. **Wait for `state: detached`.**
5. **`POST ?action=attach {hostId, disableFrontend: true}`** — maintenance mode.
6. **Wait for `state: attached`, then assert `spec.disableFrontend` is true.** The assert is a
   precondition, not a formality: without it the revert fails at step 7, after the workload is
   already at zero, and the message names the wrong thing.
7. **`POST ?action=snapshotRevert {name}`**, demanding HTTP 200.
8. **`POST ?action=detach {}`, then wait for `detached`.**
9. **Strip the PVC's `homelab.daniel-hunter.com/seeded` annotation.** `k8s/seed-volume` skips
   its whole seed pod cycle on that key, and the key asserts something about the volume's
   contents that step 7 has just replaced. In practice the two cannot disagree — every snapshot
   this role can pick was taken during a deploy, long after the volume was seeded, so the
   in-volume `.seeded` marker is in all of them. Stripping it removes the need for that to stay
   true. Stripping it needlessly costs one seed pod cycle on the next deploy of this service,
   which re-reads the marker and re-annotates; keeping it after a revert that did land pre-seed
   would leave a volume permanently believed seeded when it is not.

Steps 1 and 2 are reads, and carry `check_mode: false` — but that only matters if this role
starts at all. See "The guard is `k8s_no_mutate`, and neither `--check` nor `--dry-run` reaches
this role" below: today, they don't.

## The guard is `k8s_no_mutate`, and neither `--check` nor `--dry-run` reaches this role

**Corrected**: an earlier version of this doc claimed a dry run "answers the question that
matters most about a rollback — is there a snapshot to revert to?" That is false as this role is
actually called. `roles/k8s/manifests/tasks/main.yml` gates the whole
`include_role: k8s/volume-revert` on `when: not (k8s_no_mutate | bool)` — the same fact
(`ansible_check_mode or (k8s_dry_run | bool)`) every mutating task inside this role also checks.
So under `--check` or `--dry-run`, this role never starts: not step 1's PVC lookup, not step 2's
snapshot listing, not the `Fail when no snapshot matches this deploy` task, and not its dry-run
sibling below. Running a dry run and getting silence proves nothing about whether a snapshot
exists — the role that would have checked never ran. `k8s/volume-snapshot`'s CLAUDE.md documents
the identical trap for its own call site.

`Fail when no snapshot matches this deploy` still carries `when: not (k8s_no_mutate | bool)`,
and `Report a dry run with nothing to revert` still carries the opposite guard
(`when: k8s_no_mutate | bool`) as its sibling. Keeping both is deliberate, not a leftover: these
internal guards are defence in depth for a future call site that includes this role
unconditionally (or under `--check`/`--dry-run` directly), matching the same choice
`k8s/volume-snapshot` made for its own internal guards. Nothing in this repo exercises the
`k8s_no_mutate: true` branch today — the one call site that exists already keeps the role from
starting under either mode — so `test_volume_revert.py`'s coverage of it pins the branch's own
correctness in isolation, not that anything reaches it.

## The attach and the detach pair on an empty ticket key

Read from longhorn-manager v1.12.1: `manager.Attach` stores the attachment ticket under
whatever `attachmentID` the caller sends, and `manager.Detach` does
`delete(tickets, attachmentID)` and **ignores `hostId` entirely**. So:

- neither call sends an `attachmentID`, both therefore key the ticket `""`, and the detach
  removes the ticket the attach created;
- adding an `attachmentID` to the attach alone would make the detach delete nothing **and
  return HTTP 200 while doing it**, leaving the volume attached with its frontend disabled and
  the workload at zero;
- the detach does not send `hostId`, because the server never reads it and sending it would
  document a guarantee that does not exist.

The wait on `state: detached` after the detach is the only check that a 200 detach actually
detached. It must never be suppressed with `failed_when: false`.

**Why the revert needs no equivalent state check.** `api.SnapshotRevert` calls
`manager.RevertSnapshot`, which talks to the running engine through the proxy and returns the
engine's error — so a 200 there means the engine performed the revert, where the detach's 200
only means a map delete was accepted. The same handler is where the frontend-enabled 500 comes
from, and where the server refuses a snapshot it sees as `Removed`.

## Maintenance mode does not leak

The drill measured `spec.disableFrontend: false` on the volume after the normal scale-up that
follows a revert. The flag lives on the attachment ticket, and the ticket this role creates is
removed by its own detach; the pod's own attach afterwards is an ordinary one. So a successful
revert leaves nothing to clean up.

A **failed** revert can leave the flag set, because the detach is the last step. See below.

## A multi-claim revert is not atomic

`tdarr` (`tdarr-configs`, `tdarr-server`) and `code-server` (`code-server-config`,
`code-server-workspace`) each hold two claims. Each claim reverts separately, at a slightly
different instant, and a failure on the second stops the play with the first already reverted.
That is deliberate: continuing past a failed revert would be worse. But it means a partial
failure leaves a service's volumes mutually inconsistent — a different, and possibly worse,
state than the bad deploy it was recovering from.

**Recovering a partial revert by hand.** The play stops with the workload at zero replicas and
possibly one volume still attached in maintenance mode. Read the failing task's name to see
which claim it stopped on, then:

1. `kubectl -n longhorn-system get volumes.longhorn.io <vol> -o jsonpath='{.status.state}{"|"}{.spec.disableFrontend}'`
   for each of the service's volumes.
2. Any volume still attached with `disableFrontend: true` needs
   `POST {api}/v1/volumes/<vol>?action=detach {}` against **this node's own** longhorn-manager
   pod IP — the ClusterIP is a coin flip, see `k8s/longhorn-api`.
3. Decide per claim whether to finish the revert or to accept the volumes as they are. Reverting
   the remaining claims by hand is the same sequence as above.
4. Deploy the service to bring it back up: `./scripts/deploy.sh --tags <service>`.

**A timeout is not proof the revert did not happen.** `volume_revert_api_timeout` bounds the
client, not the server: a `snapshotRevert` that timed out may still have been performed. Read
the volume's snapshot chain before retrying rather than assuming nothing changed — a revert
applied twice to the same snapshot is harmless, but a revert applied to a volume that already
moved on is not what the operator thinks they are doing.

## This role is deliberately absent from `k8s_dry_run_unsupported`

It mutates outside `roles/k8s/manifests`, which is normally what puts a role on that list. The
list keys on `ansible_run_tags`, so it only reaches roles an operator names on the command line
— a role reached as a dependency is invisible to it. `seed-volume`, `image-builder`,
`cronjob-gate` and `volume-snapshot` are all in the same position and guard themselves
internally instead. This role does the same: every mutating task and every wait carries
`when: not (k8s_no_mutate | bool)`, which is `ansible_check_mode or (k8s_dry_run | bool)`.

Guarding on either half alone is the bug that guard exists to prevent. `ansible/tests/test_volume_revert.py`
pins the whole census of mutating tasks against it.

## Measured cycle time (task-6 drill, 2026-08-21)

The drill ran this role against `speedtest` / `speedtest-config` — 1Gi RWO on
`longhorn-nobackup`, ~382 MB in the snapshot chain — on daniel-box, Longhorn v1.12.1. Marker
files proved the data moved: a marker written before the snapshot survived, a marker written
after it was gone. Per-phase numbers come from the `profile_tasks` callback.

| Phase | Task | Measured |
|---|---|---|
| scale-down | Scale the Deployment to zero replicas | 0.29s |
| wait-for-detach | Wait for the detach that precedes the attach | **36.27s** |
| maintenance attach | Attach the volume in maintenance mode (POST) | 0.41s |
| wait-for-attach | Wait for the maintenance-mode attach | 2.53s |
| the revert itself | Revert the volume to the pre-deploy snapshot (POST) | 0.24s |
| detach | Detach the volume (POST) | 0.34s |
| wait-for-detach | Wait for the detach after the revert | 2.53s |
| | **whole role, one claim** | **42.6s** |

The rollout back up — which this role never performs, see above — took **32.46s** separately.

**The dominant term is the pod's termination grace period, not the data.** `speedtest`,
`home-assistant` and `tdarr` all set `terminationGracePeriodSeconds: 30`, which accounts for
roughly 30 of that 36.27s; the Longhorn detach itself is the remaining ~6s. A second,
independent measurement of the same transition (scaling to zero outside this role) gave
**34.04s**, consistent. So the cycle cost is close to fixed per claim, and what varies with the
volume is only the few seconds Longhorn spends coalescing the delta written since the snapshot.

**This measurement is on a 1Gi volume and is not the worst case.** The largest volume slice 7's
promotion actually covers is `home-assistant-config` at 4Gi; `code-server`'s 10Gi stays blocked
on an immutable image tag. The media-adjacent volumes — `jellyfin-config`, the *-arr configs,
`tdarr-server` — are **unmeasured**, and nothing here should be read as covering them. The
grace-period finding is what makes 4Gi a reasonable extrapolation rather than a guess, but it
is still an extrapolation.

### What the numbers say about the defaults — sized, task 6b

Worst case per claim is `3 x volume_revert_state_timeout + 3 x volume_revert_api_timeout`,
because the role makes three state waits and three API calls. At the original 180/60 that was
**720s per claim, 1440s for a two-claim service** — and `tdarr`/`code-server` each hold two
claims and are inside slice 7's promotion set, so this was not hypothetical.

Task 4's manifests-level rehearsal (Phase 4 of the task-6 drill) is the number this sizing uses,
because it is the path production actually takes: a rollback redeploy pays the snapshot wait
AND the full revert cycle, not the revert alone. That run measured the revert's detach wait at
**40.80s** (worst observed state wait) and every API call under **0.41s**. Set, with the
headroom each keeps:

- `volume_revert_state_timeout: 180 -> 90` — ~2.2x the worst observed wait, and still ~3x the
  fixed 30s grace period that dominates it.
- `volume_revert_api_timeout: 60 -> 30` — ~70x the worst observed call. These three calls
  update CRs and return; they do not wait for the state change.

That puts one claim's worst case at 360s and `tdarr`/`code-server`'s at 720s. The **realistic**
case is much smaller: the drill's own two-claim-shaped numbers (one claim's ~47s manifests-level
cycle) put a real two-claim revert closer to **~150s**, not 720s — the 720s figure is a ceiling
sized for a stalled Longhorn API, not the expected run.

`gitops_deploy_k8s_rollback_timeout_s` (`K8S_ROLLBACK_TIMEOUT_S`) stays at **900s**, its own
budget rather than sharing the forward deploy's. That covers the 720s worst-case revert with
180s left for the rest of the SAME playbook run — the pre-revert snapshot wait and the
post-apply rollout — at their REALISTIC cost (the drill measured ~38s combined for one claim),
not at their own ceiling. **720s is reached by every wait/call succeeding right at its own
ceiling, or by the very last one failing after everything before it also succeeded slowly — a
failure anywhere stops the play immediately (no `ignore_errors`, no `failed_when`), so a
snapshot-phase failure and a revert-phase failure can never both consume their own worst case in
one run.** The real residual is narrower than a compounding-failure scenario: a two-claim
service's snapshot phase, succeeding but slowly, can still cost up to 240s
(`volume_snapshot_timeout` x 2 claims) ADDITIVE to a slow-but-successful 720s revert on one
continuous timeline where nothing fails — and that combined slow-success total is not proven to
fit in the 180s left. That is an accepted residual risk (a slow full success getting cut short),
not a proven-safe one — see `ansible/roles/setup/gitops_deploy/CLAUDE.md`'s rollback-timeout
section. The unit's `TimeoutStartSec` was raised from 25min to 35min to fit
`K8S_DEPLOY_TIMEOUT_S` (900s, the forward attempt) plus `K8S_ROLLBACK_TIMEOUT_S` (900s) plus the
flock wait.

## Things measured rather than assumed

- The Longhorn API answers only from the **node-local** manager pod: the longhorn-manager
  NetworkPolicy's `from:` is all podSelectors, so host-originated traffic reaches no other. This
  role never resolves that itself — `k8s/longhorn-api`, included with `tasks_from: resolve.yml`,
  does, once per run and before the first claim is touched.
- `kubectl`'s jsonpath has **no `&&`** — `unrecognized character in action: U+0026`, verified
  2026-08-21. The snapshot listing filters on `spec.volume` alone and does the name prefix and
  `markRemoved` in Jinja. Do not fold them back together.
- An **indexed** jsonpath (`{.items[0]...}`) against a zero-match query returns rc=1 with
  `array index out of bounds`, which aborts the task before any guard runs. `{range .items[…]}`
  returns rc=0 and empty stdout, which is what makes the failure message reachable.
- `argv:`, never a folded `cmd:` string. `ansible.builtin.command` shlex-splits a `cmd`, so an
  argument containing a space is torn in half — slice 4 shipped that and made a branch dead code
  for a round.
- `snapshotRevert` takes a `snapshotInput`, whose only field this needs is `name` — read from
  the server's own schema at `/v1/schemas/snapshotInput`, not from memory.

## What is not covered by tests

`kubectl` in a Claude session authenticates as a read-only ServiceAccount and `sudo` is denied,
so **no test in this repo drives this sequence against a real Longhorn volume**. The tests pin
the order, the guards, the request bodies and the snapshot selection. That the Longhorn API
performs the revert is proven by the task-6 drill of 2026-08-21, which ran this role through
Ansible against `speedtest-config` and verified the outcome with marker files.

That drill also found what source-text tests structurally cannot. The role's input guard wrote
its SHA check as a bare `regex_search`, which returns the matched STRING; ansible-core 2.21
refuses a non-boolean conditional, so **the assert aborted on every invocation, with a valid
SHA, before the role did anything** — the rollback path could never have run. Every test of that
guard read its source text and passed throughout. `ansible/tests/test_volume_revert_input_guard.py`
now runs the guard instead, and is the test that would have caught it.
