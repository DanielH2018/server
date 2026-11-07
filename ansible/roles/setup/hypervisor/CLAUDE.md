# `setup/hypervisor` — emptied; daniel-server's host package owns the hypervisor

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/hypervisor/tasks/main.yml`. H7.34 emptied it. It stays until H7.20a, because
`ansible/initial_setup.yml` names it and an edit to that playbook parks the deployer. Repo-root
`CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "hypervisor"`; ends on hosts where `True` (leading
  `meta: end_role`)
- **Crons / timers:** none (the role ends on every host before its first task)
<!-- /generated_from -->

## Where the work went

`hosts/components/hypervisor/` owns the KVM/libvirt host, the `staging` network, its egress fence
and the monthly etcd restore drill in `homelab-daniel-server`. Its `templates/`, `files/` and
`defaults/main.yml` are the ones this role installed, and `hosts/components/hypervisor/CLAUDE.md`
holds the rules.

## Rollback

The drill goes back with a revert of H7.34, then a hand apply on daniel-server after
`homelab_host_applied_info` shows the reverted version:
`uv run ansible-playbook ansible/initial_setup.yml --tags hypervisor` (operator-only).
