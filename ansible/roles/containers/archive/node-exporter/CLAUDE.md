# node-exporter — the Pi's host metrics container (RETIRED 2026-09-18)

> **Archived, not deployed.** No `containers_list` entry references this role, so nothing
> renders or deploys it. It is kept for the Compose plumbing, the way `archive/dozzle` is.
>
> **Why it went.** The same exporter runs as a host systemd unit since #2005 —
> `roles/setup/optimize_pi`, section 10, `node_exporter.service`, the upstream v1.12.1
> arm64 binary pinned by checksum, the same collector flags, the same LAN IP and port 9100
> — so the cluster's `node-pi` scrape job did not change. What the container cost was one
> containerd shim (~5 MB of anonymous memory) plus its share of dockerd's and containerd's
> state, on a 456 MB host that keeps 10-25 MB free; the Pi memory budget review of
> 2026-09-18 has the numbers. node-exporter was the one container whose host form changes no
> decision: it reads `/proc` and `/sys` and needs no Docker API.
>
> **Its host artifacts were removed imperatively** at retire time — the container and its
> `containers/node-exporter/` Compose project directory. Nothing in Ansible recreates them.
>
> **To revive it**, restore the `containers_list` entry in `daniel-pi.yml`, move this role
> back to `roles/containers/`, disable the host unit, and re-add `node-exporter` to
> `EXPECTED_PUBLISHERS` in `ansible/tests/services/test_pi_publishing_containers.py`.
