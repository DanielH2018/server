---
id: "0013"
title: daniel-pi stays on Docker and out of the cluster
status: Accepted
date: 2026-08-01
governs: []
---

# ADR-0013: daniel-pi stays on Docker and out of the cluster

## Status

Accepted. Decided with [ADR-0002](0002-k3s-over-docker-compose-for-the-cluster-nodes.md) and
unchanged since.

## Context

The k3s migration covered `daniel-box` and `daniel-server`. `daniel-pi` was named explicitly
out of scope at design time rather than left undecided, because "we will get to the Pi
later" is how a two-platform homelab becomes permanent by accident instead of by choice.

The Pi runs a small set of LAN-only utilities: WireGuard, a Docker socket proxy, a metrics
viewer, an autoheal watchdog, and the node-exporter and promtail agents that feed the
cluster's monitoring (`containers_list` in `host_vars/daniel-pi.yml` is the current set; the
[Services reference](../reference/services.md) renders it). It is a different architecture, it is the
WireGuard endpoint that must stay reachable when the cluster is down, and it is the machine
an operator uses to get back in when something else has broken.

## Decision

`daniel-pi` keeps Docker Compose and stays out of the cluster. It is driven remotely over
SSH with `-e target=daniel-pi`, sets `has_gitops: false`, and is not a k3s node of any kind.

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

**The Pi's services are LAN-bound, not routed.** It sets `expose_mode: lan`, so its
containers bind the LAN address directly rather than passing through Traefik. Anything that
reasons about routes has to special-case it — the networking reference page excludes Docker
services for exactly this reason.

**A container on the Pi cannot reach a port its neighbour publishes on the same bridge.**
Same-bridge hairpin NAT, which broke homepage's widgets and a monitor check. **Containers
also lose their network across a Pi reboot** and come up `Up (healthy)` with no networks
attached, because the healthcheck passes on loopback. Neither has a cluster equivalent, and
both are the standing cost of keeping the platform.

## Governs

No single line. The decision is expressed by the Pi's inventory entry and by the continued
existence of `ansible/roles/containers/`, so `governs:` is empty.
