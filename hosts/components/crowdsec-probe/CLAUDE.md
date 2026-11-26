# `hosts/components/crowdsec-probe`

## Autonomous-role contract (the timer probes the edge every minute with no human in the loop)

- **Scope / exclusions:** one HTTPS GET to the edge VIP, with jellyfin's hostname in the SNI and the
  Host header, and an append to the status file. It changes no cluster, host or firewall state,
  and it writes nowhere outside /var/log/pi-health/. The ban it makes visible is the verify
  Job's, not this unit's.
- **Mode (explicit and reversible):** the timer's enable link in this package arms it. Dropping the
  link from a package version, or reverting this component, stops the timer on the install that
  drops it (reconcile's step 1). `systemctl stop homelab-crowdsec-probe.timer` on the Pi stops
  it until the next package install. The status file then stops growing and stays bounded.
- **Authoritative sources:** this package at the SHA the Pi installed. `domain` and
  `k3s_metallb_ingress_vip` come from group_vars and render into the service at build time, so
  a change to either needs a new package version before the Pi's probe follows it.
- **Abort valves:** the updater's hold (docs/host-packages.md).
- **Required evidence, every cycle:** one `http=<code>` line in
  /var/log/pi-health/crowdsec-edge-probe.status, with `http=000` when nothing answered, and the
  counter `loki_process_custom_crowdsec_edge_probe_total` by status in Prometheus.
- **Next-run review:** a verify Job that fails naming the evidence channel means the probe did not
  run or Alloy did not count it. Read the status file's tail and
  `systemctl status homelab-crowdsec-probe.timer` on the Pi before changing the Job.

ENFORCED: hosts/lib/tests/components/test_crowdsec_probe_component.py, which renders the package and holds
the units, the script path, the status path and the shared User=.
