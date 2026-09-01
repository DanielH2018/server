---
id: "0006"
title: Longhorn provides replicated block storage for the cluster
status: Accepted
date: 2026-08-01
governs: []
---

# ADR-0006: Longhorn provides replicated block storage for the cluster

## Status

Accepted. Decided as part of the k3s migration
([ADR-0002](0002-k3s-over-docker-compose-for-the-cluster-nodes.md)).

## Context

Every service in the homelab bind-mounted local state under Docker. That is precisely what
made Docker Swarm useless here: an orchestrator that reschedules a container onto a node
where its data does not exist has not made anything more available.

So the storage decision was not separable from the orchestrator decision. Choosing k3s only
pays off if a pod can be rescheduled and still find its volume, which requires replicated
storage the cluster itself manages.

Longhorn also brings its own backup mechanism, backing up whole block volumes to an
S3-compatible target. That mattered because it offered a route off the existing host-level
backup tool for anything living on a persistent volume.

## Decision

Longhorn provides the cluster's storage. Workload state lives on Longhorn PersistentVolumes
rather than host paths, and Longhorn's own backup target ships those volumes offsite.

## Consequences

**A volume's size must be a multiple of its block size.** Longhorn refuses anything else.
Enforced across the tree by `ansible/tests/longhorn/test_pvc_sizes_match_block_size.py`, because it is
a deploy-time rejection that is trivially avoidable at authoring time.

**`actualSize` is not filesystem usage.** A successful cleanup inside a volume can leave
`actualSize` unchanged, so a freed-space check that reads it reports a no-op. Count what the
tool deleted instead.

**The Longhorn API only answers from the node-local manager.** Dialling the ClusterIP is a
coin flip; the local manager pod has to be resolved first.

**Its state metrics are one-hot over a label.** Counting series returns the total volume
count for every state, so a naive query says every volume is in whichever state was asked
about.

**A revert needs maintenance mode** — the volume attached with `disableFrontend`, not
detached — and snapshot deletion is an asynchronous purge rather than an immediate one.

**The engine's HTTP delete bypasses admission**, where `kubectl delete` does not. `error=lost`
is that path's success state, which reads as a failure to anyone who has not met it before.

**A Job that touches storage has a bimodal cost without a node pin**: 3.02 seconds against 37.57 seconds
for the same Job, almost all of it image pull on the cold node.

## Governs

No single line. The decision is expressed by the Longhorn StorageClass and by every role's
PVC, so `governs:` is empty.
