# Homelab-local agent/skill evals — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grade the `/homelab-review` fleet (5 reviewer agents + the orchestration skill) with the existing chezmoi subagent-eval engine, adding only backward-compatible glue.

**Architecture:** One engine (chezmoi `evals/`) stays source of truth. A new `EVAL_CASE_DIRS` env var (mirroring the existing `EVAL_AGENT_DIRS`) lets it discover homelab cases in `~/server/evals/cases/`; a `SKILL.md` loader fallback lets the `homelab-review` skill be graded as a pseudo-agent. Reviewer-agent cases are embedded-snippet + `--tools ""` (hermetic); the skill also gets a manual live-dispatch smoke case.

**Tech Stack:** Node ≥18 (`node --test`, ES modules `.mjs`) for the chezmoi engine; Python + `uv`/pytest for the homelab-side case-schema validator; the `claude` CLI (v2.1.204 contract) as the agent runner.

## Global Constraints

- **Two repos, two commits.** Engine code lands in **chezmoi** (`~/.local/share/chezmoi`, dir `evals/`, tests in `tests/`); cases + validator + README land in **~/server** (dir `evals/`), committed to `master`. Never mix the two in one commit.
- **Backward compatibility is mandatory:** with `EVAL_CASE_DIRS` unset, the chezmoi engine must behave exactly as before (CI/hermetic default path unchanged). New env vars default to empty.
- **Signed commits only.** Never pass `--no-verify` or any flag bypassing signing. In ~/server, `git add <explicit paths>` only (a second Claude session may hold unstaged work) and pass multi-line messages via `git commit -F - <<'EOF'` (backticks in `-m` get zsh-substituted).
- **Case schema (exact fields), matching chezmoi's existing cases:** `id` (string, `"<agent>/<slug>"`), `agent` (string), `input` (string), `assert` (`{ "must_match": [regex…], "must_not_match": [regex…] }`), `rubric` (string), `k` (int), `threshold` (`"all"` or `"rate>=X/Y"`). Live-tier cases add `"mode": "live"`.
- **`--tools ""` fidelity boundary:** hermetic cases must be fully self-contained in `input` — the agent has no filesystem. Head each embedded config with a `# ansible/roles/.../templates/…j2` path comment so the agent can cite `file:line`.
- **Illustrative secrets stay low-entropy.** Fake credentials in case snippets (e.g. `changeme`) must be low-entropy placeholders so gitleaks doesn't block the commit — the reviewer flags the hardcoded *credential key in a git-tracked template*, not the value's strength. Do not "improve" them into realistic high-entropy secrets.
- **no-overflag principle:** a hermetic no-overflag case embeds the *justifying comment/context inside the snippet* and tests that the agent respects in-context justification rather than pattern-flagging. Memory-dependent settled decisions (that an isolated agent cannot know) are tested at the **skill tier**, where prime-from-memory is part of the graded contract — not at the agent tier.
- Default consistency knobs for hermetic cases: `k: 3`, `threshold: "rate>=2/3"` (use `"all"` only for the deterministic output-format case). Live case: `k: 1`, `threshold: "all"`.

---

## File Structure

**chezmoi (`~/.local/share/chezmoi`):**
- `evals/lib/load-cases.mjs` — **new.** `envCaseDirs()` + `loadCases(opts, caseDirs)` (multi-root discovery, `--agent`/`--case` filters, skips `mode:"live"`).
- `evals/lib/load-agent.mjs` — **modify.** `loadAgentFromRepo` gains a `<name>/SKILL.md` fallback.
- `evals/lib/live-args.mjs` — **new.** `buildLiveArgs()` — pure builder for the live-runner CLI args.
- `evals/run-evals.mjs` — **modify.** Import `loadCases`/`envCaseDirs`; drop the inline `loadCases`.
- `evals/run-live.mjs` — **new.** Manual live-dispatch runner for `mode:"live"` cases.
- `tests/evals-load-cases.test.mjs`, `tests/evals-live-args.test.mjs` — **new.** Unit tests.
- `tests/evals-load-agent.test.mjs` — **modify.** Add the SKILL.md-fallback test.

**~/server:**
- `evals/README.md` — **new.** Invocation, cost note, fidelity boundary.
- `evals/test_eval_cases.py` — **new.** Case-schema validator + tests (pytest).
- `evals/cases/<agent>/*.json` — **new.** The 15 cases.
- `pyproject.toml` — **modify.** Add `"evals"` to `testpaths`.

---

## Task 1: `EVAL_CASE_DIRS` case discovery (chezmoi)

**Files:**
- Create: `~/.local/share/chezmoi/evals/lib/load-cases.mjs`
- Modify: `~/.local/share/chezmoi/evals/run-evals.mjs` (imports at top; `loadCases` call in `main`)
- Test: `~/.local/share/chezmoi/tests/evals-load-cases.test.mjs`

**Interfaces:**
- Produces: `envCaseDirs(): string[]` — parses colon-separated `EVAL_CASE_DIRS`, empty by default. `loadCases(opts, caseDirs): object[]` — `opts` has optional `.agent`/`.case`; iterates each existing dir in `caseDirs`, reads `<dir>/<agentName>/*.json`, applies filters, skips any case with `mode === 'live'`.

- [ ] **Step 1: Write the failing test**

```javascript
// ~/.local/share/chezmoi/tests/evals-load-cases.test.mjs
import { test } from 'node:test';
import assert from 'node:assert';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { envCaseDirs, loadCases } from '../evals/lib/load-cases.mjs';

function fixtureRoot() {
  const root = mkdtempSync(join(tmpdir(), 'evalcases-'));
  const dir = join(root, 'security-review');
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, '001.json'), JSON.stringify({ id: 'security-review/001', agent: 'security-review', input: 'x' }));
  writeFileSync(join(dir, '002-live.json'), JSON.stringify({ id: 'security-review/002', agent: 'security-review', input: 'y', mode: 'live' }));
  return root;
}

test('envCaseDirs parses colon-separated, empty by default', () => {
  delete process.env.EVAL_CASE_DIRS;
  assert.deepStrictEqual(envCaseDirs(), []);
  process.env.EVAL_CASE_DIRS = '/a:/b:';
  assert.deepStrictEqual(envCaseDirs(), ['/a', '/b']);
  delete process.env.EVAL_CASE_DIRS;
});

test('loadCases discovers cases, skips live, honors filters, ignores missing dirs', () => {
  const root = fixtureRoot();
  try {
    const all = loadCases({}, [root, '/does/not/exist']);
    assert.strictEqual(all.length, 1);                       // live case skipped
    assert.strictEqual(all[0].id, 'security-review/001');
    assert.strictEqual(loadCases({ agent: 'nope' }, [root]).length, 0);
    assert.strictEqual(loadCases({ case: 'security-review/001' }, [root]).length, 1);
    assert.strictEqual(loadCases({ case: 'security-review/002' }, [root]).length, 0); // live filtered before match
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.local/share/chezmoi && node --test tests/evals-load-cases.test.mjs`
Expected: FAIL — `Cannot find module '../evals/lib/load-cases.mjs'`.

