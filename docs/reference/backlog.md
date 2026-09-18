---
generated_from: scripts/docs/reference/backlog.py
generated_at: 2026-09-18 18:17 UTC
generated_sha: 633b9be9a
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/backlog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Backlog

Findings Claude confirmed and did not fix in the session that found them, filed through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has been seen three times carries **escalated** (the filing plus two re-observations) and needs a durable owner: a test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A row marked in the Verify-by column carries a description of how to check it in its issue body — run `findings.py verify --all` to print them. That command reports and runs nothing; closing stays with `findings.py close`.

| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Claim | Verify-by |
|---|---|---|---|---|---|---|---|---|
| [#2013](https://github.com/DanielH2018/server/issues/2013) | medium | gap | backup-observability | Oversized Kuma push messages make Discord reject the whole DOWN alert with HTTP 400 | 2026-09-18 | 0 | - | ✓ |
| [#2015](https://github.com/DanielH2018/server/issues/2015) | medium | gap | security | pi-peer-backup's forced-command wrapper admits rsync -s (--secluded-args), which re-reads the path from the protocol stream | 2026-09-18 | 0 | - | ✓ |
| [#1469](https://github.com/DanielH2018/server/issues/1469) | low | gap | container | Restore the Valheim mods disabled by the 1.0 break | 2026-09-09 | 0 | - | ✓ |
| [#2003](https://github.com/DanielH2018/server/issues/2003) | low | improvement | container | dockerd and containerd take 46% of daniel-pi's major faults; GOGC=off needs their live heaps measured first | 2026-09-18 | 0 | worktree-pi-docker-metrics-2003 | ✓ |
| [#2004](https://github.com/DanielH2018/server/issues/2004) | low | improvement | container | Retire glances on daniel-pi: 66 MB of anon for facts node-exporter already exports | 2026-09-18 | 0 | - | ✓ |
| [#2005](https://github.com/DanielH2018/server/issues/2005) | low | improvement | container | Run node-exporter on daniel-pi as a host unit: the one container whose package form changes no decision | 2026-09-18 | 0 | - | ✓ |
| [#2016](https://github.com/DanielH2018/server/issues/2016) | low | gap | security | Six setup-plane template tasks render a Kuma push token into a host script without no_log | 2026-09-18 | 0 | - | ✓ |
| [#2017](https://github.com/DanielH2018/server/issues/2017) | low | improvement | container | sonarr's exportarr sidecar is CFS-throttled in ~48% of periods at the shared 100m limit; the comment justifies it from a 30-minute average | 2026-09-18 | 0 | - | ✓ |
| [#2021](https://github.com/DanielH2018/server/issues/2021) | low | improvement | cicd | Dead Docker-era stale_composes_alerted marker persists on daniel-box | 2026-09-18 | 0 | worktree-issue-fanout-2026-09-18 | ✓ |
| [#2022](https://github.com/DanielH2018/server/issues/2022) | low | gap | cicd | manual_plane k3s was cleared by hand without k3s-bringup --tags release-staleness running, and clear-manual-plane leaves no journal trace | 2026-09-18 | 0 | worktree-issue-fanout-2026-09-18 | ✓ |
