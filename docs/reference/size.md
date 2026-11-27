---
generated_from: scripts/docs/reference/size.py
generated_at: 2026-11-27 06:17 UTC
generated_sha: f8cc3c595
---

!!! warning "Generated file — do not edit"
    This page is rendered from this repository by `scripts/docs/reference/size.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Repo size

Tracked files and lines at the last commit before 00:00 UTC on the day in `generated_at`, and at the last commit before 00:00 UTC 30 and 90 days earlier. A proposal names its net complexity delta against these numbers. A line is one line of a tracked file as `git diff --numstat` counts it from the empty tree; a binary file counts as a file with no lines. The files the docs-refresh cron commits are not counted: `docs/reference/`, `docs/assets/generated/`, `scripts/dev/pytest_shard_runner_weights.json`, `scripts/dev/affected_tests.json`.

## Files and lines per top-level tree

| Tree | Files today | Lines today | Files 30 days earlier | Lines 30 days earlier | Files 90 days earlier | Lines 90 days earlier |
|---|---:|---:|---:|---:|---:|---:|
| `scripts/` | 1,238 | 261,042 | 1,168 | 271,244 | 113 | 29,048 |
| `k8s/` | 1,052 | 179,826 | 0 | 0 | 0 | 0 |
| `docs/` | 210 | 125,104 | 448 | 110,984 | 78 | 22,797 |
| `hosts/` | 942 | 110,301 | 263 | 29,853 | 0 | 0 |
| `tests/` | 472 | 83,469 | 0 | 0 | 0 | 0 |
| `.claude/` | 111 | 20,263 | 114 | 20,710 | 57 | 6,152 |
| `evals/` | 56 | 7,245 | 53 | 4,956 | 36 | 1,038 |
| `.github/` | 21 | 5,587 | 14 | 4,241 | 4 | 627 |
| (root files) | 15 | 4,974 | 19 | 5,220 | 20 | 3,146 |
| `secrets/` | 14 | 1,193 | 0 | 0 | 0 | 0 |
| `styles/` | 3 | 685 | 3 | 671 | 3 | 342 |
| `estate/` | 2 | 187 | 0 | 0 | 0 | 0 |
| `.vscode/` | 1 | 15 | 1 | 18 | 1 | 18 |
| `ansible/` | 0 | 0 | 2,217 | 397,330 | 1,207 | 155,119 |
| **Total** | 4,137 | 799,891 | 4,300 | 845,227 | 1,519 | 218,287 |

## Tests and instructions

A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module directly under `.claude/hooks/`; its tests are not counted.

| Measure | today | 30 days earlier | 90 days earlier |
|---|---:|---:|---:|
| Test files | 1,455 | 1,552 | 225 |
| Instruction lines (every `CLAUDE.md` plus `.claude/`) | 31,575 | 30,919 | 13,354 |
| Rules (`.claude/rules/*.md`) | 7 | 7 | 3 |
| Skills (`.claude/skills/*/SKILL.md`) | 15 | 13 | 10 |
| Agents (`.claude/agents/*.md`) | 8 | 8 | 8 |
| Hooks (`.claude/hooks/*.py`) | 18 | 18 | 14 |