- [ ] **Step 3: Write the module**

```javascript
// ~/.local/share/chezmoi/evals/lib/load-cases.mjs
import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

// Extra case roots, colon-separated, from EVAL_CASE_DIRS. Empty by default so the
// hermetic/CI path is unchanged; set it to grade cases living outside this repo
// (e.g. ~/server/evals/cases). Mirrors envAgentDirs() in load-agent.mjs.
export function envCaseDirs() {
  const raw = process.env.EVAL_CASE_DIRS;
  return raw ? raw.split(':').filter(Boolean) : [];
}

export function loadCases(opts, caseDirs) {
  const cases = [];
  for (const root of caseDirs) {
    if (!existsSync(root)) continue;
    for (const agent of readdirSync(root, { withFileTypes: true }).filter(d => d.isDirectory())) {
      if (opts.agent && agent.name !== opts.agent) continue;
      const dir = join(root, agent.name);
      for (const f of readdirSync(dir).filter(f => f.endsWith('.json'))) {
        const c = JSON.parse(readFileSync(join(dir, f), 'utf8'));
        if (opts.case && c.id !== opts.case) continue;
        if (c.mode === 'live') continue;   // live cases run via run-live.mjs, not the hermetic runner
        cases.push(c);
      }
    }
  }
  return cases;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.local/share/chezmoi && node --test tests/evals-load-cases.test.mjs`
Expected: PASS (2 tests).

- [ ] **Step 5: Wire `run-evals.mjs` to the new module**

In `~/.local/share/chezmoi/evals/run-evals.mjs`: change the top imports — remove `readdirSync` and `readFileSync` from the `node:fs` import (keep `writeFileSync`), and add the new import:

```javascript
import { writeFileSync } from 'node:fs';
```
```javascript
import { loadCases, envCaseDirs } from './lib/load-cases.mjs';
```

Delete the inline `function loadCases(opts) { … }` block entirely. In `main()`, replace `const cases = loadCases(opts);` with:

```javascript
  const cases = loadCases(opts, [CASES_DIR, ...envCaseDirs()]);
```

(`CASES_DIR = join(HERE, 'cases')` stays.)

- [ ] **Step 6: Verify the built-in path still works and full suite is green**

Run: `cd ~/.local/share/chezmoi && node --test tests/evals-*.test.mjs`
Expected: PASS (all eval tests, including the new one).
Run: `cd ~/.local/share/chezmoi && node -e "import('./evals/lib/load-cases.mjs').then(m=>console.log(m.loadCases({}, ['./evals/cases']).length))"`
Expected: prints the number of built-in chezmoi cases (non-zero, e.g. `31`).

- [ ] **Step 7: Commit**

```bash
cd ~/.local/share/chezmoi && git add evals/lib/load-cases.mjs evals/run-evals.mjs tests/evals-load-cases.test.mjs
git commit -F - <<'EOF'
evals: discover cases from EVAL_CASE_DIRS

Extract loadCases into lib/load-cases.mjs and let it iterate extra case
roots from a colon-separated EVAL_CASE_DIRS (empty by default, so the CI
path is unchanged). Skips mode:"live" cases — those run via run-live.mjs.
Lets the homelab repo host its own cases at ~/server/evals/cases without
the engine forking.
EOF
```

---

## Task 2: `SKILL.md` pseudo-agent fallback (chezmoi)

**Files:**
- Modify: `~/.local/share/chezmoi/evals/lib/load-agent.mjs` (`loadAgentFromRepo`, ~lines 49-59)
- Test: `~/.local/share/chezmoi/tests/evals-load-agent.test.mjs` (append)

**Interfaces:**
- Consumes: `parseAgent`, `agentSearchDirs` (existing).
- Produces: `loadAgentFromRepo(name, repoRoot, extraDirs)` — unchanged signature; now resolves `<dir>/<name>.md` **or** `<dir>/<name>/SKILL.md` (flat file wins if both exist).

- [ ] **Step 1: Write the failing test (append to the existing file)**

```javascript
// append to ~/.local/share/chezmoi/tests/evals-load-agent.test.mjs
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join as pjoin } from 'node:path';

test('loadAgentFromRepo resolves a skill via <name>/SKILL.md and tolerates allowed-tools frontmatter', () => {
  const extra = mkdtempSync(join(tmpdir(), 'skilldir-'));
  const sdir = pjoin(extra, 'homelab-review');
  mkdirSync(sdir, { recursive: true });
  writeFileSync(pjoin(sdir, 'SKILL.md'),
    '---\nname: homelab-review\ndescription: Multi-agent review.\nallowed-tools: Read, Grep, Glob, Bash, Agent\n---\n\nRun a review and STOP.');
  const fakeRepo = mkdtempSync(join(tmpdir(), 'repo-'));  // no chezmoi agent shadows the name
  try {
    const a = loadAgentFromRepo('homelab-review', fakeRepo, [extra]);
    assert.strictEqual(a.name, 'homelab-review');
    assert.strictEqual(a.description, 'Multi-agent review.');
    assert.match(a.systemPrompt, /Run a review and STOP\./);
  } finally {
    rmSync(extra, { recursive: true, force: true });
    rmSync(fakeRepo, { recursive: true, force: true });
  }
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.local/share/chezmoi && node --test tests/evals-load-agent.test.mjs`
Expected: FAIL — the loader throws `agent "homelab-review" not found` (no `<name>/SKILL.md` fallback yet).

- [ ] **Step 3: Add the fallback**

In `~/.local/share/chezmoi/evals/lib/load-agent.mjs`, replace the loop body of `loadAgentFromRepo`:

```javascript
export function loadAgentFromRepo(name, repoRoot, extraDirs = envAgentDirs()) {
  const dirs = agentSearchDirs(repoRoot, extraDirs);
  for (const dir of dirs) {
    const flat = join(dir, `${name}.md`);
    if (existsSync(flat)) return parseAgent(readFileSync(flat, 'utf8'));
    const skill = join(dir, name, 'SKILL.md');   // skill dir: <name>/SKILL.md
    if (existsSync(skill)) return parseAgent(readFileSync(skill, 'utf8'));
  }
  throw new Error(
    `agent "${name}" not found in: ${dirs.join(', ')}. ` +
    `If it lives in the work overlay, set EVAL_AGENT_DIRS (e.g. ~/work-laptop-config/.claude/agents).`
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.local/share/chezmoi && node --test tests/evals-load-agent.test.mjs`
Expected: PASS (existing tests + the new one).

- [ ] **Step 5: Commit**

```bash
cd ~/.local/share/chezmoi && git add evals/lib/load-agent.mjs tests/evals-load-agent.test.mjs
git commit -F - <<'EOF'
evals: load a skill's SKILL.md as a pseudo-agent

loadAgentFromRepo now falls back to <dir>/<name>/SKILL.md when no flat
<name>.md exists, so an orchestration skill (e.g. homelab-review) can be
graded through the same engine. parseAgent already ignores the skill's
allowed-tools: frontmatter (the hyphen fails its key regex); --tools ""
governs regardless.
EOF
```

