# `setup/claude_code` — emptied; the host packages own Claude Code's host half

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/claude_code/tasks/main.yml`. H7.40's fourth slice emptied it. It stays
until H7.20a, because `ansible/initial_setup.yml` names it and an edit to that playbook parks
the deployer. Repo-root `CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "claude_code"` when `has_claude_code`; ends on
  hosts where `True` (leading `meta: end_role`)
- **Crons / timers:** none (the role ends on every host before its first task)
<!-- /generated_from -->

## Where the work went
`hosts/components/claude-code/` owns Claude Code's host half in `homelab-daniel-server` and
`homelab-daniel-box`. Its `templates/`, `files/` and `defaults/main.yml` are the ones this role
installed, moved there in #6773, and `hosts/components/claude-code/CLAUDE.md` holds the rules
for what they render. The role keeps no defaults, so no other role in the play can read a
`claude_code_*` variable from it.

## Rollback
The tasks read their sources from this directory, so a rollback reverts #6773's move first,
then H7.40's fourth slice. That restores the role's tasks, and the operator then re-applies the
role on daniel-box after `homelab_host_applied_info` shows the reverted version: `cd
/home/ubuntu/server && uv run ansible-playbook ansible/initial_setup.yml --tags claude_code`
(operator-only).
