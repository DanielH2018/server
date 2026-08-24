---
id: "0002"
title: k3s replaces Docker Compose on the two cluster nodes
status: Accepted
date: 2026-08-01
governs: []
---

# ADR-0002: k3s replaces Docker Compose on the two cluster nodes

## Status

Accepted. Decided 2026-08-01, executed by 2026-08-14.

## Context

The homelab ran roughly 50 services as Docker Compose stacks split across `daniel-box` and
`daniel-server`, deployed by Ansible. Three goals drove a change: headroom, a use for
`daniel-server` once `daniel-box` proved the stronger machine on both CPU and memory, and
resilience against a single host's failure.

Docker Swarm was the obvious cheaper option and was rejected on a specific ground. Swarm
reschedules containers but not their data, and every service here bind-mounts local state,
so a rescheduled container would come up pointing at a directory that does not exist on its
new host. Rescheduling without the data is not resilience.

Two nodes cannot form a quorum, so a highly available control plane was never on the table:
three managers would be needed. The choice was therefore between a single-control-plane
orchestrator and staying on Compose.

## Decision

k3s, with a single control plane on `daniel-box` and `daniel-server` as an agent. Longhorn
provides replicated storage so that a rescheduled pod finds its data, which is the thing
Swarm could not offer. `--cluster-init` is used even for the single server, to keep a third
node possible later.

The migration ran service by service with the Docker stack live throughout, so every step
had a per-service rollback and there was no big-bang cutover.

## Consequences

**`daniel-box` is a single point of failure for the control plane.** Accepted knowingly:
two nodes cannot form a quorum, so the alternative was not a better cluster but no cluster.

**Docker is gone from both cluster nodes.** It was uninstalled from `daniel-server` on
2026-08-14, which is the migration's end state. Anything that shelled out to `docker` on
those hosts broke — `probe.py health` died with `FileNotFoundError: 'docker'` on both nodes
for two days afterwards, because its only mode was the Docker one.

**Ansible became the only write path to the cluster.** Plain `kubectl` authenticates as a
read-only ServiceAccount and `sudo` is denied, so a change reaches the cluster through a
playbook or not at all. That is a deliberate consequence, not an accident, but it means
every fix is a deploy.

**`kubectl apply` leaves things behind.** A manifest removed from a role keeps serving as an
orphaned object, and a key removed from a Secret stays live once ownership breaks. Both read
green from the repo side, so neither is visible without diffing rendered names against live
ones.

**The inventory pins both nodes to `connection=local`.** A one-shot play written for a
remote host silently runs on the wrong box.

**Portainer was retired with the Compose stacks**, which orphaned the Pi's `portainer-agent`,
the `portainer_manager_host` flag and a firewall rule admitting `daniel-server`. All three
were removed together — the only intrusion into the otherwise out-of-scope Pi, and a removal
rather than a change to what it runs.

## Governs

No single line. The decision is expressed by the existence of `ansible/roles/k8s/` and the
absence of Docker on both nodes, so `governs:` is empty.
