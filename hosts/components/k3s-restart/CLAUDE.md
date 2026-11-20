# `hosts/components/k3s-restart`

## Autonomous-role contract (restarts the cluster's control plane unattended)

- **Scope / exclusions:** one unit, `k3s_restart_unit`, once per changed hash of the files the
  package ships for it (manifest `activations` entries whose action is `manual:<unit>`). It
  never drains or cordons.
- **Mode (explicit and reversible):** the timer's enable link arms it. `sudo systemctl mask --now
  homelab-k3s-restart.timer` pauses it, and `unmask` then `start` resumes it.
  `sudo systemctl start homelab-k3s-restart.service` runs it now, behind the same gates.
- **Abort valves:** `k3s_gates.py host` (storage, nodes, CoreDNS, no SUC run, no Flux promotion in
  flight, and on an agent host the server-restart guard), `TimeoutStartSec=7min`, and one
  attempt per hash, a killed attempt included.
- **Authoritative sources:** the manifest, the API read with the read-only ServiceAccount's token
  (never an event, which would read an absent restart as clean), and the node's own heartbeat.
- **Required evidence:** notice lines in the journal, and homelab_k3s_restart.prom with `pending`,
  `failed` and `last_success_timestamp_seconds` on every run. K3sConfigRestartFailed pages a
  failed attempt and K3sConfigRestartPending a refusal that outlasts 72 hours.
- **Next-run review:** read `journalctl -u homelab-k3s-restart -p notice` for the last windows
  before widening the hash set or narrowing the window.
