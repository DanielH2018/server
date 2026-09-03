---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-03 12:23 UTC
generated_sha: 1806968e
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A row marked in the Verify-by column carries a read-only command in its issue body — run `findings.py verify --all` to re-check every one and close what now passes.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Verify-by |
|---|---|---|---|---|---|---|---|
| [#942](https://github.com/DanielH2018/server/issues/942) | medium | gap | backup-observability | valheim-config ships 4 GB of trimmed-away blocks in every B2 backup because seed and weekly snapshots pin them | 2026-09-03 | 0 | - |
