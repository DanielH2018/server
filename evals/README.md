# Homelab eval cases

Regression cases for the homelab-local reviewer **agents** (`.claude/agents/`) and the
`/homelab-review` orchestration **skill** (`.claude/skills/homelab-review/`). They are run by the
**chezmoi** eval engine — this repo only hosts the cases; the engine stays a single source of truth.

Design: `docs/superpowers/specs/2026-07-10-homelab-agent-skill-evals-design.md`.

## Running (needs the chezmoi checkout)

```bash
# hermetic tier — all homelab agent + skill cases. One --agent per target so the run
# loads ONLY homelab agents; a bare run without --agent also executes chezmoi's own
# builtin suite (and needs its work-overlay agents present).
export EVAL_CASE_DIRS=$HOME/server/evals/cases
export EVAL_AGENT_DIRS=$HOME/server/.claude/agents:$HOME/server/.claude/skills
for a in security-review homelab-network-diagnostician homelab-backup-observability-reviewer \
         homelab-cicd-reviewer homelab-container-reviewer homelab-review; do
  node $HOME/.local/share/chezmoi/evals/run-evals.mjs --agent "$a"
done

# filters + cheap iteration
… run-evals.mjs --agent security-review        # one agent
… run-evals.mjs --smoke                         # k=1 everywhere (overrides case k)
… run-evals.mjs --k 1                           # force k=N for every case (smoke > --k > case k)
… run-evals.mjs --case security-review/001-hardcoded-secret
… run-evals.mjs --agent security-review --json report.json   # + machine-readable report (see caveat below)

# live smoke (manual, costly, non-deterministic — real subagent dispatch in ~/server)
EVAL_CASE_DIRS=$HOME/server/evals/cases \
  node $HOME/.local/share/chezmoi/evals/run-live.mjs
```

## Auth & fidelity (read before trusting pass rates)

Two run modes, and they are NOT equally trustworthy:

- **`ANTHROPIC_API_KEY` set → hermetic.** The engine adds `--bare`, so no ambient global
  `CLAUDE.md`, hooks, skills, or memory load and `--tools ""` cleanly strips tools. Results are
  reproducible — the mode the harness (and CI) is designed for. This is pay-per-token **API**
  billing (single-digit dollars for a full `k=3` sweep), *separate from any Claude subscription*.
- **Subscription / interactive OAuth (no API key) → NON-hermetic; results are noisy.** `--bare` is
  incompatible with OAuth (it prints "Not logged in"), so the run takes the fallback path: your
  global `CLAUDE.md`, the superpowers skill framework, and your homelab memory leak into the eval
  agent, and `--tools ""` is not reliably honored. Observed 2026-07-10: a reviewer agent read the
  live repo and cited `traefik.yml.j2` internals absent from the case input; another opened with a
  live `grep`; a sound catch-defect case scored 1/3 then 1/1 run-to-run. Good enough for a rough
  "does the wiring work / does the case catch its defect" sanity check — **not** for regression
  numbers. Costs subscription usage, not dollars.

`--json report.json` dumps each case's aggregate + per-run detail. Only persist/commit it as a
baseline from a **hermetic** (API-key) run — a subscription run's numbers are noisy per the above,
so a committed subscription baseline would trend noise, not regressions.

## Schema guard (offline, free)

`uv run pytest evals` validates every case file's shape without an API call or a subscription
call. The LLM run above is manual — see **Auth & fidelity** for its cost and its trust caveats.

## What's tested (v1: the /homelab-review fleet)

- **catch-defect** — a planted regression (drawn from this repo's documented gotchas) the agent must flag.
- **no-overflag** — an accepted trade-off *with its justifying comment embedded in the snippet*; the
  agent must respect the in-context justification and not flag it.
- **skill** — hermetic synthesis contract (dedup / drop-settled / prioritize / STOP) + one live smoke.

Fidelity boundary: hermetic cases run with `--tools ""`, so they grade judgment + output discipline,
not file navigation or real Task-dispatch. security-review's severity standards live in an
`@`-included `DETAILED_GUIDE.md` that the engine does not expand — the agent body is passed as a
literal system prompt and `--tools ""` blocks reading it — so its eval fidelity is reduced versus a
live run. Add a case by dropping a JSON in `cases/<agent>/`.
