# gitops_deploy — pull-based deploy on master change (every has_gitops host)

Installs a systemd **timer** (every `gitops_deploy_tick_interval`, 10 min) that runs
`/opt/gitops-deploy/gitops_deploy.py` as `{{ sys_user }}`. The script fetches `origin/master`;
if it advanced, it maps each changed path to a service or a plane, `--ff-only` merges, and
deploys what it is allowed to via `uv run --frozen ansible-playbook` (the repo-pinned env;
`uv` must be on the unit's PATH). A config-only change triggers a scoped, health-gated
redeploy too; `tasks/` and a role `CLAUDE.md` are deliberately NOT auto-deployed.

This file carries the rules. The incidents each arm was added after, the measurements each
budget was sized from, and the accepted trade-offs are in `docs/gitops-pipeline.md` under
*The deployer's record*; read that before re-deriving any of them.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates or playbook entry. -->
- **Applied by:** `initial_setup.yml --tags "gitops_deploy"`
- **Timers (4):** `gitops-deploy.timer` (`OnBootSec=10min`, `OnUnitActiveSec={{
  gitops_deploy_tick_interval }}`), `staging-backfill.timer` (`OnBootSec=20min`,
  `OnUnitActiveSec=1h`), `kuma-check-github-ruleset-drift.timer` (`OnCalendar=*-*-* {{ '%02d' |
  format(gitops_deploy_ruleset_drift_cron_hour | int) }}:{{ '%02d' |
  format(gitops_deploy_ruleset_drift_cron_minute | int) }}:00`),
  `kuma-check-github-interaction-limit.timer` (`OnCalendar=*-*-* {{ '%02d' |
  format(gitops_deploy_interaction_limit_cron_hour | int) }}:{{ '%02d' |
  format(gitops_deploy_interaction_limit_cron_minute | int) }}:00`)
<!-- /generated_from -->

## A host with `has_gitops: false` is reaped, and the code refuses on its own

`tasks/main.yml` dispatches on `has_gitops`: `install.yml` on the deployer, `teardown.yml`
everywhere else. `teardown.yml` removes every unit, cron, script and directory `install.yml`
creates (the `DECIDED:` at that task says why the state directory goes too);
`ansible/tests/setup/test_gitops_deploy_reaps_on_non_deployer.py` derives both censuses from
`install.yml`. The teardown reaches a non-deployer host only by hand — `initial_setup.yml
--tags gitops_deploy`, with `-e target=daniel-pi` for the Pi.

The code carries the same gate. `deploy_phases.refuse_unless_deployer` reads this host's own
`host_vars/<hostname>.yml` at the top of `main()`, ahead of every state write, and raises
`NotTheDeployerHost` on a top-level `has_gitops: false`; `entrypoint()` turns that into one
journal line and exit 0 — no Discord post, no `last_run`. It fails open on every other shape
(no host_vars file, the key absent, an indented or commented-out occurrence): a false refusal
on daniel-box parks every landing in the fleet.
`tests/test_gitops_deploy_not_the_deployer.py` pins both halves.

## Triggering a tick by hand

The procedure is the **`gitops-tick` skill**. It runs the identical code path the timer runs,
so there is **no dry-run mode**, and an uneventful tick logs nothing — check `last_run` rather
than the journal. The role's `Run gitops-deploy once` handler already kicks a run whenever the
script, config or units change, so provisioning stays fully IaC.

## Health gate + rollback

After a Docker deploy it polls each container's health (`HEALTH_TIMEOUT_S`). On failure it
`git reset --hard`es to the previous HEAD, redeploys the prior version, writes the bad SHA to
`/var/lib/gitops-deploy/hold_sha`, and alerts the dedicated Discord webhook. The marker (and
the red **GitOps Deploy — Status** tile) clears only when a later tick completes a successful
service deploy on this host — `clear_service_hold()` — or by hand, `rm` of the marker. A
BROAD hold clears only when its own plane is applied (*Which apply clears a hold*).
**A service migrated off this host must take its rendered compose with it**: `containers_for()`
treats a present `containers/<svc>/docker-compose.yml` as proof the service is deployed here,
so a stale one health-gates a phantom container to the run budget and false-rollbacks.

## Autonomous-role contract (it deploys to production with no human in the loop)

This is a **change-producing autonomous role**: every 10 minutes it may fast-forward the
primary checkout, run `deploy.yml` against the cluster, and on a failed health gate roll the
tree and the service back. Its authority is written down here so a later edit cannot quietly
widen it. Each line is a summary; the section it names carries the detail.

- **Scope / exclusions:** a Docker service whose template or bind-mounted config changed; a
  k8s role ONLY for an image-pin bump to a non-denylisted service; every setup-plane change
  the role can apply itself, and its own role. **Never** the bring-up playbooks
  (`_BROAD_MANUAL_PREFIXES`: `bootstrap.yml`, `k3s-bringup.yml`, `initial_setup.yml`),
  **never** a `tasks/`- or docs-only change, **never** a red or unfinished CI verdict, and
  **never** a SHA a previous tick held. The two daily GitHub crons act on one setting each:
  `github-interaction-limit.sh` re-applies a declared value; `github-ruleset-drift.sh` only
  alerts (*The two GitHub settings this role watches*).
- **Mode (explicit + reversible):** `has_gitops` in `host_vars` installs the timer on one
  host and tears it down everywhere else, and the code refuses on its own when the var reads
  `false` (*A host with `has_gitops: false` is reaped*). Returning a host to hand-deploy is
  the var flip plus a re-run of `initial_setup.yml --tags gitops_deploy`.
- **Authoritative sources:** `origin/master` as fetched this tick; GitHub's check-runs API for
  the CI verdict, authenticated through `gh auth token`; the live rollout and restart state
  for the health gate. Never a cached verdict, never the working tree.
- **Abort valves:** the CI gate (a non-green tip deploys the newest green ancestor or
  nothing — *Safety*); `hold_sha` / `hold_plane`, which park a failed SHA until a later
  successful apply of the same plane clears it (*Health gate + rollback*, *Which apply clears
  a hold*); `RUN_BUDGET_S` and the rollback budget, which bound one tick's wall clock; the
  staging gate, which asks `daniel-stage` about every commit that would auto-deploy a k8s
  service and, when `STAGING_GATE_BLOCKING` is on, stops the prod deploy on a rejection.
- **Required evidence:** every tick writes `last_run` (the GitOps-Alive tile expires without
  it); every deploy, rollback, hold and deferral posts to the dedicated Discord webhook, and
  a `hold_sha` pages through the **GitOps Deploy — Status** tile until cleared. An
  uneventful tick logs nothing, so `last_run` is the record that it ran.
- **Next-run review:** before widening scope (a new auto-deployable plane, a shorter
  `hold` rule, a wider denylist exemption), read the last week's Discord log for the holds
  and rollbacks that actually fired — *The deployer's record* in `docs/gitops-pipeline.md`
  is the list of what an earlier widening cost.

## Safety

Each arm below is a rule and the function that holds it. The record page has the incident.

- **CI gate — the tip must be green before anything is merged or deployed** (`REQUIRE_CI`,
  `deploy_logic.ci_verdict`). It queries GitHub's check-runs API for `origin/master`,
  authenticated through `gh auth token` (`deploy_logic.github_token`; `GH_TOKEN`/`GITHUB_TOKEN`
  win, a logged-out gh degrades to anonymous at 60/hour per source IP), and only on a tick
  that would otherwise deploy.
  - **A tip that is not green sends the gate walking**: `deploy_phases.assess` reads `git
    rev-list --first-parent <local>..<origin>` newest first, asks GitHub about each commit, and
    deploys the first `pass`; a `fail` is skipped and a held SHA dropped. Bounded by
    `CI_ANCESTOR_WALK_MAX` (10) counting the tip. **An UNAUTHENTICATED host does not walk at
    all** — the `DECIDED:` in `deploy_git.ci_walk_candidates`. The property that holds: the
    tree the host runs always has its own green verdict.
  - `fail` on the tip with no green ancestor → `ci_failed`: no ff-merge, no deploy, one Discord
    alert per SHA (`ci_alerted_sha`); a red tip the tick fast-forwarded PAST still pages once
    (`deploy_handlers.alert_red_tip`). `pending` with no green ancestor → `ci_pending`, silent.
    An unreachable or malformed API reads as `pending`, never `pass`; `last_run` is still
    written.
  - Both outcomes leave the host on `local`, which `behind_marker` records, so a persistently
    red master pages through the 6h behind-origin watchdog; don't add a second timer. An
    ancestor fast-forward keeps it armed: `entrypoint()` re-resolves the real `origin/<branch>`
    after `main()`, so `behind_since` names the tip. `Landing.tick_state` checks the PR's own
    merge commit before it believes `behind_since` (#1786).
  - **This gates the DEPLOY, and it is the only gate**: there is no branch protection on
    `master`, and PR CI is scoped to changed files while master runs the full sweep.
    `cancelled`/`stale` are **no verdict, not failure**. `CI_CONTEXTS` must match `ci.yml`'s
    `name:` exactly; an empty list disarms the gate with a log line.
- **Staging gate — two switches.** `STAGING_GATE` asks daniel-stage about every commit that
  would auto-deploy a k8s service; `STAGING_GATE_BLOCKING` decides whether a REJECTION stops
  the prod deploy. Both default false; daniel-box sets only the first (`docs/staging-phase-c.md`
  owns the flip). NO VERDICT never blocks (`staging_blocks`, `tests/test_staging_blocking.py`).
  `consult_staging` runs BEFORE the ff-merge, so a rejection holds the SHA with nothing to
  roll back; moving it after the merge silently breaks that. The escape hatch is one tick:
  `touch /var/lib/gitops-deploy/staging_gate_override`, read only where the gate would block.
- Read-only against the repo (no push); rollback is local-only + self-guarding.
- **A dirty working tree skips the deploy, not the tick** (`next_action(..., dirty=True) ->
  "dirty"`): `last_run` is still written, so GitOps-Alive stays green, and the page is
  throttled to twice per America/Chicago day (`should_alert_dirty`, `dirty_alerted_date`).
- **Test-suite paths are skipped before every plane** (`deploy_logic._is_test_only_path`):
  `ansible/tests/`, any role-local `tests/`, a `test_*.py`/`conftest.py` beside its module. The
  invariant — no role ships a test file — is `ansible/tests/repo/test_no_role_ships_a_test_file.py`.
- **A new module in `files/` goes in two lists in `tasks/main.yml`** — the copy task's
  `loop:` and `stamp_deployed_pairs`; a module missing from the copy loop passes CI and kills
  the deployer at import. ENFORCED by `ansible/tests/deploy/test_gitops_deploy_ship_list.py`.
- **Broad changes split three ways** (`deploy_logic._BROAD_*_PREFIXES`). A setup-plane change
  (`roles/setup/<name>/`, `requirements.yml`) applies as `initial_setup.yml --tags <name>`
  with the tag from `setup_tags_for`; a deploy-plane change (`ansible/templates/*`,
  `inventory/`, `common/`, `deploy.yml`) applies as `deploy.yml` narrowed by
  `deploy_narrow.plan`. **A range carrying both planes applies both, setup first**, sharing
  one `BROAD_DEPLOY_TIMEOUT_S` (1800) — it was an if/else until #2046.
  - **The deploy plane is narrowed before it is applied**: `scripts/deploy_tools/deploy_tags.py
    narrow <local> <origin>` as a subprocess, at the two refs the tick pinned. Tags →
    `deploy.yml --tags`; no tags → nothing applied, `broad_applied` records
    `narrowed-to-nothing`; any refusal → the full `deploy.yml` (the `# DECIDED:` at the
    fallback in `deploy_narrow.py`). The narrowed list is not filtered through
    `K8S_AUTODEPLOY_DENYLIST` (the `# DECIDED:` on `deploy_narrow.denylisted_in`). A failed
    narrowed apply writes `hold_plane` naming its tags. The caller graph is read from the
    WORKING TREE, still on `local`, so a range adding a caller of a shared role refuses.
  - **`roles/setup/<name>/` is not the same thing as `initial_setup.yml --tags <name>`**: the
    playbook may not include the role (`k3s`, `common`) and the tag may not be the directory
    name (`chezmoi_setup` → `chezmoi`). `setup_role_playbook` / `setup_role_tag` own the
    routing; `ansible/tests/deploy/test_setup_role_playbooks_agree.py` derives the truth.
  - **The ff-merge happens BEFORE the apply** — applying first renders from the pre-merge tree
    and deploys nothing. `test_broad_remediation_puts_the_ff_merge_before_the_playbook`.
  - **Both arms are FORWARD-ONLY.** A failure writes `hold_sha` and `hold_plane`, alerts
    saying nothing was rolled back, and leaves the tree fast-forwarded — no `git reset`, which
    would leave the tree claiming the old commit over half-new live state.
    `deploy_logic.broad_budget_ok` has no production caller.
  - **`_BROAD_MANUAL_PREFIXES` parks with no ff-merge**: the bring-up playbooks, plus a setup-plane
    path that resolves to no role. Staying parked keeps `behind_since` set, and the journal names
    the park's reason every tick (`deploy_remediation.broad_park_reason`). **A setup ROLE whose
    tag cannot be derived fast-forwards and is recorded in `manual_plane`** instead (`k3s`,
    `common`; the `DECIDED:` in `deploy_defer.py`'s docstring) — one line per role, first-seen
    stamp kept, logged on EVERY later tick, paged once per SHA, cleared by the tick applying the
    role's real playbook or by `gitops_state.py clear-manual-plane <role>`. **Its remediation
    names the NARROWEST tag the change needs** (#2307), derived by `deploy_defer.record` into the
    `manual_plane_tags` sidecar every surface READS; on doubt, the role tag plus
    `deploy_remediation.maximal_tag_warning`.
  - **This role applies itself.** The `Run gitops-deploy once` handler is `state: started`,
    which Ansible skips for an `activating` unit. The `DECIDED:` above
    `_BROAD_MANUAL_PREFIXES` in `deploy_logic.py`.
- **Behind-origin watchdog** (`deploy_logic.behind_marker`): a tick that ends behind origin
  records `"<origin_sha> <first_seen_ts>"` to `behind_since`, cleared on convergence, and
  **GitOps Deploy — Status** pages past `GITOPS_BEHIND_MAX_MIN` (6 h). **The stamp measures
  TIME WITHOUT A FAST-FORWARD, not time behind the tip**: any tick that moved the tree renews
  it, and every path that stops the deployer converging leaves HEAD where it was, so a wedge
  cannot game it (#1846, refuted). `manual_plane` is the second arm: `gitops_status` pages
  once the oldest pending line is older than the same 6 h, last of its four arms.
- **Secrets-only pushes** (`secrets.yml` with no template) fast-forward but do not redeploy;
  the deployer alerts once per SHA (`secrets_alerted_sha`) to redeploy the consumers.
- **k8s roles auto-deploy ONLY for an image-pin bump to a non-denylisted service; every
  other k8s change defers-and-alerts.** `deploy_logic.split_k8s_auto_deploy` is diff-shape
  first, identity second: the only path touched under the role is `defaults/main.yml`, every
  changed line assigns an `*_image:` var, the feature is enabled, the pilot (if set) names it,
  and it is not denylisted. The rest stays in `ChangeSet.k8s`: `--ff-only` merged, not
  deployed, alerted once per SHA (`k8s_alerted_sha`) with `deploy.yml --tags <svc>`.
  - **The denylist derives from each role's own `k8s_autodeploy` declaration**
    (`filter_plugins/k8s_autodeploy.py`, fail-closed). To stop a role auto-deploying, set
    `k8s_autodeploy: false` in its defaults.
  - **That edit lands under `roles/k8s/`, so nothing re-renders `config.env` from the changed
    path.** `deploy_phases.reconcile_denylist` closes the gap (#1294): each tick compares
    config.env with the declarations at HEAD and re-renders by running `initial_setup.yml
    --tags gitops_deploy` itself, gating on the FILE-level enable flag (#1317), passing
    `gitops_deploy_kick_after_change=false` (ENFORCED by
    `ansible/tests/deploy/test_denylist_render_suppresses_the_kick.py`), once per checkout SHA
    (`denylist_rendered_sha`), ENDING the tick, never from a dirty tree, and writing no
    `hold_sha` on failure — the `DECIDED:` markers in `deploy_phases.py`. The origin-side
    comparison (`k8s_declarations_at(origin)`, a regex biased toward denied,
    `test_denylist_parsers_agree.py`) stays the fail-safe: any mismatch disarms auto-deploy for
    that tick and pages once (`stale_denylist_alerted`), leading with the re-render.
  - **The gate is in the play, not here**: `roles/k8s/manifests` applies, `rollout-drain`
    waits, `post_tasks/k8s_stabilise_gate.yml` soaks. `containers_for()` returns `[]` for k8s.
  - **A k8s rollback is local-only, and not sufficient on its own**: `skip_hold` matches only
    while `origin_head == hold_sha`, so the bad pin is still on master. Revert on the remote.
    **A clean k8s tick is the second place `hold_sha` clears**; a BROAD hold survives it.
  - **One tick promotes at most `gitops_deploy_k8s_autodeploy_max_per_tick` (3) services**,
    and at most `gitops_deploy_k8s_autodeploy_max_claim_services_per_tick` (1) claim-declaring
    ones — the `DECIDED:` in `deploy_k8s.py` beside that cap. The surplus defer-and-alerts and
    is NOT retried: the ff-merge runs first, so `local == origin` afterwards.
  - **The deferral page is a one-shot, not a durable signal** (#947; the `# DECIDED:` at the
    `cs.k8s` branch of `deploy_alerts.alert_deferred`). The durable signal is the
    `release-staleness` cron in `roles/setup/k3s/`, running `probe.py releases --stale-only`
    as `sys_user` against each service's release record (`releases.py`'s
    `manifest_affecting_shared_roles()` names the shared roles it charges; the deploy plane is
    charged through `narrow_broad.broad_path_tags`).
  - **Accepted trade-offs, each marked `# DECIDED:` at the line that makes it:** the
    batch-abort blast radius (`deploy_k8s.py`, at the claim cap), the silently skipped snapshot
    (`k8s/volume-snapshot/tasks/claim.yml`, the warn-and-skip task) and the volume left in
    maintenance mode after a failed revert (`k8s/volume-revert/tasks/claim.yml`, the attach).
  - **The pilot list is empty, so the denylist alone decides** — an empty pilot means every
    non-denylisted service, the opposite of the empty-denylist guard. The
    `ansible/tests/test_k8s_autodeploy_*.py` family enforces the role shapes that must never
    be eligible. The deploy is bounded by `K8S_DEPLOY_TIMEOUT_S`, not `RUN_BUDGET_S`;
    promotion is refused when the tick also carries Docker services.
- **A service's structural dirs (`tasks/`, `defaults/`, `vars/`, `handlers/`) and
  `meta/deps.yml`** are ff-merged but NOT auto-deployed; the deployer defers-and-alerts once
  per SHA (`tasks_alerted_sha` / `meta_alerted_sha`, `deploy_logic.deferred_service_alerts`).
  `*.md` stays a silent ff-merge.
- Acts **only when origin is strictly ahead of local** (`is_ancestor(local, origin)`);
  un-pushed local commits are a no-op. **Divergence watchdog** (`deploy_logic.is_diverged`):
  neither an ancestor of the other → `diverged_sha` written each tick and **GitOps Deploy —
  Status** pages.
- Health-gates **only services deployed on THIS host** (`deploy_logic.containers_to_gate`).
  **Pi-only services are NOT auto-deployed by GitOps** (accepted, 2026-06-30): the Pi has
  `has_gitops: false`; deploy by hand with `-e target=daniel-pi`.

## The two GitHub settings this role watches

Both are daily `kuma-check` timers on the deploy host (`Restart=on-failure` reruns a down
verdict every 30 min; `Persistent=true`), authenticated with the deploy user's `gh auth token`.

- **`github-ruleset-drift.sh`** compares the live master ruleset against
  `gitops_deploy_expected_ruleset_contexts` and checks it excludes `refs/heads/renovate/**`
  (#1759). It never writes: a ruleset changes because a human changed it.
- **`github-interaction-limit.sh`** re-applies `gitops_deploy_interaction_limit` daily, since
  GitHub lets a limit lapse silently after six months; `none` clears it. A missing token, a
  failed PUT and a differing stored value all push DOWN, never `armed`
  (`ansible/tests/setup/test_github_interaction_limit.py`).

## Config / secrets
`/etc/gitops-deploy/config.env` (0600) is templated from the SOPS var
`gitops_deploy_discord_webhook`. Liveness is `/var/lib/gitops-deploy/last_run`, read by
`monitor-bridge`; the deployer pushes nothing to Kuma.

## How the deployer's Python is laid out

Three layers, and which one a function belongs in is decided by what it touches.

| layer | modules | holds |
|---|---|---|
| decisions (pure) | `deploy_changes` (which services and planes a path list reaches), `deploy_git` (what a tick does given the two HEADs, the hold and the CI verdict), `deploy_health` (the Docker gate and the delivery queue), `deploy_inventory` (what this host declares), `deploy_k8s` (auto-deploy eligibility, the denylist, the revert note), `deploy_remediation` (the text a deferred alert prescribes), `deploy_staging` (the staging subset, its verdict, whether it blocks) | every branch the tick takes, as functions over plain values |
| what a phase hands the next | `deploy_tick_types` | `TickTarget`, `TickPlan` and `RetryableFetchError`, no behaviour |
| transport | `deploy_io`, `deploy_alerts` | subprocess, docker, every message body, and the alert queue's own I/O |
| transport leaves | `gitops_markers`, `deploy_config`, `deploy_state`, `deploy_failtext` | the marker table and parsers, the config file, the state directory, and the text a failed run's alert quotes — each importing nothing from `deploy_io` |
| the seam | `deploy_toolbox` | `DeployTools`, one frozen object holding every boundary the tick crosses, and `default_tools(CONFIG)` |
| the phases | `deploy_phases`, `deploy_handlers`, `deploy_defer`, `deploy_staging_io` | `assess` and `plan_tick`; one `handle_*` per terminal branch; the staging gate's I/O shell; what the broad arm does with the half it will not apply |
| the tick | `gitops_deploy` | the config constants, `STATE`, `tick_config()`, `main()` sequencing the phases, and `entrypoint()` |

- **`main()` sequences, it does not decide.** `assess()` returns a frozen `TickTarget`,
  `plan_tick()` a frozen `TickPlan`, and one `handle_*` owns each terminal branch. No leaf
  imports `gitops_deploy` (ENFORCED by `test_no_leaf_imports_the_entry_module`). The branch
  order — broad before k8s before Docker — is load-bearing: the broad plane has to win.
- **Every process boundary is injected, not patched.** `main(tools)` threads one frozen
  `DeployTools` through every phase; a test builds one from `tests/_deploy_fakes.py`.
  `deploy_io.deploy`, `deploy_k8s` and `deploy_broad` stay outside it because the suite
  asserts on the argv they build, so `tests/conftest.py` keeps ONE patch, `deploy_io.run`.
- **`deploy_io` and `deploy_alerts` are reached QUALIFIED** — never `from deploy_io import`.
  ENFORCED in `ansible/tests/deploy/test_gitops_deploy_imports.py`, which also holds every
  module's sibling imports to an explicit `ALLOWED` map, keeps `deploy_logic.py` defining
  nothing (it re-exports every decision name, so a `deploy_logic.<name>` citation stays true),
  and keeps `deploy_staging` import-pure — `await_ci.py`, `land_tags.py` and
  `backfill_staging_gate.py` import the index with only this `files/` on `sys.path`, so its
  I/O shell lives in `deploy_staging_io.py`.
- **Configuration is parsed once, and parsing cannot fail.** `deploy_config.load_config`
  collects a malformed value into `Config.errors`; `CONFIG.validate()` at the top of `main()`
  turns it into one line naming the key plus a Discord post. `tick_config()` snapshots the
  module-level constants back onto a `Config` once per tick, which keeps a
  `monkeypatch.setattr(gitops_deploy, "REPO", ...)` live. Three keys keep a `C.get("<KEY>",
  "<literal>")` call because `scripts/docs/gen_doc_fragments.py` parses them by name.
- **One marker module, copied.** `files/gitops_markers.py` is the hand-edited source of the
  state directory, the `MARKERS` table and the marker parsers. `scripts/dev/gen_gitops_markers.py`
  writes a verbatim copy into every other reader (monitor-bridge, deploy-ui, renovate-agent,
  `scripts/lib/deployer_park.py`), and `ansible/tests/deploy/test_gitops_markers_copies.py`
  fails on a stale copy, a missing ship-list entry, or an import in the source. Edit the
  source, run the generator, commit every copy in the same PR.
- **State is one object.** `deploy_state.DeployerState` wraps the marker files and the hold
  writes; a caller names a marker (`state.path("hold")`), never a path. `read()` returns None
  for a missing AND an empty marker, and PROPAGATES any other `OSError` — an unreadable state
  directory must not read as "no hold" (`tests/test_deployer_state.py`).

Tests: one `tests/test_deploy_<module>.py` per decision module (a second file where a module
answers two questions), and the `tests/test_gitops_deploy_*.py` family for the entry module,
of which `_main_branches` drives whole ticks against the scripted `tick` fixture.
`tests/conftest.py` points `GITOPS_DEPLOY_CONFIG` at the canned `tests/config.env` before any
import and `state_dir` repoints every marker at `tmp_path`. Run:
`uv run pytest ansible/roles/setup/gitops_deploy/tests`.

## Which apply clears a hold

**`hold_sha` clears only when the plane the hold names is applied**
(`DeployerState.clear_broad_hold` / `clear_service_hold`, through
`deploy_logic.broad_hold_cleared_by`). Coverage, not equality: an untagged run covers any tag
set, a tagged run covers a held tag set it is a superset of, never an untagged hold. Every
consumer gates on `hold_sha` alone — `gitops_status`, `land.sh`, `renovate_agent.decide` — so
an early clear turns the tile green over an unapplied plane (#878). A hand `ansible-playbook`
run clears nothing: after fixing forward, `rm /var/lib/gitops-deploy/hold_sha
/var/lib/gitops-deploy/hold_plane`, as the alert and the monitor both print.

## A failed run's error string

`run()` raises a `RuntimeError` carrying the argv, the exit code, then a bounded slice of
**stdout followed by stderr** — stdout is where `ansible-playbook` writes the failing `TASK`,
the `fatal:` line and the `PLAY RECAP`. `_failure_detail` puts the last un-ignored
`fatal:`/`failed:` task first, drops profile_tasks' timing table, and spends the rest of
`RUN_ERROR_STDOUT_CHARS` on the tail. The Discord posts trim through `_alert_excerpt`
(`ALERT_EXCERPT_CHARS`, 700) because `host_lib.discord_post` cuts at 1900 keeping the HEAD,
so an unbounded error evicts the remediation prose after it
(`tests/test_gitops_deploy_failure_output.py`).

## Traps

- **Do not wrap `initial_setup.yml --tags gitops_deploy` in `flock
  /var/lock/server-git-tree.lock`.** The `Run gitops-deploy once` handler invokes the
  deployer, whose ExecStart is `flock -w 180 -E 75` on the same lock; held from outside, the
  smoke run waits 180 s and deploys nothing, and `SuccessExitStatus=75` makes systemd and the
  play both report success. The one immediate signal is the unit's `tick skipped (lock
  contention)` journal marker, which `gitops_tick.sh` reads to exit 3.
- **Moving a config source changes which remediation the alert prescribes.** `config.env` is
  rendered only by `initial_setup.yml --tags gitops_deploy`, so a value that moves from this
  role's defaults to `roles/k8s/<role>/defaults/main.yml` routes to `ChangeSet.k8s` and the
  alert names `deploy.yml`, which cannot re-render it. Before moving any value that lands in a
  host config file, check which `_ACTIVE_*` regex its new path matches and what
  `broad_remediation()` says for that plane. A set difference says what diverged, never why —
  the re-render leads in both directions (`deploy_phases._promote_k8s_auto_deploys`).
- **`restore_sha=origin[:8]` is a fixed slice; `git rev-parse --short=8` is a minimum
  width.** They diverge only when 8 hex chars are ambiguous in this repo's history, and then
  `k8s/volume-revert`'s no-snapshot assert fires before the scale-down — the safe failure.
  Accepted; the `DECIDED:` in `deploy_handlers.py` and `test_gitops_deploy_main_branches.py`
  pin the slice.

## Rollback timeout (`K8S_ROLLBACK_TIMEOUT_S` / `gitops_deploy_k8s_rollback_timeout_s`)

The rollback redeploy also reverts each claimed volume to its pre-deploy snapshot
(`k8s/volume-revert`), so it gets its own timeout, sized for the worst SINGLE promoted,
claim-declaring service. `test_k8s_rollback_budget_covers_the_worst_single_promoted_service` in
`tests/test_gitops_deploy_timeout_budgets.py` computes the ceiling from role sources, so a
rollout bump or a new promoted claim-declaring role fails it rather than under-sizing the
budget; the arithmetic and the re-sizing that produced today's figure are in
`docs/gitops-pipeline.md`.

- Two claim-declaring services in one batch stack additively;
  `gitops_deploy_k8s_autodeploy_max_claim_services_per_tick` bounds that (the `DECIDED:` in
  `deploy_k8s.py`, which also says why per-service rollback invocation does not fit). A
  failure aborts the play, so failure-driven worst cases cannot stack; the residual is a SLOW
  BUT SUCCESSFUL run cut short.
- Forward and rollback run sequentially in one activation: `TimeoutStartSec` in
  `gitops-deploy.service.j2` is `max(broad, staging + k8s + rollback)` plus the flock wait,
  and the template's own comment carries the arithmetic. Under-sizing the staging pair does
  not fail safe: a timed-out consultation is NO VERDICT.
- **All four tree-lock waiters are pinned as a census** (`_LOCK_WAITERS`,
  `_worst_lock_hold()` in the same test file): `deploy.sh`, secret-rotate, docs-refresh and
  eval-run each wait 3000 s, derived from the four timeouts.
- **A busy service lock is contention, not a failed deploy** (`ServiceLockBusy`, caught ahead
  of each handler's failure arm; `deploy_defer.for_contention` resets to `local`, returns 0,
  writes no hold). The streak IS recorded in `contention_since` (#1847): monitor-bridge pages
  past `GITOPS_CONTENTION_MAX_MIN` (30) and `gitops_state.py clear-contention` drops it. A
  broad apply takes `all` EXCLUSIVELY. **Waiting for a service lock spends the phase's own
  budget** (`deploy_locks.locked_budget`), so a queued phase can be SIGTERMed early and read
  as a failed deploy. The lock order is `all` first, then each service sorted — the
  `# DECIDED:` in `files/deploy_locks.py`; `deploy.sh` takes the order `deploy_locks.py plan`
  prints and refuses (exit 79) without it.
- An overrun never produces a second concurrent run — the timer coalesces the new start into
  the activation in flight — and it does not alert: `-E 75` plus `SuccessExitStatus=75` is
  `Result=success`.
