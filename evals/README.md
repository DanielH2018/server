# Homelab eval cases

Regression cases for the homelab-local reviewer **agents** (`.claude/agents/`) and the
`/homelab-review` orchestration **skill** (`.claude/skills/homelab-review/`). They are run by the
**chezmoi** eval engine — this repo only hosts the cases; the engine stays a single source of truth.

The eval scaffold shipped in commit `469bcf533`.

## Running (needs the chezmoi checkout)

```bash
# hermetic tier — all homelab agent + skill cases. One --agent per target so the run
# loads ONLY homelab agents; a bare run without --agent also executes chezmoi's own
# builtin suite (and needs its work-overlay agents present).
export EVAL_CASE_DIRS=$HOME/server/evals/cases
export EVAL_AGENT_DIRS=$HOME/server/.claude/agents:$HOME/server/.claude/skills
for a in security-review homelab-network-diagnostician homelab-backup-observability-reviewer \
         homelab-cicd-reviewer homelab-container-reviewer skeptic homelab-review ha-review; do
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

## Trend tracking (cross-run)

A single `--json` report is point-in-time. `trend.py` rolls hermetic reports into
`evals/history.json` across runs, so a case that was reliably passing and drops shows up as a
**regression** instead of looking like a one-off flake:

```bash
# after a hermetic run that wrote report.json:
node $HOME/.local/share/chezmoi/evals/run-evals.mjs --agent security-review --json report.json
uv run python evals/trend.py report.json --epoch "opus-4.8/cc-2.0"  # append + flag REGRESSED / RECOVERED / FLAKY / STABLE
uv run python evals/trend.py report.json --no-write                 # report only, don't touch history
```

- Records **hermetic** runs only — `--mode subscription` prints current FAILs but never writes
  (subscription numbers are too noisy to trend, per **Auth & fidelity** above).
- **Tag the worker epoch** (`--epoch "<model>/<coding-agent-version>"`, or the `CLAUDE_EVAL_EPOCH`
  env var). The eval holds the model + agent as a fixed worker; a regression that coincides with a
  **worker change** (a model or Claude Code upgrade) is likely the worker, not the harness, so the
  summary annotates a regression that crosses an epoch boundary (`epoch e1 -> e2`). Rerun the
  hermetic sweep after any worker upgrade so the baseline is comparable — an untagged run records
  `epoch: "unknown"`.
- **Records each run's cost** under the `_runs` key: one `{ts, mode, epoch, costUsd}` entry per
  run, where `costUsd` sums every case report's `costUsd`, so it equals the engine's
  `sweep cost:` line (or the sum of those lines over a multi-agent report). A report with any case
  missing `costUsd`, such as one from before the engine priced its calls, records `null`, not 0.
- Exits non-zero if any case **regressed** (was passing last run, now failing) — usable as a gate.
- **STABLE** = met threshold in the last `--stable-n` (default 3) hermetic runs; a candidate
  skip-set. The chezmoi engine has no `--skip` flag yet, so this is advisory today — wiring
  `--skip-stable` into `run-evals.mjs` is a small follow-up if you want the paid-run cost saving
  automated.
- `history.json` is committed (a hermetic run is reproducible, so it's a real baseline). The pure
  trend logic is offline-tested in `test_trend.py`, part of `uv run pytest evals/tests`.

## Section ablation (does a CLAUDE.md section change an outcome?)

`ablate.py` measures whether a doc section changes what an agent does (#4259). A hermetic run
loads no `CLAUDE.md` at all, so the script appends the doc to each case's agent body itself and
points the engine at the copies through `EVAL_AGENT_DIRS`. The baseline arm gets the whole doc.
The `-doc` arm gets none of it. Each `-<heading>` arm gets the doc minus one `## ` section.

`run` needs `ANTHROPIC_API_KEY` in the environment, and it refuses to start without one: a
non-hermetic run is noise. The key is SOPS-encrypted and an agent never decrypts it, so
exporting it is an operator-only step.

```bash
uv run python evals/ablate.py rank                     # repo docs by instructions.log loads
uv run python evals/ablate.py run --dry-run --agent skeptic   # the plan and section sizes, free
uv run python evals/ablate.py run --agent skeptic --section "Secrets Management" --k 3
```

- **Each run stays under $10.** The operator set that cap on 2026-10-10, and `--cap-usd` can
  only lower it. Before each engine call the runner projects the next run at 1.5x the costliest
  run so far, and it stops when that projection would cross the cap. It also stops as soon as
  the measured spend crosses it, and when a run writes no report, because its spend is then
  unknown. The report names the arms it did not measure.
- **The cap is checked between runs, not inside one.** The engine writes its `--json` only when
  an invocation ends, so the runner calls it once per case per repetition at `--k 1`. One run
  can still cost more than its projection; the engine's per-call `--max-budget-usd` bounds that.
