# R4 — Delete the k8s Ansible path

Status: PLANNED

Depends on: D6

Related issues: [#4527](https://github.com/DanielH2018/server/issues/4527), which proposes a
shared `_as_filter` helper for the filter plugins. No symbol of that name exists at
`70591c307`. The issue folds into R4, because R4 changes the exception the plugins raise.

Unblocks: H7, which removes Ansible and ansible-core once no slice needs them

## Goal and exit criterion

After R4, no Ansible task renders, applies, prunes, drains, verifies or records anything for
a k8s service. The roles under `ansible/roles/k8s/` keep only what the renderer reads:
templates, defaults, files and their tests. Two runbook roles keep their tasks until H7 (exit
1). `homelab-render` is the only render path, and it runs with ansible-core unimportable, so
H7 can remove ansible-core.

R4 deletes code. It converts nothing. [D5](../deploy/d5-bulk-move.md) converts every
imperative task in a role before it flips that role's entry (D5, *How each kind of task
moves*), and Flux is the only writer to the cluster before R4 starts (C4). No PR in R4 moves
ownership of a live object. Each R4 PR either deletes code that no longer writes anything,
or changes how the renderer reads the tree without changing a rendered byte.

**Exit criterion.** Six checks, all green at one master commit:

1. **The path is gone.** `git ls-files 'ansible/roles/k8s/*/tasks/*'` prints exactly the
   runbook allowlist that PR 1 records. PR 8 adds a test that asserts the set equals that
   allowlist. At `70591c307` the allowlist would be `volume-revert` (`main.yml`,
   `claim.yml`) and `longhorn-api` (`main.yml`, `resolve.yml`). D5.2 makes
   `k8s/volume-revert` the operator's revert runbook, and it includes `k8s/longhorn-api`, so
   both stay until H7.
   `ansible/deploy.yml` has no k8s play. `ansible/tasks/k8s_batch.yml`,
   `ansible/tasks/k8s_retire.yml`, `ansible/post_tasks/k8s_stabilise_gate.yml`,
   `ansible/post_tasks/k8s_image_drift_gate.yml`, `ansible/filter_plugins/k8s_retire.py` and
   `ansible/roles/setup/render_records/` are absent.
2. **No unit is left behind.** On daniel-box,
   `systemctl list-unit-files 'kuma-check-render-records*'` lists nothing.
3. **The rendered bytes did not move.** Every R4 PR from PR 1 to PR 10 merged with a green
   `render-diff` run on its last commit, under `Render-diff: none` or under the exact list of
   files it named. The run is required from PR 2 on, and advisory on PR 1, whose merge adds
   the context to the ruleset. Each run compares two complete C1 trees, `cluster/`, `flux/`
   and every `kustomization.yaml`, rendered from the PR's merge base and its head with one
   pinned commit value (*The render-diff gate*). Each PR's description quotes its run, and
   PR 11's description lists every run with its conclusion. A comparison of R4's first and
   last commits is not the check, because other sessions merge into that span: Renovate's
   image bumps move rendered pins, and a new or retired service changes the tag set.
4. **The renderer runs without ansible-core.** A test renders every tag, and runs the
   validator, in a subprocess where `import ansible` raises. Parity tests compare each
   reimplemented filter with ansible-core's while ansible-core is still installed.
5. **Every former verify half has a D5 successor.** PR 1 maps each verify row of R1's
   out-of-render census to the successor D5 shipped. A successor has one of the three shapes
   that C7 lists under *Verify successors*:
    - a rule labelled `service="<tag>"` and either `severity="critical"` or `verify="true"`.
      D5.3's soak Job reads both kinds on every promotion, an automerged one included, and
      reports its verdict as the `flux/soak` commit status (D5, *The soak as a Job*). D5's
      converted rules default to `severity="warning"` with `verify="true"`;
    - a check in `probe.py health <tag>`, only for a role whose defaults set
      `k8s_autodeploy: false`. `probe.py health` runs only from `land.sh`, so it misses every
      automerged bump, and D5 forbids it as the only successor of any other role. The key
      outlives D6 with its Renovate meaning (D6, *The `k8s_autodeploy` key keeps its
      Renovate readers*);
    - a Job in a `<tag>-verify` Kustomization that depends on the tag's last Kustomization:
      `<tag>-deferred` where one exists, `<tag>` otherwise (D5, *`<tag>-verify`*).

   A row with no successor stops R4, and the gap goes back to D5. The full suite passes.
6. **The renderer ends at a stated size.** The non-test Python under `scripts/renderer/` is
   at most 2,600 lines at R4's last PR. The slices' stated additions sum to about 1,940:
   R1's 600, R2's about 500, D3's about 300, D4's about 170 and 140, and R4's about 150 of
   filters and 80 of targeting. The headroom covers what the measured set holds with no
   stated estimate: D2.6b's Kustomization and root writers, D2's footprint digests, D5's
   suffixed kinds, D6's deletion policy and tag rule, R4's moved task-made facts, and H3's
   host and package modes if they have landed, which C1 puts in the same CLI.
   `ansible/filter_plugins/` is outside the set. An overrun stops R4, and the operator
   decides whether the dialect ADR's subset shrinks or the bound rises (**operator-only**).

## Baseline at `70591c307`

R4 runs after D5 and D6. This baseline measures the tree before either, so R4's first PR
re-measures every item and drops what D5 or D6 already deleted.

- **86 task files, 9,635 lines,** under `ansible/roles/k8s/*/tasks/`. 57 of them include
  `k8s/manifests`, and nine include `k8s/image-builder`.
- **The manifests role** holds eight task files, `files/secret_hmac.py`, its tests and
  `defaults/main.yml`. Its `CLAUDE.md` documents the render, apply, prune, drain, digest and
  stamp cycle.
- **The k8s play** is the second play in `ansible/deploy.yml` (210 lines in all). It filters
  and toposorts `containers_list`, applies the `homelab` Namespace and LimitRange, retires
  services through `ansible/tasks/k8s_retire.yml` (146 lines), and batches roles through
  `ansible/tasks/k8s_batch.yml` (32 lines). Two post_tasks gate the play:
  `k8s_stabilise_gate.yml` (109 lines) and `k8s_image_drift_gate.yml` (131 lines).
  `ansible/filter_plugins/k8s_retire.py` has one caller, `k8s_retire.yml`, which loops over
  `k8s_retired_services` in `ansible/inventory/group_vars/all.yml`. D2 adds the
  `ansible_owned` filter to `ansible/filter_plugins/toposort.py`, and D6's ownership table
  assigns its deletion to R4.
