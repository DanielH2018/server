# `hosts/components/eval-sweep`

## Autonomous-role contract (the sweep publishes eval results with no human in the loop)

Weekly (Sunday 02:00, daniel-box only), the sweep grades every case under `evals/cases/`, rolls
the result into `evals/history.json`, then publishes it.

- **Scope:** run the existing cases and commit the trended result; never edits a case, an agent or a
  skill definition, and never runs the eval engine's live-smoke path. Every step after the key
  gate runs in the cron's own worktree, `/var/lib/homelab/eval-run.d/tree`, so the primary
  checkout is never written.
- **Mode:** binary, no dial — hermetic when the API key exists, else a no-op reporting UP. The
  timer's enable link arms the unit, and dropping the link from a package version, or reverting
  this component, stops the timer on the install that drops it.
- **Authoritative sources:** the chezmoi eval engine's own grading, never a cached report, run
  over the primary checkout's HEAD, which the tree is checked out at and cleaned of every
  untracked and ignored file.
- **Abort valves:** the unlanded-branch guard; the empty-key gate.
- **Required evidence:** `status=<up|down> <msg>` in the log, and `eval-run.prom` in the
  node_exporter textfile directory, which EvalSweepFailed and EvalSweepNotReporting read.
- **Next-run review:** before widening scope, read the last few `evals-history/*` PRs this unit
  opened for what regressed or flaked. `docs/host-baseline-record.md`'s *The eval sweep, in
  detail* has the REGRESSED-case commit rule and why there is no non-hermetic fallback.

ENFORCED: hosts/lib/tests/test_eval_sweep_component.py, and the tests in
hosts/tests/repo_crons/test_eval_sweep_cron.py and test_eval_run_failure_path.py, which read the
rendered script.
