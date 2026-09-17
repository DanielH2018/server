---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-17 06:17 UTC
generated_sha: 72f7b5d07
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/state.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# State of the lab

6 of 7 loops within cadence.

!!! warning "Status is a heuristic over the last recorded state"
    `late` means the loop's last recorded run is more than 2x its expected cadence old. `unreadable` means this generator could not reach the loop's state at all (wrong host, permission, or unparseable content) -- not that the loop is unhealthy. `never` means the state is reachable and simply has no run recorded yet.

| Loop | Last run | Age | Cadence | Status | Last outcome |
|---|---|---|---|---|---|
| gitops-deploy | 2026-09-17T06:11:49+0000 | 5m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-09-12T12:04:20+0000 | 4d18h | 1d | late | session completed |
| renovate-notify | 2026-09-16T13:02:52+0000 | 17h14m | 1d | ok | notified |
| docs-refresh | 2026-09-16T18:17:00+0000 | 12h | 12h | ok | generators: ok |
| secret-rotate | 2026-09-16T21:52:41+0000 | 8h24m | 7d | ok | last touched by: chore(secrets): auto-rotate due auto-tier push tokens |
| longhorn-restore-drill | 2026-09-17T04:10:48+0000 | 2h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-09-14T10:20:02+0000 | 2d19h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1789353902.zip) |