---

## Task 3: Live-dispatch runner (chezmoi)

**Files:**
- Create: `~/.local/share/chezmoi/evals/lib/live-args.mjs`
- Create: `~/.local/share/chezmoi/evals/run-live.mjs`
- Test: `~/.local/share/chezmoi/tests/evals-live-args.test.mjs`

**Interfaces:**
- Consumes: `envCaseDirs` (Task 1), `classifyRun`, `checkAssertions`, `judge`, `gradeFromParts` (existing).
- Produces: `buildLiveArgs({ input, maxBudgetUsd?, permissionMode? }): string[]` — CLI args for a real (tools-enabled, non-`--agent`) `claude -p` run.

- [ ] **Step 1: Write the failing test**

```javascript
// ~/.local/share/chezmoi/tests/evals-live-args.test.mjs
import { test } from 'node:test';
import assert from 'node:assert';
import { buildLiveArgs } from '../evals/lib/live-args.mjs';

test('buildLiveArgs enables real tools/dispatch (no --agent, no --tools "")', () => {
  const a = buildLiveArgs({ input: 'Review the homelab security area.' });
  assert.ok(a.includes('-p') && a.includes('Review the homelab security area.'));
  assert.ok(a.includes('--output-format') && a.includes('json'));
  assert.ok(!a.includes('--agent'));
  assert.ok(!(a.includes('--tools') && a[a.indexOf('--tools') + 1] === ''));
  const b = buildLiveArgs({ input: 'x', maxBudgetUsd: 3, permissionMode: 'plan' });
  assert.strictEqual(b[b.indexOf('--max-budget-usd') + 1], '3');
  assert.strictEqual(b[b.indexOf('--permission-mode') + 1], 'plan');
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.local/share/chezmoi && node --test tests/evals-live-args.test.mjs`
Expected: FAIL — `Cannot find module '../evals/lib/live-args.mjs'`.

- [ ] **Step 3: Write `live-args.mjs`**

```javascript
// ~/.local/share/chezmoi/evals/lib/live-args.mjs
// Args for a LIVE run: the skill + real subagent dispatch must engage, so we do
// NOT pass --agent or --tools "". permissionMode defaults to acceptEdits so read
// tools + Task dispatch run headless; the homelab's block-protected-edits hook is
// the write safety-net (the review contract is read-only). Override with
// EVAL_LIVE_PERMISSION_MODE (e.g. 'plan' for a stricter, edit-free run).
export function buildLiveArgs({
  input,
  maxBudgetUsd = 2.0,
  permissionMode = process.env.EVAL_LIVE_PERMISSION_MODE || 'acceptEdits',
}) {
  return [
    '-p', input,
    '--output-format', 'json',
    '--max-budget-usd', String(maxBudgetUsd),
    '--permission-mode', permissionMode,
  ];
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.local/share/chezmoi && node --test tests/evals-live-args.test.mjs`
Expected: PASS.

- [ ] **Step 5: Write the runner `run-live.mjs`**

```javascript
#!/usr/bin/env node
// Manual, quarantined live tier. Runs each mode:"live" case from EVAL_CASE_DIRS as
// a REAL `claude -p` invocation (in EVAL_LIVE_CWD, default ~/server) so the skill and
// its parallel subagent dispatch actually run, then grades the final result with the
// same assertion gate + judge as the hermetic runner. Non-deterministic + costly:
// never a CI gate. Usage:
//   EVAL_CASE_DIRS=$HOME/server/evals/cases node evals/run-live.mjs
import { execFile } from 'node:child_process';
import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { envCaseDirs } from './lib/load-cases.mjs';
import { buildLiveArgs } from './lib/live-args.mjs';
import { classifyRun } from './lib/classify.mjs';
import { checkAssertions } from './lib/assertions.mjs';
import { judge } from './lib/judge.mjs';
import { gradeFromParts } from './lib/grade.mjs';

const CWD = process.env.EVAL_LIVE_CWD || join(process.env.HOME, 'server');

function loadLiveCases() {
  const cases = [];
  for (const root of envCaseDirs()) {
    if (!existsSync(root)) continue;
    for (const agent of readdirSync(root, { withFileTypes: true }).filter(d => d.isDirectory())) {
      const dir = join(root, agent.name);
      for (const f of readdirSync(dir).filter(f => f.endsWith('.json'))) {
        const c = JSON.parse(readFileSync(join(dir, f), 'utf8'));
        if (c.mode === 'live') cases.push(c);
      }
    }
  }
  return cases;
}

function runClaude(args) {
  return new Promise((resolve) => {
    execFile('claude', args, { cwd: CWD, timeout: 600000, maxBuffer: 64 * 1024 * 1024, encoding: 'utf8' },
      (err, stdout) => {
        if (err && !stdout) { resolve({ is_error: true, subtype: 'exec_error', result: String(err.message || err) }); return; }
        try { resolve(JSON.parse(stdout)); }
        catch { resolve({ is_error: true, subtype: 'parse_error', result: (stdout || '').slice(0, 500) }); }
      });
  });
}

async function main() {
  const cases = loadLiveCases();
  if (!cases.length) { console.error('no live cases found (set EVAL_CASE_DIRS)'); process.exit(2); }
  let failed = 0;
  for (const c of cases) {
    const raw = await runClaude(buildLiveArgs({ input: c.input }));
    const inv = classifyRun(raw);
    if (inv.status !== 'ok') { console.log(`INFRA  ${c.id}: ${inv.reason}`); failed++; continue; }
    const a = checkAssertions(inv.text, c.assert);
    if (!a.pass) { console.log(`FAIL   ${c.id}: ${a.failures.join('; ')}`); failed++; continue; }
    const j = await judge({ rubric: c.rubric, output: inv.text });
    const g = gradeFromParts({ invocation: inv, assertion: a, judgeResult: j });
    console.log(`${g.pass ? 'PASS ' : 'FAIL '}  ${c.id}: ${g.judgeReason || g.reason || ''}`);
    if (!g.pass) failed++;
  }
  process.exit(failed ? 1 : 0);
}

main();
```

- [ ] **Step 6: Verify the runner loads without a live run**

Run: `cd ~/.local/share/chezmoi && node evals/run-live.mjs`
Expected: exits `2` with `no live cases found (set EVAL_CASE_DIRS)` (no `EVAL_CASE_DIRS` set yet — the live case arrives in Task 11). This confirms the module graph imports cleanly.

- [ ] **Step 7: Commit**

```bash
cd ~/.local/share/chezmoi && git add evals/lib/live-args.mjs evals/run-live.mjs tests/evals-live-args.test.mjs
git commit -F - <<'EOF'
evals: add a manual live-dispatch runner for mode:"live" cases

run-live.mjs runs a case's input as a real `claude -p` invocation in
~/server (tools + subagent dispatch enabled, no --agent) and grades the
result with the same assertion+judge path. Quarantined from the hermetic
runner: non-deterministic and costly, never a CI gate. buildLiveArgs is
unit-tested; the permission mode is overridable via EVAL_LIVE_PERMISSION_MODE.
EOF
```

