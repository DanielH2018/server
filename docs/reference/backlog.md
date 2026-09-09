---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-09 22:50 UTC
generated_sha: 02af13398
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A row marked in the Verify-by column carries a description of how to check it in its issue body — run `findings.py verify --all` to print them. That command reports and runs nothing; closing stays with `findings.py close`.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Claim | Verify-by |
|---|---|---|---|---|---|---|---|---|
| [#1467](https://github.com/DanielH2018/server/issues/1467) | high | gap | cicd | GitOps tick completes without fast-forwarding a clean checkout 9 commits behind | 2026-09-09 | 0 | - | ✓ |
| [#1175](https://github.com/DanielH2018/server/issues/1175) | medium | gap | backup-observability | The full etcd restore has never been executed; the weekly drill is list-only | 2026-09-05 | 0 | - | - |
| [#1345](https://github.com/DanielH2018/server/issues/1345) | medium | gap | network | Traefik's new startupProbe has no live red-proof, and a container restart reuses the plugin emptyDir — *no vetted remediation* | 2026-09-06 | 0 | - | ✓ |
| [#1390](https://github.com/DanielH2018/server/issues/1390) | medium | improvement | security | Give Headlamp OIDC login through Authelia (needs a k3s API-server change) | 2026-09-06 | 0 | - | ✓ |
| [#1444](https://github.com/DanielH2018/server/issues/1444) | medium | addition | backup-observability | Nothing scrapes Pi-hole, so DNS query and block rates are invisible | 2026-09-09 | 0 | - | ✓ |
| [#1445](https://github.com/DanielH2018/server/issues/1445) | medium | addition | backup-observability | NUT exports no Prometheus metrics, so UPS state reaches Grafana only through Home Assistant | 2026-09-09 | 0 | - | ✓ |
| [#1450](https://github.com/DanielH2018/server/issues/1450) | medium | gap | security | Authelia's filesystem notifier makes password reset and WebAuthn enrolment un-deliverable | 2026-09-09 | 0 | - | ✓ |
| [#1457](https://github.com/DanielH2018/server/issues/1457) | medium | gap | container | Homepage's Jellyfin widget 404s on /emby/Sessions and renders API Error | 2026-09-09 | 0 | - | ✓ |
| [#1466](https://github.com/DanielH2018/server/issues/1466) | medium | gap | cicd | land.sh burns all three retries when master's merge rate outruns the tick-and-deploy cycle | 2026-09-09 | 0 | - | ✓ |
| [#1468](https://github.com/DanielH2018/server/issues/1468) | medium | gap | cicd | No validator renders setup-plane Jinja templates, so a broken one passes CI green | 2026-09-09 | 0 | - | ✓ |
| [#1470](https://github.com/DanielH2018/server/issues/1470) | medium | gap | backup-observability | No guard rejects a duplicate Grafana dashboard uid, which freezes all provisioning | 2026-09-09 | 0 | - | ✓ |
| [#1471](https://github.com/DanielH2018/server/issues/1471) | medium | gap | backup-observability | Nothing alerts on host temperature, CPU throttling, or the Pi undervoltage alarm | 2026-09-09 | 0 | - | ✓ |
| [#1473](https://github.com/DanielH2018/server/issues/1473) | medium | gap | cicd | Pending-check dwell clocks all restarted at zero after the marker fix | 2026-09-09 | 0 | - | ✓ |
| [#1476](https://github.com/DanielH2018/server/issues/1476) | medium | gap | backup-observability | coredns-host serves DNS with no metrics listener while its unit reads green | 2026-09-09 | 0 | - | ✓ |
| [#1477](https://github.com/DanielH2018/server/issues/1477) | medium | gap | cicd | A crashed renovate-agent run pushes nothing, so the monitor expires with no reason attached | 2026-09-09 | 1 | - | ✓ |
| [#1068](https://github.com/DanielH2018/server/issues/1068) | low | gap | cicd | k3s control-plane bumps need an operator-driven upgrade plan | 2026-09-04 | 0 | - | - |
| [#1288](https://github.com/DanielH2018/server/issues/1288) | low | improvement | backup-observability | Re-derive CLAUDE_CGROUP_STALL_MAX_PCT from seven days of history | 2026-09-06 | 0 | - | - |
| [#1332](https://github.com/DanielH2018/server/issues/1332) | low | improvement | cicd | hooks is the new CI pole at 109s, now that pytest is sharded | 2026-09-06 | 0 | - | - |
| [#1364](https://github.com/DanielH2018/server/issues/1364) | low | improvement | backup-observability | Longhorn fast-check cannot detect bit rot; measure the cost of snapshot-data-integrity=enabled | 2026-09-06 | 0 | - | ✓ |
| [#1413](https://github.com/DanielH2018/server/issues/1413) | low | gap | cicd | No regression guard on browser_close's storageState reload, which docs/claude-tooling.md now prescribes | 2026-09-06 | 0 | - | ✓ |
| [#1428](https://github.com/DanielH2018/server/issues/1428) | low | gap | container | homepage's ServiceAccount cannot list Ingresses, so its pod log carries a denial every few minutes | 2026-09-06 | 0 | - | ✓ |
| [#1429](https://github.com/DanielH2018/server/issues/1429) | low | improvement | cicd | deploy.sh exit 4 names the wrong repair when the cause is a parked GitOps tick | 2026-09-06 | 0 | - | - |
| [#1430](https://github.com/DanielH2018/server/issues/1430) | low | addition | backup-observability | No panel charts sonarr's episode quality mix | 2026-09-06 | 0 | - | ✓ |
| [#1431](https://github.com/DanielH2018/server/issues/1431) | low | gap | cicd | gh pr create is refused on its title text by the worktree-containment check — *no vetted remediation* | 2026-09-06 | 0 | - | ✓ |
| [#1435](https://github.com/DanielH2018/server/issues/1435) | low | gap | cicd | Automatic gc is disabled in the primary checkout, so worktree churn accumulates unreachable objects | 2026-09-06 | 0 | - | ✓ |
| [#1436](https://github.com/DanielH2018/server/issues/1436) | low | gap | cicd | docs-refresh commit failed on 'files were modified by this hook' with every test passing, and the failure log cannot name the file — *no vetted remediation* | 2026-09-06 | 0 | - | ✓ |
| [#1446](https://github.com/DanielH2018/server/issues/1446) | low | addition | backup-observability | Intel GPU transcode load is unmeasured | 2026-09-09 | 0 | - | ✓ |
| [#1447](https://github.com/DanielH2018/server/issues/1447) | low | gap | container | Bazarr's subtitle providers are PVC state the repo cannot describe | 2026-09-09 | 0 | - | ✓ |
| [#1448](https://github.com/DanielH2018/server/issues/1448) | low | addition | container | Jellyfin's proven init-container plugin pattern installs only two of the catalog's useful plugins | 2026-09-09 | 0 | - | ✓ |
| [#1449](https://github.com/DanielH2018/server/issues/1449) | low | improvement | container | n8n runs with community node packages disabled | 2026-09-09 | 0 | - | ✓ |
| [#1451](https://github.com/DanielH2018/server/issues/1451) | low | improvement | network | Pi-hole has no conditional forwarding, so LAN clients appear as bare IPs | 2026-09-09 | 0 | - | ✓ |
| [#1452](https://github.com/DanielH2018/server/issues/1452) | low | gap | container | autoheal restarts Pi containers without notifying anyone | 2026-09-09 | 0 | - | ✓ |
| [#1453](https://github.com/DanielH2018/server/issues/1453) | low | improvement | security | Retire or triage healthchecks_smtp_user after the SMTP rewiring | 2026-09-09 | 0 | - | - |
| [#1454](https://github.com/DanielH2018/server/issues/1454) | low | addition | home-assistant | Nothing can drive the cast Nest Hub dashboard from an automation | 2026-09-09 | 0 | - | ✓ |
| [#1459](https://github.com/DanielH2018/server/issues/1459) | low | improvement | container | Homepage logs an ingresses RBAC denial on every page load, burying real widget errors | 2026-09-09 | 0 | - | ✓ |
| [#1464](https://github.com/DanielH2018/server/issues/1464) | low | gap | cicd | daniel-stage never exercises Authelia's SMTP notifier branch | 2026-09-09 | 0 | - | - |
| [#1469](https://github.com/DanielH2018/server/issues/1469) | low | gap | container | Restore Serverside_Simulations when it supports Valheim l-1.0.7 | 2026-09-09 | 0 | - | ✓ |
| [#1478](https://github.com/DanielH2018/server/issues/1478) | low | improvement | container | Cap jellyfin's snapshotMaxSize so pre-apply snapshots cannot grow the backend unbounded | 2026-09-09 | 0 | - | ✓ |
| [#1483](https://github.com/DanielH2018/server/issues/1483) | low | gap | backup-observability | Speedtest records no rows, and the check cannot tell a stopped schedule from failing runs | 2026-09-09 | 0 | - | ✓ |
