---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-11-10 06:17 UTC
generated_sha: e27d5c427
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/state.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# State of the lab

5 of 6 loops within cadence.

!!! warning "Status is a heuristic over the last recorded state"
    `late` means the loop's last recorded run is more than 2x its expected cadence old. `unreadable` means this generator could not reach the loop's state at all (wrong host, permission, or unparseable content) -- not that the loop is unhealthy. `never` means the state is reachable and simply has no run recorded yet.

| Loop | Last run | Age | Cadence | Status | Last outcome |
|---|---|---|---|---|---|
| gitops-deploy | 2026-11-09T17:49:44+0000 | 12h27m | 10m | late | ticked, no hold |
| renovate-agent | 2026-11-09T12:11:07+0000 | 18h6m | 1d | ok | session completed |
| renovate-notify | 2026-11-09T13:02:53+0000 | 17h14m | 1d | ok | notified |
| docs-refresh | 2026-11-09T18:18:00+0000 | 11h59m | 12h | ok | generators: ok |
| longhorn-restore-drill | 2026-11-10T04:10:49+0000 | 2h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-11-09T10:20:02+0000 | 19h57m | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1794182402.zip) |
