# gitops_deploy — pull-based deploy on master change (every has_gitops host)

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
the held SHA, which stops the `skip_hold` short-circuit — but the marker itself (and the red
**GitOps Deploy — Status** monitor, which pages on a non-empty `hold_sha`, no origin
comparison) only clears when a later tick completes a *successful service deploy on this
host*: `write_hold(None)` sits in the clean-deploy branch, and noop/docs-only ticks return
before reaching it. If everything after the held SHA maps to no service here, diagnose the
hold, then delete `/var/lib/gitops-deploy/hold_sha` by hand.

**A service migrated off this host must take its rendered compose with it.** `containers_for()`
treats a present `containers/<svc>/docker-compose.yml` as proof the service is deployed here,
and a compose with no `container_name:` falls back to gating a container named `<svc>`. Removing
a service from this host's `containers_list` deletes neither — so a later template-only commit
for the now-other-host service deploys as a no-op, then health-gates a phantom container to the
run budget and false-rollbacks + holds. That is the 2026-08-08 `configarr` hold (`02360cc3`):
configarr had moved to a k8s CronJob on daniel-box, its daniel-server compose (a one-shot
`compose run --rm` with no persistent container by design) was left rendered, and the recyclarr
pin commit made the gate poll a container that could never exist. When migrating a service off a
host, delete its `containers/<svc>/docker-compose.yml` on that host (config/ and data dirs can
stay).

## Safety
- Read-only against the repo (no push); rollback is local-only + self-guarding.
- Refuses to *deploy* from a dirty working tree (operator mid-edit) but the tick still
  completes normally and writes `last_run` (`next_action(..., dirty=True) -> "dirty"`) — the
  skip is healthy, not an outage, so it must not trip the GitOps-Alive monitor's stale-file
  threshold. The dirty-tree Discord page is throttled (`should_alert_dirty`) to at most once
  per slot — twice per America/Chicago day, on the first tick at/after 08:00 CT (morning) and
  at/after 20:00 CT (evening) — without it a long edit session would re-page every 30-min tick.
  State: `/var/lib/gitops-deploy/dirty_alerted_date` (holds the `YYYY-MM-DD:am|pm` slot key).
- **Broad changes** (shared `ansible/templates/*`, `inventory/`, `common/`, `deploy.yml`)
  are NOT auto-scoped — the deployer alerts and defers to a manual full deploy.
- **Behind-origin watchdog** (`deploy_logic.behind_marker`): every deferral above leaves the host
  parked on an old tree, and until 2026-08-02 nothing but a Discord message said so — `last_run`
  keeps ticking (Alive green) and `is_diverged` is false (origin is a strict *descendant*, so
  Status green too). daniel-server ran a 12-commit-old tree for hours that way, and the miss only
  surfaced when its un-deployed Pi-hole DNS records were noticed by hand. Each tick now records
  `"<origin_sha> <first_seen_ts>"` to `/var/lib/gitops-deploy/behind_since` when it ends behind
  origin, cleared on convergence, and `monitor-bridge`'s **GitOps Deploy — Status** pages once the
  age exceeds `GITOPS_BEHIND_MAX_MIN` (6 h). Written AFTER `main()` so it reflects the state the
  tick finished in, not the one it started in. The stamp is **not** refreshed per-SHA — a trickle
  of pushes to a stuck host would otherwise restart the clock forever. Age-gated because being
  behind is normal in the small: a push is behind for one tick, and the dirty path is behind for a
  whole edit session by design.
- **Secrets-only pushes** (`ansible/vars/secrets.yml` changed with no service template — a
  rotation pushed from another machine) are fast-forwarded but **not** redeployed: the new
  value only reaches a container on its next deploy, so the deployer alerts (once per SHA,
  `secrets_alerted_sha` marker) to redeploy the consumer(s). `secrets.yml` is deliberately
  NOT in the broad list — the `/add-secret` flow ships it WITH the consuming template, which
  stays a scoped single-service deploy (`deploy_logic.ChangeSet.secrets`).
