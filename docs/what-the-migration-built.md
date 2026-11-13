# What the target-architecture migration built

The target-architecture migration ran from 2026-10-11 to 2026-11-12. It replaced the GitOps
deployer, Uptime Kuma and Ansible with three planes that each have one write path from git.
This page says what runs at the end of it and where each part is documented. The plans that
sequenced the work are kept under *History* as a record. They describe how the lab got here,
not how it runs.

## The cluster: Flux applies a promoted artifact

CI renders every k8s manifest with `homelab-render` and publishes the result as an OCI
artifact. A `promote` workflow moves the tag that Flux follows. Flux applies the artifact
server-side, with prune and wait. Neither Ansible nor a deployer script applies a k8s workload,
and a bad change reverts through `promote` or holds through the `DEPLOY_HOLD` repository
variable.

- Decision: [ADR-0020](adr/0020-flux-applies-the-cluster-from-a-promoted-artifact.md).
- The renderer and the Jinja subset it supports:
  [ADR-0028](adr/0028-the-renderer-supports-a-closed-jinja-subset.md) and
  [the render contract](homelab-render.md).
- The cluster's own SOPS file and age key:
  [ADR-0022](adr/0022-the-cluster-decrypts-its-own-file-with-its-own-key.md).
- How a change reaches the cluster: [Flux deploy plane](flux-deploy-plane.md),
  [Deploying](deploying.md) and [Landing](landing.md).

## Monitoring: one alert plane on Prometheus and Alertmanager

Every check pages through Alertmanager, and Uptime Kuma is retired. Alert rules live in git
and are tested by `promtool test rules` and pint. A host producer writes a `node_exporter`
textfile metric, `blackbox_exporter` runs the ingress probes, and the `loki-homelab` ruler owns
the log rules. monitor-bridge is an exporter for the checks that only it can read. A
`Watchdog` rule always fires to Healthchecks.io, so an Alertmanager outage still pages.

- Decision: [ADR-0026](adr/0026-one-alert-plane-on-prometheus-and-alertmanager.md).
- The checks monitor-bridge still owns: [monitor-bridge checks](monitor-bridge-checks.md).
- The off-site deadman: [Healthchecks.io deadman](healthchecks-io-deadman.md).

## Hosts: each host is configured by its own package

Ansible is gone. Each host installs a signed Debian package built for it from the repository:
`homelab-<hostname>`, with `homelab-updater` and `homelab-tools`. The LAN builder on daniel-box
builds only a master commit with a green CI verdict. `dpkg` removes a file the package stops
shipping, and the reconcilers in `homelab-reconcile` own the state `dpkg` cannot. The Pi's
Compose services ship in its own package. system-upgrade-controller upgrades k3s.

- Decision: [ADR-0029](adr/0029-each-host-is-configured-by-its-own-package-not-ansible.md).
- What the builder trusts:
  [ADR-0021](adr/0021-the-host-package-builder-trusts-master-plus-green-ci.md).
- k3s upgrades: [ADR-0025](adr/0025-suc-upgrades-k3s-and-homelab-k3s-restart-restarts-it.md)
  and [the k3s upgrade runbook](k3s-upgrade.md).
- Packages, components and bring-up: [Host packages](host-packages.md) and
  [Host bring-up](host-bring-up.md).

## Docs: facts are generated or pointed at

`docs/facts.lock` and its hash test are deleted. Facts live in generated blocks, in
`docs/reference/`, in `# DECIDED:` markers and in `--help` text. A non-blocking CI notice lists
the `CLAUDE.md` sections that cite code a PR changed.

- Decision: [ADR-0023](adr/0023-fact-support-is-a-diff-notice.md).
- The generated pages: [Reference](reference/services.md) and
  [Decisions](reference/decisions.md).

## The plans

[The design](target-architecture/design.md) says why the migration ran and in what order.
[The contracts](target-architecture/contracts.md) state the interfaces the slices built
against. Each slice plan's `Status:` line records the PRs that carried it. The plans keep
their paths under `target-architecture/` because hundreds of files link into them.
