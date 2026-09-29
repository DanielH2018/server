---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-29 01:30 UTC
generated_sha: fea456d31
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/state.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# State of the lab

3 of 7 loops within cadence.

!!! warning "Status is a heuristic over the last recorded state"
    `late` means the loop's last recorded run is more than 2x its expected cadence old. `unreadable` means this generator could not reach the loop's state at all (wrong host, permission, or unparseable content) -- not that the loop is unhealthy. `never` means the state is reachable and simply has no run recorded yet.

| Loop | Last run | Age | Cadence | Status | Last outcome |
|---|---|---|---|---|---|
| gitops-deploy | never | — | 10m | unreadable | state directory not reachable from here |
| renovate-agent | never | — | 1d | unreadable | state directory not reachable from here |
| renovate-notify | 2026-09-28T13:01:53+0000 | 12h28m | 1d | ok | checked, nothing new to notify |
| docs-refresh | 2026-09-29T00:15:00+0000 | 1h15m | 12h | ok | generators: failed: gen_infra_map.py |
| secret-rotate | 2026-09-28T19:44:48+0000 | 5h45m | 7d | ok | last touched by: Delete the Cluster Prometheus gate and scrutiny's own Discord notify, which both page twice |
| longhorn-restore-drill | never | — | 1d | unreadable | state directory not reachable from here |
| etcd-restore-drill | never | — | 7d | never | no run recorded yet |