- **It refuses the weekly sweep's day**, and any day `history.json` records a sweep on, because
  both spend the same monthly credit. It re-checks before every run, so a run started late on
  Saturday stops at midnight.
- **An infra error makes a case inconclusive, not changed.** A row compares pass counts only
  when every run in both arms was healthy.
- **The report goes to `~/.cache/homelab-evals/ablation/<stamp>/`**, never to `history.json`. An
  ablation arm is not a sweep, and `trend.py` would mix it into the regression baseline.
- **A "same" verdict is bounded by the task set.** The cases grade tool-less reviewer judgment,
  so a section about shell commands or deploy steps has nothing here to change. "Same" then
  means the cases do not exercise the section, not that the section is useless. Read a verdict
  against the cases it ran on before you delete or shorten a section.
- **`instructions.log` ranks docs, not sections.** It records which doc loaded, so `rank` picks
  the doc, counting the rotated `instructions.log.1` too. `run` takes that doc's sections in
  doc order unless `--section` names them.

## Review outcomes (structured data, not prose)

`review_outcomes.jsonl` holds one JSON object per `/homelab-review` run — `date`, the confirmed
finding counts by severity (`high`/`medium`/`low`), `refuted`, `downgraded`, and the fix-skeptic
pass's `fixes_proposed`/`fixes_confirmed_safe`/`fixes_refuted`, plus the `prs` that shipped and the
`ledger` memory file name. The skill's step 7 appends a row here right after writing the dated
ledger memory — see `.claude/skills/homelab-review/SKILL.md`'s ledger-writing step for the exact
command. A count the ledger prose doesn't state as a number is `null`, never a guess.

```bash
uv run python scripts/dev/review_metrics.py         # trend table: false-positive + fix-refusal rate
uv run python scripts/dev/review_metrics.py --json   # same, as JSON
uv run pytest evals/tests/test_review_outcomes.py    # schema guard over the committed file
```

- **False-positive rate** = refuted findings / (confirmed + refuted). This is the number the
  skill's step 2 priming (the standing don't-re-flag list) exists to drive down.
- **Fix-refusal rate** = fixes the fix-skeptic pass refused (UNSAFE or LAUNDERS) / fixes proposed.
  This is the number the skill's step 7 fix-skeptic pass exists to drive down.

## What's tested (v1: the /homelab-review fleet)

