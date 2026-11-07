# `setup/chezmoi_setup` — emptied; the dotfiles repo and the host package own this

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/chezmoi_setup/tasks/main.yml`. H7.62 emptied it. It stays until H7.20a,
because `ansible/initial_setup.yml` names it and an edit to that playbook parks the deployer.
Repo-root `CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "chezmoi"` when `has_chezmoi`; ends on hosts where
  `True` (leading `meta: end_role`)
- **Crons / timers:** none (the role ends on every host before its first task)
<!-- /generated_from -->

## Where the work went

- `hosts/packages/homelab-tools/package.yml` pins the chezmoi binary, at
  `/usr/lib/homelab/bin/chezmoi`. Step 5 of `hosts/bootstrap-host.sh` installs that package.
- `hosts/daniel-box/package.yml` depends on the distro packages the dotfiles' `install-cli-tools`
  script cannot install itself, because `sudo` has no TTY there.
- `docs/host-packages.md`, *Rebuilding daniel-box*, has the hand step that runs `chezmoi init
  --apply` (**operator-only**). `bin/land` in the dotfiles repo moves daniel-box's chezmoi source
  onto its `main`, and the operator's `chezmoi apply` deploys it. No timer applies the operator's
  dotfiles.
- The agent user's chezmoi seed is the claude-code component's
  `hosts/components/claude-code/templates/agent-chezmoi-seed.toml.j2`.
