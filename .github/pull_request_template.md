<!--
Merging is not shipping here. The host packages apply the host plane.
`promote` and Flux apply every k8s change after master CI, image pins and deploy-plane paths
included. `land.sh` waits on that promotion and gates the tags its changed set names. The
boxes below are the follow-through, not paperwork.
Full procedure: CLAUDE.md → "After a PR Merges — Pull, Deploy, Verify".
-->

## What changed, and why

<!-- Closes #<n> for every findings.py issue this fixes; GitHub closes it on merge. -->

## Services touched

<!-- The deploy tags this maps to, or "none — docs/tasks only". -->

## Deploy

- [ ] Deployed after merge (`land.sh` printed `VERDICT: settled`)
- [ ] Deliberately deferred — reason:
- [ ] Nothing to deploy (docs-, test-, or `tasks/`-only)

## Verification

<!--
Two verifications, because they see different things. `probe.py health <svc>` gates the rollout
and the 180s restart window; it cannot see whether YOUR change took effect — an Authelia 302
fires in the middleware before the backend is reached, and dead Grafana panels sit behind a 1/1
pod. Say what you exercised.
-->

- [ ] `uv run python scripts/diagnostics/probe.py health <svc>` passed
- [ ] Exercised the changed behaviour itself — how:

## Blast radius

- [ ] This is a **broad** change (`ansible/inventory/`, `ansible/templates/` or a shared
      template). `promote` applies it to every tag that renders it, so do not hand-run anything.
      Nothing applies `ansible/roles/setup/` or a bring-up playbook on its own; run a bring-up
      playbook by hand.
- [ ] New check, guard, or probe — ships with a paired test: one input it must accept, one it must reject.
