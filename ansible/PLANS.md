# Future Plans

- Setup Authelia for Raspberry Pi
- Add scraping python scripts to git(clean up git hook, and refactor for shared functionality.)
- Migrate images off of latest tag and watchtower to Renovate.
- Setup PR Automation with Renovate PRs
- Add a `--check` (dry-run) gate in CI for host-deployed `setup/` roles, run in the frozen uv
  env: `uv run --frozen ansible-playbook ansible/initial_setup.yml --tags gitops_deploy --check
  --connection local` (or a molecule-style role test against an ephemeral container). **Primary
  justification — env validation:** the test env (`prek`/`pytest`) and the *deploy* env (ansible
  runtime + `community.docker`'s `requests`/`docker`) are coupled by the uv lock but nothing
  validates the deploy env until a real deploy runs. The uv migration left that gap *twice*
  (test deps, then docker deps); a `--check` run fails at module-import time on the PR instead
  of on the host. It also catches structural/ordering bugs: e.g. a var loaded under `tasks:`
  instead of `pre_tasks:` (undefined when rendered at role time). Note the limits: it would
  NOT have caught the health-gate role-vs-container_name bug (a logic bug in the deployer `.py`
  — that was a unit-test catch, see `files/test_deploy_logic.py`), `command`/`shell` tasks are
  skipped in check mode, and check mode introduces cascade false-positives (a task needing a
  prior task's real side effect — e.g. templating into a not-yet-created `/etc/gitops-deploy` —
  reports failure because the dir-creating task didn't actually run). Worth it only if more
  host-side `setup/` roles get added.
