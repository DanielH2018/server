# Staging Phase C — gating the GitOps pipeline on a staging deploy

Makes `gitops-deploy` deploy a merged change to `daniel-stage` first, and touch prod only if
that succeeded. Phases A and B (`staging-cluster.md`) built a cluster and taught the repo to
deploy to it. This is the phase where the cluster starts refusing things.

**Status as of 2026-08-30: slices 1-3 built, and the gate is ON in advisory mode on
daniel-box.** It asks daniel-stage about every commit that would auto-deploy a k8s service,
logs and alerts the verdict, and deploys prod either way. Slice 4 (blocking) is not started,
and its entry condition is evidence rather than effort — see *Entry condition* at the end,
**rescoped 2026-08-30** because the original version could not be satisfied: the gate is
reachable by roughly one real tick a month, so the evidence is now gathered by a deliberate
backfill rather than by waiting on merges.

Set `gitops_deploy_staging_gate: false` and re-run `initial_setup.yml --tags gitops_deploy` to
switch it back off, at which point the deployer behaves exactly as it did before any of this
existed.

---

## What changes, precisely

One arm of `gitops_deploy.py:main()`. Today:

```
cs.k8s_deploy → git merge --ff-only origin → deploy_k8s(prod) → on failure: hold + reset + revert
```

Phase C:

```
cs.k8s_deploy → git merge --ff-only origin → deploy_k8s(STAGING)
                                              ├── fail → hold + reset. Prod never ran.
                                              └── pass → deploy_k8s(prod) → existing failure path
```

The ff-merge stays first: staging has to render the commit under test, and the deployer renders
from its own checkout.

**A staging failure is a cheaper failure than the one we have today.** The current path deploys
prod, discovers the problem, then holds, resets *and* reverts each claimed volume to a
pre-apply snapshot. If staging fails, prod was never applied — so there is nothing to revert,
and `k8s/volume-revert` is not involved at all. That asymmetry is the main prize, and it is
worth more than the gate's ability to catch any specific bug.

---

## Decision 1 — where the staging deploy runs, and why this is the hard part

**The deployer cannot reach staging.** `gitops-deploy.service` runs on daniel-box
(`has_gitops: true` there and nowhere else). `daniel-stage` sits on a libvirt NAT network
inside daniel-server, reachable from that host only — *Decision 2* of the Phase A/B spec, and
deliberate: a staging cluster that could announce on the prod L2 is the one design where
staging can hurt prod.

So Phase C cannot be a step added to `main()`. Something has to cross a host boundary. Three
ways, and the choice shapes everything else:

| Option | Shape | Cost |
|---|---|---|
| **A. Deployer shells to daniel-server** | daniel-box `ssh daniel-server 'deploy.sh --tags … -e target=daniel-stage'` | A systemd unit gains an ssh key and a second host's checkout state as a dependency. daniel-server's tree must be at the same SHA, which nothing guarantees. |
| **B. A second deployer on daniel-server** | daniel-server runs its own staging-only unit; daniel-box waits on a verdict it publishes | Two units, two schedules, and a verdict-freshness problem — a stale pass is worse than no gate. |
| **C. Route staging from daniel-box** | Add a route so daniel-box can reach 192.168.140.0/24 | Cheapest to build, and it widens the surface *Decision 2* narrowed. Inbound routing is not the egress fence, but it is one firewall rule away from being. |

**Recommendation: A.** It keeps one deployer, one schedule and one verdict, and the
checkout-state dependency is solvable — the staging deploy can be told which SHA to render
rather than trusting whatever daniel-server happens to be at. B's stale-verdict failure mode is
the dangerous one: a gate that passes because it is reading yesterday's answer is worse than
having no gate, because it is trusted. C should be rejected explicitly rather than left as a
tempting shortcut, and this document is where that rejection is recorded.

**Whichever is chosen, `--tags` scoping is not optional.** `deploy.sh` resolves its checkout
from the working directory, and staging's own inventory is what makes `-e target=daniel-stage`
mean anything. Both are properties of the host the command runs on, not of the deployer.

---

## Decision 2 — what counts as a pass

**Start with: the playbook exited zero.** That is a stronger signal than it sounds, because
`deploy_k8s` deliberately has no health-poll phase of its own — the gate lives inside the play.
`roles/k8s/manifests` applies, `roles/k8s/rollout-drain` waits on `rollout status`, and
`post_tasks/k8s_stabilise_gate.yml` hard-fails on a restart-count delta or a readiness
shortfall. A non-zero exit already means the workload did not come up.

