---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-12 18:17 UTC
generated_sha: 71abb0266
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
| [#1469](https://github.com/DanielH2018/server/issues/1469) | low | gap | container | Restore the Valheim mods disabled by the 1.0 break | 2026-09-09 | 0 | - | ✓ |
| [#1787](https://github.com/DanielH2018/server/issues/1787) | low | gap | backup-observability | Two health crons push Kuma but log no status= line, so their DOWN periods reach no alerts episode | 2026-09-11 | 0 | - | ✓ |
| [#1789](https://github.com/DanielH2018/server/issues/1789) | low | gap | container | daniel-pi autoheal stops recurrently; only the cron's restart hides it | 2026-09-11 | 0 | - | ✓ |
| [#1790](https://github.com/DanielH2018/server/issues/1790) | low | gap | backup-observability | probe.py alerts --limit over Loki's 5000 cap crashes with a JSONDecodeError | 2026-09-11 | 0 | - | ✓ |