---

## Task 4: Homelab evals scaffold — README, validator, testpaths (~/server)

**Files:**
- Create: `~/server/evals/README.md`
- Create: `~/server/evals/test_eval_cases.py`
- Modify: `~/server/pyproject.toml` (`testpaths` list)

**Interfaces:**
- Produces: `validate_case(obj: dict) -> list[str]` — returns a list of problem strings (empty = valid). Used by `test_all_case_files_valid` and reusable by later case tasks as the schema gate.

- [ ] **Step 1: Write the failing test + validator scaffold**

```python
# ~/server/evals/test_eval_cases.py
"""Schema validation for the homelab eval cases consumed by the chezmoi engine.

These are cheap, offline guards (no `claude` calls): they only assert each case
file is well-formed so a typo can't make a case silently not run. The paid LLM
eval itself is run manually — see evals/README.md.
"""
import json
import re
from pathlib import Path

CASES_DIR = Path(__file__).parent / "cases"
REQUIRED = ("id", "agent", "input", "assert", "rubric", "k", "threshold")
_THRESHOLD_RE = re.compile(r"^(all|rate>=\d+/\d+)$")


def validate_case(obj: dict) -> list[str]:
    problems: list[str] = []
    for field in REQUIRED:
        if field not in obj:
            problems.append(f"missing field: {field}")
    if "assert" in obj:
        a = obj["assert"]
        if not isinstance(a, dict) or "must_match" not in a or "must_not_match" not in a:
            problems.append("assert must be an object with must_match and must_not_match arrays")
        else:
            for key in ("must_match", "must_not_match"):
                for pat in a.get(key, []):
                    try:
                        re.compile(pat)
                    except re.error as e:
                        problems.append(f"{key} has invalid regex {pat!r}: {e}")
    if "threshold" in obj and not _THRESHOLD_RE.match(str(obj["threshold"])):
        problems.append(f"bad threshold: {obj['threshold']!r} (want 'all' or 'rate>=X/Y')")
    if "id" in obj and "agent" in obj and not str(obj["id"]).startswith(f"{obj['agent']}/"):
        problems.append(f"id {obj['id']!r} must start with '{obj['agent']}/'")
    if obj.get("mode") not in (None, "live"):
        problems.append(f"unknown mode: {obj['mode']!r}")
    return problems


def _all_case_files() -> list[Path]:
    return sorted(CASES_DIR.rglob("*.json"))


def test_validate_case_accepts_a_good_case():
    good = {
        "id": "security-review/001", "agent": "security-review", "input": "x",
        "assert": {"must_match": ["High"], "must_not_match": []},
        "rubric": "r", "k": 3, "threshold": "rate>=2/3",
    }
    assert validate_case(good) == []


def test_validate_case_flags_missing_field_and_bad_threshold_and_regex():
    bad = {
        "id": "x/1", "agent": "x", "input": "i",
        "assert": {"must_match": ["("], "must_not_match": []},
        "rubric": "r", "k": 3, "threshold": "most",
    }
    problems = validate_case(bad)
    assert any("bad threshold" in p for p in problems)
    assert any("invalid regex" in p for p in problems)


def test_all_case_files_valid():
    files = _all_case_files()
    for f in files:
        obj = json.loads(f.read_text())
        problems = validate_case(obj)
        assert not problems, f"{f}: {problems}"
```

- [ ] **Step 2: Wire `testpaths` and run to verify the pure tests pass (collection is empty)**

Add `"evals",` to the `testpaths` list in `~/server/pyproject.toml` (after the existing last entry, before the closing `]`):

```toml
  "ansible/roles/containers/home-assistant/tests", # HA Jinja macro logic tests
  "evals",                                         # homelab eval-case schema validation
]
```

Run: `cd ~/server && uv run pytest evals -q`
Expected: PASS — 3 tests (`test_all_case_files_valid` passes vacuously; no case files yet).

- [ ] **Step 3: Write the README**

```markdown
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
```

- [ ] **Step 4: Commit**

```bash
cd ~/server && git add evals/README.md evals/test_eval_cases.py pyproject.toml
git commit -F - <<'EOF'
evals: scaffold homelab eval cases (README, schema validator, testpaths)

Cases for the /homelab-review fleet live here and are run by the chezmoi
engine via EVAL_CASE_DIRS. Adds an offline pytest schema validator so a
malformed case can't silently fail to run, wired into testpaths; the paid
LLM run stays manual (see evals/README.md).
EOF
```

---

## Task 5: security-review cases (~/server)

**Files:**
- Create: `~/server/evals/cases/security-review/001-hardcoded-secret.json`
- Create: `~/server/evals/cases/security-review/002-readonly-tmpfs-accepted.json`

**Interfaces:**
- Consumes: `validate_case` (Task 4) via `uv run pytest evals`.

- [ ] **Step 1: Write the catch-defect case**

```json
{
  "id": "security-review/001-hardcoded-secret",
  "agent": "security-review",
  "description": "A plaintext secret committed in a compose template must be flagged and pointed at SOPS.",
  "input": "Review this compose template for security issues:\n```\n# ansible/roles/containers/example-app/templates/docker-compose.yml.j2\nservices:\n  example-app:\n    image: example/app:1.4.2\n    environment:\n      - TZ={{ tz }}\n      - ADMIN_PASSWORD=changeme\n      - DB_DSN=postgres://app:changeme@db:5432/app\n    restart: unless-stopped\n```\nThis file is committed to the git repo.",
  "assert": { "must_match": ["ADMIN_PASSWORD", "(SOPS|secrets\\.yml|encrypt)"], "must_not_match": [] },
  "rubric": "PASS only if the review (1) flags the plaintext ADMIN_PASSWORD and/or DB_DSN credential committed in a git-tracked template, (2) rates it High or Critical, and (3) recommends moving it to SOPS-encrypted secrets.yml referenced via a {{ variable }}.",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 2: Write the no-overflag case**

```json
{
  "id": "security-review/002-readonly-tmpfs-accepted",
  "agent": "security-review",
  "description": "The read_only+tmpfs noexec residual is a documented accepted trade-off; must not be flagged High.",
  "input": "Review this compose service's hardening:\n```\n# ansible/roles/containers/widget/templates/docker-compose.yml.j2\nservices:\n  widget:\n    image: widget/app:2.1.0\n    read_only: true\n    security_opt:\n      - no-new-privileges:true\n    cap_drop:\n      - ALL\n    tmpfs:\n      # tmpfs is writable+executable: Compose's long-form exposes only size/mode,\n      # there is NO Compose-native way to set noexec. suid is already neutered by\n      # no-new-privileges + cap_drop:[ALL]; closing the exec residual needs a daemon\n      # AppArmor change, out of compose scope. Reviewed 2026-07-05 and accepted.\n      - /tmp\n      - /run\n    restart: unless-stopped\n```",
  "assert": { "must_match": [], "must_not_match": ["(tmpfs|noexec|writable).{0,40}(vulnerab|critical|high\\b)"] },
  "rubric": "PASS only if the review does NOT raise the tmpfs/noexec-residual as a High/Critical finding. The embedded comment documents it as a consciously accepted trade-off (no Compose-native noexec; suid already neutered). Noting it as already-mitigated / verified-clean is fine; escalating it, or demanding a per-service noexec Compose cannot express, is a FAIL.",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 3: Validate**

