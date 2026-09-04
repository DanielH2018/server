---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-04 20:39 UTC
generated_sha: 9b71a483
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
| gitops-deploy | 2026-09-04T20:32:05+0000 | 8m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-09-04T12:22:54+0000 | 8h17m | 1d | ok | session completed |
| renovate-notify | 2026-09-04T18:33:26+0000 | 2h7m | 1d | ok | notified |
| docs-refresh | 2026-09-04T20:39:00+0000 | 1m | 12h | ok | generators: ok |
| secret-rotate | 2026-09-04T13:47:42+0000 | 6h52m | 7d | ok | last touched by: Remove the last two CodeQL false-positive shapes at their source |
| longhorn-restore-drill | 2026-09-04T04:10:47+0000 | 16h29m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-08-31T10:20:03+0000 | 4d10h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1788144302.zip) |
