---
generated_from: scripts/docs/reference/size.py
generated_at: 2026-11-19 22:39 UTC
generated_sha: f1939c91b
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
| `scripts/` | 1,211 | 260,576 | 1,014 | 240,073 | 57 | 17,831 |
| `k8s/` | 974 | 172,572 | 0 | 0 | 0 | 0 |
| `docs/` | 198 | 118,818 | 425 | 106,678 | 45 | 16,601 |
| `hosts/` | 838 | 105,611 | 49 | 3,819 | 0 | 0 |
| `tests/` | 481 | 81,764 | 0 | 0 | 0 | 0 |
| `.claude/` | 113 | 19,425 | 105 | 19,532 | 51 | 5,085 |
| `evals/` | 64 | 6,951 | 53 | 4,443 | 36 | 1,038 |
| `.github/` | 21 | 6,254 | 11 | 3,271 | 3 | 445 |
| (root files) | 15 | 4,913 | 19 | 4,991 | 16 | 2,120 |
| `secrets/` | 14 | 1,141 | 0 | 0 | 0 | 0 |
| `inventory/` | 5 | 1,122 | 0 | 0 | 0 | 0 |
| `cluster/` | 1 | 1,037 | 0 | 0 | 0 | 0 |
| `styles/` | 3 | 684 | 3 | 666 | 0 | 0 |
| `.vscode/` | 1 | 14 | 1 | 18 | 1 | 18 |
| `ansible/` | 0 | 0 | 2,092 | 363,145 | 1,023 | 117,421 |
| **Total** | 3,939 | 780,882 | 3,772 | 746,636 | 1,232 | 160,559 |

## Tests and instructions

A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module directly under `.claude/hooks/`; its tests are not counted.

| Measure | today | 30 days earlier | 90 days earlier |
|---|---:|---:|---:|
| Test files | 1,474 | 1,356 | 111 |
| Instruction lines (every `CLAUDE.md` plus `.claude/`) | 29,205 | 28,837 | 9,505 |
| Rules (`.claude/rules/*.md`) | 7 | 7 | 3 |
| Skills (`.claude/skills/*/SKILL.md`) | 14 | 13 | 10 |
| Agents (`.claude/agents/*.md`) | 8 | 8 | 8 |
| Hooks (`.claude/hooks/*.py`) | 17 | 17 | 10 |