Run: `cd ~/server && uv run pytest evals -q`
Expected: PASS (`test_all_case_files_valid` now covers 2 files).

- [ ] **Step 4 (optional, paid): smoke one case**

Run:
```bash
cd ~/server && EVAL_CASE_DIRS=$HOME/server/evals/cases \
EVAL_AGENT_DIRS=$HOME/server/.claude/agents:$HOME/server/.claude/skills \
  node $HOME/.local/share/chezmoi/evals/run-evals.mjs --smoke --agent security-review
```
Expected: two lines `PASS … security-review/001…` and `… security-review/002…` (k=1). Investigate any FAIL/INCONCLUSIVE before proceeding.

- [ ] **Step 5: Commit**

```bash
cd ~/server && git add evals/cases/security-review/
git commit -F - <<'EOF'
evals: security-review cases (hardcoded-secret catch + read_only/tmpfs no-overflag)

catch: a plaintext credential in a git-tracked compose template must be
flagged High and pointed at SOPS. no-overflag: the read_only+tmpfs noexec
residual, carrying its documented "accepted 2026-07-05" comment, must not
be escalated (docker.md records it as a conscious trade-off).
EOF
```

---

## Task 6: container-reviewer cases (~/server)

**Files:**
- Create: `~/server/evals/cases/homelab-container-reviewer/001-named-volume-unbacked.json`
- Create: `~/server/evals/cases/homelab-container-reviewer/002-named-volume-accepted.json`

- [ ] **Step 1: Write the catch-defect case (irreplaceable state on a named volume, no justification)**

```json
{
  "id": "homelab-container-reviewer/001-named-volume-unbacked",
  "agent": "homelab-container-reviewer",
  "description": "Irreplaceable app state on a named volume silently escapes Kopia's bind-mount scope — must be flagged.",
  "input": "Review this new service's storage:\n```\n# ansible/roles/containers/notesdb/templates/docker-compose.yml.j2\nservices:\n  notesdb:\n    image: postgres:16.3\n    environment:\n      - POSTGRES_DB=notes\n    volumes:\n      - notesdb_data:/var/lib/postgresql/data\n    restart: unless-stopped\nvolumes:\n  notesdb_data:\n```\nContext: notesdb holds the only copy of the user's notes app database.",
  "assert": { "must_match": ["(named volume|bind mount|Kopia|backup)"], "must_not_match": [] },
  "rubric": "PASS only if the review flags that notesdb_data is a named volume holding irreplaceable data that escapes Kopia's containers/ bind-mount backup scope, and recommends a ./data bind mount under containers/ (or explicit Kopia coverage). Must NOT wave it through as an accepted named-volume exception — this data is not regenerable and carries no justifying comment.",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 2: Write the no-overflag case (documented regenerable named-volume exception)**

```json
{
  "id": "homelab-container-reviewer/002-named-volume-accepted",
  "agent": "homelab-container-reviewer",
  "description": "prometheus_data is a documented named-volume exception (regenerable TSDB, deliberately out of Kopia scope); must not be flagged.",
  "input": "Review this service's storage:\n```\n# ansible/roles/containers/prometheus/templates/docker-compose.yml.j2\nservices:\n  prometheus:\n    image: prom/prometheus:v2.53.0\n    volumes:\n      # named volume: TSDB is bulky + regenerable, deliberately OUTSIDE Kopia's\n      # containers/ scope (documented exception in .claude/rules/docker.md).\n      - prometheus_data:/prometheus\n    restart: unless-stopped\nvolumes:\n  prometheus_data:\n```",
  "assert": { "must_match": [], "must_not_match": ["prometheus_data.{0,60}(not backed up|data.?loss|bind mount|\\bgap\\b)"] },
  "rubric": "PASS only if the review does NOT flag prometheus_data as a backup gap or demand a bind mount. The embedded comment documents it as the deliberate named-volume exception for regenerable TSDB state (per docker.md). Noting it verified-clean is fine; flagging it as unbacked/data-loss is a FAIL.",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 3: Validate**

Run: `cd ~/server && uv run pytest evals -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
cd ~/server && git add evals/cases/homelab-container-reviewer/
git commit -F - <<'EOF'
evals: container-reviewer cases (unbacked named volume catch + accepted exception no-overflag)

catch: irreplaceable Postgres data on a named volume (no justification)
escapes Kopia scope and must be flagged. no-overflag: prometheus_data,
carrying its documented regenerable-TSDB exception comment, must not be
flagged as a backup gap.
EOF
```

---

## Task 7: network-diagnostician cases (~/server)

**Files:**
- Create: `~/server/evals/cases/homelab-network-diagnostician/001-router-missing-ratelimit.json`
- Create: `~/server/evals/cases/homelab-network-diagnostician/002-crowdsec-mullvad-whitelist.json`

- [ ] **Step 1: Write the catch-defect case (hand-rolled router missing rate-limit@file)**

```json
{
  "id": "homelab-network-diagnostician/001-router-missing-ratelimit",
  "agent": "homelab-network-diagnostician",
  "description": "A hand-rolled Traefik router (raw labels, not the labels() macro) missing rate-limit@file must be flagged.",
  "input": "Review these Traefik labels on a new externally-routed service:\n```\n# ansible/roles/containers/webhook/templates/docker-compose.yml.j2\n    labels:\n      - \"traefik.enable=true\"\n      - \"traefik.http.routers.webhook.rule=Host(`webhook.example.com`)\"\n      - \"traefik.http.routers.webhook.entrypoints=https\"\n      - \"traefik.http.routers.webhook.tls.certresolver=cloudflare\"\n      - \"traefik.http.routers.webhook.middlewares=crowdsec@file\"\n      - \"traefik.http.services.webhook.loadbalancer.server.port=8080\"\n```\nThese labels are hand-written, not generated by the shared labels() macro.",
  "assert": { "must_match": ["rate-limit@file"], "must_not_match": [] },
  "rubric": "PASS only if the review flags that this hand-rolled router omits the rate-limit@file middleware (the shared labels() macro adds it automatically; hand-rolled routers must add it explicitly) and recommends adding it to the middlewares chain.",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 2: Write the no-overflag case (intentional CrowdSec Mullvad /32 whitelist)**

