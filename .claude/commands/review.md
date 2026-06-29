---
description: Lightweight local review of unpushed changes (committed-but-unpushed + uncommitted) before pushing
---
## Changes to Review

Committed but not yet pushed:

!`git --no-pager log origin/master..HEAD --oneline`

Files changed vs the remote:

!`git --no-pager diff --name-only origin/master...HEAD`

Uncommitted working-tree changes (if any):

!`git --no-pager diff --stat`

## Detailed Diff (committed-but-unpushed)

!`git --no-pager diff origin/master...HEAD`

## Instructions

Review the above changes for:
1. Code quality / convention adherence (see CLAUDE.md + `.claude/rules/`)
2. Security (exposed secrets, unsafe permissions, container hardening)
3. Missing test coverage (`uv run pytest`) or template validation
4. Correctness / idempotency for Ansible changes

Give specific, actionable feedback per file. This is the fast local pass; for a deep
multi-agent audit use the `homelab-review` skill, and for a cloud review use
`/code-review ultra`.
