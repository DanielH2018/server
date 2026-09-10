---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-10 18:17 UTC
generated_sha: f95da3c99
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
| [#1390](https://github.com/DanielH2018/server/issues/1390) | medium | improvement | security | Give Headlamp OIDC login through Authelia (needs a k3s API-server change) | 2026-09-06 | 0 | worktree-issue-fanout-0910c | ✓ |
| [#1672](https://github.com/DanielH2018/server/issues/1672) | medium | gap | cicd | Every k8s service reads stale on volume-claim/claim.yml, and no deploy tag applies it | 2026-09-10 | 0 | - | - |
| [#1288](https://github.com/DanielH2018/server/issues/1288) | low | improvement | backup-observability | Re-derive CLAUDE_CGROUP_STALL_MAX_PCT from seven days of history | 2026-09-06 | 1 | - | - |
| [#1364](https://github.com/DanielH2018/server/issues/1364) | low | improvement | backup-observability | Longhorn fast-check cannot detect bit rot; measure the cost of snapshot-data-integrity=enabled | 2026-09-06 | 0 | worktree-issue-fanout-0910c | ✓ |
| [#1469](https://github.com/DanielH2018/server/issues/1469) | low | gap | container | Restore Serverside_Simulations when it supports Valheim l-1.0.7 | 2026-09-09 | 0 | - | ✓ |
| [#1630](https://github.com/DanielH2018/server/issues/1630) | low | gap | backup-observability | The UPS on-battery arm is outside the absence census, so losing that one series silently unmonitors mains loss | 2026-09-10 | 1 | - | ✓ |
| [#1640](https://github.com/DanielH2018/server/issues/1640) | low | gap | backup-observability | A Kuma push can exhaust its retries and leave a health cron with no verdict for an hour — *no vetted remediation* | 2026-09-10 | 0 | - | ✓ |
| [#1653](https://github.com/DanielH2018/server/issues/1653) | low | gap | docs | The fan-out signing gate and its exit code 6 are undocumented | 2026-09-10 | 0 | - | ✓ |
| [#1662](https://github.com/DanielH2018/server/issues/1662) | low | gap | cicd | The staging traefik route expectation asks for a status its IngressRoute cannot answer — *no vetted remediation* | 2026-09-10 | 0 | - | ✓ |
| [#1663](https://github.com/DanielH2018/server/issues/1663) | low | gap | backup-observability | probe.py health always gates the production cluster, so a staging deploy reads green about prod — *no vetted remediation* | 2026-09-10 | 0 | - | ✓ |
| [#1664](https://github.com/DanielH2018/server/issues/1664) | low | gap | container | Jellyfin Trakt plugin is installed but inert until the dashboard OAuth device authorisation is done | 2026-09-10 | 0 | - | ✓ |
| [#1668](https://github.com/DanielH2018/server/issues/1668) | low | gap | cicd | n8n's netpol-probe-job.yaml is pruned by k8s/manifests on every deploy | 2026-09-10 | 0 | - | ✓ |
| [#1669](https://github.com/DanielH2018/server/issues/1669) | low | gap | cicd | registry stages four job manifests inside the directory k8s/manifests prunes | 2026-09-10 | 0 | - | ✓ |
| [#1670](https://github.com/DanielH2018/server/issues/1670) | low | gap | cicd | Nothing stops a role claiming a reserved staging directory name like <service>-claims | 2026-09-10 | 0 | - | ✓ |
