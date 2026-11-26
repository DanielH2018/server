# `hosts/components/registry-gc`

## Autonomous-role contract (the run scales two Deployments and edits two stores with no human in the loop)

- **Scope / exclusions:** registry-gc.sh scales `deploy/ghcr-mirror` and then
  `deploy/dockerhub-mirror`, one at a time. It runs each mirror's Job from
  /etc/rancher/k3s/manifests/registry-jobs/, writes each keep-set ConfigMap, and warms each
  mirror through its loopback hostPort. It never scales `deploy/registry`, the push store. The
  script is the one writer of the two mirrors' `spec.replicas`. Nothing else.
- **Mode (explicit and reversible):** the timer's enable link arms it. `registry_k8s_manage_gc: false`
  in host_vars, or reverting this component, stops it on the install that drops the link.
  `systemctl stop homelab-registry-gc.timer` stops it until the next package install.
- **Authoritative sources:** the live cluster's workload specs and statuses for the keep sets, and
  each store's own revision links for what they keep.
- **Abort valves:** the flock on `registry_k8s_gc_lock_file`, the Job deadline, and the keep set that
  fails closed: a run that cannot read the workloads prunes nothing and reports down.
- **Required evidence, every run:** the `registry-gc` textfile under
  /var/lib/node-exporter-textfile/, which RegistryGcFailed and RegistryGcNotReporting read, a
  ping to the `registry-gc` Healthchecks.io check, /var/lib/registry-gc/state.json, and both
  mirror phases in `journalctl -t registry-gc`. The EXIT trap exits 1 from a run that ends
  `status=down`, so the unit's result agrees with the textfile. The alerts read the textfile.
- **Next-run review:** `/var/lib/registry-gc/<mirror>-down` left behind names a mirror a killed run
  stranded at zero. The next run restores it first. Read the registry-gc-<mirror> Job logs
  before changing a keep count.

ENFORCED: hosts/components/registry-gc/tests/test_registry_gc_component.py (the units, the links
and the legacy entries), tests/services/registry_gc/test_registry_gc_ghcr_mirror.py and
test_registry_gc_dockerhub_mirror.py (the script's behaviour against stubs).
