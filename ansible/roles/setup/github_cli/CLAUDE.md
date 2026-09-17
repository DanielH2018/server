# `setup/github_cli` — the GitHub CLI and its git credential helper

Installs `gh` from GitHub's APT repository (keyring under `/etc/apt/keyrings`, the deprecated
one-line source removed) and points git at `!gh auth git-credential`, so a host can clone the
PRIVATE dotfiles repo and the deployer can call the GitHub API authenticated. Applied by
`initial_setup.yml` (`--tags github_cli`) where `has_github_cli` is set, before
`chezmoi_setup`.

- **The login itself is interactive and stays a hand step.** The role checks `gh auth status`
  at the end and reports when a login is still required; it cannot perform one. Until it is
  done, `chezmoi_setup` fails at the clone and `gitops_deploy`'s CI gate runs anonymous
  (60 requests/hour, shared with every landing's `await_ci.py` poll on the host).
- **The credential helper is per-user git config**, so it lives with the connecting user and
  the deploy user reads the same token through `gh auth token` — the one identity behind the
  deployer's CI gate, the two GitHub crons in `gitops_deploy`, and `renovate_agent`'s session.
- **The keyring task sets an explicit mode**, and
  `ansible/tests/setup/test_apt_keyring_permissions.py` refuses one that does not: a key
  written under the ambient umask can land unreadable by the unprivileged `_apt` user, and
  `apt update` then fails with an error that names the repository, not the mode.
