# config_files — emptied; the Pi package owns the SOPS export

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/config_files/tasks/main.yml`. H7.25 emptied it. It stays until H7.20a,
because `ansible/initial_setup.yml` names it and an edit to that playbook parks the deployer.
Repo-root `CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "config_files"`; ends on hosts where `True`
  (leading `meta: end_role`)
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## Where the work went
The role copied a tracked `.bashrc` and `.gitconfig` to the Pi, the only host without chezmoi.
`hosts/components/pi-tuning/` now ships `/etc/profile.d/homelab.sh`, which carries the
`SOPS_AGE_KEY_FILE` export the tracked `.bashrc` held. The Pi keeps the `.bashrc` and
`.gitconfig` it holds, and a rebuilt Pi gets Ubuntu's own `.bashrc` and no `.gitconfig`.
chezmoi owns the dotfiles on daniel-box and daniel-server.

## Rollback
A revert of H7.25 restores the tasks, and the operator then re-applies the role on the Pi after
`homelab_host_applied_info` shows the reverted version: `cd /home/ubuntu/server && uv run
ansible-playbook ansible/initial_setup.yml --tags config_files -e target=daniel-pi`
(operator-only).
