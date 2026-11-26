# `hosts/components/loki-route-witness`

## Autonomous-role contract (the witness reports a verdict with no human in the loop)

It changes no cluster state: it reads the route and reports. Its only host state is a down marker.

- **Scope / exclusions:** one `probe.py loki-labels` request through the Loki read route, run from
  the primary checkout as sys_user. It writes loki-read-route-health.prom in node_exporter's
  textfile directory and a syslog line. It creates
  /var/lib/homelab/check/loki-route-witness.down after a down verdict and removes it after an up
  one. The timer fires hourly, and a down verdict reruns every 15 minutes until the route answers.
  It changes no cluster state.
- **Mode (explicit and reversible):** the timer's enable link in this package arms it. Dropping
  the link from a package version, or reverting this component, stops the timer on the install
  that drops it. `systemctl stop homelab-loki-route-witness.timer` stops it until the next
  package install.
- **Authoritative sources:** `probe.py loki-labels`'s body, judged by loki_route_health.py. The
  exit code is not the verdict, because probe.py's curl carries no `-f` and a 404 exits 0.
- **Abort valves:** the boot grace (`loki_route_witness_boot_grace_s`), which exits 1 without a
  verdict, and the updater's hold (docs/host-packages.md).
- **Required evidence, every run:** a `status=<up|down>` line tagged loki-read-route-health in
  syslog, and loki-read-route-health.prom in the textfile directory, which LokiReadRouteFailed
  and LokiReadRouteNotReporting read.
- **Next-run review:** a down verdict for the route means a broken ClientIP grant or a dead
  loki-homelab route. Read the syslog line's message before changing the route. Both nodes
  must witness it, because which source address a host arrives as depends on where traefik
  runs.

ENFORCED: hosts/tests/repo_crons/test_loki_route_witness.py (the script's behaviour against
stubs), hosts/lib/tests/test_loki_route_witness_component.py (the units and legacy entries)
and hosts/lib/tests/test_component_retired_stamp_fragments.py (the manifest fragment names).
