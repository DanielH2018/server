# gitops_deploy — pull-based deploy on master change (every has_gitops host)

Installs a systemd **timer** (every `gitops_deploy_tick_interval`, 10 min) that runs
`/opt/gitops-deploy/gitops_deploy.py` as `{{ sys_user }}`. The script fetches `origin/master`;
if it advanced, it maps each changed path to a service or a plane, `--ff-only` merges, and
applies the setup roles it may. `promote` and Flux apply every k8s change since D6.12. `tasks/` and a role `CLAUDE.md` are deliberately NOT
auto-deployed, and triggering a tick by hand is the **`gitops-tick` skill**.

Config is `/etc/gitops-deploy/config.env` (0600), from the SOPS var
`gitops_deploy_discord_webhook`; liveness is `/var/lib/gitops-deploy/last_run`, read by
`monitor-bridge`. This file carries the rules; `docs/gitops-pipeline.md`'s *The
deployer's record* carries the incident behind each, the budget arithmetic, the module layout
and the trade-offs.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "gitops_deploy"`
- **Timers (3):** `gitops-deploy.timer` (`OnBootSec=10min`, `OnUnitActiveSec=10min`),
  `kuma-check-github-ruleset-drift.timer` (`OnCalendar=*-*-* 06:40:00`),
  `kuma-check-github-interaction-limit.timer` (`OnCalendar=*-*-* 06:50:00`)
<!-- /generated_from -->

## A host with `has_gitops: false` is reaped, and the code refuses on its own

`tasks/main.yml` dispatches on `has_gitops`: `install.yml` on the deployer, `teardown.yml`
everywhere else. `deploy_phases.refuse_unless_deployer` repeats the gate in code, ahead of every
state write, and **fails OPEN on any shape but a top-level `false`** — a wrong refusal on
daniel-box parks every landing in the fleet. ENFORCED:
`ansible/tests/setup/test_gitops_deploy_reaps_on_non_deployer.py`.

**`ansible/inventory/group_vars/all.yml:has_gitops` defaults to `false`** (#2810), and a
non-deployer still writes it: the record page says why.

## Autonomous-role contract (it deploys to production with no human in the loop)

Every 10 minutes this role may fast-forward the primary checkout and apply the setup roles the
range changes, as `initial_setup.yml`. It applies no k8s role and runs no `deploy.yml` since
D6.12: `promote` publishes the bundle and Flux applies it.

- **Scope / exclusions:** every setup-plane change it can apply itself. **Never** a k8s role,
  an image-pin bump, a deploy-plane path, a Pi Docker role, the bring-up playbooks, a
  `tasks/`- or docs-only change, a red or unfinished CI verdict, or a held SHA.
- **Mode (explicit + reversible):** `has_gitops` in `host_vars` arms it on one host and tears it
  down elsewhere, so hand-deploy is that flip plus `initial_setup.yml --tags gitops_deploy`.
- **Authoritative sources:** `origin/master` as fetched this tick, GitHub's check-runs API, and
  the live rollout state — never a cached verdict, never the working tree.
- **Abort valves:** the CI gate; `hold_sha` and `hold_plane`; `BROAD_DEPLOY_TIMEOUT_S`, which
  bounds one tick's apply.
- **Required evidence:** `last_run` every tick (GitOps-Alive expires without it), a Discord post
  per apply, hold and deferral, and a `hold_sha` that pages **GitOps Deploy — Status**.
- **Next-run review:** before widening scope, read the last week's Discord log for the holds and
  rollbacks that fired; the record page lists what an earlier widening cost.
  `increase(gitops_tick_runs_total[7d])` counts the same week by outcome, lock skips and
  parks included. `files/gitops_tick_metrics.py` writes it each tick, and the unit's second
  `ExecStopPost=` writes it for a tick `flock` skipped. Two shadow rules in
  `ansible/roles/k8s/observability/files/rules/gitops-checks.yaml` read the file:
  `GitOpsTickStale` and `GitOpsTickOutcomeRepeating`. The "Containers and system monitoring"
  board plots the duration and the outcomes.

**The two GitHub timers write a textfile beside their Kuma push (M6 batch B3).** The ruleset-drift
and interaction-limit scripts call `write_check_textfile` from
`ansible/roles/setup/initial_setup/files/homelab-check-lib.sh` in `push()`, with a `DEADLINE_S` of
90000 that equals their Kuma tiles' interval. The rules in
`ansible/roles/k8s/observability/files/rules/gitops-checks.yaml` read the files in shadow.
ENFORCED: `ansible/tests/services/test_gitops_check_rule_literals.py` pins each rule's deadline
literals to the rendered `DEADLINE_S`. Both scripts source the check library with `|| exit 1`, so
a host that lacks the library fails its timer unit instead of skipping the file.

## Safety

Each arm is a rule and the function that holds it; the record page has its incident.

- **The tick applies the host plane only (D6.12).** A k8s role change, an image-pin bump and a
  deploy-plane path fast-forward here and apply nothing: `promote` and Flux apply them, and
  `land.sh` waits on that promotion. `hold_sha` therefore covers the host plane only.
- **CI gate — the tip must be green before anything is merged or deployed** (`REQUIRE_CI`,
  `hosts/lib/ci_verdict.py:ci_verdict`); this is the ONLY gate. `ci_verdict._CI_NO_VERDICT_CONCLUSIONS` are
  **no verdict, not failure** (`docs/landing.md` owns that rule), and `CI_CONTEXTS` must match
  `ci.yml`'s `name:` exactly. `docs/gitops-pipeline.md`'s *The safety arms, in full* has the
  ancestor-walk fallback and the rulesets that guard the merge itself.
- **Broad changes split two ways** (`deploy_changes._BROAD_*_PREFIXES`): a setup-plane change
  (`roles/setup/<name>/`, `requirements.yml`) applies as `initial_setup.yml`. A deploy-plane
  change (`ansible/templates/*`, `inventory/`, `common/`, `deploy.yml`) applies nothing on the
  tick and owes nothing: the renderer reads it and `promote` applies it.
  `docs/gitops-pipeline.md`'s *Broad changes* has the setup narrowing.
- **The ff-merge runs BEFORE the apply**, since applying first renders the pre-merge tree, and
  **every broad arm is FORWARD-ONLY**: a failure writes `hold_sha` and a `hold_plane` entry and
  leaves the tree merged, because a reset would claim the old commit over half-new state.
- **`_BROAD_MANUAL_PREFIXES` parks with no ff-merge.** A setup role parks only while
  `scripts/deploy_tools/setup_routing.py` fails or times out; any other the tick cannot apply
  merges into `manual_plane` (#4326, narrowing `deploy_defer`'s `DECIDED:` marker).
- **No k8s role takes anything from this tick, so `flux_suspend: true` in its defaults is how
  you stop one**: it suspends every Kustomization of the tag (`docs/flux-deploy-plane.md`,
  *Stopping one service*). The `k8s_autodeploy` denylist
  (`filter_plugins/k8s_autodeploy.py`) still renders into `config.env` through
  `deploy_phases.reconcile_denylist`, and no apply reads it; D6.2a deletes both.
- **Pi Docker changes are never auto-deployed**, and an encrypted-vars-only push merges and
  alerts once per SHA. It applies only the setup roles reading a key it changed, which
  `deploy_defer.route_secret_readers` routes (#5436). A k8s role reading one takes the new
  value through `homelab-cluster-vars`, which `promote` applies.
- **A k8s tag whose `containers_list` entry sets `owner` goes to `cs.flux`**, beside `cs.k8s`.
  Flux applies it, so the tick merges and logs it and never applies, defers, alerts on or owes
  it. Ownership is read at the commit being applied. `docs/gitops-pipeline.md`'s *A tag Flux applies is merged and skipped* has each
  site, and `ansible/tests/deploy/test_owner_readers_agree.py` holds the three readers equal.
- **A new module under `files/` goes in the copy `loop:` and `stamp_deployed_pairs` in
  `tasks/code.yml`**. ENFORCED: `ansible/tests/deploy/test_gitops_deploy_ship_list.py`.

## Which apply clears a hold

**`hold_sha` covers the host plane only, and clears once every plane `hold_plane` lists is
applied**, each entry dropped by an apply covering it (`clear_broad_hold`). No tick writes a
k8s hold since D6.12, so one left from before it clears only by the operator's clear. `files/gitops_hold.py`'s `Hold`
owns that rule for every writer, the deploy UI's Clear included (#3658). `docs/gitops-pipeline.md`'s
*Which apply clears a hold* has the coverage rule for each shape — including a narrowed setup
apply's `<role>:<block>` form (#3138) — the operator's clear (`gitops_state.py clear-hold`), and
why a hand `rm` of `hold_sha` is not one.

## Traps

Four live in `docs/gitops-pipeline.md`: the failed-run error string, the tree-lock self-deadlock,
the config source that changes an alert's remediation, and the two timeout budgets. Read them
before editing an alert or raising a cap.
