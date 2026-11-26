# `hosts/components/worktree-sweep`

## Autonomous-role contract (the timer removes worktrees and branches with no human in the loop)

- **Scope / exclusions:** merged worktrees under `.claude/worktrees/`, a fan-out batch's among them
  once its unit is inactive and its lock is a day old (`scripts/lib/fanout_lock.py`), orphan
  session branches (`worktree-*` and `claude/*`) whose content is on origin/master, unregistered
  worktree directories whose every file a ref already reaches, and unreachable objects older than
  a day. On `ops_cron_host` alone it also releases the issue claims `findings.py reap` judges
  stale, and only after a run that removed a worktree. No other host reaps, because its checkout
  cannot see the orchestrator's worktree and would read the orchestrator's live claims as stale.
  Nothing else.
- **The pruner is the dotfiles repo's.** `chezmoi apply` deploys it for the timer's user at
  `~/.claude/hooks/prune-worktrees.py`, and it reads this repo's policy from
  `.claude/prune-worktrees.json` in the primary checkout. The script passes `--require` with
  `scripts/lib/_claude_worktree.py:REQUIRED_INTERFACE`, so a host with no copy, or a copy older
  than this repo was written for, fails the run and pages
  (`hosts/tests/repo_crons/test_worktree_sweep_cron_reports_its_exit.py::test_a_host_with_no_pruner_fails_the_run_and_pings_the_failure`).
  daniel-server's dotfiles checkout needs a `git pull --ff-only` before its `chezmoi apply`.
- **A checkout with no policy file fails the run.** Without `.claude/prune-worktrees.json` the
  pruner keeps every fan-out tree and exits 0, so the script skips the pruner and exits 66
  (`hosts/tests/repo_crons/test_worktree_sweep_cron_reports_its_exit.py::test_a_checkout_with_no_policy_file_fails_the_run_and_pings_the_failure`).
  The cause is a stale primary checkout, which `CheckoutSyncFailed` or
  `CheckoutSyncNotReporting` pages first on a host that lists `checkout-sync`. Clear what
  parks the sync, or run `git pull --ff-only` in `/home/<sys_user>/server`.
- **Mode (explicit and reversible):** the timer's enable link in this package arms it. Dropping the
  link from a package version, or reverting this component, stops the timer on the install that
  drops it. `systemctl stop homelab-worktree-sweep.timer` stops it until the next install.
- **Authoritative sources:** `git worktree list` and origin/master as the pruner reads them.
- **Abort valves:** the pruner refuses a worktree it cannot prove merged, and keeps a
  fan-out tree whose unit is active or whose user manager it cannot ask.
- **Required evidence, every run:** a ping to the `<host>-worktree-sweep` Healthchecks.io check with
  the exit code, and the script's `--brief` lines in the journal under `worktree-sweep`.
- **Next-run review:** a non-zero exit pages through Healthchecks.io. Read
  `journalctl -t worktree-sweep` for the removal or branch deletion that failed.

ENFORCED: hosts/lib/tests/components/test_worktree_sweep_component.py, and
hosts/tests/repo_crons/test_worktree_sweep_cron_reports_its_exit.py, which runs the rendered script.
