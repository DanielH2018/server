---
id: "0033"
title: A game server's replica count is runtime state, set from deploy.local
status: Accepted
date: 2026-11-14
governs:
  - "k8s/roles/deploy-ui/templates/games-rbac.yaml.j2#DECIDED: deployments/scale only"
---

# ADR-0033: A game server's replica count is runtime state, set from deploy.local

## Status

Accepted.

## Context

The Terraria and Valheim servers run when someone wants to play and are parked the rest of
the time. Each game is two Deployments: the server, and its game-stats exporter, which has
nothing to read while the server is down. Flux renders `spec.replicas` on all four from
`terraria_k8s_replicas` and `valheim_k8s_replicas` in `inventory/group_vars/all.yml`.
Starting a game therefore takes a PR, master CI and a promotion, which is minutes at best. A
`DEPLOY_HOLD`, red master CI or the Sunday promotion window blocks it outright.

A live `kubectl scale` does not help. Flux's drift correction resets the count to the
rendered value at the next reconcile, and the read-only identity a session runs as cannot
scale anyway. Running a game server is an operator's choice of the moment, not a
configuration change, so the operator ruled on 2026-11-14 to make it runtime state.

Three shapes were considered:

- **A button that opens and lands a PR.** It keeps git as the only write path, but each
  toggle waits on CI and promotion and adds a commit to a public repository.
- **A write-capable identity with `patch` on `deployments`.** It can restart a pod by
  patching the template, but it can also rewrite the pod spec. Pod Security Admission only
  warns and audits on this cluster, so a privileged container or a hostPath mount of `/`
  would be admitted, and a leaked token would mean root on a node.
- **An identity with `deployments/scale` on the game Deployments only.** It can change
  replica counts and nothing else. A restart becomes scale to 0, wait for the pod to go,
  then scale back, which these single-replica `Recreate` Deployments lose nothing by.

## Decision

The four game Deployments' `spec.replicas` is runtime state, which deploy.local's Games
panel sets through the `homelab-games` ServiceAccount. That identity holds one rule:
`get`, `patch` and `update` on `deployments/scale`, limited by `resourceNames` to the
Deployments `game_servers` lists. Flux stops rendering `spec.replicas` on those Deployments.
Before the field is dropped, the operator co-owns it under the `homelab-operator` field
manager. Flux's takeover leaves that manager alone, so the count survives the cutover.

## Consequences

- **The cluster gets a second write path.** `promote` and Flux stay the only path for
  everything else, including these Deployments' images, config and claims. The second path is
  bounded by the Role: a holder of the token can start or stop a game server and nothing
  more. `tests/k8s/_rbac_property_rows.py` holds that bound on every rendered Role and binding
  that names the identity.
- **Any pod in the cluster can reach the panel.** deploy-ui does no authentication, and a pod
  reaches it without Authelia, which was accepted for landings on 2026-09-10. Once the panel
  carries this identity, any pod can start or stop the games. The operator accepted that
  reach on 2026-11-14, because it is limited to the replica counts of four Deployments.
- **The game's state is no longer in git.** A rebuilt cluster starts each game Deployment at
  one replica, the API server's default, until the panel parks it. Nothing in the tree says
  which games were running.
- **A stopped exporter is an empty scrape target, not a missing job.** The game-stats jobs
  render whatever the count, so `ScrapeTargetDown` and `ScrapeTargetFlapping` for those jobs
  read the live replica count and stay quiet while kube-state-metrics reports 0.
- **Terraria's start stays gated on issue #822.** Version 1.4.5.7 cannot stay up while
  anything opens a TCP connection to it. The panel refuses to start Terraria until that
  issue's restore checklist is done.
- **The identity lives in the deploy-ui tag, not cluster-base.** A rebuilt cluster applies
  cluster-base before anything creates `homelab`, so a `homelab` object there would stall the
  bring-up. The deploy-ui tag prunes, so removing the identity from the render deletes it.
- **The token is operator-minted.** The operator reads the token Secret, stores it in
  daniel-box's host secrets, and the deploy-ui unit loads it with `LoadCredential=`. It never
  lands in `~/.kube/config`, which `claude-agent-setup.sh` copies to the agent user.

## Governs

- `k8s/roles/deploy-ui/templates/games-rbac.yaml.j2`, the
  `DECIDED: deployments/scale only` marker on the Role.
