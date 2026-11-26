---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-11-26 18:17 UTC
generated_sha: 57382e988
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
| renovate-agent | 2026-11-25T12:05:32+0000 | 1d6h | 1d | ok | session completed |
| renovate-notify | 2026-11-26T13:01:25+0000 | 5h16m | 1d | ok | notified |
| docs-refresh | 2026-11-26T06:18:00+0000 | 11h59m | 12h | ok | generators: ok |
| longhorn-restore-drill | 2026-11-26T04:11:05+0000 | 14h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-11-23T10:20:04+0000 | 3d7h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1795392005.zip) |
