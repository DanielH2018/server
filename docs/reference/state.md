---
generated_from: scripts/docs/reference/state.py
generated_at: 2026-11-13 00:39 UTC
generated_sha: 4815c0d91
---

!!! warning "Generated file — do not edit"
    This page is rendered from this repository by `scripts/docs/reference/state.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# State of the lab

0 of 5 loops within cadence.

!!! warning "Status is a heuristic over the last recorded state"
    `late` means the loop's last recorded run is more than 2x its expected cadence old. `unreadable` means this generator could not reach the loop's state at all (wrong host, permission, or unparseable content) -- not that the loop is unhealthy. `never` means the state is reachable and simply has no run recorded yet.

| Loop | Last run | Age | Cadence | Status | Last outcome |
|---|---|---|---|---|---|
| renovate-agent | never | — | 1d | unreadable | state directory not reachable from here |
| renovate-notify | never | — | 1d | unreadable | state directory not reachable from here |
| docs-refresh | 2026-10-01T19:24:00+0000 | 42d5h | 12h | late | generators: failed: gen_infra_map.py |
| longhorn-restore-drill | never | — | 1d | unreadable | stamp ConfigMap not readable from here: no readable kubeconfig (tried $KUBECONFIG, /home/ubuntu/.kube/config, /etc/rancher/k3s/k3s.yaml) |
| etcd-restore-drill | never | — | 7d | never | no run recorded yet |
