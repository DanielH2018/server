---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-10 18:17 UTC
generated_sha: f95da3c99
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
| gitops-deploy | 2026-09-10T18:17:37+0000 | 0m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-09-10T11:05:16+0000 | 7h12m | 1d | ok | session completed |
| renovate-notify | 2026-09-10T13:02:28+0000 | 5h15m | 1d | ok | notified |
| docs-refresh | 2026-09-10T06:17:00+0000 | 12h1m | 12h | ok | generators: ok |
| secret-rotate | 2026-09-10T17:56:14+0000 | 21m | 7d | ok | last touched by: Arm the Longhorn snapshot-headroom monitor |
| longhorn-restore-drill | 2026-09-10T04:10:48+0000 | 14h7m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-09-07T10:20:02+0000 | 3d7h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1788749102.zip) |
