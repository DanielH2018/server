---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-10-03 06:17 UTC
generated_sha: 8d0a33d9a
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
| gitops-deploy | 2026-10-03T06:14:00+0000 | 3m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-10-02T11:14:37+0000 | 19h2m | 1d | ok | session completed |
| renovate-notify | 2026-10-02T13:00:46+0000 | 17h16m | 1d | ok | notified |
| docs-refresh | 2026-10-02T18:17:00+0000 | 12h | 12h | ok | generators: ok |
| secret-rotate | 2026-09-30T16:23:13+0000 | 2d13h | 7d | ok | last touched by: Alarm on the etcd drill's egress-fence verdict with a tile of its own |
| longhorn-restore-drill | 2026-10-03T04:11:08+0000 | 2h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-09-28T10:20:04+0000 | 4d19h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1790563503.zip) |
