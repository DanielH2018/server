---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-10-05 06:17 UTC
generated_sha: cd4d51240
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
| gitops-deploy | 2026-10-05T06:14:53+0000 | 2m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-10-03T11:11:12+0000 | 1d19h | 1d | ok | session completed |
| renovate-notify | 2026-10-04T21:20:53+0000 | 8h56m | 1d | ok | notified |
| docs-refresh | 2026-10-04T18:26:00+0000 | 11h51m | 12h | ok | generators: ok |
| secret-rotate | 2026-10-04T21:15:00+0000 | 9h2m | 7d | ok | last touched by: Rotate the Discord alert webhook |
| longhorn-restore-drill | 2026-10-05T04:10:38+0000 | 2h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-09-28T10:20:04+0000 | 6d19h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1790563503.zip) |
