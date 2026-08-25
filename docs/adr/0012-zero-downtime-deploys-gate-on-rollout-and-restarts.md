---
id: "0012"
title: A deploy is not finished until the rollout completes and the pod stays up
status: Accepted
date: 2026-08-20
governs: []
---

# ADR-0012: A deploy is not finished until the rollout completes and the pod stays up

## Status

Accepted.

## Context

An Ansible deploy applies manifests and returns. Applying is not shipping: `kubectl apply`
succeeds the moment the API server accepts the object, long before a pod is running, and
often before the old one has gone away.

Two specific things made a naive "did it apply" check worthless here.

`kubectl wait --for=condition=Available` returns instantly, because a Deployment that already
has available replicas from the *previous* ReplicaSet satisfies the condition immediately. It
reports on the old pods.

And readiness flips a Deployment to Available before a bad liveness probe starts killing it,
so a rollout check on its own reports green on a workload that is about to crashloop.

## Decision

A deploy gates on two things, and both must hold. First, `rollout status` — not
`wait --for=condition=Available` — so the check follows the *new* ReplicaSet: observed
generation caught up, every replica updated, ready and available. Second, a stability soak:
no container restarted in the last 180 seconds.

An unreadable restart time counts as recent, so the gate fails closed.

## Consequences

**A workload with no readiness probe cannot be gated**, so those roles are denylisted from
auto-deploy rather than deployed blind. Some are unprobeable rather than merely probe-less —
a scratch image running an outbound loop has no listener, no shell, and therefore no possible
`httpGet`, `tcpSocket` or `exec` probe.

**A role's deploy rolls every workload it declares as an extra rollout**, not just the primary
one. The gate is role-level, so one edited template restarts the primary plus every name in
`manifests_extra_rollouts` — including stateful ones.

**A pod gate cannot see application-level breakage.** Nineteen dead Grafana panels rendered
nothing for 55 minutes behind a 1/1 pod with clean migrations and zero errors. Verifying a
deploy therefore means exercising the thing that changed, not only reading the gate.

**A rollout restart issued by the read-only ServiceAccount still reports success**, printing
the same "successfully rolled out" line a real one does. The command's own output is not
evidence that a write happened; live state is.

**A partial run is worse than no run** where ordering is load-bearing. An aborted
policies-first deploy leaves exactly the state the ordering exists to prevent — four routes
returned 5xx for 17 minutes.

**`--detach` returning is not a verified deploy.** It backgrounds the rollout wait, which is
most of the deploy.

## Governs

No single line. `governs:` is empty; the gate lives in `scripts/probe/probe.py health` and in the
shared rollout tasks.
