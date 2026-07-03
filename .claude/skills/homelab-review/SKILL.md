---
name: homelab-review
description: Run a multi-agent review of the homelab — dispatches per-domain reviewer agents in parallel, deduplicates their findings into one prioritized report, and recommends next steps. Read-only; does NOT implement, deploy, or commit. Use when the user asks to review the server/homelab for gaps, improvements, or additions, or to audit its state.
allowed-tools: Read, Grep, Glob, Bash, Agent
---

Run a fine-grained, multi-agent review of the homelab: dispatch one read-only reviewer agent per
domain **in parallel**, then synthesize their findings into a single deduplicated, prioritized
report. **READ-ONLY: this skill reviews and recommends — it does NOT implement, deploy, or commit.**
Stop after the report; let the operator drive any changes.

## 1. Resolve scope
Default: all six areas. If the user named a subset (e.g. `homelab-review security,network`), run only
those. Map each area to its agent:

| Area | Agent | Size |
|---|---|---|
| Security & hardening | `security-review` | opus (frontmatter) |
| Network & reverse proxy | `homelab-network-diagnostician` | sonnet (frontmatter) |
| Home Assistant | `home-assistant-engineer` — **invoke REVIEW-ONLY**: it is read+write, so explicitly instruct it to make NO changes, only review | dispatch with `model: opus` |
| Backups & observability | `homelab-backup-observability-reviewer` | opus (frontmatter) |
| CI/CD & GitOps | `homelab-cicd-reviewer` | opus (frontmatter) |
| Media & container infra | `homelab-container-reviewer` | sonnet (frontmatter) |

**Sizing:** reviewer tiers are pinned in each agent's frontmatter so a routine review never
silently rides the session model. Judgment-heavy domains (security, backup/alert-chain, GitOps)
run opus; pattern/consistency scans (container hygiene) and live-wiring triage (network) run
sonnet. `home-assistant-engineer` keeps `model: inherit` for its real engineering work, so
review-only dispatches must pass `model: opus` explicitly. Only when the operator asks for a
**deep audit** should you override per-dispatch with a bigger `model` (e.g. the session model).

## 2. Prime from memory FIRST (the signal-booster — do this before dispatching)
This is a **mature** setup: a cold agent will re-flag dozens of settled decisions. Before dispatching,
read the most recent `review-*-state` memories and the accepted-decision ("don't re-flag") memories
from the auto-memory index. For each area, extract its relevant don't-re-flag items **plus** the
discipline: *verify a candidate finding against the role's CLAUDE.md, role crons, and monitor-bridge
`check.py` BEFORE reporting it.* Pull this at runtime — never rely on a hardcoded list (it goes stale,
the exact failure mode these reviews keep finding).

## 3. Dispatch all selected agents IN PARALLEL
Issue every dispatch in a single message so they run concurrently (one agent per independent domain —
see the `dispatching-parallel-agents` skill; 5–6 parallel reviewers is normal here). Each agent prompt
must include:
- its **scope** (the area's surface);
- the **repo conventions** — `containers/` is generated/read-only, so cite the
  `ansible/roles/containers/<svc>/templates/` source, never `containers/`;
- its **domain don't-re-flag list** (from step 2) + the verify-first discipline;
- the **output format** below.

## 4. Output format each agent must return
Findings grouped **High / Medium / Low**. Each: a 1-line title, the `file:line` (ansible source), what's
wrong, and a concrete fix — tagged **[GAP] / [IMPROVEMENT] / [ADDITION]**. Note verified-clean areas in
one line. End with a **3-bullet top-priorities** summary. Be specific and skeptical: 5 real findings beat
20 speculative ones.

## 5. Synthesize (your job once agents return)
- **Deduplicate** findings multiple agents surfaced (e.g. a healthcheck gap seen by both the security
  and container reviewers — report it once).
- **Surface cross-cutting THEMES** no single agent can see (e.g. a "co-located failure domain" spanning
  security + backups + network) — this is the main value of synthesizing over relaying.
- Present **one consolidated report** grouped by severity, with a top-priorities shortlist and a clear
  recommendation. Cite each finding's ansible `file:line`.
- **STOP.** Recommend next steps; do not implement, deploy, or commit. Offer to record a new
  `review-<date>-state` memory capturing the run's verified-correct + deferred (don't-re-flag) outcomes
  — the established pattern that keeps the next review high-signal.

## Notes
- All six agents are read-only investigators; `home-assistant-engineer` is the only read+write one, so
  it MUST be told to review only.
- This skill is the **review** half of the flow only. Implementation (implement → deploy via `/deploy`
  → commit) stays an explicit, operator-gated sequence — keep it out of this skill.
