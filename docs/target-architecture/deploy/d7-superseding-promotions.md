# D7 — Superseding promotions

Status: DONE on 2026-11-12. D7.1 to D7.5 landed on 2026-10-24: D7.1 in #6140, D7.2 in
#6145, D7.4 in #6156, the refactor D7.5 needed in #6159, D7.5 in #6193, D7.7 in #6200, D7.8
in #6205, and the hold attribution #6199 asked for in #6204. The operator turned `SUPERSEDE`
on in D7.6 (#6210), which merged on 2026-10-25. The live pair ran that day, and
*Verification* records what it proved and what it left open. The red proof for the live
pair ran on 2026-10-29 and passed: promote run 40882393836, for #6497 (P), #6498 (T) and the
revert #6508. The exit criterion's pair ran naturally on 2026-11-12: promote run
[43290958373](https://github.com/DanielH2018/server/actions/runs/43290958373) for #8140 (P)
printed `superseded`, and run
[43292081624](https://github.com/DanielH2018/server/actions/runs/43292081624) for #8149 (T)
printed `good` and moved `last-good` to T. *The exit pair's result, 2026-11-12* under
*Verification* quotes both runs and names the one part the pair did not exercise.

Depends on: D5, through #5704's soak fix (the soak Job exits once `homelab` no longer
holds its revision). D2.5's in-flight guard and D5.2b1's hold without a reset are the
rules this slice amends.

Unblocks: nothing. This slice shortens the time a merged change waits before Flux
applies it.

## Goal and exit criterion

A green master commit T that merges while an earlier commit P is still in `verify` is
applied without waiting for P to settle, when T's own change is disjoint from P's. One
verification then covers both commits. `last-good` moves once, to T, and a failure in
either commit still holds and resets as C2 says.

The exit criterion is one measured pair of promote runs on production:

- P's `tag` moved `prod` to P, and T's `tag` moved `prod` to T before P's `verify` ended;
- P's `verify` printed `superseded`, and P's `settle` wrote neither `last-good` nor
  `DEPLOY_HOLD`;
- T's `verify` expected the names of both commits and printed `good`, and T's `settle`
  moved `last-good` to `sha-<T>`;
- `land.sh` read `settled` for both PRs, and T's landing settled earlier than P's
  `settle` plus T's own verification would have allowed.

The slice was closed against a narrower bar. Issue #8115 asked for DONE on one natural pair
in which P printed `superseded` and T printed `good`. The 2026-11-12 pair meets the first two
bullets in full. It meets the third and fourth only in part, because T changed no service:

- **Third bullet.** T printed `good` and moved `last-good`, but T's expected set held only
  P's names. The 2026-10-29 red proof is the run whose T expected names from both commits,
  and that T failed on purpose.
- **Fourth bullet.** `land.sh` read `settled` for P and `nothing-to-deploy` for T. With no
  verification of T's own, the timing comparison was not measured.

No run has yet shown a superseding T with its own change that both verifies the union and
settles `good`; #8208 tracks that run.

## Baseline at `53fe3f196`

Each fact below is what the design rests on, with its evidence.

- **A verification in flight blocks every later promotion.** `decide` calls
  `promote_lib/verify_promotion.py:wait_settled`, which polls every 30 seconds while
  `prod` and `last-good` name different digests. It gives up as `current` after
  `IN_FLIGHT_WAIT_S`, 350 minutes. The `DECIDED` marker above that constant records why
  this check guards the gap rather than a concurrency group.
- **`tag` re-reads `prod` before it moves it, and `settle` does not.** The `Move prod`
  step in `.github/workflows/promote.yml` moves `prod` only while it names the digest
  `decide` read. The `Apply the verdict` step in `settle` tags `last-good`, or resets
  `prod` to `last-good`, without reading `prod` first. No run can race `settle` today,
  because nothing else moves `prod` while a verification is in flight.
- **The changed set is taken against `prod`.** `verify_promotion.py:expected_set` diffs
  T's `digests.json` against `prod`'s bundle. `promote.py:_promotion` already diffs
  against `last-good` for `no_reset` and `stateful` when the two tags differ.
- **The soak already judges against `last-good`.** The soak Job diffs R's `digests.json`
  against the `last-good` OCIRepository (`docs/flux-deploy-plane.md`, *The soak Job*). On
  a superseding revision it already covers the earlier commit's services.
- **Every Kustomization posts a status on every revision, changed or not.** Commit
  `53fe3f196` changed no cluster footprint: it touched the hypervisor's SUC rehearsal
  only. Its commit statuses still carry 100 distinct `flux/*` contexts, all `success`,
  all posted between 19:28:48 and 19:30:54 UTC on 2026-10-24. A Kustomization that a
  revision leaves unchanged therefore posts `flux/<name>` on that revision within about
  two minutes of the move. (`ghread api repos/DanielH2018/server/commits/<sha>/statuses`.)
- **A Kustomization in a health check finishes the old revision first.**
  kustomize-controller v1.9.6 runs without `--feature-gates`
  (`k8s/roles/flux/files/gotk-components.yaml`), so it does not cancel a health
  check when the source moves (#5704). A Kustomization that is idle picks up a new
  revision on its next source event. A Kustomization still health-checking P applies T
  only after P's check ends.
- **The superseded soak fails.** Since #5704 the soak Job re-reads `homelab` on each
  poll and exits 1 once it no longer holds R. Moving `prod` from P to T therefore posts
  `flux/soak` `failure` on P within one poll.
- **Promotion cadence.** The measured numbers are in *Measured cadence* below. They
  decide whether this slice is worth building (Open question 1).

### Measured cadence

Measured over every completed `promote` run from the workflow's first landing,
2026-10-12T22:23Z, to 2026-10-24T19:28Z: 11.9 days and 873 runs that did not skip.
Every job's timing and every `promote` job's log was read, so nothing here is sampled.

- **Merges.** 743 first-parent commits reached master, about 62 a day.
- **Verifications are short.** 512 runs reached `verify`. The job took 0.8 minutes at
  the median, 8.9 at p90, and 330.4 at most, which is the cap. From `tag`'s start to
  `settle`'s end took 1.5 minutes at the median and 10.2 at p90. Only 5 runs took more
  than 38 minutes.
- **Waiting is the exception.** 111 of the 784 successful `promote` jobs (14%) waited on
  a verification in flight. The wait was 2.5 minutes at the median, 10.3 at p90, and
  296.5 at most.
- **Of those 111, 62 went on to promote**, with a median wait of 2.4 minutes and a
  longest of 67.9. Another 42 ended `current` after the wait, because the run in flight
  already carried their commit, so they delayed nothing. 6 ended `held`.
- **The longest waits sat behind a broken verification.** On 2026-10-22, run
  `39855439990`'s `verify` ran to the 330-minute cap, and run `39860107369`'s `decide`
  waited 296.5 minutes behind it, then read `held`. A verification that is failing or
  never reports is the case condition 4 and the missing-status rule refuse, so this
  slice does not shorten it.
- **Batching already happens.** 43 of 665 promotions (6.5%) carried two or more merges.
  21 runs waited and then promoted a newer tip than the commit that triggered them.
- **A second queue sits before `decide`.** 35 `promote` jobs sat pending in the
  `promote` concurrency group for over two minutes, 3.4 minutes at the median and 141.3
  at most. GitHub evicted another 62 pending jobs, and the next `decide` picked up their
  merges. This slice does not touch that queue.

In short, the wait this slice removes is usually two or three minutes, on about one
promotion in seven. The long waits come from verifications that fail or stall, and the
slice deliberately does not supersede those.

## Design

The slice changes one rule: a verdict on T counts only while `prod` still names T's
digest. A newer revision may then take over an in-flight one, under conditions narrow
enough that the takeover never hides a failure.

### Terms

- **In flight.** `prod` and `last-good` name different digests, and `prod` is not the
  `held` digest. P is `prod`'s commit.
- **The base.** `last-good`'s bundle, except when `prod` names the `held` digest, where
  the base is `prod`'s bundle as today. With nothing in flight the base equals `prod`, so
  every rule below reads exactly as C2 reads it today.
- **P's set.** P's changed set against the base.
- **T's own change.** The names whose `footprint` differs between T and P, without
  dependents.

### When `decide` supersedes

`wait_settled` returns as soon as it reads P in flight, rather than waiting, and hands
P to the walk. The walk picks T as today. `decide` promotes T over P only when every
condition below holds. Otherwise it waits as it does today, and promotes T after P
settles.

1. `SUPERSEDE` is `True` in `promote.py`. It ships `False`, as `REVERT_RULE` did.
1. `DEPLOY_HOLD` reads `none`.
1. The chain is short. `prod`'s digest is not the one the OCI tag `superseding` names.
   `tag` writes that tag on T before it moves `prod` over P, so a T that superseded a run
   is never superseded itself, and at most one supersession happens per verification.
   `prod` never returns to that digest once it moves on: it moves forward, or back to
   `last-good`.
1. P has no failure yet. No name in P's set reads `failure` or `error` on P's commit.
   `decide` already makes this read for its H1.1i record. A P that is failing settles
   as failed, and T waits for that.
1. T's own change is disjoint from P's set, ignoring `homelab` and `soak`.
1. No special rollout is involved. Neither T's own change nor P's set holds
   `longhorn`, `flux` or `cluster-vars`, and neither was widened (an `_unclassified`
   change or a bundle with no `digests.json`). Those names have their own provisional
   windows in `verify`, which this slice does not compose.
1. No hold without a reset is involved. T's `no_reset` and `stateful`, both taken
   against `last-good`, are empty. T includes P, so this also covers P.
1. T is outside the reboot window, judged with T's derived timeout over the union.

Everything else in the walk is unchanged: the signature, CI, the object-loss check
against `prod`'s bundle, the suspended-tag check and the forward-only gate.

### What T's verification expects

- **The changed set is taken against the base.** For a superseding T this is P's set
  plus T's own change. For every other T the base is `prod`, as today.
- **The H1.1i record is read on the base's commit.** P's own statuses are still
  arriving, and a failure caused by P must not excuse a dependent of T.
- **The timeout is `promote_timeout` over the union.** A name that P was still rolling
  is counted in full. The timeout is therefore longer than needed, never shorter.
- **The statuses come from T's commit and from after T's move.** Each name in P's set
  that is still health-checking P finishes P, then reconciles T and posts on T. Names
  P left idle post on T within about two minutes.

### What a superseded run does

- **`verify` prints `superseded`.** On each poll it reads `prod`'s digest before it
  reads the statuses. When `prod` no longer names T's digest, it prints `superseded`
  and exits 0. It makes that read once more before it returns `failed`, because the
  superseded soak posts `failure` on P right after the move. The job gains
  `packages: read`, which `decide` already holds. It keeps no write permission.
- **`settle` writes nothing for a superseded run.** For every verdict, `settle` reads
  `prod` before each write, and writes only while `prod` names T's digest. This covers
  the `last-good` move, the hold, the `held` tag and the reset. A mismatch logs which
  run moved `prod` and exits 0. It dispatches no new run: the run that superseded it
  owns the dispatch.
- **`tag` re-reads `DEPLOY_HOLD`** just before it moves `prod`, and moves nothing when
  it reads anything but `none`.
- **`tag` tags T `superseding`** before it moves `prod`, when `decide` named the run T
  supersedes in its `supersedes` output. Tagged after the move, a failed write would leave
  a T that the next run may supersede again.

### What `land-wait` changes

`land-wait` reads the `flux/soak` verdict on R, `homelab`'s revision at that poll.
Before it returns `deploy-failed` with cause `soak-failed`, it re-reads `homelab`. When
`homelab` has moved to a descendant of R, the failure may be a supersession, and the
wait goes on. The `deadline` it extends for an earlier verification in flight
(`wait.py:_earlier_in_flight`) keeps working unchanged: the superseding T descends from
P's merge, so P's landing settles on T.

### The cases

Each row is one test pair for the `decide`, `verify` and `settle` changes in the same
PR. The must-pass case is the row as written. The must-refuse case breaks the one
condition the row names.

| P's state when T's `decide` runs | Condition | Outcome |
|---|---|---|
| verifying, no failure yet, disjoint | all eight hold | T promotes; P prints `superseded`; T verifies P's set and T's own change |
| verifying, a name in P's set reads `failure` | condition 4 | T waits; P settles as failed, holds and resets |
| verifying, T changes a name in P's set | condition 5 | T waits as today |
| verifying, P superseded an earlier run | condition 3 | T waits as today |
| verifying, T or P changes `longhorn`, `flux` or `cluster-vars` | condition 6 | T waits as today |
| verifying, T names a forward-only tag or a snapshot role | condition 7 | T waits as today |
| held without a reset (`prod` is `held`) | not in flight | T decides against the hold as today |
| settled (`prod` is `last-good`) | not in flight | unchanged: T promotes against `prod` |

Two more pairs cover the superseded run:

- P's `verify` reads `prod` moved and prints `superseded`, including when its last
  poll read `flux/soak` `failure`. Its red half: with `prod` still on P, the same
  failure prints `failed`.
- P's `settle` with `prod` on T writes nothing. Its red half: with `prod` on P, it
  holds and resets as today.

### The race this leaves open

`settle` reads `prod` and then writes, and T's `tag` can move `prod` between the two.
Joining `settle` to the `promote` group would close it, but the `DECIDED` marker on
`IN_FLIGHT_WAIT_S` rejects that: a pending `settle` in a group can be evicted with its
hold unwritten. This slice accepts the window, because condition 4 already refused T
for any P with a failure in flight. What each interleaving leaves:

- **P good.** P moves `last-good` to P after T's move. T's base was read before that,
  so T still verifies the union, and T's `settle` moves `last-good` to T. No state is
  wrong.
- **P failed between T's `decide` and T's `tag`.** P writes its hold first, then resets
  `prod` to the old `last-good`, which may land after T's move. T's `verify` reads
  `prod` moved and prints `superseded`, and T's `settle` writes nothing. The cluster
  ends at `last-good` with P's hold set, which is the state C2 wants after P fails. T
  promotes again after the operator clears the hold.
- **P settled good between T's `decide` and T's `tag`.** `prod` still names P, so `tag`'s
  `prod` check passes, but T's base and reset digest are the `last-good` from before P. A
  failed T would then reset past a verified P and leave `prod` and `last-good` apart for
  good. So a superseding `tag` also reads `last-good` and moves nothing when it differs
  from the digest `decide` read (D7.7). P's `settle` dispatches the next run, which
  promotes T against the new `last-good`.
- **P settles good around T's `tag`.** P's `settle` reads `prod` before T's move, and T's
  `tag` reads `last-good` before P's write. Both checks pass and both writes land: the
  D7 live drill on 2026-10-25 hit this with the two writes one second apart
  ([#6268](https://github.com/DanielH2018/server/issues/6268)). `last-good` then names P,
  while T's reset digest is still the one from before P. So a superseding T's `settle`
  reads `last-good` again before a reset, and resets to it when it differs from the digest
  `decide` read. A failed read falls back to the digest `decide` read, with a warning. The
  hold still stops the next promotion, and `prod` ends equal to `last-good`, so no
  `reset_to_last_good` dispatch is needed after the operator clears the hold.

### Call tree

```diff
 promote.py decide
-  wait_settled            waits while prod != last-good
+  wait_settled            returns P in flight at once
+  supersedable(P, T)      the eight conditions; else wait_settled's old wait
   _promotion
-    expected_set(T, prod)
+    expected_set(T, base)
-    prior_statuses(prod's commit)
+    prior_statuses(base's commit)
 promote.py verify T
+  prod_digest() != T      -> superseded (each poll, and before failed)
 promote.yml
   tag
+    re-read DEPLOY_HOLD; tag T superseding before the move
   verify
+    packages: read
   settle
+    read prod before each write; superseded -> no write, no dispatch
 promote_lib/wait.py land_wait
+  soak failure on R and homelab past R -> keep waiting
```

### Options not taken

- **One pointer per service.** Each tag would follow its own `prod-<tag>` and promote,
  verify and hold alone. It gives fully independent promotions, at the cost of
  per-tag `last-good`, holds, resets and stall alerts, and of atomic moves for shared
  kinds and `dependsOn` edges. It reverses
  [ADR-0020](../../adr/0020-flux-applies-the-cluster-from-a-promoted-artifact.md)'s
  single promoted artifact. This slice leaves the cluster at one revision.
- **Enable `CancelHealthCheckOnNewRevision`.** A new revision would cancel any
  Kustomization's health check, which helps overlapping changes rather than disjoint
  ones. It also changes every tag's rollout wait, and #5704 already chose the soak-side
  fix.
- **A batching delay in `decide`.** Waiting a fixed time to collect merges before a
  promotion adds latency to every promotion, and today's walk already batches merges
  that arrive before a `decide`.
- **No disjointness rule.** The mechanism works for an overlapping T: the overlapping
  name finishes P, then applies T. The rule stays because an overlapping failure would
  name T in the hold while P caused it, and because the overlap case gains least.

## Steps, as PRs

Every PR before D7.5 is inert while `SUPERSEDE` is `False`, because no run then
supersedes another.

1. **D7.1 — `settle` writes only while `prod` names T.** The read-compare-write on
   every write in `Apply the verdict`, and `tag`'s re-read of `DEPLOY_HOLD`. The
   workflow tests in `tests/repo/` that pin the step shapes gain the pairs.
   About 120 lines.
1. **D7.2 — `verify` prints `superseded`.** The `prod` read in
   `verify_promotion.py:verify`, the verdict in `report`, `packages: read` on the job,
   and `settle`'s branch for the verdict. `land-wait` re-reads `homelab` before a soak
   failure ends the wait. About 150 lines.
1. **D7.3 — the base. Folded into D7.5.** On its own it added a parameter nothing passed
   until D7.5, so it ships with the code that passes it.
1. **D7.4 — the `superseding` tag.** `tag` tags T `superseding` before the move when
   `decide`'s `supersedes` output is set, and the retention rule keeps the tagged
   version. It replaces the `PROMOTE_CHAIN` variable this plan first named (Open question
   3), so no operator step creates state. About 60 lines.
1. **D7.5 — `supersedable` behind `SUPERSEDE = False`, and the base.** The eight
   conditions and the case table's pairs. `expected_set` and `prior_statuses` take the
   base, which is `prod` unless T supersedes. `decide` prints the superseded commit as
   `supersedes=<P>`. The `post` job posts only snapshot-role holds, which a superseding T
   never writes, so #6199 names P in `settle`'s log and says in `land-wait`'s
   `promote-held` line whether a failed name is the landing PR's own.
1. **D7.7 — `tag` compares what `decide` read.** `decide` outputs the DEPLOY_HOLD value
   it read, and `tag` moves `prod` only while DEPLOY_HOLD still reads it. D7.1's re-read
   accepted only `none`, which would refuse every revert-rule promotion. A superseding
   `tag` also moves nothing once `last-good` has moved since `decide` read it.
1. **D7.6 — turn it on.** **Operator-only ruling**, in a one-line PR of its own, as
   for `REVERT_RULE`.

## Verification

- Each PR's pairs, in `scripts/deploy_tools/tests/` and `tests/repo/`.
- **The live pair**, after D7.6. Merge two PRs that each change one stateless leaf, a
  few minutes apart, so the second merges while the first is in `verify`. Read the two
  promote runs: P's `verify` log prints `superseded`, P's `settle` log names T's move,
  and T's `verify` log lists the names of both. Then
  `uv run python scripts/diagnostics/probe.py landing` shows `last-good` at T and
  `DEPLOY_HOLD` at `none`.
- **The live pair's result, 2026-10-25.** The pair was #6206 (bento-pdf) and #6207
  (ical-proxy), merged 42 seconds apart. Another session's commit, 924f69a9, was already in
  `verify`, so #6206's run superseded it instead of #6207 superseding #6206.
    - Promote run 40197487331's `decide` printed `63d02b85… supersedes the verification of
      924f69a9…`. Its `tag` tagged `sha-63d02b85` `superseding`, then moved `prod`.
    - #6207's first `decide` (run 40197827680) read `superseding` at `prod`. Under the
      at-most-once rule it waited instead of superseding again. A `HTTP 502` reading
      `DEPLOY_HOLD` then ended that run (#6269). A dispatch run, 40197840620, promoted
      3dd97f74 once 63d02b85 settled, with `flux/bento-pdf` and `flux/ical-proxy` both
      recorded `success`.
    - Both landings ended `settled`. `probe.py landing` showed `prod` equal to `last-good`
      and `DEPLOY_HOLD` at `none`.
    - Not exercised: `verify` printing `superseded`. 924f69a9's `verify` returned `good` at
      13:29:09, 14 seconds before 63d02b85's `tag` moved `prod`. Its `settle` then moved
      `last-good` to 924f69a9 one second after `tag` had checked `last-good`. Both writes
      landed, which #6268 records. Had 63d02b85 failed, its reset would have gone to the
      `last-good` from before 924f69a9. The fix for #6268 makes that reset go to the live
      `last-good` instead.
- **The red proof for the live pair** is the failing variant. The second PR breaks a
  readiness probe on its leaf, as #5605 did for speedtest. T's `verify` must print
  `failed` naming that leaf, and `settle` must hold `<T> failed=<leaf>` and reset
  `prod` to the `last-good` from before P.
- **The red proof's result, 2026-10-29.** It ran and passed. The three PRs were:
    - P, #6497, merge 0a064b4b1d at 13:23:12Z: an ical-proxy annotation.
    - T, #6498, merge 0b56c1dd24 at 13:23:57Z: bento-pdf's readinessProbe set to
      `/flux-drill-not-found`.
    - #6508, merged at 13:55:40Z: the revert of T.

  T's promote run is 40882393836. Its logs, quoted without their timestamps, read as follows.
    - `decide` read `last-good` at commit 5abf9894bc, which is before P (committed at
      13:17:20Z), and superseded P's verification:

      ```
      promote.py: recorded on last-good's commit 5abf9894bc9f05ff22722f6a15e96990e4f743d9: flux/bento-pdf=success, flux/ical-proxy=success, flux/netpol-baseline=success, flux/netpol-baseline-deferred=success, flux/netpol-baseline-verify=success, flux/soak=success
      promote.py: 0b56c1dd24067c02a79eb53085aea80ffd0491fb supersedes the verification of 0a064b4b1def60ccab73a3556a538199d2291565
      ```
    - `verify` expected the names of both commits, including `flux/ical-proxy` from P, then
      failed T's leaf:

      ```
      verify: expecting flux/bento-pdf, flux/homelab, flux/ical-proxy, flux/netpol-baseline, flux/netpol-baseline-deferred, flux/netpol-baseline-verify, flux/soak on 0b56c1dd within 19800s
      target=0b56c1dd24067c02a79eb53085aea80ffd0491fb verdict=failed failed=bento-pdf
      ```
    - `settle` held T and reset `prod`:

      ```
      DEPLOY_HOLD set to '0b56c1dd24067c02a79eb53085aea80ffd0491fb failed=bento-pdf'
      prod reset to sha256:1a7369c3a5b9cfe1a8cbd71d03bf01e04802bac131cdbc00dec965c1b89c605d
      ```
    - That reset digest is the `last-good` from before P. `tag` and `settle` both logged it as
      `RESET_DIGEST: sha256:1a7369c3…c605d`, the value `decide` output. It differs from
      `PRIOR_DIGEST: sha256:fb0fae7e…7956`, which is P's digest and the `prod` that `decide`
      read. A registry read on 2026-11-02 resolved `sha-5abf9894bc…` to
      `sha256:1a7369c3a5b9cfe1a8cbd71d03bf01e04802bac131cdbc00dec965c1b89c605d` and
      `sha-0a064b4b1d…` to the `fb0fae7e…` digest.
    - P's own run, 40882216959, ended superseded and wrote nothing. Its `verify` printed
      `target=0a064b4b1def60ccab73a3556a538199d2291565 verdict=superseded failed=`, and its
      `settle` printed `verify read prod moved past
      sha-0a064b4b1def60ccab73a3556a538199d2291565, so this run writes nothing and dispatches
      nothing`. This is the first `verify` that printed `superseded` on production.

  A rerun of the proof, prepared on 2026-11-02 as #6999, #7001 and #7002, was closed
  unmerged once this record was found.
- **The exit pair's result, 2026-11-12.** A natural burst of merges produced it; nobody
  merged to force it. The two PRs were:
    - P, #8140, merge cede3400e6 at 19:30:29Z: k8s role docs and comments. Its render
      changed ten tags, so P's set was 29 names.
    - T, #8149, merge 7465972305 at 19:36:51Z: `k8s/roles/longhorn/CLAUDE.md` and
      a test, with no cluster footprint of its own.

  P's promote run is
  [43290958373](https://github.com/DanielH2018/server/actions/runs/43290958373), and T's is
  [43292081624](https://github.com/DanielH2018/server/actions/runs/43292081624). Their logs,
  quoted without their timestamps, check each exit bullet.
    - T's `tag` moved `prod` to T at 19:39:42Z, before P's `verify` ended at 19:40:02Z.
      T's `decide` read its base on `last-good`'s commit 73e730d65d and superseded P, and
      T's `tag` tagged T `superseding` before the move:

      ```
      promote.py: 74659723050256149802662925faf69a05150355 supersedes the verification of cede3400e6914bbfcbdceeadb9d78535e23cc51b
      tagged sha-74659723050256149802662925faf69a05150355 superseding: it supersedes the verification of cede3400e6914bbfcbdceeadb9d78535e23cc51b
      ```
    - P's `verify` printed `superseded`, and P's `settle` wrote nothing:

      ```
      verify: prod is at sha256:eca3a5fc9dcef8cb8682340767c23c1cc12eaff90414fe433d0dfccc20295f5d, not T's sha256:902092e4fb50bee8f963d4d7a2b383ce3b0fd3c5b95fd7f93d9ffbf021d5622d: a later run moved it, so this verification is superseded
      target=cede3400e6914bbfcbdceeadb9d78535e23cc51b verdict=superseded failed=
      verify read prod moved past sha-cede3400e6914bbfcbdceeadb9d78535e23cc51b, so this run writes nothing and dispatches nothing
      ```
    - T's `verify` expected P's 29 names on T and printed `good`. T's `settle` moved
      `last-good` to T and dispatched the next run:

      ```
      target=74659723050256149802662925faf69a05150355 verdict=good failed=
      last-good moved to sha-74659723050256149802662925faf69a05150355
      dispatched promote for df27b94ccb5e8aa1c3e3dde2b7ce6231a1239314, master's newest green commit, which is newer than 74659723050256149802662925faf69a05150355
      ```
    - `probe.py landing-verdicts` recorded #8140 as
      `VERDICT: settled (PR #8140, cede3400e6…)` at 19:48:12Z, 40 seconds after T's
      `settle` moved `last-good`. It recorded #8149 as `VERDICT: nothing-to-deploy`,
      because T touched no service.
    - Not exercised: the disjoint union. T's own change was empty, so T's expected set
      equals P's set, and the third bullet holds because T added no names. The red proof
      of 2026-10-29 is the run in which T's `verify` expected names from both commits.
      The fourth bullet's timing comparison has no T verification to weigh, for the same
      reason.

## Rollback

- D7.6: set `SUPERSEDE` back to `False`. Runs in flight finish under whichever rule
  their `decide` used, and the `settle` read from D7.1 keeps a mixed pair safe.
- D7.1 to D7.5: each reverts on its own while `SUPERSEDE` is `False`.
- A partial failure leaves at worst a superseded P with no verdict written, and T's
  run owns the cluster. A T whose run dies leaves `prod` and `last-good` apart, which
  every later run reads as in flight, and `PromotionStalled` pages it as today.

## Risks

- **The soak never settles under steady merges.** Each supersession starts a new soak
  Job with its full window. Condition 3 bounds this to one supersession per
  verification, so `last-good` advances at least every second verification. Class 5,
  *sequences treated as transactions*: the bound is what keeps the chain finite.
- **A failure is attributed to T that P caused.** Condition 4 refuses T while P has a
  failure, and condition 5 refuses an overlap. A failure P causes after T's move still
  holds as `<T> failed=<names>`. The hold names P's service, and the reset returns to
  the `last-good` from before P, so the cluster state is right and only the SHA in the
  hold is the later one. `decide`'s and `settle`'s logs name P, and `land-wait` says
  whether a failed name is the landing PR's own (#6199).
- **`verify` reads the wrong `prod`.** A failed registry read keeps the wait going and
  never reads as superseded, the same rule as a failed status read. Class 3, *empty is
  read as clean*: an empty read must not turn into a verdict.
- **The union timeout is longer than P's alone.** It is bounded by
  `chained_timeout.VERIFY_CAP_S`, as every verification is.

## Tests and docs that change in the same PR

- `docs/flux-deploy-plane.md`, *Verification* and *Publishing and promotion*, in D7.2
  and D7.5.
- `docs/landing.md`, the `soak-failed` bullet, in D7.2.
- The marker `DECIDED a verification in flight is guarded by two checks` in
  `verify_promotion.py` gains a sentence in D7.5: the guard is a wait unless
  `supersedable` holds.
- `docs/target-architecture/contracts.md`, C2's *Verification*, *On failure* and
  *A verification in flight blocks the next promotion*, from the request below.

## Complexity delta

Added: one `decide` predicate with eight conditions, one `verify` verdict, one
read-compare-write in `settle`, one repository variable and one flag. About 650 lines
with tests. Removed: nothing. The in-flight wait stays for every case the predicate
refuses.

## Open questions

1. **Is the saving worth the predicate?** *Measured cadence* says the wait it removes
   is about 2.5 minutes, on 14% of promotions, and that the long waits are the ones it
   refuses. Recommendation: do not build D7.1 to D7.5 at this cadence. Re-measure when
   merges or verification times grow, for example once a typical `verify` exceeds 10
   minutes or waits reach a third of promotions. D7.1 has no value on its own, because
   nothing races `settle` while every run waits. **Ruled on 2026-10-24:** the operator
   read these numbers and chose to build D7.1 to D7.5 as planned. D7.6 stays a ruling of
   its own.
1. **Should a stalled verification be cut short instead?** The 296-minute wait came
   from a `verify` that reached its cap. A shorter derived timeout, or a `verify` that
   fails a name once its Kustomization reads `Stalled`, would remove more waiting than
   this slice does. That is C2's open request on the shape of `verify` (D4 Open question
   14), not this slice. Resolution: weigh it in that request.
1. **One supersession per verification.** A deeper chain shortens waits under bursts
   and lengthens the longest time `last-good` can stay behind. Resolution: start at one,
   and revisit with the stall alert's history. A deeper chain needs a counter in place of
   the `superseding` tag.
1. **Does a state variable beat a derivation?** Resolved in D7.4: an OCI tag, written
   by `tag` beside `prod`, in place of a repository variable. It needs no operator step,
   it is read with the registry HEAD `decide` already makes, and it follows the `held`
   tag's pattern.
1. **The C2 request on the shape of `verify`** (D4 Open question 14). If `verify`
   becomes a short re-entering job or a poller, the `superseded` read moves with it.
   Resolution: whichever lands second carries the read.

## Requested amendment

C2 gains, under *Requests on C2*:

- **Superseding promotions** (D7). *Verification* takes the changed set against the
  base, `last-good` unless `prod` names `held`. *On failure* applies a verdict only
  while `prod` names T's digest, and `verify` gains the verdict `superseded`. *A
  verification in flight blocks the next promotion* gains the exception that
  `supersedable` defines. Gate: before D7.5 merges.

## Review log

No review round has run.
