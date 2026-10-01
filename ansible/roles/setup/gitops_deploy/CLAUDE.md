# gitops_deploy — pull-based deploy on master change (every has_gitops host)

Installs a systemd **timer** (every `gitops_deploy_tick_interval`, 10 min) that runs
`/opt/gitops-deploy/gitops_deploy.py` as `{{ sys_user }}`. The script fetches `origin/master`;
if it advanced, it maps each changed path to a service or a plane, `--ff-only` merges, and
deploys what it may. `tasks/` and a role `CLAUDE.md` are deliberately NOT
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

Every 10 minutes this role may fast-forward the primary checkout, run `deploy.yml` against the
cluster, and roll the tree and service back on a failed health gate.

- **Scope / exclusions:** a k8s role ONLY for an image-pin bump to a non-denylisted service, plus
  every setup-plane change it can apply itself. **Never** a Pi Docker role, the bring-up
  playbooks, a `tasks/`- or docs-only change, a red or unfinished CI verdict, or a held SHA.
- **Mode (explicit + reversible):** `has_gitops` in `host_vars` arms it on one host and tears it
  down elsewhere, so hand-deploy is that flip plus `initial_setup.yml --tags gitops_deploy`.
- **Authoritative sources:** `origin/master` as fetched this tick, GitHub's check-runs API, and
  the live rollout state — never a cached verdict, never the working tree.
- **Abort valves:** the CI gate; `hold_sha` and `hold_plane`; `K8S_DEPLOY_TIMEOUT_S` and the
  rollback budget, which bound one tick's wall clock.
- **Required evidence:** `last_run` every tick (GitOps-Alive expires without it), a Discord post
  per deploy, rollback, hold and deferral, and a `hold_sha` that pages **GitOps Deploy — Status**.
- **Next-run review:** before widening scope, read the last week's Discord log for the holds and
  rollbacks that fired; the record page lists what an earlier widening cost.

## Safety

Each arm is a rule and the function that holds it. The record page has the incident behind it.

- **The health gate is in the play, not the deployer**: a k8s deploy fails when the batch drain
  (`roles/k8s/manifests/tasks/drain.yml`) or `post_tasks/k8s_stabilise_gate.yml` fails, and the
  deployer then writes `hold_sha` FIRST, resets to the previous HEAD and redeploys the prior pin
  (`deploy_handlers.py:_rollback_k8s`). **That rollback is local-only** — the bad pin is still on
  master, so revert it there.
- **CI gate — the tip must be green before anything is merged or deployed** (`REQUIRE_CI`,
  `deploy_logic.ci_verdict`). A non-green tip sends the gate walking `<local>..<origin>` newest
  first for the first `pass`, bounded by `CI_ANCESTOR_WALK_MAX`; an **unauthenticated host does
  not walk at all**. `deploy_git._CI_NO_VERDICT_CONCLUSIONS` are **no verdict, not failure**
  (`docs/landing.md` owns that rule), `CI_CONTEXTS` must match `ci.yml`'s `name:` exactly, and
  this is the ONLY gate — `master` has no branch protection.
- **Broad changes split three ways** (`deploy_logic._BROAD_*_PREFIXES`): a setup-plane change
  (`roles/setup/<name>/`, `requirements.yml`) applies as `initial_setup.yml`, narrowed per role
  by `narrowed_setup_tags` to its block tags, else `--tags <name>`; a deploy-plane change
  (`ansible/templates/*`, `inventory/`, `common/`, `deploy.yml`) as `deploy.yml` narrowed by
  `deploy_narrow.plan`, denylisted roles included, else the FULL play; and a range carrying
  both applies both, setup first. **A role directory is not the same thing as its tag**:
  `setup_role_playbook` and `setup_role_tag` own that routing.
- **The ff-merge runs BEFORE the apply**, since applying first renders the pre-merge tree, and
  **every broad arm is FORWARD-ONLY**: a failure writes `hold_sha` and a `hold_plane` entry and
  leaves the tree merged, because a reset would claim the old commit over half-new state.
- **`_BROAD_MANUAL_PREFIXES` parks with no ff-merge**, while a setup role whose tag cannot be
  derived merges and is recorded in `manual_plane` for a human to apply and clear.
- **k8s roles auto-deploy ONLY for an image-pin bump to a non-denylisted service; every other
  k8s change defers-and-alerts.** `deploy_logic.split_k8s_auto_deploy` is diff-shape first: the
  only path touched is the role's `defaults/main.yml`, and every changed line assigns an
  `*_image:` var. One tick promotes at most 3 services, 1 claim-declaring.
- **The denylist derives from each role's own `k8s_autodeploy` declaration**
  (`filter_plugins/k8s_autodeploy.py`, fail-closed), so `k8s_autodeploy: false` is how you stop a
  role; `deploy_phases.reconcile_denylist` re-renders `config.env`.
- **Pi Docker changes are never auto-deployed**, and an encrypted-vars-only push merges without
  redeploying, alerting once per SHA.
- **A new module in `files/` goes in the copy `loop:` and `stamp_deployed_pairs` in
  `tasks/code.yml`**. ENFORCED: `ansible/tests/deploy/test_gitops_deploy_ship_list.py`.

## Which apply clears a hold

**`hold_sha` clears only once every plane `hold_plane` lists is applied** — one entry per failed
apply, each dropped by an apply covering it (`clear_broad_hold` / `clear_service_hold`): an
untagged run covers any tag set, a tagged run a held set it is a superset of, never an untagged
hold. Every consumer gates on `hold_sha` alone, so an early clear turns the tile green over an
unapplied plane (#878). A hand `ansible-playbook` run clears nothing, and the deploy UI's Clear
button drops EVERY entry at once as the operator's override. **A narrowed setup apply holds
`<role>:<block>`** for each block tag it ran (`deploy_git.held_tag`), which that block or the
role's whole-role tag covers and a different block of the role does not (#3138).

## Traps

Five live in `docs/gitops-pipeline.md`: the failed-run error string, the tree-lock self-deadlock,
the config source that changes an alert's remediation, the two timeout budgets, and the
`restore_sha=origin[:8]` fixed slice. Read them before editing an alert or raising a cap.
