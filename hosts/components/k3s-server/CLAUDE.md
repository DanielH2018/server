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

ENFORCED: hosts/tests/k3s/test_k3s_server_component.py, hosts/tests/host_checks/test_kubelet_shutdown_lock_check.py, hosts/tests/host_checks/test_wired_gateway_check.py.

## Autonomous-role contract (`homelab-kubepods-memory-low.path`, the reload after kubelet's reset)

- **Scope / exclusions:** each trigger runs `systemctl daemon-reload` and nothing else. It sets
  no property itself, so it cannot rewrite the file it watches.
- **Mode (explicit and reversible):** the path unit's enable link arms it. k3s-agent ships the
  same unit, so dropping either component stops it on the install that drops it.
- **Authoritative source:** kubelet's `/run/systemd/transient/kubepods.slice.d/50-MemoryLow.conf`,
  which kubelet rewrites with `MemoryLow=0` at each start.
- **Abort valves:** drop the enable link. The `MemoryLow` drop-ins still apply at the next
  reload any other package runs.
- **Required evidence, every run:** `/sys/fs/cgroup/kubepods.slice/memory.low` reads
  `k3s_kubepods_memory_low`. `docs/claude-code-rc-caps.md` has the derivation and the verify
  commands.

ENFORCED: hosts/tests/k3s/test_k3s_server_component.py::test_kubelets_reset_of_kubepods_memory_low_is_overridden_and_reapplied.
