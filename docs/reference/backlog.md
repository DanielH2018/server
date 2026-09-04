---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-04 06:17 UTC
generated_sha: 62c488dd
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A row marked in the Verify-by column carries a read-only command in its issue body — run `findings.py verify --all` to re-check every one and close what now passes.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Verify-by |
|---|---|---|---|---|---|---|---|
| [#1003](https://github.com/DanielH2018/server/issues/1003) | low | gap | backup-observability | k10temp Tctl vs real junction temp on daniel-box stays unresolved (no Tdie/Tccd exported) — *no vetted remediation* | 2026-09-03 | 0 | - |
| [#1052](https://github.com/DanielH2018/server/issues/1052) | low | gap | cicd | Backup-health shim test writes fixture verdicts into the host syslog | 2026-09-04 | 0 | - |
