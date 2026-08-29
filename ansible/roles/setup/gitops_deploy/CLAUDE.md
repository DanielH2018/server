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

## Triggering a tick by hand

The procedure, how to read a tick that logged nothing, and why the wrapper exists rather than
a plain `systemctl start` are in the **`gitops-tick` skill**. The short version: it runs the
identical code path the timer runs, so there is **no dry-run mode**, and an uneventful tick
logs nothing — check `last_run` rather than the journal before concluding it did not run.

This is a *convenience* path, not the activation path: the role's `Run gitops-deploy once`
handler already kicks a run whenever the script, config or units change, so provisioning stays
fully IaC.

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
  - **This gates the DEPLOY, and it is the only gate.** This line read "branch protection gates
    the MERGE" until 2026-08-29, when `gh api repos/DanielH2018/server/branches/master/protection`
    was found to return 404 — there is no branch protection on `master`. The gate's value is
    unchanged and its necessity is higher than the old wording implied: PR CI is scoped to changed
    files while master runs the full sweep, so a whole-tree failure can only appear *after* the
    merge, and nothing else stops it landing. Adding branch protection is a separate decision.
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
- **Broad changes split three ways** (`deploy_logic._BROAD_*_PREFIXES`). A setup-plane change
  under `roles/setup/<name>/` (or `requirements.yml`) fast-forwards and applies as
  `initial_setup.yml --tags <name>`, with the tag derived by `setup_tags_for` rather than left as
  the `<role>` placeholder `broad_remediation` prints. A deploy-plane change (shared
  `ansible/templates/*`, `inventory/`, `common/`, `deploy.yml`) fast-forwards and applies as a
  full `deploy.yml`.
  - **The ff-merge happens BEFORE the apply**, which is the order the *Deploying this role under
    the shared tree lock* trap below already prescribes for the manual path: applying first
    renders from the pre-merge tree and deploys nothing. It also means an unrelated commit sharing
    the tick lands even when the apply fails — stranding a docs-only commit behind somebody else's
    setup change was the original complaint this arm fixes.
  - **Both arms are FORWARD-ONLY.** `deploy_logic.broad_budget_ok` showed a rollback re-run did
    not fit: 180s max flock + 1212s forward (measured 2026-08-22) + 1212s rollback against
    `TimeoutStartSec=2700` left 96s, and a rollback SIGTERMed partway is worse than none.
    **The budget argument expired on 2026-08-29** — the staging gate raised the ceiling to 60min,
    at which the same numbers fit (2904 against 3600). The arm is still forward-only, on the
    second half of the argument alone: funding a broad rollback needs a fresh deploy.yml
    measurement against today's tree, not the slack a ceiling raise left behind. Nothing changed
    behaviour when the ceiling moved — `broad_budget_ok` has no production caller. A
    failure writes `hold_sha` **and** `hold_plane`, alerts saying nothing was rolled back, and
    leaves the tree fast-forwarded. It deliberately does not `git reset` — resetting without
    redeploying would leave the tree claiming the old commit while live state is half-new.
  - **`_BROAD_MANUAL_PREFIXES` keeps the old defer-and-alert with no ff-merge**:
    `roles/setup/gitops_deploy/`, `bootstrap.yml`, `k3s-bringup.yml`, `initial_setup.yml`.
    Applying this role runs a playbook whose handler restarts the unit executing the tick — a
    self-modification defect no timeout makes safe. Staying parked is also what keeps
    `behind_since` set, which is the only durable signal that an unapplied plane exists. A
    setup-plane change whose tag cannot be derived joins them, since the only automatic
    alternative is an unscoped `initial_setup.yml` (a whole-host reprovision).
  - `BROAD_DEPLOY_TIMEOUT_S` (1800, `gitops_deploy_broad_timeout_s`) bounds one apply. Without it
    a wedged run is SIGTERMed at `TimeoutStartSec` with no hold written and no alert sent.
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

    That denylist is no longer hand-maintained: `filter_plugins/k8s_autodeploy.py` derives it
    from each role's own `k8s_autodeploy` declaration in `roles/k8s/<name>/defaults/main.yml`,
    where the reason sits beside the role it describes. The filter fails closed — a role that
    declares nothing raises at template time rather than dropping out of the denylist, since
    absence from it means auto-deployable. To stop a role auto-deploying, set
    `k8s_autodeploy: false` in its defaults; there is no central list to edit.

    That edit alone does not reach the host. It lands under `roles/k8s/<role>/`, so the
    deployer routes it to `ChangeSet.k8s` and its defer-and-alert names `ansible/deploy.yml
    --tags <svc>` — `deploy.yml` runs no setup role, so it never re-renders
    `/etc/gitops-deploy/config.env`, and the denylist on the host stays the old one while the
    role you just denied is still auto-deployable. Run
    `uv run ansible-playbook ansible/initial_setup.yml --tags gitops_deploy` to push the
    change. This is the same silent-no-op class `broad_remediation()`'s docstring warns about
    (`deploy_logic.py` around line 177) — naming `deploy.yml` for a setup-plane change leaves
    it unapplied while a plain ff-merge clears the divergence.

    The deployer detects this itself instead of relying on an operator to remember it. Each
    tick, `k8s_declarations_at(origin)` reads every role's `defaults/main.yml` at the SHA the
    tick already pinned for the diff and the alert — not a re-resolved `origin/<branch>`, which
    would open a TOCTOU against a concurrent fetch — and `declared_denylist()` parses it with a
    stdlib regex, since the unit runs under `uv run --no-project` and cannot import `yaml` or the
    filter plugin. If that result disagrees with `K8S_AUTODEPLOY_DENYLIST`, or can't be read at
    all, k8s auto-deploy is disarmed for that tick. Both mismatch directions usually mean the
    same thing — config is behind origin — since a role can be denied there OR promoted there;
    either way the page leads with the re-render (`ansible/initial_setup.yml --tags
    gitops_deploy`), and only when a role was removed from the denylist does it also name the
    secondary cause, an operator who rendered locally before pushing (`git push` it). It includes
    the read exception's type and message when the declarations couldn't be read at all. The disarm
    itself is stateless: it is recomputed every tick, so it self-clears the moment the config is
    re-rendered. Only the page is throttled, on `STALE_DENYLIST_FILE`. The regex is deliberately
    biased toward denied — unanimity is required across every match, an absent or unparseable
    declaration counts as denied, and a shared role skips the check entirely — so a parsing bug
    here almost always produces a spurious disarm rather than a permitted deploy. The one gap is
    a file that is invalid YAML overall, which the regex can still read a value out of but the
    filter would raise on rather than deny; CI is what closes it, not the regex's own bias —
    `ansible/tests/test_denylist_parsers_agree.py` runs the filter against the live tree and
    fails on exactly that shape, and `REQUIRE_CI` refuses to promote a red tip.
  - **The gate is in the play, not here.** `roles/k8s/manifests` applies,
    `roles/k8s/rollout-drain` runs `rollout status --timeout`, and
    `ansible/post_tasks/k8s_stabilise_gate.yml` holds the post-Available soak that hard-fails
    on a restart-count delta or a readiness shortfall. (All three lived in
    `roles/k8s/manifests` until 5eea64e6 batched the rollouts and deferred the soak to
    end-of-play.) This deployer adds no health-poll phase for
    k8s: `containers_for()` returns `[]` for a k8s service, which is exactly the 2026-08-08
    configarr false-rollback. The denylist covers the roles that gate can't protect — see each
    role's own `roles/k8s/<name>/defaults/main.yml`, where its `k8s_autodeploy_reason` carries
    the reason.
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
  - **Accepted, not fixed here: the batch-abort blast radius is this branch's most likely bad
    day.** `K8S_AUTODEPLOY_MAX_PER_TICK` (3) and `ansible/tasks/k8s_batch.yml` share one
    `ansible-playbook` run with no `rescue` — so one service's revert failing during a rollback
    aborts the WHOLE batch's rollback, not just that service's. Every co-batched service that had
    already been reset for its own revert is left mid-rollback: on the failed commit's manifests
    (or worse, mid-revert with a volume still attached in maintenance mode) over migrated data —
    exactly the state this slice exists to prevent, reached in a single tick rather than needing
    a separate independent failure. The proper fix is per-service invocation on the rollback
    path (each service's rollback in its own `ansible-playbook` run, so one failure can't abort
    a sibling's), not attempted in this pass. See the rollback-timeout section below for the
    same batching mechanism's effect on `K8S_ROLLBACK_TIMEOUT_S`.
  - **Accepted: a silently skipped snapshot is only an Ansible `debug` line, and that is what
    makes the batch-abort item above reachable in a single tick.** When
    `k8s/volume-snapshot`'s maintenance-mode attach fails on a detached volume, the deploy
    proceeds unprotected with a loud `ansible.builtin.debug` warning — but nothing forwards that
    warning to Discord, and a later rollback's revert-status note
    (`rollback_volume_revert_note`) names a service as one the revert targets purely by reading
    its role DEFAULTS (`k8s_autodeploy_snapshot_pvcs`), not what the snapshot phase actually did
    on THIS run. So an operator reading the rollback alert has no signal that this particular
    snapshot never happened, and no reason to suspect the revert that follows will fail loudly
    rather than silently do nothing.
  - **Accepted: nothing self-heals a volume left attached in Longhorn maintenance mode after a
    failed rollback, and the next deploy's seed pod mounts the same RWO claim.** If
    `k8s/volume-revert` stops partway (a wait exhausts, an API call fails) the volume can be left
    attached with `disableFrontend: true` and the workload at zero replicas — see
    `k8s/volume-revert/CLAUDE.md`'s manual-recovery steps. `k8s/seed-volume` runs ahead of
    `k8s/manifests` on every one of the 13 opted-in roles' NEXT deploy and mounts the same claim
    to seed it; whether that mount succeeds, hangs, or fails against a volume already attached in
    maintenance mode by a different (non-pod) attachment is untested — nothing in this repo
    exercises a real Longhorn revert (see `k8s/volume-revert/CLAUDE.md`'s "What is not covered by
    tests"). Treat a stuck maintenance-mode attach as blocking the affected service's next deploy
    until cleared by hand, not as something the pipeline will route around on its own.
  - **The pilot list is empty, so the denylist alone decides.** `gitops_deploy_k8s_autodeploy_pilot`
    scoped eligibility to `speedtest` from 2026-08-14 until slice 3 cleared it. An empty list means
    "every non-denylisted service", not "none" — the opposite of the empty-denylist guard right
    above it, which disarms the feature. Six services were added to the denylist in the same commit
    (qbittorrent/bazarr/tdarr, livesync, valheim, valheim-stats): each matched a published exclusion
    class already, and was outside the list only because the pilot made the list non-binding.
    The `ansible/tests/test_k8s_autodeploy_*.py` family now enforces the three role shapes that must never
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
`files/test_deploy_logic*.py` covers `deploy_logic.py`, split by which decision each group is
about: `_diff` (path→service mapping), `_git` (the next-action decision, divergence, and
`container_names()` — the health gate inspects every `container_name:` in the changed service's
rendered compose, since a role often runs several containers and the bumped image's container is
usually not the role-named one), `_health` (health gating and the Discord queue), `_inventory`
(declared services and platform routing), and `test_deploy_logic.py` itself (auto-deploy
eligibility, the CI gate, rollback). Run via the repo pytest hook
(`uv run pytest ansible/roles/setup/gitops_deploy/files`).

## Traps

### Deploying this role under the shared tree lock self-deadlocks
Do not wrap `initial_setup.yml --tags gitops_deploy` in
`flock /var/lock/server-git-tree.lock`. "Run gitops-deploy once" is a handler
(`handlers/main.yml:11`), notified by five tasks in `tasks/main.yml`, and it invokes the
deployer, whose systemd ExecStart is
`flock -w 180 -E 75 /var/lock/server-git-tree.lock …`. Holding the lock from outside makes the
smoke run wait its full 180s and deploy nothing.

**Since 2026-08-23 that failure is silent.** `-E 75` plus `SuccessExitStatus=75`
(`gitops-deploy.service.j2:75`) make systemd report the unit `Result=success`, so
`handlers/main.yml:11-16`'s `ansible.builtin.systemd: state: started` returns rc 0 and the play
recaps green. Nothing deployed, `last_run` untouched, no Discord message (the webhook belongs to
the deployer, which never started), no `OnFailure`. The first alert of any kind is GitOps-Alive,
once `last_run` ages past `GITOPS_MAX_AGE_S` — up to 90 minutes later (`check.py:148`). The one
immediate signal is the unit's `ExecStopPost` marker in the journal, `tick skipped (lock
contention)`, which `scripts/deploy_tools/gitops_tick.sh` also reads to exit 3.

Observed 2026-08-20, *before* that change, when contention still failed the play:
`PLAY RECAP … changed=2 failed=1`, with `gitops_deploy : Run gitops-deploy once` timing at
**180.30s** — the flock wait, not a slow task. The config had already rendered, so only the smoke
run failed. Re-running with no outer flock succeeded (`ok=10 changed=0 failed=0`). That recap is
kept as the empirical proof of the mechanism: a non-zero flock exit did propagate, which is
exactly why a zero one now yields `ok`.

Run it unlocked. The deployer takes the lock itself, which is what serializes it against the
30-minute timer; the outer flock adds nothing and converts serialization into a silent no-op.
This is a counter-example to the repo-root `CLAUDE.md` guidance that a deploy should take the
tree lock, and it applies to any role whose tasks re-enter a lock-taking command.

Because the smoke run is a handler, it fires only when something changed. A no-op run
(`changed=0`) never invokes the deployer and cannot deadlock however it is wrapped —
verified 2026-08-20 19:22, `ok=10 changed=0 failed=0` with no smoke-run task in the recap.
So a missing smoke-run step is evidence that nothing needed re-rendering, not evidence of a
broken or incomplete run.

Separately, the deployer's broad-change alert says to run the remediation playbook and *then*
`git merge --ff-only`. That order renders from the pre-merge tree and deploys nothing.
Fast-forward first, then run the playbook.

### Moving a config source changes which remediation the alert prescribes
`/etc/gitops-deploy/config.env` is rendered only by
`initial_setup.yml --tags gitops_deploy`. `deploy.yml` runs no setup role, so a change that
must reach that file is unapplied by a `deploy.yml` run — while a plain ff-merge clears the
divergence and every repo-side check reads green. `broad_remediation()`'s docstring names
this: *"naming deploy.yml there is a silent no-op that leaves the change unapplied"*
(2026-07-16 review M1).

The remediation the deployer prescribes is decided by the path you edited, not by what the
edit affects. Slice 1b (PR #290) derived the k8s auto-deploy denylist from each role's
`k8s_autodeploy` declaration instead of a CSV in this role's defaults — same value, same
rendered `config.env` line, different edit site:

| edit site | routes to | alert names | re-renders config.env? |
|---|---|---|---|
| `roles/setup/gitops_deploy/defaults/main.yml` (old CSV) | `_BROAD_SETUP_PREFIXES` | `initial_setup.yml --tags <role>` | yes |
| `roles/k8s/<role>/defaults/main.yml` (declaration) | `ChangeSet.k8s` | `deploy.yml --tags <svc>` | **no** |

So denying a role from auto-deploy leaves it auto-deployable on the host, and the alert names
the command that cannot fix it. It fails on a safety-tightening edit, which is the worst
direction. Before moving any value that lands in a host config file, check which `_ACTIVE_*`
regex its new path matches and what `broad_remediation()` says for that plane.

A second instance appeared during slice 2 (PR #292), a different mechanism with the same
failure. The stale-denylist alert added in slice 1c split on the direction of the set
difference: `added` (denied at origin, absent from config) → "config is behind, re-render";
`removed` (in config, not denied at origin) → "config is ahead — `git push` it". That second
branch assumed one cause and has two. An operator rendering locally before pushing is one.
The other is a promotion: a role leaving the denylist at origin shrinks the set, producing
the identical signature while meaning the opposite. Promoting `node-exporter` was the first
change to trigger it, so the host would disarm auto-deploy fleet-wide and page with
`git push`, which does nothing; the alert is throttled per origin SHA, so it re-fires on
every later push while the host stays stale.

A set difference tells you what diverged, never why, so a remediation inferred from its
direction is a guess. On a pull-based host origin is the source of truth, so the re-render is
the right lead in both directions and the push case belongs as a secondary check. Fixed in
`7f5f629b`. The alert-direction logic lives in `gitops_deploy.py`'s `main()`, which no test
imports — the `test_deploy_logic*.py` family covers only `deploy_logic.py`.

`test_deploy_logic.py` itself also covers `deploy_k8s()` and its two call sites inside `main()`.
`gitops_deploy.py` can't be imported the normal way in CI (`C = cfg()` reads
`/etc/gitops-deploy/config.env` at module level, which doesn't exist there) — it stubs
`host_lib.parse_env_file` with canned values before the one import, so it works the same in CI
and on a host where the real config.env exists, and never reads the real file. The rollback call
site itself (inside `main()`, not unit-testable without mocking git/CI/Discord/state-file I/O) is
covered by an AST source-check pinning that it passes `restore_sha=origin[:8]` EXACTLY (not a
prefix check — the full 40-char `origin` also starts with `"origin"` and would match no snapshot,
since volume-snapshot names with `git rev-parse --short=8`) and never `local`, and its own
`K8S_ROLLBACK_TIMEOUT_S` budget, not the forward deploy's `K8S_DEPLOY_TIMEOUT_S`.

**Accepted, docs reconciled rather than code changed: `origin[:8]` is a fixed 8-character slice
of the full 40-char SHA, and `git rev-parse --short=8` is a MINIMUM width, not a fixed one.**
`origin = run(["git", "rev-parse", f"origin/{BRANCH}"])` returns the full 40-char SHA (no
`--short`); `origin[:8]` truncates it to exactly 8 characters, always. `k8s/volume-snapshot`
names its snapshots from `git rev-parse --short=8 HEAD`'s raw stdout, which is git's shortest
UNAMBIGUOUS abbreviation — 8 characters in the overwhelmingly common case, but MORE when 8
collides with another object in the repo's history, and both `k8s/volume-revert/CLAUDE.md` and
`k8s/volume-snapshot/CLAUDE.md` say plainly that the role "uses it verbatim" / "transforms
nothing", because truncating a longer short-SHA back down to 8 would build a snapshot-name
prefix that no longer matches. That is exactly what `origin[:8]` does at this one call site.
Since git's longer abbreviation is always a superset-prefix of the shorter one, the two agree in
the common case (8 chars is already unambiguous) and diverge only in the rare case where it
isn't — at which point `origin[:8]`'s prefix stops matching one character short of the real
snapshot name (a literal `-` in the constructed prefix lands where the real name has a 9th hex
digit instead), and `k8s/volume-revert`'s "no snapshot matches this deploy" assert fires —
loud, and before the scale-down, the same safe failure mode as every other unmatched-prefix
case. Probability is negligible for this repo's history size; not changed here because the
failure mode is already the correct, safe one and `restore_sha=origin[:8]` is pinned by the test
above — this note exists so the inconsistency between this call site and the two roles' own
"never truncate" documentation is not mistaken for an oversight.

`deploy_logic.declares_snapshot_claims()` and `rollback_volume_revert_note()` are pure and fully
unit-tested: they decide the rollback alert's one revert-status line — whether the rollback
redeploy itself failed (in which case the note says the revert task may never have run, not that
it was attempted), and, when it succeeded, which of the batch's services actually declare
`k8s_autodeploy_snapshot_pvcs` (only those revert; the alert must not imply the rest did too).
`gitops_deploy.read_local_k8s_default()` is the one line of I/O feeding it — a plain file read
against the working tree AFTER `git reset --hard local`, matching exactly what
`roles/k8s/manifests` itself reads for the claim list.

## Rollback timeout (`K8S_ROLLBACK_TIMEOUT_S` / `gitops_deploy_k8s_rollback_timeout_s`)
The rollback redeploy also reverts each claimed volume to its pre-deploy snapshot
(`k8s/volume-revert`), which is strictly more work than the forward deploy — per claim, worst
case is 3 state waits + 3 API calls against Longhorn, and `tdarr`/`code-server` each hold two
claims. It gets its own timeout rather than sharing `K8S_DEPLOY_TIMEOUT_S`.

**Re-sized (round 2 of this slice's fix pass) from 900s to 1320s: the prior number never named
the forward apply's own rollout wait, and was never checked against the true worst promoted
service.** The 900s figure (task 6b) budgeted 720s for a two-claim revert plus 180s of
unnamed "the rest" — which silently assumed the forward apply's own rollout wait
(`manifests_rollout_timeout`) fit inside that 180s. It defaults to 300s, but `radarr`/`sonarr`
override it to 660s for first-boot DOCKER_MODS installs — already more than the unnamed
residual on its own. Computed per currently-promoted (`k8s_autodeploy: true`), claim-declaring
role, at task 6b's drill-sized `volume_revert_state_timeout`/`api_timeout` (90/30) and
`volume_snapshot_timeout` (120):

```
ceiling = claims x (volume_snapshot_timeout + 3xvolume_revert_state_timeout + 3xvolume_revert_api_timeout)
          + manifests_rollout_timeout + k8s_rollout_stabilise_seconds
        = claims x 480 + manifests_rollout_timeout + 60
```

`radarr`/`sonarr` (1 claim, 660s rollout) come to 1200s — the number an earlier draft of this
fix used as *the* ceiling, because they are the only roles with a non-default rollout timeout
and so the most visible case. `tdarr` (2 claims, default 300s rollout) is actually worse:
2x480 + 300 + 60 = **1320s**. `K8S_ROLLBACK_TIMEOUT_S` is set to 1320 to cover the true worst
case among currently-promoted services, not the loudest one.
`ansible/roles/setup/gitops_deploy/files/test_gitops_discord_contract.py::test_k8s_rollback_budget_covers_the_worst_single_promoted_service`
computes this from role sources rather than pinning a number, so a future rollout-timeout bump
or a new promoted claim-declaring role fails it instead of silently under-sizing the budget.

**NOT covered by this number: two or more claim-declaring services landing in the SAME batch.**
One tick can promote up to `gitops_deploy_k8s_autodeploy_max_per_tick` (3) services into a single
`ansible-playbook` run. Inside that run, each role's snapshot+revert phase runs in sequence — only
the ROLLOUT WAIT is deduped/batched across services, via `roles/k8s/rollout-drain`'s
`max()`-not-`sum()` drain — so a batch with two claim-declaring services stacks their
snapshot+revert costs additively. Two `radarr`/`sonarr`-shaped services batched together would
need roughly 2x480 + 660 + 60 = 1680s, already past 1320s. This is the same mechanism as "The
batch-abort blast radius" below (`K8S_AUTODEPLOY_MAX_PER_TICK` is 3, one shared run, no rescue);
the proper fix for both is per-service invocation on the rollback path rather than a larger
constant here — not done in this pass.

**Also newly true at 1320s: a rollback redeploy pays an EXTRA full Recreate cycle.** The revert
leaves the workload at zero replicas; the apply that follows restores `replicas: 1` from the
manifest, which starts the pod — but the reset tree (from `git reset --hard local`) makes
`manifests_render` register as `changed` for every role in the batch, so the "Roll the
deployment after a config change" task ALSO fires and tears that just-started pod straight back
down for a `rollout restart`. Roughly a minute (see the scale-down/attach cycle timings in
`ansible/roles/k8s/volume-revert/CLAUDE.md`), charged exactly when the budget is tightest —
right after the revert's own worst-case cost. Folded into the 60s stabilisation term above only
by coincidence of rounding, not by design; not separately budgeted.

**Corrected (task 6b, second pass): a failure aborts the play, so failure-driven worst
cases from different phases cannot stack.** `k8s/volume-revert`'s three waits are `until:` with
no `ignore_errors` and no `failed_when`, and `k8s/manifests` runs
snapshot → revert → apply → rollout as one play with no `rescue`. An exhausted wait fails its
task and the WHOLE PLAY stops there — a snapshot-phase failure means `k8s/volume-revert` never
runs at all, and a claim-1 failure means claim 2 never runs. So a "snapshot phase fails at its
240s ceiling AND THEN the revert phase also fails at its 720s ceiling" scenario cannot happen:
the first failure ends the run. An earlier draft of this note added those two independent
FAILURE-driven worst cases together and concluded 900s wasn't provably safe — that was wrong,
for the reason above.

**What 900s does need to cover, and does only partially: a SLOW BUT FULLY SUCCESSFUL run.**
`worst_case_revert` (720s for two claims) is reached either by every step succeeding right at
its own ceiling, or by the very last wait failing after everything before it also succeeded
slowly — both consume the same wall time, so 720s is a real ceiling on "how long before this
role's fate (success or failure) is decided," not an unreachable one. On a genuinely slow but
ultimately successful run, the pre-revert snapshot wait (`volume_snapshot_timeout`, up to 120s
per claim) and the post-apply rollout wait are ADDITIVE to the revert on one continuous
timeline — nothing fails, so nothing stops the play early. That combined slow-success total for
a two-claim service (up to 240s snapshot + 720s revert + apply + rollout) can exceed 900s, and
`K8S_ROLLBACK_TIMEOUT_S` would SIGTERM a run that was genuinely still succeeding, just slowly.
The 180s left after the 720s revert covers that additive cost only at its REALISTIC size (~38s
measured for one claim), not at every phase's own ceiling. This is the real, smaller residual:
not a compounding-failure risk, but a slow-full-success risk, and it is still open.

**The reachable failure mode is bounded by the first exhausted wait, and the timeout was never
what stood between it and partial state.** When a wait does exhaust, the play stops right there
— workload at zero, that claim's revert incomplete — which is exactly the manual-recovery case
`k8s/volume-revert/CLAUDE.md` already documents. Raising or shrinking
`gitops_deploy_k8s_rollback_timeout_s` does not change whether that failure happens; it only
changes whether a SLOW SUCCESS gets cut short.

**The forward attempt and the rollback run sequentially, not concurrently, inside one systemd
unit activation.** A failed forward deploy can spend its full `K8S_DEPLOY_TIMEOUT_S` (900s)
before `gitops_deploy.py` gives up on it; the rollback that follows can then spend its full
`K8S_ROLLBACK_TIMEOUT_S` (1320s, re-sized above). `gitops-deploy.service.j2`'s `TimeoutStartSec`
was raised from 25min to 35min (task 6b), then to 45min so 180s max flock wait + 900 + 1320 =
2400s fits with margin — see that template's own arithmetic comment for both the Docker-path and
k8s-path budgets it now covers.

**With the staging gate armed, two more budgets join that same sequence, which is why the
ceiling is 60min.** `consult_staging` runs inside `main()`'s `if cs.k8s_deploy:` block, ahead of
`deploy_k8s`, so `STAGING_GATE_TIMEOUT_S` (600s) and `STAGING_EXPECT_TIMEOUT_S` (120s) are
additive to the pair above rather than alternative to them: 180 + 600 + 120 + 900 + 1320 = 3120s
against 3600s. Both are sized from a measured staging deploy — a full six-service run of the
whole `STAGING_SUBSET` took 130s cold and 53s warm on 2026-08-29, so 600s is ~4.6x the cold
case — and `defaults/main.yml` carries the measurement. Under-sizing them does not fail safe: a
staging consultation that times out reports NO VERDICT, indistinguishable from a staging that is
down, and slice 4's entry condition is a measured false-failure rate.

**Consequence for the lock, newly true at 1320s: this unit's own lock hold can now exceed the
30-minute timer interval, where at 900s it landed exactly at the edge (900 + 900 = 1800s = 30min
flat) without crossing it.** `ExecStart` wraps the whole run in `flock -w 180
/var/lock/server-git-tree.lock` — the same lock `./scripts/deploy.sh` and the weekly
secret-rotate cron take. In the pathological case (a stalled forward deploy followed by a
stalled rollback), this unit can hold that lock for up to 2940s (600 + 120 + 900 + 1320 with the
staging gate armed, 2220s without it, excluding its own flock wait) — past the 30-minute (1800s)
timer interval. A concurrent `./scripts/deploy.sh`
during that window waits `LOCK_WAIT=3000` (`deploy.sh:57`, used at `:286`) — **not** the unit's
own `-w 180`, which governs only the deployer — so it **outlasts the 2940s hold and then
deploys**, rather than returning exit 75. It returns exit 75 only if the lock stays busy past
the full 3000s. The secret-rotate cron waits on the same lock rather than failing outright,
which is true only because its `flock -w` is likewise 3000s and so clears that 2940s hold. The
cron's was 1200s until 2026-08-22, at which point this paragraph was wrong in the direction that
matters: the cron gave up mid-incident and skipped that week's rotation, with no retry until the
next weekly tick.

Both 3000s waits are derived from the same four timeouts and are machine-pinned against them —
`files/test_gitops_discord_contract.py`, `test_secret_rotate_lock_wait_clears_the_deployers_worst_case_hold`
and `test_deploy_sh_lock_wait_clears_the_deployers_worst_case_hold`, which share one
`_worst_lock_hold()` derivation so the two waits cannot disagree. Raising any of
`gitops_deploy_k8s_timeout_s`, `gitops_deploy_k8s_rollback_timeout_s`,
`gitops_deploy_staging_gate_timeout_s` or `gitops_deploy_staging_expect_timeout_s` fails those tests rather
than silently shortening an operator's wait, which is how `deploy.sh`'s copy rotted once already
(it sat at 1500 through two `TimeoutStartSec` bumps).

Neither wait is silently wrong — both correctly report "the lock stayed busy" — but an operator
seeing exit 75 during this window should check whether gitops-deploy is mid double-timeout before
assuming the lock is stuck.

**Consequence for the operator: a pathological double-timeout run can overrun the 30-minute
timer tick — verified live against the real unit, not inferred from the man page alone.**
`systemctl show gitops-deploy.service -p ActiveEnterTimestamp` on daniel-box returns EMPTY: a
`Type=oneshot` service with `RemainAfterExit=no` never enters the "active" state at all — it
goes inactive → activating → inactive directly. `systemctl show gitops-deploy.timer -p
LastTriggerUSec` matches this unit's own `ExecMainStartTimestamp` from the same run — so the
30-min `OnUnitActiveSec` interval is measured from when the timer last STARTED the unit, not
from when it finished. (An earlier draft of this note claimed the opposite — that the interval
resets from completion — which this measurement disproves.)

What that means for an overrunning run: the next scheduled elapse (also start-time-based, so it
recurs every ~30min through the overrun) finds the unit already `activating` and, per `man
systemd.timer`, "it is not restarted, but simply left running. There is no concept of spawning
new service instances in this case" — the mechanism is systemd coalescing the new start job into
the one already in flight, the same behavior whether the man page's literal wording is `active`
or `activating`. **Proven: an overrun never produces a second, overlapping, or concurrent run.**
Not verified here: whether each absorbed tick still advances `LastTriggerUSec` (so the next
elapse keeps recomputing every ~30min throughout the overrun) or whether the schedule is left
alone until the run finishes — either way, no second execution happens, so the distinction
doesn't change the safety property, only how quickly the first tick after a long overrun lands.
Belt and suspenders regardless: `ExecStart` sits behind `flock -w 180`, so even a hypothetical
second invocation would block up to 180s on the lock and then exit rather than interleave with
the git tree the first run is still using. **It does not alert.** `-E 75` plus
`SuccessExitStatus=75` make systemd report `Result=success` on lock contention, so no
`OnFailure` fires and nothing pages — see the *Deploying this role under the shared tree lock*
trap above, which documents the same mechanism as a silent no-op. This paragraph said "fail
cleanly (exit 1, alerting via `OnFailure`)" until 2026-08-24 (review L-4), contradicting the
corrected text at the head of this file: PR #392 fixed the head and left the tail.
