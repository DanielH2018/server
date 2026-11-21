---
generated_from: scripts/docs/reference/size.py
generated_at: 2026-11-21 15:45 UTC
generated_sha: 7a64823b9
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
| `scripts/` | 1,207 | 257,860 | 1,015 | 240,185 | 66 | 21,371 |
| `k8s/` | 1,037 | 182,423 | 0 | 0 | 0 | 0 |
| `docs/` | 206 | 122,332 | 426 | 106,712 | 47 | 17,037 |
| `hosts/` | 890 | 108,098 | 49 | 3,819 | 0 | 0 |
| `tests/` | 455 | 83,036 | 0 | 0 | 0 | 0 |
| `.claude/` | 111 | 20,248 | 105 | 19,532 | 51 | 5,204 |
| `evals/` | 65 | 7,415 | 53 | 4,769 | 36 | 1,038 |
| `.github/` | 21 | 5,930 | 11 | 3,271 | 3 | 563 |
| (root files) | 15 | 4,862 | 19 | 4,991 | 16 | 2,225 |
| `secrets/` | 14 | 1,183 | 0 | 0 | 0 | 0 |
| `cluster/` | 1 | 1,063 | 0 | 0 | 0 | 0 |
| `inventory/` | 5 | 1,043 | 0 | 0 | 0 | 0 |
| `styles/` | 3 | 686 | 3 | 666 | 0 | 0 |
| `.vscode/` | 1 | 14 | 1 | 18 | 1 | 18 |
| `ansible/` | 0 | 0 | 2,082 | 360,186 | 1,063 | 130,823 |
| **Total** | 4,031 | 796,193 | 3,764 | 744,149 | 1,283 | 178,279 |

## Tests and instructions

A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module directly under `.claude/hooks/`; its tests are not counted.

| Measure | today | 30 days earlier | 90 days earlier |
|---|---:|---:|---:|
| Test files | 1,428 | 1,352 | 133 |
| Instruction lines (every `CLAUDE.md` plus `.claude/`) | 31,058 | 28,849 | 11,153 |
| Rules (`.claude/rules/*.md`) | 7 | 7 | 3 |
| Skills (`.claude/skills/*/SKILL.md`) | 15 | 13 | 10 |
| Agents (`.claude/agents/*.md`) | 8 | 8 | 8 |
| Hooks (`.claude/hooks/*.py`) | 18 | 17 | 10 |
