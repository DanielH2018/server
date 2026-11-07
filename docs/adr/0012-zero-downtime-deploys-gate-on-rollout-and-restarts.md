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

Amended on 2026-10-17 (D5.3b3, #5426). A flux-owned tag no longer deploys through Ansible, so
the two gates below describe only the Ansible path. A flux-owned tag gates on the soak Job,
which the second paragraph of *Decision* describes. The rationale is unchanged: applying is
not shipping, and an unreadable signal fails closed.

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

A flux-owned tag gates on the soak Job instead (`docs/flux-deploy-plane.md`, *The soak Job*).
It runs once per promoted revision R and judges the services R changed:

- It anchors on each in-scope pod's first Ready time after R reached the cluster, and waits W
  past the latest one. W is the longer of the stabilise window and the longest `for:` among
  those services' critical or `verify="true"` rules, plus the scrape and evaluation
  allowance. Its floor is the 180 seconds above.
- It fails on a restart after first Ready, and on a critical or `verify="true"` alert that
  activated after R reached the cluster.
- It fails closed on a missing Ready sample, a restart inside a scrape gap, or a pod it
  cannot map to a service.

Its verdict is the `flux/soak` commit status, which `promote.py verify` waits on. A `failure`
holds promotion, and for a revision that changes no snapshot role it also resets `prod` to
`last-good`.

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

## Amendment 2026-11-06: ADR-0020 supersedes this record in part

The operator approved [ADR-0020](0020-flux-applies-the-cluster-from-a-promoted-artifact.md) and
this note on 2026-11-06. The decision text above stays as accepted. ADR-0020 overtakes the
statements below, because every k8s `containers_list` entry is Flux-owned and no Ansible k8s
play applies one (`ansible/deploy.yml` drops them through `ansible_owned`).

- *Consequences*, "denylisted from auto-deploy": the `k8s_autodeploy: false` flag only stops
  Renovate from merging an image bump for that role unattended. `promote` and Flux apply every merged
  change, and the soak Job gates it.
- *Consequences*, "a role's deploy rolls every workload it declares as an extra rollout": no
  Ansible k8s deploy runs. The render code in `scripts/renderer/renderer_lib/restart.py` stamps
  the restarts for `manifests_rollout` and for each `manifests_extra_rollouts` entry whose
  `restart_on` names `config` or `secret`.
- *Consequences*, "`--detach` returning is not a verified deploy": `--detach` applies to the
  setup plane and the Pi only, because `deploy.sh` refuses a k8s tag with exit 5.
- *Governs*, the Ansible gate in the shared rollout tasks: those tasks (`drain.yml` and
  `post_tasks/k8s_stabilise_gate.yml`) gate no k8s deploy. `probe.py health` stays the gate
  that `land.sh` runs after a promotion.

## Governs

No single line. `governs:` is empty. The Ansible gate lives in `scripts/diagnostics/probe.py health`
and in the shared rollout tasks. The Flux gate lives in
`ansible/roles/k8s/manifests/files/flux_soak.py` and in `promote.py verify`.
