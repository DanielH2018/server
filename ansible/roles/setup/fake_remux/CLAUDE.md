# `setup/fake_remux` — emptied; daniel-box's package owns the fake-remux detector, reconciler and mkv repair

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/fake_remux/tasks/main.yml`. H7.37 emptied it. It stays until H7.20a, because
`ansible/initial_setup.yml` names it and an edit to that playbook parks the deployer. Repo-root
`CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "fake_remux"` when `inventory_hostname ==
  fake_remux_host`; ends on hosts where `True` (leading `meta: end_role`)
- **Crons / timers:** none (the role ends on every host before its first task)
<!-- /generated_from -->

## Where the work went
`hosts/components/fake-remux/` owns it, and its `CLAUDE.md` holds the rules. The role's templates, files,
defaults and tests moved there in H7.37.

## Rollback
A revert of H7.37 restores the role's tasks, and the operator then re-applies the role on
daniel-box after `homelab_host_applied_info` shows the reverted version: `cd /home/ubuntu/server &&
uv run ansible-playbook ansible/initial_setup.yml --tags fake_remux` (operator-only).
