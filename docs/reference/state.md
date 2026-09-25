---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-25 18:17 UTC
generated_sha: 77c9f0418
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
| gitops-deploy | 2026-09-25T18:12:54+0000 | 4m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-09-25T11:08:03+0000 | 7h9m | 1d | ok | session completed |
| renovate-notify | 2026-09-25T13:57:17+0000 | 4h20m | 1d | ok | notified |
| docs-refresh | 2026-09-25T06:18:00+0000 | 11h59m | 12h | ok | generators: ok |
| secret-rotate | 2026-09-25T16:42:39+0000 | 1h34m | 7d | ok | last touched by: Page when the Healthchecks.io console drifts from the deadman doc |
| longhorn-restore-drill | 2026-09-25T04:10:38+0000 | 14h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-09-21T10:20:03+0000 | 4d7h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1789958702.zip) |
