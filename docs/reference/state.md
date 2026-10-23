---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-10-23 06:17 UTC
generated_sha: 1d8f84182
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
| gitops-deploy | 2026-10-23T06:09:06+0000 | 8m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-10-22T11:05:03+0000 | 19h12m | 1d | ok | session completed |
| renovate-notify | 2026-10-22T13:03:27+0000 | 17h14m | 1d | ok | notified |
| docs-refresh | 2026-10-22T18:17:00+0000 | 12h | 12h | ok | generators: skipped |
| secret-rotate | 2026-10-17T13:41:40+0000 | 5d16h | 7d | ok | last touched by: Switch on the agent's triage unit on daniel-box |
| longhorn-restore-drill | 2026-10-23T04:10:38+0000 | 2h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-10-19T10:20:03+0000 | 3d19h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1792377903.zip) |
