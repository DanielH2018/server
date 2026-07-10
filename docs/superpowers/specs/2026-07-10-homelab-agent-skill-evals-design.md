# Homelab-local agent/skill evals — design

**Date:** 2026-07-10
**Status:** approved (design), pre-implementation
**Scope (v1):** the `/homelab-review` fleet — the 5 reviewer agents plus the `/homelab-review`
orchestration skill.

## Problem

The custom Claude Code subagents defined in **chezmoi** (`implementer`, `planner`,
`migration-reviewer`) already have a regression harness at `<chezmoi>/evals/` — fixed prompt →
deterministic assertion gate → opus LLM judge, run `k` times for consistency (design:
`<chezmoi>/docs/superpowers/specs/2026-07-08-subagent-evals-design.md`).

The **homelab-local** agents and skills in `~/server/.claude/` have no such coverage. We want to
evaluate them — starting with the `/homelab-review` flagship — so that a change to a reviewer
agent's prompt (or the orchestration skill) that regresses its judgment or output discipline is
caught, not discovered live during a real review.

Two structural mismatches make this non-trivial:

1. **The reviewer agents investigate a real repo.** Their prompts tell them to read `file:line`,
   cite `ansible/roles/...` sources, and verify candidate findings against role `CLAUDE.md`,
   role crons, and `monitor-bridge check.py`. The chezmoi harness deliberately runs agents with
   `--tools ""` (no filesystem) to stay deterministic, hermetic, and side-effect-free.
2. **`/homelab-review` is an orchestration *skill*, not an agent.** It resolves scope, primes from
   memory, dispatches reviewer agents **in parallel**, deduplicates, adversarially verifies, and
   synthesizes one report. The chezmoi harness's `--agent <name>` mode runs the main loop as a
   single agent and, per that harness's own README, does **not** exercise nested `Task`-dispatch
   subagent semantics.

## Decisions (locked in brainstorming)

1. **Scope v1** — the `/homelab-review` fleet only: agents `security-review`,
   `homelab-network-diagnostician`, `homelab-backup-observability-reviewer`,
   `homelab-cicd-reviewer`, `homelab-container-reviewer`, plus the `homelab-review` skill. HA plane
   and side-effecting action skills (`deploy`, `add-secret`, `new-container`, `z2m-device-setting`)
   are deferred.
2. **Grading model — hybrid.** An embedded-snippet, no-tools tier for the bulk of agent cases
   (reuses the chezmoi engine as-is), plus a small live tier for end-to-end/orchestration.
3. **Home + reuse — one engine, cases in `~/server`.** The chezmoi engine stays the single source
   of truth. Homelab cases live in `~/server/evals/cases/` (co-located with the agents they test),
   discovered via a new `EVAL_CASE_DIRS` env var that mirrors the existing `EVAL_AGENT_DIRS`. The
   reviewer agents load via `EVAL_AGENT_DIRS=~/server/.claude/agents`.
4. **Orchestration skill grading — behavior-contract + live smoke.** Default: hermetic
   "pseudo-agent" cases that grade the skill's synthesis contract (dedup / drop-settled /
   prioritize / STOP). Plus one live end-to-end case, run manually, to confirm real parallel
   dispatch still works.

## Architecture

One engine, external cases and agents. Three small, backward-compatible extensions to the chezmoi
harness; the CI/hermetic default path is unchanged when the new env vars are unset.

### Engine extension 1 — `EVAL_CASE_DIRS` (case discovery)

`run-evals.mjs`'s `loadCases()` currently reads only `<chezmoi>/evals/cases`. Generalize it to
iterate `[builtinCasesDir, ...envCaseDirs()]`, where `envCaseDirs()` parses a colon-separated
`EVAL_CASE_DIRS` (empty by default, exactly like `envAgentDirs()`). Each case's target agent is
already carried by the case JSON's own `"agent"` field, so a homelab case at
`~/server/evals/cases/security-review/001-*.json` sets `"agent": "security-review"` and needs no
other wiring. The `--agent` / `--case` filters keep working across all roots.

### Engine extension 2 — reviewer agents via existing `EVAL_AGENT_DIRS` (no code change)

`loadAgentFromRepo` already searches `EVAL_AGENT_DIRS` after the chezmoi agents dir. Setting
`EVAL_AGENT_DIRS=~/server/.claude/agents` loads the 5 reviewer agents with their frontmatter model
pins intact. **No name collisions** with chezmoi's `implementer`/`planner`/`migration-reviewer`
(verified). The engine passes each agent's pinned model inside the `--agents` JSON and forces
`--tools ""` — identical to how chezmoi's own reviewer (`migration-reviewer`) is graded.

### Engine extension 3 — skill-as-pseudo-agent (loader fallback)

