---
generated_from: scripts/docs/reference/size.py
generated_at: 2026-11-16 18:17 UTC
generated_sha: 17f361985
---

!!! warning "Generated file — do not edit"
    This page is rendered from this repository by `scripts/docs/reference/size.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Repo size

Tracked files and lines at the last commit before 00:00 UTC on the day in `generated_at`, and at the last commit before 00:00 UTC 30 and 90 days earlier. A proposal names its net complexity delta against these numbers. A line is one line of a tracked file as `git diff --numstat` counts it from the empty tree; a binary file counts as a file with no lines. The files the docs-refresh cron commits are not counted: `docs/reference/`, `docs/assets/generated/`, `scripts/dev/pytest_shard_weights.json`, `scripts/dev/pytest_shard_runner_weights.json`, `scripts/dev/affected_tests.json`.

## Files and lines per top-level tree

| Tree | Files today | Lines today | Files 30 days earlier | Lines 30 days earlier | Files 90 days earlier | Lines 90 days earlier |
|---|---:|---:|---:|---:|---:|---:|
| `scripts/` | 1,196 | 257,311 | 947 | 222,074 | 51 | 16,842 |
| `k8s/` | 970 | 172,868 | 0 | 0 | 0 | 0 |
| `docs/` | 199 | 118,777 | 165 | 102,565 | 40 | 14,218 |
| `hosts/` | 836 | 105,785 | 49 | 3,819 | 0 | 0 |
| `tests/` | 477 | 81,232 | 0 | 0 | 0 | 0 |
| `.claude/` | 112 | 19,284 | 103 | 19,142 | 51 | 5,085 |
| `evals/` | 63 | 6,936 | 53 | 4,113 | 36 | 1,038 |
| `.github/` | 19 | 6,053 | 11 | 2,928 | 3 | 445 |
| (root files) | 15 | 4,963 | 19 | 4,806 | 16 | 2,095 |
| `secrets/` | 14 | 1,147 | 0 | 0 | 0 | 0 |
| `inventory/` | 5 | 1,119 | 0 | 0 | 0 | 0 |
| `cluster/` | 1 | 1,025 | 0 | 0 | 0 | 0 |
| `styles/` | 3 | 684 | 3 | 666 | 0 | 0 |
| `.vscode/` | 1 | 14 | 1 | 18 | 1 | 18 |
| `ansible/` | 0 | 0 | 1,935 | 332,697 | 959 | 106,056 |
| **Total** | 3,911 | 777,198 | 3,286 | 692,828 | 1,157 | 145,797 |

## Tests and instructions

A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module directly under `.claude/hooks/`; its tests are not counted.

| Measure | today | 30 days earlier | 90 days earlier |
|---|---:|---:|---:|
| Test files | 1,464 | 1,264 | 92 |
| Instruction lines (every `CLAUDE.md` plus `.claude/`) | 29,021 | 26,806 | 9,456 |
| Rules (`.claude/rules/*.md`) | 7 | 7 | 3 |
| Skills (`.claude/skills/*/SKILL.md`) | 13 | 13 | 10 |
| Agents (`.claude/agents/*.md`) | 8 | 8 | 8 |
| Hooks (`.claude/hooks/*.py`) | 17 | 16 | 10 |
