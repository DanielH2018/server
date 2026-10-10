# volume-revert drill, timeout sizing and the Longhorn API findings

Working-out moved off `ansible/roles/k8s/volume-revert/CLAUDE.md` (#2995), which a session reads
on every touch of the rollback path. The role doc keeps the caller contract, the sequence and the
guards; this page keeps the 2026-08-21 drill timings, the timeout derivation behind
`volume_revert_state_timeout` and `volume_revert_api_timeout`, the longhorn-manager behaviour the
attach and detach were written against, the hand-recovery steps for a partial revert, and what no
test in this repo covers.

## Hand-recovery after a partial revert

A multi-claim revert is not atomic: `tdarr` (`tdarr-configs`, `tdarr-server`) and `code-server`
(`code-server-config`, `code-server-workspace`) each hold two claims, each claim reverts
separately, and a failure on the second stops the play with the first already reverted. The play
stops with the workload at zero replicas and possibly one volume still attached in maintenance
mode. Read the failing task's name to see which claim it stopped on, then:

1. `kubectl -n longhorn-system get volumes.longhorn.io <vol> -o jsonpath='{.status.state}{"|"}{.spec.disableFrontend}'`
   for each of the service's volumes.
2. Any volume still attached with `disableFrontend: true` needs
   `POST {api}/v1/volumes/<vol>?action=detach {}` against **this node's own** longhorn-manager
   pod IP — the ClusterIP is a coin flip, see `k8s/longhorn-api`.
3. Decide per claim whether to finish the revert or to accept the volumes as they are. Reverting
   the remaining claims by hand is the same sequence the role runs.
4. Deploy the service to bring it back up: `./scripts/deploy.sh --tags <service>`.

**A timeout is not proof the revert did not happen.** `volume_revert_api_timeout` bounds the
client, not the server: a `snapshotRevert` that timed out may still have been performed. Read the
volume's snapshot chain before retrying rather than assuming nothing changed — a revert applied
twice to the same snapshot is harmless, but a revert applied to a volume that already moved on is
not what the operator thinks they are doing.

A successful revert leaves nothing to clean up. The drill measured `spec.disableFrontend: false`
on the volume after the normal scale-up that follows a revert: the flag lives on the attachment
ticket, the ticket this role creates is removed by its own detach, and the pod's own attach
afterwards is an ordinary one. A **failed** revert can leave the flag set, because the detach is
the last step.

## The ninth step, removed 2026-09-01

The sequence's ninth step, which stripped the PVC's `homelab.daniel-hunter.com/seeded`
annotation, was removed on 2026-09-01 because `k8s/volume-claim` stopped seeding and nothing
reads that key.

## The dry-run guard is per-task, not tag-keyed

Every mutating task and every wait carries `when: not (k8s_no_mutate | bool)`, because this role
is reached as a dependency and no tag-keyed refusal could cover it.
[`volume-snapshot-drills.md`](volume-snapshot-drills.md), *The internal `k8s_no_mutate` guards are
defence in depth*, holds the reasoning that both roles share.

Both halves of the pair stay in `claim.yml`: `Fail when no snapshot matches this deploy` under
`when: not (k8s_no_mutate | bool)`, and `Report a dry run with nothing to revert` under the
opposite guard. Nothing in this repo exercises the `k8s_no_mutate: true` branch, so
`test_volume_revert.py`'s coverage of it pins the branch's own correctness in isolation, not that
anything reaches it.

## The attach and the detach pair on an empty ticket key

Read from longhorn-manager v1.12.1: `manager.Attach` stores the attachment ticket under whatever
`attachmentID` the caller sends, and `manager.Detach` does `delete(tickets, attachmentID)` and
**ignores `hostId` entirely**. So:

- neither call sends an `attachmentID`, both therefore key the ticket `""`, and the detach removes
  the ticket the attach created;
- adding an `attachmentID` to the attach alone would make the detach delete nothing **and return
  HTTP 200 while doing it**, leaving the volume attached with its frontend disabled and the
  workload at zero;
- the detach does not send `hostId`, because the server never reads it and sending it would
  document a guarantee that does not exist.

The wait on `state: detached` after the detach is the only check that a 200 detach actually
detached. It must never be suppressed with `failed_when: false`.

**Why the revert needs no equivalent state check.** `api.SnapshotRevert` calls
`manager.RevertSnapshot`, which talks to the running engine through the proxy and returns the
engine's error — so a 200 there means the engine performed the revert, where a 200 from the detach
only means a map delete was accepted. The same handler is where the frontend-enabled 500 comes from, and
where the server refuses a snapshot it sees as `Removed`.

## `gitops-deploy`'s rollback passes a fixed 8-character slice

The role's rule is that `volume_revert_sha` is `k8s/volume-snapshot`'s own string, used verbatim.
`gitops_deploy.py` passes `restore_sha=origin[:8]` — a fixed 8-character slice of the FULL 40-char
SHA, not `git rev-parse --short=8`'s own (possibly longer) output. The two agree whenever 8
characters is already unambiguous, which is the overwhelmingly common case, and diverge only on an
8-hex-character collision in the repo's history — negligibly likely, and safe when it happens: the
prefix this role builds then fails to match, and "no snapshot matches this deploy" fires before
anything moves, the same fail-closed outcome as every other unmatched prefix. See
`docs/gitops-pipeline.md`'s *Trap: moving a config source* section for the full reconciliation; it
was not changed, because the failure mode it falls into is already correct.

## Measured cycle time (task-6 drill, 2026-08-21)

The drill ran the role against `speedtest` / `speedtest-config` — 1Gi RWO on `longhorn-nobackup`,
roughly 382 MB in the snapshot chain — on daniel-box, Longhorn v1.12.1. Marker files proved the
data moved: a marker written before the snapshot survived, a marker written after it was gone.
Per-phase numbers come from the `profile_tasks` callback.

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

The rollout back up — which the role never performs — took **32.46s** separately.

**The dominant term is the pod's termination grace period, not the data.** `speedtest`,
`home-assistant` and `tdarr` all set `terminationGracePeriodSeconds: 30`, which accounts for
roughly 30 of that 36.27s; the Longhorn detach itself is the remaining 6s or so. A second,
independent measurement of the same transition (scaling to zero outside the role) gave **34.04s**,
consistent. So the cycle cost is close to fixed per claim, and what varies with the volume is only
the few seconds Longhorn spends coalescing the delta written since the snapshot.

**This measurement is on a 1Gi volume and is not the worst case.** The largest volume slice 7's
promotion actually covers is `home-assistant-config` at 4Gi; `code-server`'s 10Gi stays blocked on
an immutable image tag. The media-adjacent volumes — `jellyfin-config`, the *-arr configs,
`tdarr-server` — are **unmeasured**, and nothing here should be read as covering them. The
grace-period finding is what makes 4Gi a reasonable extrapolation rather than a guess, but it is
still an extrapolation.

## What the numbers say about the defaults — sized, task 6b

Worst case per claim is `3 x volume_revert_state_timeout + 3 x volume_revert_api_timeout`, because
the role makes three state waits and three API calls. At the original 180/60 that was **720s per
claim, 1440s for a two-claim service** — and `tdarr`/`code-server` each hold two claims and both
declare `k8s_autodeploy_snapshot_pvcs`, so this was not hypothetical. Both are
`k8s_autodeploy: false`, which does not exempt them: each role's defaults declare those PVCs
regardless of that flag, precisely because the snapshot and revert protect a MANUAL deploy too, and
a manual deploy is the only kind either service gets.

Task 4's manifests-level rehearsal (Phase 4 of the task-6 drill) is the number this sizing uses,
because it is the path production actually takes: a rollback redeploy pays the snapshot wait AND
the full revert cycle, not the revert alone. That run measured the detach wait after the revert at
**40.80s** (worst observed state wait) and every API call under **0.41s**. Set, with the headroom
each keeps:

- `volume_revert_state_timeout: 180 -> 90` — roughly 2.2x the worst observed wait, and still
  roughly 3x the fixed 30s grace period that dominates it.
- `volume_revert_api_timeout: 60 -> 30` — roughly 70x the worst observed call. These three calls
  update CRs and return; they do not wait for the state change.

That puts one claim's worst case at 360s and `tdarr`/`code-server`'s at 720s. The **realistic**
case is much smaller: the drill's own two-claim-shaped numbers (one claim's 47s or so
manifests-level cycle) put a real two-claim revert closer to **150s**, not 720s — the 720s figure
is a ceiling sized for a stalled Longhorn API, not the expected run.

