# `setup/github_cli` — emptied; the host packages and the dotfiles repo own this

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/github_cli/tasks/main.yml`. H7.62 emptied it. It stays until H7.20a, because
`ansible/initial_setup.yml` names it and an edit to that playbook parks the deployer. Repo-root
`CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "github_cli"` when `has_github_cli`; ends on hosts
  where `True` (leading `meta: end_role`)
- **Crons / timers:** none (the role ends on every host before its first task)
<!-- /generated_from -->

## Where the work went

- `hosts/components/apt-github-cli/` ships `github-cli.sources` and the keyring under
  `/etc/apt/keyrings`, and it depends on `gh`. The packages of daniel-box and daniel-server list it.
  dpkg creates the file at mode 0644 whatever the umask, so the unreadable-keyring failure that
  `ansible/tests/setup/test_apt_keyring_permissions.py` guards against cannot occur. The keyring's
  sha256 is held by `scripts/renderer/tests/test_apt_source_components.py`.
- The dotfiles repo's `.gitconfig` template sets the `gh auth git-credential` helper for
  `https://github.com` when `gh` is on PATH as chezmoi renders it. A host applied before `gh` was
  installed needs a second `chezmoi apply`.
- `gh auth login` is interactive and stays a hand step (**operator-only**), in
  `docs/host-packages.md`, *Rebuilding daniel-box*. Until it runs, every `gh auth token` reader on
  the host runs anonymous at 60 requests/hour: `gitops_deploy`'s CI gate, the two timers of the
  `github-checks` component, and the `renovate-agent` component's session.
