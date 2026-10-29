---
id: "0013"
title: daniel-pi stays on Docker and out of the cluster
status: Accepted
date: 2026-08-01
governs: []
---

# ADR-0013: daniel-pi stays on Docker and out of the cluster

## Status

Accepted. Decided with [ADR-0002](0002-k3s-over-docker-compose-for-the-cluster-nodes.md).
Amended 2026-10-28: the Pi's services ship in its host package, and `ansible/roles/containers/`
is retired (*Amendment* below).

## Context

The k3s migration covered `daniel-box` and `daniel-server`. `daniel-pi` was named explicitly
<!-- vale Google.Will = NO -->
out of scope at design time rather than left undecided, because "we will get to the Pi
later" is how a two-platform homelab becomes permanent by accident instead of by choice.
<!-- vale Google.Will = YES -->

The Pi runs a small set of LAN-only utilities: WireGuard, a Docker socket proxy, a metrics
viewer, an autoheal watchdog, and the node-exporter and Alloy agents that feed the
cluster's monitoring (`containers_list` in `host_vars/daniel-pi.yml` is the current set; the
[Services reference](../reference/services.md) renders it). It is a different architecture, it is the
WireGuard endpoint that must stay reachable when the cluster is down, and it is the machine
an operator uses to get back in when something else has broken.

## Decision

`daniel-pi` keeps Docker Compose and stays out of the cluster. It is driven remotely over
SSH with `-e target=daniel-pi`, sets `has_gitops: false`, and is not a k3s node of any kind.

## Amendment (2026-10-28): the Pi's services ship in its host package

The operator accepted this amendment on 2026-10-24, before H5.2b merged
([h5-pi-compose.md](../target-architecture/host/h5-pi-compose.md), *Open questions* 7). H5.12
records it here.

**The deploy path changes, and the decision does not.** `daniel-pi` keeps Docker Compose and
stays out of the cluster. Its Docker services are no longer Ansible roles that
`deploy.sh -e target=daniel-pi` renders and applies over SSH. They are `compose_services`
entries in `ansible/inventory/host_vars/daniel-pi.yml`, rendered from
`hosts/components/pi-compose/services/<name>/` into the `homelab-daniel-pi` host package. The
Pi's updater installs each package version, and `homelab-compose.service` runs the services as
one Compose project, `homelab`. A merged change reaches the Pi without a hand-run deploy.

**`ansible/roles/containers/` is retired.** H5.6 to H5.9 moved docker-proxy, autoheal, alloy and
wg-easy into the package, and H5.12 deleted the shared `common` role and the tree with it. A
service's configuration still lives in the tree that deploys it: the Pi's under
`hosts/components/pi-compose/`, the cluster's under `ansible/roles/k8s/`.

**`-e target=daniel-pi` stays for the setup plays.** `initial_setup.yml` and the Docker engine
upgrade still reach the Pi over SSH with it. It no longer deploys a service.

The first two consequences below are read with this amendment: the second platform's tree is
`hosts/components/pi-compose/`, and a Compose service on either cluster node still deploys
nothing.

## Consequences

**The repo carries two deployment platforms permanently.** `ansible/roles/k8s/` and
`ansible/roles/containers/` are both live, and a service's configuration lives in whichever
tree deploys it — never across the boundary. Eleven roles under `containers/` were
configuration-only sources for a k8s counterpart until 2026-08-14; they moved into it, and
reviving one as a Docker service means recovering its Compose plumbing from git history.

**A Compose role on either cluster node deploys nothing.** Neither has Docker, so a new
service belongs under `roles/k8s/` unless it must be on the Pi. This is the failure mode the
split invites, and it is silent.

**The recovery path does not depend on the thing being recovered.** WireGuard staying on the
Pi is what makes remote access survive a cluster outage. That is the main reason the
exception is worth its cost.

**The Pi's services are LAN-bound, not routed.** Its containers publish their ports on the
host's LAN address directly rather than passing through Traefik. Anything that
reasons about routes has to special-case it — the networking reference page excludes Docker
services for exactly this reason.

**A container on the Pi cannot reach a port its neighbour publishes on the same bridge.**
Same-bridge hairpin NAT, which broke homepage's widgets and a monitor check. **Containers
also lose their network across a Pi reboot** and come up `Up (healthy)` with no networks
attached, because the healthcheck passes on loopback. Neither has a cluster equivalent, and
both are the standing cost of keeping the platform.

## Governs

No single line. The decision is expressed by the Pi's inventory entry and its
`compose_services` key, and by the existence of `hosts/components/pi-compose/`, so `governs:`
is empty.
