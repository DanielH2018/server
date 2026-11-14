---
generated_from: scripts/docs/reference/size.py
generated_at: 2026-11-14 07:08 UTC
generated_sha: 7abd7c96f
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
| `scripts/` | 1,135 | 243,397 | 866 | 197,371 | 43 | 11,886 |
| `k8s/` | 959 | 169,264 | 0 | 0 | 0 | 0 |
| `docs/` | 191 | 115,343 | 163 | 98,875 | 30 | 9,055 |
| `hosts/` | 779 | 97,026 | 34 | 2,762 | 0 | 0 |
| `tests/` | 466 | 79,069 | 0 | 0 | 0 | 0 |
| `.claude/` | 109 | 18,797 | 103 | 19,046 | 48 | 4,467 |
| `.github/` | 18 | 5,799 | 11 | 2,272 | 3 | 445 |
| `evals/` | 54 | 5,759 | 53 | 4,113 | 24 | 836 |
| (root files) | 17 | 4,735 | 19 | 4,591 | 16 | 2,015 |
| `secrets/` | 13 | 1,146 | 0 | 0 | 0 | 0 |
| `inventory/` | 5 | 1,105 | 0 | 0 | 0 | 0 |
| `cluster/` | 1 | 1,019 | 0 | 0 | 0 | 0 |
| `styles/` | 3 | 684 | 3 | 666 | 0 | 0 |
| `.vscode/` | 1 | 16 | 1 | 18 | 1 | 18 |
| `ansible/` | 0 | 0 | 1,845 | 314,963 | 925 | 99,490 |
| **Total** | 3,751 | 743,159 | 3,098 | 644,677 | 1,090 | 128,212 |

## Tests and instructions

A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module directly under `.claude/hooks/`; its tests are not counted.

| Measure | today | 30 days earlier | 90 days earlier |
|---|---:|---:|---:|
| Test files | 1,403 | 1,178 | 75 |
| Instruction lines (every `CLAUDE.md` plus `.claude/`) | 27,617 | 26,350 | 8,664 |
| Rules (`.claude/rules/*.md`) | 7 | 7 | 3 |
| Skills (`.claude/skills/*/SKILL.md`) | 13 | 13 | 10 |
| Agents (`.claude/agents/*.md`) | 8 | 8 | 7 |
| Hooks (`.claude/hooks/*.py`) | 17 | 16 | 8 |
