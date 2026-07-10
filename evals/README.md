# Homelab eval cases

Regression cases for the homelab-local reviewer **agents** (`.claude/agents/`) and the
`/homelab-review` orchestration **skill** (`.claude/skills/homelab-review/`). They are run by the
**chezmoi** eval engine — this repo only hosts the cases; the engine stays a single source of truth.

Design: `docs/superpowers/specs/2026-07-10-homelab-agent-skill-evals-design.md`.

## Running (needs the chezmoi checkout)

```bash
# hermetic tier — all homelab agent + skill cases
EVAL_CASE_DIRS=$HOME/server/evals/cases \
EVAL_AGENT_DIRS=$HOME/server/.claude/agents:$HOME/server/.claude/skills \
  node $HOME/.local/share/chezmoi/evals/run-evals.mjs

# filters + cheap iteration
… run-evals.mjs --agent security-review        # one agent
… run-evals.mjs --smoke                         # k=1 everywhere
… run-evals.mjs --case security-review/001-hardcoded-secret

# live smoke (manual, costly, non-deterministic — real subagent dispatch in ~/server)
EVAL_CASE_DIRS=$HOME/server/evals/cases \
  node $HOME/.local/share/chezmoi/evals/run-live.mjs
```

Set `ANTHROPIC_API_KEY` for the fully-hermetic `--bare` path (see the chezmoi eval README).

## Schema guard (offline, CI-cheap)

`uv run pytest evals` validates every case file's shape without spending a cent. The paid LLM run
above is manual — a full `k=3` sweep is single-digit dollars.

## What's tested (v1: the /homelab-review fleet)

- **catch-defect** — a planted regression (drawn from this repo's documented gotchas) the agent must flag.
- **no-overflag** — an accepted trade-off *with its justifying comment embedded in the snippet*; the
  agent must respect the in-context justification and not flag it.
- **skill** — hermetic synthesis contract (dedup / drop-settled / prioritize / STOP) + one live smoke.

Fidelity boundary: hermetic cases run with `--tools ""`, so they grade judgment + output discipline,
not file navigation or real Task-dispatch. Add a case by dropping a JSON in `cases/<agent>/`.