**And do not stop there, because that gate has a measured blind spot.** On 2026-08-28 the
staging deploy of `ical-proxy` reported `68 ok, 3 changed, 0 failed` with the stabilisation gate
passing, and every route on the service returned 404 — its `ClientIP` guard named a LAN address
no NAT guest can present, so the route was unsatisfiable by construction. Pod health cannot see
that. Neither can rollout status. The same session saw an Authelia-fronted route where a 200
would have meant *failure* (the middleware had not applied) and a 302 was the pass.

So the pass criteria has two parts:

1. **The play exits zero.** Necessary, automatic, already built.
2. **Each gated service answers on its own route the way that service is supposed to answer.**
   Not "returns 200" — `freshrss` behind forward-auth must return 302, `ical-proxy` must return
   200 on `/calendar1.ics` and 404 on `/`. This is per-service expected behaviour, and it has to
   be declared somewhere rather than inferred.

Part 2 is the real design work in Phase C. The cheapest honest form is a per-service expectation
in the inventory entry — a path and an expected status — checked after the play. Anything
weaker re-creates the exact failure this section documents.

**Do not reuse `probe.py health <svc>` as the gate.** Run from daniel-box it authenticates
against prod's cluster and reports prod's healthy copy of the same service name. Green, and
about the wrong cluster. Staging and prod share a domain and a service naming scheme; the only
reliable discriminator is `CN = TRAEFIK DEFAULT CERT` on the staging VIP.

---

## Decision 3 — what happens to a change staging cannot gate

Staging runs six services of roughly fifty-four. **A gate over a subset gates only that
subset**, and the Phase A/B spec calls this the design's single most important limitation.

Three cases, and each needs a stated answer rather than a default:

- **Change touches only subset services.** Gate normally. This is the case the gate exists for.
- **Change touches only non-subset services.** Staging has nothing to say. Deploy to prod as
  today — but say so in the log, because a silent skip and a silent pass look identical
  afterwards.
- **Change touches both.** Gate on the subset half. Deploying the half staging never saw to prod on the
  strength of an unrelated service's staging pass is the failure mode this bullet exists to name.

The temptation once the tile is green is to read it as the deploy being safe rather than as six
services having rendered and started. Whatever the log says on a skip is the main defence against that.

---

## Decision 4 — failure handling, and the override

**On a staging failure: hold the SHA, reset the tree, do not touch prod.** Reuse the existing
`write_hold` + `git reset --hard local` path. No volume revert — prod was never applied.

**The alert must distinguish two things the operator treats differently:** staging rejected the
change, versus staging itself is broken. A guest that will not boot, a VM host out of disk, an
expired ssh key and a genuine bad manifest all surface as the same message: the staging deploy failed.
The first three are not caused by the change, and an operator who cannot tell them apart quickly
learns to override on reflex.

**Build the override before the gate.** A gate with no escape hatch becomes a gate somebody
deletes at 2 AM, and the deletion will not be reviewed. An extra-var or a marker file that skips
the staging step for one tick, alerting loudly that it was used, is sufficient — the requirement
is that using it is easy and *visible*, not that it is hard.

**~~Staging can only be asked about the tip, and slice 4 has to answer for that.~~ RESOLVED
2026-08-29 — and the premise was wrong.** The remote script fetches, then fast-forwards the
staging checkout to the SHA under test, and `deploy.sh` then refuses any tree behind
`origin/master` (exit 4). So a merge landing anywhere in the window between the tick reading
`origin` and the staging deploy finishing turned a perfectly good change into NO VERDICT.
Observed 2026-08-28 on the first hand-run of the gate: `720cb6b0` was master's tip when the run
started, `#567` merged while it deployed, and the gate returned exit 2 — correctly, since
`deploy.sh` had exit 4 and slice 1's `classify()` maps that to NO VERDICT rather than a
rejection. It happened twice more on 2026-08-29, in two of four hand-runs.

**The gate was never asking about the tip, so the staleness refusal was measuring the wrong
property.** `gitops_deploy.py`'s `main()` resolves `origin` ONCE, ff-merges to it, calls
`consult_staging(cs.k8s_deploy, origin)` with that same SHA, and then deploys that same tree —
all inside `if cs.k8s_deploy:`. A merge landing mid-run moves the tip but changes nothing about
what this tick ships. Prod gets the pinned SHA and staging was asked about the pinned SHA;
they agree by construction, whatever master does meanwhile.

`deploy.sh`'s staleness guard is right for a production host, where a tree behind origin renders
stale templates and reverts live config while every repo-side check reads green. On the staging
checkout, being behind origin is the *intended* state. The remote script therefore passes
`--skip-staleness-check`, with the reasoning at the line, pinned by
`scripts/deploy_tools/test_staging_gate.py::test_the_staging_deploy_does_not_refuse_a_tree_behind_the_tip`
and its red proof.