- **k8s-platform roles (`ansible/roles/k8s/<role>/...`) are defer-and-alert, never auto-deployed.**
  This deployer's path→service mapping (`_ACTIVE_CONFIG`/`_ACTIVE_TASKS`/`_ACTIVE_META`) is
  Docker-platform only — it feeds `deploy(cs.services)`, which is a Docker-role concept. On
  daniel-box, where every `containers_list` entry is `platform: k8s`, a change under
  `ansible/roles/k8s/**` used to match none of those regexes at all: `services_from_changed_paths`
  returned an empty `ChangeSet`, and `main()`'s `if not cs.services:` branch took that as a
  docs-only push — silently `--ff-only` merging a Traefik/Authelia/etc. manifest change with no
  redeploy and no alert (verified 2026-08-13). `deploy_logic._ACTIVE_K8S` now matches the whole
  role dir into `ChangeSet.k8s`, and `alert_deferred` (the same call site tasks/meta already use,
  reached on both the no-services branch and after a successful deploy) alerts on it once per SHA
  (`k8s_alerted_sha` marker) — still `--ff-only` merges, still doesn't deploy. **Compose/GitOps
  mechanisms in this doc — `tasks/`, `meta/deps.yml`, `containers_for()`, the health gate — are
  inert for k8s roles**; they only ever act on `containers/<svc>/docker-compose.yml`, which no k8s
  role renders. Redeploy a k8s change by hand the same way the alert says:
  `uv run ansible-playbook ansible/deploy.yml --tags <svc>` (deploy.yml's k8s play picks up
  `platform: k8s` entries the Docker play filtered out).
- **A service's structural dirs (`tasks/`, `defaults/`, `vars/`, `handlers/`) and `meta/deps.yml`**
  are ff-merged but NOT auto-deployed, so the deployer defers-and-alerts (once per SHA,
  `tasks_alerted_sha` / `meta_alerted_sha`) to redeploy the affected service(s) by hand. `tasks/` and
  the `defaults`/`vars`/`handlers` catch-all (`deploy_logic._ACTIVE_ROLE`) share the `tasks` channel;
  `*.md` (CLAUDE.md/README) stays a silent ff-merge. This fires whether or not the tick deployed something else: a
  *combined* push (svcA's template + svcB's `meta/deps.yml`) deploys svcA but still flags svcB's
  unapplied graph change (`deploy_logic.deferred_service_alerts`, keyed on the not-deployed
  remainder `cs.tasks|meta - deployed`, run on both branches). A service whose own template changed
  rode its scoped `--tags` redeploy, so it's not re-flagged. Only fires on a clean deploy — a
  health-gate rollback git-resets the whole commit, reverting the structural change too.
- Acts **only when origin is strictly ahead of local** (`is_ancestor(local, origin)` →
  `next_action(..., origin_ahead=…)`). Un-pushed local commits make origin an *ancestor* of
  local; that's a no-op, not a deploy — otherwise the tick would diff `local..origin` (the
  *reverse* of those commits) and mis-fire a redeploy + false rollback. Push to clear it.
- **Divergence watchdog** (`deploy_logic.is_diverged`): if local and origin differ yet *neither*
  is an ancestor of the other (e.g. `secret-rotate` committed locally, its push failed, then origin
  advanced), the deployer can't fast-forward and every tick noops while origin's new commits — a
  Renovate/security bump — never deploy. Both other GitOps signals stay green (`last_run` keeps
  ticking, no hold), so each tick writes the diverged SHA to `/var/lib/gitops-deploy/diverged_sha`
  (cleared once resolved) and `monitor-bridge`'s **GitOps Deploy — Status** monitor pages on it.
  A merely-unpushed local commit (`local_ahead`) is NOT flagged — that's the plain no-op above.
- Health-gates **only services deployed on THIS host** — each `has_gitops` host runs its own
  independent instance of this deployer, own git clone, own state dir, own lock, gating only its
  own `containers_list`. A changed template for a service deployed on a DIFFERENT host renders no
  compose here, so `containers_for()` returns `[]` and it's skipped — without this the gate polls
  a phantom container until `HEALTH_TIMEOUT_S` and false-rollbacks (`deploy_logic.containers_to_gate`).
  - **By design: Pi-only services are NOT auto-deployed by GitOps (accepted, 2026-06-30).** The Pi
    has `has_gitops: false`; there is deliberately **no GitOps/CI deploy path to daniel-pi**. A
    change to a Pi-only service (e.g. `wg-easy`/`dozzle` on the Pi) ff-merges and
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
