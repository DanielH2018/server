---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-10 06:17 UTC
generated_sha: af3e4e492
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
| [#1345](https://github.com/DanielH2018/server/issues/1345) | medium | gap | network | Traefik's new startupProbe has no live red-proof, and a container restart reuses the plugin emptyDir — *no vetted remediation* | 2026-09-06 | 0 | - | ✓ |
| [#1390](https://github.com/DanielH2018/server/issues/1390) | medium | improvement | security | Give Headlamp OIDC login through Authelia (needs a k3s API-server change) | 2026-09-06 | 0 | - | ✓ |
| [#1470](https://github.com/DanielH2018/server/issues/1470) | medium | gap | backup-observability | No guard rejects a duplicate Grafana dashboard uid, which freezes all provisioning | 2026-09-09 | 0 | worktree-bridge-cse_01Gv7DxKcLEt3S37oHhrKuzG | ✓ |
| [#1489](https://github.com/DanielH2018/server/issues/1489) | medium | gap | security | The systemctl cat guard names systemctl show -p as safe, but Exec properties print secrets in argv | 2026-09-09 | 0 | worktree-bridge-cse_01Gv7DxKcLEt3S37oHhrKuzG | ✓ |
| [#1491](https://github.com/DanielH2018/server/issues/1491) | medium | gap | security | healthchecks SECRET_KEY lives only in a 2023 file on the PVC, outside SOPS and rotation | 2026-09-09 | 0 | worktree-bridge-cse_01Gv7DxKcLEt3S37oHhrKuzG | - |
| [#1527](https://github.com/DanielH2018/server/issues/1527) | medium | improvement | cicd | Renovate PR bodies can disagree with their branch content; triage from the merge base, not the PR age | 2026-09-10 | 0 | - | ✓ |
| [#1548](https://github.com/DanielH2018/server/issues/1548) | medium | improvement | backup-observability | The UPS alert path still runs through Home Assistant, so the new NUT series feed nothing | 2026-09-10 | 0 | - | ✓ |
| [#1562](https://github.com/DanielH2018/server/issues/1562) | medium | gap | backup-observability | postflight's Uptime-Kuma drift check errors on a moved probe constant | 2026-09-10 | 0 | - | ✓ |
| [#1564](https://github.com/DanielH2018/server/issues/1564) | medium | gap | backup-observability | postflight reports Authelia down when its pod is on the other node | 2026-09-10 | 0 | - | ✓ |
| [#1068](https://github.com/DanielH2018/server/issues/1068) | low | gap | cicd | k3s control-plane bumps need an operator-driven upgrade plan | 2026-09-04 | 0 | - | - |
| [#1288](https://github.com/DanielH2018/server/issues/1288) | low | improvement | backup-observability | Re-derive CLAUDE_CGROUP_STALL_MAX_PCT from seven days of history | 2026-09-06 | 0 | - | - |
| [#1332](https://github.com/DanielH2018/server/issues/1332) | low | improvement | cicd | hooks is the new CI pole at 109s, now that pytest is sharded | 2026-09-06 | 0 | worktree-fanout-placement-slice4 | - |
| [#1364](https://github.com/DanielH2018/server/issues/1364) | low | improvement | backup-observability | Longhorn fast-check cannot detect bit rot; measure the cost of snapshot-data-integrity=enabled | 2026-09-06 | 0 | - | ✓ |
| [#1413](https://github.com/DanielH2018/server/issues/1413) | low | gap | cicd | No regression guard on browser_close's storageState reload, which docs/claude-tooling.md now prescribes | 2026-09-06 | 0 | - | ✓ |
| [#1431](https://github.com/DanielH2018/server/issues/1431) | low | gap | cicd | gh pr create is refused on its title text by the worktree-containment check — *no vetted remediation* | 2026-09-06 | 0 | - | ✓ |
| [#1435](https://github.com/DanielH2018/server/issues/1435) | low | gap | cicd | Automatic gc is disabled in the primary checkout, so worktree churn accumulates unreachable objects | 2026-09-06 | 0 | - | ✓ |
| [#1436](https://github.com/DanielH2018/server/issues/1436) | low | gap | cicd | docs-refresh commit failed on 'files were modified by this hook' with every test passing, and the failure log cannot name the file — *no vetted remediation* | 2026-09-06 | 0 | - | ✓ |
| [#1446](https://github.com/DanielH2018/server/issues/1446) | low | addition | backup-observability | Intel GPU transcode load is unmeasured | 2026-09-09 | 0 | - | ✓ |
| [#1449](https://github.com/DanielH2018/server/issues/1449) | low | improvement | container | n8n runs with community node packages disabled | 2026-09-09 | 0 | - | ✓ |
| [#1451](https://github.com/DanielH2018/server/issues/1451) | low | improvement | network | Pi-hole has no conditional forwarding, so LAN clients appear as bare IPs | 2026-09-09 | 0 | - | ✓ |
| [#1452](https://github.com/DanielH2018/server/issues/1452) | low | gap | container | autoheal restarts Pi containers without notifying anyone | 2026-09-09 | 0 | - | ✓ |
| [#1453](https://github.com/DanielH2018/server/issues/1453) | low | improvement | security | Retire or triage healthchecks_smtp_user after the SMTP rewiring | 2026-09-09 | 0 | worktree-bridge-cse_01Gv7DxKcLEt3S37oHhrKuzG | - |
| [#1454](https://github.com/DanielH2018/server/issues/1454) | low | addition | home-assistant | Nothing can drive the cast Nest Hub dashboard from an automation | 2026-09-09 | 0 | - | ✓ |
| [#1464](https://github.com/DanielH2018/server/issues/1464) | low | gap | cicd | daniel-stage never exercises Authelia's SMTP notifier branch | 2026-09-09 | 0 | - | - |
| [#1469](https://github.com/DanielH2018/server/issues/1469) | low | gap | container | Restore Serverside_Simulations when it supports Valheim l-1.0.7 | 2026-09-09 | 0 | - | ✓ |
| [#1483](https://github.com/DanielH2018/server/issues/1483) | low | gap | backup-observability | Speedtest records no rows, and the check cannot tell a stopped schedule from failing runs | 2026-09-09 | 0 | - | ✓ |
| [#1502](https://github.com/DanielH2018/server/issues/1502) | low | addition | security | Authelia offers no WebAuthn second factor now that the notifier can deliver its enrolment confirmation — *no vetted remediation* | 2026-09-10 | 0 | - | ✓ |
| [#1505](https://github.com/DanielH2018/server/issues/1505) | low | gap | cicd | run_all.py registers five of the seven prek validators | 2026-09-10 | 0 | - | ✓ |
| [#1524](https://github.com/DanielH2018/server/issues/1524) | low | gap | cicd | A channel-tag-plus-digest _image: pin can be downgraded on the auto-deploy path with nothing reading the version — *no vetted remediation* | 2026-09-10 | 0 | - | ✓ |
| [#1547](https://github.com/DanielH2018/server/issues/1547) | low | gap | backup-observability | check_host_temp's arms are tested but its propagation of their verdicts is not | 2026-09-10 | 0 | - | ✓ |
| [#1557](https://github.com/DanielH2018/server/issues/1557) | low | gap | cicd | Jellyfin's Webhook plugin pin has no Renovate manager | 2026-09-10 | 0 | - | ✓ |
| [#1559](https://github.com/DanielH2018/server/issues/1559) | low | improvement | container | Jellyfin Merge Versions and Trakt plugins still unadded after the Webhook install landed | 2026-09-10 | 0 | - | ✓ |
| [#1560](https://github.com/DanielH2018/server/issues/1560) | low | gap | backup-observability | A reached Longhorn snapshotMaxSize latches: volume-snapshot snapshots before it prunes | 2026-09-10 | 0 | - | ✓ |
| [#1565](https://github.com/DanielH2018/server/issues/1565) | low | improvement | cicd | Stale renovate/* branches linger with no PR and no dashboard entry — *no vetted remediation* | 2026-09-10 | 0 | - | ✓ |
| [#1566](https://github.com/DanielH2018/server/issues/1566) | low | gap | cicd | deploy.sh validates tags before it checks staleness, so a new role reads as a tag miss | 2026-09-10 | 0 | - | ✓ |
| [#1569](https://github.com/DanielH2018/server/issues/1569) | low | gap | container | Jellyfin loads an unmanaged Media Cleaner plugin the repo never installs | 2026-09-10 | 0 | - | ✓ |