To grade `homelab-review` through the same engine, load its live `SKILL.md` as a pseudo-agent —
**no copy, no drift.** Two ~2-line changes in `load-agent.mjs`:

- In `loadAgentFromRepo`, after trying `<dir>/<name>.md`, fall back to `<dir>/<name>/SKILL.md`.
- Include `~/server/.claude/skills` in the agent search path (appended to `EVAL_AGENT_DIRS`:
  `EVAL_AGENT_DIRS=~/server/.claude/agents:~/server/.claude/skills`).

Then a case with `"agent": "homelab-review"` resolves to
`~/server/.claude/skills/homelab-review/SKILL.md`. `parseAgent` already tolerates the skill's
frontmatter: its `name:`/`description:` lines parse normally, and `allowed-tools:` is silently
ignored (the hyphen fails the loader's `^([A-Za-z_]+):` key regex) — harmless, since the engine
forces `--tools ""` regardless.

### Data flow (unchanged from chezmoi)

Per run: `invokeAgent` (`claude -p <input> --agents <json> --agent <name> --output-format json
--tools "" --max-budget-usd <cap> --setting-sources project --strict-mcp-config`, `--bare` when
`ANTHROPIC_API_KEY` is set) → infra-health classify → assertion gate (`must_match` /
`must_not_match`, case-insensitive) → opus judge against the case `rubric`. A run passes iff
infra-healthy AND gate passes AND judge returns `pass`. Case verdict from `threshold` (`"all"` or
`"rate>=X/Y"`); too few healthy runs → `INCONCLUSIVE`. Reused verbatim.

## Case taxonomy — reviewer agents (embedded-snippet, hermetic)

Each case's `input` is fully self-contained: the config snippet under review, prefixed with a
`# ansible/roles/containers/<svc>/templates/<file>.j2` header (so the agent can cite `file:line`),
plus any cross-file context the judgment needs stated inline (the chezmoi `migration-reviewer`
cases do the same — e.g. "the fraud-service still runs `SELECT legacy_token`"). This tests the
agent's **judgment and output discipline**, not its file-navigation.

Two archetypes per agent, drawn from the operator's own documented gotchas and the "don't-re-flag"
decision ledger (so the cases encode *real* regressions and *real* accepted trade-offs, not
synthetic ones):

### A. catch-defect — a planted regression the agent must catch

Embed a config with a domain-relevant defect; `must_match` the defect keyword, rubric requires
flagging it at the right severity with a correct fix. Candidate defects by agent:

- **security-review** — hardcoded/world-readable secret in a template, or a service missing
  `no-new-privileges` / running as root where the class doesn't need it.
- **homelab-container-reviewer** — a stateful service on a **named volume** (escapes Kopia's
  `containers/data` bind-mount scope), or a `:latest` tag on a critical/stateful-tier service.
- **homelab-network-diagnostician** — a hand-rolled Traefik router missing `rate-limit@file`
  (documented rule: new routers must add it), or a service on the wrong Docker network.
- **homelab-backup-observability-reviewer** — a new stateful service with no Kopia coverage / no
  healthcheck / no Kuma monitor.
- **homelab-cicd-reviewer** — a config bind-mount task not registered with
  `common_config_changed` (documented recreate gotcha), or a direct-`urllib` Discord POST missing
  the `User-Agent` header (documented 403/Cloudflare-1010 silent failure).

### B. no-overflag — an accepted trade-off the agent must NOT flag

This targets the fleet's documented **#1 failure mode**: cold agents re-flagging settled
decisions. Feed an accepted trade-off from the don't-re-flag ledger with its justification. The
gate here must be **item-specific**, not a bare severity word — reviewers emit `High`/`Medium`/`Low`
*section headers* even when empty, so `must_not_match: ["High"]` would false-fail. Instead assert on
the accepted item's own token co-occurring with alarm language (e.g. a `must_not_match` regex like
`(kopia|canary).*(vulnerab|remediate|rotate)`), and let the **judge rubric carry the real weight**:
PASS only if the agent does not flag the accepted item (or explicitly notes it verified-clean).
Candidates by agent:

- **security-review** — Kopia unauthenticated-on-purpose (basic-auth bug, intentional); the
  AWS-credentials **canary token** in git history (a tripwire — must not be "rotated").
- **homelab-container-reviewer** — a service in the watchtower opt-out (pinned/Renovate) tier;
  `read_only`-incompatible services (couchdb) correctly left writable.
- **homelab-cicd-reviewer** — `grafana`/`loki`/`prometheus` on `:latest` = the CI-enforced
  `WATCHTOWER_AUTOUPDATE` frozenset, **not** drift.
- **homelab-network-diagnostician** — Traefik v4-only port binds (IPv6 bypass is intentional; the
  DOCKER-USER Cloudflare-only-origin allowlist is v4-only by design).
