# gitops_deploy — pull-based deploy on master change (daniel-server only)

Installs a systemd **timer** (every 30 min) that runs `/opt/gitops-deploy/gitops_deploy.py`
as `{{ sys_user }}`. The script fetches `origin/master`; if it advanced, maps changed
`roles/containers/<svc>/templates/docker-compose.yml.j2` files to service tags, `--ff-only`
merges, and deploys each via `uv run --frozen ansible-playbook ansible/deploy.yml --tags <svc>`
(the repo-pinned env, same as the operator — needs `uv` on the unit's PATH).

## Health gate + rollback
After deploy it polls each container's health (`max(5min)` default, see HEALTH_TIMEOUT_S).
On failure it `git reset --hard`es to the previous HEAD, redeploys the prior version,
writes the bad SHA to `/var/lib/gitops-deploy/hold_sha` (so the next tick won't redeploy it),
and alerts the dedicated Discord webhook. Reverting the offending PR advances `origin` past
the held SHA and the hold clears automatically.

## Safety
- Read-only against the repo (no push); rollback is local-only + self-guarding.
- Refuses to *deploy* from a dirty working tree (operator mid-edit) but the tick still
  completes normally and writes `last_run` (`next_action(..., dirty=True) -> "dirty"`) — the
  skip is healthy, not an outage, so it must not trip the GitOps-Alive monitor's stale-file
  threshold.
- **Broad changes** (shared `ansible/templates/*`, `inventory/`, `common/`, `deploy.yml`)
  are NOT auto-scoped — the deployer alerts and defers to a manual full deploy.

## Config / secrets
`/etc/gitops-deploy/config.env` (0600) is templated from the SOPS var
`gitops_deploy_discord_webhook`. Liveness is now written to `/var/lib/gitops-deploy/last_run`
(a Unix-timestamp file) on every non-crashing completion; `monitor-bridge` reads this file
to drive the GitOps-Alive Uptime-Kuma monitor — no Kuma pushing from the deployer.

## Logic tests
`files/test_deploy_logic.py` covers path→service mapping, the next-action decision, and
`container_names()` (the health gate inspects every `container_name:` in the changed
service's rendered compose — a role often runs several containers and the bumped image's
container is usually not the role-named one). Run via the repo pytest hook
(`uv run pytest ansible/roles/setup/gitops_deploy/files`).