`gitops_deploy_k8s_rollback_timeout_s` (`K8S_ROLLBACK_TIMEOUT_S`) stays at **900s**, its own budget
rather than sharing the forward deploy's. That covers the 720s worst-case revert with 180s left for
the rest of the SAME playbook run — the pre-revert snapshot wait and the post-apply rollout — at
their REALISTIC cost (the drill measured 38s or so combined for one claim), not at their own
ceiling. **720s is reached by every wait and call succeeding right at its own ceiling, or by the
very last one failing after everything before it also succeeded slowly — a failure anywhere stops
the play immediately (no `ignore_errors`, no `failed_when`), so a snapshot-phase failure and a
revert-phase failure can never both consume their own worst case in one run.** The real residual is
narrower than a compounding-failure scenario: a two-claim service's snapshot phase, succeeding but
slowly, can still cost up to 240s (`volume_snapshot_timeout` x 2 claims) ADDITIVE to a
slow-but-successful 720s revert on one continuous timeline where nothing fails — and that combined
slow-success total is not proven to fit in the 180s left. That is an accepted residual risk (a slow
full success getting cut short), not a proven-safe one — see `docs/gitops-pipeline.md`'s *The
rollback timeout, derived* section. The unit's `TimeoutStartSec` was raised from 25 minutes to 35
minutes to fit `K8S_DEPLOY_TIMEOUT_S` (900s, the forward attempt) plus `K8S_ROLLBACK_TIMEOUT_S`
(900s) plus the flock wait.