- **homelab-backup-observability-reviewer** — B2 free-tier hidden-version growth is expected
  churn, not a leak; Loki has a Kuma probe (not an unmonitored gap).

### C. output-format (shared, 1 case)

One case asserting a reviewer returns the required shape: findings grouped **High / Med / Low**,
each with `file:line`, and a `[GAP]`/`[IMPROVEMENT]`/`[ADDITION]` tag (mirrors chezmoi's
`migration-reviewer/008-output-format`).

## Case taxonomy — `/homelab-review` orchestration skill

### Tier 1 — hermetic behavior-contract (default, in `run-evals.mjs`)

`SKILL.md` loaded as a pseudo-agent. `input` = a set of **pre-collected raw per-agent findings**
seeded with: a **duplicate** finding surfaced by two agents, one **settled don't-re-flag** item,
and one **known misfire** (e.g. an Authelia `trusted_proxies` proposal). Grade that the skill:
deduplicates, drops/annotates the settled item, groups High/Med/Low with `file:line` +
`[GAP]/[IMPROVEMENT]/[ADDITION]` tags, ends with a 3-bullet top-priorities shortlist, and
**STOPs without implementing** (`must_not_match` on deploy/commit/edit intent). 2–3 cases.

Fidelity boundary (explicit): tier 1 grades the **synthesis contract**, not the real parallel
`Task`-dispatch or the memory-priming step — the same boundary the chezmoi README already accepts
for `--agent` mode.

### Tier 2 — live smoke (manual, quarantined)

A separate path (`run-live.mjs`, or `mode:"live"` cases skipped unless `--live` is passed) that
runs `claude -p "review the homelab security area"` from within `~/server` with **real tools +
Agent dispatch** (no `--tools ""`, no `--agent`), then feeds the resulting `.result` through the
**same** assertion gate + judge. Confirms real parallel dispatch, dedup, and the read-only STOP
still hold end-to-end. Non-deterministic and comparatively expensive (5 parallel subagents + a
verify pass) → **never a gate**; run on demand.

## v1 case inventory (~15 cases)

| Target | catch-defect | no-overflag | other |
|---|---|---|---|
| security-review | 1 | 1 | — |
| homelab-container-reviewer | 1 | 1 | — |
| homelab-network-diagnostician | 1 | 1 | — |
| homelab-backup-observability-reviewer | 1 | 1 | — |
| homelab-cicd-reviewer | 1 | 1 | output-format ×1 |
| homelab-review (skill) | — | — | hermetic ×3, live ×1 |

Total: 10 agent + 1 output-format + 3 hermetic-skill + 1 live = **15**. Extensible by dropping a
JSON into `~/server/evals/cases/<agent>/`.

## Testing

- **New pure logic** gets unit tests alongside the existing `node --test tests/evals-*.test.mjs`
  in chezmoi: `envCaseDirs()` parsing (empty/one/many/trailing-colon), and the
  `<name>.md` → `<name>/SKILL.md` fallback resolution in `loadAgentFromRepo`.
- **No live API calls** in unit tests — the deterministic library boundary is preserved.
- A **smoke** of the full wiring: `EVAL_CASE_DIRS=~/server/evals/cases
  EVAL_AGENT_DIRS=~/server/.claude/agents:~/server/.claude/skills node <chezmoi>/evals/run-evals.mjs
  --smoke --agent security-review` (k=1) confirms external case + agent discovery end-to-end.

## Files & repos

Two repos, two commits:

- **chezmoi** (`~/.local/share/chezmoi`, source edit under `home`-root's sibling `evals/`):
  `evals/run-evals.mjs` (`EVAL_CASE_DIRS` in `loadCases`), `evals/lib/load-agent.mjs`
  (`envCaseDirs` export + `SKILL.md` fallback), optional `evals/run-live.mjs` / `--live` gate,
  `evals/tests/` unit tests. Its own signed commit.
- **~/server**: `evals/cases/<agent>/*.json` (the 15 cases), `evals/README.md` (the
  `EVAL_CASE_DIRS`/`EVAL_AGENT_DIRS` invocation, cost note, fidelity boundary). Its own signed
  commit to `master`.

## Non-goals (v1)

- HA plane (`home-assistant-engineer`, `/ha-review`, `ha-*` skills) — needs HA live-state fixtures.
- Side-effecting action skills (`deploy`, `add-secret`, `new-container`, `z2m-device-setting`) —
  mutate state/secrets; need heavy sandboxing.
- A frozen fixture-repo tier with read tools — higher fidelity than embedded snippets but
  non-deterministic and side-effect-prone; revisit only if embedded cases prove too low-fidelity.
- Any per-PR CI gate — a full `k=5` run costs real money (single-digit-to-low-tens of dollars);
  these run manually / on demand, in both repos.
