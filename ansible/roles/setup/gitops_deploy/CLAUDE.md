# gitops_deploy — pull-based deploy on master change (daniel-server only)

Installs a systemd **timer** (every 30 min) that runs `/opt/gitops-deploy/gitops_deploy.py`
as `{{ sys_user }}`. The script fetches `origin/master`; if it advanced, maps each changed file
under `roles/containers/<svc>/{templates,files}/` (the compose template OR a bind-mounted config
template / `files/` asset) to its service tag, `--ff-only` merges, and deploys each via
`uv run --frozen ansible-playbook ansible/deploy.yml --tags <svc>` (the repo-pinned env, same as
the operator — needs `uv` on the unit's PATH). A config-only change therefore triggers a scoped,
health-gated redeploy too (closing the loop so live config matches master), not a silent ff-merge;
`tasks/` and the role `CLAUDE.md` are deliberately NOT auto-deployed (structural/docs — deploy
those manually).

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
  threshold. The dirty-tree Discord page is throttled (`should_alert_dirty`) to at most once
  per America/Chicago calendar day, on the first tick at/after 07:00 CT — without it a long
  edit session would re-page every 30-min tick. State: `/var/lib/gitops-deploy/dirty_alerted_date`.
- **Broad changes** (shared `ansible/templates/*`, `inventory/`, `common/`, `deploy.yml`)
  are NOT auto-scoped — the deployer alerts and defers to a manual full deploy.
- **Secrets-only pushes** (`ansible/vars/secrets.yml` changed with no service template — a
  rotation pushed from another machine) are fast-forwarded but **not** redeployed: the new
  value only reaches a container on its next deploy, so the deployer alerts (once per SHA,
  `secrets_alerted_sha` marker) to redeploy the consumer(s). `secrets.yml` is deliberately
  NOT in the broad list — the `/add-secret` flow ships it WITH the consuming template, which
  stays a scoped single-service deploy (`deploy_logic.ChangeSet.secrets`).
- Acts **only when origin is strictly ahead of local** (`is_ancestor(local, origin)` →
  `next_action(..., origin_ahead=…)`). Un-pushed local commits make origin an *ancestor* of
  local; that's a no-op, not a deploy — otherwise the tick would diff `local..origin` (the
  *reverse* of those commits) and mis-fire a redeploy + false rollback. Push to clear it.
- Health-gates **only services deployed on THIS host** (daniel-server). A changed template for
  an other-host-only service (e.g. `dozzle` is daniel-pi-only) renders no compose here, so
  `containers_for()` returns `[]` and it's skipped — without this the gate polls a phantom
  container until `HEALTH_TIMEOUT_S` and false-rollbacks (`deploy_logic.containers_to_gate`).
  - **By design: Pi-only services are NOT auto-deployed by GitOps (accepted, 2026-06-30).** The
    deployer runs on daniel-server only; there is deliberately **no GitOps/CI deploy path to
    daniel-pi**. A change to a Pi-only service (e.g. `wg-easy`/`dozzle` on the Pi) ff-merges and
    "deploys" as a local no-op, then skips the health gate per the rule above — so the tick reports
    success while the Pi never actually redeploys ("cross-host phantom-success", review CI-L2). This
    is intentional, not a gap: the Pi is a memory-constrained Zero 2 W driven manually over SSH (see
    [[daniel-pi-zero2w-memory-constrained]]), and a Renovate image bump to a Pi service is rare. Push
    deploys to the Pi by hand: `uv run ansible-playbook ansible/deploy.yml --tags <svc> -e target=daniel-pi`.
    Revisit (a Pi-side deployer / a CI cross-host gate) only if Pi-service churn ever makes the manual
    step a real miss.

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
