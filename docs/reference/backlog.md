---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-02 18:17 UTC
generated_sha: b49b867a
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed |
|---|---|---|---|---|---|---|
| [#862](https://github.com/DanielH2018/server/issues/862) | low | improvement | cicd | Code scanning runs under default setup, so every false positive costs a hand-dismissal that a refactor undoes | 2026-09-02 | 0 |
| [#866](https://github.com/DanielH2018/server/issues/866) | low | addition | - | Give navidrome a real music library and unpark it | 2026-09-02 | 0 |
