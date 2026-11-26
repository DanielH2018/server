# `hosts/components/checkout-sync`

## Autonomous-role contract (it moves the primary checkout's HEAD with no human in the loop)

- **Scope / exclusions:** a `git merge --ff-only` of `master` in the primary checkout, to the newest
  first-parent commit of origin/master whose required CI is green, within the walk bound. Never
  a red, pending or cancelled commit, never a non-fast-forward, never another branch, never a
  dirty tree. It applies nothing: it reads no `DEPLOY_HOLD` and no hold SHA. After any run
  that refused nothing it runs `uv sync --frozen --compile-bytecode` in the checkout, which
  writes the venv's bytecode as sys_user and removes a package uv.lock no longer lists.
- **Mode (explicit and reversible):** the timer's enable link in this package arms it. Dropping the
  link from a package version, or reverting this component, stops the timer on the install that
  drops it. `systemctl stop homelab-checkout-sync.timer` stops it until the next package install.
- **Abort valves:** the git-tree lock (a held lock skips the run), the branch and dirty-tree gates,
  the ancestry gate, the CI gate with its refusal of an empty context set or a non-positive
  walk bound, the missing-token refusal, the walk's 100-second budget, and
  `TimeoutStartSec=150`.
- **Authoritative sources:** origin/master as fetched this run, and GitHub's check-runs and
  check-suites for each graded commit, read with the builder's token. No verdict is cached.
- **Required evidence:** a `homelab-checkout-sync` journal line naming the merged commit and its
  distance below the tip, or the refusal and its reason; checkout_sync.prom in the
  node-exporter textfile directory with `homelab_checkout_sync_success_timestamp_seconds`,
  `homelab_checkout_dirty`, `homelab_checkout_sync_refused{reason}` and check `checkout-sync`'s
  `homelab_check_*` series, which CheckoutSyncFailed and CheckoutSyncNotReporting read.
- **Next-run review:** before widening scope, read `journalctl -t homelab-checkout-sync` for the
  last week's refusals. A `dirty` refusal parks the checkout until the stray file moves.

ENFORCED: hosts/lib/tests/host_lib/test_checkout_sync.py (the sync over a fixture repository) and
hosts/lib/tests/components/test_checkout_sync_component.py (the units), and
tests/services/alert_rules/test_b6a_rule_literals.py (the rules' deadline on every host).

The contract is per host. daniel-box and daniel-server list the component, and each host's
copy of the two rules names that host
(`k8s/roles/observability/files/rule-tables/host-checks.yaml`, selector
`{component: checkout-sync}`).
