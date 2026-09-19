---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-19 06:17 UTC
generated_sha: f9b05e4ae
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A row marked in the Verify-by column carries a description of how to check it in its issue body — run `findings.py verify --all` to print them. That command reports and runs nothing; closing stays with `findings.py close`.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Claim | Verify-by |
|---|---|---|---|---|---|---|---|---|
| [#2077](https://github.com/DanielH2018/server/issues/2077) | high | gap | security | Rotate monitor_discord_webhook_url: AutoKuma printed it into Loki on 2026-09-18 | 2026-09-18 | 0 | - | - |
| [#1469](https://github.com/DanielH2018/server/issues/1469) | low | gap | container | Restore the Valheim mods disabled by the 1.0 break | 2026-09-09 | 0 | - | ✓ |
| [#2078](https://github.com/DanielH2018/server/issues/2078) | low | improvement | cicd | Export READONLY_BASE from claude_guard and move the dmesg guard under the shared-verdict replay | 2026-09-18 | 0 | - | ✓ |
| [#2081](https://github.com/DanielH2018/server/issues/2081) | low | improvement | container | Apply the dockerd/containerd GOGC=off drop-ins on daniel-pi one a day and grade each the day after | 2026-09-18 | 0 | - | ✓ |
