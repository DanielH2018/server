# `hosts/components/worktree-sweep`

## Autonomous-role contract (the timer removes worktrees and branches with no human in the loop)

- **Scope / exclusions:** merged worktrees under `.claude/worktrees/`, a fan-out batch's among them
  once its unit is inactive and its lock is a day old (`scripts/lib/fanout_lock.py`), orphan
  session branches (`worktree-*` and `claude/*`) whose content is on origin/master, unregistered
  worktree directories whose every file a ref already reaches, and unreachable objects older than
  a day. On `ops_cron_host` alone it also releases the issue claims `findings.py reap` judges
  stale. Every other host passes `--no-reap`, because its checkout cannot see the orchestrator's
  worktree and would read the orchestrator's live claims as stale. Nothing else.
- **Mode (explicit and reversible):** the timer's enable link in this package arms it. Dropping the
  link from a package version, or reverting this component, stops the timer on the install that
  drops it. `systemctl stop homelab-worktree-sweep.timer` stops it until the next install.
- **Authoritative sources:** `git worktree list` and origin/master as `prune_worktrees.py` reads
  them.
- **Abort valves:** `prune_worktrees.py` refuses a worktree it cannot prove merged, and keeps a
  fan-out tree whose unit is active or whose user manager it cannot ask.
- **Required evidence, every run:** a ping to the `<host>-worktree-sweep` Healthchecks.io check with
  the exit code, and the script's `--brief` lines in the journal under `worktree-sweep`.
- **Next-run review:** a non-zero exit pages through Healthchecks.io. Read
  `journalctl -t worktree-sweep` for the removal or branch deletion that failed.

ENFORCED: hosts/lib/tests/test_worktree_sweep_component.py, and
hosts/tests/test_worktree_sweep_cron_reports_its_exit.py, which runs the rendered script.
