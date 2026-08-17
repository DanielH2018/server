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

**Also extract the still-OPEN confirmed findings**, not just the settled ones. A finding carried
across runs must be reported as a **recurrence** — "open since 2026-08-15, third run" — never as a
discovery. A third appearance is an escalation signal (it belongs in a durable owner: a test, a
lint, a CLAUDE.md rule), and re-presenting it as new is how it stays unowned. Verify each is still
live before reporting: the register is only as fresh as the last ledger.

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
**Manifest the candidates first:** list every High/Medium finding, then give each its own verdict row —
a candidate with no row was *skipped*, not cleared (a verification miss, not an implicit pass). A verdict
that rests on a comment, a name, or a "by design" claim is not done until it is re-checked against the
executable code.

## 7. Synthesize and close the loop (your job once the verdicts are in)
- **Surface cross-cutting THEMES** no single agent can see (e.g. a "co-located failure domain" spanning
  security + backups + network) — this is the main value of synthesizing over relaying.
- Present **one consolidated report** grouped by severity, with a top-priorities shortlist and a clear
  recommendation. Cite each finding's ansible `file:line`.
- **STOP.** Recommend next steps; do not implement, deploy, or commit.
- Offer to record a new `review-<date>-state` memory — the established pattern that keeps the next
  review high-signal, and the thing step 2 reads. It is what step 2 needs or it is noise, so give it:
  a one-line headline; then, **tagged by area** (the step-1 names, so a later run can filter to its
  own domain), the **confirmed** findings and what happened to each (shipped / open / deferred), the
  **refuted** ones *with the evidence that disproved them* (a bare "refuted" teaches the next run
  nothing), and the **deliberate trade-offs** not to re-flag. If a `review-<date>-state` memory
  already exists for today, write the next letter suffix (`…-state`, `…b-state`) rather than
  overwriting — the earlier run's ledger stays live.
- **Then fold the durable half into `homelab-review-standing-donot-reflag`** — a new deliberate
  trade-off, a refutation that will recur, a recurring-open item's incremented run count, or an
  entry this run proved stale. Step 2 reads that file instead of every ledger, so a run that
  writes only its dated ledger quietly re-grows the priming cost this skill was rebuilt to avoid.
  Per the repo's corroborate-before-promote rule, promote an item only on a **second** independent
  occurrence or against real evidence; a single run's say-so stays in the dated ledger.

## Notes
- All six reviewer agents are read-only investigators. `security-review` is the one without `Bash`
  (`Read, Grep, Glob` only), so it cannot run `git log`, `kubectl`, or `probe.py` — its findings reach
  live/history evidence only through the step-6 skeptic pass, which is where that verification belongs.
- Home Assistant review is handled by the separate `/ha-review` skill.
- This skill is the **review** half of the flow only. Implementation (implement → deploy via `/deploy`
  → commit) stays an explicit, operator-gated sequence — keep it out of this skill.