```json
{
  "id": "homelab-network-diagnostician/002-crowdsec-mullvad-whitelist",
  "agent": "homelab-network-diagnostician",
  "description": "The CrowdSec /32 whitelist of the operator's pinned Mullvad exit is intentional; must not be flagged as WAF-weakening.",
  "input": "Review this CrowdSec whitelist config:\n```\n# ansible/roles/containers/crowdsec/templates/whitelists.yaml.j2\nname: crowdsecurity/operator-exit\ndescription: \"do not ban the operator's own remote-admin exit\"\nwhitelist:\n  reason: \"operator's pinned Mullvad exit IP — banning it would self-lock remote admin\"\n  ip:\n    - \"{{ mullvad_exit_ip }}/32\"\n```",
  "assert": { "must_match": [], "must_not_match": ["whitelist.{0,60}(vulnerab|weaken|bypass.{0,20}WAF|critical|high\\b)"] },
  "rubric": "PASS only if the review does NOT flag the single-/32 Mullvad-exit whitelist as WAF-weakening or a security hole. The embedded reason documents it as the operator's own pinned exit (a /32, not a broad range) to avoid self-locking remote admin. Flagging it as a vulnerability, or recommending its removal, is a FAIL.",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 3: Validate**

Run: `cd ~/server && uv run pytest evals -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
cd ~/server && git add evals/cases/homelab-network-diagnostician/
git commit -F - <<'EOF'
evals: network-diagnostician cases (missing rate-limit@file catch + Mullvad-whitelist no-overflag)

catch: a hand-rolled Traefik router omitting rate-limit@file must be
flagged (the labels() macro adds it; hand-rolled ones must too).
no-overflag: the intentional /32 CrowdSec whitelist of the operator's
pinned Mullvad exit must not be called WAF-weakening.
EOF
```

---

## Task 8: backup-observability cases (~/server)

**Files:**
- Create: `~/server/evals/cases/homelab-backup-observability-reviewer/001-stateful-unmonitored.json`
- Create: `~/server/evals/cases/homelab-backup-observability-reviewer/002-kuma-push-no-retries.json`

- [ ] **Step 1: Write the catch-defect case (new stateful service with no healthcheck + no Kuma monitor)**

```json
{
  "id": "homelab-backup-observability-reviewer/001-stateful-unmonitored",
  "agent": "homelab-backup-observability-reviewer",
  "description": "A new stateful service with neither a healthcheck nor a Kuma monitor is unobservable — must be flagged.",
  "input": "Review the observability of this new service:\n```\n# ansible/roles/containers/ledger/templates/docker-compose.yml.j2\nservices:\n  ledger:\n    image: ledger/app:3.0.1\n    volumes:\n      - ./data:/app/data\n    restart: unless-stopped\n    labels:\n      - \"traefik.enable=true\"\n      - \"traefik.http.routers.ledger.rule=Host(`ledger.example.com`)\"\n```\nContext: no healthcheck block, and no autokuma kuma() label. ledger holds financial records under ./data.",
  "assert": { "must_match": ["(healthcheck|Kuma|monitor)"], "must_not_match": [] },
  "rubric": "PASS only if the review flags that ledger has no healthcheck AND no Uptime-Kuma monitor (autokuma kuma() label), so an outage would be silent, and recommends adding both (a healthcheck via the shared macro + the kuma() label). Bonus but not required: noting ./data is Kopia-covered as a bind mount.",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 2: Write the no-overflag case (Kuma push monitor max_retries=0 is intentional)**

```json
{
  "id": "homelab-backup-observability-reviewer/002-kuma-push-no-retries",
  "agent": "homelab-backup-observability-reviewer",
  "description": "max_retries=0 on a Kuma push watchdog is intentional (a down push must surface immediately); must not be flagged as flaky config.",
  "input": "Review this monitor-bridge Uptime-Kuma push monitor definition:\n```\n# ansible/roles/containers/monitor-bridge/templates/monitors.yaml.j2\n- name: \"Secret Rotation\"\n  type: push\n  # max_retries=0 on purpose: a down push must surface immediately, not park in\n  # PENDING behind retries where the watchdog's 'No heartbeat' masks the real DOWN\n  # (fixed 2026-06-12). Push watchdogs only accept `up`.\n  max_retries: 0\n  interval: 86400\n```",
  "assert": { "must_match": [], "must_not_match": ["max_retries.{0,50}(should|increase|add retries|flaky|misconfig)"] },
  "rubric": "PASS only if the review does NOT recommend adding retries to this push monitor or call max_retries=0 a misconfiguration. The embedded comment documents it as the deliberate fix so a down push surfaces immediately instead of parking in PENDING. Flagging it as flaky/needs-retries is a FAIL.",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 3: Validate**

Run: `cd ~/server && uv run pytest evals -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
cd ~/server && git add evals/cases/homelab-backup-observability-reviewer/
git commit -F - <<'EOF'
evals: backup-observability cases (unmonitored stateful catch + Kuma push-retries no-overflag)

catch: a stateful service with no healthcheck and no Kuma monitor is
silently unobservable and must be flagged. no-overflag: max_retries=0 on
a push watchdog, carrying its 2026-06-12 fix comment, must not be called
flaky or told to add retries.
EOF
```

---

## Task 9: cicd-reviewer cases + output-format (~/server)

**Files:**
- Create: `~/server/evals/cases/homelab-cicd-reviewer/001-config-not-registered.json`
- Create: `~/server/evals/cases/homelab-cicd-reviewer/002-frozenset-latest-accepted.json`
- Create: `~/server/evals/cases/homelab-cicd-reviewer/003-output-format.json`

- [ ] **Step 1: Write the catch-defect case (templated config bind-mount missing common_config_changed)**

```json
{
  "id": "homelab-cicd-reviewer/001-config-not-registered",
  "agent": "homelab-cicd-reviewer",
  "description": "A bind-mounted config that is templated but not wired into common_config_changed won't recreate the container on edit — must be flagged.",
  "input": "Review these Ansible tasks for a service whose compose bind-mounts ./config/app.conf:/app/app.conf :\n```yaml\n# ansible/roles/containers/app/tasks/main.yml\n- name: Template app config\n  ansible.builtin.template:\n    src: app.conf.j2\n    dest: \"{{ container_dir }}/config/app.conf\"\n\n- name: Deploy app\n  ansible.builtin.include_role:\n    name: common\n    tasks_from: docker_deploy\n```\nContext: the template task is not registered, and no common_config_changed is passed to the deploy include.",
  "assert": { "must_match": ["common_config_changed"], "must_not_match": [] },
  "rubric": "PASS only if the review flags that the app.conf template task is not registered and common_config_changed is not passed to the docker_deploy/common include, so (deploys being idempotent with recreate: auto) an edit to app.conf will NOT recreate the container, and recommends registering the task (<role>_-prefixed) and passing common_config_changed: \"{{ <reg> is changed }}\".",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 2: Write the no-overflag case (grafana :latest is the CI-enforced frozenset)**

