---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-10 06:17 UTC
generated_sha: af3e4e492
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
| gitops-deploy | 2026-09-10T06:12:51+0000 | 4m | 10m | ok | ticked, no hold |
| renovate-agent | 2026-09-05T11:35:25+0000 | 4d18h | 1d | late | session completed |
| renovate-notify | 2026-09-10T03:31:53+0000 | 2h45m | 1d | ok | notified |
| docs-refresh | 2026-09-09T22:50:00+0000 | 7h27m | 12h | ok | generators: ok |
| secret-rotate | 2026-09-10T03:38:59+0000 | 2h38m | 7d | ok | last touched by: Rotate renovate_agent_kuma_push_token after it reached an agent transcript |
| longhorn-restore-drill | 2026-09-10T04:10:48+0000 | 2h6m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-09-07T10:20:02+0000 | 2d19h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1788749102.zip) |
