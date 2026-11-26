# `hosts/components/github-checks`

## Autonomous-role contract (the interaction-limit check writes a repository setting with no human in the loop; the ruleset check only reads and reports)

- **Scope / exclusions:** one GitHub setting, the interaction limit of each repo in
  `github_interaction_limit_repos`, re-applied to the declared value. The ruleset check changes nothing, because a ruleset is a
  setting a human changes on purpose (the script's DECIDED marker).
- **Mode (explicit and reversible):** the timers' enable links in this package arm them. Dropping a
  link from a package version, or reverting this component, stops that timer on the install that
  drops it. `github_interaction_limit: none` makes the check clear the limit instead.
- **Authoritative sources:** defaults/main.yml for what should hold, and GitHub's REST API for what
  does.
- **Abort valves:** a reachout gate before each read, and no token means a `down` verdict with
  nothing written. The updater's hold stops every package install after a failed reconcile until `--clear-hold`.
- **Required evidence, every run:** a `status=<up|down>` line tagged github-ruleset-drift or
  github-interaction-limit in the journal, and each check's .prom in node_exporter's textfile
  directory, which the GitHub rules in observability's gitops-checks.yaml read.
- **Next-run review:** a `down` that survives one rerun means the setting moved. Read the status line
  before you change defaults/main.yml: the declaration is reconciled only when the change on
  GitHub was deliberate.

ENFORCED: hosts/tests/repo_crons/test_github_ruleset_drift.py and
hosts/tests/repo_crons/test_github_interaction_limit.py (each script against stubs), and
hosts/lib/tests/test_h737_box_components.py (the units and the legacy entries).
