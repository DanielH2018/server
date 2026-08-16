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
| Security & hardening | `security-review` | opus (frontmatter) |
| Network & reverse proxy | `homelab-network-diagnostician` | sonnet (frontmatter) |
| Backups & observability | `homelab-backup-observability-reviewer` | opus (frontmatter) |
| CI/CD & GitOps | `homelab-cicd-reviewer` | opus (frontmatter) |
| Media & container infra | `homelab-container-reviewer` | sonnet (frontmatter) |
| Docs vs live-config drift | `homelab-docs-freshness-reviewer` | sonnet (frontmatter) |

**Sizing:** reviewer tiers are pinned in each agent's frontmatter so a routine review never
silently rides the session model. Judgment-heavy domains (security, backup/alert-chain, GitOps)
run opus; pattern/consistency scans (container hygiene, docs-vs-config drift) and live-wiring triage
(network) run sonnet. Only when the operator asks for a **deep audit** should you override
per-dispatch with a bigger `model` (e.g. the session model). The docs-freshness reviewer is
report-only by nature — its findings are stale-doc edits for the operator, never infra changes.

## 2. Prime from memory FIRST (the signal-booster — do this before dispatching)
This is a **mature** setup: a cold agent will re-flag dozens of settled decisions. Before dispatching,
read **every** `review-*-state` memory, newest first (there is more than one, and same-day runs carry
a letter suffix — e.g. `review-2026-08-15-state` *and* `review-2026-08-15b-state`; the later one is
not a superset of the earlier), plus the accepted-decision ("don't re-flag") memories from the
auto-memory index. For each area, extract its relevant don't-re-flag items **plus** the
discipline: *verify a candidate finding against the role's CLAUDE.md, role crons, and monitor-bridge
`check.py` BEFORE reporting it.* Pull this at runtime — never rely on a hardcoded list (it goes stale,
the exact failure mode these reviews keep finding).

**Done when** every in-scope area has a don't-re-flag list — or an explicit "none recorded for this
area". An area you skipped reading for is not an area with nothing to skip.

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

## 5. Deduplicate (before anything is verified)
Merge findings that several agents surfaced — e.g. a healthcheck gap seen by both the security and
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
finding dispatch one skeptic — `general-purpose`, **`model: sonnet`**, all in one
parallel message — whose ONLY job is to try to **refute** it against: the role's CLAUDE.md
(accepted trade-offs), the role's tasks/templates + shared macros, monitor-bridge `check.py` +
role crons, the don't-re-flag memories, **git history (`git log`/`git blame` the cited lines — a
finding already fixed in a later commit or intentionally reverted with a rationale is not live)**,
and live state via `scripts/probe.py` where relevant. **`probe.py health <svc>` shells `docker
inspect`, so it only answers for the Pi's Compose services** — for a cluster workload use
`kubectl get`/`logs`/`describe` (the readonly SA covers get/list/watch, not Secrets or `exec`), or
`probe.py targets | metric | alerts | b2-longhorn`.

**Merged history is not the whole history.** Several sessions work this repo at once, one branch each,
so a fix can be real and not yet on master: also check `gh pr list` (and `gh pr diff <n>` when a title
looks related) plus the other live worktrees' changed paths from the SessionStart banner. A finding
already fixed on an open branch is REFUTED-as-live and reported as "fixed in PR #n, unmerged", not
re-raised.

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

## Notes
- All six reviewer agents are read-only investigators. `security-review` is the one without `Bash`
  (`Read, Grep, Glob` only), so it cannot run `git log`, `kubectl`, or `probe.py` — its findings reach
  live/history evidence only through the step-6 skeptic pass, which is where that verification belongs.
- Home Assistant review is handled by the separate `/ha-review` skill.
- This skill is the **review** half of the flow only. Implementation (implement → deploy via `/deploy`
  → commit) stays an explicit, operator-gated sequence — keep it out of this skill.
