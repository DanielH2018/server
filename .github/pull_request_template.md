<!--
Merging is not shipping here. The GitOps deployer applies a setup-role change on its own.
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

## Render-diff

<!--
The `render-diff` job compares this PR's render with its merge base's, and reads one line at
the start of a line in this body. The line is mandatory when the PR touches
`ansible/roles/k8s/*/tasks/`, deletes anything under `ansible/roles/k8s/`, or touches
`scripts/renderer/`, `scripts/lib/k8s_roles.py`, `scripts/lib/ansible_jinja_env.py` or
`ansible/filter_plugins/`, or moves a `uv.lock` or `pyproject.toml` entry of a renderer
dependency, such as Jinja2 or MarkupSafe. Without it those PRs fail. Elsewhere it is optional,
and it binds when present. Write one of these two forms below this comment, on a line of its own:

    Render-diff: none
    Render-diff: flux/x.yaml, cluster/x/y.yaml

`none` allows no rendered difference. A list must equal the files that differ. A line inside
this comment or a code fence does not count.
-->

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
      `ansible.cfg`, or a bring-up playbook). The GitOps deployer applies a setup-plane change as
      `initial_setup.yml --tags <role>`, and `promote` applies a deploy-plane change, so do not
      hand-run those. Only a bring-up playbook (`bootstrap.yml`, `k3s-bringup.yml`,
      `initial_setup.yml`) parks the tick for every session and needs its playbook run by hand.
      A setup role whose tag the deployer cannot derive merges and lands in `manual_plane`;
      `land.sh` prints the command to apply it.
- [ ] New check, guard, or probe — ships with a paired test: one input it must accept, one it must reject.
