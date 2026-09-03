---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-09-03 12:23 UTC
generated_sha: 1806968e
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/docs/reference/state.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# State of the lab

5 of 7 loops within cadence.

!!! warning "Status is a heuristic over the last recorded state"
    `late` means the loop's last recorded run is more than 2x its expected cadence old. `unreadable` means this generator could not reach the loop's state at all (wrong host, permission, or unparseable content) -- not that the loop is unhealthy. `never` means the state is reachable and simply has no run recorded yet.

| Loop | Last run | Age | Cadence | Status | Last outcome |
|---|---|---|---|---|---|
| gitops-deploy | 2026-09-03T11:59:34+0000 | 24m | 10m | late | ticked, no hold |
| renovate-agent | never | — | 1d | never | no run recorded yet |
| renovate-notify | 2026-09-02T20:32:29+0000 | 15h51m | 1d | ok | checked, nothing new to notify |
| docs-refresh | 2026-09-03T12:22:00+0000 | 1m | 12h | ok | generators: ok |
| secret-rotate | 2026-09-02T16:23:18+0000 | 20h | 7d | ok | last touched by: Page when the staging-gate ratchet stops running, not only when it fails (#847) |
| longhorn-restore-drill | 2026-09-03T04:10:37+0000 | 8h13m | 1d | ok | PVC restore proven |
| etcd-restore-drill | 2026-08-31T10:20:03+0000 | 3d2h | 7d | ok | list-only restore proven (snapshot offbox-daniel-box-1788144302.zip) |
