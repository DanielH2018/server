---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-21 06:17 UTC
generated_sha: a2cead23f
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
| gitops-deploy | 2026-09-21T06:09:29+0000 | 8m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-09-19T11:06:55+0000 | 1d19h | 1d | ok | session completed |
| renovate-notify | 2026-09-20T13:00:03+0000 | 17h17m | 1d | ok | notified |
| docs-refresh | 2026-09-20T18:17:00+0000 | 12h | 12h | ok | generators: ok |
| secret-rotate | 2026-09-19T13:10:45+0000 | 1d17h | 7d | ok | last touched by: Rotate monitor_discord_webhook_url after AutoKuma printed it into Loki |
| longhorn-restore-drill | 2026-09-21T04:11:18+0000 | 2h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-09-14T10:20:02+0000 | 6d19h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1789353902.zip) |
