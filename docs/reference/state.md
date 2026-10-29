---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-10-29 18:17 UTC
generated_sha: 7c54cb69d3
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
| gitops-deploy | 2026-10-29T18:17:50+0000 | 0m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-10-29T11:37:31+0000 | 6h40m | 1d | ok | session completed |
| renovate-notify | 2026-10-29T13:02:47+0000 | 5h15m | 1d | ok | notified |
| docs-refresh | 2026-10-29T06:18:00+0000 | 12h | unknown | ok | generators: ok |
| secret-rotate | 2026-10-29T14:07:49+0000 | 4h10m | unknown | ok | last touched by: Retire the three monitor-bridge push tokens a week after M5's cutover |
| longhorn-restore-drill | 2026-10-29T04:11:19+0000 | 14h7m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-10-26T10:20:03+0000 | 3d7h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1792982702.zip) |