## Things measured rather than assumed

- The Longhorn API answers only from the **node-local** manager pod: the longhorn-manager
  NetworkPolicy's `from:` is all pod selectors, so host-originated traffic reaches no other. The
  role never resolves that itself — `k8s/longhorn-api`, included with `tasks_from: resolve.yml`,
  does, once per run and before the first claim is touched.
- `kubectl`'s `jsonpath` has **no `&&`** — `unrecognized character in action: U+0026`, verified
  2026-08-21. The snapshot listing filters on `spec.volume` alone and does the name prefix and
  `markRemoved` in Jinja. Do not fold them back together.
- An **indexed** `jsonpath` (`{.items[0]...}`) against a zero-match query returns rc=1 with
  `array index out of bounds`, which aborts the task before any guard runs. `{range .items[…]}`
  returns rc=0 and empty stdout, which is what makes the failure message reachable.
- `argv:`, never a folded `cmd:` string. `ansible.builtin.command` shlex-splits a `cmd`, so an
  argument containing a space is torn in half — slice 4 shipped that and made a branch dead code
  for a round.
- `snapshotRevert` takes a `snapshotInput`, whose only field this needs is `name` — read from the
  server's own schema at `/v1/schemas/snapshotInput`, not from memory.

## What is not covered by tests

`kubectl` in a Claude session authenticates as a read-only ServiceAccount and `sudo` is denied, so
**no test in this repo drives this sequence against a real Longhorn volume**. The tests pin the
order, the guards, the request bodies and the snapshot selection. That the Longhorn API performs
the revert is proven by the task-6 drill of 2026-08-21, which ran the role through Ansible against
`speedtest-config` and verified the outcome with marker files.

That drill also found what source-text tests structurally cannot. The role's input guard wrote its
SHA check as a bare `regex_search`, which returns the matched STRING; ansible-core 2.21 refuses a
non-boolean conditional, so **the assert aborted on every invocation, with a valid SHA, before the
role did anything** — the rollback path could never have run. Every test of that guard read its
source text and passed throughout.
`ansible/tests/longhorn/test_volume_revert_input_guard.py` now runs the guard instead, and is the
test that would have caught it.
