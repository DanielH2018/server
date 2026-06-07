# gitops_deploy — pull-based deploy on master change (daniel-server only)

Installs a systemd **timer** (every 30 min) that runs `/opt/gitops-deploy/gitops_deploy.py`
as `{{ sys_user }}`. The script fetches `origin/master`; if it advanced, maps changed
`roles/containers/<svc>/templates/docker-compose.yml.j2` files to service tags, `--ff-only`
merges, and deploys each via `ansible-playbook ansible/deploy.yml --tags <svc>`.

## Health gate + rollback
After deploy it polls each container's health (`max(5min)` default, see HEALTH_TIMEOUT_S).
On failure it `git reset --hard`es to the previous HEAD, redeploys the prior version,
writes the bad SHA to `/var/lib/gitops-deploy/hold_sha` (so the next tick won't redeploy it),
and alerts the dedicated Discord webhook. Reverting the offending PR advances `origin` past
the held SHA and the hold clears automatically.

## Safety
- Read-only against the repo (no push); rollback is local-only + self-guarding.
- Refuses to run on a dirty working tree (the host clone is deploy-managed).
- **Broad changes** (shared `ansible/templates/*`, `inventory/`, `common/`, `deploy.yml`)
  are NOT auto-scoped — the deployer alerts and defers to a manual full deploy.

## Config / secrets
`/etc/gitops-deploy/config.env` (0600) is templated from SOPS vars
`gitops_deploy_discord_webhook` and `gitops_deploy_kuma_push_token`. Liveness pings the
`gitops-deploy` Uptime-Kuma push monitor (provisioned via an AutoKuma label on
`monitor-bridge`) by launching a throwaway curl container on the `monitoring` network — the
host can't resolve container DNS directly.

## Logic tests
`files/test_deploy_logic.py` covers path→service mapping, the next-action decision, and
`container_names()` (the health gate inspects every `container_name:` in the changed
service's rendered compose — a role often runs several containers and the bumped image's
container is usually not the role-named one). Run via the repo pytest hook
(`uv run pytest ansible/roles/setup/gitops_deploy/files`).
