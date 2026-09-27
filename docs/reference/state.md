---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-27 06:17 UTC
generated_sha: 0e5e82b9e
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
| gitops-deploy | 2026-09-27T06:13:49+0000 | 3m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-09-26T11:33:34+0000 | 18h43m | 1d | ok | session completed |
| renovate-notify | 2026-09-26T13:03:12+0000 | 17h14m | 1d | ok | notified |
| docs-refresh | 2026-09-26T18:18:00+0000 | 11h59m | 12h | ok | generators: ok |
| secret-rotate | 2026-09-25T23:53:13+0000 | 1d6h | 7d | ok | last touched by: Add an hourly render-record producer for the staleness reader, shipped disarmed until dry runs stop writing the host |
| longhorn-restore-drill | 2026-09-27T04:10:48+0000 | 2h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-09-21T10:20:03+0000 | 5d19h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1789958702.zip) |
