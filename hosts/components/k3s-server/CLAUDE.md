# `hosts/components/k3s-server`

## Autonomous-role contract (the lock and gateway checks run with no human in the loop)

- **Scope / exclusions:** the lock check reads logind's lock list, and the gateway check pings
  `lan_router_ip` bound to the node's `k3s_node_static_link`. Each writes one textfile and
  changes nothing: no route, link or NIC setting.
- **Mode (explicit and reversible):** each timer's enable link arms it. k3s-agent ships the same
  timers, so dropping either component stops them on the install that drops it. Emptying
  `k3s_node_static_link` drops the gateway check's files and enable link.
- **Authoritative source:** `systemd-inhibit --list` for the lock, and a reply to `ping -I <link>`
  for the gateway.
- **Abort valves:** none needed, since a run only reads.
- **Required evidence, every run:** the `kubelet-shutdown-lock` and `wired-gateway` textfiles.

ENFORCED: hosts/tests/test_k3s_server_component.py, hosts/tests/test_kubelet_shutdown_lock_check.py, hosts/tests/test_wired_gateway_check.py.
