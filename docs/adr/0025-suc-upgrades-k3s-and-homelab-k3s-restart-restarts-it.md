---
id: "0025"
title: "SUC upgrades k3s, and `homelab-k3s-restart` restarts it"
status: Accepted
date: 2026-10-24
governs: []
---

# ADR-0025: SUC upgrades k3s, and `homelab-k3s-restart` restarts it

## Status

Accepted. The operator accepted it on 2026-10-24. Slice H6 step H6.1a (the
[H6 plan](../target-architecture/host/h6-k3s-suc.md)) records the approval, which PR H6.1b
required before it could merge.

## Context

Ansible applies every k3s change by hand. `ansible/k3s-bringup.yml` is one of the bring-up
playbooks that the deployer never runs, and the comment above `k3s_server_args` in
`ansible/roles/setup/k3s/defaults/main.yml` says why: "precisely so nothing automated ever
applies it." A restart of the control plane takes down Pi-hole and Traefik, which sit behind
the server, so the rule was deliberate.

The rule has two costs. Each bump of `k3s_version` makes the tick write a `manual_plane`
line that pages six hours later, and the operator then runs the upgrade by hand from
`docs/k3s-upgrade.md`. About 56 of the 74 line edits to the 12 most-edited keys in the role's
defaults are cluster configuration, which ships only through the same hand-run playbook.

The [target architecture](../target-architecture/design.md) (*Host plane*) fixes
system-upgrade-controller (SUC) as the upgrade mechanism for k3s, and Flux as the deploy
plane ([ADR-0020](0020-flux-applies-the-cluster-from-a-promoted-artifact.md)). Slice H6
weighs no other k3s upgrade mechanism. This ADR does not decide whether to use SUC. It
records the operator's approval to reverse the "nothing automated" rule, and the limits
under which the reversal is safe.

## Decision

SUC upgrades k3s, and `homelab-k3s-restart` restarts k3s to apply a configuration change.
Both run unattended, and each runs only behind a gate and, for the restart unit, inside a
window.

- **Upgrades.** A Flux-owned role, `system-upgrade`, ships SUC and two Plans, `k3s-server`
  and `k3s-agent`. The pin `k3s_version` moves into that role, so a bump routes to a
  Flux-owned tag and writes no `manual_plane` line. SUC upgrades the server first, because
  Kubernetes forbids a kubelet newer than its API server. The Plans neither cordon nor
  drain, which matches the runbook.
- **Gates.** One stdlib script, `hosts/lib/k3s_gates.py`, runs in each Plan's `prepare`
  container and refuses on a read error, a missing member or an empty list. It checks
  Longhorn volume health and in-progress backups, node readiness, the CoreDNS rollout, a
  windowed restart in progress and a recent server restart. On daniel-box it also checks the
  deployer's hold and `all` lock until H7.19a retires the deployer. A refusal that outlasts
  the Job deadline is a `JobFailed`, which holds promotion and pages.
- **Configuration.** Each host package ships the k3s flags as a `config.yaml.d` drop-in and
  marks the k3s unit `windowed`, so reconcile never restarts it. A timer starts
  `homelab-k3s-restart` at `k3s_restart_window`, a different slot on each host. The unit
  hashes the shipped files, runs the same gates and restarts k3s once per changed hash. A
  killed attempt counts as that attempt. Ansible keeps only the fresh-host install.
- **Mutual exclusion.** `prepare` refuses while the restart unit's runtime directory exists,
  and the unit refuses while a Plan is applying or a SUC Job runs. SUC and the unit never
  restart one node together.
- **Pause levers.** Four levers stop the automation, and the H6 plan's *Pause and resume*
  table gives the commands:
    - a node label pauses SUC on that node at once;
    - `system_upgrade_nodes: []` pauses it on every node, through git;
    - masking `homelab-k3s-restart.timer` pauses the restart on one host;
    - masking `homelab-updater.timer` pauses every package change on one host.
