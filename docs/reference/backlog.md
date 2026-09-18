---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-18 12:39 UTC
generated_sha: c49d5c4a4
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A row marked in the Verify-by column carries a description of how to check it in its issue body — run `findings.py verify --all` to print them. That command reports and runs nothing; closing stays with `findings.py close`.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Claim | Verify-by |
|---|---|---|---|---|---|---|---|---|
| [#1873](https://github.com/DanielH2018/server/issues/1873) | medium | improvement | cicd | Drift-enforcement audit 2026-09-17: tracking issue for #1851-#1860 — *no vetted remediation* | 2026-09-17 | 0 | - | ✓ |
| [#1962](https://github.com/DanielH2018/server/issues/1962) | medium | gap | cicd | GitOps broad-plane narrowing applies denied roles reached through a shared inventory key | 2026-09-18 | 0 | - | ✓ |
| [#1469](https://github.com/DanielH2018/server/issues/1469) | low | gap | container | Restore the Valheim mods disabled by the 1.0 break | 2026-09-09 | 0 | - | ✓ |
| [#1898](https://github.com/DanielH2018/server/issues/1898) | low | improvement | cicd | claude-guard slice 5, full: consolidate the Bash hooks around the package (policy decision first) — *no vetted remediation* | 2026-09-17 | 0 | worktree-bridge-cse_01RiSmz1nPEE4wQnfjctzH1T | ✓ |
| [#1963](https://github.com/DanielH2018/server/issues/1963) | low | gap | cicd | renovate_agent still lands the crowdsec bouncer plugin bump, a traefik redeploy, unattended | 2026-09-18 | 0 | - | ✓ |
| [#1967](https://github.com/DanielH2018/server/issues/1967) | low | improvement | container | alloy on daniel-pi takes a third of the host's major faults: global reclaim swaps its heap out and every GC cycle faults it back | 2026-09-18 | 0 | worktree-alloy-gogc-1967 | ✓ |
| [#1974](https://github.com/DanielH2018/server/issues/1974) | low | gap | network | Origin-direct requests reach the public Host rules without Cloudflare and share one rate-limit bucket | 2026-09-18 | 0 | - | ✓ |
| [#1976](https://github.com/DanielH2018/server/issues/1976) | low | improvement | backup-observability | Swallowed-verdicts pod selector keys on the generic container name `pull`, pinned against one template only | 2026-09-18 | 0 | - | ✓ |
