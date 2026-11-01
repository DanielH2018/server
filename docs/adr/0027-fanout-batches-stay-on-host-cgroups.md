---
id: "0027"
title: Fan-out batches stay on host cgroups, with per-batch limits, not in k8s pods
status: Accepted
date: 2026-10-31
governs:
  - "scripts/dev/fanout_lib/launch.py#each batch gets its own runaway limits"
---

# ADR-0027: Fan-out batches stay on host cgroups, with per-batch limits, not in k8s pods

## Status

Accepted. The operator chose this path on 2026-10-31.

## Context

A fan-out batch is a headless `claude -p` session in a transient `systemd-run --user` unit,
inside a locked worktree on daniel-box or daniel-server. Until this record, no batch had a
limit of its own. Every batch, and every interactive session of the same user, shared the
`MemoryHigh` on `user.slice` and on the user's own slice. One runaway could throttle all of
them. On 2026-09-05 one session scope reached 20.4G of anon memory, and a 30-minute stall on
daniel-box used all 8 GB of swap and starved k3s.

The question was whether to run batches as k8s pods or Jobs instead, so that the cluster
schedules them and limits each one. k8s would give three things:

- a scheduler that would replace the hand-written placement in
  `scripts/dev/fanout_lib/placement.py` and `launch_gates.py`
- a per-pod memory limit, so an OOM kill takes one agent
- a low PriorityClass, so the kubelet evicts agents before services

The second and third already have host equivalents. A unit's `MemoryMax` confines an OOM kill
to that unit. k3s holds 3.1 times the fleet's CPU weight (313 against 100), and the fleet's
`MemoryHigh` throttles agents before `kubepods`.

A move to pods would cost the following:

- The orchestrator would need a ServiceAccount that can create Jobs, and a role that can
  create pods can escalate through the pod spec. That breaks the rule that Ansible is the only
  write path to the cluster, and it puts `cluster-base` RBAC under the approval floor that the lander checks.
- Egress NetworkPolicies are not enforced on the k3s box (ADR-0009), so pods would not fence
  agents any better than the host does.
- The lander unit that polkit lets the agent start, `rootq-socket`, the worktree-holders
  socket, `dockrun` and the worktree-keyed claims have no pod equivalent that keeps the
  isolation a pod is meant to add.
- claude-guard and the chezmoi hooks would have to ship as an image, rebuilt on every dotfiles
  change.
- Neither host gains memory. daniel-box is memory-bound either way.

Two other options moved the work off the hosts: GitHub-hosted runners, which are free on this
public repo, and Claude Code cloud sessions. The operator ruled both out on 2026-10-31. Batches
run locally, and nothing new is paid for.

## Decision

Fan-out batches stay in systemd user units on daniel-box and daniel-server. Each batch unit
carries its own runaway limits on memory, swap and PIDs, set in `systemd_run_command`. The
memory limit is a multiple of the placement reservation, `RESERVATION_BYTES`. The limits bound
a runaway and never a normal batch. When a limit fires, `place status` names the unit's systemd
`Result` (`result=oom-kill`) on the batch's `failed` line.

## Consequences

- A batch that grows past its memory limit is killed and has to be placed again. The Discord
  page for it reads `failed`. The reason appears only on the status line, because the polkit
  rule for `fanout-alert@` admits a fixed set of state names.
- The limits cover only batches that `fanout.py place` launches. The `issue-fanout` skill's
  Agent-tool fallback runs no unit and gets none. Remote Control sessions stay under the
  login-slice cap.
- `io` is not delegated to the user managers, so a batch's disk I/O is not limited.
- Placement stays hand-written. That is the cost this record accepts.

Reopen this record when one of these holds:

- A third node joins the cluster.
- `placement.py` and `launch_gates.py` become the most-changed files in `fanout_lib` over 90
  days.
- Batches need a clean, throwaway environment, for example for a repo the operator does not
  trust.
- Waiting batches outgrow what `cc-wait fanout-headroom` handles.
- The operator lifts the local-only ruling.

A pod design starts from `kubernetes-sigs/agent-sandbox` (a Sandbox CRD with sandboxed container
runtimes) in a namespace that enforces PodSecurity `restricted`.

## Governs

The per-batch limits are set in `systemd_run_command` in `scripts/dev/fanout_lib/launch.py`.
The `# DECIDED:` marker there opens "each batch gets its own runaway limits" and cites this
record. `scripts/dev/tests/test_fanout_batch_caps.py` holds the limits on every launch shape.