- **Render metadata lives in task files.** 43 task files outside the `manifests` and
  `image-builder` roles set `manifests_*` or `image_builder_*` include vars. At the baseline
  the renderer reads `manifests_exclude_files`, `manifests_files`, `manifests_secret_files`,
  `manifests_deferred_files`, `manifests_deferred_dir_name` and `manifests_prune`, plus the
  `image_builder_*` vars. That list predates D3 and D5. D5.1e stamps `checksum/role-config`
  on a role's `manifests_rollout` workload and its `manifests_extra_rollouts` entries, and
  `checksum/restart` on each `manifests_self_rollouts` entry, so those keys are renderer
  inputs after D5. PR 1 re-measures the list. `k8s_autodeploy_snapshot_pvcs` already lives
  in role defaults (D5's baseline greps `defaults/main.yml` for it), and D5.2a's
  `<tag>-snapshot` reads it, so it stays there as a renderer input. D6 assigns the key to R4
  because the manifests role reads it. Twelve roles pass explicit file lists in
  `tasks/main.yml`: authelia, gpu-exporter, headlamp, livesync, longhorn-ui, navidrome,
  netpol-baseline, observability, pihole, scrutiny, traefik and uptime-kuma. authelia and
  traefik build theirs with folded Jinja expressions. headlamp, netpol-baseline and pihole pass
  `manifests_deferred_files`, which places files in the `<tag>-deferred` Kustomization (C3).
  n8n includes `k8s/image-builder` twice. All 63 role directories under `ansible/roles/k8s/`
  have a `defaults/main.yml`.
- **Readers of role task files.** Seven, and the first one falls back silently:
    - `scripts/lib/k8s_roles.py:_named_files` reads `roles/<role>/tasks/main.yml` textually
      for the four file-list keys, and returns `{}` when that file is missing.
      `resolved_manifest_files` then derives the lists: every own manifest template, with no
      deferred file subtracted. Its callers are `scripts/diagnostics/probe_lib/health.py`,
      `scripts/validate/k8s_manifests.py`, `scripts/docs/catalog_lib/catalog_facts.py`,
      `scripts/docs/catalog_lib/route_facts.py` and R1's renderer.
    - `scripts/lib/k8s_roles.py:role_callers` globs `*/tasks` for `include_role` edges, and
      returns nothing once the task files go.
    - R1's `renderer_lib.task_inputs.include_vars` reads the `vars:` of each `include_role`
      of `k8s/manifests` and `k8s/image-builder`. `task_inputs.derived_facts` computes the
      task-made facts in the next item.
    - `scripts/deploy_tools/render_targets.py:includes_manifests` decides which services
      render by grepping role tasks for `k8s/manifests`.
    - `scripts/dev/new_k8s_service.py` scaffolds a `tasks/main.yml`.
    - `scripts/docs/gen_role_glance.py` names `tasks` in its role readers and in the marker
      it writes into every k8s role's `CLAUDE.md`.
    - `ansible/tests/deploy/test_image_builder_content_tag.py` reads
      `TASKS = ROLE / "tasks" / "main.yml"`, and fails with "no set_fact assigns" when the
      expression is missing.
- **Task-made facts that templates read.** R1's census found these, all derivable from the
  tree:
    - `image_builder_content_tag`, set by the task "Compute the content tag" in
      `ansible/roles/k8s/image-builder/tasks/main.yml`. The task "Publish the content tag to
      the play" adds it to `k8s_built_image_tags`, which nine image pins read.
    - Four script checksums: `monitor_bridge_script_checksum` in monitor-bridge's
      `tasks/main.yml`, `autofix_bridge_script_checksum` in autofix-bridge's, and the two
      `game_stats_*_script_checksum` facts in game-stats' `terraria.yml` and `valheim.yml`.
    - `kuma_declarations` in uptime-kuma's `tasks/main.yml`, and `janitorr_dry_run`, which
      janitorr's `tasks/main.yml` maps from `janitorr_k8s_dry_run`.
    - Five live-read facts, which R3 and D5 remove.
- **Seven utility roles** are reached only through `include_role`: `manifests`,
  `image-builder` (ten include sites), `cronjob-gate` (configarr and pi-peer-backup),
  `arr-notification`, `volume-snapshot`, `volume-revert` and `longhorn-api`. `volume-revert`
  has one caller, the `include_role` in `ansible/roles/k8s/manifests/tasks/main.yml`, and it
  includes `k8s/longhorn-api` itself. The deployer and `ansible/tests/longhorn/` also name
  the last three.
- **Utility roles hold renderer inputs outside `tasks/`.** D5.4 renders each image's context
  from `ansible/roles/k8s/image-builder/templates/context-configmap.yaml.j2`. D5's
  `sonarr-verify` and `radarr-verify` Jobs run
  `ansible/roles/k8s/arr-notification/files/seed_arr_notification.py`. The manifests role's
  `defaults/main.yml` holds `manifests_prune: true` and `manifests_shared_defaults`.
  `scripts/lib/k8s_roles.py:SHARED_MANIFEST_DEFAULTS` mirrors the second, and
  `scripts/lib/render_context.py:render_context` layers role defaults in Ansible's
  precedence. Deleting one of these roles whole could therefore move a rendered byte.
- **Imperative halves, which D5 converts.** D5's class table assigns each one a class:
    - Verify: six `verify.yml` files (crowdsec, janitorr, jellyfin, qbittorrent, sonarr,
      tdarr), netpol probe Jobs in five roles (n8n, prowlarr, headlamp, registry,
      netpol-baseline), `cronjob-gate`'s gate Job, media-volume's hardlink probe and the
      manifests role's `verify_secret_keys.yml`.
    - In-cluster writes: authelia's TOTP seeding, crowdsec's machine registration,
      observability's Grafana admin and dashboard ConfigMaps, `arr-notification`'s API calls,
      and `kubectl patch`, `exec` or `delete` calls in jellyfin, media-volume, observability,
      pihole, wg-easy and volume-snapshot.
    - Host-side: R1's census names nine roles that write host files: artifacts, configarr,
      crowdsec, janitorr, observability, pi-peer-backup, qbittorrent, registry and traefik.
      D5's baseline names two, artifacts and docs. pi-peer-backup's tasks "Install the
      forced-command wrapper on daniel-pi" and "Authorize the pull key on daniel-pi" write on
      daniel-pi.
    - Two pre-apply cluster writes in traefik. "Install the Traefik CRDs" applies an upstream
      URL pinned by `traefik_k8s_crd_version`. "Install the Traefik RBAC" writes
      `rbac.yaml.j2` under `/etc/rancher/k3s/` and "Apply the Traefik RBAC" applies it. The
      comment above them says Traefik's Secret informers lose their watch, failing green,
      when the cluster-wide grant goes.
- **The render-record producer.** `ansible/roles/setup/render_records/` runs
  `deploy.sh --dry-run` hourly from the `kuma-check-render-records` timer. Its tasks key on
  `render_records_state`, which its `defaults/main.yml` derives from `render_records_enabled`
  in `group_vars/all.yml`; the absent arm removes the units, the worktree and the role's
  directories. `files/render_records.py:main` pushes `down` and returns 1 when
  `render_targets.py` lists nothing ("render_targets.py found nothing to render"), and
  `Restart=on-failure` reruns it every `render_records_restart_sec`. D2.4 makes
  `render_targets.py` leave flux-owned tags out of that list, so the list is empty from D5's
  last flip. The producer imports the deployer's modules from `/opt/gitops-deploy`. Its
  readers are `scripts/diagnostics/probe_lib/releases_render.py`, the deployer's
  `deploy_release.py` and `deploy_cross_role.py`, `scripts/deploy_tools/narrow_setup.py` and
  `scripts/secrets_mgmt/consumers.py`.
    - `deploy_cross_role.py:_KUMA_CHECK` names `render_records` among the roles that ship
      the shared kuma-check timer files.
      `ansible/tests/setup/test_setup_cross_role_files.py:test_the_deployers_table_equals_the_trees_cross_role_files`
      holds that table equal to the tree, so the role's deletion edits this deployer module
      in the same commit.
    - `consumers.py:CROSS_HOST_PUSH_TOKENS` lists `render_records_push_token`.
      `scripts/secrets_mgmt/tests/test_secret_consumers.py:test_every_auto_tier_token_resolves_a_consumer_or_is_known_manual`
      fails on an `auto`-tier registry token that is in neither that set nor a consumer's
      tags. `ansible/secret_rotation.yml` holds the token at tier `auto`.
    - `scripts/deploy_tools/render_targets.py` has four Python callers at the baseline:
      `render_records.py`, `shared_role_callers.py`, the deployer's `deploy_narrow.py` and
      `test_render_targets.py`. D6 deletes the second and edits the third. R1 adds three
      more: the renderer, its determinism modules and `shadow_diff.py compare` (R1,
      *Design*). Among tracked files that are not Python or Markdown, only the test
      shard-weights table `scripts/dev/pytest_shard_weights.json` names it, so no shell
      script, workflow or template calls it.
- **How the deployer routes R4's paths.** Measured with
  `deploy_changes.py:services_from_changed_paths`:
    - `ansible/roles/k8s/<role>/tasks/` or `defaults/` gives `k8s={'<role>'}`, the k8s arm
      that D6.1 deletes.
    - A path under `ansible/roles/setup/render_records/` gives `broad_setup=True` and
      `setup_roles={'render_records'}`. The tick applies `initial_setup.yml --tags
      render_records` itself, with the become password `pre_tasks/load_secrets.yml` loads.
    - `ansible/initial_setup.yml` gives `broad_manual=True`. The tick parks with no
      fast-forward, and every later landing waits behind it until the primary checkout is
      fast-forwarded by hand (`docs/gitops-pipeline.md`, *When a tick parks*). That section
      says "Fast-forward first, then run it": the hand fast-forward clears the park, and a
      playbook the range owes still has to be run. That section and the park alert both
      name `origin/master` as the target. The alert's text comes from
      `deploy_remediation.py:broad_remediation`, which returns "`git merge --ff-only
      origin/{branch}` FIRST, then" the playbooks. A hand fast-forward past a commit strands
      that commit's plane for good. The docstring of
      `scripts/deploy_tools/land_lib/land_tags.py:self_applied_command` gives the reason:
      once local equals origin, "`next_action()` returns `noop` for every later tick".
      Called as `deploy_defer.py:park` calls it, on PR 6's path set, `broad_remediation`
      returns "`git merge --ff-only origin/master` FIRST, then `ansible-playbook
      ansible/deploy.yml` and `ansible-playbook ansible/initial_setup.yml` and", followed
      by one clause each for `gitops_deploy` and `render_records`. That run was off-host,
      so the role clauses read "could not be routed from the playbooks"; on daniel-box the
      routing derivation may word them differently. The alert therefore names a full
      `deploy.yml`, the whole `initial_setup.yml` and a deleted role, besides the target.
    - `land.sh` squash-merges. `scripts/deploy_tools/land_lib/merge.py:_merge_past_review`
      is its one merge call, and it sends `merge_method=squash`, so each PR is one commit
      on master.
    - `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py` gives
      `broad_setup=True` and `setup_roles={'gitops_deploy'}`, as does the role's
      `defaults/main.yml`.
    - PR 6's full path set, `ansible/initial_setup.yml`, the render_records role's files,
      `deploy_cross_role.py`, `scripts/secrets_mgmt/consumers.py` and `group_vars/all.yml`,
      gives `{'broad': True, 'broad_deploy': True, 'broad_setup': True, 'setup_roles':
      {'gitops_deploy', 'render_records'}, 'broad_manual': True, 'manual_playbooks':
      {'ansible/initial_setup.yml'}}`. `deploy_defer.py:parks_the_tick` returns true on
      `broad_manual`, so the range parks, and nothing then applies `gitops_deploy`.
    - `ansible/vars/secrets.yml`, `ansible/secret_rotation.yml` and
      `scripts/secrets_mgmt/consumers.py` together give `{'secrets': True}`, the deployer's
      secrets-defer path.
    - `ansible/inventory/group_vars/all.yml`, `ansible/deploy.yml`,
      `ansible/tasks/k8s_batch.yml`, `ansible/post_tasks/k8s_stabilise_gate.yml` and every
      file under `ansible/filter_plugins/` that R4 edits give `broad_deploy=True`, the
      deploy-plane arm that D6.1 and D6.4 delete. From D6.1 on, the tick fast-forwards past
      such a path with no apply and writes no ledger line (D6, *The route of a deploy-plane
      path after D6*).
    - `scripts/deploy_tools/render_targets.py`, `scripts/renderer/`, `.github/` and
      `ansible/volume-revert.yml` give an empty change set.
    - D6 keeps the deployer, because it applies the host plane until H7 (D6, *Goal*, and its
      exit 2).
- **Tests.** 47 files in `ansible/tests/k8s/` and `ansible/tests/deploy/` name the manifests
  role, the k8s batch or `deploy.yml`. Across `ansible/tests/` and `scripts/`, 102 test files
  name the manifests role, the k8s batch, `deploy.yml` or one of the two post_tasks. D6
  assigns "about 70 Ansible-mechanics tests" to R4. Some police Ansible mechanics, such as
  `test_manifests_apply_guarded.py`, `test_k8s_dry_run.py` and
  `test_secret_digest_reads_no_content.py`. Others check rendered properties through
  `ansible/tests/_k8s_render.py`, such as `test_rendered_properties.py` and
  `test_k8s_manifests.py`.
- **Kept tests read the play.** `ansible/tests/_k8s_render.py:deploy_play` raises
  `AssertionError("deploy.yml no longer has a play named ...")` once the k8s play is gone.
  Two modules that also hold render-property tests import it:
    - `ansible/tests/k8s/test_k8s_manifests.py`. Its
      `test_the_k8s_play_does_not_filter_an_already_filtered_list` checks the play itself,
      and `test_the_build_job_mounts_nothing_over_the_serviceaccount_token` checks a render.
    - `ansible/tests/k8s/test_namespace_resource_defaults.py`. `_homelab_ns_items` builds
      the Namespace and LimitRange from the play's pre_task, and its tests check that
      LimitRange's content. `test_the_namespace_apply_reports_changed_per_line` checks the
      play's `changed_when`. D5.1a1 moves the Namespace and LimitRange into the shared kind
      `homelab-namespace`.

  `grep -rlE 'roles/k8s/manifests/tasks|ansible/deploy.yml|"deploy.yml"|k8s_batch|k8s_stabilise_gate|k8s_image_drift_gate|k8s_retire' ansible/tests scripts .claude --include='*.py'`
  lists 86 files. That selector misses tests that read a role's `tasks/` or the manifests
  role's `defaults/` and `files/`, all of which PR 8 deletes. An example is
  `ansible/tests/k8s/test_shared_manifest_defaults.py`, which holds
  `SHARED_MANIFEST_DEFAULTS` against the manifests role's defaults. Adding `roles/k8s/[a-z0-9-]+/tasks`,
  `k8s/manifests/(defaults|files)` and `/ "tasks"` to the selector, over `ansible/tests`,
  `scripts`, `ansible/roles/k8s` and `.claude`, lists 270 files. PR 1 sorts that wider set:
  tests that PRs 3 to 5 retarget at defaults, tests that PR 7c retargets at the renderer,
  tests that PR 6 or PR 8 deletes with their subject, and code that is not a test.
- **`docs/facts.lock` records rows that R4 breaks.** A path atom hashes existence, so a row
  goes `missing` when its file is deleted. A symbol atom hashes content. At the baseline:
    - `ansible/roles/setup/render_records/CLAUDE.md` has three rows, and
      `#How one run works` cites `scripts/deploy_tools/render_targets.py`.
    - `ansible/roles/k8s/manifests/CLAUDE.md` has four rows. `#Release records` cites
      `ansible/roles/setup/render_records/`, and `#Rules a caller can break` cites
      `ansible/tasks/k8s_batch.yml` and `ansible/post_tasks/k8s_stabilise_gate.yml`.
    - Two `ansible/roles/k8s/crowdsec/CLAUDE.md` rows cite crowdsec's `tasks/main.yml` and
      `tasks/verify.yml`.
    - Root `CLAUDE.md#Adding a New Service` and `ansible/roles/k8s/janitorr/CLAUDE.md#Notable`
      cite `ansible/deploy.yml` as a path atom. That file keeps the Pi's play, so those
      atoms survive, but their prose names the k8s play.
- **Ansible-core reaches the renderer by three routes.**
    - `scripts/lib/ansible_jinja_env.py:register_ansible_filters` imports `b64encode`,
      `comment`, `get_hash`, `mandatory`, `to_bool` and `to_uuid` from
      `ansible.plugins.filter.core`, and `search` from `ansible.plugins.test.core`.
    - `ansible/filter_plugins/toposort.py` and `ansible/filter_plugins/authelia_access.py`,
      which the renderer imports, raise `ansible.errors.AnsibleFilterError`.
    - `scripts/lib/k8s_roles.py:is_leftover_dir` runs a deferred
      `from k8s_autodeploy import is_leftover_dir`, and
      `ansible/filter_plugins/k8s_autodeploy.py` imports `ansible.errors`. `role_dirs` calls
      `is_leftover_dir`. `scripts/validate/k8s_manifests.py`,
      `scripts/deploy_tools/setup_routing.py` and `scripts/docs/catalog_lib/catalog_backup.py`
      call `role_dirs`. D6.2 deletes `k8s_autodeploy.py`.
- **Licences.** `LICENSE` is the MIT License. ansible-core 2.21.5's package metadata reads
  `License-Expression: GPL-3.0-or-later`.
- **Docs that send the operator to `deploy.sh` for the cluster.**
  `docs/longhorn-disaster-recovery.md` (*Procedure (fresh host, total loss)*, step 5,
  *Deploy*), `docs/k3s-etcd-restore.md` (*After the restore*, step 4, "Redeploy from
  Ansible") and `docs/break-glass.md` in three places: *daniel-box is dead* step 4, the shed
  scenario's step 3 and the laptop scenario's step 3. *Bootstrapping Flux without Ansible*
  names the slice whose PR edits each of them.
- **What the runbook roles read from shared inventory.** The task files of `volume-revert` and
  `longhorn-api` read three keys outside their own defaults: `k8s_namespace`, `k8s_dry_run`
  and `k8s_no_mutate`. All three are in `group_vars/all.yml`, and none comes from the
  manifests role's defaults. D5.2c adds `ansible/volume-revert.yml`, a playbook that includes
  `k8s/volume-revert` alone, and says that R4's revert-entry PR, PR 7a here, is not needed.
- **No stage cluster exists.** `docs/archive/staging-cluster.md` opens "ARCHIVED 2026-09-29
  (#2941). The staging cluster is retired". `scripts/lib/kubectl.py:RETIRED_CLUSTER_NODES`
  holds `daniel-stage`. C8, *No stage cluster*, forbids gating on a stage run and names the
  three substitutes a proof may use.
- **The agent cannot edit workflows, and only three contexts gate a merge.**
  `docs/claude-agent-user.md`, *What the `claude` user cannot do*, says the agent's token has
  no `workflow` scope, so the operator pushes any change under `.github/workflows/`. The
  required contexts are `gitops_deploy_expected_ruleset_contexts` in
  `ansible/roles/setup/gitops_deploy/defaults/main.yml`: `prek (lint + validate + tests +
  secrets)`, `pull + boot changed images` and `renovate config validator`. The docstring of
  `ansible/tests/repo/test_every_ci_job_is_gated.py` says a job outside the merge gate "runs
  advisory-only".

The commands behind the counts above, from the repo root at `70591c307`. PR 1 reruns them.

```bash
ls ansible/roles/k8s/*/tasks/*.yml | wc -l                                 # 86
wc -l ansible/roles/k8s/*/tasks/*.yml | tail -1                            # 9635
wc -l ansible/deploy.yml ansible/tasks/k8s_*.yml ansible/post_tasks/k8s_*.yml
grep -lE "^\s+(manifests_|image_builder_)[a-z_]+:" ansible/roles/k8s/*/tasks/*.yml \
  | grep -v "roles/k8s/manifests/\|roles/k8s/image-builder/" | wc -l      # 43
grep -lE "^\s+manifests_(secret_|exclude_|deferred_)?files:" \
  ansible/roles/k8s/*/tasks/main.yml | grep -v /k8s/manifests/             # 12 roles
ls ansible/roles/k8s/*/defaults/main.yml | wc -l                           # 63
ls ansible/roles/k8s/*/tasks/verify*.yml                                   # 6 + verify_secret_keys
grep -l 'netpol-probe-job' ansible/roles/k8s/*/tasks/*.yml                 # 5 roles
grep -lE "roles/k8s/manifests|k8s/manifests|k8s_batch|deploy\.yml" \
  ansible/tests/k8s/*.py ansible/tests/deploy/*.py | wc -l                 # 47
grep -rlE "k8s/manifests|k8s_batch|deploy\.yml|k8s_stabilise_gate|k8s_image_drift_gate" \
  ansible/tests scripts --include='test_*.py' | wc -l                      # 102
grep -rnE '^\s*(from|import) ansible' scripts/lib ansible/filter_plugins \
  | grep -v '/tests/'                     # 2 in ansible_jinja_env.py, 5 filter plugins
grep -n "is_leftover_dir\|from k8s_autodeploy" scripts/lib/k8s_roles.py
grep -rn "k8s/volume-revert" --include='*.yml' ansible | grep -v roles/k8s/volume-revert/
grep -rln 'render_records\|k8s-renders.d' scripts ansible/roles/setup/gitops_deploy/files
grep -n "deploy.sh" docs/longhorn-disaster-recovery.md docs/k3s-etcd-restore.md \
  docs/break-glass.md
grep -ohE '\b(k8s|manifests)_[a-z_]+' ansible/roles/k8s/volume-revert/tasks/*.yml \
  ansible/roles/k8s/longhorn-api/tasks/*.yml | sort -u       # the three shared keys
grep -n RETIRED_CLUSTER_NODES scripts/lib/kubectl.py
grep -n -A4 '^gitops_deploy_expected_ruleset_contexts' \
  ansible/roles/setup/gitops_deploy/defaults/main.yml
grep -n '^_KUMA_CHECK' \
  ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py
grep -n 'render_records_push_token' scripts/secrets_mgmt/consumers.py \
  ansible/secret_rotation.yml
grep -rln 'deploy_play' ansible/tests scripts --include='*.py'
grep -rlE 'roles/k8s/[a-z0-9-]+/tasks|k8s/manifests/(defaults|files)|/ "tasks"|roles/k8s/manifests/tasks|ansible/deploy.yml|"deploy.yml"|k8s_batch|k8s_stabilise_gate|k8s_image_drift_gate|k8s_retire' \
  ansible/tests scripts ansible/roles/k8s .claude --include='*.py' | wc -l  # 270
grep -nE '^    "ansible/roles/(k8s/manifests|setup/render_records)/CLAUDE.md#' \
  docs/facts.lock                                                          # 4 + 3 rows
grep -rln 'render_targets' scripts ansible --include='*.py'      # the file + 4 callers
grep -n 'import_tasks\|include_tasks' ansible/roles/k8s/*/tasks/*.yml \
  | grep -v '/manifests/\|/image-builder/'   # game-stats imports its checksum files
grep -n 'merge_method=' scripts/deploy_tools/land_lib/merge.py           # squash, once
grep -n 'ff-only origin' \
  ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_remediation.py
```

The deployer routes come from `services_from_changed_paths` in
`ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_changes.py`, called on each
path list from the repo's `uv` environment with `ansible/roles/setup/gitops_deploy/files`
on `sys.path`, printing the non-empty fields of the returned change set. PR 6's command also
prints the park alert's text, from `broad_remediation` called as `park` calls it. R1's baseline holds
the commands for the nine host-writing roles and the 20 roles with kubectl write verbs.

```bash
uv run python -c "
import sys; sys.path.insert(0, 'ansible/roles/setup/gitops_deploy/files')
from deployer_lib.deploy_changes import services_from_changed_paths as s
cs = s(['ansible/initial_setup.yml',
        'ansible/roles/setup/render_records/defaults/main.yml',
        'ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py',
        'scripts/secrets_mgmt/consumers.py', 'ansible/inventory/group_vars/all.yml'])
print({k: v for k, v in vars(cs).items() if v})                 # PR 6's route
from deployer_lib.deploy_remediation import broad_remediation as b
print(b(cs.broad_deploy, cs.broad_setup, cs.setup_roles, 'master',
        playbooks=cs.manual_playbooks))"                          # its park alert
```

## Design

### R4 deletes, D5 converts

D5 converts every imperative row of a role in the PR that flips it, because Ansible stops
running that role's tasks at the flip (D5, *How each kind of task moves*). R4 therefore owns
no verify Job, no host-file move, no RBAC move and no post-apply Job. It checks that D5
finished, then deletes.

The check is a precondition test in `scripts/renderer/tests/`, added by PR 1. Its domain is
the role of every k8s entry in `containers_list`; after H1 it is the set of Kustomizations.
Utility roles such as `cronjob-gate` or `volume-snapshot` are not entries, so the test never
walks them; the first rule below already forbids an entry's role to include one. For each
role in the domain, each task in `tasks/` must be one of two kinds:

- an `include_role` of `k8s/manifests` or `k8s/image-builder`, with only `vars:`;
- one of the `set_fact` tasks `derived_facts` reads, or one of the two render-time asserts
  on `kuma_declarations` in uptime-kuma ("Check the declaration index is populated" and
  "Check every push token is in the format AutoKuma accepts"). The test names each one, and
  PR 4 moves them all;
- an `import_tasks` or `include_tasks` whose argument is a literal file name that resolves
  under the same role's `tasks/`. The test applies the same grammar to that file, and to any
  file it imports in turn. game-stats needs this, because its `tasks/main.yml` imports
  `terraria.yml` and `valheim.yml`, which set the two `game_stats_*_script_checksum` facts.

Any other task fails it. A templated argument fails too: janitorr and configarr import
`{{ role_path }}/../../setup/common/tasks/install_host_lib.yml`, a host write outside the
role. A red result stops R4, and the row goes back to D5 as a finding. The test has a
named-member floor: its domain must contain `pihole`, `traefik` and `flux`.

**`flux` is the one named exemption from the task grammar.** Its role's `tasks/main.yml`
holds D1's install code until PR 8 deletes it. Besides the include, D1 gives it a `rollout
status` wait on the three controllers with a restart-count snapshot, a `kubectl wait
--for=condition=Established` on the four Flux CRDs, the deferred apply of the sync objects,
and the delete of `ocirepository/verify-drill` while its lever is off (D1, *What D1 adds*).
D5's flux PR flips the role and strips none of these tasks. So without the exemption the
test is red on `flux` from PR 1 to PR 7. Those tasks are dead code, not unconverted rows:

- C4 says that, from the PR that flips an entry, the Ansible k8s play, `deploy.sh`, the
  deployer's k8s arms and `land.sh` all skip it. No path runs a flux-owned role's tasks.
- PR 1's ownership test proves that `flux` is flux-owned. The precondition reads flux's
  ownership from the same source, `owner: flux` before H1 and the Kustomizations after it,
  and exempts `flux` only while it is flux-owned. If D5's flux PR is reverted, `flux` goes
  back to Ansible, and the precondition fails on it again.
- The exemption names the role `flux` and no pattern. Every other role in the domain still
  meets the grammar, and a red fixture proves it.

`flux` stays in the floor, because the floor checks the domain, not the grammar. The
exemption does not waive the successor check. PR 1 maps flux's rows the way it maps a verify
row (*Steps*, PR 1), and a row with no successor goes back to D5 as a finding. PR 8
deletes the file.

The test's fixtures:

- red: an entry whose role has one `kubectl` command task;
- red: a role whose `tasks/main.yml` imports a sibling file that holds a `kubectl` command
  task;
- red: a role that imports `{{ role_path }}/../../setup/common/tasks/install_host_lib.yml`;
- red: a domain that holds a flux-owned `flux` with D1's tasks and one other role with a
  `kubectl` command task. The test fails and names the other role alone, so the exemption
  reaches no other role;
- red: a `flux` entry without the ownership flag whose role holds D1's tasks. The test fails
  on `flux`;
- green: a role whose `tasks/main.yml` imports a sibling file that holds only a permitted
  `set_fact`.

PR 8 deletes the test with its subject, and adds the runbook-allowlist equality test in its
place. Until then, PR 1 adds a weaker allowlist test: each allowlisted file exists.

R4 checks four things that D5 owns:

- **Verify successors** have the shape exit 5 names. A Job in the role's own Kustomization
  does not count. Flux applies it with the Deployment and starts it at once, while the old
  pod still serves the Service. That is the trap the header of
  `ansible/roles/k8s/janitorr/tasks/verify.yml` describes: a readiness wait is satisfied by
  the old pod, and only `rollout status` gates on the new ReplicaSet.
- **Host-side rows** of all nine roles in R1's census have an owner. D5's baseline names two.
- **traefik's cluster RBAC and CRDs** have a writer outside Ansible. The CRDs come from a URL,
  which C1's no-live-reads rule forbids the renderer to fetch.
- **The render-record producer** is disarmed (see *render_records* below).

### The overlap with D6

D6's ownership table gives R4 the manifests role's tasks, the k8s play with its pre_tasks
and post_tasks, `render_records`, the render halves of role task files, "about 70
Ansible-mechanics tests" and the `ansible_owned` filter. R4 starts from the tree D6 leaves.
PR 1 lists every baseline item with `git ls-files` and deletes only what is still there.
`render_targets.py` is the one item R4 moves before it deletes, because C1 names it as a
renderer input. PR 5 moves its logic into `renderer_lib/targets.py`, and PR 6 deletes the file
with its last caller, `render_records.py`.

D6 keeps `deploy.sh`'s refusal of a k8s tag, which prints the Flux landing path. After R4 the
refusal stays and its test stays: `deploy.sh --tags <k8s tag>` still exits 5 with that
message. Until H1 it reads the k8s tags from `containers_list`.

### The render-diff gate

R1's `shadow_diff.py compare` maps an Ansible render onto a C1 tree. It ignores
`kustomization.yaml` and never reads `flux/<tag>.yaml`, which carries `dependsOn`,
`postBuild` and the `-deferred` Kustomizations. PR 8 deletes the Ansible render it compares
against. R4 needs a C1-to-C1 comparison instead. This gate is not D6's
`scripts/validate/render_diff_gates.py`, whose arms refuse an edge, a change to a suspended
tag or an unannounced removal. The two share a word and nothing else, and PR 8 edits that
module's retirement arm (D6, *The render-diff gates after D6*).

PR 1 adds a workflow, `.github/workflows/render-diff.yml`, triggered on `pull_request` with
the types `opened`, `synchronize`, `reopened` and `edited`, so an edit to the PR body re-runs
it. It renders the PR's merge base from a worktree of that commit, and the PR head, each with
`homelab_render.py cluster` in placeholder mode. The merge base, not the base branch's tip,
is the comparison point, so another session's merge never shows up as this PR's change.

**Both renders take one pinned commit value.** D5's soak Job embeds the commit SHA it
verifies, and its name ends in a digest of that spec (D5, *The soak as a Job* and *Rules for
every Job Flux applies*). Two renders of two commits therefore always differ under
`cluster/soak/` and in whatever names that Job, even when no input changed. The renderer
takes the commit as an input, and the job passes the PR head's SHA to both renders through
it. No contract names that input yet. R4 refers to it as `--commit`, the name it proposes in
Open question 10, and PR 1 uses whatever name C1 records. With the value pinned, a remaining
difference comes from the tree. PR 1's red-proofs show that the pin covers every
commit-derived byte, not only the soak's (*Verification*).

The job then compares the two trees:

- `diff -r` over the complete trees, `cluster/`, `flux/` and every `kustomization.yaml`
  included;
- the head's tag set against the base's. Under `Render-diff: none` the sets must be equal.
  Under a list, every `flux/<tag>.yaml` that appears or disappears must be on the list;
- a floor on both trees, so two empty renders cannot pass: `flux/pihole.yaml`,
  `flux/traefik.yaml` and `cluster/pihole-deferred/` exist, and the `flux/` file count is
  at least the number PR 1 measures;
- the sorted-tree sha256 that `shadow_diff.py digest` prints (R1's exit 2), for the PR
  description.

The PR body's `Render-diff:` line states the expected result. `Render-diff: none` requires
zero differences. A list of paths requires the differing files to equal that list. A line
binds whenever it is present, on any PR. The line is mandatory, and its absence fails the
job, when the PR touches any of these paths:

- anything under `ansible/roles/k8s/*/tasks/`;
- a deletion anywhere under `ansible/roles/k8s/`;
- `scripts/renderer/`, `scripts/lib/k8s_roles.py` or `scripts/lib/ansible_jinja_env.py`;
- `ansible/filter_plugins/`;
- the renderer's dependency pins, from PR 7e: a `uv.lock` or `pyproject.toml` entry for a
  package in the renderer's dependency closure, Jinja2 and MarkupSafe among them.

Every R4 PR touches one of them or carries the line anyway. On a PR that touches none and
carries no line, the job only prints the diff, because most PRs change render output on
purpose. A role's `defaults/` stays out of the list, because Renovate's image bumps edit it;
R4's moves into defaults also delete from `tasks/`, which the list covers.

**A renderer dependency bump stops for a declaration.** After PR 8 no oracle compares a
render with Ansible's. A dependency bump that changes output would only print a diff, and
Renovate could automerge it into a pruning apply. PR 7e therefore adds the renderer's
dependency pins to the list. The job reads the closure from `uv.lock`, starting from one
literal set of the renderer's direct dependencies, and compares those entries between base
and head. A bump of any other package needs no line. A Renovate PR that bumps one fails the
job until a session adds the line, so it cannot automerge. A census test asserts that every
third-party module the renderer imports is in that literal set. PR 11 removes this hazard
with the rest of the gate, and Open question 12 asks whether it should outlive R4.

**Authors learn the line from the template and from the failure.** PR 1 adds a
`Render-diff:` section to `.github/pull_request_template.md` that names the hazard paths and
the line's two forms. The user-level `pr-authoring` skill lives outside this repo, and its
first rule is "A repo's own `.github/pull_request_template.md` wins", so the template
reaches every author who uses it. A run that fails for a missing line prints the hazard path
it matched and both forms. Other slices edit these paths while R4 is in flight, H3's host
and package modes under `scripts/renderer/` among them, so each such PR carries the line.

**The gate binds only once its context is required.** A job outside the ruleset runs
advisory-only, and a merge goes through with it red (Baseline). The agent cannot push a
workflow file, so the operator pushes `render-diff.yml`. PR 1 adds the job's context to
`gitops_deploy_expected_ruleset_contexts`, and the operator adds it to the live ruleset when
PR 1 merges (**operator-only**). `github-ruleset-drift.sh` compares the two daily. The
workflow has no `paths:` filter, because a required context that never reports blocks every
merge. No later R4 PR merges until the context is required. The proof that it binds can
exist only after PR 1 merges: a throwaway PR with a red render-diff run shows the merge
blocked, and PR 2's description quotes it.

**The gate ends with R4.** Its line exists to prove that R4's refactors moved no byte. After
R4 the renderer is the only render path, and a change to it usually moves output on purpose,
so a mandatory line would tax every later renderer PR for a proof no slice asked for. PR 11
removes the gate in this order:

1. The operator removes the context from the live ruleset (**operator-only**). Until then a
   PR that deletes the workflow would wait on a context that never reports.
1. PR 11 removes the context from `gitops_deploy_expected_ruleset_contexts`, the workflow
   file, which the operator pushes, and the template's `Render-diff:` section. It merges on
   the same day as step 1, so `github-ruleset-drift.sh` sees no day on which the ruleset and
   the list disagree.

A later slice that needs the same proof runs the comparison under *Verification* by hand and
quotes it, or re-adds the gate through PR 1's operator steps. H7's base-and-head render
proof is the first such case, and it needs the same pinned commit value.

### The renderer stops reading tasks

**File lists and include vars move to role defaults.** The keys keep their names, so the
renderer's logic does not change. Each one is an unprefixed role variable, so each line
carries `# noqa var-naming[no-role-prefix]`, as `k8s_autodeploy` does in every role's
defaults. The image-builder vars become one list per role, `image_builder_builds`, because
n8n builds two images.

**Which keys move.** Exactly the keys the post-D5 renderer reads from an include. PR 1
measures that set from the readers' own key lists, and it includes the rollout keys D5.1e
stamps from (`manifests_rollout`, `manifests_extra_rollouts`, `manifests_self_rollouts`). A
`manifests_*` key outside that set was read only by the Ansible path. It stays in `tasks/`,
where nothing runs it, and PR 8 deletes it with the task file. Moving it would add a dead
key to defaults.

The move runs in three stages, so no commit has two live sources for one key:

1. PR 2 teaches both readers, `k8s_roles._named_files` and `task_inputs.include_vars`, to
   read `defaults/main.yml` first and `tasks/` second. A key that both sources set raises.
2. PRs 3a to 3d move the keys by role group. Each PR deletes a key from `tasks/` in the same
   commit that adds it to defaults. Nothing runs those `include_role` calls for a flux-owned
   role, so the deletion changes no behaviour.
3. PR 5 drops the `tasks/` read. A missing `defaults/main.yml` raises instead of returning
   `{}`, and so does any key of the moved set under a role's `tasks/`. The set is one
   literal in the renderer, which both readers and the error read, so a key the renderer
   starts reading later joins the error too. An Ansible-only key left in `tasks/` does not
   raise.

The render-diff gate proves each stage moved no byte. A lost `manifests_deferred_files` would
move pihole instance 2 out of `cluster/pihole-deferred/`, and the gate fails on that path.

**Task-made facts move into `renderer_lib`.** PR 4 moves each expression out of the task
file and into a function in `renderer_lib`:

- the content-tag rule, `sha-` plus the first 12 hex digits of `sha256` over the context
  template's rendered `data` through `dictsort | to_json`;
- the four script checksums;
- `kuma_declarations`, with uptime-kuma's two asserts on it as checks that raise during the
  render.

`janitorr_dry_run` is only a rename, so the janitorr template reads `janitorr_k8s_dry_run`
directly instead.

`test_image_builder_content_tag.py` is retargeted at the new function. While ansible-core is
installed, it still evaluates the old expression, held in the test as a literal, through
`Templar` as the oracle. D5.4's tag test in the `images` job is retargeted at the same
function. A content tag that moved would rebuild all nine images and roll their pods, so
PR 4 carries `Render-diff: none`.

**The service set stops reading tasks.** PR 5 moves `render_targets()` into
`renderer_lib/targets.py`, and repoints R1's importers at it: the renderer, the
determinism modules and `shadow_diff.py compare`. A service renders when its role has a
manifest template or a shared default its entry earns.
`scripts/deploy_tools/render_targets.py` stays as it is until PR 6, for two reasons.
`render_records.py` still calls it, and the facts-locked section
`render_records/CLAUDE.md#How one run works` cites it as a path atom, so deleting it in
PR 5 would turn that row `missing` and fail CI.

Two tests hold the move:

- An equality test imports the old predicate, `render_targets.py:includes_manifests`,
  computes it and the new one over the same tree, and asserts the two sets are equal. The
  two copies coexist only from PR 5 to PR 6, and this test compares them for exactly that
  window. PR 6 deletes the test with `render_targets.py`.
- A floor test asserts that `pihole`, `traefik` and `flux` are members of the new
  predicate's set, so an empty set cannot pass. It stays after R4.

### The renderer stands alone

- **The filters are reimplemented, not copied.** ansible-core is GPL-3.0-or-later, and the
  repo is MIT. `renderer_lib/filters.py` holds an independent implementation of each filter
  `register_ansible_filters` imports. Tests import ansible-core only as the parity oracle,
  until H7 removes it. `to_json` becomes plain-data JSON in place of the `to_json_stub`
  stand-in, and PR 4's content-tag oracle keeps it exact.
- **`is_leftover_dir`'s rule moves to `scripts/lib/`**, unless D6 already moved it before
  deleting `k8s_autodeploy.py`.
- **`toposort.py` and `authelia_access.py` raise a plain `ValueError` subclass** in place of
  `AnsibleFilterError`. Ansible reports any exception a filter raises, so the setup plane
  still sees the failure until H7.
- **The ansible-free check runs the code, not a grep.** A test starts a subprocess that sets
  `sys.modules['ansible'] = None`, renders every tag and runs the validator. Its red half
  runs the same subprocess with a shim that makes `renderer_lib.filters`, a module the
  renderer imports on every run, import `ansible` first. That run must fail.
- **The validator and the test harness read the renderer's output.**
  `scripts/validate/k8s_manifests.py` validates the renderer's output instead of rendering
  on its own; its schema and semantic rules stay. `ansible/tests/_k8s_render.py` renders
  through the renderer, so the render-property tests read the bytes Flux applies.

### The renderer's dialect is specified

After PR 8, nothing checks the renderer against Ansible. R1 rebuilds `hostvars`, variable
precedence and `include_role` vars. PR 10 reimplements about 150 lines of filters. PR 8
deletes the Ansible render that R1's byte-for-byte comparison reads, and PR 9 deletes the
comparison. After H7 removes ansible-core, PR 10's parity tests lose their oracle too. The
renderer is then a private dialect of Jinja under `ansible/`, defined only by its code.
PR 7d writes the dialect down and holds the renderer to it.

- **An ADR states the supported subset.** It lists every filter, lookup and test that the
  renderer accepts. It states the variable semantics the renderer implements: the
  precedence of role defaults, `group_vars`, `host_vars` and `include_role` vars, what
  `hostvars` holds, and how an undefined variable fails. Anything off the list is
  unsupported. Approving the ADR is **operator-only**.
- **Spec tests hold the renderer to the subset.** They sit in `scripts/renderer/tests/`.
  Each supported construct gets a fixture and its expected output as a literal, so the
  tests need no oracle. PR 7d checks each literal once against Ansible, and the PR body
  quotes the two outputs equal:
    - a filter, lookup or test fixture against ansible-core's `Templar`;
    - a precedence, `hostvars` or `include_role` fixture against an `ansible-playbook`
      render of a fixture inventory. `Templar` templates against one flat vars dict and
      applies no precedence, so it cannot check these.

  The second check needs the Ansible play, which PR 8 deletes, so PR 7d must merge before
  PR 8. The literals then outlive H7.
- **Refusals are part of the spec.** A template that uses a filter, lookup or test off the
  list fails the render with an error that names it. A census test parses every template
  under `ansible/roles/k8s/` and asserts that each filter, lookup and test it uses is on the
  list. The refusal and the census read one literal in the renderer, and a test asserts
  that the ADR's table names the same set.
- **PR 10 keeps the subset.** Its reimplemented filters are exactly the ADR's list, and the
  spec tests pass before and after the switch.

### render_records

The producer fails hourly from D5's last flip, when `render_targets.py` lists nothing. The
disarm belongs at or before that flip, so D5 carries it (Open question 5).
The disarm is one line in `ansible/roles/setup/render_records/defaults/main.yml`:
`render_records_state: absent`. That path routes to the setup arm, so the tick applies the
role's absent arm itself and `land.sh` follows it. No hand apply, sudo or age key is needed.
A flip of `render_records_enabled` in `group_vars/all.yml` would route to the deploy plane
and apply nothing to the role. `services_from_changed_paths` gives that path only
`broad_deploy=True`. `ansible/deploy.yml` runs no setup role (the comment on
`deploy_cross_role.py:SETUP_ROLES_CALLING_FILTER_PLUGINS`), and the deploy plane's
narrowing reads no `roles/setup/` tree (`narrow_lib/broad.py:ROLE_TREES`). D5 carries the
defaults line in its own disarm PR, which lands before the flux PR and quotes this route (D5,
*The render-records disarm PR*).

PR 1 checks that the units are gone, and reads the units rather than the inventory key. If
they are still there, whatever D5 committed, PR 1 disarms the producer through the defaults
line first. PR 6 then deletes the role.

**The deletion parks the tick and owes one apply.** Three edits must share PR 6's commit:

- the role's directory;
- its entry in `ansible/initial_setup.yml`, because a playbook that names a missing role
  fails when it loads;
- `render_records` in `deploy_cross_role.py:_KUMA_CHECK`, because the table test holds that
  table equal to the tree.

That path set parks the tick (`broad_manual`, measured in the Baseline), and it also names
`gitops_deploy`, whose shipped module changed. The hand fast-forward clears the park and
applies nothing. So the operator then runs `initial_setup.yml --tags gitops_deploy` on
daniel-box (**operator-only**: it needs become and the age key). Without that apply,
`/opt/gitops-deploy` keeps the old `deploy_cross_role.py`. No `owed.jsonl` line records this
apply, because a parked range writes none, so PR 6's description names it, and PR 6's
landing is not done until the operator confirms it ran.

**The hand fast-forward stops at PR 6's commit.** The park alert says to fast-forward to
`origin/master` (Baseline). Another session's merge, or a Renovate automerge, can land
between PR 6's merge and the operator's step, which can take hours. A fast-forward to
`origin/master` would carry that commit past the tick, and the tick would never route it.
So the operator fast-forwards to PR 6's squash commit, after a `rev-list` check that lists
the span from the primary checkout's `HEAD` to that commit. Commits that merged after
PR 6 stay in the tick's next range, and the tick routes them as it routes any range. A
commit the check finds before PR 6 parks with it. At master's merge rate that is the usual
case, so PR 6's step quotes each such commit's route, names its owner and runs or agrees
every apply it owes. PR 6's step holds the procedure.

**The push token leaves after the role.** PR 6 keeps `render_records_push_token` in
`consumers.py:CROSS_HOST_PUSH_TOKENS`. The consumers census fails on an `auto`-tier registry
token that has no consumer, and the token stays in `ansible/secret_rotation.yml` until the
operator removes it. A follow-up commit after PR 6 does three things at once. The operator
removes the key from `secrets.yml` with `sops` (**operator-only**). The same commit runs
`secret_rotation.py sync`, which drops the registry row, and drops the `consumers.py`
entry. That path set routes to `{'secrets': True}` (Baseline), the deployer's secrets-defer
path, so it merges without a deploy and alerts once. The removal waits for PR 6, because
before it the role's template and the Kuma tile still reference the key.

### The revert runbook

`k8s/volume-revert`'s only caller at the baseline is the manifests role, which PR 8 deletes.
D5.2c makes it an **operator-only** runbook command with its own entry,
`ansible/volume-revert.yml`. PR 1 records that entry point. If the playbook is missing, or
the command still reaches `volume-revert` through the manifests role or `deploy.yml`, PR 7a
gives it a standalone playbook before PR 8 deletes that path.

PR 8 is the step that could break the runbook, because it deletes the manifests role and
k8s-play keys from `group_vars/all.yml`. It keeps the three shared keys the runbook reads
(Baseline), and a cluster-free test proves that every variable the allowlisted task files
read still resolves.

### Bootstrapping Flux without Ansible

R4 owns no bootstrap procedure and no bootstrap drill. D5 owns both, in one place. D5's
bootstrap drill PR writes the procedure and its kind drill, and D5's flux PR publishes the
procedure in `docs/flux-deploy-plane.md` as *Bootstrapping Flux on a rebuilt cluster* (D5,
the bootstrap drill PR and the flux PR). This section states only what R4 relies on and
what PR 7b checks. It restates no step of the procedure.

Earlier rounds of this plan held a four-step procedure here. D5 copied it, and H1 wrote a
third order for the same rebuild (H1, *Bring-up and DR order after H1*). The copies
disagreed on two points. R4's copy and D5's applied the root Kustomization before any
restore, so the root would provision empty claims under the backed-up names. Nothing
applied Longhorn before the root either. They also rendered at different commits: H1 and
R4 at `prod`, D5 at `last-good`. One owner removes both disagreements, so R4 keeps no copy
and writes no fallback. That owner withholds the root rather than suspending it. H1's
order suspends the root and resumes it later. A root applied and then suspended could
reconcile between the two commands, and a root that does not exist cannot (D5, *Why the
root is withheld, not suspended*).

- **D5's flux PR removes the last live install path, not PR 8.** That PR flips `flux` to
  `owner: flux`. From then on the Ansible k8s play, `deploy.sh`, the deployer's k8s arms and
  `land.sh` skip the role (C4), and `deploy.sh --tags flux` exits 5 (D2). D5 says the flux
  PR "removes the only path that installs Flux on a rebuilt cluster". R4 depends on D6,
  which follows D5, so no path runs the flux role's `tasks/main.yml` by the time PR 8
  deletes it. PR 8 deletes dead code there.
- **What R4 needs from D5's section.** PR 7b checks four properties at PR 8's merge base,
  and PR 8 merges only while all four hold:
    1. The procedure carries D5's withheld-root order. From `cluster/flux-deferred/` the
       operator applies the OCIRepository `homelab` and nothing else, so the root
       `homelab` Kustomization does not exist yet. The operator hand-applies the plumbing
       tags with their `dependsOn` closure, `cluster-vars` and `homelab-namespace`. D5's
       renderer test keeps that closure from reaching `flux-deferred` or the root. The
       backupstore sync, the DR gate (`scripts/deploy_tools/runbook_gates.py longhorn-dr`)
       and the restores follow. Only then does the operator hand-apply the root at `prod`,
       and that apply is the resume. No step patches the root's `spec.suspend`. The
       mechanical half of this property is D5's flux-PR text test over the bootstrap
       section, in `ansible/tests/repo/`. That test reads only the section, bounded by its
       heading, and fails when the root's apply comes before the restore step or a hand
       apply lacks `--field-manager=kustomize-controller`. Its red fixture is the section
       with step 9 moved before step 7.
    1. No step runs the flux role, the Ansible k8s play or `./scripts/deploy.sh` against the
       cluster. No step sets `flux_sync_suspend`, which the renderer refuses from D5's flux
       PR on. A D1 role variable cannot suspend the root either, because no Ansible path
       runs that role after the flux PR.
    1. The operator renders every hand-applied document at the commit `prod` names. C2
       defines `prod` as the revision Flux follows, so once the hand apply at `prod`
       creates the root, the root applies `prod`'s render and overwrites anything
       hand-applied from another commit. `last-good` can trail `prod` on purpose. A
       forward-only hold leaves `prod` on a revision that has not passed verify, as H1's
       step 6 does with its arm commit. The objects rendered at `last-good` then differ
       from the ones the root applies once it exists, and a tag that exists only at `prod`
       has no object to hand-apply. A standing `DEPLOY_HOLD` is settled by the operator's
       reset decision before the render, and a reset moves `prod` itself.
    1. D5's drill ran green on master after the last change to its inputs, and its three
       jobs cover that order. The drill's fixture bundle holds a plumbing tag that the
       closure applies and a stateful tag whose one claim only the root applies. A
       stand-in for the restores creates that claim, with a marker label, before the root
       exists.
        - The green job passes when both fixture tags' Kustomizations are Ready, the claim
          still carries the marker label and the UID the stand-in created, the plumbing
          Kustomization carries the root's ownership labels, and `homelab-cluster-vars`
          holds the fixture key.
        - The second job skips the `sops-age` step and passes only when `cluster-vars`
          fails to decrypt.
        - The third job applies the root before the stand-in, and passes only when the
          claim check fails, because the root created the claim first.

  A property that fails stops R4, and the gap goes back to D5, as a missing verify
  successor does under exit 5.
- **PR 8 shows the deletion leaves the procedure whole.** Its description quotes a green
  run of D5's drill on PR 8's head, all three jobs. The drill's path filter triggers it
  when it names a path PR 8 deletes. Otherwise the run is a `workflow_dispatch` on PR 8's
  branch, which GitHub allows because the workflow file is on the default branch from
  D5's bootstrap drill PR on.
- **The runbooks, by owner.** Five steps name `./scripts/deploy.sh` for the cluster at
  `70591c307` (Baseline). R4 edits one of them:
    - `docs/break-glass.md`, *daniel-box is dead*, step 4: D5's flux PR replaces it with a
      link to the bootstrap section. H1.2 writes its bring-up order into `docs/break-glass.md`
      first when H1.2 lands before that PR.
    - `docs/longhorn-disaster-recovery.md`, *Procedure (fresh host, total loss)*, step 5:
      D5's flux PR, after H1.2 wrote the order there.
    - `docs/k3s-etcd-restore.md`, *After the restore*, step 4: D5's flux PR.
    - `docs/break-glass.md`, the shed scenario's step 3: H1, in its rewrite of that
      scenario.
    - `docs/break-glass.md`, the laptop scenario's step 3: R4 PR 8. After PR 8 the cluster
      changes only through Flux's promotion, so the step names that path for the cluster
      and keeps `./scripts/deploy.sh` for what Ansible still deploys.

  PR 8 also edits any line of these runbooks, or of D5's section, that names a path PR 8
  deletes. Such an edit changes the cited path and no step. The `vanished-identifier` lint
  flags each line (*Risks*, failure class 8).

### Where each half of a task file goes

| Half | Successor | Owner | R4's part |
|---|---|---|---|
| Render (include vars) | Role `defaults/main.yml` | R4 PRs 2, 3, 5 | Moves it |
| Task-made facts | Functions in `renderer_lib` | R4 PR 4 | Moves it |
| Apply, prune, drain, restart | Flux `wait` and `prune`, the soak rule | D4, D5 | Deletes the code |
| Verify | A rule the soak Job reads (`severity="critical"` or `verify="true"`), a `probe.py health` check for a `k8s_autodeploy: false` role, or a `<tag>-verify` Job | D5 | Checks it, PR 1 |
| In-cluster writes | A Job named by role digest, or an initContainer | D5 | Checks it, PR 1 |
| Host files | A setup role, then a host package (C5) | D5, then H2 to H5 | Checks it, PR 1 |
| Release and render records | Flux commit statuses (C6) | D5 disarms, D6 deletes readers | Deletes the producer, PR 6 |

### File tree

```diff
- ansible/roles/k8s/*/tasks/                      (86 files, less the runbook allowlist)
- ansible/roles/k8s/manifests/                     (whole role, once no default is a renderer input)
- ansible/roles/k8s/{image-builder,cronjob-gate,arr-notification,volume-snapshot}/tasks/
  ansible/roles/k8s/{image-builder,arr-notification}/   (templates, files, defaults stay)
  ansible/roles/k8s/{volume-revert,longhorn-api}/tasks/   (runbook allowlist, until H7)
  ansible/roles/k8s/*/defaults/main.yml            (+ render metadata from include vars)
  ansible/deploy.yml                               (- the k8s play)
- ansible/tasks/k8s_batch.yml, ansible/tasks/k8s_retire.yml
- ansible/post_tasks/k8s_stabilise_gate.yml, ansible/post_tasks/k8s_image_drift_gate.yml
- ansible/filter_plugins/k8s_retire.py
  ansible/filter_plugins/toposort.py               (- ansible_owned; ValueError subclass)
  ansible/filter_plugins/authelia_access.py        (ValueError subclass)
  ansible/inventory/group_vars/all.yml             (- k8s_retired_services, k8s-play-only keys,
                                                     render_records_*)
- ansible/roles/setup/render_records/
  ansible/initial_setup.yml                        (- the render_records entry)
  ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py
                                                   (- render_records from _KUMA_CHECK)
  scripts/secrets_mgmt/consumers.py                (- render_records_push_token, after PR 6)
- scripts/diagnostics/probe_lib/releases_render.py (if D6 left it)
  scripts/validate/render_diff_gates.py            (- the retirement arm; D6.1 names the file)
+ scripts/renderer/renderer_lib/targets.py         (render_targets logic, task-free)
+ scripts/renderer/renderer_lib/filters.py         (reimplemented ansible-core filters)
+ scripts/renderer/renderer_lib/derived.py         (content tag, checksums, declarations)
- scripts/deploy_tools/render_targets.py           (PR 6, with its last caller)
  scripts/renderer/shadow_diff.py                  (- compare, stubcheck; keeps stubs, digest,
                                                     roundtrip)
  scripts/lib/k8s_roles.py                         (reads defaults; is_leftover_dir rule)
  scripts/lib/ansible_jinja_env.py                 (imports the reimplemented filters)
  scripts/validate/k8s_manifests.py                (validates the renderer's output)
  scripts/dev/new_k8s_service.py                   (scaffolds no tasks/)
  scripts/docs/gen_role_glance.py                  (marker names no tasks)
  ansible/tests/_k8s_render.py                     (renders through the renderer; - deploy_play)
  .github/workflows/render-diff.yml                (PR 1 adds it, PR 7e adds the pin hazard,
                                                   PR 11 deletes it)
  .github/pull_request_template.md                 (PR 1 adds Render-diff:, PR 11 removes it)
  ansible/roles/setup/gitops_deploy/defaults/main.yml  (render-diff context: PR 1 +, PR 11 -)
  docs/break-glass.md                              (the laptop scenario's step 3, PR 8)
+ docs/adr/<next free>-the-renderer-supports-a-closed-jinja-subset.md   (PR 7d)
  scripts/renderer/tests/                          (+ dialect spec tests and census, PR 7d)
```

R4 adds no bootstrap workflow and no bootstrap section. D5 owns both (*Bootstrapping Flux
without Ansible*).

## Steps, as PRs

No PR in R4 moves ownership of a live object, so the two-writer rule (C4) has nothing to
check beyond PR 1's precondition.

PRs 6 and 8 are deletion PRs, sized by subject rather than by line count. That sizing rests
on a contract amendment that has not landed (Open question 11). A deletion PR removes one
subject together with the tests whose subject it is and the docs that describe it. Its
description carries the census command that proves nothing still names what it deleted.
Until G2 retires `docs/facts.lock`, it also names every lock row it moves:
`fact_status.py verify` for a section it edits, and `fact_status.py forget` for a section
it deletes. Every other PR stays within about 200 lines. Every PR carries a `Render-diff:` line and quotes the
deployer route of its paths, measured with `services_from_changed_paths` at its base.

1. **Inventory, the precondition and the render-diff gate** (about 200 lines).
    - The ownership test in `scripts/renderer/tests/`: every k8s service is flux-owned.
      Before H1 it reads `owner: flux` in `containers_list`; after H1 it reads the
      Kustomizations.
    - The precondition test from *R4 deletes, D5 converts*, with its `flux` exemption and
      its six fixtures, and the weaker allowlist test: each allowlisted runbook file exists.
    - The `render-diff` workflow, with its run time and the `flux/` count for its floor
      recorded in the description. Both renders take the PR head's SHA through the
      renderer's commit input (*The render-diff gate*), so this PR waits until a contract
      names that input (Open question 10). The operator pushes the workflow file, and adds
      its context to the live ruleset when this PR merges (**operator-only**). This PR adds
      the same context to `gitops_deploy_expected_ruleset_contexts`, and the `Render-diff:`
      section to `.github/pull_request_template.md`.
    - The description lists every baseline item still in the tree with the command that
      found it, maps each verify row to its D5 successor (exit 5), and quotes the deployer
      route of every path R4 will touch. The mapping accepts only C7's three shapes.
    - The same mapping covers the rows of `flux`'s `tasks/main.yml` that the precondition
      exempts, each to the successor the description quotes:
        - the `rollout status` wait on the three controllers and the restart-count snapshot:
          the `flux` Kustomization's health wait and the soak rule, as for the apply and
          restart half of any role (*Where each half of a task file goes*);
        - the CRD wait and the deferred apply of the sync objects: the `flux-deferred`
          Kustomization, which applies the sync objects after `flux` (D5, *Who applies the
          Flux objects after the flux PR*), and D5's bootstrap section on a rebuilt cluster;
        - the delete of `ocirepository/verify-drill` while `flux_verify_drill` is false:
          the prune of `flux-deferred`, when the render drops that object. The description
          quotes the `prune` field of the rendered `flux/flux-deferred.yaml`. If it is
          `false`, the row has no successor and goes back to D5.
    - It re-measures four inputs to later PRs:
        - the set of include keys the post-D5 renderer reads, rollout keys included, which
          PRs 3 and 5 use (*Which keys move*);
        - the defaults of the manifests role and the utility roles that the renderer reads.
          A render with that role's `defaults/main.yml` emptied, diffed against the normal
          render, names them;
        - the 270 files the wider selector lists, which name the play, the batch, the
          post_tasks, a role's `tasks/` or the manifests role's `defaults/` and `files/`.
          Each is assigned to PRs 3 to 5, PR 6, PR 7c or PR 8, or marked as code that is not
          a test (Baseline, *Kept tests read the play*);
        - the `docs/facts.lock` rows that cite a path R4 deletes, each assigned to the PR
          that deletes it.
    - It records the revert runbook's entry point. If `ansible/volume-revert.yml` exists and
      includes `k8s/volume-revert` alone, PR 7a is skipped.
    - It confirms the render_records units are gone. If they are not, a first PR disarms the
      producer through its role defaults (*render_records*), and `land.sh` follows the tick's
      apply.
2. **Readers take defaults first** (about 120 lines). `k8s_roles._named_files` and
   `task_inputs.include_vars` read `defaults/main.yml`, then `tasks/`, and raise on a key
   both set. Tests in `scripts/lib/tests/`: a fixture role with the key in both sources
   raises, and one with the key in either source alone resolves the same lists.
   `Render-diff: none`.
3. **Render metadata to defaults, in four role groups** (each under 150 lines). The twelve
   explicit-list roles, the nine image-builder callers, then the rest of the 43 task files.
   Each group moves every key of the set PR 1 measured, the rollout keys included, and
   leaves the Ansible-only keys for PR 8. Each moved key adds a `noqa` line. The baseline's
   file-list keys give about 40 between the four groups, and the rollout keys add more, so
   PR 1 states the count. Each PR deletes a key from `tasks/` in the commit that adds it to
   defaults. `Render-diff: none`. If D5.4's `images` job reads `image_builder_*` vars from
   tasks, the image-builder group switches it to `image_builder_builds` in the same PR.
4. **Task-made facts into `renderer_lib`** (about 200 lines). The content-tag rule, the four
   script checksums and `kuma_declarations`, with its two asserts, become functions in
   `renderer_lib/derived.py`. The janitorr template reads `janitorr_k8s_dry_run`.
   `test_image_builder_content_tag.py` and D5.4's tag test target the new functions, with
   ansible-core's `Templar` as the oracle. A test feeds a declaration list under the floor
   and a malformed push token, and each must raise. `Render-diff: none`.
5. **Readers take defaults only** (about 180 lines). Drop the `tasks/` read from both
   readers, with the two hard errors from *The renderer stops reading tasks*. Their red
   halves: a fixture role with no `defaults/main.yml`, and one with `manifests_deferred_files`
   left in `tasks/main.yml`; each must raise. A green fixture leaves an Ansible-only
   `manifests_*` key in `tasks/main.yml`, and the render passes. Move `render_targets()`
   into `renderer_lib/targets.py`, repoint R1's importers at it, and add the equality and
   floor tests. `scripts/deploy_tools/render_targets.py` stays until PR 6. Teach
   `scripts/dev/new_k8s_service.py` the defaults location. `Render-diff: none`.
6. **Delete render_records** (deletion PR, about 650 lines removed in the role alone). One
   commit deletes the role with its tests, its `initial_setup.yml` entry, `render_records`
   from `deploy_cross_role.py:_KUMA_CHECK`, its `render_records_*` keys in
   `group_vars/all.yml`, the `render-records` Kuma tile if M6 has not retired Kuma, and the
   readers D6 left (*render_records*).
    - **Tests whose subject goes:** the role's own `tests/`,
      `ansible/tests/setup/test_render_records_role.py`, the render_records rows of the
      tests that list setup roles, `scripts/deploy_tools/tests/test_render_targets.py`, and
      PR 5's equality test. `scripts/deploy_tools/render_targets.py` goes too, because
      `render_records.py` was its last caller. PR 5's floor test stays.
    - **Lock rows:** `forget` the three `ansible/roles/setup/render_records/CLAUDE.md` rows.
      Edit `ansible/roles/k8s/manifests/CLAUDE.md#Release records`, which cites the role's
      directory, and `verify` it, unless D6 already removed that section.
    - **The park and the owed apply.** The range parks the tick (`broad_manual`), with the
      route quoted in the Baseline. Extra commits in the parked range are the expected
      case, not an exception. The comment above the CI walk depth in
      `ansible/roles/setup/gitops_deploy/defaults/main.yml` measures about 124 merges a day,
      a median gap of 315s, against a 10-minute tick. So a commit that merged after the last
      tick usually parks together with PR 6, and step 3 below routes it. No check before
      the merge can hold the range to one commit, because another merge can land between
      the check and PR 6's own merge.
      This step departs from the park alert and from `docs/gitops-pipeline.md` *When a tick
      parks*. Both say to fast-forward to `origin/master`, which would carry any later
      commit past the tick unrouted (*render_records*). The alert also names a full
      `deploy.yml`, the whole `initial_setup.yml` and the deleted `render_records` role
      (Baseline). Step 5 below is the whole owed set, and the operator runs none of the
      alert's commands. The PR description says so above the commands.
      **Operator-only**, on daniel-box, in the primary checkout, in one sitting:
        1. Read PR 6's squash SHA with `gh pr view <n> --json mergeCommit`.
        1. Run `git rev-list --oneline HEAD..<PR 6 sha>`. PR 6 is one line of its output,
           because `land.sh` squash-merges (Baseline). Any other line is a commit that
           merged after the last tick and before PR 6, which is the usual case.
        1. If it prints more than PR 6's line, the extra commits park with PR 6. Quote
           `services_from_changed_paths` over `git diff --name-only HEAD..<PR 6 sha>`, with
           the Baseline's command, and name every apply the change set owes besides
           `gitops_deploy`. A setup role owes `initial_setup.yml --tags <role>`, the apply
           the tick would have run, and a bring-up playbook owes its own run. Then assign
           each extra commit an owner:
            - A commit with no live session is the operator's. A Renovate automerge is
              one, and so is a commit whose session has ended. The operator runs its
              applies in step 5 and waits for no counterpart.
            - A commit whose session is still live is that session's to clear (*When a
              tick parks*), so the operator agrees its applies with that session before
              step 4. `deploy_defer.py:park` merges nothing and re-parks the whole
              `local..origin` range on every tick, so every landing waits on this step. A
              session that does not answer at once hands its commit to the operator, who
              runs its applies in step 5.
        1. Run `git merge --ff-only <PR 6 sha>`, not `origin/master`.
        1. Run `initial_setup.yml --tags gitops_deploy`, then every apply step 3 named.

      Commits that merged after PR 6 stay in the `<PR 6 sha>..origin/master` range, and
      the next tick routes them. A tick that fires between steps 4 and 5 routes that range
      with the old `/opt/gitops-deploy`, whose `_KUMA_CHECK` still names `render_records`,
      so step 5 follows step 4 at once. The description quotes the output of steps 2 and
      3. The `group_vars/all.yml` edit needs nothing more, because a deploy-plane path
      applies nothing after D6.1.
    - **Operator-only, after this PR:** the secrets commit from *render_records* removes
      `render_records_push_token` with `sops`, runs
      `uv run python scripts/secrets_mgmt/secret_rotation.py sync`, and drops the token
      from `consumers.py:CROSS_HOST_PUSH_TOKENS`. It merges without a deploy and alerts
      once.
    - **Operator-only:** remove `/var/lib/homelab/k8s-renders.d` on daniel-box. It needs
      root, which the agent's permissions deny.
7. **Five prerequisites of PR 8.**
    - **PR 7a: a standalone entry for the revert runbook** (about 60 lines). It runs only if
      PR 1 found no `ansible/volume-revert.yml`, or found that D5's command still reaches
      `volume-revert` through the manifests role or `deploy.yml`. D5.2c ships that playbook,
      so this PR is normally skipped. It adds a playbook that includes `k8s/volume-revert`
      alone. **Operator-only:** before PR 8, run D5.2c's speedtest drill through the new
      playbook on production. The PR states its blast radius in C8's three parts:
        - **Objects touched:** the `speedtest` Deployment, its Longhorn volume, and the
          `speedtest` and `speedtest-snapshot` Kustomizations, which are suspended and then
          resumed. The volume returns to the drill's snapshot, so speedtest results recorded
          after that snapshot are lost.
        - **What keeps serving:** every other service. speedtest is down from the scale to
          zero until its resume. `DEPLOY_HOLD` holds every promotion from the drill's
          breaking PR until the drill's last step clears it.
        - **Rollback:** the runbook's own resume steps. If the revert fails, the volume's
          hand recovery in `ansible/roles/k8s/volume-revert/CLAUDE.md` applies, and
          speedtest stays down until it runs.
    - **PR 7b: the bootstrap check** (no file changes, so no PR of its own; PR 8's
      description carries it). It keeps its number because other slices cite it. D5's
      bootstrap drill PR and flux PR own the procedure, its drill, its renderer test and
      the operator's `age-keygen -y` comparison. PR 7b checks the four properties that
      *Bootstrapping Flux without Ansible* lists, at PR 8's merge base, each in its own way:
        - Property 1: D5's flux-PR text test over the bootstrap section, in
          `ansible/tests/repo/`. CI's `pytest` job runs the whole suite on every PR, so
          PR 8's green `pytest` job covers it. The description names the test's result
          line from that job.
        - Property 2: one `awk` program over D5's section, which must exit 0 and print
          nothing. It also refuses a hand suspend of the root, which property 1 forbids.
        - Property 3 has no mechanical half. The description quotes the section's render
          step, which names `prod`'s commit.
        - Property 4: the latest master run of D5's drill, quoted with its three jobs.

      ````bash
      awk '
        /^[ \t]*(```|~~~)/ { fence = !fence }
        !fence && /^#+ / {
          if (f) { if (index($0, " ") <= l) exit }
          else if ($0 ~ /^#+ Bootstrapping Flux on a rebuilt cluster$/) { f = 1; l = index($0, " "); next }
        }
        f {
          n++
          if ($0 ~ /runbook_gates\.py longhorn-dr/) a = 1
          if ($0 ~ /\.\/scripts\/deploy\.sh|ansible-playbook [^ ]*deploy\.yml|roles\/k8s\/flux|^[ \t]*flux_sync_suspend: *true|flux_sync_suspend=|(patch|suspend) kustomizations?[^ ]*[ \/]homelab( |$)/) { h = 1; print FNR ": " $0 }
        }
        END {
          if (!f) { print "no section read" > "/dev/stderr"; exit 2 }
          if (n < 20 || !a) { print "section: " n " lines, DR gate anchor " (a ? "present" : "absent") > "/dev/stderr"; exit 3 }
          if (h) exit 1
        }' docs/flux-deploy-plane.md
      ````

      The program reads the section from its heading to the next heading of the same or a
      higher level, so the section's own subheadings stay inside the check. Its exit codes
      say what it read:
        - **0:** the section was read whole, and no forbidden command form is in it.
        - **1:** a forbidden form is in the section. The program prints each such line
          with its line number.
        - **2:** no section was read. awk exits 2 itself when the file is missing, and the
          `END` block exits 2 when no line matches the heading.
        - **3:** the section has fewer than 20 lines, or it never names
          `runbook_gates.py longhorn-dr`. D5's procedure has nine numbered steps and a
          paragraph on the field manager, so any whole copy of it is longer than 20 lines.
          A section that never names the DR gate cannot carry property 1's order.

      The fence toggle applies to the heading match only. A line inside a fenced code
      block never ends the section and never starts it, so a column-zero `# ` shell
      comment in a code block, which the DR runbooks use, cannot stop the read early.
      Every line inside a fence is still scanned for the forbidden forms, because the
      runbook's commands live in code blocks. The patterns name command and YAML forms, so a
      sentence that says `deploy.sh --tags flux` exits 5 does not match, and neither does
      D5's prose on why the root is withheld rather than suspended. That prose says the
      renderer refuses `flux_sync_suspend: true`, so the variable's patterns match only a
      YAML key at the start of a line and an `-e` assignment. The last pattern catches a
      hand patch or a `flux suspend` of the root Kustomization, and its `homelab( |$)`
      ending leaves `homelab-namespace` out. A sentence that names a forbidden form only to
      rule it out still exits 1. That is the one exit 1 that does not stop R4: the
      description quotes each printed line and says why it rules the form out.

      Its red half runs the same program over `git show <sha>:docs/flux-deploy-plane.md`
      at D5's flux PR merge commit, with awk reading standard input in place of the file.
      There the section still holds the `./scripts/deploy.sh` step that D5's namespace PR
      deletes, and the program prints it and exits 1. Four throwaway copies of that
      section, kept outside the tree, prove the other exits. PR 7b still changes no file.
        - The heading renamed: exit 2.
        - A fenced code block holding a column-zero `# ` comment, placed before the
          `./scripts/deploy.sh` step: the program prints the step and exits 1. The earlier
          `awk | grep` pipeline printed nothing on this copy, because the comment ended its
          read.
        - The line naming `runbook_gates.py longhorn-dr` deleted: exit 3.
        - A `kubectl patch kustomization homelab` line added: exit 1.

      At `70591c307`, where `docs/flux-deploy-plane.md` does not exist, the program exits
      2 with awk's `cannot open file` error. The earlier `awk | grep` pipeline printed
      nothing there and passed. The description quotes every output and exit code. A
      property that fails stops R4, and the gap goes back to D5.
    - **PR 7c: the render harness and the renderer stop reading the play and the manifests
      role** (about 150 lines). PR 8 cannot pass CI while a kept test still calls
      `_k8s_render.py:deploy_play`, so this PR moves every kept reader first:
        - `ansible/tests/_k8s_render.py` renders through the renderer, and `deploy_play`
          goes.
        - The LimitRange content tests in `test_namespace_resource_defaults.py` read the
          `homelab-namespace` render instead of the play's pre_task, unless D5 already
          moved them. `test_the_namespace_apply_reports_changed_per_line` and
          `test_k8s_manifests.py:test_the_k8s_play_does_not_filter_an_already_filtered_list`
          stay for PR 8, because their subject is the play.
        - Every other test PR 1 assigned here is retargeted the same way.
        - Each manifests-role default that PR 1 found the renderer reads moves into
          `renderer_lib` as a named constant, with its source line quoted. A utility role's
          renderer inputs stay where they are, because PR 8 keeps those roles.

      `Render-diff: none`. The description quotes `grep -rln deploy_play ansible/tests
      scripts --include='*.py'`, which prints no kept test.
    - **PR 7d: the dialect ADR and its spec tests** (about 200 lines, most of them
      fixtures). It adds the ADR, the spec tests, the construct census and the literal set
      they read (*The renderer's dialect is specified*). It merges before PR 8, because its
      precedence fixtures are checked against an `ansible-playbook` render. Approving the ADR is
      **operator-only**. `Render-diff: none`.
    - **PR 7e: the renderer's dependency pins join the hazard list** (about 60 lines). It
      adds the closure read, its census test and the template's wording (*The render-diff
      gate*). The operator pushes the workflow change, as for PR 1. `Render-diff: none`.
8. **Delete the k8s play and the manifests role** (deletion PR).
    - Delete the play, the batch and retire task files, the two post_tasks, and every
      `tasks/` directory under `ansible/roles/k8s/` outside the runbook allowlist. Delete
      the manifests role whole: PR 7c moved every default the renderer read from it.
    - Delete only the `tasks/` of `image-builder`, `cronjob-gate`, `arr-notification` and
      `volume-snapshot`. A utility role goes whole only when a grep of `scripts/renderer/`
      and `scripts/lib/` for the role's directory, and for each of its template, file and
      default names, prints nothing, and the description quotes that grep. At the baseline
      `image-builder` keeps `templates/context-configmap.yaml.j2` (D5.4) and
      `arr-notification` keeps `files/seed_arr_notification.py` (D5's arr verify Jobs). The
      render-diff run is the backstop: a deleted renderer input moves a byte or fails the
      render.
    - **Tests whose subject goes, in this PR.** PR 1's precondition test goes, and PR 1's
      allowlist test becomes the equality test from exit 1. The Ansible-mechanics tests PR 1
      assigned here go, `test_the_k8s_play_does_not_filter_an_already_filtered_list` and
      `test_the_namespace_apply_reports_changed_per_line` among them.
      `test_derived_manifest_files.py` holds the manifests role's copy of the file-list rule
      in step with `k8s_roles.py`'s, so that comparison goes, and any test of the
      `k8s_roles.py` copy alone stays. Each test's citations leave the docs in the same PR,
      as `.claude/rules/python-layout.md` (*A check retires when its subject does*)
      requires.
    - Delete the `ansible_owned` filter, `ansible/filter_plugins/k8s_retire.py`,
      `k8s_retired_services` with the retirement arm of D6's
      `scripts/validate/render_diff_gates.py`, which D6 leaves to R4, and every other
      `group_vars/all.yml` key that only the k8s play, its task files or the manifests role
      read. The description quotes the grep that shows each key has no other reader. A key
      that an allowlisted runbook file reads stays: at the baseline, `k8s_namespace`,
      `k8s_dry_run` and `k8s_no_mutate`. `k8s_autodeploy_snapshot_pvcs` stays in role
      defaults, because `<tag>-snapshot` reads it.
    - **Lock rows:** `forget` the four `ansible/roles/k8s/manifests/CLAUDE.md` rows. If D5
      left crowdsec's task files, edit and `verify` the two crowdsec rows that cite them.
      Root `CLAUDE.md#Adding a New Service` and `janitorr/CLAUDE.md#Notable` keep their
      `ansible/deploy.yml` path atom, but their prose describes the k8s play's toposort, so
      edit and `verify` both.
    - **The runbook still resolves.** A test in `ansible/tests/longhorn/` lists every
      variable the allowlisted task files read. It also lists the names that a
      `group_vars/all.yml` value it uses reads in turn, so `k8s_no_mutate` brings in
      `ansible_check_mode` and `k8s_dry_run`. Each name must resolve from one of five
      sources:
        - those roles' own defaults;
        - `group_vars/all.yml`;
        - a `register`, `set_fact` or loop variable in those files;
        - the three `-e` inputs D5.2c's runbook passes (`volume_revert_service`,
          `volume_revert_claims`, `volume_revert_sha`);
        - an allow-set of Ansible magic variables and gathered facts, which the test
          enumerates name by name. At the baseline it holds two names.
          `ansible_check_mode` is a magic variable, which the value of `k8s_no_mutate` in
          `group_vars/all.yml` reads. `ansible_hostname` is a gathered fact, which
          `ansible/roles/k8s/longhorn-api/tasks/resolve.yml` reads four times. PR 8
          re-measures the set with a grep for `ansible_` over the allowlisted task files
          and quotes it.

      A gathered fact resolves only when the play gathers facts. So the test also reads
      `ansible/volume-revert.yml`, D5.2c's or PR 7a's, and asserts that its play gathers
      them: it leaves
      `gather_facts` unset or true, or it runs `ansible.builtin.setup`, as
      `ansible/pre_tasks/load_secrets.yml` does unless `load_secrets_gather_facts` is false.
      The repo's `ansible.cfg` sets no `gathering`, so an unset `gather_facts` gathers.
      Its red halves:
        - a fixture copy of `group_vars/all.yml` without `k8s_no_mutate` fails the test;
        - a fixture task file that reads `ansible_fqdn` fails it, because the allow-set
          names facts one by one and admits no prefix;
        - a fixture copy of the playbook with `gather_facts: false` and no setup task
          fails it.
      The operator decides in this PR's review whether to rerun D5.2c's speedtest drill
      after the merge (**operator-only**), with the blast radius PR 7a states.
    - Change the marker `scripts/docs/gen_role_glance.py` writes so it no longer names
      `tasks`. The `regen-role-glance` hook rewrites every k8s role's glance block in this
      PR.
    - This PR deletes the flux role's `tasks/main.yml`, which D1 added. No path has run it
      since D5's flux PR made `flux` flux-owned (*Bootstrapping Flux without Ansible*).
      PR 1's precondition exempted it by name until now, and this PR deletes that test with
      the file, so the exemption goes too. A
      rebuilt cluster gets Flux from D5's *Bootstrapping Flux on a rebuilt cluster* in
      `docs/flux-deploy-plane.md`, which this PR cites and does not restate. PR 7b's four
      properties must hold at this PR's merge base. The description quotes PR 7b's outputs
      and exit codes, and a green run of D5's drill on this PR's head, all three jobs.
    - **The runbooks.** This PR rewrites the laptop scenario's step 3 in
      `docs/break-glass.md` and no other cold-start step. D5's flux PR owns the three steps
      that send the operator to `./scripts/deploy.sh` to rebuild the cluster, and H1 owns
      the shed scenario (*Bootstrapping Flux without Ansible*, *The runbooks, by owner*).
      The description quotes the Baseline's `grep -n "deploy.sh"` over the three runbooks
      after the edit. Every line it prints uses `deploy.sh` for something other than the
      cluster. A line that still sends the operator to it for the cluster stops this PR,
      and the gap goes to the slice that owns that step.
    - **Route.** The deploy-plane paths, `ansible/deploy.yml`, `ansible/tasks/`,
      `ansible/post_tasks/`, `ansible/filter_plugins/` and `group_vars/all.yml`, fast-forward
      with no apply from D6.1 on (D6, *The route of a deploy-plane path after D6*). PR 1
      re-measures that route and the route of a deleted role `tasks/` path after D6.1, and
      this PR quotes both.
    - `/var/lib/homelab/k8s-releases.d` loses its last writer. **Operator-only:** remove it on
      daniel-box once D6 has deleted its readers.
9. **Retire the Ansible-tree comparison and reconcile the count** (deletion PR, small).
   PRs 6 and 8 deleted the tests whose subject they deleted. This PR deletes what is left
   and reconciles the totals.
    - **`shadow_diff.py` loses `compare` and `stubcheck`.** R1 says these two are the only
      subcommands that read an Ansible tree, and that R4 deletes them "once `compare` and
      `stubcheck` have no subject" (R1, *Design*). PR 8 deleted that subject. `compare` also
      runs `stubcheck`'s rule first, so the two go together, with their tests. The
      render-diff job's `diff -r` replaces `compare`'s C1 half.
    - Three subcommands outlive Ansible and stay. `stubs` writes the stub file that R1's
      determinism module renders with. `digest` serves that module and the render-diff job.
      R2's `roundtrip` proves, through `flux build`, that the placeholder render substitutes
      back to the stub render. R1 says the first two "outlive the Ansible path that R4
      deletes" (R1, *Design*). The third checks a renderer property that has nothing to do
      with Ansible.
    - The entry point and those three names do not change, so R2's CI step, which runs
      `shadow_diff.py roundtrip`, stays as it is, and no workflow changes. R1 adds no
      workflow step. The description greps `.github/workflows/` and `scripts/` for
      `shadow_diff`, and every hit names a kept subcommand.
    - The description reconciles the tests R4 deleted in PRs 6, 8 and 9 with D6's "about
      70" and with the 102 files in the baseline.
10. **Reimplement the filters and switch the validator** (about 200 lines).
    `renderer_lib/filters.py` with one parity test per filter, and `ansible_jinja_env.py`
    importing it. The `ValueError` subclass in `toposort.py` and `authelia_access.py`.
    `is_leftover_dir`'s rule in `scripts/lib/k8s_roles.py`, if D6 left the import.
    `scripts/validate/k8s_manifests.py` reads the renderer's output. The ansible-free
    runtime test. `Render-diff: none`.
11. **Retire the render-diff gate** (about 60 lines removed). It runs after every other R4
    PR has merged, in the order *The gate ends with R4* gives. **Operator-only:** remove the
    `render-diff` context from the live ruleset first. The PR then removes it from
    `gitops_deploy_expected_ruleset_contexts`, deletes `.github/workflows/render-diff.yml`,
    which the operator pushes, and removes the `Render-diff:` section from
    `.github/pull_request_template.md` and the gate's section from
    `docs/homelab-render.md`. Its route is `gitops_deploy`'s setup arm, which the tick
    applies itself. The description lists every R4 PR with its `render-diff` run and
    conclusion, which is exit 3's evidence.

## Verification

**Exit checks:**

```bash
git ls-files 'ansible/roles/k8s/*/tasks/*'     # exactly the runbook allowlist
grep -c "^- name: Deploy k8s workloads" ansible/deploy.yml                 # 0
uv run pytest
```

The ansible-free runtime test and the runbook-allowlist equality test run inside
`uv run pytest`.

**Exit 3** is the conjunction of the per-PR runs. For each R4 PR from PR 1 to PR 10, the
`render-diff` run on its last commit concluded `SUCCESS`, and its body's `Render-diff:` line
is `none` or the exact list the run compared:

```bash
gh pr view <n> --json statusCheckRollup,body \
  --jq '[.statusCheckRollup[] | select(.name == "render-diff") | .conclusion],
        (.body | split("\n") | map(select(startswith("Render-diff:"))))'
```

To reproduce one PR's verdict by hand, render its merge base and its head with the same
commit value. `--commit` stands for the input Open question 10 asks C1 to name.

```bash
git worktree add --detach /tmp/r4-base <merge base>
uv run python /tmp/r4-base/scripts/renderer/homelab_render.py cluster \
  --out /tmp/r4-base-out --commit <head sha>
uv run python scripts/renderer/homelab_render.py cluster \
  --out /tmp/r4-head-out --commit <head sha>
diff -r /tmp/r4-base-out /tmp/r4-head-out && echo identical
ls /tmp/r4-base-out/flux | sort > /tmp/r4-base.tags
ls /tmp/r4-head-out/flux | sort > /tmp/r4-head.tags
diff /tmp/r4-base.tags /tmp/r4-head.tags && wc -l < /tmp/r4-head.tags
ls -d /tmp/r4-head-out/flux/pihole.yaml /tmp/r4-head-out/flux/traefik.yaml \
  /tmp/r4-head-out/cluster/pihole-deferred
```

`diff -r` must print nothing, or only the files the PR named. The tag lists must match unless
the PR named the `flux/<tag>.yaml` that differs, the count must be at least the floor PR 1
recorded, and the three named members must exist.

On daniel-box, `systemctl list-unit-files 'kuma-check-render-records*'` lists nothing, and
`/opt/render-records` and `/etc/render-records` are gone.

Exit 6 is read at R4's last PR with `git ls-files 'scripts/renderer/*.py' | grep -v
/tests/ | xargs cat | wc -l`, which prints at most 2,600.

**Red-proofs:**

- The dialect census (PR 7d): a fixture template that uses a filter off the list fails the
  census and the render, and the failure names the filter. A precedence spec test run
  against a renderer with `group_vars` and `host_vars` swapped fails.
- The dependency-pin hazard (PR 7e): a fixture pair whose `uv.lock` bumps Jinja2 with no
  `Render-diff:` line fails the job, and the same pair bumping an unrelated test dependency
  passes. A fixture renderer module that imports a package outside the literal set fails
  the census.

- The ownership precondition: a fixture service without Flux as its writer fails the test,
  and a flux-owned one passes.
- The task precondition: a fixture entry whose role has one `kubectl` command task fails it,
  and a fixture domain without `traefik` fails the member floor. A role that imports a
  sibling file holding a `kubectl` command task fails, and so does a role that imports
  `{{ role_path }}/../../setup/common/tasks/install_host_lib.yml`. A role that imports a
  sibling file holding only a permitted `set_fact` passes.
- The `flux` exemption: a fixture domain that holds a flux-owned `flux` with D1's tasks and
  one other role with a `kubectl` command task fails, and the failure names the other role
  alone. A fixture `flux` entry without the ownership flag fails on `flux`. On the real tree
  at PR 1, the test passes with `flux` in the domain, and PR 1's description quotes flux's
  rows with their successors.
- The runbook allowlist: PR 1's existence test fails on a fixture tree missing an
  allowlisted file. PR 8's equality test also fails on a fixture tree with one extra task
  file.
- The render-diff job: PR 1's description shows the job failing in five cases on branch
  commits. One edits a `flux/<tag>.yaml` field under `Render-diff: none`. One touches
  `scripts/renderer/` with no `Render-diff:` line. One renders an empty tree on both sides.
  One removes `cluster/pihole-deferred/`. One raises `k8s_rollout_stabilise_seconds` above
  the longest `for:` among the soak's rules, which moves the soak Job's wait W (D5, *The
  soak as a Job*), under `Render-diff: none`, and the run names a file under
  `cluster/soak/`. The first passes once the line names that file.
- The commit pin: PR 1's head changes no render input, yet its SHA differs from its merge
  base's. Its run under `Render-diff: none` passes, which shows the pin covers every
  commit-derived byte, the soak Job's included. The same comparison run by hand, with each
  render given its own SHA, shows `cluster/soak/` differing. So the soak is in the render,
  and the pin is what made the first run pass.
- The readers' hard errors: a fixture role with no `defaults/main.yml`, and a fixture role
  with `manifests_deferred_files` in `tasks/main.yml`, each raise. A fixture role with an
  Ansible-only `manifests_*` key in `tasks/main.yml` renders.
- The task-free service set: the equality test computes both predicates over one tree. A
  fixture role with no manifest template is left out of both. The floor test fails on a
  fixture that drops `traefik` from the new predicate.
- The bootstrap check (PR 7b): its `awk` program exits 0 and prints nothing at PR 8's
  merge base. At D5's flux PR merge commit it prints the bootstrap section's
  `./scripts/deploy.sh` step, which D5's namespace PR deletes later, and exits 1. A copy
  with the heading renamed exits 2, and so does the tree at `70591c307`, where the file
  does not exist. A copy with a fenced `# ` comment before that step still prints the
  step. A copy without the DR gate line exits 3. Property 1's order is D5's flux-PR text
  test, green in PR 8's `pytest` job. The hand-applied-documents renderer test, the
  bootstrap-section text test and their red fixtures are D5's, and R4 adds none of them.
- Each reimplemented filter: its parity test feeds the inputs that make ansible-core's
  version differ from a naive one. `bool("false")` must be False, `to_uuid` must be stable,
  and `hash('sha256')` must match `hashlib`. A test that swaps in Python's `bool()` must fail.
- The ansible-free check: the shim that makes `renderer_lib.filters` import `ansible` makes
  the subprocess fail.
- The runbook-resolution test: a fixture copy of `group_vars/all.yml` without
  `k8s_no_mutate` fails it. So does a fixture task file that reads `ansible_fqdn`, which the
  allow-set does not name, and a fixture copy of `ansible/volume-revert.yml` with
  `gather_facts: false` and no setup task. The real tree passes, with `ansible_check_mode`
  and `ansible_hostname` resolved from the allow-set.
- The bootstrap drill on PR 8's head: D5's drill runs all three of its jobs. The green job
  passes when the claim keeps the marker label and UID the restore stand-in gave it,
  because the root did not exist until the stand-in had run. The second job skips the
  `sops-age` step and passes only when `cluster-vars` fails to decrypt. The third job
  applies the root before the stand-in and passes only when the claim check fails.
- The render-diff context: after PR 1 merges and the operator adds the context to the
  ruleset, a throwaway PR with a red render-diff run shows the merge blocked. PR 2's
  description quotes it.

**PR 6's landing.** PR 6's description quotes three outputs from the primary checkout on
daniel-box. The operator produces them, because each needs the host:

- before the fast-forward, `git rev-list --oneline HEAD..<PR 6 sha>`, with PR 6's line
  and any extra commits, each with the route, the applies and the owner step 3 named for
  it;
- after it, `git rev-parse HEAD`, which equals PR 6's squash SHA and not a later commit;
- after the owed apply, `cmp ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py
  /opt/gitops-deploy/deployer_lib/deploy_cross_role.py`, which prints nothing.

## Rollback

- **PRs 1 to 5 and 10** change how the tree is read. The render-diff gate shows each moved no
  byte, so a revert moves none either. A PR 3 group reverts whole: a partial revert would
  leave one key in both sources, and PR 2's reader raises on that rather than rendering.
- **PR 6.** A revert restores the role, its tests, `render_targets.py`, PR 5's equality
  test, the `_KUMA_CHECK` entry and the playbook entry with `render_records_state: absent`,
  so it re-arms nothing. That revert also edits `initial_setup.yml`, so it parks the tick the
  same way and owes the same `initial_setup.yml --tags gitops_deploy` (**operator-only**).
  The operator clears that park with PR 6's five steps, with the revert's squash SHA in place
  of PR 6's. The fast-forward stops at the revert's commit, never at `origin/master`, and an
  extra commit that the `rev-list` check finds gets its route quoted and its applies run.
  If the operator's secrets commit has landed, revert it first, because the restored role
  and tile reference `render_records_push_token`. That second revert is a `sops` edit too.
  A revert after PR 8 has one more ordering. Revert PR 8 first, or drop PR 5's equality
  test from the revert. The old predicate, `render_targets.py:includes_manifests`, greps a
  role's task files for `k8s/manifests`, and after PR 8 no role outside the runbook
  allowlist has a `tasks/` directory. The old predicate then returns the empty set while the
  new one lists every service, so the restored equality test fails CI. The restored
  `render_targets.py` lists nothing at that point. That is harmless only while the restored
  role stays pinned absent by `render_records_state: absent`, because no unit then runs the
  producer that reads the list.
- **PR 7a.** A revert restores the old runbook path. Revert PR 8 first, because PR 8 deletes
  that path.
- **PR 7b** changes no file, so it has nothing to revert. The procedure and its drill are
  D5's, and so is their rollback.
- **PR 7c.** A revert restores `deploy_play` and the play-reading tests, which pass only
  while the play exists. Revert PR 8 first.
- **PR 1's ruleset change.** The operator removes the render-diff context from the live
  ruleset in the same window as a revert of PR 1, or every later merge waits on a job that
  no longer exists.
- **PR 8** deletes code that runs for no flux-owned entry, and the tests whose subject that
  code was, in one commit. A revert restores both together, so the restored tests find
  their subject. The flux role's `tasks/main.yml` comes back as code that no path runs,
  because `flux` stays flux-owned, so the bootstrap still goes through D5's section. It
  also restores the manifests role's defaults. The renderer reads PR 7c's
  constants instead, so the restored file is not a second source for it.
- **PR 9** deletes `compare` and `stubcheck`. A revert restores two subcommands with no
  Ansible tree to read.
- **PR 11.** A revert re-adds the context to `gitops_deploy_expected_ruleset_contexts`, the
  workflow and the template section. The operator re-adds the context to the live ruleset in
  the same window (**operator-only**), and pushes the workflow file.
- **A partial failure** cannot reach the cluster. R4 changes no rendered byte, and the
  Ansible code it deletes runs for no flux-owned entry. Two effects reach a host. The
  render_records absent arm runs through the tick before R4 deletes anything. PR 6 holds
  every later landing until the operator clears its park, and leaves `/opt/gitops-deploy`
  one module behind until the owed apply runs. A fast-forward to `origin/master` in place of
  PR 6's commit would skip the route of every commit merged after PR 6, for good. PR 6's
  procedure stops at its own commit to prevent that.

## Risks

- **Failure class 3 (empty read as clean).** `_named_files` returns `{}` for a missing task
  file, and the renderer would then derive different lists. Mitigation: the three-stage move,
  the hard errors in PR 5, and the render-diff gate on every PR. The gate's floor, three
  named members and a minimum `flux/` count, stops two empty renders from passing, and its
  `Render-diff:` line is mandatory on the hazard paths. PR 1's inventory names its expected
  members: the nine host-side roles, the six `verify.yml` files and the twelve
  explicit-list roles.
- **Failure class 4 (selector drift).** The task-free service predicate could narrow the
  service set. PR 5 computes old and new over one tree and holds a member floor.
- **Failure class 4 (selector drift), for exit 3.** A comparison of R4's first and last
  commits would cover every other session's merge in that span, so it could never pass,
  and an exit check that cannot pass gets waived. Mitigation: exit 3 is the conjunction of
  the per-PR runs, each from its own merge base, so the comparison covers exactly one PR's
  change. The commit pin removes the one difference that every pair of commits has.
- **Failure class 2 (proxy).** The precondition test proves only that D5 removed the tasks.
  Exit 5 also requires each verify successor to be one of three shapes. Two gate every
  promotion through `flux/soak`. The third, a `probe.py health` check, is accepted only for a
  role that Renovate never automerges, because every merge of such a role passes through
  `land.sh`. D5 owns each successor's red-proof.
- **Failure class 2 (proxy), for the render-diff gate.** A green run of a job outside the
  ruleset proves nothing, because a red one does not block the merge. Mitigation: the
  context joins the ruleset before PR 2 merges, and PR 2's description quotes a throwaway
  PR whose red run left the merge blocked.
- **Failure class 5 (sequences as transactions).** render_records is a disarm, then a
  deletion. Deleting first would leave the timer running a role that no longer exists. PR 1
  confirms the units are gone before PR 6.
- **Failure class 5, for PR 6's park.** Clearing the park is a fast-forward, and the apply
  the range owes is a second step that nothing records, because a parked range writes no
  `owed.jsonl` line. Mitigation: PR 6's description names `initial_setup.yml --tags
  gitops_deploy`, and its landing waits for the operator to confirm that apply. The
  fast-forward is a hazard of its own: to `origin/master`, it would also pass every commit
  merged after PR 6, and nothing would route or record those commits. Mitigation: the
  operator fast-forwards to PR 6's squash commit after a `rev-list` check, and quotes the
  route of any extra commit the check finds. The token's removal is a third step, ordered
  after PR 6, so the consumers census never sees a registry token without a consumer.
- **Failure class 5, for PR 8's tests.** A test that reads the play fails the moment the
  play goes. Mitigation: PR 7c moves every kept reader off the play first, and PR 8 deletes
  the rest in the same commit as their subject.
- **Failure class 8 (prose asserts facts).** Many role `CLAUDE.md` files describe their
  `tasks/main.yml`. The `vanished-identifier` lint flags a section that names a deleted
  identifier, so each deletion PR fixes the docs it orphans.
- **The deployer parks on one path (design.md, defect class B).** PR 6 edits
  `initial_setup.yml`, which parks every later landing. PR 8 edits deploy-plane paths,
  which fast-forward with no apply after D6.1 (D6, *The route of a deploy-plane path after
  D6*). Mitigation: each PR quotes its measured route, and PR 1 re-measures PR 8's. The
  operator clears PR 6's park by fast-forwarding to PR 6's squash commit, never to
  `origin/master`, after `git rev-list` lists the span up to that commit. Commits merged
  after PR 6 stay in the tick's next range. At about 124 merges a day, the span usually
  holds commits merged before PR 6 as well. Each gets its route quoted and an owner. The
  operator runs the applies of every commit with no live session, a Renovate automerge
  among them, together with PR 6's own, and waits for no counterpart.
- **The `flux` exemption hides a row** (failure class 2, proxy). A named exemption could
  let flux's install tasks pass the precondition while one of them still has no successor.
  Mitigation: the exemption holds only while `flux` is flux-owned, and a red fixture shows
  it reaches no other role. PR 1 maps each exempted row to a successor, and a row without
  one goes back to D5.
- **A renderer input deleted with a role.** The utility roles and the manifests role hold
  templates, files and defaults the renderer reads. Mitigation: PR 8 deletes only their
  `tasks/`, unless a quoted grep shows a role holds no renderer input. PR 7c moves the
  manifests role's read defaults into `renderer_lib` first, and PR 8's render-diff run fails
  on any byte that still moves.
- **A moved content tag.** A changed tag would rebuild all nine images and roll their pods.
  PR 4's oracle and the render-diff gate hold every tag.
- **Licence.** Copying ansible-core's GPL-3.0-or-later filter code into an MIT repo would
  change the repo's licence terms. The filters are reimplemented, and ansible-core stays a
  test-only oracle.
- **The revert runbook loses its caller or a variable.** D5.2c's playbook, or PR 7a's, is its
  entry before PR 8 deletes the manifests role. PR 8 keeps the three shared keys the runbook
  reads, and its resolution test fails if one goes. The same test fails on a magic variable
  or fact outside its enumerated allow-set, and on a revert playbook that gathers no facts,
  because `ansible_hostname` would then resolve to nothing at run time.
- **A cold rebuild finds no way to install Flux.** D5's flux PR removes the last live
  Ansible install path, and PR 8 deletes its dead code. Mitigation: D5's bootstrap drill PR
  and flux PR land before R4 starts (R4 depends on D6, which follows D5). PR 7b checks D5's
  section at PR 8's merge base, and PR 8 quotes a green run of D5's drill on its own head.
- **Bootstrap orders that drift apart** (failure class 8, prose asserts facts). Three slices
  held three orders for one rebuild, and R4's own copy applied the root before the
  restores, which would provision empty claims under the backed-up names. Mitigation: D5's
  section is the only procedure. R4 restates no step and writes no fallback, and PR 7b's
  property 1 holds D5's section to its own withheld-root order: the root does not exist
  until the restores have finished, and its hand apply at `prod` is the resume. A failed
  property stops R4 rather than adding a second procedure.
- **A bootstrap check that passes on nothing** (failure class 3). A text check over a
  section reads nothing when the file is missing, the heading is renamed, or a shell
  comment in a code block looks like a heading, and a check that must print nothing then
  passes. Mitigation: PR 7b's `awk` program exits 2 when it reads no section and 3 when
  the section is shorter than 20 lines or never names the DR gate. Its fence toggle
  keeps a fenced `# ` comment from ending the read. A red copy proves each exit.
- **A proof that needs a stage cluster cannot run** (failure class 2). No stage cluster
  exists (Baseline). Mitigation: each such proof in R4 names a substitute that exists: the
  kind drill in CI, or an **operator-only** production drill with its blast radius stated.

- **The renderer drifts from what its templates assume once the oracle is gone.** After
  PR 8 no render compares with Ansible's, and after H7 no parity test does. Mitigation:
  PR 7d's ADR fixes the subset, its spec tests hold the renderer to literals checked once
  against ansible-core, and its census refuses a template outside the subset. PR 7e makes a
  renderer dependency bump declare its render diff.

## Tests and docs that change in the same PR

- PR 1: `docs/homelab-render.md` (the render-diff gate, its pinned commit value and its
  end at PR 11); `docs/landing.md` if it lists CI jobs;
  `gitops_deploy_expected_ruleset_contexts` and any test that pins that list;
  `.github/pull_request_template.md` (the `Render-diff:` section, naming the hazard paths
  and both forms of the line).
- PRs 2 to 5: `docs/homelab-render.md` (where render metadata and task-made facts live);
  `.claude/skills/new-k8s-service/SKILL.md` (no `tasks/main.yml`, from PR 5); root
  `CLAUDE.md`, *Adding a New Service*. `ansible/tests/k8s/test_derived_manifest_files.py`
  and every other test that reads the moved keys from `tasks/` change in the PR that moves
  them.
- PR 6: `docs/k8s-manifest-cycle-record.md`, *The hourly render-record producer*, marked as
  history; `docs/gitops-pipeline.md`, which names `render_targets.py`;
  `docs/claude-tooling.md` if it lists `probe.py releases`;
  `ansible/tests/setup/test_setup_cross_role_files.py`'s table equality, which holds the
  edited `_KUMA_CHECK`; the deleted tests listed under PR 6; the lock rows PR 6 names.
  `ansible/secret_rotation.yml` and `consumers.py` change in the operator's secrets commit
  after PR 6, not in PR 6.
- PR 7a: `docs/volume-snapshot-drills.md`, the revert runbook's command.
- PR 7b changes no test and no doc. D5's bootstrap drill PR and flux PR write
  *Bootstrapping Flux on a rebuilt cluster*, its drill, its renderer test and the text
  test over its order.
- PR 7c: `ansible/tests/_k8s_render.py` and the tests that read the play, retargeted;
  `docs/homelab-render.md`, for the manifests-role defaults that became renderer constants.
- PR 8:
    - `ansible/roles/k8s/manifests/CLAUDE.md` goes with the role, and
      `docs/k8s-manifest-cycle-record.md` moves to `docs/archive/`.
    - Root `CLAUDE.md`, *Where to Look*, loses the manifests-role row and the config-restart
      row. The `deploy` skill loses its k8s modes.
    - `docs/break-glass.md`, the laptop scenario's step 3, names Flux's promotion for the
      cluster. PR 8 edits no other cold-start step: D5's flux PR owns *daniel-box is dead*
      step 4, `docs/longhorn-disaster-recovery.md` step 5 and `docs/k3s-etcd-restore.md`
      *After the restore* step 4, and H1 owns the shed scenario. A line in those runbooks or
      in D5's bootstrap section that names a path PR 8 deletes changes its path only.
    - `ansible/tests/longhorn/` gains the runbook-resolution test.
    - Every k8s role's generated glance block, rewritten by the `regen-role-glance` hook.
    - The tests whose subject PR 8 deletes, and their citations in the docs.
    - Until G2 retires `docs/facts.lock`: `uv run python scripts/dev/fact_status.py forget
      '<doc>#<heading>'` for each row of a deleted doc, and `fact_status.py verify` for each
      edited section, root `CLAUDE.md` and `janitorr/CLAUDE.md` among them. The lock commits
      with the PR.
- PRs 6, 8 and 9: `docs/failure-classes.md` loses any detector citation whose test goes, as
  that page's own meta-test requires.
- PR 7d: the dialect ADR, `docs/adr/index.md` and the `mkdocs.yml` nav; the spec tests and
  the construct census in `scripts/renderer/tests/`; `docs/homelab-render.md`, which links
  the ADR.
- PR 7e: the `render-diff` workflow's hazard list, its census test, and the template's
  `Render-diff:` section, which names the dependency pins.
- PR 9: `docs/homelab-render.md` and any other doc that names `compare` or `stubcheck`.
- PR 10: `docs/homelab-render.md` (no ansible-core import; reimplemented filters).
- PR 11: `docs/homelab-render.md` loses the gate's section, and
  `.github/pull_request_template.md` loses the `Render-diff:` section.

## Complexity delta

- **Removed:** 86 task files less the four on the runbook allowlist (9,635 lines in all); the
  manifests role; the `tasks/` of four utility roles; the k8s play, two task files and two
  post_tasks (about 420 lines); `k8s_retire.py`, the `ansible_owned` filter and the
  retirement arm of `render_diff_gates.py`; the render-record producer, `render_targets.py`
  and one `_KUMA_CHECK` entry; the release-record readers D6 left; `shadow_diff.py compare`
  and `stubcheck` with their tests; the Ansible-mechanics tests, which PR 1 classifies from
  the 102 files in the baseline.
- **Moved:** render metadata to defaults, about 40 `noqa` lines with it plus the rollout
  keys PR 1 counts; task-made facts to `renderer_lib`; `render_targets()` to
  `renderer_lib`; the manifests-role defaults the renderer reads, to `renderer_lib`
  constants.
- **Added:** about 150 lines of reimplemented filters with parity tests; about 80 lines of
  task-free targeting; the floor, allowlist, ansible-free and runbook-resolution tests;
  possibly one runbook playbook; an ADR on the renderer's dialect, its spec tests and its
  construct census (PR 7d); a size bound in exit 6, read once at R4's last PR. R4 adds no bootstrap workflow, fixture bundle or
  hand-apply test: D5 owns them, so the plan carries one bootstrap procedure where it
  carried three.
- **Added, then removed within R4:** the `render-diff` workflow, its required context and
  its template section live from PR 1 to PR 11. The precondition test, with its one named
  `flux` exemption and two fixtures for it, lives from PR 1 to PR 8, and the equality test
  from PR 5 to PR 6. The dependency-pin hazard lives from PR 7e to PR 11, unless Open
  question 12 keeps it. R4's end state carries none of them.

## Open questions

1. **Where does a verify Job live?** C3 defines `<tag>` and `<tag>-deferred` Kustomizations,
   and neither orders a Job after a rollout. *Resolution:* resolved. D5 adopts `<tag>-verify`
   (D5, *`<tag>-verify`*), and C3 lists it: it depends on the tag's last Kustomization and
   sets `wait: true`. Its Job embeds the role's `digest8`. R4's exit 5 counts no Job in the
   role's own Kustomization.
2. **Does the revert runbook have its own entry point?** *Resolution:* resolved in D5.2c.
   The entry is `ansible/volume-revert.yml`. Its snapshots keep the shape
   `autodeploy-<tag>-<digest8>-<claim>-<token>`, which `volume-revert` reads with
   `volume_revert_sha=<digest8>` (D5, *The snapshot name keeps `volume-snapshot`'s shape*).
   PR 1 records the entry, and PR 7a runs only if it is missing. H7 replaces the runbook
   with a task-free tool.
3. **Does the renderer's service list survive H1?** H1 removes `containers_list`, the
   renderer's service index. *Resolution:* H1 states the replacement index, and PR 5's
   predicate reads it. This is listed as a contract gap.
4. **Do D5's host-side and pre-apply rows cover the whole census?** Four gaps for D5:
    - R1's census names nine host-writing roles, and D5's baseline names two.
    - traefik's CRDs come from a URL, which neither a render nor a Job may fetch under C1.
      They need an in-tree copy pinned by `traefik_k8s_crd_version`.
    - traefik's RBAC becomes a pruned render object at its move. A later revert of only that
      move would let Flux prune the ClusterRole. D5 marks the move forward-only, or exempts
      the ClusterRole from prune before the move.
    - pi-peer-backup's two writes on daniel-pi have no owner in H3 or H5. Until H5's package
      lists them, a setup role applied to the Pi owns them.

   M6 batch B6 rewrites crons that six of these roles install: crowdsec's allowlists and
   AppSec check, registry-gc, the artifacts sync, configarr and janitorr health, and
   observability's telemetry health check. *Resolution:* D5 resolves each gap at that role's
   move and agrees with M6 which slice moves each cron. R4's precondition test finds whatever
   is left.
5. **When is render_records disarmed?** The producer fails from D5's last flip, when the list
   it renders goes empty, not at D6. *Resolution:* resolved in D5. Its render-records disarm
   PR sets `render_records_state: absent` in the role's defaults before the flux PR, while
   `render_targets.py` still lists `flux`, and leaves `group_vars/all.yml` alone. Its
   rollback reverts the same defaults path, which the tick applies through the setup arm.
   D5's review log corrects D5-R2-13, which had flipped `render_records_enabled` in
   `group_vars/all.yml`, a path that routes to `broad_deploy` and never reaches the role. M6
   batch B4 then drops render_records rather than converting a producer R4 deletes. If the
   units are still present at PR 1, PR 1 disarms the producer first.
6. **Does D6.2 break the setup plane?** `role_dirs` imports `is_leftover_dir`'s rule from
   `k8s_autodeploy.py`, which D6.2 deletes, and `setup_routing.py` calls `role_dirs`.
   *Resolution:* D6.2 moves the rule into `scripts/lib/k8s_roles.py` first. If it has not,
   R4's PR 10 does, and PR 1 reports the breakage as a D6 finding.
7. **Which route does the deployer give a deploy-plane path after D6?** PR 8 edits
   `ansible/deploy.yml`, `ansible/tasks/`, `ansible/post_tasks/`, `ansible/filter_plugins/`
   and `group_vars/all.yml`, all `broad_deploy` at the baseline. *Resolution:* resolved in
   D6, *The route of a deploy-plane path after D6*: from D6.1 on, the tick applies nothing
   for such a path, fast-forwards past it as it passes a docs-only one, and writes no ledger
   line. PR 1 re-measures the route to confirm it, and PR 8 quotes it.
8. **Do ADR-0011, ADR-0017 and ADR-0012 need amendments?**
   [ADR-0011](../../adr/0011-one-lock-serialises-every-deploy-path.md) and
   [ADR-0017](../../adr/0017-the-tree-lock-guards-the-tree-not-the-cluster.md) lose
   `deploy.sh`'s k8s path as a lock holder once R4 deletes the play.
   [ADR-0012](../../adr/0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md)'s rollout
   gate is the k8s play's post_task, which PR 8 deletes. *Resolution:* the operator decides
   whether each needs an amendment, in PR 8's review (**operator-only**). design.md, *ADR
   changes this migration needs*, assigns these to D6 and R4.
9. **Does stub mode outlive the shadow comparison?** C1 says stub mode "exists only for R1's
   shadow comparison", which PR 9 deletes. R2's `roundtrip` renders in stub mode, and R1's
   determinism module writes the stub file. R4 keeps stub mode, `stubs`, `digest` and
   `roundtrip` (PR 9), which contradicts C1 as written. *Resolution:* a requested
   amendment. C1 names `roundtrip` and R1's determinism module as stub mode's readers after
   R4. The gate is "before R4 PR 9 merges", and contracts.md lists the request under
   *Requested amendments not yet applied* until it lands.
10. **What is the renderer's commit input called?** D5 says "The renderer takes the commit
    SHA as an input" for the soak Job (D5, *The soak as a Job*), and C1 says only that two
    renders of one commit are byte-identical. No contract names the input, its default, or
    which commit the publish job passes. The render-diff job must pass one value to both
    renders. *Resolution:* a requested amendment to C1, or a statement in D5, that names the
    input. R4 proposes `homelab-render cluster --commit SHA`, where the publish job passes
    the commit it publishes, R, and the render-diff job passes the PR head's SHA to both
    renders. The gate is "before R4 PR 1 merges". H7's base-and-head proof uses the same
    input.
11. **May a deletion PR exceed the 100–200 line guide?** PRs 6 and 8 are sized by subject.
    The slice document format asks for about 100–200 lines per PR, and the exemption for a
    pure deletion is listed under *Requested amendments not yet applied* (D6 Open question
    6). *Resolution:* R4 relies on that amendment for PRs 6 and 8. If it is refused, PR 8
    splits by subject in this order, each part with its own tests, docs, lock rows and
    `Render-diff: none`:
    1. the `tasks/` directories of the roles under `ansible/roles/k8s/`, in groups, which
       removes every `include_role` of the manifests role. The precondition test goes with
       the last group, and the allowlist equality test replaces it;
    1. the play, the batch and retire task files, the two post_tasks, `k8s_retire.py`, the
       `ansible_owned` filter, the play's tests, and the retirement arm of
       `render_diff_gates.py`;
    1. the manifests role and the `tasks/` of the utility roles, now that nothing includes
       them;
    1. the inventory keys only the deleted code read, with the runbook-resolution test.

    The groups go first because an `include_role` of a role that no longer exists fails the
    syntax check. PR 6 cannot split below the role: the role, its `initial_setup.yml` entry
    and its `_KUMA_CHECK` entry must leave in one commit (*render_records*). Its readers
    that D6 left can go in a PR of their own before it.
12. **Does the renderer-dependency hazard outlive R4?** PR 11 removes the `render-diff`
    gate and its hazard list. After R4, nothing then stops a renderer dependency bump that
    changes output from automerging. Two options (**operator-only**):
    - keep the `render-diff` job and its required context after PR 11, with the hazard
      list narrowed to the renderer's dependency pins;
    - remove the required context as planned, keep the job as an advisory one that prints
      the diff, and add a Renovate rule that disables automerge for the renderer's
      dependency closure.

    Recommendation: the second, because it keeps no required context, and a human reads
    the printed diff before merging. Resolution: the operator rules in PR 11's review, and
    PR 11 implements the choice.

## Review log

### Round 1 (per-slice adversarial review)

Raised: 2 critical, 9 high, 8 medium. All were fixed. None was refuted, because each claim
checked out against the tree at `70591c307`.

- **R4-C1 (critical)** The renderer reads task files through `k8s_roles.py`, which falls back
  silently. *Fixed:* the reader census now lists `_named_files`, `resolved_manifest_files`,
  `role_callers` and `task_inputs`. The move runs in three stages, with hard errors and red
  halves in PR 5. The render-diff gate runs on every R4 PR.
- **R4-C2 (critical)** The rollback for PRs 3 to 5 assumed a reverted task runs again.
  *Fixed:* R4 no longer moves any writer, so no R4 rollback changes a live object's owner.
  The traefik RBAC hazard goes to D5 (Open question 4).
- **R4-H1 (high)** PR 7 deleted the content-tag expression and its oracle before anything
  moved them. *Fixed:* PR 4 moves every task-made fact into `renderer_lib` and retargets the
  oracle, before the deletion PR.
- **R4-H2 (high)** R4 claimed conversions that D5 owns. *Fixed:* D5 owns every conversion,
  and R4 checks the result with a precondition test. The census gaps, the Traefik CRD source
  and M6 B6's overlap are in Open question 4.
- **R4-H3 (high)** A verify Job in the role's own Kustomization races the rollout. *Fixed:*
  exit 5 counts no such Job. Open question 1 proposes `<tag>-verify` with a digest-named Job.
- **R4-H4 (high)** The plan assumed nothing applies a setup role after D6. *Fixed:* the
  baseline quotes the measured route of every R4 path. The disarm uses the setup route, PR 6
  handles its park, and PR 8 waits on D6's route (Open question 7). Class B is in Risks.
- **R4-H5 (high)** The hand `initial_setup.yml` apply needed the age key and sudo. *Fixed:*
  the hand apply is gone, because the tick applies the disarm. Every root or decrypting step
  is marked operator-only.
- **R4-H6 (high)** The producer turns red at D5's last flip. *Fixed:* Open question 5 dates
  it to D5, asks D5 to carry the disarm, and names M6 B4.
- **R4-H7 (high)** Exit 1 forbade every k8s task file, including D5's revert runbook.
  *Fixed:* exit 1 is a named allowlist, and PR 7 gives the runbook a standalone entry if
  needed.
- **R4-H8 (high)** Exit check 3 used `shadow_diff.py compare` on two C1 trees. *Fixed:* a
  `diff -r` of complete trees, a tag-set comparison, and a floor of three named members and
  a minimum `flux/` count. The `render-diff` workflow runs it on every R4 PR, re-runs on a
  body edit, and requires the `Render-diff:` line on the hazard paths. `shadow_diff.py`
  retires in PR 9, with R1's CI call to it.
- **R4-H9 (high)** The grep missed a third ansible-core route. *Fixed:* the baseline names
  `is_leftover_dir`, and a runtime test with `ansible` unimportable replaces the grep.
- **R4-M1 (medium)** Exit 5 relied on M6's health reader. *Fixed:* rule successors carry
  `severity="critical"` and are read by D5.3's soak Job.
- **R4-M2 (medium)** The service-set assertion was hard-coded to 56. *Fixed:* the test
  computes both predicates over one tree, with a member floor.
- **R4-M3 (medium)** The DR runbooks still said `deploy.sh`, and no Flux bootstrap procedure
  existed. *Fixed:* PR 8 edits the three runbooks and adds the operator-only bootstrap,
  exercised on stage. The `facts.lock` step is listed.
- **R4-M4 (medium)** PR 2 was oversized and left two sources. *Fixed:* split into PR 2, four
  PR 3 groups and PR 5. No key has two sources at any commit. The glance-marker change is in
  PR 8, and the `noqa` lines are counted.
- **R4-M5 (medium)** Copying ansible-core filters would import GPL code. *Fixed:* the filters
  are reimplemented, and ansible-core is a test-only oracle.
- **R4-M6 (medium)** pi-peer-backup's Pi writes had no owner in H5. *Fixed:* it is a contract
  gap in Open question 4, with a Pi setup role as the interim owner.
- **R4-M7 (medium)** R4 omitted items D6 assigns to it. *Fixed:* PR 8 deletes `ansible_owned`,
  `k8s_retire.py`, `k8s_retired_services` and the k8s-play-only keys. The post-R4 behaviour
  of `deploy.sh --tags <k8s tag>` is stated, and PR 9 reconciles the test count.
- **R4-M8 (medium)** The ADR questions were missing. *Fixed:* Open question 8, operator-only.

### Round 2 (integration review)

Raised against R4: three high and two medium. The highs were X-05, X-07 and X-08, and the
mediums X-15 and X-17. All five were fixed for R4's share, and none was refuted. Contract
amendments made in the same round settled two of R4's Open questions: C3's `<tag>-verify`,
C7's *Verify successors* and C8's *No stage cluster*.

- **X-05 (high)** D5 disarmed render_records through `group_vars/all.yml`, which routes to
  the deploy plane. *Fixed for R4's share:* R4's lever was already the role-defaults line. I
  measured both routes at `70591c307` with `services_from_changed_paths`.
  `['ansible/inventory/group_vars/all.yml']` gives `{'broad': True, 'broad_deploy': True}`.
  `['ansible/roles/setup/render_records/defaults/main.yml']` gives `{'broad': True,
  'broad_setup': True, 'setup_roles': {'render_records'}}`. *render_records* now cites why
  the deploy plane never reaches the role, and PR 1 reads the units rather than the key. D5
  corrected its own lever in the same round (D5, *The render-records disarm PR*), so Open
  question 5 is marked resolved.
- **X-07 (high)** Exit 5 rejected D5's `warning` plus `verify="true"` rules, and accepted a
  `probe.py health` successor that D5 forbids for automerged roles. *Fixed:* the first shape
  accepts `severity="critical"` or `verify="true"`. The second applies only to
  `k8s_autodeploy: false` roles, a key that outlives D6 (D6, *The `k8s_autodeploy` key keeps
  its Renovate readers*). The third depends on the tag's last Kustomization. Exit 5 cites
  C7's *Verify successors*, which the contracts gained in this round. The halves table and
  the class-2 risk say the same.
- **X-08 (high)** PR 7's drill and PR 8's bootstrap proof needed the retired stage cluster.
  *Fixed:* the Baseline cites `docs/archive/staging-cluster.md` and
  `scripts/lib/kubectl.py:RETIRED_CLUSTER_NODES`. PR 7a uses D5.2c's speedtest drill on
  production, with its blast radius in C8's three parts. D5.2c's `ansible/volume-revert.yml`
  normally makes PR 7a unnecessary. A new PR 7b proves the bootstrap procedure on a kind
  cluster in CI, with a throwaway key and a fixture bundle, and says why kind suffices. An
  operator-only `age-keygen -y` check covers the real key. Open question 1's stage red-proof
  is gone, because D5 and C3 settled `<tag>-verify`. The R4-M3 entry above is history and
  stays as written.
- **X-15 (medium)** R4 PR 8's runbook created `homelab-cluster-vars` by hand. *Fixed:* the
  procedure creates only `sops-age`, and the `cluster-vars` Kustomization produces
  `homelab-cluster-vars` through its secretGenerator (D3). The keypair rotation is D3's
  share, and D3 fixed it in the same round.
- **X-17 (medium)** PR 9 deleted `shadow_diff.py` and dropped R2's `roundtrip`. *Fixed:* PR 9
  deletes only `compare`. `stubs`, `digest` and `roundtrip` keep their names, so R2's CI step
  stays valid and no workflow changes. The finding's request to edit R2's CI step is moot for
  that reason. R4's old claim that "R1's CI job" calls `shadow_diff.py stubs` was wrong. R1
  says "R1 adds no workflow step, so no file under `.github/workflows/` calls
  `shadow_diff.py` after R1" (R1, *Design*). Open question 9 asks C1 to name stub mode's
  remaining readers.

Found while fixing these, and fixed:

- **R4-I1 (high)** The render-diff gate was a workflow outside the ruleset, so a red run
  would not block a merge. The agent also cannot push a workflow file. *Fixed:* the operator
  pushes the workflow and adds its context to the live ruleset (**operator-only**). PR 1 adds
  the context to `gitops_deploy_expected_ruleset_contexts`. The workflow has no `paths:`
  filter. After PR 1 merges, a throwaway PR shows a red run blocking the merge, and PR 2's
  description quotes it.
- **R4-I2 (medium)** PR 8's sweep of k8s-play-only keys in `group_vars/all.yml` could take
  `k8s_namespace`, `k8s_dry_run` and `k8s_no_mutate`, which the revert runbook reads.
  *Fixed:* PR 8 names them as kept, and a cluster-free test proves that every variable the
  allowlisted task files read still resolves.
- **R4-I3 (medium)** The bootstrap procedure omitted the `homelab-critical` PriorityClass.
  D1 sets it on the Flux controllers, and the k3s setup role creates it. A blank cluster
  refuses those pods. Once H1 moves the class into the Flux-applied `cluster-base`, only
  Flux would apply it. *Fixed:* the procedure's first step applies the classes from the
  `cluster-base` render when H1 has moved them, and the drill applies them from the same
  source. A cross-slice note tells H1.
- **R4-I5 (medium)** A dispatch-only drill cannot run before its file reaches the default
  branch, so PR 7b could not quote a run. *Fixed:* the drill also triggers on a
  path-filtered `pull_request`, and one run carries the green and the red job.
- **R4-I4 (low)** *The render-diff gate* said PR 7 deletes the Ansible render. *Fixed:* PR 8.

### Round 10 (per-slice adversarial review)

Raised: four high and nine medium. All four highs are fixed. Seven of the mediums are
fixed in full. M2 and M7 are fixed in part, and the part not taken is refuted with the
evidence below. Each claim was re-measured at `70591c307` before the fix. The PR numbers
stay as they were, because D5, G5, H7, R1 and R2 cite R4's PRs 1, 2, 7, 8, 9 and 10 by
number. The new steps are PR 7c and PR 11.

- **R4-R10-H1 (high) `Render-diff: none` could never pass, because the soak Job embeds the
  commit SHA.** *Fixed.* Confirmed in D5, *The soak as a Job*: the Job's "spec embeds R's
  SHA". A grep of `docs/target-architecture/` finds no named commit input in any slice or
  contract. Both renders now take the PR head's SHA through the renderer's commit input
  (*The render-diff gate*). Two red-proofs show the pin works: PR 1's own head passes under
  `Render-diff: none`, and a branch commit that moves the soak's wait W fails and names
  `cluster/soak/`. Exit 3 and *Verification* pass the same value. Open question 10 asks C1
  or D5 to name the input before PR 1 merges, with cross-slice notes to contracts, D5 and H7.
- **R4-R10-H2 (high) Exit 3 compared R4's base with its last commit, a span holding every
  other session's merges.** *Fixed.* Exit 3 is now the conjunction of the per-PR runs from
  PR 1 to PR 10, each from its own merge base. PR 11's description lists them. The
  base-versus-last `diff -r` and the span-wide tag-set equality are gone. *Risks* gains the
  class-4 entry.
- **R4-R10-H3 (high) PR 8 deleted the play before PR 9 retargeted the tests that read it.**
  *Fixed.* Confirmed: `ansible/tests/_k8s_render.py:deploy_play` raises once the play is
  gone, `test_k8s_manifests.py` and `test_namespace_resource_defaults.py` import it, and the
  finding's grep prints 86. That selector misses tests that read a role's `tasks/` or the
  manifests role's `defaults/` and `files/`, such as `test_shared_manifest_defaults.py`. A
  wider selector lists 270 files, and PR 1 sorts that set. PR 7c now moves every kept reader
  off the play and the manifests role before PR 8.
  PR 6 and PR 8 delete the tests whose subject they delete, PR 5's equality test in PR 6
  with `render_targets.py`. PR 9 keeps only the `shadow_diff.py` removals and the count.
  *Rollback* says a PR 8 revert restores its tests with their subject, and that PR 7c
  reverts only after PR 8.
- **R4-R10-H4 (high) PR 6 omitted `_KUMA_CHECK`, and "No playbook is owed" was false.**
  *Fixed.* `deploy_cross_role.py:_KUMA_CHECK` names `render_records`, and
  `test_setup_cross_role_files.py:test_the_deployers_table_equals_the_trees_cross_role_files`
  holds it equal to the tree. The measured route of PR 6's full path set is in the Baseline,
  and it includes `broad_manual` and `setup_roles={'gitops_deploy', 'render_records'}`. The
  park cannot be avoided by a split. A playbook that names a missing role fails when it
  loads, and the table test binds the table to the tree, so the role, its playbook entry and
  its table entry share one commit. PR 6 therefore names the owed apply,
  `initial_setup.yml --tags gitops_deploy` after the hand fast-forward (**operator-only**).
  The token leaves in the operator's secrets commit after PR 6, together with its
  `consumers.py` entry. That set routes to `{'secrets': True}`, so the consumers census
  never sees a registry token without a consumer.
- **R4-R10-M1 (medium) "Delete the utility roles whose last caller is gone" would delete
  renderer inputs.** *Fixed.* PR 8 deletes only their `tasks/`, unless a quoted grep shows a
  role holds no renderer input. The same rule now covers the manifests role, whose defaults
  hold `manifests_prune: true` and `manifests_shared_defaults`. PR 1 measures which of them
  the renderer reads, and PR 7c moves those into `renderer_lib`.
- **R4-R10-M2 (medium) PR 5 deleted `render_targets.py` while a facts-locked section cited
  it.** *Fixed in part.* Confirmed: `render_records/CLAUDE.md#How one run works` cites
  `scripts/deploy_tools/render_targets.py`. That file now stays until PR 6. PR 6 forgets the
  three render_records rows and verifies `manifests/CLAUDE.md#Release records`, which cites
  the role's directory. PR 8 forgets the four manifests rows and handles the two crowdsec
  rows. Every deletion PR names its lock rows. *Refuted in part:* PR 8 does not break the
  `deploy.yml` atoms. `docs/facts.lock` cites `ansible/deploy.yml` as a path atom in root
  `CLAUDE.md#Adding a New Service` and `janitorr/CLAUDE.md#Notable`. A path atom hashes
  existence, and `deploy.yml` keeps the Pi's play. Their prose still describes the k8s
  play's toposort, so PR 8 edits and verifies both sections anyway.
- **R4-R10-M3 (medium) R4 said D6 had not stated the post-D6 route.** *Fixed.* Open
  question 7 is resolved, citing D6's *The route of a deploy-plane path after D6*. The
  Baseline, the class-B risk and PR 8 say the paths fast-forward with no apply, and PR 1
  re-measures that route.
- **R4-R10-M4 (medium) Open question 9 claimed C1 already names stub mode's readers.**
  *Fixed.* C1 still reads "exists only for R1's shadow comparison". Open question 9 is now
  a request with the gate "before R4 PR 9 merges", and a cross-slice note asks contracts.md
  to list it.
- **R4-R10-M5 (medium) Deletion PRs relied on an amendment that has not landed.** *Fixed.*
  Open question 11 cites the pending amendment and names PRs 6 and 8. It gives PR 8's
  fallback split by subject, ordered so that no `include_role` names a deleted role, and
  says why PR 6 cannot split below the role.
- **R4-R10-M6 (medium) The precondition grammar rejected game-stats' `import_tasks`.**
  *Fixed.* Confirmed: `game-stats/tasks/main.yml` imports `terraria.yml` and `valheim.yml`,
  which set the checksum facts. The grammar now allows an import of a literal file under the
  same role's `tasks/` and applies itself to that file. A templated import that leaves the
  role, as janitorr's and configarr's `install_host_lib.yml` imports do, stays red. The
  fixtures are one green and two new red.
- **R4-R10-M7 (medium) The gate became a permanent required check with no author guidance
  and no way out.** *Fixed in part.* PR 1 adds the `Render-diff:` section to
  `.github/pull_request_template.md`, and a run that fails for a missing line prints the
  hazard path and both forms. The gate ends at PR 11, in an operator-first order that keeps
  `github-ruleset-drift.sh` quiet. Cross-slice notes go to H1, H2 and H3. *Refuted in
  part:* the `pr-authoring` skill edit. That skill is not in this repo, because
  `.claude/skills/` holds no `pr-authoring` directory. It is user-level, and its first rule
  reads "A repo's own `.github/pull_request_template.md` wins", so the template edit
  reaches every author who uses it. Root `CLAUDE.md` gains nothing, because the template
  and the failure message carry the rule and G5 is shrinking that file.
- **R4-R10-M8 (medium) The bootstrap drill swapped more of the OCIRepository than it
  admitted, and the procedure applied the sync objects twice.** *Fixed.* The drill lists a
  fourth substitution: the OCIRepository's URL, its `verify` block and registry trust.
  Keyless verification is proven by production's pull on every promotion and by D5's
  pinning test. Step 2 applies `cluster/flux/`. Step 4 applies only the OCIRepository and
  the root from `cluster/flux-deferred/` and leaves any placeholder Secret to Flux. The
  procedure names the render mode, and PR 7b adds a renderer test that the hand-applied
  documents hold no `${` and no `$$`.
- **R4-R10-M9 (medium) The Baseline's list of renderer inputs predated D3 and D5, and
  `k8s_autodeploy_snapshot_pvcs` was unaddressed.** *Fixed.* The Baseline names the
  rollout keys D5.1e stamps from, and PR 1 re-measures the set. PR 3 moves every key of
  that set. As written, the required fix left PR 5's error firing on the dead keys that
  PR 8 deletes later. So PR 5's error covers only the moved set, and a green fixture leaves
  an Ansible-only key in `tasks/`. `k8s_autodeploy_snapshot_pvcs` stays in role defaults
  as a renderer input, and PR 8's key sweep leaves it.

Found while fixing these, and fixed:

- **R4-R10-I1 (medium) `stubcheck` outlived its subject.** R1, *Design*, says "Only
  `compare` and `stubcheck` read an Ansible tree". R4 kept `stubcheck` without a word.
  PR 9 now deletes both, and `compare` already runs `stubcheck`'s rule first.
- **R4-R10-I2 (medium) D6 leaves the retirement arm of `render_diff_gates.py` to R4.** D6,
  *The render-diff gates after D6*, says "Its retirement arm stays until R4 deletes
  `k8s_retired_services` with the play". R4 never named it. PR 8 now deletes the arm with
  the key. *The render-diff gate* says how R4's gate differs from that module.
- **R4-R10-I3 (low) `compare` imports `render_targets`.** R1's `compare` and determinism
  modules read `render_targets`, so PR 5 repoints them at `renderer_lib.targets` before
  PR 6 deletes the old file.
- **R4-R10-I4 (low) Two slices cite R4 PRs that do not match.** H7 says R4 PR 8 writes the
  bootstrap section, but PR 7b does. G5 says R4 PR 2 edits *Adding a New Service* and
  PR 7 removes the manifests-role row, but PRs 2 to 5 do the first and PR 8 the second.
  Cross-slice notes go to H7 and G5.

### Round integration-2 (integration review)

Raised against R4: one high, X2-07. It is fixed for R4's share, and nothing was refuted.
The finding also targets D1, D5 and H1, and their shares go to them as cross-slice notes.

- **X2-07 (high) Three incompatible post-H1 bring-up and DR procedures, and D5's bootstrap
  had no way to install Longhorn before the restores.** *Fixed for R4's share.* I checked
  the claim against the plans and the tree at `70591c307` before the fix:
    - R4's own procedure had the defect, and D5 copied it. Its step 4 applied the root
      `homelab` Kustomization from `cluster/flux-deferred/` unsuspended, and nothing
      hand-applied Longhorn first. D5's bootstrap drill PR says it "carries what R4's PR 7b
      describes". H1's *Bring-up and DR order after H1* holds the root suspended through
      step 10 and resumes it at step 11 by re-running D1's `flux` role. D5's flux PR makes
      that role flux-owned, and its renderer refuses `flux_sync_suspend: true`.
    - The commits disagreed as the finding says. R4 and H1 render at `prod`, and D5 at
      `last-good`. `contracts.md` C2 defines `prod` as "the revision Flux follows", so R4
      settles on `prod`.
    - `grep -n "deploy.sh"` over the three runbooks finds five cluster steps:
      `docs/break-glass.md` *daniel-box is dead* step 4, the shed scenario's step 3 and the
      laptop scenario's step 3; `docs/longhorn-disaster-recovery.md` *Procedure (fresh
      host, total loss)* step 5; and `docs/k3s-etcd-restore.md` *After the restore* step 4.
      R4's PR 8 claimed all five, while D5's flux PR claims three and H1 the shed scenario.
    - `docs/flux-deploy-plane.md` does not exist at `70591c307`, so the section is future
      text in every slice.

  The fix makes D5's *Bootstrapping Flux on a rebuilt cluster* the single owner, and R4
  keeps no copy. *Bootstrapping Flux without Ansible* now states only what R4 relies on:
  four properties of D5's section, among them its withheld-root order and the render at
  `prod`. D5's section applies only the OCIRepository from `cluster/flux-deferred/` and
  withholds the root until the restores have finished. Its hand apply of the root is the
  resume, and no step patches `suspend`. Round i1 corrected this entry, which first said
  the root was held suspended with the `homelab-operator` hand patch. PR 7b is now a check
  with no file changes. It keeps its number because D5 and H1 cite it, and its mechanical
  half is a check with a red half at D5's flux PR. PR 8 cites the section, quotes a green
  run of D5's drill on its own head, and rewrites only the laptop scenario's step 3.
  *Bootstrapping Flux without Ansible* names the owner of each of the five runbook steps.
  R4 also corrected its own claim that PR 8 deletes the last install path. D5's flux PR
  removes the last live path, and PR 8 deletes dead code. *Verification*, *Rollback*,
  *Risks*, *Tests and docs*, the file tree and *Complexity delta* changed with it. R4 no
  longer adds the drill workflow, its fixture bundle or the hand-apply renderer test.

### Round i1 (per-slice adversarial review)

Raised against R4: one high and one medium. Both are fixed, and nothing was refuted.

- **R4-i1-H1 (high) PR 7b held D5's bootstrap section to the suspended-root order that D5
  rejected, so PR 8 could never merge.** *Fixed.* I checked the claim against D5's text
  before the fix. D5's bootstrap drill PR, step 5, applies "the OCIRepository `homelab` and
  nothing else", and "The root Kustomization is withheld until step 9". D5's *Why the root
  is withheld, not suspended* says a root applied and then suspended "could reconcile
  between the two commands and provision empty PVCs under the backed-up names", and that
  "Step 9's apply is the resume". D5's drill has three jobs, and none holds a suspended
  root. R4's property 1 required the suspend patch and its resume, so it failed against
  D5 as written. Property 1 also contradicted properties 2 and 3: a root rendered at
  `prod` carries no `spec.suspend`, so a patch could suspend it only after it existed.
    - Property 1 now states D5's withheld-root order. Only the OCIRepository comes from
      `cluster/flux-deferred/`, the root does not exist until the backupstore sync, the DR
      gate and the restores have finished, and its hand apply at `prod` is the resume. No
      step patches the root's `spec.suspend`.
    - Property 3 no longer speaks of the root resuming. It says the root applies `prod`'s
      render once the hand apply creates it.
    - Property 4 lists D5's three jobs and what each asserts.
    - PR 8 and *Verification* quote all three jobs, and *Verification* no longer describes
      a suspended root.
    - *Risks*, *Bootstrap orders that drift apart*, says property 1 holds D5's section to
      its own withheld-root order, not to H1's.
    - The integration-2 entry above now describes D5's section correctly, and says so.
    - The history paragraph in *Bootstrapping Flux without Ansible* no longer implies
      that suspending the root was the cure. It names D5's reason for withholding it.
- **R4-i1-M1 (medium) PR 7b's mechanical pipeline passed when it read nothing** (failure
  class 3). *Fixed.* I reproduced each case on throwaway copies outside the tree before
  the fix. `ls docs/flux-deploy-plane.md` fails at `70591c307`, and the old pipeline's
  statuses there were `2 1`: awk's error went to stderr and grep printed nothing. On a copy
  with a fenced `# ` comment before a `./scripts/deploy.sh` line, the statuses were `0 1`
  and nothing printed, because `index($0, " ")` returned 2 and ended the read.
  `grep -n '^# ' docs/longhorn-disaster-recovery.md` prints two such comment lines inside
  a code block, so the DR runbooks use this style.
    - PR 7b's check is now one `awk` program with no `grep` stage. Its fence toggle keeps
      a fenced line from matching a heading, and it still scans fenced lines for the
      forbidden forms.
    - It exits 2 when it reads no section, and awk exits 2 itself on a missing file. It
      exits 3 when the section is shorter than 20 lines or never names
      `runbook_gates.py longhorn-dr`, and 1 on a hit.
    - Red copies prove a renamed heading (exit 2), a fenced comment before the
      `./scripts/deploy.sh` step (prints the step, exit 1), a missing DR gate line (exit
      3) and a root suspend patch (exit 1). I ran the program on each copy and on the
      missing file before writing it in.
    - Property 1's mechanical half is D5's flux-PR text test over the bootstrap section,
      in `ansible/tests/repo/`, which fails when the root's apply comes before the restore
      step. It replaces the hand quote of the section's steps. PR 8's `pytest` job runs
      it.
    - The program gained one pattern beyond the finding's fix: a hand patch or a `flux
      suspend` of the root Kustomization, which property 1 now forbids. *Risks* gained
      *A bootstrap check that passes on nothing*.
    - The `flux_sync_suspend: true` pattern matched D5's own prose, which says the
      renderer refuses that value, so the green half would have exited 1 on a correct
      section. It now matches only a YAML key at the start of a line and an `-e`
      assignment. The program exits 0 on a copy that holds D5's sentence. A sentence that
      names a form only to rule it out still exits 1, and PR 7b's text says how the
      description clears it.

### Round f0 (per-slice adversarial review)

Raised against R4: one high. It is fixed, and nothing was refuted.

- **R4-i2-H1 (high) PR 6's hand fast-forward to `origin/master` skipped every other change
  in the parked range, and nothing recorded that.** *Fixed.* I checked the claim at
  `70591c307` before the fix, and it holds.
    - `docs/gitops-pipeline.md` *When a tick parks* prescribes `git merge --ff-only
      origin/master`. `deploy_remediation.py:broad_remediation` builds the park alert's
      text, and it returns "`git merge --ff-only origin/{branch}` FIRST, then" the
      playbooks.
    - `deploy_defer.py:parks_the_tick` returns true on `cs.broad_manual`, and `park` merges
      nothing, so every commit after PR 6 waits in the same `local..origin` range.
    - The docstring of `land_tags.py:self_applied_command` says that a hand `git merge
      --ff-only` makes local equal origin, after which "`next_action()` returns `noop` for
      every later tick — so the plane is stranded permanently". A setup-plane commit
      merged after PR 6 and before the operator's step would therefore never be applied,
      and no ledger line would name it.
    - "Land this PR alone" named no mechanism, and the operator step can come hours after
      the merge.

  The fix:
    - PR 6's step now holds a five-step operator procedure: read the squash SHA, check
      that `git rev-list --oneline HEAD..<PR 6 sha>` prints one line, quote the route of
      any extra commit and name every apply it owes, fast-forward to PR 6's commit rather
      than to `origin/master`, then run `initial_setup.yml --tags gitops_deploy` and the
      named applies. Before the merge, the primary checkout's `HEAD` equals
      `origin/master`.
    - The step says that it departs from the park alert and from *When a tick parks*. I
      reproduced the alert for PR 6's path set by calling `broad_remediation` as `park`
      does. Besides the `origin/master` target, it names a full `deploy.yml`, the whole
      `initial_setup.yml` and the deleted `render_records` role. The step says that its
      step 5 is the whole owed set and that none of the alert's commands is run. The
      Baseline quotes the alert, and its commands block reproduces it.
    - A tick that fires between the fast-forward and the apply routes the later range with
      the old `/opt/gitops-deploy`. The step names that window, so the apply follows at once.
    - The Baseline quotes `broad_remediation`, `self_applied_command` and the squash merge
      in `merge.py:_merge_past_review`, which makes PR 6 one commit.
      *render_records* states why the fast-forward stops at PR 6's commit.
    - *Rollback* applies the same procedure to the PR 6 revert, with the revert's SHA, and
      *A partial failure* names the skipped-route hazard.
    - *Risks* (failure class 5 for PR 6's park, and design.md's class B) state the bounded
      fast-forward as the mitigation.
    - *Verification* gained *PR 6's landing*: the `rev-list` output, `HEAD` after the
      fast-forward, and a `cmp` of the shipped `deploy_cross_role.py`.
    - *Complexity delta* is unchanged. The fix adds operator steps and no code.

### Round f1 (per-slice adversarial review)

Raised against R4: one high and three mediums. All four are fixed, and nothing was refuted.

- **R4-f1-H1 (high) PR 1's precondition was red by construction on `flux`, a member its
  floor requires.** *Fixed*, with the finding's option (a). I checked the claim before the
  fix. `git ls-tree` at `70591c307` lists no `ansible/roles/k8s/flux/`, so D1 adds the
  file. D1's *What D1 adds* describes its `tasks/main.yml` as an include plus "own rollout
  wait; CRD wait; deferred sync apply; delete ocirepository/verify-drill while the lever is
  off". D5's flux PR, step 29, flips the role and has no step that strips those tasks.
  R4's PR 8 deletes the file. So from PR 1 to PR 7 the grammar fails on `flux`.
    - *R4 deletes, D5 converts* gains a named exemption for `flux`. Its grounds are C4,
      under which no path runs a flux-owned role's tasks, and PR 1's ownership test. The
      exemption holds only while `flux` carries the ownership flag, so a revert of D5's flux
      PR turns the precondition red on `flux` again. `flux` stays in the floor.
    - Two red fixtures join the four. One holds a flux-owned `flux` with D1's tasks and
      another role with a `kubectl` task, and the test names the other role alone. The other
      holds a `flux` entry without the ownership flag, and the test fails on `flux`.
    - PR 1's description maps flux's exempted rows to successors: the `flux`
      Kustomization's health wait and the soak rule, `flux-deferred` and D5's bootstrap
      section, and the prune of `flux-deferred` for `verify-drill`. A rendered `prune:
      false` there sends the row back to D5.
    - PR 8 says the exemption goes with the test. *Rollback*, PR 8, and *Bootstrapping
      Flux without Ansible* were already right under option (a) and did not change.
      *Verification*, *Risks* and *Complexity delta* name the exemption.
- **R4-f1-M1 (medium) PR 6's pre-merge condition had no owner, and step 3 named no
  counterpart for a commit with no session.** *Fixed*, by dropping the condition. The
  comment above the CI walk depth in `ansible/roles/setup/gitops_deploy/defaults/main.yml`
  reads "master takes about 124 merges a day (median gap 315s)". `deploy_defer.py:park`
  logs, alerts once and merges nothing, and `parks_the_tick` re-parks the whole range on
  each tick. So a condition that the range starts at PR 6's parent would usually fail, and
  no actor could hold it.
    - PR 6's step treats extra commits as the expected case and cites that measurement.
      Its step 2 no longer says the `rev-list` prints one line.
    - Step 3 assigns each extra commit an owner. A commit with no live session, such as a
      Renovate automerge or one from a finished session, is the operator's, who runs its
      applies in step 5 without waiting. A live session's commit is agreed with that session,
      and a session that does not answer at once hands it to the operator.
    - *render_records*, *Verification* (*PR 6's landing*) and *Risks* (design.md's
      class B) now say the same. The Risks entry no longer claims a converged checkout.
- **R4-f1-M2 (medium) A revert of PR 6 after PR 8 restored an equality test that cannot
  pass.** *Fixed.* `render_targets.py:includes_manifests` greps a role's task files with
  `_INCLUDES_MANIFESTS`, and PR 8 deletes every `tasks/` directory outside the runbook
  allowlist. *Rollback*, PR 6, now says to revert PR 8 first or to drop PR 5's equality test
  from the revert. It also says the restored `render_targets.py` lists nothing then, which
  is harmless only while the restored role stays pinned by `render_records_state: absent`.
- **R4-f1-M3 (medium) PR 8's runbook-resolution test had no source for the magic
  variables and facts the runbook reads.** *Fixed.* `git grep` at `70591c307` finds
  `ansible_hostname` four times in `ansible/roles/k8s/longhorn-api/tasks/resolve.yml`.
  `ansible_check_mode` is in a comment in `volume-revert/tasks/claim.yml` and in the value
  of `k8s_no_mutate` in `group_vars/all.yml`. `ansible.cfg` sets no `gathering`, and
  `ansible/pre_tasks/load_secrets.yml` runs `ansible.builtin.setup` unless
  `load_secrets_gather_facts` is false.
    - The test gains a fifth source: an allow-set that enumerates `ansible_check_mode` and
      `ansible_hostname`, re-measured and quoted by PR 8. It follows a `group_vars/all.yml`
      value's own names, so `k8s_no_mutate` brings in `ansible_check_mode`.
    - It asserts that `ansible/volume-revert.yml` gathers facts.
    - Two red fixtures join the `k8s_no_mutate` one. A task that reads `ansible_fqdn`
      fails, and so does a playbook copy with `gather_facts: false` and no setup task.
    - *Verification* and *Risks* (*The revert runbook loses its caller or a variable*)
      name both checks.

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review round covers it.

- **F-10 (the dialect).** *The renderer's dialect is specified* and PR 7d add an ADR that
  states the supported filters, lookups, tests and variable semantics. Spec tests hold the
  renderer to literals checked once against ansible-core, and a census refuses a template
  outside the subset. *Verification*, *Risks*, *Tests and docs*, the file tree and the
  delta follow. PR 7 lists five prerequisites of PR 8.
- **T3 (renderer dependency bumps).** *The render-diff gate* and PR 7e add the renderer's
  dependency pins to the hazard list, keyed on named `uv.lock` entries rather than the
  whole file. PR 11 still removes the gate, so Open question 12 asks the operator whether
  the hazard outlives R4.
- **F-05 (end size).** Exit 6 bounds the renderer at 2,600 non-test lines, against stated
  additions of about 1,940, and names what the headroom covers.
