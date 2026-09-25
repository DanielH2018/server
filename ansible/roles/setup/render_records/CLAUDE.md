# `setup/render_records` — the hourly k8s render-record producer on `daniel-box`

Writes `/var/lib/homelab/k8s-renders.d/<service>.json` for every renderable k8s service once
an hour, so `probe.py releases --stale-only` can clear a path-matched stale service by comparing
digests instead of trusting the path (#2587). The record format and the reader's rules are in
`roles/k8s/manifests/CLAUDE.md`, *Release records*. Applied by `initial_setup.yml --tags
render_records`, which the GitOps tick runs itself; it is not in `containers_list`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates or playbook entry. -->
- **Applied by:** `initial_setup.yml --tags "render_records"`
- **Timer:** `kuma-check-render-records.timer` (`OnCalendar=*-*-* *:{{ '%02d' |
  format(render_records_minute | int) }}:00`)
<!-- /generated_from -->

## How one run works

`files/render_records.py` runs as `{{ sys_user }}` from the shared kuma-check timer pair
(`roles/setup/common/tasks/kuma_check_timer.yml`), hourly at `render_records_minute`
(`group_vars/all.yml`):

1. It reads the primary checkout's `origin/master` and never fetches. The GitOps deployer
   fetches that ref every tick, and the staleness reader resolves the same ref, so a record's
   `commit` can match what the reader compares against. A second fetcher would contend with the
   deployer's for the ref locks.
2. It picks the newest commit there whose CI is green, with the deployer's own rule:
   `deploy_toolbox.fetch_ci_verdict` and `deploy_git.ci_walk_candidates`, imported from
   `/opt/gitops-deploy` rather than copied. The CI settings come from the deployer's
   `config.env`, and so does the primary checkout's path (`REPO_DIR`).
3. It moves its own detached, locked worktree (`render_records_checkout_dir`) to that commit.
   The render reads that tree, so it takes no tree lock and cannot read a half fast-forwarded
   primary checkout. The script creates the worktree itself when it is missing.
4. It asks that tree for the service list (`scripts/deploy_tools/render_targets.py`): every
   k8s service `containers_list` declares on this host whose role includes `k8s/manifests`,
   minus `k8s_dry_run_unsupported`. The list is derived at the commit being rendered.
5. It runs `deploy.sh --dry-run --skip-staleness-check -e manifests_render_record=true` over
   that list. The staleness gate is skipped because the chosen commit may sit below the tip;
   the record names its own commit, which is what the reader compares.
6. It reads every record back and pushes the verdict to the `render-records` Kuma tile.

## Autonomous-role contract (it runs a playbook hourly with no human in the loop)

- **Scope / exclusions:** a `--dry-run` render only. It applies nothing to the cluster
  (`--dry-run=server`), writes no release record, and takes no deploy lock. Its writes are the
  render records, its own worktree, and the throwaway render directories the dry run makes.
- **Mode (explicit + reversible):** `render_records_host` in `group_vars/all.yml`. Every
  other host runs the role's absent arm, which stops the timer and removes the units, the
  worktree and the role's directories. Setting the var to a host that does not exist retires
  the producer everywhere. The records it already wrote stay; the reader refuses them once
  their `commit` falls behind origin/master.
- **Authoritative sources:** the records themselves. The tile goes green only when every
  listed service has a record whose `commit` is the chosen SHA, `tree_dirty` is false, `host`
  is this host and `rendered_at` falls inside this run. Ansible's exit code is reported in the
  message and decides nothing.
- **Abort valves:** a run inside `render_records_boot_grace_s` of boot, and a CI walk that
  finds no green commit, both exit 1 without a push. Neither is a verdict, so neither may turn
  the tile green or red; `Restart=on-failure` reruns them every `render_records_restart_sec`.
- **Required evidence:** every run logs `status=<up|down>` with the commit and duration to the
  journal under the `render-records` syslog tag, at NOTICE because this host's journald drops
  INFO.
- **Next-run review:** before widening the list (a second host, a service outside
  `k8s/manifests`), read the tile's message history for what the previous runs failed to
  refresh.

## Limits

- **A record matches origin/master only until the next merge.** Master takes on the order of a
  hundred merges a day, and the producer runs hourly. The reader refuses a record whose
  `commit` is not origin/master, so between a merge and the next run it falls back to the
  path logic. That is the cadence the operator chose (2026-09-25); a render every 30 minutes
  would keep a play running 18% of the time.
- **A deferral is silent until the monitor expires.** A master whose CI stays pending or red
  for longer than `render_records_push_interval_s` turns the tile red with no message, because
  the producer pushes nothing while it defers.

## Working on it

- Tests: `uv run pytest ansible/roles/setup/render_records/tests
  scripts/deploy_tools/tests/test_render_targets.py`.
- To run it by hand on daniel-box: `sudo systemctl start kuma-check-render-records.service`,
  then `journalctl -u kuma-check-render-records -n 50`.
