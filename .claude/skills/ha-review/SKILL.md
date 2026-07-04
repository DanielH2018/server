---
name: ha-review
description: Run a read-only review of the Home Assistant setup — automations, scenes, scripts, template sensors, lighting/fan logic, and HA config. Read-only; does NOT implement, deploy, or commit. Use when the user asks to review or audit Home Assistant for gaps, improvements, or additions.
allowed-tools: Read, Grep, Glob, Bash, Agent
---

Run a read-only review of the Home Assistant domain: dispatch the `home-assistant-engineer` agent
REVIEW-ONLY, adversarially verify its High/Medium findings, and present a prioritized report.
**READ-ONLY: this skill reviews and recommends — it does NOT implement, deploy, or commit.**
Stop after the report; let the operator drive any changes.

## 1. Prime from memory FIRST (the signal-booster — do this before dispatching)
This is a **mature** setup: a cold agent will re-flag dozens of settled decisions. Before dispatching,
read the most recent `review-*-state` memories and the HA-relevant accepted-decision ("don't re-flag")
memories from the auto-memory index. Extract the don't-re-flag items **plus** the discipline: *verify a
candidate finding against the home-assistant role's CLAUDE.md, `sanctioned_writers.yml`, and existing
automations BEFORE reporting it.* Pull this at runtime — never rely on a hardcoded list (it goes stale,
the exact failure mode these reviews keep finding).

## 2. Dispatch the reviewer
Dispatch `home-assistant-engineer` with `model: opus`. Its frontmatter keeps `model: inherit` for its
real engineering work, so a review dispatch must pass `model: opus` explicitly — a routine review must
never silently ride the session model. Only when the operator asks for a **deep audit** should you
override with a bigger `model` (e.g. the session model).

**The agent is read+write — explicitly instruct it to make NO changes, only review.** Its prompt must
include:
- its **scope**: the home-assistant role (`ansible/roles/containers/home-assistant/`) — automations,
  scenes, scripts, template sensors/macros, configuration — plus live state via `scripts/probe.py ha …`;
- the **repo conventions** — `containers/` is generated/read-only, so cite the ansible role source,
  never `containers/`;
- its **don't-re-flag list** (from step 1) + the verify-first discipline;
- the **output format** below.

## 3. Output format the agent must return
Findings grouped **High / Medium / Low**. Each: a 1-line title, the `file:line` (ansible source), what's
wrong, and a concrete fix — tagged **[GAP] / [IMPROVEMENT] / [ADDITION]**. Note verified-clean areas in
one line. End with a **3-bullet top-priorities** summary. Be specific and skeptical: 5 real findings beat
20 speculative ones.

## 4. Adversarially verify High/Medium findings (before they reach the report)
Reviews here have a misfire history — a wrong finding costs the operator more than a missed one. For
each High/Medium finding dispatch one skeptic — `general-purpose`, **`model: sonnet`**, all in one
parallel message — whose ONLY job is to try to **refute** it against: the home-assistant role's
CLAUDE.md (accepted trade-offs + verification traps), the role's tasks/templates and
`sanctioned_writers.yml`, the don't-re-flag memories, and live state via `scripts/probe.py ha …`
(mind the recorder-stale and alias-slug traps — the `ha-verify-state` skill encodes them).
Verdict per finding: **CONFIRMED** (refutation failed), **REFUTED** (cite the disproving evidence), or
**UNCERTAIN**. Refuted findings drop to a one-line "refuted in verification" appendix; UNCERTAIN ones
stay but are marked unverified. Lows skip verification.

## 5. Report
Present the findings grouped by severity with a top-priorities shortlist, citing each finding's ansible
`file:line`. **STOP.** Recommend next steps; do not implement, deploy, or commit. Offer to record a
review-state memory capturing the run's verified-correct + deferred (don't-re-flag) outcomes — the
established pattern that keeps the next review high-signal.

## Notes
- `home-assistant-engineer` is a read+write agent — it MUST be told to review only.
- This skill is the **review** half only. Implementation (`/ha-edit-automation` → `/ha-deploy` →
  `/ha-verify-state`) stays an explicit, operator-gated sequence — keep it out of this skill.
- For a whole-homelab review across the other domains, use `/homelab-review` (HA was split out of it
  into this skill).
