---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-10-27 18:17 UTC
generated_sha: 7d6f242f48
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
| gitops-deploy | 2026-10-27T18:14:38+0000 | 2m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-10-27T11:06:56+0000 | 7h10m | 1d | ok | session completed |
| renovate-notify | 2026-10-27T13:03:44+0000 | 5h13m | 1d | ok | notified |
| docs-refresh | 2026-10-27T06:17:00+0000 | 12h | unknown | ok | generators: skipped |
| secret-rotate | 2026-10-25T22:41:39+0000 | 1d19h | unknown | ok | last touched by: Rotate the Prowlarr API key |
| longhorn-restore-drill | 2026-10-27T04:10:49+0000 | 14h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-10-26T10:20:03+0000 | 1d7h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1792982702.zip) |