- **Contract.** `ansible/roles/k8s/system-upgrade/CLAUDE.md` carries the *Autonomous-role
  contract*: scope, arming, abort valves, required evidence and the next-run review. The
  Plans select no node until a PR widens `system_upgrade_nodes`, one node at a time.

## Alternatives not taken

The H6 plan rejects these.

- **Run the gate as a merge-time rule in `land_lib/policy.py`.** Such a
  rule would be inert for the PRs that carry k3s bumps, and it would gate the merge, not the
  apply. The gate runs in `prepare`, whichever path merged the PR.
- **Make the agent's `prepare` fail fast while the server upgrades.** SUC starts both Jobs
  when the Plans resolve, and three failed pods end in `JobFailed` within minutes. The agent
  waits for the server Plan and for Longhorn instead.
- **Cordon and drain before an upgrade.** The runbook does neither, and a failed Job would
  leave the node cordoned.
- **Rehearse in k3d.** In a k3d node the k3s process is the container's entrypoint, so a
  binary replacement never takes the path a real node takes. The rehearsal uses a throwaway
  k3s guest on daniel-server, a systemd host like daniel-box.
- **Read a Flux status as the evidence for a selector-only change.** Such a change keeps the
  Plan's version and hash, so Flux can read Current before any Job exists. The evidence is
  the node's `plan.upgrade.cattle.io/<plan>` label.

## Consequences

- **A control-plane restart takes DNS and ingress down.** The window holds config restarts to
  a weekday slot between health-cron fires, and every restart passes the storage gates first.
  A bump applies when promotion applies it, so the operator lands a bump in a quiet slot.
- **A held `system-upgrade` revision stops every later promotion.** The tag is forward-only,
  so promotion never lowers a Plan below a version a node runs. The price is that the
  operator clears the hold by hand after fixing the cause.
- **The deployer stays coupled to the restart path until H7.19a.** The gates read its hold
  and lock. A tick that overlaps a SUC server restart can still fail its deploy and write
  `hold_sha`. H7.19a's tick-stopping PR removes the seven deployer-coupled items, before any
  PR deletes `deploy_locks.py`.
- **The Job installs a binary that nothing on the node checks.** `rancher/k3s-upgrade` is
  pulled by tag. H6.3e adds a sum pin and a verify Job, which catch a mismatch after the Job
  ran and before `last-good` moves.
- **A rehearsal is not production.** The guest runs the server Plan without `prepare`, on one
  node with no Longhorn. The gates rest on their fixtures and on a parity test against the
  runbook's own predicates.
- **To reverse,** write a superseding ADR, set `system_upgrade_nodes: []`, and revert the
  slice's PRs in the order the H6 plan's *Rollback* gives. The pin returns to `setup/k3s`,
  and every k3s change is a hand apply again.

## Open questions

The H6 plan leaves these open. Each is the operator's or another slice's to settle.

- **The window.** The operator confirms or moves `k3s_restart_window` for each host in
  H6.3b's review.
- **`k3s_readonly_token` in SOPS.** Adding it is **operator-only**. Without it, the
  daniel-server restart stays a hand step until H7.
- **H4's restart contract.** H4 says reconcile is the only process that restarts a unit
  outside the boot path. H4 is asked to name SUC's upgrade container and
  `homelab-k3s-restart` as the two other processes that restart a `windowed` k3s unit. H6.3b
  does not merge before that amendment.
- **The server-restart signal.** The guard reads the `kube-controller-manager` Lease's
  `acquireTime`, which no agent has measured. H6.3a's operator read decides whether the guard
  ships.
- **`rancher/k3s-upgrade` by digest.** Whether SUC accepts a digest in `upgrade.image` is
  unread. H6.3c quotes the source and names the choice.
- **The agent join token.** H6 moves no token. H7's fresh join decides whether agents take an
  agent-scoped token.

## Governs

No `# DECIDED:` marker references this record yet. The H6 PRs that add the role, the gate
script and the restart unit add the markers and fill the `governs:` list in the same PR.
