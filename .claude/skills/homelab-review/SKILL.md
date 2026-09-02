---
name: homelab-review
description: Run a multi-agent review of the homelab — dispatches per-domain reviewer agents in parallel, deduplicates their findings into one prioritized report, and recommends next steps. Read-only; does NOT implement, deploy, or commit. Use when the user asks to review the server/homelab for gaps, improvements, or additions, or to audit its state.
allowed-tools: Read, Grep, Glob, Bash, Agent
---

Run a fine-grained, multi-agent review of the homelab: dispatch one read-only reviewer agent per
domain **in parallel**, then synthesize their findings into a single deduplicated, prioritized
report. **READ-ONLY: this skill reviews and recommends — it does NOT implement, deploy, or commit.**
Stop after the report; let the operator drive any changes.

The steps run in the order they are numbered: scope → prime → dispatch → collect → **deduplicate →
verify** → report. Deduplicating before verification is what keeps the skeptic pass from paying
twice for one defect.

## 1. Resolve scope
Default: all six areas. If the user named a subset (e.g. `homelab-review security,network`), run only
those. Home Assistant is NOT one of them — its review lives in the separate `/ha-review` skill; if the
user asks for HA, point them there (or invoke it) rather than folding it in here. Map each area to its
agent:

| Area | Agent | Size |
|---|---|---|
| Security & hardening | `security-review` | opus, effort medium |
| Network & reverse proxy | `homelab-network-diagnostician` | sonnet (frontmatter) |
| Backups & observability | `homelab-backup-observability-reviewer` | opus, effort medium |
| CI/CD & GitOps | `homelab-cicd-reviewer` | opus, effort medium |
| Media & container infra | `homelab-container-reviewer` | sonnet (frontmatter) |
| Docs vs live-config drift | `homelab-docs-freshness-reviewer` | sonnet (frontmatter) |

**Sizing:** reviewer tiers are pinned in each agent's frontmatter so a routine review never
silently rides the session model. Judgment-heavy domains (security, backup/alert-chain, GitOps)
run opus; pattern/consistency scans (container hygiene, docs-vs-config drift) and live-wiring triage
(network) run sonnet. **The three opus finders also pin `effort: medium`** — opus at a high effort
inherited from the session turns a routine six-domain review into an expensive one, and these
reviewers read templates and cite `file:line` rather than solving anything hard. The sonnet
reviewers pin no effort and inherit the session's. Only when the operator asks for a **deep audit**
should you override per-dispatch with a bigger `model` (e.g. the session model); raising effort
needs an edit to the agent, since the Agent tool takes a `model` override but no effort override. The docs-freshness reviewer is
report-only by nature — its findings are stale-doc edits for the operator, never infra changes.

## 2. Prime from memory FIRST (the signal-booster — do this before dispatching)
This is a **mature** setup: a cold agent will re-flag dozens of settled decisions. Read, in this
order:

1. **`homelab-review-standing-donot-reflag`** — the distilled deliberate trade-offs, durable
   refutations, verified-clean list, and the recurring-open register. This is the bulk of the
   priming and it is one file by design: the dated ledgers grow by one per run, and reading all of
   them at the most expensive moment (immediately before a 4–6 way fan-out that each get a slice)
   is what this file replaces.
2. **The newest two dated `review-*-state` memories** — for recency only: what shipped since the
   standing list was last distilled, and this week's refutations with their evidence. Same-day runs
   carry a letter suffix (`review-2026-08-16-state` *and* `…16b-state`); the later is not a superset.
3. Any other accepted-decision memory the auto-memory index surfaces for an in-scope area.

For each area, extract its don't-re-flag items **plus** the discipline: *verify a candidate finding
against the role's CLAUDE.md, role crons, and monitor-bridge `check.py` BEFORE reporting it.* Pull
this at runtime — never rely on a hardcoded list (it goes stale, the exact failure mode these
reviews keep finding). The standing list is a **prior, not a verdict**: re-flag any of it on new
evidence at a cited `file:line`.

**A row's section and its provenance token decide how much work overturning it takes.** They are
not all the same strength, and treating them alike is what makes reviewers either re-derive
settled reasoning or suppress a live finding:

| Where / what | What it means | What overturns it |
|---|---|---|
| `## Deliberate trade-offs`, `[operator]` | The operator ruled. | Only the operator. Report it as a settled decision, never as a finding. |
| `## Deliberate trade-offs`, `[enforced]` | A test, hook or `# DECIDED:` marker makes the alternative fail. | **Run the named check.** Do not re-derive the reasoning — the row already names the thing that decides. |
| `## Durable refutations` | An agent disproved it at a cited `file:line`. | New evidence at a cited `file:line`. This is the ordinary re-flaggable case. |
| `## Open and recurring` (legacy rows only) | The finding is **live**. Some rows also bar a specific remediation. | Nothing — report it as a recurrence and record it with `findings.py touch <n>`, or `findings.py open` if it was never filed. A barred fix means *don't propose that fix*, never *don't report the finding*. |

That last section is closed to new rows: an open or recurring finding is a GitHub Issue, filed
with `findings.py open` and re-observed with `findings.py touch`. It is in the table because
rows written before 2026-09-02 are still there, and because of how the section misfired: two
open findings sat under a *do not re-flag* heading with only their prose saying they were open,
so a reviewer extracting the section's list would have suppressed both. **A barred remediation
is not a settled finding** — a row that prohibits a fix while calling the underlying gap real
belongs in an issue, and step 7 files it.

**Also extract the still-OPEN confirmed findings**, not just the settled ones. A finding carried
across runs must be reported as a **recurrence** — "open since 2026-08-15, third run" — never as a
discovery. A third appearance is an escalation signal (it belongs in a durable owner: a test, a
lint, a CLAUDE.md rule), and re-presenting it as new is how it stays unowned. Verify each is still
live before reporting: the register is only as fresh as the last ledger.

**Run `uv run python scripts/dev/findings.py list` before you read the register.** The open
findings live as GitHub Issues labelled `claude`; that command prints them severity-first with
the first-seen date and the re-observation count. A finding a reviewer surfaces that matches an
open issue is a **recurrence**: record it with `findings.py touch <n> --source review-<date>`
(the third sighting adds `escalated`), never as a new row or a new issue. The list is only as
fresh as the last close, so still confirm each item at a cited `file:line` before reporting it.

**When the standing list and a dated ledger disagree, the ledger wins** and the standing list is
stale — say so in the report so the next distillation fixes it.

**Done when** every in-scope area has a don't-re-flag list *and* its open-recurrence list — or an
explicit "none recorded for this area". An area you skipped reading for is not an area with nothing
to skip.

