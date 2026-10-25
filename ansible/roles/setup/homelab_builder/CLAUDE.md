# homelab_builder — emptied; daniel-box's package owns the builder, the signer and the apt repository

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/homelab_builder/tasks/main.yml`. H7.41b emptied it. It stays until H7.20a,
because `ansible/initial_setup.yml` names it and an edit to that playbook parks the deployer.
Repo-root `CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "homelab_builder"` when `has_gitops`; ends on hosts
  where `True` (leading `meta: end_role`)
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## Where the work went
`hosts/components/builder/` owns the builder, the signer and the apt repository, and
`hosts/components/failure-alert/` owns the page a failed run opens. `hosts/components/builder/CLAUDE.md`
holds the rules. The scripts live in `hosts/lib/`. This role keeps only `tests/`, which cover those
scripts and run from `ansible/roles/*/*/tests`.

## Rollback
A revert of H7.41b restores the role's tasks and units, and the operator then re-applies the role on
daniel-box after `homelab_host_applied_info` shows the reverted version: `cd /home/ubuntu/server &&
uv run ansible-playbook ansible/initial_setup.yml --tags homelab_builder` (operator-only).
