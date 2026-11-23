---
generated_from: scripts/docs/reference/size.py
generated_at: 2026-11-23 06:17 UTC
generated_sha: 2933fca28
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
| `scripts/` | 1,227 | 262,018 | 1,090 | 252,307 | 98 | 24,731 |
| `k8s/` | 1,041 | 184,692 | 0 | 0 | 0 | 0 |
| `docs/` | 208 | 124,202 | 442 | 108,476 | 73 | 21,633 |
| `hosts/` | 920 | 111,145 | 63 | 6,286 | 0 | 0 |
| `tests/` | 462 | 83,599 | 0 | 0 | 0 | 0 |
| `.claude/` | 111 | 20,441 | 107 | 19,653 | 54 | 5,612 |
| `evals/` | 56 | 7,117 | 53 | 4,769 | 36 | 1,038 |
| `.github/` | 21 | 6,101 | 13 | 3,926 | 3 | 579 |
| (root files) | 15 | 4,868 | 19 | 5,038 | 18 | 2,908 |
| `secrets/` | 14 | 1,183 | 0 | 0 | 0 | 0 |
| `cluster/` | 1 | 1,063 | 0 | 0 | 0 | 0 |
| `inventory/` | 5 | 1,034 | 0 | 0 | 0 | 0 |
| `styles/` | 3 | 686 | 3 | 671 | 3 | 294 |
| `.vscode/` | 1 | 14 | 1 | 18 | 1 | 18 |
| `ansible/` | 0 | 0 | 2,183 | 389,021 | 1,141 | 138,760 |
| **Total** | 4,085 | 808,163 | 3,974 | 790,165 | 1,427 | 195,573 |

## Tests and instructions

A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module directly under `.claude/hooks/`; its tests are not counted.

| Measure | today | 30 days earlier | 90 days earlier |
|---|---:|---:|---:|
| Test files | 1,442 | 1,436 | 180 |
| Instruction lines (every `CLAUDE.md` plus `.claude/`) | 31,482 | 29,675 | 12,279 |
| Rules (`.claude/rules/*.md`) | 7 | 7 | 3 |
| Skills (`.claude/skills/*/SKILL.md`) | 15 | 13 | 10 |
| Agents (`.claude/agents/*.md`) | 8 | 8 | 8 |
| Hooks (`.claude/hooks/*.py`) | 18 | 17 | 12 |
