---
generated_from: scripts/docs/reference/size.py
generated_at: 2026-11-20 01:49 UTC
generated_sha: a1215438a
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
| `scripts/` | 1,209 | 260,327 | 1,014 | 240,073 | 57 | 18,307 |
| `k8s/` | 994 | 175,408 | 0 | 0 | 0 | 0 |
| `docs/` | 217 | 121,098 | 425 | 106,692 | 47 | 17,004 |
| `hosts/` | 859 | 107,980 | 49 | 3,819 | 0 | 0 |
| `tests/` | 459 | 81,874 | 0 | 0 | 0 | 0 |
| `.claude/` | 110 | 19,964 | 105 | 19,532 | 51 | 5,085 |
| `evals/` | 65 | 7,415 | 53 | 4,769 | 36 | 1,038 |
| `.github/` | 20 | 5,864 | 11 | 3,271 | 3 | 563 |
| (root files) | 15 | 4,901 | 19 | 4,991 | 16 | 2,187 |
| `secrets/` | 14 | 1,164 | 0 | 0 | 0 | 0 |
| `inventory/` | 5 | 1,105 | 0 | 0 | 0 | 0 |
| `cluster/` | 1 | 1,049 | 0 | 0 | 0 | 0 |
| `styles/` | 3 | 686 | 3 | 666 | 0 | 0 |
| `.vscode/` | 1 | 14 | 1 | 18 | 1 | 18 |
| `ansible/` | 0 | 0 | 2,092 | 363,327 | 1,040 | 123,515 |
| **Total** | 3,972 | 788,849 | 3,772 | 747,158 | 1,251 | 167,717 |

## Tests and instructions

A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module directly under `.claude/hooks/`; its tests are not counted.

| Measure | today | 30 days earlier | 90 days earlier |
|---|---:|---:|---:|
| Test files | 1,438 | 1,356 | 115 |
| Instruction lines (every `CLAUDE.md` plus `.claude/`) | 29,978 | 28,837 | 10,266 |
| Rules (`.claude/rules/*.md`) | 7 | 7 | 3 |
| Skills (`.claude/skills/*/SKILL.md`) | 15 | 13 | 10 |
| Agents (`.claude/agents/*.md`) | 8 | 8 | 8 |
| Hooks (`.claude/hooks/*.py`) | 18 | 17 | 10 |
