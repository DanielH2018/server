---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-02 17:42 UTC
generated_sha: 24c4bd31
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed |
|---|---|---|---|---|---|---|
| [#803](https://github.com/DanielH2018/server/issues/803) | low | addition | - | Add Navidrome | 2026-09-02 | 0 |
| [#822](https://github.com/DanielH2018/server/issues/822) | - | gap | backup-observability | The Kuma TCP port monitor crashes terraria, which pages k3s Workload Health | 2026-09-02 | 0 |
