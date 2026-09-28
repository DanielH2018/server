---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-28 12:57 UTC
generated_sha: 022174825
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
| gitops-deploy | 2026-09-28T12:57:29+0000 | 0m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-09-28T11:23:11+0000 | 1h35m | 1d | ok | session completed |
| renovate-notify | 2026-09-27T13:02:22+0000 | 23h55m | 1d | ok | notified |
| docs-refresh | 2026-09-28T12:43:00+0000 | 15m | 12h | ok | generators: skipped |
| secret-rotate | 2026-09-27T20:09:47+0000 | 16h48m | 7d | ok | last touched by: Gate three prom readers and add a WAN gate so one outage pages once |
| longhorn-restore-drill | 2026-09-28T04:10:48+0000 | 8h47m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-09-28T10:20:04+0000 | 2h38m | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1790563503.zip) |