**This removes a slice-4 requirement rather than satisfying one.** The earlier draft of this
section prescribed a third path: re-ask at the new tip, once, and only then decide. Do not
build that — it would ask staging about a SHA this tick is not deploying, which is a worse
question than the one that was being refused. The next tick asks about the new tip on its own.

---

## Decision 5 — the window

A full prod deploy of 54 services measures 20m12s. Six services is a small fraction of that, so
a staging pass should fit inside the 30-minute tick comfortably — but **that is an inference and
the spec it comes from says to measure before sizing.** An earlier draft of the Phase A/B spec
reasoned from a stale 59-minute figure and concluded the window roughly doubles, which was
wrong.

Measure the staging half specifically: the six-service deploy, cold and warm. `K8S_DEPLOY_TIMEOUT_S`
is 900s today and applies per `deploy_k8s` call, so a staging step inherits it unless given its
own — which it should be, because a staging timeout and a prod timeout mean different things.

---

## Sequencing

Vertical slices; each leaves something exercisable, and the gate arrives last on purpose.

1. **Reachability, decided and built.** — DONE (#559, `scripts/deploy_tools/staging_gate.py`).
   Implement Decision 1 without wiring it to anything:
   daniel-box can cause a staging deploy of a named SHA and read its exit code. Exercisable by
   running it by hand.
2. **Per-service expectations, declared and checked.** — DONE (#564,
   `scripts/deploy_tools/staging_expectations.py`). Add the expectation to each subset
   service's inventory entry and a checker that reads them. Exercisable against staging as it
   stands today — and it should immediately reproduce the `ical-proxy` 404 if pointed at the
   pre-#548 config.
3. **Advisory mode.** — BUILT (#566) and ON. `consult_staging()` runs both checks, logs and
   alerts the verdict, and deploys prod regardless. It is advisory *by construction*, not by
   intent: the function returns nothing, every child process it starts sits inside a broad
   `except`, and `test_staging_gate_is_advisory.py` fails if either changes or if `main()`
   starts branching on it. It is switched on (`gitops_deploy_staging_gate: true`) rather than
   merely built, because building it exercises nothing. **It does not, on its own, collect the
   false-failure rate** — that was this spec's original plan and it does not work; see *Entry
   condition* for why the organic sample rate is about one a month, and what replaced it. What
   slice 3 does supply is the two real gated ticks part 2 of that condition requires, and the
   live path a backfill would otherwise only simulate.
4. **Enforcing mode, with the override.** Flip advisory to blocking. Ship the override in the
   same slice, never later.

Slice 3 is the point. It is also the one most likely to be skipped, because by then everything
works and enforcing is one flag away.

---

## Entry condition, and why it is not "when the code is ready"

The number matters because it decides whether the gate can be trusted, and trust is the whole
mechanism. A gate whose false-failure rate is unknown will block a good deploy, get overridden
once, and then get overridden by habit — at which point it costs 20 minutes a tick and prevents
nothing. That reasoning is unchanged. What changed is how the number is obtained.

### The original condition could not be met, and waiting was never going to fix it

The Phase A/B spec gated Phase C on slice 6 having run against real merges for long enough to
know its false-failure rate, and said slice 3 would collect it. The clock started 2026-08-28.
**Thirty-six hours later it had produced zero samples, and that is the expected result rather
than bad luck.**

`consult_staging` runs only when a tick carries `cs.k8s_deploy`, so a verdict needs a service
that is in the staging subset AND auto-deployable AND image-pin-bumped by that commit. Measured
2026-08-29:

| subset service | `k8s_autodeploy` | can a tick ever gate it? |
|---|---|---|
| traefik | false | no |
| authelia | false | no |
| registry | false | no |
| freshrss | true | yes |
| node-exporter | true | yes |
| ical-proxy | true | yes |

Half the subset is structurally unreachable by a tick, and no image-pin bump landed for the other three in
the preceding three weeks — the bumps that did land were Traefik, Prometheus and the OpenTelemetry
collector, none of them in that set. The organic rate is on the order of **one sample a month**,
so a *rate* is not reachable by waiting at all. An entry condition that cannot be met is not a
high bar; it is a condition that will eventually be waived under pressure, which is worse than a
lower one honestly stated.

### The rescoped condition (2026-08-30)

The false-failure rate is a property of the gate MECHANISM, not of Renovate's schedule, so it is
measured deliberately. Three parts, all required.

**1. A backfill of 20 consecutive gate runs against real master SHAs, with zero false failures.**

Run it with `uv run python scripts/deploy_tools/backfill_staging_gate.py`, `--dry-run` first —
that lists the commits and the tags it would gate and touches staging not at all. It exits 0
only when the condition below is met, so it is a check rather than a report to interpret.

Two properties of that script are load-bearing rather than tidy. It gates **oldest-first**,
because the staging checkout only moves forward and asking about a commit older than its HEAD
used to return a verdict about the wrong tree. And it reports a REJECTED as `needs-triage`
rather than guessing: nothing in an exit code distinguishes the gate misfiring from a genuine
defect in that commit, and guessing either way corrupts the measurement in a different
direction. Any run left untriaged holds the verdict at NOT MET.

**History cannot supply 20 samples, and the script does not pretend otherwise.** Measured
2026-08-30, eleven of the last 400 master commits are gateable once two filters apply: the
commit must change a service in the staging subset, and staging must have *run* that service at
that commit. The second filter is not fussiness — a commit predating its role's per-cluster
switch deploys prod-shaped config to a cluster that cannot take it and comes back REJECTED, an
outcome that is neither a gate misfire nor a defect in the commit and so has no honest triage
answer. So the 20 accumulate in a **ledger**: `--jsonl <path>` is read back as well as written,
and the streak spans every recorded run rather than one invocation. Eleven historical samples
plus each future gated commit reach 20 without a third rescope.

**A backfill is a one-shot.** The gate's checkout only fast-forwards, so a run leaves it at the
newest commit in the window and a second pass over the same window is all ancestors. The script
reads that checkout's HEAD before running and refuses a plan it has already moved past, naming
the `git reset --hard` that would make the window runnable — a refusal is cheaper than eleven
false failures that say nothing about the gate. Resuming a run that died partway needs another
reset for the same reason.

- A *false* failure is any non-PASS whose cause is the gate rather than the change: staleness,
  prep failure, ssh transport, dispatcher refusal, timeout, lock contention.
- A REJECTED traced to a genuine defect in that SHA is a **true** failure. It does not break the
  run, is recorded separately, and is evidence *for* the gate.
- Each run must use a tick's own shape: the SHA that was master's tip, and tags equal to that
  services changed by that commit, intersected with `STAGING_SUBSET`. A backfill that gates services no
  tick would have gated measures a gate nobody runs.
- **Consecutive, not averaged.** A fix that takes the failure rate from 60% to 5% is not ready,
  and a mean over the whole history hides exactly that.

**2. At least two real gated ticks, both PASS.**

Not a rate — proof that the invocation path works at all. The backfill drives `staging_gate.py`
from an operator shell; the deployer drives `consult_staging` from a systemd unit, under
`uv run --no-project`, from a different working directory and a different environment. That
difference has already produced two defects no harness could have seen (#569: `sys.executable`
resolving to whichever venv sat in `WorkingDirectory`, and `uv run` picking its project from
cwd). If no eligible bump lands naturally, force one by bumping an image pin on freshrss,
node-exporter or ical-proxy.

**3. A written answer to what blocking mode does on NO_VERDICT.**

A decision rather than data, and the old condition hid it inside the phrase "false-failure rate." NO_VERDICT
means the gate could not be asked, which is never the change's fault. Blocking on it parks prod
behind staging's availability; passing on it makes any staging outage a way through the gate.
Slice 4 must state which, and if the answer is to block, it must also state the operator's route
past it — the override this slice ships anyway.

### What is deliberately NOT in the condition

**A false-PASS rate.** That measures the gate's *coverage*, which is what the expectation checks
and the subset already bound, and it is the wrong question for this decision. A gate that misses
a defect leaves prod exactly where it is today; a gate that blocks a good change is a regression
against today. Only the second decides whether blocking is safe.

### Where the evidence stands

Not yet satisfied. Encouraging, and not a substitute for part 1: after the staleness fix (#599),
six consecutive hand runs against real master SHAs all returned PASS, two of them through the
restricted key. Those were ad-hoc — varied tags, chosen SHAs — so they are a prior, not the
backfill. Every NO VERDICT observed before that fix was the gate's own staleness bug rather than
staging's opinion, which is the reason the count starts from #599 rather than from 2026-08-28.

One honest input to that rate, from the day the subset landed: staging's own tooling produced
three wrong verdicts (two guard bugs in the variable sentinel, one stand-in value read as
supplied) against one genuine misconfiguration it caught. Early false failures are likely to
outnumber true ones, and they will be caused by staging rather than by the change under test.

---

## Prerequisites this spec does not own

- **`initial_setup.yml` does not work against `daniel-stage`** (`staging-cluster.md`, Sequencing).
  It matters here if Phase C ever wants staging rebuilt by the same playbook that builds the
  other hosts.
- **The staging subset is six services.** Widening it is a config change, but each addition
  needs the same question asked: does this role mutate anything outside the VM?
