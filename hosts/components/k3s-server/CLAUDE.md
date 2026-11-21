# `hosts/components/k3s-server`

## Autonomous-role contract (the lock check runs with no human in the loop)

- **Scope / exclusions:** it reads logind's lock list and writes one textfile. It changes nothing.
- **Mode (explicit and reversible):** the timer's enable link arms it. k3s-agent ships the same
  timer, so dropping either component stops it on the install that drops it.
- **Authoritative source:** `systemd-inhibit --list`.
- **Abort valves:** none needed, since a run only reads.
- **Required evidence, every run:** the `kubelet-shutdown-lock` textfile.

ENFORCED: hosts/tests/test_k3s_server_component.py, hosts/tests/test_kubelet_shutdown_lock_check.py.
