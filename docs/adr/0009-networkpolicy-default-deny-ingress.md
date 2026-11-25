---
id: "0009"
title: Every pod is fenced by a default-deny ingress NetworkPolicy
status: Accepted
date: 2026-08-16
governs: []
---

# ADR-0009: Every pod is fenced by a default-deny ingress NetworkPolicy

## Status

Accepted. All five slices deployed and enforcing by 2026-08-20.

Amended on 2026-11-25. This record said egress was not enforced, from one probe on
2026-08-07. A re-test found that it is, and *Consequences* holds the measurement. The
decision stands: the baseline is ingress only.

## Context

Kubernetes admits every pod-to-pod connection by default, and a NetworkPolicy only constrains
the pods it *selects*. A pod that no policy selects accepts everything, forever.

Before this, the cluster had four workload policies and **zero** with an empty `podSelector`,
so roughly 50 workloads accepted connections from anything in the cluster. Authentication and
the WAF sit at the edge — Traefik, then Authelia, then CrowdSec — which covers traffic
arriving from outside and does nothing about traffic that is already inside.

The threat is not a hypothetical attacker with cluster access. It is one compromised or
misbehaving workload reaching every other one, including the ones holding credentials.

## Decision

Default-deny ingress at namespace scope. Every pod in `homelab` and `observability` is fenced
unless it carries the `netpol-baseline-exempt` label, and the exempt set is declared in one
place with a reason inline per entry. A cluster-side exact-set gate refuses the deploy if the
live set drifts from the declared one.

Rolled out in five slices rather than at once, because a default-deny that breaks callers is
indistinguishable from an outage.

## Consequences

**Egress is enforced, except in the first seconds of a pod's life.** The re-test of
2026-11-25, on k3s v1.37.2+k3s1, ran a pod under deny-all egress and an unfenced control on
each node. Both dialled a LAN node, a pod, the API Service and the internet for 90 seconds.
The control connected every time. The fenced pods connected on their first dial, at 0 s, on
seven of eight rows, and never again. The next dial came about 3 s later, so the gap is
shorter than that. An egress policy therefore does not stop a pod that dials as it starts.
The probe of 2026-08-07 dialled once, as it started, on a cluster that ran kube-router
v2.6.3-k3s1. Anything reasoning about outbound restriction must still probe with an unfenced
control rather than trusting the object's existence.

**Narrowing an allowlist needs a caller census first.** Two of three narrowings broke callers
that were inside the CIDR being treated as the attacker. Authelia's `remote_ip` log is the
census tool.

**A census that excludes a workload's own role still misses callers** — its siblings' init
containers are not visible that way.

**Ports are pod ports, not Service ports.** Traefik's 80 and 443 are really 8000 and 8443,
and fencing the wrong pair denies every in-cluster caller.

**A sidecar arrives as its host pod.** The CrowdSec agent inside Authelia is `app=authelia` to
anything writing policy about it.

**kube-router's source-pod lookup lags pod creation**, so a Job that dials immediately on
start fails. Rules with no `from:` clause are immune, which is why the class stayed hidden.

**Counts go stale within a day** and are deliberately not recorded in the design document —
it named four exempt workloads from 2026-08-17 until 2026-08-23, by which point there were
five. Query the cluster instead.

## Governs

No single line. `governs:` is empty; the exempt set and its reasons live in
`k8s/roles/netpol-baseline/defaults/main.yml`.
