# `hosts/components/docs-refresh`

## Autonomous-role contract (the refresh publishes generated docs with no human in the loop)

Twice daily (06:17 and 18:17, daniel-box only), docs-refresh regenerates the reference pages
and the infra map, and refreshes the test shard-weights table from CI's own logs. It records
what each test module depends on for one twelfth of the suite, rebuilds the site, and publishes
the diff through a PR that a detached landing merges.

- **Scope:** four derived paths and nothing else — `docs/reference/`, `docs/assets/generated/`,
  the shard-weights table `scripts/dev/pytest_shard_runner_weights.json`, and
  `scripts/dev/affected_tests.json`; a
  write outside them degrades the run. Every step runs in the cron's own worktree,
  `/var/lib/homelab/docs-refresh.d/tree`, so the primary checkout is never written.
- **Mode:** degrade, never abort. A failing generator sets `GENERATORS_OK=0`, publishes what
  succeeded and reports DOWN. A red test suite still exits 1. The timer's enable link arms it,
  and dropping the link from a package version, or reverting this component, stops the timer
  on the install that drops it.
- **Abort valves:** the unlanded-branch guard, the only one.
- **Authoritative sources:** the primary checkout's HEAD, which the tree is checked out at and
  cleaned of every untracked and ignored file, and origin's branch list for the
  unlanded-branch guard. The cluster Prometheus is the source of
  `docs/assets/generated/critical-memory-p95.json`, each critical-tier container's 7-day p95
  (`scripts/docs/reference/critical_memory.py`). When a recorded p95 grows past twice a
  container's memory request, the suite goes red on
  `critical-tier-memory-request-covers-half-its-p95` and the run publishes nothing until a PR
  raises the request.
- **Required evidence:** `status=<up|down> <msg>` in the log, and `docs-refresh.prom` in the
  node_exporter textfile directory, which DocsRefreshFailed and DocsRefreshNotReporting read.
- **Next-run review:** before adding a fifth staged path, check no prek hook matching it can fail
  on generator output. `docs/host-baseline-record.md`'s *The generated-docs refresh cron, in
  detail* has why, the `PUSH_STATUS` default and the `vale` exception.

ENFORCED: hosts/lib/tests/test_docs_refresh_component.py, and the failure-path tests in
hosts/tests/test_docs_refresh_*.py, which read the rendered script.
