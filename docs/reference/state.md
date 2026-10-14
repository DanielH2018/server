---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-10-14 18:17 UTC
generated_sha: 00689f715
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
| gitops-deploy | 2026-10-14T18:16:48+0000 | 0m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-10-14T11:19:17+0000 | 6h58m | 1d | ok | session completed |
| renovate-notify | 2026-10-14T13:05:02+0000 | 5h12m | 1d | ok | notified |
| docs-refresh | 2026-10-14T06:18:00+0000 | 11h59m | 12h | ok | generators: ok |
| secret-rotate | 2026-10-13T19:32:58+0000 | 22h44m | 7d | ok | last touched by: Add google_assistant_project_id secret for R2 PR 5 |
| longhorn-restore-drill | 2026-10-14T04:10:47+0000 | 14h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-10-12T10:20:02+0000 | 2d7h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1791773103.zip) |
