---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-18 12:39 UTC
generated_sha: c49d5c4a4
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
| gitops-deploy | 2026-09-18T12:39:11+0000 | 0m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-09-12T12:04:20+0000 | 6d | 1d | late | session completed |
| renovate-notify | 2026-09-17T13:01:19+0000 | 23h38m | 1d | ok | notified |
| docs-refresh | 2026-09-18T12:33:00+0000 | 7m | 12h | ok | generators: ok |
| secret-rotate | 2026-09-18T03:03:34+0000 | 9h36m | 7d | ok | last touched by: Wire registry-gc's Kuma tile and make the two library-bypassing pushers log a line the swallowed-verdicts reader parses |
| longhorn-restore-drill | 2026-09-18T04:10:38+0000 | 8h29m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-09-14T10:20:02+0000 | 4d2h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1789353902.zip) |
