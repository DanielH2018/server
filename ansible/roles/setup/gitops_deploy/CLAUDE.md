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
- **CI gate — the tip must be green before anything is merged or deployed** (`REQUIRE_CI`,
  `deploy_logic.ci_verdict`). Before this the deployer applied whatever landed on master: nothing
  in the pull path consulted a workflow result, so a red commit reached the homelab on the next
  tick. It queries GitHub's check-runs API **unauthenticated** (public repo) for `origin/master`,
  and only on a tick that would otherwise deploy — a noop/dirty/held tick spends no request, which
  keeps the 60/hour anonymous limit irrelevant at one tick per 30 min.
  - `fail` → `next_action` returns **`ci_failed`**: no ff-merge, no deploy, and a Discord alert
    throttled once per SHA (`ci_alerted_sha`).
  - `pending` → **`ci_pending`**: silent deferral. Unfinished CI is the normal state for the first
    tick after a push, so it logs and retries; only *sustained* behind-ness is a problem.
  - **An unreachable or malformed API reads as `pending`, never `pass`** — the gate fails closed or
    it is not a gate. The tick still completes and writes `last_run`, so a GitHub outage does NOT
    trip GitOps-Alive the way `RetryableFetchError` would.
  - Both outcomes leave the host parked on `local`, which `behind_marker` records — so a
    persistently red master pages through the existing **6h behind-origin watchdog** rather than
    needing an escalation path of its own. That reuse is deliberate; don't add a second timer.
  - **This gates the DEPLOY; branch protection gates the MERGE.** They are not redundant: PR CI is
    scoped to changed files while master runs the full sweep, so a whole-tree failure can only
    appear *after* the merge — which is exactly the window this closes.
  - `cancelled`/`stale` count as **no verdict, not failure**: `ci.yml` sets
    `concurrency: cancel-in-progress` on `github.ref`, so two pushes in quick succession cancel the
    first run. Mapping those to a failure would page on an ordinary back-to-back push.
  - `CI_CONTEXTS` holds GitHub check-run **names**, which must match `ci.yml`'s `name:` exactly
    (that file carries a comment saying so). An empty list or repo **disarms** the gate with a log
    line rather than passing everything — the same fail-closed shape as the k8s denylist. Set
    `gitops_deploy_require_ci: false` to turn it off.
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
- **k8s-platform roles are auto-deployed ONLY for an image-pin bump to a non-denylisted service;
  every other k8s change still defers-and-alerts.** (Changed 2026-08-14 — this bullet used to read
  "never auto-deployed", which is why the paragraph below is written against that older state.)
  Eligibility is decided by `deploy_logic.split_k8s_auto_deploy` and is deliberately **diff-shape
  first, identity second**: gating on the service name alone would not be safe, because
  `_ACTIVE_K8S` matches the WHOLE role dir — a name-only allowlist would auto-deploy configmap,
  `tasks/` and template pushes too, none of which carry Renovate's soak. A service qualifies only
  when the feature is enabled, it is not in `gitops_deploy_k8s_autodeploy_denylist`, the pilot
  scope (if set) names it, the only path the push touched under its role is
  `defaults/main.yml`, and every changed line in that file assigns an `*_image:` var. Everything
  else stays in `ChangeSet.k8s` and behaves exactly as described below.
  - **The gate is in the play, not here.** `roles/k8s/manifests` applies,
    `roles/k8s/rollout-drain` runs `rollout status --timeout`, and
    `ansible/post_tasks/k8s_stabilise_gate.yml` holds the post-Available soak that hard-fails
    on a restart-count delta or a readiness shortfall. (All three lived in
    `roles/k8s/manifests` until 5eea64e6 batched the rollouts and deferred the soak to
    end-of-play.) This deployer adds no health-poll phase for
    k8s: `containers_for()` returns `[]` for a k8s service, which is exactly the 2026-08-08
    configarr false-rollback. The denylist covers the roles that gate can't protect — see
    `defaults/main.yml`, where every entry carries its reason.
  - **A k8s rollback is local-only, and that is not sufficient on its own.** On failure the tick
    holds the bad SHA, `git reset --hard`es, redeploys the prior pin, and pages. But `skip_hold`
    matches only while `origin_head == hold_sha`, so **the bad pin is still on master** — the next
    push past the held commit redeploys it. The Discord page says so; the fix is a revert on the
    remote (or a Renovate `allowedVersions` pin), not just clearing the hold.
  - **A clean k8s tick is the second place `hold_sha` clears.** `write_hold(None)` also sits in the
    Docker health-gate branch, which an all-k8s host never reaches — without this the first
    rollback would leave **GitOps Deploy — Status** red permanently and need a manual `rm`.
  - **One tick promotes at most `gitops_deploy_k8s_autodeploy_max_per_tick` services (default 3).**
    Every promoted service shares a single `ansible-playbook` run under one
    `K8S_DEPLOY_TIMEOUT_S`, and the failure path resets the whole merged range — so an
    overlong batch discards the good bumps with the bad one. The surplus stays in `ChangeSet.k8s`
    and defer-and-alerts, which posts a Discord message naming the services to deploy by hand.
    **It is not retried on a later tick**: the ff-merge runs before the deploy, so a successful
    tick leaves `local == origin` and every subsequent `next_action()` is a noop. The pilot list
    used to bound this at one service; since it was cleared (2026-08-16) the cap is the only
    bound, which is what it was written for.
  - **The pilot list is empty, so the denylist alone decides.** `gitops_deploy_k8s_autodeploy_pilot`
    scoped eligibility to `speedtest` from 2026-08-14 until slice 3 cleared it. An empty list means
    "every non-denylisted service", not "none" — the opposite of the empty-denylist guard right
    above it, which disarms the feature. Six services were added to the denylist in the same commit
    (qbittorrent/bazarr/tdarr, livesync, valheim, valheim-stats): each matched a published exclusion
    class already, and was outside the list only because the pilot made the list non-binding.
    `ansible/tests/test_k8s_autodeploy_guard.py` now enforces the three role shapes that must never
    be eligible — a rendered Deployment whose name isn't in the gated set (a role gating a second
    Deployment by name via `manifests_extra_rollouts` is fine; a name that can't be resolved
    statically counts as ungated), `manifests_rollout: ''`, and a gated Deployment with no
    `readinessProbe`.
  - **The deploy is time-bounded by `K8S_DEPLOY_TIMEOUT_S`, not `RUN_BUDGET_S`.** The latter feeds
    `gate_services()` — the Docker gate — and is inert here, so without an explicit timeout the
    only bound is systemd's `TimeoutStartSec` SIGTERM, which can land mid-rollback.
  - Promotion is refused when the tick also carries Docker services: the k8s branch returns before
    the Docker deploy would run. No host is mixed today.

  The original rationale, still accurate for every non-eligible k8s change:
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
