# `setup/github_cli` — the GitHub CLI and its git credential helper

Installs `gh` from GitHub's APT repository and points git at `!gh auth git-credential`, so the deployer can call
the GitHub API authenticated. Applied by `initial_setup.yml` (`--tags github_cli`) where
`has_github_cli` is set, before `chezmoi_setup`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "github_cli"` when `has_github_cli`
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

- **The login itself is interactive and stays a hand step.** The role checks `gh auth status`
  at the end and reports when a login is still required; it cannot perform one. Until it is
  done, `gitops_deploy`'s CI gate runs anonymous (60 requests/hour, shared with every
  landing's `await_ci.py` poll on the host).
- **`chezmoi_setup` does not depend on the login.** DanielH2018/dotfiles is public, so the
  clone is anonymous and #2838 dropped the precheck that said otherwise. The ordering in
  `initial_setup.yml` survives for a narrower reason, stated there: the dotfiles' own
  `.gitconfig` template selects this credential helper only when `gh` is on PATH as chezmoi
  renders it.
- **The credential helper is per-user git config**, so it lives with the connecting user and
  the deploy user reads the same token through `gh auth token` — the one identity behind the
  deployer's CI gate, the two GitHub crons in `gitops_deploy`, and `renovate_agent`'s session.
- **The apt source and the keyring belong to the host packages, not this role.**
  `hosts/components/apt-github-cli/` ships `github-cli.sources` and the keyring under
  `/etc/apt/keyrings`, and the packages of daniel-box and daniel-server list it. dpkg creates the file at mode
  0644 whatever the umask, so the unreadable-keyring failure that
  `ansible/tests/setup/test_apt_keyring_permissions.py` guards against cannot occur. The
  keyring's sha256 is held by `scripts/renderer/tests/test_apt_source_components.py`.
