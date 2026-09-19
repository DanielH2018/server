---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-19 18:17 UTC
generated_sha: 8cf1ba83c
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/state.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# State of the lab

7 of 7 loops within cadence.

!!! warning "Status is a heuristic over the last recorded state"
    `late` means the loop's last recorded run is more than 2x its expected cadence old. `unreadable` means this generator could not reach the loop's state at all (wrong host, permission, or unparseable content) -- not that the loop is unhealthy. `never` means the state is reachable and simply has no run recorded yet.

| Loop | Last run | Age | Cadence | Status | Last outcome |
|---|---|---|---|---|---|
| gitops-deploy | 2026-09-19T18:12:32+0000 | 5m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-09-19T11:06:55+0000 | 7h10m | 1d | ok | session completed |
| renovate-notify | 2026-09-19T13:00:22+0000 | 5h17m | 1d | ok | notified |
| docs-refresh | 2026-09-19T06:17:00+0000 | 12h | 12h | ok | generators: ok |
| secret-rotate | 2026-09-19T13:10:45+0000 | 5h6m | 7d | ok | last touched by: Rotate monitor_discord_webhook_url after AutoKuma printed it into Loki |
| longhorn-restore-drill | 2026-09-19T04:10:47+0000 | 14h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-09-14T10:20:02+0000 | 5d7h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1789353902.zip) |