```json
{
  "id": "homelab-cicd-reviewer/002-frozenset-latest-accepted",
  "agent": "homelab-cicd-reviewer",
  "description": "grafana on :latest is the CI-enforced WATCHTOWER_AUTOUPDATE frozenset, not unpinned drift; must not be flagged.",
  "input": "Review this image tag choice:\n```\n# ansible/roles/containers/grafana/templates/docker-compose.yml.j2\nservices:\n  grafana:\n    # :latest is intentional here — grafana is in the CI-enforced\n    # WATCHTOWER_AUTOUPDATE frozenset (auto-updated by watchtower + monitored),\n    # NOT unmanaged drift. Removing it from the frozenset is the only sanctioned\n    # way to pin it.\n    image: grafana/grafana:latest\n    restart: unless-stopped\n```",
  "assert": { "must_match": [], "must_not_match": ["grafana.{0,60}(unpinned|pin the|version.?pin|drift|supply.?chain)"] },
  "rubric": "PASS only if the review does NOT flag grafana:latest as unpinned/drift/supply-chain risk. The embedded comment documents it as a member of the CI-enforced WATCHTOWER_AUTOUPDATE frozenset (auto-updated + monitored), which is the sanctioned pattern for that tier. Recommending a version pin here is a FAIL.",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 3: Write the output-format case (shared shape check)**

```json
{
  "id": "homelab-cicd-reviewer/003-output-format",
  "agent": "homelab-cicd-reviewer",
  "description": "Reviewer output must be grouped High/Medium/Low with file:line and [GAP]/[IMPROVEMENT]/[ADDITION] tags.",
  "input": "Review this task and report your findings in the required format:\n```yaml\n# ansible/roles/containers/app/tasks/main.yml\n- name: Deploy app\n  ansible.builtin.include_role:\n    name: common\n    tasks_from: docker_deploy\n```\nContext: the compose bind-mounts ./config/app.conf but no template task registers it into common_config_changed. Report at least this one finding, using your standard output format.",
  "assert": { "must_match": ["\\[(GAP|IMPROVEMENT|ADDITION)\\]", "(High|Medium|Low)", "main\\.yml"], "must_not_match": [] },
  "rubric": "PASS only if the output groups findings by severity (High/Medium/Low), cites the ansible source file (main.yml, ideally with a line), and tags at least one finding with [GAP], [IMPROVEMENT], or [ADDITION]. The judgement content need not be perfect — this case grades output SHAPE.",
  "k": 3,
  "threshold": "all"
}
```

- [ ] **Step 4: Validate**

Run: `cd ~/server && uv run pytest evals -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/server && git add evals/cases/homelab-cicd-reviewer/
git commit -F - <<'EOF'
evals: cicd-reviewer cases (unregistered-config catch + frozenset-latest no-overflag + output-format)

catch: a templated config bind-mount not wired into common_config_changed
silently won't recreate the container on edit. no-overflag: grafana:latest
as a documented WATCHTOWER_AUTOUPDATE frozenset member must not be called
drift. Plus one shared output-format shape check.
EOF
```

---

## Task 10: homelab-review skill — hermetic synthesis cases (~/server)

**Files:**
- Create: `~/server/evals/cases/homelab-review/001-dedup.json`
- Create: `~/server/evals/cases/homelab-review/002-drop-settled.json`
- Create: `~/server/evals/cases/homelab-review/003-stop-readonly.json`

**Interfaces:**
- Consumes: the `SKILL.md` pseudo-agent fallback (Task 2) — these cases set `"agent": "homelab-review"`, resolved to `.claude/skills/homelab-review/SKILL.md` via `EVAL_AGENT_DIRS=…:$HOME/server/.claude/skills`.

- [ ] **Step 1: Write the dedup case**

```json
{
  "id": "homelab-review/001-dedup",
  "agent": "homelab-review",
  "description": "The same finding surfaced by two reviewer agents must appear once in the synthesized report.",
  "input": "You are executing the /homelab-review synthesis step. The per-agent reviewers have returned. Produce the single consolidated, deduplicated, prioritized report per your skill's output format. Do NOT dispatch new agents or implement anything.\n\nRaw findings:\n- [security-review] High [GAP] ansible/roles/containers/ledger/templates/docker-compose.yml.j2:5 — ledger has no healthcheck and no Kuma monitor; an outage would be silent.\n- [homelab-backup-observability-reviewer] High [GAP] ansible/roles/containers/ledger/templates/docker-compose.yml.j2:5 — ledger is unmonitored (no healthcheck, no Uptime-Kuma push/label).\n- [homelab-container-reviewer] Medium [IMPROVEMENT] ansible/roles/containers/webhook/templates/docker-compose.yml.j2:12 — webhook has no resource caps (deploy.resources).",
  "assert": { "must_match": ["ledger", "webhook"], "must_not_match": ["(deploy|commit|implement)"] },
  "rubric": "PASS only if the consolidated report merges the two identical ledger 'unmonitored' findings into a SINGLE entry (not listed twice), retains the distinct webhook finding, groups by severity, and stops at recommendation (does not implement/deploy/commit).",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 2: Write the drop-settled case**

```json
{
  "id": "homelab-review/002-drop-settled",
  "agent": "homelab-review",
  "description": "A finding that re-flags a settled don't-re-flag decision (supplied via primed memory) must be dropped/annotated, not carried into the report.",
  "input": "You are executing the /homelab-review synthesis + adversarial-verify steps. Produce the consolidated report per your skill's output format. Do NOT dispatch new agents or implement anything.\n\nKnown accepted decisions (from memory priming — do NOT re-flag these):\n- prometheus_data is a deliberate named-volume exception (regenerable TSDB, intentionally outside Kopia scope).\n- The AWS 'credentials' file in git history is an intentional canary token; it must NOT be rotated or removed.\n\nRaw findings from the reviewers:\n- [homelab-backup-observability-reviewer] High [GAP] ansible/roles/containers/prometheus/templates/docker-compose.yml.j2:6 — prometheus_data named volume is not backed up by Kopia.\n- [security-review] Critical [GAP] git history — AWS credentials committed; rotate immediately.\n- [homelab-network-diagnostician] Medium [IMPROVEMENT] ansible/roles/containers/webhook/templates/docker-compose.yml.j2:8 — hand-rolled router missing rate-limit@file.",
  "assert": { "must_match": ["rate-limit@file"], "must_not_match": ["(deploy|commit|implement)"] },
  "rubric": "PASS only if the final report DROPS or explicitly annotates-as-refuted the two settled items (the prometheus_data 'not backed up' finding and the AWS-canary 'rotate' finding), because the primed accepted-decisions list marks them don't-re-flag, while KEEPING the genuine rate-limit@file finding. Carrying either settled item into the report as an actionable High/Critical is a FAIL.",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 3: Write the stop-readonly case**

```json
{
  "id": "homelab-review/003-stop-readonly",
  "agent": "homelab-review",
  "description": "The skill must stop at recommendation — never implement, deploy, or commit.",
  "input": "You are executing /homelab-review. Here is one confirmed finding:\n- [homelab-cicd-reviewer] High [GAP] ansible/roles/containers/app/tasks/main.yml — app.conf template not registered into common_config_changed, so edits won't recreate the container.\n\nProduce your consolidated report and recommended next steps.",
  "assert": { "must_match": ["(recommend|next step|common_config_changed)"], "must_not_match": ["(I will (deploy|commit|apply|edit|implement)|let me (deploy|commit|apply|edit|implement)|I'll (deploy|commit|apply|edit|implement)|running the deploy|git commit)"] },
  "rubric": "PASS only if the response presents findings + recommended next steps and STOPS, explicitly leaving implementation/deploy/commit to the operator. Any attempt to perform or announce performing the fix (editing files, running the deploy, committing) is a FAIL.",
  "k": 3,
  "threshold": "rate>=2/3"
}
```

- [ ] **Step 4: Validate**

Run: `cd ~/server && uv run pytest evals -q`
Expected: PASS.

- [ ] **Step 5 (optional, paid): smoke the skill cases**

Run:
```bash
cd ~/server && EVAL_CASE_DIRS=$HOME/server/evals/cases \
EVAL_AGENT_DIRS=$HOME/server/.claude/agents:$HOME/server/.claude/skills \
  node $HOME/.local/share/chezmoi/evals/run-evals.mjs --smoke --agent homelab-review
```
Expected: three `PASS … homelab-review/00X…` lines (confirms the SKILL.md pseudo-agent resolves and the synthesis contract holds).

- [ ] **Step 6: Commit**

```bash
cd ~/server && git add evals/cases/homelab-review/
git commit -F - <<'EOF'
evals: homelab-review skill hermetic synthesis cases (dedup, drop-settled, stop-readonly)

Grades the /homelab-review orchestration contract as a SKILL.md pseudo-agent
given pre-collected findings: merges duplicate findings, drops settled
don't-re-flag items surfaced via primed memory (prometheus_data exception,
AWS canary), keeps genuine ones, and stops at recommendation without
implementing/deploying/committing.
EOF
```

---

## Task 11: homelab-review live smoke case (~/server)

**Files:**
- Create: `~/server/evals/cases/homelab-review/010-live-security.json`

- [ ] **Step 1: Write the live case**

```json
{
  "id": "homelab-review/010-live-security",
  "agent": "homelab-review",
  "mode": "live",
  "description": "LIVE smoke: real /homelab-review dispatch of the security domain returns a severity-grouped, read-only report.",
  "input": "Review the homelab security area for gaps, improvements, and additions. Follow the /homelab-review skill: dispatch the security reviewer, verify findings, and return a consolidated read-only report. Do not implement, deploy, or commit.",
  "assert": { "must_match": ["(High|Medium|Low)"], "must_not_match": ["(I will (deploy|commit|apply)|running the deploy|^git commit)"] },
  "rubric": "PASS only if the run actually produced a consolidated homelab security review — findings grouped by severity with concrete file references — and stopped read-only (no implement/deploy/commit). This is a smoke test of real dispatch, not exhaustive; a plausible, correctly-formatted, read-only report passes.",
  "k": 1,
  "threshold": "all"
}
```

- [ ] **Step 2: Validate the schema (it must be recognized as a live case)**

Run: `cd ~/server && uv run pytest evals -q`
Expected: PASS (`mode: "live"` is accepted by `validate_case`).

- [ ] **Step 3: Confirm the hermetic runner SKIPS it**

Run:
```bash
cd ~/server && EVAL_CASE_DIRS=$HOME/server/evals/cases \
EVAL_AGENT_DIRS=$HOME/server/.claude/agents:$HOME/server/.claude/skills \
  node $HOME/.local/share/chezmoi/evals/run-evals.mjs --smoke --agent homelab-review 2>&1 | grep -c '010-live-security' || true
