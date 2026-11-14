---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-11-14 12:28 UTC
generated_sha: 4fa943fb7
---

!!! warning "Generated file — do not edit"
    This page is rendered from this repository by `scripts/docs/reference/state.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# State of the lab

4 of 5 loops within cadence.

!!! warning "Status is a heuristic over the last recorded state"
    `late` means the loop's last recorded run is more than 2x its expected cadence old. `unreadable` means this generator could not reach the loop's state at all (wrong host, permission, or unparseable content) -- not that the loop is unhealthy. `never` means the state is reachable and simply has no run recorded yet.

| Loop | Last run | Age | Cadence | Status | Last outcome |
|---|---|---|---|---|---|
| renovate-agent | 2026-11-12T12:01:54+0000 | 2d | 1d | late | session completed |
| renovate-notify | 2026-11-13T13:01:55+0000 | 23h26m | 1d | ok | notified |
| docs-refresh | 2026-11-14T06:18:00+0000 | 6h10m | 12h | ok | generators: ok |
| longhorn-restore-drill | 2026-11-14T04:10:39+0000 | 8h18m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-11-09T10:20:02+0000 | 5d2h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1794182402.zip) |
