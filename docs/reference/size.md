---
generated_from: scripts/docs/reference/size.py
generated_at: 2026-11-28 06:17 UTC
generated_sha: 696c721c2
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
| `scripts/` | 1,266 | 265,734 | 1,170 | 271,711 | 136 | 35,012 |
| `k8s/` | 1,086 | 182,503 | 0 | 0 | 0 | 0 |
| `docs/` | 214 | 126,260 | 448 | 111,096 | 80 | 23,344 |
| `hosts/` | 949 | 111,147 | 265 | 30,050 | 0 | 0 |
| `tests/` | 475 | 83,662 | 0 | 0 | 0 | 0 |
| `.claude/` | 112 | 20,437 | 115 | 20,746 | 74 | 8,129 |
| `evals/` | 56 | 7,245 | 53 | 4,956 | 37 | 1,051 |
| `.github/` | 21 | 5,587 | 14 | 4,241 | 4 | 627 |
| (root files) | 15 | 5,031 | 20 | 5,236 | 20 | 3,018 |
| `secrets/` | 15 | 1,245 | 0 | 0 | 0 | 0 |
| `styles/` | 3 | 690 | 3 | 671 | 3 | 359 |
| `estate/` | 2 | 187 | 0 | 0 | 0 | 0 |
| `.vscode/` | 1 | 15 | 1 | 18 | 1 | 18 |
| `ansible/` | 0 | 0 | 2,221 | 398,529 | 1,245 | 163,710 |
| **Total** | 4,215 | 809,743 | 4,310 | 847,254 | 1,600 | 235,268 |

## Tests and instructions

A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module directly under `.claude/hooks/`; its tests are not counted.

| Measure | today | 30 days earlier | 90 days earlier |
|---|---:|---:|---:|
| Test files | 1,495 | 1,558 | 261 |
| Instruction lines (every `CLAUDE.md` plus `.claude/`) | 31,924 | 30,966 | 15,615 |
| Rules (`.claude/rules/*.md`) | 7 | 7 | 3 |
| Skills (`.claude/skills/*/SKILL.md`) | 15 | 13 | 16 |
| Agents (`.claude/agents/*.md`) | 8 | 8 | 8 |
| Hooks (`.claude/hooks/*.py`) | 18 | 18 | 21 |
