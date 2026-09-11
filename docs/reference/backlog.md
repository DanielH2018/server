---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-11 06:17 UTC
generated_sha: 37772d46c
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A row marked in the Verify-by column carries a description of how to check it in its issue body — run `findings.py verify --all` to print them. That command reports and runs nothing; closing stays with `findings.py close`.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Claim | Verify-by |
|---|---|---|---|---|---|---|---|---|
| [#1175](https://github.com/DanielH2018/server/issues/1175) | medium | gap | backup-observability | The full etcd restore has never been executed; the weekly drill is list-only | 2026-09-05 | 0 | - | - |
| [#1759](https://github.com/DanielH2018/server/issues/1759) | medium | gap | cicd | Empty-merge CI check cannot block an automerge that fires at PR open from a stale green | 2026-09-11 | 0 | worktree-renovate-stale-green-race | ✓ |
| [#1288](https://github.com/DanielH2018/server/issues/1288) | low | improvement | backup-observability | Re-derive CLAUDE_CGROUP_STALL_MAX_PCT from seven days of history | 2026-09-06 | 2 | - | - |
| [#1364](https://github.com/DanielH2018/server/issues/1364) | low | improvement | backup-observability | Longhorn fast-check cannot detect bit rot; measure the cost of snapshot-data-integrity=enabled | 2026-09-06 | 0 | - | ✓ |
| [#1469](https://github.com/DanielH2018/server/issues/1469) | low | gap | container | Restore Serverside_Simulations when it supports Valheim l-1.0.7 | 2026-09-09 | 0 | - | ✓ |
