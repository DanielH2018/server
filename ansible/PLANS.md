# Future Plans

- Setup Authelia for Raspberry Pi
- Add scraping python scripts to git(clean up git hook, and refactor for shared functionality.)
- Migrate images off of latest tag and watchtower to Renovate.
- Setup PR Automation with Renovate PRs
- Add a `--check` (dry-run) gate in CI for host-deployed `setup/` roles. The gitops_deploy
  rollout hit three failures that `ansible-playbook --check` or a molecule-style role test
  would have caught before touching the host: a var loaded under `tasks:` instead of
  `pre_tasks:` (undefined at role time), a missing `/etc/gitops-deploy` dir (template won't
  mkdir its parent), and the health gate inspecting the role name vs the container_name.
  Worth it only if more host-side `setup/` roles get added.
