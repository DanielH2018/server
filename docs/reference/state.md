---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-10-05 18:17 UTC
generated_sha: 4fdc475b7
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
| gitops-deploy | 2026-10-05T18:08:53+0000 | 8m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-10-05T16:43:34+0000 | 1h34m | 1d | ok | session completed |
| renovate-notify | 2026-10-05T13:02:14+0000 | 5h15m | 1d | ok | notified |
| docs-refresh | 2026-10-05T06:17:00+0000 | 12h | 12h | ok | generators: ok |
| secret-rotate | 2026-10-05T17:13:28+0000 | 1h4m | 7d | ok | last touched by: Give the claude agent user its own GitHub identity, fenced to its branches |
| longhorn-restore-drill | 2026-10-05T04:10:38+0000 | 14h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-10-05T10:20:03+0000 | 7h57m | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1791168302.zip) |
