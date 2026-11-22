---
generated_from: scripts/docs/reference/size.py
generated_at: 2026-11-22 06:17 UTC
generated_sha: 0d03adcb5
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
| `scripts/` | 1,216 | 260,044 | 1,048 | 246,673 | 79 | 21,841 |
| `k8s/` | 1,042 | 183,870 | 0 | 0 | 0 | 0 |
| `docs/` | 207 | 123,109 | 435 | 107,474 | 47 | 17,073 |
| `hosts/` | 894 | 108,924 | 49 | 3,819 | 0 | 0 |
| `tests/` | 460 | 83,416 | 0 | 0 | 0 | 0 |
| `.claude/` | 111 | 20,385 | 107 | 19,652 | 54 | 5,536 |
| `evals/` | 56 | 6,677 | 53 | 4,769 | 36 | 1,038 |
| `.github/` | 21 | 6,045 | 12 | 3,533 | 3 | 563 |
| (root files) | 15 | 4,890 | 19 | 5,015 | 16 | 2,448 |
| `secrets/` | 14 | 1,183 | 0 | 0 | 0 | 0 |
| `cluster/` | 1 | 1,063 | 0 | 0 | 0 | 0 |
| `inventory/` | 5 | 1,027 | 0 | 0 | 0 | 0 |
| `styles/` | 3 | 686 | 3 | 666 | 0 | 0 |
| `.vscode/` | 1 | 14 | 1 | 18 | 1 | 18 |
| `ansible/` | 0 | 0 | 2,116 | 369,593 | 1,096 | 133,509 |
| **Total** | 4,046 | 801,333 | 3,843 | 761,212 | 1,332 | 182,026 |

## Tests and instructions

A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module directly under `.claude/hooks/`; its tests are not counted.

| Measure | today | 30 days earlier | 90 days earlier |
|---|---:|---:|---:|
| Test files | 1,430 | 1,380 | 160 |
| Instruction lines (every `CLAUDE.md` plus `.claude/`) | 31,302 | 29,461 | 11,848 |
| Rules (`.claude/rules/*.md`) | 7 | 7 | 3 |
| Skills (`.claude/skills/*/SKILL.md`) | 15 | 13 | 10 |
| Agents (`.claude/agents/*.md`) | 8 | 8 | 8 |
| Hooks (`.claude/hooks/*.py`) | 18 | 17 | 12 |