- **catch-defect** — a planted regression (drawn from this repo's documented gotchas) the agent must flag.
- **no-overflag** — an accepted trade-off *with its justifying comment embedded in the snippet*; the
  agent must respect the in-context justification and not flag it.
- **skeptic** — the verifier's own judgment, which nothing measured until 2026-08-17. Every
  `homelab-review` case grades the *orchestrator*: it is handed pre-computed verdicts and checked on
  what it does with them. The `skeptic` cases grade the verdict itself — refute on cited evidence,
  refuse to accept a comment as proof, and (the one that matters) keep a finding alive when the
  decisive check cannot be run. Refuting is cheap and always defensible, which is how real findings
  die; see the `review-skeptics-drop-real-findings` memory.
- **skill** — hermetic synthesis contract (dedup / anti-merge / drop-settled / verdict-manifest /
  fixed-on-an-unmerged-branch / HA-out-of-scope / recurrence-not-discovery / collection-manifest /
  skeptic-sizing / seam-surfaces / standing-list-foldback / prioritize / STOP) + one live smoke.

Each `/homelab-review` case pins one contract paragraph the skill grew after a real misfire, so a
paragraph and its case move together: `001` dedup vs `004` anti-merge are deliberately a matched
pair — `001` alone rewards collapsing findings, and only `004` measures the counter-force.

**Four case-authoring rules.** The first three were learned on 2026-08-17. In a non-hermetic run the
agent still has tools, so anything that invites it to go *check* something ends the run with a tool
call and no report — every `must_match` then reads as missing, including trivial ones. That
signature means "no output", not "wrong answer". Those three were confirmed by fixing a failing case
and re-running it, not by reasoning:

1. **Cite paths that exist.** `003` cited `roles/containers/app` (no such role; the Pi runs four
   plus the shared `common`) and scored 1/3 twice; repointed at the real `roles/containers/wg-easy`
   it scores **3/3**. Cases citing retired roles (`k8s/kopia`) or roles that never existed
   (`k8s/longhorn` — the real files are under `roles/setup/k3s/templates/`) were corrected for the
   same reason. `test_no_case_cites_a_retired_container_role` holds every cited
   `roles/containers/<x>` to daniel-pi's `containers_list`. A fixture for a service that does not
   exist yet goes under `roles/k8s/`, marked "(a new role, not yet merged)".
   `test_no_case_cites_a_missing_role_path` holds every other cited `ansible/roles/...` path to
   the tree. A path that does not exist passes only when one citation under its role carries that
   marker and the role directory is absent; a marker on a role that exists exempts nothing.
2. **Pre-supply anything an earlier step would have gathered.** `012` asked for the step-3 dispatch
   plan, so the model correctly went to do step-2 priming first — a memory read — and never got to
   the briefs: 0/3. With the primed material inlined and priming declared done, **3/3**. Same
   principle as `006`, which hands over pre-gathered `gh pr list` output.
3. **Forbid tool calls in a sentence, not a parenthetical** — especially for a tool-using agent like
   `skeptic`, whose whole definition tells it to go check git history and open PRs. A trailing
   "(you cannot run further commands)" did not hold; the explicit "You cannot run any commands —
   no bash, no git, no gh, no kubectl, no file reads … do not attempt a tool call" does.
4. **Keep a case's premise consistent with the agent body, the only context a hermetic run has.**
   A hermetic run sees the agent's frontmatter-stripped body and the case input, and nothing else:
   no memory, no repo, no `@` include. A non-hermetic run also had the operator's ambient context,
   which hid the drift. The first hermetic sweep (2026-10-09, PR #3981, issue #3984) scored 0/3 on
   six cases whose premise the agent body contradicts or never states. The cicd case presented
   `:latest` as Watchtower-managed, but the agent body says Watchtower is retired. The network
   case's Mullvad-exit whitelist was framed as the remote-admin path, where the body names
   WireGuard. Other cases cited `roles/containers/<svc>` Docker roles for services that run on k3s,
   a `rate-limit@file` middleware that left with the Docker edge, or an automation stripped of the
   20s delay that makes it correct. Three cases now quote the live file they cite (the whitelist,
   the image pin and the automation). The other three are fixtures for a new role, so each now
   names a `roles/k8s/` path marked "(a new role, not yet merged)". This rule is a
   diagnosis, not yet a measurement: the sweep deleted its per-run output, so the next hermetic
   sweep is what confirms it. Of the sweep's two 1/3 cases, `008` was missing the `findings.py
   list` output its skill's priming step asks for, and now carries it. `006` cited
   `roles/k8s/traefik/templates/middleware.yaml.j2`, which does not exist, for an orphaned
   compression middleware; compression is attached entrypoint-wide in `dynamic.yaml.j2`, so the
   finding was false against any live file. #4020 replaced it with an open, true finding on a
   live file: bazarr hand-writes the NetworkPolicy that prowlarr, which its header calls the same
   shape, renders from the shared macro (#3712). It was picked because the claim checks against
   the file as written, and because rendering from the macro edits the file rather than
   deleting it. The same
   change rebuilt eight cases that passed the sweep but cited retired `roles/containers/` roles,
   and one of them, `homelab-review/002`, also carried a settled "Pi images are unpinned"
   decision that the Pi's digest pins had made false. #4045 widened the check from Compose role
   names to every cited `ansible/roles/` path and rebuilt the six cases citing five paths that no
   longer existed. Three were path-only repoints in synthesis cases, where the path is a label: the
   Authelia session secret, the homepage widget file, and `grafana-data` in `k8s/observability`.
   The two `ha-review` cases now name `files/automations/lighting.yaml` and the live actuator
   `light.bedroom_lights`, with their planted automation marked a proposed addition.
   `skeptic/002` plants the jsonpath bug the bash reaper really shipped, which the Python
   rewrite in `scripts/backup/` fixed, so it became a new-role fixture. A passing case edited this
   way is unverified until the next hermetic sweep.

**Grade judgment in the rubric, not the regex.** `skeptic/003` first asserted
`must_not_match: REFUTED`, which fires on "this is *not* refuted" — the assertion rejected correct
answers. Verdict words are unusable as negative regexes. Keep `must_match`/`must_not_match` to cheap
structural pre-filters (a service name, a drop-the-finding phrase) and let the rubric decide whether
the verdict was right. Same case also over-specified the answer: it demanded UNCERTAIN when
CONFIRMED-on-cited-corroboration was equally correct. What the contract actually requires is that
the *finding survives*, so that is what the rubric asks.

One coverage caveat: `006` hands the model pre-gathered `gh pr list` / `gh pr diff` output, so it
grades the *interpretation* half of the open-PR rule. The half that fails in practice — deciding to
look at open branches at all — needs tools, so only the live case exercises it.

Fidelity boundary: hermetic cases run with `--tools ""`, so they grade judgment + output discipline,
not file navigation or real Task-dispatch. The engine passes the agent body as a literal system
prompt and expands no `@` include, so a fact an agent needs in a hermetic run belongs in its body.
`security-review.md` repeats `DETAILED_GUIDE.md`'s severity scale and secrets rule for this reason;
the per-category detail stays in the guide, so that agent's eval fidelity is still below a live
run's. Add a case by dropping a JSON in `cases/<agent>/`.
