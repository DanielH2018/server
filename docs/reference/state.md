---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-11-28 18:17 UTC
generated_sha: 23c576cd4
---

!!! warning "Generated file — do not edit"
    This page is rendered from this repository by `scripts/docs/reference/state.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# State of the lab

5 of 5 loops within cadence.

!!! warning "Status is a heuristic over the last recorded state"
    `late` means the loop's last recorded run is more than 2x its expected cadence old. `unreadable` means this generator could not reach the loop's state at all (wrong host, permission, or unparseable content) -- not that the loop is unhealthy. `never` means the state is reachable and simply has no run recorded yet.

| Loop | Last run | Age | Cadence | Status | Last outcome |
|---|---|---|---|---|---|
| renovate-agent | 2026-11-28T12:07:54+0000 | 6h9m | 1d | ok | session completed |
| renovate-notify | 2026-11-28T13:00:15+0000 | 5h17m | 1d | ok | notified |
| docs-refresh | 2026-11-28T06:18:00+0000 | 11h59m | 12h | ok | generators: ok |
| longhorn-restore-drill | 2026-11-28T04:11:50+0000 | 14h5m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-11-23T10:20:04+0000 | 5d7h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1795392005.zip) |
