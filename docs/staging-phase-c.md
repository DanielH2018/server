# Staging Phase C — gating the GitOps pipeline on a staging deploy

Makes `gitops-deploy` deploy a merged change to `daniel-stage` first, and touch prod only if
that succeeded. Phases A and B (`staging-cluster.md`) built a cluster and taught the repo to
deploy to it. This is the phase where the cluster starts refusing things.

**Status as of 2026-09-02: slices 1-4 are built, and the gate BLOCKS on daniel-box.** It asks
daniel-stage about every commit that would auto-deploy a k8s service, logs and alerts the
verdict, and — since `gitops_deploy_staging_gate_blocking` was armed on 2026-09-02 — holds the
SHA and skips prod on a REJECTION. NO VERDICT still deploys prod.

**The entry condition below was met in full before the flip**, which is the whole reason it
exists: 20 consecutive clean gate runs (the ledger read 23/20 with zero needing triage), two
real gated ticks both PASS, and the written NO_VERDICT answer. It was **rescoped 2026-08-30**
because the original version could not be satisfied — the gate is reachable by roughly one real
tick a month, so part 1's evidence was gathered by a deliberate backfill rather than by waiting
on merges. That backfill ratchet was retired on 2026-09-24; see
[After the flip: the ratchet is retired](#after-the-flip-the-ratchet-is-retired).

To return to advisory, set `gitops_deploy_staging_gate_blocking: false` and re-run
`initial_setup.yml --tags gitops_deploy`. Editing the inventory alone does not reach the host: that
change routes to `deploy.yml`, which runs no setup role and re-renders no deployer config.

Set `gitops_deploy_staging_gate: false` and re-run `initial_setup.yml --tags gitops_deploy` to
switch it back off, at which point the deployer behaves exactly as it did before any of this
existed.

---

## What changes, precisely

One arm of the tick, `deploy_handlers.handle_k8s`. Today:

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
change, versus staging itself is broken. A guest that does not boot, a VM host out of disk, an
expired ssh key and a genuine bad manifest all surface as the same message: the staging deploy failed.
The first three are not caused by the change, and an operator who cannot tell them apart quickly
learns to override on reflex.

**Build the override before the gate.** A gate with no escape hatch becomes a gate somebody
deletes at 2 AM, and nobody reviews the deletion. An extra-var or a marker file that skips
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
`scripts/deploy_tools/tests/test_staging_gate.py::test_the_staging_deploy_does_not_refuse_a_tree_behind_the_tip`
and its red proof.

**This removes a slice-4 requirement rather than satisfying one.** The earlier draft of this
section prescribed a third path: re-ask at the new tip, once, and only then decide. Do not
build that — it would ask staging about a SHA this tick is not deploying, which is a worse
question than the one that was being refused. The next tick asks about the new tip on its own.

---

## Decision 5 — the window

A full prod deploy of 54 services measures 20m12s. Six services is a small fraction of that, so
a staging pass should fit inside the 10-minute tick comfortably — but **that is an inference and
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
   `except`. `test_staging_gate_cannot_break_prod.py` is that file after slice 4 rewrote its
   three advisory-specific checks; what it still pins is the property both modes need — a wedged
   guest cannot reach the prod deploy. It is switched on (`gitops_deploy_staging_gate: true`) rather than
   merely built, because building it exercises nothing. **It does not, on its own, collect the
   false-failure rate** — that was this spec's original plan and it does not work; see *Entry
   condition* for why the organic sample rate is about one a month, and what replaced it. What
   slice 3 does supply is the two real gated ticks part 2 of that condition requires, and the
   live path a backfill would otherwise only simulate.
4. **Enforcing mode, with the override.** — DONE, armed on daniel-box 2026-09-02.
   `consult_staging` returns a verdict, `staging_blocks` decides whether it stops the prod
   deploy, and `main()` holds the SHA and skips prod on a rejection. The override shipped in the
   same change, as Decision 4 asks. It stayed false for the hours between the code landing and
   the entry condition being met, which is why it is a second switch rather than a widening of
   `gitops_deploy_staging_gate`: the code and the evidence arrive at different times.

Slice 3 is the point. It is also the one most likely to be skipped, because by then everything
works and enforcing is one flag away.

---

## Entry condition, and why it is not "when the code is ready"

The number matters because it decides whether the gate can be trusted, and trust is the whole
mechanism. A gate whose false-failure rate is unknown blocks a good deploy, gets overridden
once, and then gets overridden by habit — at which point it costs 20 minutes a tick and prevents
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
high bar; it is a condition that gets waived under pressure sooner or later, which is worse than a
lower one honestly stated.

### The rescoped condition (2026-08-30)

The false-failure rate is a property of the gate MECHANISM, not of Renovate's schedule, so it is
measured deliberately. Three parts, all required.

**1. A backfill of 20 consecutive gate runs against real master SHAs, with zero false failures.**

The harness was `scripts/deploy_tools/backfill_staging_gate.py`, retired with its timer on
2026-09-24 (#2414) and recoverable from git history. A real run exited 0 only when the
condition below was met, so it was a check rather than a report to interpret. A `--dry-run`
listed the commits and tags it would gate and touched staging not at all.

Two properties of that script were load-bearing rather than tidy. It gates **oldest-first**,
because the staging checkout only moves forward and asking about a commit older than its HEAD
used to return a verdict about the wrong tree. And it reports a REJECTED as `needs-triage`
rather than guessing: nothing in an exit code distinguishes the gate misfiring from a genuine
defect in that commit, and guessing either way corrupts the measurement in a different
direction. Any run left untriaged holds the verdict at NOT MET.

**History cannot supply 20 samples, and the script does not pretend otherwise.** Measured
2026-08-30, **five** of the last 400 master commits are gateable once three filters apply. The
commit must change a service in the staging subset; staging must have *run* that service at
that commit; and the `ansible.cfg` at that commit must be able to find the Ansible collections.

None of the three is fussiness, and each was found by a run rather than by reading. A commit
predating its role's per-cluster switch deploys prod-shaped config to a cluster that cannot
take it and comes back REJECTED — neither a gate misfire nor a defect in the commit, so it has
no honest triage answer. A commit predating #560 has a repo-relative `collections_path`, and the
gate's checkout installs no collections of its own, so `community.sops.load_vars` is
unresolvable and `deploy.sh` exits 4 in `pre_tasks`. Exit 4 is its *staleness* code, so six of
the first eleven runs read as a stale tree while the real cause was a missing collection.

So the 20 accumulate in a **ledger**: `--jsonl <path>` is read back as well as written, and the
streak spans every recorded run rather than one invocation. Five historical samples plus each
future gated commit reach 20 without a third rescope.

**The ledger ratcheted on a timer, not by hand.** `staging-backfill.timer` on daniel-box ran
the harness hourly with `--since-ledger`, which derived its window from the newest recorded run.
Those commits were descendants of the gate's checkout, so the scheduled form needed no reset.
That is why the ratchet was a different shape from the one-shot backfill.

**Two things watched the ratchet.** `OnFailure=staging-backfill-alert.service` paged when a run
failed. The Kuma monitor "Staging Backfill Ratchet" paged when runs stopped happening at all.
It read a heartbeat that the unit's `ExecStopPost=` wrote to
`/var/lib/gitops-deploy/staging-backfill-last-run`, through monitor-bridge's
`check_staging_backfill_alive`. Neither watched the ledger's outcomes, so a NO VERDICT that broke
the streak paged nothing (#2455). All of it was removed with the ratchet.

**Measured 2026-08-30, the arrival rate is about 2.5 gateable commits a week, and it is lumpy.**
Over 592 master commits in the preceding fortnight, five were gateable — and none at all in the
last thirty merged PRs. So the remaining fifteen samples are roughly six weeks away rather than
days, which is still a different order from the one-a-month tick rate the rescope was reacting
to. Do not read the timer's hourly cadence as the sample rate; it is only how often the harness
checks whether master has produced anything to ask about.

**Contention with the 10-minute tick was a skip, not a sample.** Both reached the same staging
lock. The loser answered PREP_FAILED, which the harness scored as a false failure, so each
collision would have reset the measured streak to zero. For the ratchet's lifetime
`staging_gate_remote.sh` exited a separate `GATE_BUSY` (76) for lock contention, and
`staging_gate.py --report-busy` surfaced it as `NOT_RUN` so the harness recorded nothing. Both
went with the ratchet. The remote reports a busy lock as PREP_FAILED again, and the deployer
reads it as NO_VERDICT, as it always did. `staging_verdict_summary` reads any non-zero that is
not 2 as REJECTED, which is why no third code ever reached the tick.

**A backfill was a one-shot.** The gate's checkout only fast-forwards, so a run left it at the
newest commit in the window and a second pass over the same window was all ancestors. The
script refused a plan its checkout had already moved past, naming the `git reset --hard` that
would make the window runnable.

**That reset was not safe until the runner moved out of the checkout.** The first attempt on
2026-08-30 reset the tree to `9bc53639`, a commit predating the gate itself, and the dispatcher
exec'd `./scripts/deploy_tools/staging_gate_remote.sh` from that tree — which did not exist
there. Eleven runs returned 127, and `staging_gate.py` reported 127 as proof the restricted key
had not authenticated. That was false: `ssh -v` showed the key accepted and the forced command
running. The runner is now installed by `roles/setup/hypervisor` at
`hypervisor_staging_gate_runner_path`, so the gate's mechanism is no longer chosen by the commit
it is judging, and the 127 message names both of its causes.

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

**3. A written answer to what blocking mode does on NO_VERDICT.** — ANSWERED 2026-09-02.

A decision rather than data, and the old condition hid it inside the phrase "false-failure rate."
NO_VERDICT means the gate could not be asked, which is never the change's fault. Blocking on it
parks prod behind staging's availability; passing on it makes any staging outage a way through
the gate.

**The answer is: NO_VERDICT passes through. Only a REJECTED blocks.**

The asymmetry that decides it is the one this spec already used to exclude a false-PASS rate from
the condition. A gate that misses a defect leaves prod exactly where it is today; a gate that
blocks a good change is a regression against today. Blocking on NO_VERDICT would import the
availability of one guest on a NAT network — no HA, not on prod's critical path, covering six services of
fifty-four — into every prod deploy, in exchange for closing a hole whose worst case is the
behaviour prod had before any of this existed.

**The cost is real and is paid for with noise rather than with a block.** A permanently broken
staging degrades the gate to nothing, silently, unless every NO_VERDICT is loud. So it is:
`consult_staging` alerts on every non-PASS, and as of slice 4 that includes the internal-error
path, which returned before reaching the alert while nothing branched on the answer. That alert
is the only signal left. It fires only on a real gated tick, which arrives about once a month,
so a broken staging can go unreported for weeks. The "Staging Backfill Ratchet" monitor was a
second signal until the ratchet was retired on 2026-09-24.

**The operator's route past a block** is `touch /var/lib/gitops-deploy/staging_gate_override` on
daniel-box. It lets exactly one blocking tick through, posts to Discord naming itself when it is
spent, and removes itself; `rm` disarms it before use. It is consumed at the point the gate would
block, never on entry — otherwise arming it before a quiet tick would spend it on a tick that
needed nothing, and the operator's actual push would meet the block with the hatch already gone.

A staging rejection also **holds the SHA** (`hold_sha`), so the block does not re-fire every
tick — which matters here, because re-consulting costs 600s + 120s of the tick's budget and of
the shared tree lock.

**The block and the marker clear on different schedules, and only the first is quick.** The block
stops applying as soon as master moves past the held SHA: `skip_hold` matches only while
`origin_head == hold_sha`. The marker does not clear then. `write_hold(None)` sits in two
branches this host reaches — a successful k8s auto-deploy and a successful broad apply — so the
**GitOps Deploy — Status** monitor, which pages on a non-empty `hold_sha` with no origin
comparison, stays red until one of those lands. At the measured arrival rate for a gated tick
that can be weeks, not the next push. An operator clearing a staging block should expect to
delete the marker by hand, exactly as this role's CLAUDE.md already prescribes for a k8s
rollback whose follow-up commits reach no service here.

### What is deliberately NOT in the condition

**A false-PASS rate.** That measures the gate's *coverage*, which is what the expectation checks
and the subset already bound, and it is the wrong question for this decision. A gate that misses
a defect leaves prod exactly where it is today; a gate that blocks a good change is a regression
against today. Only the second decides whether blocking is safe.

### Where the evidence stands

**Part 1: MET, 2026-09-02.** `backfill_staging_gate.py` reported `clean streak=20/20` over 26
recorded runs — 20 pass, 6 false-failure (all six predating the fixes described above), 0
true-failure, 0 needs-triage. The raw ledger is kept on daniel-box at
`/var/lib/gitops-deploy/staging-backfill.jsonl`, and nothing writes to it after the retirement.

**Part 2: MET 2026-09-02, 2 of 2.** `staging: PASS on ['freshrss']` at 19:47:52 (gate PASS for
`5e06d859`, 1/1 expectations) and `staging: PASS on ['ical-proxy']` at 20:16:25 (gate PASS for
`c5a1b6d4`, **2/2 expectations** — the two-directional check, `/calendar1.ics` 200 AND `/` 404,
the one ical-proxy's own 404 made necessary). Both were real deployer ticks, not hand runs.

The account below is kept because the failed first attempt is the reusable lesson.

**How it stood before those two, and what the first attempt cost.**
`journalctl -u gitops-deploy` carries no `staging:` line at all since the gate was switched on
2026-08-30: not a rejection, not a pass, not even the `nothing to gate` skip. That is the
one-a-month arrival rate showing up exactly as predicted.

#857 bumped freshrss's digest deliberately to force one, which the spec authorises — node-exporter
is already at its current release and ical-proxy's pin has no upstream to move, so freshrss is
the only one of the three that can supply a sample. **It did not produce one.** The pod ended up
on the new digest, so the change shipped; the tick that ff-merged it did not promote it, and
`land.sh` deployed it at step 5 as a deferral instead.

**The reason generalises, and it is what a future attempt has to plan around.** A tick diffs
`local..origin`, not one commit — and **a broad-plane path anywhere in that range pre-empts
every image-bump promotion in it**. `main()` applies the broad plane and returns before
`if cs.k8s_deploy:`, so `consult_staging` is never reached.

That is what happened. `git reflog show master` gives the range the tick fast-forwarded,
`2d8e2270..b49b867a`, and replaying `services_from_changed_paths` over it returns
`broad_setup=True` with `setup_roles=['optimize_pi']` beside `k8s=['freshrss',
'loki-homelab']`. #858 had touched `ansible/roles/setup/optimize_pi/templates/`. freshrss
stayed in `cs.k8s` and defer-alerted; `land.sh` deployed it at step 5.

**It is the range that has to be clean, not the bump's own role.**
`split_k8s_auto_deploy`'s path rule is per-role — `changed_here` filters to paths under
`roles/k8s/<svc>/` — so unrelated files cannot disqualify a service. The broad arm can, and
does, because it returns first.

Measured 2026-09-02 over the 120 most recent master commits, **28 carry a broad-plane path**,
so a one-commit range is clean about three times in four and a three-commit range fewer than
half the time. Forcing a sample therefore means merging the bump and triggering the tick as
soon as CI is green, so the range is that one commit — `land.sh`'s CI wait is precisely what
let #858 into #857's range. At those odds the hand-forced route is a coin-flip repeated, which
is the argument for making a real gated tick ratchet on its own the way part 1 did.

**The second sample needs a second such tick.** `freshrss_k8s_cache_image` — `nginx:alpine`,
pinned at `4a73073b`, and `db35bfc6` upstream as of 2026-09-02 — is the remaining candidate in
that file.

**Part 3: MET, 2026-09-02** — written above, and pinned by
`ansible/roles/setup/gitops_deploy/tests/test_staging_blocking.py::test_no_verdict_never_blocks`.

Older evidence, and not a substitute for part 1: after the staleness fix (#599),
six consecutive hand runs against real master SHAs all returned PASS, two of them through the
restricted key. Those were ad-hoc — varied tags, chosen SHAs — so they are a prior, not the
backfill. Every NO VERDICT observed before that fix was the gate's own staleness bug rather than
staging's opinion, which is the reason the count starts from #599 rather than from 2026-08-28.

One honest input to that rate, from the day the subset landed: staging's own tooling produced
three wrong verdicts (two guard bugs in the variable sentinel, one stand-in value read as
supplied) against one genuine misconfiguration it caught. Early false failures are likely to
outnumber true ones, and staging causes them rather than the change under test.

---

## After the flip: the tick ledger

The entry condition measured the gate before it was armed. This measures it after, and the two
questions are different: part 1 asked whether the gate is trustworthy enough to block on, and
this asks how often the armed gate actually stops a prod deploy.

`consult_staging` appends one row per real gated tick to
`/var/lib/gitops-deploy/staging-ticks.jsonl` (`gitops_markers.MARKERS["staging_ticks"]`), carrying the
SHA, the promoted services, the verdict and the ledger outcome. Until 2026-09-24 the hourly
ratchet's report ended with a section counting it. No automated reader remains. An
operator reads it with `jq`; for example,
`jq -c 'select(.outcome != "pass")' /var/lib/gitops-deploy/staging-ticks.jsonl` lists every tick
that was not a clean pass.

It was always a separate file from the backfill ledger, because the two measure runs of
different scope. A backfill gated the services a commit *changed*, and a tick gates the narrower
set the deployer *promoted*.

**A tick that measured nothing writes nothing.** `consult_staging` returns SKIPPED when the gate
is off and when the tick touched no staging service, and it runs every ten minutes; recording
those would bury the real samples. `staging_tick_outcome` returns None for SKIPPED, and
`test_a_skipped_tick_appends_nothing` is the half of the pair that proves it.

**A rejection is never attributed automatically.** `pass` records as `pass` and NO VERDICT as
`false-failure` — the gate could not be asked, which is never a property of the change — but a
rejection records as `needs-triage`, because it is either the gate misfiring or a real defect
and only an operator can tell which. The report flags the count; nothing else acts on it.

The recorder cannot break a prod deploy. It runs inside `consult_staging`, whose contract is
that no failure path may stop the deploy, so a ledger it cannot write costs the measurement and
nothing else.

---

## After the flip: the ratchet is retired

The operator retired the staging-backfill ratchet on 2026-09-24 (#2414). The case for keeping
it is in closed PR #2457, and the operator read it before deciding. The trade-off is recorded as
a `DECIDED:` marker above the retirement tasks in `roles/setup/gitops_deploy/tasks/install.yml`.

**What was given up.** The hourly ratchet was the only thing that drove the gate regularly: the
restricted ssh key, the dispatcher, the ff-merge and the expectations. A real gated tick reaches
`consult_staging` about once a month. A gate that rots answers NO VERDICT, and `staging_blocks`
blocks only on REJECTED, so a rotted gate lets prod through without blocking anything. The one
alarm left is `consult_staging`'s Discord post on every non-PASS of a real tick.

**Why that was accepted.** Part 1 was met, so the ratchet had finished the measurement it was
built for. Keeping it meant an hourly staging deploy, three systemd units, a Kuma monitor, a
monitor-bridge check and a push token, all for an exercise that raised no alarm. A NO VERDICT
only broke the ledger's clean streak, and the unit tolerated that as a not-met exit (#2455).

**What was removed.** The harness and its tests; the timer, service and `OnFailure=` alert
unit; the armed marker and the heartbeat; `staging_gate.py --report-busy`, the remote's
`GATE_BUSY` and `exit_codes.GATE_NOT_RUN`; monitor-bridge's `check_staging_backfill_alive`; the
"Staging Backfill Ratchet" Kuma monitor and its push token. The gate itself is unchanged:
`staging_gate.py`, `consult_staging`, `staging_blocks`, the override and the tick ledger.

**How the host converges.** `initial_setup.yml --tags gitops_deploy` on daniel-box stops and
disables the timer, stops the service and removes the three unit files. It also clears any
`failed` state those units left, and removes `staging-backfill-armed` and
`staging-backfill-last-run`. `staging-backfill.jsonl` is **kept**. It is the raw evidence
behind *Part 1: MET*, it is small, and nothing reads or writes it any more, so deleting it would
gain nothing and could not be undone. Remove it by hand when that evidence is no longer wanted.

**To exercise the gate by hand**, run `uv run python scripts/deploy_tools/staging_gate.py <sha>
--tags <services>` on daniel-box against master's tip. The gate's checkout only fast-forwards,
so an older commit is refused. That is the call the ratchet made, one commit at a time.

---

## Prerequisites this spec does not own

- **`initial_setup.yml` does not work against `daniel-stage`** (`staging-cluster.md`, Sequencing).
  It matters here if Phase C ever wants staging rebuilt by the same playbook that builds the
  other hosts.
- **The staging subset is six services.** Widening it is a config change, but each addition
  needs the same question asked: does this role mutate anything outside the VM?
