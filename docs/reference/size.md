---
generated_from: scripts/docs/reference/size.py
generated_at: 2026-11-14 06:58 UTC
generated_sha: 8ad862383
---

!!! warning "Generated file — do not edit"
    This page is rendered from this repository by `scripts/docs/reference/size.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Repo size

Tracked files and lines at HEAD and at the last commit 30 and 90 days before this page was generated. A proposal names its net complexity delta against these numbers. A line is one line of a tracked file as `git diff --numstat` counts it from the empty tree; a binary file counts as a file with no lines.

*today* is `8ad862383` (2026-11-14); *30 days earlier* is `58ed8aa29` (2026-10-15); *90 days earlier* is `57da6a28a` (2026-08-16).

## Files and lines per top-level tree

| Tree | Files today | Lines today | Files 30 days earlier | Lines 30 days earlier | Files 90 days earlier | Lines 90 days earlier |
|---|---:|---:|---:|---:|---:|---:|
| `scripts/` | 1,149 | 247,968 | 881 | 202,621 | 43 | 11,886 |
| `k8s/` | 968 | 170,702 | 0 | 0 | 0 | 0 |
| `docs/` | 231 | 118,423 | 205 | 102,719 | 30 | 9,061 |
| `hosts/` | 781 | 97,403 | 35 | 2,772 | 0 | 0 |
| `tests/` | 469 | 79,515 | 0 | 0 | 0 | 0 |
| `.claude/` | 109 | 18,813 | 103 | 19,062 | 48 | 4,467 |
| `.github/` | 18 | 5,802 | 11 | 2,520 | 3 | 445 |
| `evals/` | 54 | 5,759 | 53 | 4,113 | 24 | 836 |
| (root files) | 17 | 4,736 | 19 | 4,592 | 16 | 2,015 |
| `secrets/` | 13 | 1,146 | 0 | 0 | 0 | 0 |
| `inventory/` | 5 | 1,134 | 0 | 0 | 0 | 0 |
| `cluster/` | 1 | 1,020 | 0 | 0 | 0 | 0 |
| `styles/` | 3 | 684 | 3 | 666 | 0 | 0 |
| `.vscode/` | 1 | 16 | 1 | 18 | 1 | 18 |
| `ansible/` | 0 | 0 | 1,857 | 317,005 | 925 | 99,491 |
| **Total** | 3,819 | 753,121 | 3,168 | 656,088 | 1,090 | 128,219 |

## Tests and instructions

A test file is a `test_*.py` anywhere in the tree. Instruction lines are the lines of every `CLAUDE.md` plus every file under `.claude/`. A hook is a Python module directly under `.claude/hooks/`; its tests are not counted.

| Measure | today | 30 days earlier | 90 days earlier |
|---|---:|---:|---:|
| Test files | 1,417 | 1,193 | 75 |
| Instruction lines (every `CLAUDE.md` plus `.claude/`) | 27,844 | 26,416 | 8,664 |
| Rules (`.claude/rules/*.md`) | 7 | 7 | 3 |
| Skills (`.claude/skills/*/SKILL.md`) | 13 | 13 | 10 |
| Agents (`.claude/agents/*.md`) | 8 | 8 | 7 |
| Hooks (`.claude/hooks/*.py`) | 17 | 16 | 8 |
