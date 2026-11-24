---
generated_from: scripts/docs/reference/size.py
generated_at: 2026-11-24 06:17 UTC
generated_sha: 76aa3d52a
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
| `scripts/` | 1,228 | 262,452 | 1,134 | 262,283 | 102 | 26,507 |
| `k8s/` | 1,043 | 185,638 | 0 | 0 | 0 | 0 |
| `docs/` | 208 | 124,448 | 453 | 110,047 | 76 | 22,038 |
| `hosts/` | 926 | 111,637 | 156 | 18,078 | 0 | 0 |
| `tests/` | 464 | 83,804 | 0 | 0 | 0 | 0 |
| `.claude/` | 111 | 20,443 | 112 | 20,430 | 54 | 5,689 |
| `evals/` | 56 | 7,117 | 53 | 4,769 | 36 | 1,038 |
| `.github/` | 21 | 6,101 | 14 | 4,206 | 3 | 590 |
| (root files) | 15 | 4,874 | 19 | 5,123 | 18 | 3,013 |
| `secrets/` | 14 | 1,183 | 0 | 0 | 0 | 0 |
| `cluster/` | 1 | 1,063 | 0 | 0 | 0 | 0 |
| `inventory/` | 5 | 1,033 | 0 | 0 | 0 | 0 |
| `styles/` | 3 | 686 | 3 | 671 | 3 | 301 |
| `.vscode/` | 1 | 14 | 1 | 18 | 1 | 18 |
| `ansible/` | 0 | 0 | 2,247 | 399,057 | 1,153 | 144,692 |
| **Total** | 4,096 | 810,493 | 4,192 | 824,682 | 1,446 | 203,886 |

## Tests and instructions

A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module directly under `.claude/hooks/`; its tests are not counted.

| Measure | today | 30 days earlier | 90 days earlier |
|---|---:|---:|---:|
| Test files | 1,446 | 1,511 | 183 |
| Instruction lines (every `CLAUDE.md` plus `.claude/`) | 31,511 | 30,780 | 12,380 |
| Rules (`.claude/rules/*.md`) | 7 | 7 | 3 |
| Skills (`.claude/skills/*/SKILL.md`) | 15 | 13 | 10 |
| Agents (`.claude/agents/*.md`) | 8 | 8 | 8 |
| Hooks (`.claude/hooks/*.py`) | 18 | 18 | 12 |
