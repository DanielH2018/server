<!--
Merging is not shipping here. The GitOps deployer auto-deploys only an image-pin bump to a
non-denylisted k8s service; an ordinary manifest or template change fast-forwards onto the
primary checkout and is never applied. The boxes below are the follow-through, not paperwork.
Full procedure: CLAUDE.md → "After a PR Merges — Pull, Deploy, Verify".
-->

## What changed, and why

## Services touched

<!-- The deploy tags this maps to, or "none — docs/tasks only". -->

## Deploy

- [ ] Deployed after merge (`./scripts/deploy.sh --changed <pre-merge-SHA>` from `/home/ubuntu/server`)
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

- [ ] This is a **broad** change (`ansible/roles/setup/`, `ansible/inventory/`, `ansible/templates/`,
      `ansible.cfg`, or a bring-up playbook). It parks the GitOps tick's fast-forward for every
      session, so it needs its playbook run by hand and an `--ff-only` merge afterwards.
- [ ] New check, guard, or probe — ships with a paired test: one input it must accept, one it must reject.
