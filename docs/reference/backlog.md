---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-17 06:17 UTC
generated_sha: 72f7b5d07
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A row marked in the Verify-by column carries a description of how to check it in its issue body — run `findings.py verify --all` to print them. That command reports and runs nothing; closing stays with `findings.py close`.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Claim | Verify-by |
|---|---|---|---|---|---|---|---|---|
| [#1778](https://github.com/DanielH2018/server/issues/1778) | high | gap | backup-observability | etcd Restore Drill full has failed on every run since it shipped | 2026-09-11 | 0 | - | ✓ |
| [#1813](https://github.com/DanielH2018/server/issues/1813) | high | gap | cicd | A comma in a multi-tag deploy.sh snapshot path makes Ansible parse no inventory, so the run deploys nothing and exits 0 | 2026-09-12 | 0 | - | - |
| [#1814](https://github.com/DanielH2018/server/issues/1814) | high | gap | cicd | A deploy.sh run that matches zero hosts exits 0 and land.sh calls it settled, because the health gate passes on the old pods | 2026-09-12 | 0 | - | - |
| [#1779](https://github.com/DanielH2018/server/issues/1779) | medium | gap | backup-observability | A Kuma pod replacement blinds the long-interval push monitors for up to 25h | 2026-09-11 | 0 | - | ✓ |
| [#1781](https://github.com/DanielH2018/server/issues/1781) | medium | improvement | backup-observability | Six drift push monitors have about an hour of margin over their producing cron | 2026-09-11 | 0 | - | ✓ |
| [#1793](https://github.com/DanielH2018/server/issues/1793) | medium | gap | backup-observability | Alert History board still extracts the check name from a timestamp prefix monitor-bridge no longer prints | 2026-09-11 | 0 | - | ✓ |
| [#1798](https://github.com/DanielH2018/server/issues/1798) | medium | gap | cicd | issue-fanout groups by Ansible role, so two agents edited the same shared script | 2026-09-11 | 0 | - | ✓ |
| [#1799](https://github.com/DanielH2018/server/issues/1799) | medium | gap | cicd | A test calling main() with no argv reads pytest's flags, and nothing guards the class | 2026-09-11 | 0 | - | ✓ |
| [#1801](https://github.com/DanielH2018/server/issues/1801) | medium | gap | cicd | postflight tests fail under -n0, so pytest_shard.py --record cannot run | 2026-09-11 | 0 | - | ✓ |
| [#1802](https://github.com/DanielH2018/server/issues/1802) | medium | gap | backup-observability | A zero-available Deployment can sit 15 minutes inside the new replica grace | 2026-09-11 | 0 | - | ✓ |
| [#1803](https://github.com/DanielH2018/server/issues/1803) | medium | gap | backup-observability | A push monitor rejecting its token (HTTP 404) turns healthy checks into false DOWNs, unwatched | 2026-09-11 | 0 | - | ✓ |
| [#1804](https://github.com/DanielH2018/server/issues/1804) | medium | gap | backup-observability | Unexplained whole-estate iSCSI session reset on daniel-box, 2026-09-09 20:12 | 2026-09-11 | 0 | - | ✓ |
| [#1838](https://github.com/DanielH2018/server/issues/1838) | medium | gap | backup-observability | qbittorrent VPN kill-switch netns lockout went unactioned for 3.5 days despite a red Kuma monitor | 2026-09-16 | 0 | - | ✓ |
| [#1843](https://github.com/DanielH2018/server/issues/1843) | medium | gap | cicd | land.sh's tick kick returns 0 after joining a pre-merge tick, so nothing converges the primary until the timer | 2026-09-17 | 0 | - | - |
| [#1846](https://github.com/DanielH2018/server/issues/1846) | medium | gap | cicd | Park thresholds were sized for a landing that waited on the primary; after #1810 they are the only wedged-primary detector | 2026-09-17 | 0 | - | - |
| [#1847](https://github.com/DanielH2018/server/issues/1847) | medium | gap | cicd | A ServiceLockBusy defer in the deployer leaves no durable signal, so a wedged operator deploy parks the tick silently | 2026-09-17 | 0 | - | - |
| [#1851](https://github.com/DanielH2018/server/issues/1851) | medium | gap | container | Guard priorityClassName adoption on every pod template; claude-otel plane has none — *no vetted remediation* | 2026-09-17 | 0 | - | ✓ |
| [#1852](https://github.com/DanielH2018/server/issues/1852) | medium | gap | security | Guard automountServiceAccountToken: false on SA-less pods; valheim and valheim-stats mount a token — *no vetted remediation* | 2026-09-17 | 0 | - | ✓ |
| [#1853](https://github.com/DanielH2018/server/issues/1853) | medium | gap | cicd | Guard that every role shipping files/*.py has a tests/ dir in testpaths; karakeep-time-tagger.py has none — *no vetted remediation* | 2026-09-17 | 0 | - | ✓ |
| [#1854](https://github.com/DanielH2018/server/issues/1854) | medium | gap | docs | Setup-role CLAUDE.md files lack the Autonomous-role contract the root CLAUDE.md routes to; k3s has no CLAUDE.md — *no vetted remediation* | 2026-09-17 | 0 | - | ✓ |
| [#1469](https://github.com/DanielH2018/server/issues/1469) | low | gap | container | Restore the Valheim mods disabled by the 1.0 break | 2026-09-09 | 0 | - | ✓ |
| [#1787](https://github.com/DanielH2018/server/issues/1787) | low | gap | backup-observability | Two health crons push Kuma but log no status= line, so their DOWN periods reach no alerts episode | 2026-09-11 | 0 | - | ✓ |
| [#1789](https://github.com/DanielH2018/server/issues/1789) | low | gap | container | daniel-pi autoheal stops recurrently; only the cron's restart hides it | 2026-09-11 | 0 | - | ✓ |
| [#1790](https://github.com/DanielH2018/server/issues/1790) | low | gap | backup-observability | probe.py alerts --limit over Loki's 5000 cap crashes with a JSONDecodeError | 2026-09-11 | 0 | - | ✓ |
| [#1839](https://github.com/DanielH2018/server/issues/1839) | low | gap | cicd | land.sh's per-host split reads containers_list from the primary, not the merge SHA it deploys | 2026-09-16 | 0 | - | ✓ |
| [#1844](https://github.com/DanielH2018/server/issues/1844) | low | gap | cicd | deploy_ui reads the tree lock as deploy-in-progress, which ADR-0017 made false for most of a deploy | 2026-09-17 | 0 | - | - |
| [#1845](https://github.com/DanielH2018/server/issues/1845) | low | gap | cicd | Two operations inside deploy.sh's tree-lock hold are unbounded in code | 2026-09-17 | 0 | - | - |
| [#1848](https://github.com/DanielH2018/server/issues/1848) | low | gap | cicd | _defines_only's comment rule is unsafe for a block scalar whose continuation line starts with # | 2026-09-17 | 0 | - | - |
| [#1855](https://github.com/DanielH2018/server/issues/1855) | low | gap | docs | Pin TimeoutStartSec prose to the unit value; six copies still say 25 or 45 min against 60min — *no vetted remediation* | 2026-09-17 | 0 | - | ✓ |
| [#1856](https://github.com/DanielH2018/server/issues/1856) | low | gap | cicd | Validator lets lookup('template') app config sit at templates/ top level; homepage and crowdsec do — *no vetted remediation* | 2026-09-17 | 0 | - | ✓ |
| [#1857](https://github.com/DanielH2018/server/issues/1857) | low | gap | docs | Resolve DECIDED marker pointers; setup/k3s markers cite a CLAUDE.md that does not exist — *no vetted remediation* | 2026-09-17 | 0 | - | ✓ |
| [#1858](https://github.com/DanielH2018/server/issues/1858) | low | gap | container | Widen enableServiceLinks guard from deployment.yaml.j2 to every pod template; dri-device-plugin lacks it — *no vetted remediation* | 2026-09-17 | 0 | - | ✓ |
| [#1859](https://github.com/DanielH2018/server/issues/1859) | low | improvement | docs | Mark ten already-enforced rules ENFORCED (3 FULL, 5 SCOPED memory entries + 2 CLAUDE.md lines) — *no vetted remediation* | 2026-09-17 | 0 | - | - |
| [#1860](https://github.com/DanielH2018/server/issues/1860) | low | improvement | cicd | Fifteen held-today conventions with no regression guard (drift audit 2026-09-17 Tier 2) — *no vetted remediation* | 2026-09-17 | 0 | - | ✓ |
| [#1863](https://github.com/DanielH2018/server/issues/1863) | low | improvement | cicd | CI runs 58 ssh-dependent auto-approve vectors against the claude_guard stand-in and shows nothing | 2026-09-17 | 0 | - | ✓ |
| [#1864](https://github.com/DanielH2018/server/issues/1864) | low | improvement | cicd | Two PermissionRequest hooks judge every ssh command since the claude_guard cutover — *no vetted remediation* | 2026-09-17 | 0 | - | ✓ |
