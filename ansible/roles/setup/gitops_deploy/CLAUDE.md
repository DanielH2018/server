# `setup/gitops_deploy` — emptied; the GitOps deployer's timer is retired

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/gitops_deploy/tasks/main.yml`. H7.52 emptied it and retired
`gitops-deploy.timer`: the `legacy:` lists of `hosts/daniel-box/package.yml`,
`hosts/daniel-server/package.yml` and `hosts/daniel-pi/package.yml` remove the units, the polkit
rule and the tick's textfile. It stays until H7.20a, because `ansible/initial_setup.yml` names
it and an edit to that playbook parks the deployer. Repo-root `CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "gitops_deploy"`; ends on hosts where `True`
  (leading `meta: end_role`)
- **Crons / timers:** none (the role ends on every host before its first task)
<!-- /generated_from -->

## Where the work went
`hosts/components/checkout-sync/` fast-forwards daniel-box's primary checkout, and `promote`
and Flux apply every k8s change. `docs/gitops-pipeline.md` says what runs in the deployer's
place.

H7.53 deleted the deployer's code under `files/`, its unit templates and its tests. The role
keeps its `end_role` stub and `defaults/main.yml`, whose `gitops_deploy_*` keys surviving
checks still read. `docs/archive/gitops-deploy-role.md` and `docs/archive/gitops-deployer.md`
hold the record of the deleted code.

## Rollback
A revert of H7.52 restores the role's tasks, and the operator then re-applies the role on
daniel-box after `homelab_host_applied_info` shows the reverted version: `cd /home/ubuntu/server &&
uv run ansible-playbook ansible/initial_setup.yml --tags gitops_deploy` (operator-only).
