---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-11-01 13:18 UTC
generated_sha: 25eb772c0e
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
| gitops-deploy | 2026-11-01T13:15:12+0000 | 4m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-11-01T12:03:05+0000 | 1h16m | 1d | ok | session completed |
| renovate-notify | 2026-11-01T13:02:47+0000 | 16m | 1d | ok | notified |
| docs-refresh | 2026-11-01T06:17:00+0000 | 7h2m | unknown | ok | generators: skipped |
| secret-rotate | 2026-10-30T12:34:47+0000 | 2d | unknown | ok | last touched by: Rotate Prowlarr's API key after probe.py arr printed it |
| longhorn-restore-drill | 2026-11-01T04:10:49+0000 | 9h8m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-10-26T10:20:03+0000 | 6d2h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1792982702.zip) |
