---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-10-15 18:17 UTC
generated_sha: 2e472a3c8
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
| gitops-deploy | 2026-10-15T18:12:11+0000 | 5m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-10-15T11:14:56+0000 | 7h2m | 1d | ok | session completed |
| renovate-notify | 2026-10-15T13:02:48+0000 | 5h14m | 1d | ok | notified |
| docs-refresh | 2026-10-15T06:18:00+0000 | 11h59m | 12h | ok | generators: ok |
| secret-rotate | 2026-10-15T17:58:42+0000 | 18m | 7d | ok | last touched by: Fall back to WiFi on both k3s nodes when their ethernet link loses carrier |
| longhorn-restore-drill | 2026-10-15T04:10:48+0000 | 14h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-10-12T10:20:02+0000 | 3d7h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1791773103.zip) |