## 3. Dispatch all selected agents IN PARALLEL
Issue every dispatch in a single message so they run concurrently (one agent per independent domain —
see the `dispatching-parallel-agents` skill; 4–6 parallel reviewers is normal here). Each agent prompt
must include:
- its **scope** (the area's surface);
- the **repo conventions** — nearly every service is a k3s workload, so cite the
  `ansible/roles/k8s/<svc>/templates/` source; for the Pi's Docker services cite
  `ansible/roles/containers/<svc>/templates/`, never the generated `containers/` tree
  (which is untracked and exists only on the Pi). `roles/containers/archive/` is retired
  code — out of scope;
- its **domain don't-re-flag list** (from step 2) + the verify-first discipline;
- the **falsify-before-flag rule**: cite the specific `file:line` that makes a finding true, and cite
  the `file:line` of the defense when clearing one — a comment, a reassuring name (`*_valid`,
  `# intentional`), or "handled by Traefik/Authelia/upstream" is NOT evidence; verify it in code;
- the **`# DECIDED:` rule**: before flagging anything in a role, `grep -rn '# DECIDED:'` that role's
  tree and its `CLAUDE.md`. A `# DECIDED:` marker records a trade-off that was analysed and settled,
  with the reasoning at that line. Re-deriving one costs the operator real time — two reviewers
  re-derived the `restore_sha` prefix trade-off in a single run on 2026-08-22, against an analysis
  written out in `gitops_deploy/CLAUDE.md` specifically to stop it. A marker is still a prior, not a
  verdict: contradict it only with new evidence at a cited `file:line`, and say which marker you are
  contradicting;
- the **output format** below.

**Three surfaces fall between the six areas — name them in the brief or nobody reviews them.**
They are unowned because they sit on a seam, not because they are settled:
- **Host OS and hardware** → give it to the backup/observability brief: the UPS/NUT shutdown
  chain, SMART/scrutiny, disk and sensor health on both nodes and the Pi.
- **Cluster control plane** → give it to the network brief: etcd health, node pressure/taints, and
  MetalLB VIP announcement. The ETP-Local blackout spans network *and* workload placement, which is
  exactly why neither reviewer picks it up unprompted.
- **Cost and quota** → give it to the backup/observability brief: B2 caps and storage headroom.
  Cap breaches are the most recurrent incident class in the memory index and no area owns them.

`security-review` is the only reviewer without `Bash` (see Notes) — don't hand it a brief that
depends on running `git log`, `kubectl`, or `probe.py`; its live/history checks belong to step 6.

## 4. Output format each agent must return
Findings grouped **High / Medium / Low**. Each: a 1-line title, the `file:line` (ansible source), what's
wrong, and a concrete fix — tagged **[GAP] / [IMPROVEMENT] / [ADDITION]**. Note verified-clean areas in
one line. End with a **3-bullet top-priorities** summary. Be specific and skeptical: 5 real findings beat
20 speculative ones.

**Severity is a gate, not a flavour** — it decides what step 6 adversarially verifies, so calibrate it
the same way in every brief:
- **High** — data loss or an unrecoverable restore path, credential/secret exposure, something
  reachable from the internet that shouldn't be, or a deploy/boot path that is already broken.
- **Medium** — real degradation, a missing defense, or an alerting/backup gap that has a workaround
  or needs a second failure to bite.
- **Low** — hygiene, consistency, and clarity: nothing changes behaviour today.

Uncertain between two tiers? Take the higher one — the cost of a wrong tier is one extra skeptic.

## 5. Collect, then deduplicate (before anything is verified)
**Manifest what came back first.** List every agent you dispatched in step 3 and, next to each,
whether it returned findings, returned an explicit "clean", or returned nothing. An agent that
died, errored, or came back empty leaves a **coverage hole** — report it as an unreviewed domain,
never as a clean one. This is the step-6 manifest rule one level up: silence from a reviewer is a
missing answer, not a passing grade. Re-dispatch a failed agent if the run is still cheap; if not,
name the gap in the report.

Then merge findings that several agents surfaced — e.g. a healthcheck gap seen by both the security and
container reviewers is one finding, not two.

**Anti-merge:** only merge findings that are the *same* defect (same `file:line` + same mechanism +
same fix). Do NOT collapse findings that differ in file, parameter, service, or remediation — two
issues that need different fixes are two findings, even at one service. A merged finding keeps the
highest severity of its inputs and lists every agent that raised it.

## 6. Adversarially verify High/Medium findings (before they reach the report)
Reviews here have a misfire history (an Authelia `trusted_proxies` proposal that would have
crash-looped it; a PEP-758 `except X, Y:` misread as a syntax bug; and in the 2026-08-15 run both a
proposed `N8N_PROXY_HOPS` change and a Pi `:latest`-pinning "fix" would have caused damage) — a
wrong finding costs the operator more than a missed one. So: for each deduplicated High/Medium
finding dispatch one **`skeptic`** agent, all in one parallel message. That agent carries the whole
refutation contract — where to look (role CLAUDE.md, tasks/templates, `check.py` + crons,
don't-re-flag memories, `git log`/`git blame`, `gh pr list`, `probe.py`/`kubectl` for live state),
the rule that a comment or a reassuring name is not evidence, and the three verdicts. Do not restate
it in the brief; give the skeptic the finding, its `file:line`, and which reviewer raised it.

**Size the skeptic to the finder.** `skeptic` deliberately pins no `model`, so you set it per
dispatch. It must be at least the tier of the agent that raised the finding: a High from an opus
reviewer (security, backup/observability, CI/CD) gets `model: opus`; everything else runs
`model: sonnet`. A weaker refuter plus an explicit refute-bias is a path for real findings to die in
the appendix, and the appendix is never re-read.

**Merged history is not the whole history.** Several sessions work this repo at once, one branch
each, so a fix can be real and not yet on master. The skeptic checks `gh pr list` itself; what it
cannot see is the SessionStart banner, so pass it the other live worktrees' changed paths when they
touch the finding. A finding already fixed on an open branch is REFUTED-as-live and reported as
"fixed in PR #n, unmerged", not re-raised.

Verdict per finding: **CONFIRMED** (refutation failed), **REFUTED** (cite the disproving
evidence), or **UNCERTAIN**. Refuted findings drop to a one-line "refuted in verification"
appendix; UNCERTAIN ones stay but are marked unverified. Lows skip verification.

**Spot-check a REFUTED verdict's `file:line` against the file.** A refutation is a claim like any
other, and this loop is armoured against false positives and not at all against false negatives:
refuting is cheap and always defensible, confirming carries the risk this skill warns about, so
uncertainty resolves downward and the downward path is one-way. It has produced an invented
refutation. Asked to rule on "qbittorrent's main container has no livenessProbe", a skeptic
announced it had read the real template rather than trust the prompt, then returned REFUTED citing
"39 lines, an httpGet livenessProbe on the qbittorrent container at lines 24-30". The file is
**183 lines**; its only `livenessProbe` is at `:91`, inside the **wireguard** container, while the
qbittorrent container at `:116` has a `readinessProbe` at `:160` and no liveness check. The finding
was live and the refutation was fabricated, and one `grep` settled it. "The decisive check wasn't
run" is a reason to run it, not a verdict — and when reading a past ledger, treat its refuted list
as the least-reviewed part of it.

**Manifest the candidates first:** list every High/Medium finding, then give each its own verdict row —
a candidate with no row was *skipped*, not cleared (a verification miss, not an implicit pass). A verdict
that rests on a comment, a name, or a "by design" claim is not done until it is re-checked against the
executable code.

**Ask whether a fix's evidence is on the same side as the defect.** A verdict has to be discriminating,
not merely available. The staging egress fence (2026-08-27) was a UFW `route deny`, and the evidence
offered for it was that `ufw status` listed the rule — host-side evidence for a claim about what the
*guest* could reach, with the disputed chain sitting between the two. It was inert, and the probe from
inside the guest found the production VIP, the k3s API and an unauthenticated admin UI still answering.
Evidence gathered on the wrong side of the thing in dispute cannot come back negative, so it is not a
check. Name the side the defect lives on, then ask whether the proof was taken there.

Its companion: **the shape tests all passed while the hole was open.** They asserted the rule's TEXT,
which was correct throughout; nothing asserted its EFFECT. A whole file of green guards over an inert
control is the "fires on nothing" case the repo warns about, and it reads exactly like coverage.

## 7. Synthesize and close the loop (your job once the verdicts are in)
- **Surface cross-cutting THEMES** no single agent can see (e.g. a "co-located failure domain" spanning
  security + backups + network) — this is the main value of synthesizing over relaying.
- **Vet every remediation before you recommend it — the fix-skeptic pass.** Findings get step 6 and
  recommendations get nothing, which is the one review-loop gap still producing new memory entries.
  The ledgers measure it: 6 of 14 proposed fixes refuted and 5 more corrected (2026-08-25), 5 of 13
  unsafe (2026-08-24). Over those same runs priming drove findings to 15 confirmed / 0 refuted and
  left fix quality flat — priming feeds the pass that exists. So dispatch one **`skeptic`** per
  recommended remediation, all in one parallel message, sized to the finder exactly as step 6 sizes
  its own. Each answers one question:

  > Does this remediation change the state the finding is about, or only the signal that reported it?

  Three verdicts, and they judge the **fix**, never the finding: **SAFE**; **LAUNDERS** (the symptom
  stops being reported while the underlying state is untouched); **UNSAFE** (it introduces a new
  failure, or is worse than what it replaces). A LAUNDERS or UNSAFE verdict does not retract the
  finding. The finding ships marked **no vetted remediation**, which is a more useful report line
  than a fix the operator has to revert.

  Two shapes have already reached the operator, and they are what this pass exists to catch:
  - **Laundering.** A 2026-08-25 proposal closed its finding by changing what got measured.
  - **A false-GREEN worse than the false-RED it fixes.** Two independent reviewers in one run
    proposed adding `restore-drill` to the stamp task's tags, which would have declared five
    templates current that the run never rendered (2026-08-24). The safe direction was to *drop* a
    tag, not add one.

  An unvetted remediation is not a recommendation. Report the finding without one.
- Present **one consolidated report** grouped by severity, with a top-priorities shortlist and a clear
  recommendation. Cite each finding's ansible `file:line`. Mark each remediation with its fix-skeptic
  verdict, and mark a finding whose only remediation was refuted as **no vetted remediation**.
- **STOP.** Recommend next steps; do not implement, deploy, or commit.
- **File every CONFIRMED finding the operator does not fix in this session** with
  `uv run python scripts/dev/findings.py open --title "<one-line title>" --body-file <f> --severity
  <high|medium|low> --kind <gap|improvement|addition> --domain <step-1 name> --file <file:line>
  --source review-<date>`, adding `--no-vetted-remediation` when both remediations failed step 7.
  The body file carries where, mechanism, fix, and the two verdicts. `open` dedupes by title and
  file, so a re-filed finding touches the existing issue instead of duplicating it; an exit of 3
  means a skeptic refuted it in an earlier run, and the issue holds the evidence — read it before
  re-raising. When the skeptic REFUTES a finding that already has an issue, close it with
  `findings.py close <n> --refuted --reason "<what disproved it>"`.
- Offer to record a new `review-<date>-state` memory — the run narrative step 2 reads. Give it a
  one-line headline; then, **tagged by area**, each confirmed finding as `#<n>` plus what happened
  to it (shipped / filed / touched), each **refuted** finding *with the evidence that disproved it*,
  and the **deliberate trade-offs** not to re-flag. Do not restate an issue's body in the ledger;
  the issue is the record and the ledger links it. If a ledger already exists for today, write the
  next letter suffix rather than overwriting.
- **Then fold the durable half into `homelab-review-standing-donot-reflag`** — a new deliberate
  trade-off, a refutation of a finding that was never filed, or an entry this run proved stale.
  Open and recurring items belong in `findings.py`'s register, not there. Per the
  repo's corroborate-before-promote rule, promote a trade-off or refutation only on a **second**
  independent occurrence or against real evidence.
- **Every row you write into `Deliberate trade-offs` opens with a provenance token** — `[operator]`
  if the operator ruled, `[enforced]` if a named test, hook or `# DECIDED:` marker makes the
  alternative fail. Step 2's table says what each licenses, and the two differ in cost: an
  `[enforced]` row is settled by running the check it names, where an `[operator]` row can only be
  reopened by the operator. A trade-off you cannot label as either is not a trade-off yet — it is
  one run's inference, so leave it in the dated ledger.
- **A row that bars a fix while calling the gap real is an issue, not a `Deliberate trade-offs`
  row.** The heading is what a reviewer extracts against, so an open finding filed under *do not
  re-flag* is suppressed by the very step meant to prime them. File it with `findings.py open`
  (add `--no-vetted-remediation` when every proposed fix failed the fix-skeptic) and write the
  barred remediation into the body, so *don't propose that fix* cannot be read as *don't report
  the finding*. The standing memory keeps only deliberate trade-offs and durable refutations.
- **On its third run, a recurring class stops being a ledger row.** The standing list already counts
  runs per recurring-open class — the guard-scope class reached run 4 and the push-token one run 3,
  each time by incrementing a counter and writing the finding again. Incrementing is what let them
  recur. So when a fold takes a class to **run 3**, the fold is not a counter: it is an executable
  check landed that run, or a written statement in the standing entry of why no check can express it
  and what the operator must decide instead. This is the repo's own escalation ladder
  (run-local note → memory → CLAUDE.md rule → executable check) with a trigger attached, since the
  ladder without one is what produced the counters. A class whose remedy stayed prose is the class
  that comes back.

## Notes
- All six reviewer agents are read-only investigators. `security-review` is the one without `Bash`
  (`Read, Grep, Glob` only), so it cannot run `git log`, `kubectl`, or `probe.py` — its findings reach
  live/history evidence only through the step-6 skeptic pass, which is where that verification belongs.
- Home Assistant review is handled by the separate `/ha-review` skill.
- This skill is the **review** half of the flow only. Implementation (implement → deploy via `/deploy`
  → commit) stays an explicit, operator-gated sequence — keep it out of this skill.
