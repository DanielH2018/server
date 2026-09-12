---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-12 06:17 UTC
generated_sha: fde7491c1
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
| gitops-deploy | 2026-09-12T06:05:18+0000 | 12m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-09-10T11:05:16+0000 | 1d19h | 1d | ok | session completed |
| renovate-notify | 2026-09-11T13:03:38+0000 | 17h13m | 1d | ok | checked, nothing new to notify |
| docs-refresh | 2026-09-11T18:17:00+0000 | 12h | 12h | ok | generators: ok |
| secret-rotate | 2026-09-11T18:12:54+0000 | 12h4m | 7d | ok | last touched by: Drill the full etcd restore monthly in a throwaway guest on daniel-server |
| longhorn-restore-drill | 2026-09-12T04:10:48+0000 | 2h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-09-07T10:20:02+0000 | 4d19h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1788749102.zip) |