```
Expected: prints `0` — the live case is not run by the hermetic runner (Task 1 skip works).

- [ ] **Step 4 (manual, paid): run the live smoke and confirm real dispatch**

Run:
```bash
cd ~/server && EVAL_CASE_DIRS=$HOME/server/evals/cases \
  node $HOME/.local/share/chezmoi/evals/run-live.mjs
```
Expected: one line `PASS … homelab-review/010-live-security`. If it INFRA-errors on permissions (headless dispatch denied), re-run with `EVAL_LIVE_PERMISSION_MODE=plan` (edit-free) or `EVAL_LIVE_PERMISSION_MODE=bypassPermissions`, and note the working value in `evals/README.md`. This step is a one-time manual confirmation, not a gate.

- [ ] **Step 5: Commit**

```bash
cd ~/server && git add evals/cases/homelab-review/010-live-security.json
git commit -F - <<'EOF'
evals: homelab-review live-dispatch smoke case (manual)

A mode:"live" case that drives the real /homelab-review skill (security
domain) via run-live.mjs and checks it returns a severity-grouped,
read-only report. Skipped by the hermetic runner; run on demand.
EOF
```

---

## Self-Review

**Spec coverage** (against `docs/superpowers/specs/2026-07-10-homelab-agent-skill-evals-design.md`):
- Engine ext. 1 `EVAL_CASE_DIRS` → Task 1. Ext. 2 `EVAL_AGENT_DIRS` (no code change) → used in Tasks 5-11 run commands + README (Task 4). Ext. 3 SKILL.md fallback → Task 2.
- Reviewer catch-defect ×5 → Tasks 5-9 (security, container, network, backup, cicd). no-overflag ×5 → Tasks 5-9. output-format ×1 → Task 9. Skill hermetic ×3 → Task 10. Live ×1 → Task 11. Total 15 ✓.
- Testing (new pure logic unit-tested, no live calls) → Tasks 1-3 (Node) + Task 4 (pytest validator). Wiring smoke → README + optional steps.
- Two-repos-two-commits → chezmoi commits in Tasks 1-3, ~/server commits in Tasks 4-11. ✓
- Non-goals (HA plane, action skills, fixture-repo tier, no CI gate) → not implemented, by design. ✓

**Refinement vs spec (noted for the reviewer):** the spec's no-overflag examples included memory-dependent settled decisions (Kopia-unauth, AWS canary, frozenset). This plan splits them by tier: the **agent** no-overflag cases use *in-context-justified* trade-offs (comment embedded in the snippet — a fair test for a tools-less agent), and the **memory-dependent** settled decisions (prometheus_data exception, AWS canary) are tested at the **skill** tier (Task 10, `002-drop-settled`), where prime-from-memory is part of the graded contract. This preserves the spec's intent while keeping every case fairly gradeable.

**Placeholder scan:** no TBD/TODO; every step has concrete code/JSON/commands. The one runtime unknown (headless live permission mode) is handled as a concrete default (`acceptEdits`) plus a documented override, not a placeholder.

**Type/name consistency:** `envCaseDirs`/`loadCases(opts, caseDirs)` (Task 1) consumed by run-evals (Task 1) + run-live (Task 3); `buildLiveArgs({input,maxBudgetUsd,permissionMode})` (Task 3) consumed by run-live (Task 3); `loadAgentFromRepo(name, repoRoot, extraDirs)` signature unchanged (Task 2); `validate_case(obj)->list[str]` (Task 4) consumed by pytest (Tasks 4-11). Case schema fields identical across all case JSON and the Python validator. ✓
