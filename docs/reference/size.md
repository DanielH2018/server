---
generated_from: scripts/docs/reference/size.py
generated_at: 2026-11-15 14:48 UTC
generated_sha: 8ddb8b045
---

!!! warning "Generated file — do not edit"
    This page is rendered from this repository by `scripts/docs/reference/size.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Repo size

Tracked files and lines at the last commit before 00:00 UTC on the day in `generated_at`, and at the last commit before 00:00 UTC 30 and 90 days earlier. A proposal names its net complexity delta against these numbers. A line is one line of a tracked file as `git diff --numstat` counts it from the empty tree; a binary file counts as a file with no lines. The files the docs-refresh cron commits are not counted: `docs/reference/`, `docs/assets/generated/`, `scripts/dev/pytest_shard_weights.json`, `scripts/dev/pytest_shard_runner_weights.json`.

## Files and lines per top-level tree

| Tree | Files today | Lines today | Files 30 days earlier | Lines 30 days earlier | Files 90 days earlier | Lines 90 days earlier |
|---|---:|---:|---:|---:|---:|---:|
| `scripts/` | 1,177 | 253,280 | 894 | 207,434 | 51 | 14,640 |
| `k8s/` | 968 | 171,977 | 0 | 0 | 0 | 0 |
| `docs/` | 197 | 118,228 | 165 | 100,949 | 37 | 12,465 |
| `hosts/` | 816 | 103,137 | 35 | 2,772 | 0 | 0 |
| `tests/` | 475 | 81,022 | 0 | 0 | 0 | 0 |
| `.claude/` | 112 | 19,041 | 103 | 19,100 | 48 | 4,590 |
| `evals/` | 63 | 6,086 | 53 | 4,113 | 24 | 836 |
| `.github/` | 18 | 5,909 | 11 | 2,661 | 3 | 445 |
| (root files) | 15 | 4,877 | 19 | 4,668 | 16 | 2,095 |
| `secrets/` | 13 | 1,147 | 0 | 0 | 0 | 0 |
| `inventory/` | 5 | 1,113 | 0 | 0 | 0 | 0 |
| `cluster/` | 1 | 1,017 | 0 | 0 | 0 | 0 |
| `styles/` | 3 | 684 | 3 | 666 | 0 | 0 |
| `.vscode/` | 1 | 14 | 1 | 18 | 1 | 18 |
| `ansible/` | 0 | 0 | 1,909 | 326,195 | 945 | 104,274 |
| **Total** | 3,864 | 767,532 | 3,193 | 668,576 | 1,125 | 139,363 |

## Tests and instructions

A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module directly under `.claude/hooks/`; its tests are not counted.

| Measure | today | 30 days earlier | 90 days earlier |
|---|---:|---:|---:|
| Test files | 1,447 | 1,221 | 89 |
| Instruction lines (every `CLAUDE.md` plus `.claude/`) | 28,523 | 26,671 | 8,943 |
| Rules (`.claude/rules/*.md`) | 7 | 7 | 3 |
| Skills (`.claude/skills/*/SKILL.md`) | 13 | 13 | 10 |
| Agents (`.claude/agents/*.md`) | 8 | 8 | 7 |
| Hooks (`.claude/hooks/*.py`) | 17 | 16 | 8 |
