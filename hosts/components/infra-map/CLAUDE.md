# `hosts/components/infra-map`

## Autonomous-role contract (the timer regenerates the map every 15 minutes with no human in the loop)

- **Scope / exclusions:** one write, the HTML file at
  /home/<sys_user>/.claude/artifacts/homelab-infra-map.html. It reads the cluster through
  kubectl and daniel-pi over one ssh call, and changes no cluster, host or repository state.
- **Mode (explicit and reversible):** the timer's enable link in this package arms it. Dropping the
  link from a package version, or reverting this component, stops the timer on the install that
  drops it, and `systemctl stop homelab-infra-map.timer` stops it until the next install.
- **Authoritative sources:** the live cluster through kubectl, and the checkout's declarations.
- **Abort valves:** the updater's hold (docs/host-packages.md).
- **Required evidence:** the timestamp the page prints, and a journal line from the service on a
  failing run. A successful run prints nothing, because stdout is discarded.
- **Next-run review:** a page whose generated-at date is more than an hour old means the timer did
  not run or the run failed. Read `journalctl -u homelab-infra-map.service` before changing
  the generator.

ENFORCED: hosts/lib/tests/components/test_infra_map_component.py, which renders the package and holds the
unit, the timer, the PATH and KUBECONFIG environment and the discarded stdout.
