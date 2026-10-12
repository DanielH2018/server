# D6 — Delete the old deploy path; ADR superseding 0010

Status: PLANNED

Depends on: [D5](d5-bulk-move.md)

Unblocks: R4

## Goal and exit criterion

After D6, the code that chose, applied, rolled back and audited k8s deploys outside Flux is
gone. D5 made it unreachable, and D6 deletes it:

- the GitOps deployer's k8s arms, its k8s owed ledger and its k8s hold;
- the k8s arms of `deploy.sh` and `land.sh`;
- the reach and narrowing code that serves only the k8s deploy plane;
- the deployer's reading of `k8s_autodeploy`, and the filter plugin that derived its denylist;
- the three k8s drift checks, the `probe.py releases` family, and the release-record branch of
  `probe.py health`;
- arm 1 of D4's cross-plane gate, the plane-order arm. The gate's other arms, D4's arm 2 and
  D5's suspended-tag and removal gates, stay in a module renamed for what they check;
- D2's handback. D6.7 switches each `<tag>` Kustomization except `flux` to the default
  deletion policy, so removing a tag from the bundle deletes its objects. A census refuses a
  commit that drops `owner: flux` from a k8s entry. D6.7a proves the policy in a throwaway
  kind cluster in CI before D6.7 merges.

What stays until H7:

- The deployer's setup arm and its fast-forward. H7 deletes the deployer and ships the timer
  that replaces the fast-forward.
- Setup narrowing, the setup routing tables in `deploy_cross_role.py`, and
  `land_lib/land_reach.py`.
- The shared part of `reach.py`, and `land.sh`'s setup and Pi arms.

R4 deletes the Ansible k8s mechanics: the manifests role's tasks, the k8s play in
`ansible/deploy.yml`, `render_records`, and `render_targets.py` with D2's skip in it.

After D6, `hold_sha` covers the host plane only, and `DEPLOY_HOLD` (C2) covers the cluster.
An accepted ADR records the decision to deploy the cluster with Flux from a CI-rendered
artifact, and marks [ADR-0010](../../adr/0010-pull-based-gitops-over-argo-and-flux.md)
`Superseded by ADR-NNNN`.

**Exit criterion.** Six checks, each under *Verification*:

1. **No path reaches the k8s play.**
    - No kept module imports `deploy_k8s`, `deploy_broad_k8s`, `deploy_k8s_owed` or
      `deploy_release`.
    - `ChangeSet` has no `k8s`, `k8s_deploy`, `k8s_origins` or `k8s_consumers` field. D2's
      `flux` field stays and takes every k8s role path, and the deployer reads no `owner`
      (*D2's ownership readers after D6*).
    - `deploy.sh` refuses every k8s tag, and `land.sh` never calls it with one.
    - Every k8s `containers_list` entry carries `owner: flux`, and CI fails a commit that
      drops one.
2. **The host plane still works.**
    - A setup-plane change merged after the last D6 PR is applied by the deployer's tick,
      and `land.sh` follows it to `VERDICT: settled`.
    - The setup-drift check reads green from daniel-box's own stamps, and from
      daniel-server's. `homelab_check_success{check="setup-drift-check"}` reads 1 for each
      `origin`, and no setup-drift rule fires.
    - The two host-plane pages that rode the k8s modules still fire. A Pi container role's
      `tasks/` change pages the tasks channel once, and a broad setup range that carries
      `ansible/vars/secrets.yml` pages the secrets alert once. D6.1's two tick tests pin
      both. D6.5 changes no secret, so its merge tick sends no page (#4609).
3. **The ADR is accepted.** ADR-0010's status names its successor.
   `ansible/tests/repo/test_adr_links.py` passes, and the `mkdocs.yml` nav lists the new ADR.
4. **The docs describe the Flux path.**
    - Root `CLAUDE.md`'s *After a PR Merges* and *When to wait* describe landing through
      `promote`.
    - Every `docs/facts.lock` section that D6 edits is re-verified, unless G2 has retired
      the lock.
    - `mkdocs build --strict` passes.
5. **The deletion list holds.**
    - `git ls-files` lists no path from the deletion list.
    - Each deleted symbol has no match in the files that shrink.
    - The import grep over every kept module in `ansible/`, `scripts/` and `.claude/` prints
      nothing. Each PR quotes that grep in its body.
6. **The promotion and landing code ends at a stated size.** Each count is of non-test lines
   at D6's last PR.
    - `scripts/deploy_tools/promote.py` is at most 900 lines. D1 states about 320 and D2
      about 350. The headroom covers D5's additions and D6.7b's tag rule and object-loss
      change.
    - `land.sh`, `land.py` and `land_lib/` together are at most 6,400 lines. They measured
      6,258 at `70591c307`. The bound allows all of D2.3's about 220 lines to land there,
      less the 121 lines of `land_rerolls.py` that D6.3 deletes, and D6.3 deletes more.

## Baseline at `70591c307`

Each count comes from `wc -l` at `70591c307`. D6 re-measures before each PR, because other
sessions edit these paths. Where a list says "kept", an import grep at `70591c307` found a
reader that D6 does not delete.

### The deployer

- **Modules D6 deletes whole**, under
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/`, 1,131 lines in all:
    - `deploy_k8s.py`, 354: `is_image_only_diff`, `declared_denylist`,
      `split_k8s_auto_deploy`, `k8s_role_paths`;
    - `deploy_broad_k8s.py`, 216: `apply_broad_k8s`;
    - `deploy_k8s_owed.py`, 400: `reconcile`, `discharge_k8s_unapplied`;
    - `deploy_release.py`, 161. Its importers are `deploy_k8s_owed.py`, `deploy_narrow.py`,
      `deploy_toolbox.py` and `scripts/deploy_tools/deploy_lib/playbook.py:discharge_owed_k8s`.
- **Two of those modules are mixed.** `deploy_k8s_owed.py` and `deploy_broad_k8s.py` carry
  the only path to two host-plane pages. D6.1 moves those calls into `deploy_handlers.py`
  before it deletes the two modules whole.
    - **The tasks page.** `deploy_alerts.alert_deferred` fires the tasks channel and the k8s
      channel. Its only caller is `deploy_k8s_owed.alert_and_record_deferred`, whose
      docstring reads "`deploy_alerts.alert_deferred`, plus the durable record of what it
      paged about". `deploy_changes.services_from_changed_paths` puts a container role's
      `tasks/`, `defaults/` or other non-template path in `cs.tasks`, so that "it
      defer-and-alerts on the tasks channel instead of taking the silent docs-only ff-merge".
      `deploy_handlers.handle_no_services`, the failure arm of `deploy_handlers.handle_broad`
      and `apply_broad_k8s` all reach that page through `alert_and_record_deferred`. The Pi's
      container roles live until H5 and H7.
    - **The secrets page on a broad range.** `handle_broad`'s success exit is `return
      deploy_broad_k8s.apply_broad_k8s(...)`. The last exit of `apply_broad_k8s` calls
      `deploy_alerts.alert_secrets_deferred`, under the comment "The tick's last exit, and the
      only one a range with nothing deferred reaches, so the secrets page has to be here as
      well as on the two failure arms (#2459)". `handle_broad`'s own failure arm calls it
      directly, and so does `handle_no_services`.
    - **Coverage.** `test_gitops_deploy_phases.py:test_handle_no_services_merges_and_flags_a_rotated_secret`
      pins `handle_no_services`'s direct secrets call. A grep for the `"tasks"` slot and for
      `secrets.yml` in `test_gitops_deploy_main_branches.py`, `test_gitops_deploy_broad_k8s.py`
      and `test_gitops_deploy_phases.py` finds no test of the tasks page through a handler,
      and none of the secrets page on a broad range's success exit.
      `test_gitops_deploy_main_branches.py:test_a_secrets_change_bundled_with_an_image_bump_is_still_flagged`
      rides the image-bump path, which D6.1 deletes. `test_gitops_deploy_alert_channels.py`
      calls `alert_secrets_deferred` directly with a `ChangeSet(secrets=True)`.
    - **The annotation is k8s-only.** `apply_broad_k8s` computes `applied = cs.k8s_deploy -
      bumps` and passes that to `tools.emit_deploy_annotation`. `deploy_io.emit_deploy_annotation`'s
      docstring says "Only the k8s auto-deploy path and the broad arm call this". The setup
      applies are recorded by `state.record_receipt` and were never annotated.
      `ansible/tests/deploy/test_deploy_annotations.py` pins the emitter's name and its
      swallowed failure in `deploy_io.py`.
    - **The annotation chain is host syslog.** Two emitters write a `deploy-annotation`
      line through `logger`: `deploy_io.emit_deploy_annotation` and `deploy.sh`'s
      `deploy_lib/playbook.py:annotate`. Alloy ships host syslog to loki-homelab, and
      `scripts/grafana/inject_dashboard_annotations.py` adds one query to every provisioned
      dashboard, `ansible/roles/k8s/observability/defaults/main.yml:observability_deploy_annotation_expr`,
      which reads `{job="syslog"} |= "event=deploy" | logfmt`. The comment above that key
      names `deployer_lib/deploy_io.py` and "the emit_deploy_annotation comments", and
      records the reason for syslog: "No Grafana API call, so no pinned ClusterIP and no
      standing write credential".
    - **A third syslog stream marks landings, not deploys.** `land.py` writes one
      `event=landing` line per landing, built by `land_lib/ledger.py:annotation_line` and
      sent by `land_lib/tools.py:syslog` under the tag `landing-annotation`. Only the
      Landings board reads it (`test_landing_annotations.py`). The injected `event=deploy`
      query does not match it.
    - **Every workflow runs on a GitHub-hosted runner.** Each `runs-on` under
      `.github/workflows/` names `ubuntu-26.04`. C2's `promote` workflow runs there too, so
      a `logger` call in it writes to the runner's own syslog, which no Alloy ships.
- **`deploy_cross_role.py` (318) is mixed.**
    - The setup symbols stay until H7: `SETUP_FILES_SHIPPED_BY_OTHER_ROLES`,
      `SETUP_FILES_ROUTED_TO_OWNER`, `SETUP_ROLES_CALLING_FILTER_PLUGINS`, `CROSS_ROLE_FILE`,
      `tables_in`, `use_tables` and `owner_routed_consumers`.
    - Their readers are `deploy_changes.py:setup_roles_for`, `deploy_remediation.py`,
      `deploy_phases.py`, `scripts/deploy_tools/narrow_setup.py`, `land_lib/land_reach.py`,
      `land_lib/classify.py` and `ansible/tests/setup/test_setup_cross_role_files.py`.
    - `SETUP_FILES_SHIPPED_BY_OTHER_ROLES` routes a change to `gitops_markers.py` or
      `gitops_ledger.py` to the deploy_ui and renovate_agent installs.
    - `K8S_ROLES_IMPORTING_SETUP_FILES` and `k8s_lookup_readers` are k8s-only. Their
      readers are `deploy_changes.py`, `deploy_phases.py` and `scripts/deploy_tools/reach.py`.
    - `k8s_lookup_map` stays. `scripts/secrets_mgmt/consumers.py:_census_corpus` calls it for
      the secret-consumer census, which Flux-rendered roles still need.
- **Other deployer files that shrink.**
    - `deploy_narrow.py` (579): `narrow_deploy_plane`, `shared_role_callers`,
      `digest_provable` and `digest_provable_argv` go. `narrow_setup_role` stays.
    - `deploy_handlers.py:_rollback_k8s`, and the deploy-plane plan in
      `deploy_narrow.plan`.
    - `deploy_phases.py`: `reconcile_denylist`, `_promote_k8s_auto_deploys`, and the call to
      `deploy_cross_role.k8s_lookup_readers`.
    - `deploy_changes.py:ChangeSet`: `deploy_phases.py` sets `k8s_deploy`, `k8s_origins`
      and `k8s_consumers`. `scripts/deploy_tools/reach.py:Reach.touched` reads `k8s` and
      `k8s_deploy`, and `deploy_lib/tags.py`, `deploy_lib/staleness.py` and
      `narrow_lib/broad.py` read `k8s`. `deploy_lib/staleness.py` calls `touched` for
      `deploy.sh`'s staleness refusal, which still serves the Pi, and
      `scripts/deploy_tools/tests/test_reach.py` calls it too.
    - The exit 1 grep at `70591c307` also names `gitops_deploy.py` (`deploy_k8s_owed.reconcile`
      and `K8S_AUTODEPLOY_DENYLIST`), `deploy_io.py`, `deploy_logic.py`,
      `deploy_remediation.py:k8s_remediation`, `deploy_toolbox.py`, `deploy_defer.py`,
      `deploy_alerts.py`, `deploy_alert_text.py`, `deploy_state.py` (`OWED_K8S_DEFERRED`,
      `OWED_K8S_UNAPPLIED`), `deploy_inventory.py:declared_k8s_services` and
      `deploy_config.py`.
    - Beyond `files/`: the role's `config.env` template, `templates/gitops-deploy.service.j2`
      (its timeout derives from the k8s timeouts), `defaults/main.yml`
      (`gitops_deploy_k8s_*`), and `ansible/inventory/host_vars/daniel-box.yml`
      (`gitops_deploy_k8s_autodeploy_enabled`, `gitops_deploy_k8s_autodeploy_pilot`).
- **The deployer installs its own code file by file.** `tasks/code.yml`'s "Install deployer
  Python files" task copies one file per loop item. The file says "A copy loop removes nothing
  it stops naming", and its "Remove deployer modules folded into another" task deletes three
  modules from `/opt/gitops-deploy` with `state: absent`. A self-applied D6.1 therefore leaves
  the deleted modules on disk unless an `absent` task names them.
    - A second absent task, "Remove the deployer libraries that moved into deployer_lib",
      already names the flat copies `deploy_k8s.py`, `deploy_broad_k8s.py`,
      `deploy_k8s_owed.py` and `deploy_release.py`. Both absent tasks stay until H7, so
      `tasks/code.yml` keeps matching exit 1's grep by design.
- **Kept files that name the deleted symbols only in prose.** Exit 1's and exit 5's greps at
  `70591c307` match comments and docstrings in files that D6 keeps:
    - `gitops_markers.py` and its monitor-bridge copy: the comment above
      `RENDER_PROOF_SHARED_ROLES` names `deploy_k8s_owed.DIGEST_PROVABLE_ROLES`, and the
      comment on the `denylist_rendered` slot of `MARKERS` names
      `deploy_phases.reconcile_denylist`. `deploy_phases.py` is the slot's only reader, and
      `ansible/roles/setup/gitops_deploy/tests/test_deployer_state.py` lists it;
    - comments and docstrings in `deploy_defer.py`, `deploy_inventory.py`, `deploy_state.py`,
      `deploy_alert_text.py` and `deploy_config.py`, the module docstring of
      `deploy_phases.py`, the docstrings of the functions `deploy_narrow.py` keeps, and the
      comments in `gitops_deploy.py`;
    - the module docstring of `deploy_cross_role.py`, which names `k8s_lookup_readers`;
    - `scripts/deploy_tools/narrow_setup.py`'s docstring, which names `narrow_deploy_plane`;
    - the docstring in `scripts/deploy_tools/land_lib/tools.py` that names
      `deploy_phases.reconcile_denylist`;
    - `ansible/roles/setup/common/CLAUDE.md`, which names
      `deploy_cross_role.K8S_ROLES_IMPORTING_SETUP_FILES`.
- **Holds and the ledger.**
    - `gitops_hold.py:Hold.set_sha` says "A k8s rollback holds this way", and
      `Hold.cover_services` covers a hold through `deploy.yml --tags`. After D6 nothing runs
      `deploy.yml` for a k8s service, so a k8s hold left standing never clears by an apply.
    - `deploy_k8s_owed.py` is the only module that discharges `k8s_deferred` and
      `k8s_unapplied` lines.
    - `gitops_ledger.py:parse_owed` keeps a line whose class the tree does not know, so a
      stray line cannot crash a reader. It can still reach a page.
    - `gitops_state.py clear-owed` accepts `k8s_deferred` and `k8s_unapplied`, and
      `clear-hold <sha>` clears `hold_sha` with its `hold_plane` lines.
- **Readers of `k8s_deferred` and `k8s_unapplied` outside the deployer's modules:**
    - monitor-bridge's `files/checks/gitops.py`, plus `files/gitops_markers.py` and
      `files/gitops_ledger.py`. Those two are generated copies that
      `scripts/dev/gen_gitops_markers.py` writes and
      `ansible/tests/deploy/test_gitops_markers_copies.py` checks;
    - `ansible/roles/setup/deploy_ui/files/deploy_ui_reads.py` and
      `ansible/roles/setup/renovate_agent/files/agent_logic.py`;
    - `.claude/hooks/session-health.py`, with `test_session_health_k8s_deferred.py` and
      `test_session_health_k8s_unapplied.py` in `.claude/hooks/tests/`;
    - `scripts/lib/deployer_park.py`, `scripts/lib/narrow_git.py`,
      `scripts/deploy_tools/gitops_state.py`, `scripts/diagnostics/probe_lib/gitops_view.py`
      and `scripts/deploy_tools/deploy_lib/staleness.py`.

### How the deployer routes a deploy-plane path

`deploy_changes.py:_BROAD_DEPLOY_PREFIXES` lists `ansible/templates/`, `ansible/inventory/`,
`ansible/deploy.yml`, `ansible/pre_tasks/`, `ansible/tasks/`, `ansible/post_tasks/`,
`ansible/filter_plugins/` and `ansible.cfg`. Measured with
`deploy_changes.services_from_changed_paths`:

- `group_vars/all.yml`, `host_vars/daniel-pi.yml`, a template under `ansible/templates/`,
  `deploy.yml`, `tasks/k8s_batch.yml`, `filter_plugins/toposort.py` and `ansible.cfg` each give
  `broad=True, broad_deploy=True` and no setup role.
- `ansible/roles/k8s/bento-pdf/templates/deployment.yaml.j2` gives `k8s={'bento-pdf'}`.
- `deploy_handlers.py` hands `cs.broad_deploy` to `deploy_narrow.plan`, which plans a
  narrowed `deploy.yml` apply.
- No setup role reads an inventory change through the tick. `narrow_setup.py` greps
  `ansible/inventory/` only to find where a defaults key is overridden.
- A setup template changed beside an inventory key routes to its role.
  `services_from_changed_paths` on `ansible/roles/setup/initial_setup/templates/setup-drift-check.sh.j2`
  together with `group_vars/all.yml` gives `broad_setup=True, setup_roles={'initial_setup'}`,
  as well as `broad_deploy=True`. The tick then applies `initial_setup` on daniel-box after
  its fast-forward, so the apply reads the new `group_vars`.

### The denylist plugin and the `k8s_autodeploy` key

- **The plugin.** `ansible/filter_plugins/k8s_autodeploy.py` (207) defines
  `k8s_autodeploy_denylist`, `is_leftover_dir` and `SHARED_ROLES`.
    - Its one Jinja caller is `gitops_deploy/defaults/main.yml:gitops_deploy_k8s_autodeploy_denylist`.
    - `scripts/lib/k8s_roles.py:is_leftover_dir` imports the rule lazily, and `role_dirs`
      calls it. `role_dirs` serves `scripts/deploy_tools/setup_routing.py`,
      `scripts/validate/k8s_manifests.py`, `scripts/docs/catalog_lib/catalog_backup.py`,
      `scripts/docs/catalog_lib/glance_facts.py`, `probe_lib/releases_retired.py` and
      `scripts/dev/k8s_autodeploy_counts.py`.
    - The gitops_deploy role's `CLAUDE.md` *Safety* says "A setup role parks only while
      `scripts/deploy_tools/setup_routing.py` fails or times out". An ImportError in
      `role_dirs` would therefore park every setup-plane change.
    - `scripts/dev/k8s_autodeploy_counts.py` imports `SHARED_ROLES` and
      `k8s_autodeploy_denylist`. `scripts/docs/gen_doc_fragments.py`'s `_autodeploy_coverage`
      lists the plugin path as a source.
    - `ansible/tests/deploy/_autodeploy.py` imports `is_leftover_dir` and
      `k8s_autodeploy_denylist`, and
      `ansible/tests/deploy/test_renovate_automerge_follows_the_autodeploy_denylist.py` imports
      `_denylist` from it.
    - `deploy_cross_role.SETUP_ROLES_CALLING_FILTER_PLUGINS` routes the plugin path to
      `gitops_deploy`.
- **The key.** 62 role defaults declare `k8s_autodeploy`, each under a comment that opens
  "Auto-deploy stance". The comments differ per role, because each states that role's reason.
  Readers outside the deployer:
    - Renovate: `renovate.json`'s no-automerge rule (#1886) and its `manual — k8s_autodeploy:
      false` group names, `renovate_agent/files/land_renovate_pr.py:DENIED_SLUG`, and
      `renovate_notify/files/pending_logic.py`;
    - docs: `scripts/docs/gen_role_glance.py`, which writes "Auto-deploy: eligible" or
      "denylisted" into 57 role `CLAUDE.md` blocks at `70591c307`, plus
      `catalog_backup.py`, `service_catalog.py` and `fragment_renderers.py`. D2 replaces
      that line with the Flux line in every flux-owned role's block, so at D5's end no block
      prints it (*What D2, D4 and D5 hand to D6*);
    - `scripts/lib/estate.py`, `scripts/lib/render_context.py`, the scaffold in
      `scripts/dev/new_k8s_service.py`, and the `new-k8s-service` and `renovate-prs` skills;
    - about 25 test modules. Some of them, such as the `test_k8s_autodeploy_*_gates.py` family,
      police the k8s play's rollout gates, which R4 owns.
- **The Renovate lander reads the deployer's rendered list at runtime.**
    - `renovate_agent/files/land_renovate_pr.py:denied_roles` reads `K8S_AUTODEPLOY_DENYLIST`
      from the file that `land_renovate_pr.py:DEPLOYER_CONFIG` names,
      `/etc/gitops-deploy/config.env`.
    - Its docstring says an empty list does not mean "nothing is denied". The caller,
      `land_renovate_pr.py:main`, refuses with "the deployer's denylist is empty" when the
      key is missing or empty.
    - `ansible/inventory/host_vars/daniel-box.yml` sets `renovate_agent_enabled: true`, so
      the lander is armed.
    - The unit `renovate-agent-land@.service.j2` runs the lander as `sys_user` through `uv
      run --no-project` from `/opt/renovate-agent/`, so only the lander's own directory is
      on `sys.path`. The unit also sets `RENOVATE_AGENT_REPO_DIR`, the checkout from which
      the lander already runs `land.sh`.
    - `docs/renovate-agent-bounds-and-digest.md` says "The denied roles come from the
      deployer's rendered `K8S_AUTODEPLOY_DENYLIST`, so the lander adds no parser of its
      own". `test_land_renovate_pr.py`'s `deployer_config` fixture and
      `test_an_empty_denylist_is_not_read_as_nothing_denied` pin that read.
    - `git grep` finds no other reader of `K8S_AUTODEPLOY_DENYLIST` outside the gitops_deploy
      role. `scripts/deploy_tools/deploy_lib/detach_notify.py:CONFIG_ENV_PATH` reads the same
      file, but only its `DISCORD_WEBHOOK`, which D6 keeps.
- **Different keys that share the prefix.** The manifests role reads
  `k8s_autodeploy_snapshot_pvcs`, and R4 owns it.

### Reach and narrowing in `scripts/deploy_tools/`

- **`reach.py` (224) is shared and shrinks.**
    - `land_lib/land_shared.py` imports `role_for` and `is_role_test_path` from it.
    - `land_tags.py`, `land_platform.py`, `deploy_lib/tags.py`, `deploy_lib/staleness.py` and
      `narrow_lib/broad.py` import it too.
    - Its k8s readings are `Reach.changes.k8s` and `k8s_deploy`, and the
      `K8S_ROLES_IMPORTING_SETUP_FILES` and `k8s_lookup_readers` lookups in `Reach.tags` and
      `Reach.touched`.
- **`land_lib/land_reach.py` (563) is setup-plane and stays until H7.** Its docstring says
  "`remaining_setup_hosts_note` is the string land.sh prints and the verdict hangs on".
  `land_tags.py` imports it, and `land_lib/tools.py` binds it.
- **k8s only:**
    - `shared_role_callers.py`, 224. `land_lib/land_shared.py` imports it at top level,
      `deploy_lib/run.py` imports `expand_shared_tags` from it, and the deployer runs it as
      `deploy_narrow.SHARED_CALLERS_SCRIPT`.
    - `shared_role_reach.py`, 287. `land_lib/tools.py` imports it at top level and binds
      `paths_a_hand_must_apply`.
    - `digest_provable.py`, 138. The deployer runs it as `DIGEST_PROVABLE_SCRIPT` for
      `deploy_k8s_owed.py`.
    - `land_lib/land_rerolls.py`, 121. It imports `releases.RELEASE_DIR`, and
      `land_lib/tools.py` imports it.
- **`prune_releases.py` (123) is host-plane and stays.**
  `ansible/roles/setup/common/tasks/release_bin.yml` ships it into every host-script release
  group.
- **`narrow_lib/`.** The import grep at `70591c307` shows the following:
    - `narrow_setup.py` imports `filters`, `paths`, `foreign_keys`, `setup_index` and
      `setup_playbook`. `filters` imports `templates`, and `foreign_keys` imports
      `setup_index` and `task_walk`.
    - So setup narrowing keeps seven modules until H7: `setup_index.py` 500, `setup_playbook.py`
      75, `task_walk.py` 203, `filters.py` 139, `foreign_keys.py` 133, `paths.py` 74 and
      `templates.py` 204.
    - Only `broad.py` (600) and `containers.py` (250) serve the deploy plane alone.
      `shared_role_callers.py` imports `broad.py`, and so do two lazy imports:
      `deploy_lib/tags.py`'s `narrow_cmd` and `probe_lib/releases.py`.
- **`scripts/deploy_tools/render_targets.py` (88 lines) stays.** C1 names it as a renderer
  input, and R4 deletes it.

### The drift checks and `probe.py releases`

- **`manifest-prune-check.sh.j2` (341) has three arms.** Its header names them:
    - orphaned cluster objects;
    - "copy:-deployed setup code that differs byte-for-byte from its source";
    - "template:-rendered setup scripts whose SOURCE has changed".

  Arms 2 and 3 are host-plane, and they live in
  `ansible/roles/setup/initial_setup/files/setup-drift-lib.sh`, "shared with
  setup-drift-check.sh".
- **Only daniel-server runs the setup-drift reader.**
    - `group_vars/all.yml:setup_drift_check_hosts` lists daniel-server alone. Its comment says
      manifest-prune-check "exists on daniel-box alone".
    - `ansible/tests/deploy/test_setup_drift_check.py::test_the_reader_is_armed_where_no_manifest_prune_check_runs`
      asserts that no k3s server host is in the list.
    - The Kuma tile is "Setup Plane Drift (agent hosts)", with one token,
      `setup_drift_push_token`.
    - After manifest-prune-check goes, nothing reads daniel-box's setup-plane stamps unless
      daniel-box joins the list.
- **The other k8s checks.** `live_drift_check.py` 468, `release-staleness-check.sh.j2` 200,
  and `ansible/roles/setup/k3s/files/manifest_declares.py` 142. The last is copy-deployed
  beside manifest-prune-check and tested by `ansible/tests/k8s/test_manifest_declares.py`
  (175).
- **The release-staleness cron runs code from the primary checkout.** The script sets
  `REPO_DIR=/home/{{ sys_user }}/server` and runs `scripts/diagnostics/probe.py releases
  --stale-only --kuma` there, every 30 minutes. The tick fast-forwards that checkout. So a
  merged deletion of `releases.py` breaks the cron at the next tick, before any hand apply
  removes the cron.
- **daniel-server's setup-drift reader reads its own checkout.** It recomputes each stamped
  source's checksum from the checkout at its `REPO_DIR`, and the `TREE_MAX_DAYS` fault fires
  when that checkout ages. A changed `setup-drift-check.sh.j2` therefore reads as stale on
  daniel-server once its checkout is pulled, until that host re-renders it.
  `stamp_render_name: setup-drift-check` in `initial_setup`'s `tasks/crons.yml` stamps that
  template.
- **`ansible/roles/setup/k3s/tasks/health-crons.yml` shares tasks between checks.**
    - Four tasks carry `manifest-prune` or `release-staleness` beside `backup-health`,
      `disk-health` or `etcd-snapshot`. These include the shared env and credential files.
    - Release groups: `k3s-manifest-prune`, `k3s-release-staleness` and `k3s-live-drift`.
      `release_bin.yml` has no absent arm.
    - Stamp tasks: `stamp_render_name: k3s-manifest-prune` and `k3s-release-staleness`, and
      `stamp_deployed_name: k3s-live-drift` and `k3s-manifest-declares`. Each writes a
      fragment under `/var/lib/homelab/setup-render-manifest.d/` or
      `setup-deployed-manifest.d/`. A deleted task deletes no fragment.
    - `test_a_source_deleted_from_the_repo_is_drift_not_an_exemption` shows that a fragment
      whose source is gone reads as drift.
- **The push tokens.** `live_drift_push_token`, `manifest_prune_push_token` and
  `release_staleness_push_token`:
    - `grep -c '^<name>:' ansible/vars/secrets.yml` prints 1 for each, and
      `ansible/secret_rotation.yml` lists each.
    - `scripts/secrets_mgmt/secret_rotation.py:cmd_sync` adds back a `secrets.yml` key that
      the registry lacks.
    - `scripts/secrets_mgmt/tests/test_secret_consumers.py::test_every_auto_tier_token_resolves_a_consumer_or_is_known_manual`
      fails for a token with no consumer.
    - Their consumers are uptime-kuma's `static-monitors.yaml.j2`, the k3s role's
      `live-drift-check.env.j2`, and the shared `kuma-push.env.j2`, which renders
      `manifest_prune_push_token` and `release_staleness_push_token` beside the tokens of the
      checks that stay. Its task carries five tags, among them `backup-health`, and its
      `no_log` comment counts five tokens. `scripts/secrets_mgmt/consumers.py:CROSS_HOST_PUSH_TOKENS`
      names all three.
- **The `releases` family is 1,483 lines.** `releases.py` 594, `releases_render.py` 262,
  `releases_diff.py` 276, `releases_format.py` 179, `releases_consumers.py` 127 and
  `releases_retired.py` 45.
    - Importers of `releases.py`: `probe_lib/subcommands.py`, `land_lib/land_rerolls.py`, and
      `probe_lib/health.py:release_expected_restarts` through a lazy `RELEASE_DIR` import.
    - `render_records` writes records that only `probe.py releases` and `deploy_release.py`
      read (`ansible/roles/setup/render_records/CLAUDE.md`). It imports `deploy_config`,
      `deploy_git` and `deploy_toolbox` from `/opt/gitops-deploy`, and runs `deploy.sh
      --dry-run`. D6.1 edits `deploy_config.py` and `deploy_toolbox.py`, and D6.3 changes
      `deploy.sh`, so an armed producer would break under D6.
    - `scripts/diagnostics/probe_lib/gitops_view.py` has no reference to `render_records`, so
      `probe.py gitops-state` cannot show whether the producer is armed. D5's disarm PR reads
      it with `systemctl list-unit-files 'kuma-check-render-records*'`, and the lever is
      `ansible/roles/setup/render_records/defaults/main.yml:render_records_state`.

### What D2, D4 and D5 hand to D6

These facts come from the D2, D4 and D5 plans, because their code does not exist at
`70591c307`.

- **D2 adds the ownership readers, and D5 makes every k8s entry `owner: flux`.** D2's
  entry-point table adds, among others:
    - `deploy_inventory.py:service_owners` and a `ChangeSet.flux` set. The deployer routes a
      flux-owned role's paths to `cs.flux`: "fast-forwarded, logged, never deferred, never
      alerted, never owed". D2.2 also adds drops to `deploy_narrow.py` and
      `deploy_k8s_owed.py`, and a test that a flux-owned image bump lands in `cs.flux` and
      never in `cs.k8s_deploy`.
    - `scripts/lib/k8s_roles.py:tag_owners`. Its readers are `deploy_lib/tags.py`'s exit 5,
      `land_lib/deploy.py`'s split before `deploy_by_host`, `land_lib/merge.py`'s handback
      refusal, `smoke_caller`, `secret_rotation.py --deploy`, `probe_lib/health.py`'s
      release-record skip and `probe_lib/releases.py`'s skip.
    - The glance and catalog line. For `owner: flux`, `gen_role_glance.py` and
      `service_catalog.py` print that Flux applies the role through `promote` and that
      `deploy.sh` refuses it with exit 5, "in place of the deploy tag and the auto-deploy
      stance". After D5, therefore, no glance block prints "Auto-deploy: eligible" or
      "denylisted".
    - The `ansible_owned` filter, which R4 deletes with the play.

  After D5, `cs.k8s` stays empty for every path whose role has an entry, because each such
  role is flux-owned.

- **`scripts/validate/cross_plane_order.py --base <sha>` holds four arms.** It renders the
  cluster at the PR's base and head.
    - D4's arm 1, plane order, fails when two changed roles sit on different planes and one
      depends on the other. It judges an image-only diff as `deploy_k8s.py:is_image_only_diff`
      does.
    - D4's arm 2, edge first, fails when a flux-owned tag gains a `dependsOn` edge and its own
      `cluster/<tag>/` output also moved. Its subject is an edge between two flux-owned tags.
    - D5.1d's gate refuses a PR that changes the render of a tag suspended on its base. D5
      keeps `flux_suspend` as the way to stop a role, and `promote.py decide` holds all
      promotion with the cause `suspended-changed` when such a change reaches master.
    - D5.1f's two removal arms fail two kinds of PR. The first drops an object of any kind
      from a flux-owned tag's render without a `Removes-Object:` trailer. The second adds a
      flux-owned name to `k8s_retired_services`. The dropped-object arm exempts a handback,
      because `deletionPolicy: Orphan` deletes nothing.
- **Flux applies itself.** After D5's flux PR, the `flux` tag applies D1's
  `files/gotk-components.yaml`, which holds the controllers and the Flux CRDs, with
  `prune: true`. `flux-deferred` applies the sync objects: the OCIRepository `homelab`, the
  root Kustomization `homelab`, the Providers and the Alerts. D5's rollback of the flux PR
  relies on `deletionPolicy: Orphan` for both.
- **H1's plumbing tags render `prune: false`.** H1's five plumbing roles set `flux_prune:
  false`, and the default deletion policy follows `prune`, so deleting their Kustomizations
  deletes nothing under either policy.
- **No k8s role renders a CRD at `70591c307`.** `git grep -l 'kind: CustomResourceDefinition'
  -- ansible/roles/k8s` prints nothing.
- **media-volume renders a static PersistentVolume and its StorageClass.**
  `ansible/roles/k8s/media-volume/templates/pv.yaml.j2` renders the PersistentVolume
  `media-data` with `persistentVolumeReclaimPolicy: Retain` and a `claimRef` by name, and
  `storageclass.yaml.j2` renders `media-local`. The PVC comes from the role's `k8s_claims`.
  Seven roles mount the claim. C3 exempts only PVCs and Namespaces from prune.

### Docs, facts and ADRs

- **Failure classes.** In `docs/failure-classes.md`:
    - class 5, "Sequences treated as transactions", cites
      `test_the_reader_agrees_with_pyyaml_on_every_rendered_manifest`, "(feeds
      `manifest-prune-check.sh`)";
    - class 3, "Empty is read as clean", applies to a check left with nothing to inspect;
    - class 8 is "Prose asserts facts nothing re-derives".

  `ansible/tests/repo/test_failure_class_detectors.py` fails until a deleted cited test
  leaves the table.
- **`docs/facts.lock` records the sections D6 edits:**
    - in the gitops_deploy role's `CLAUDE.md`: *Safety*, *Which apply clears a hold*, *Traps*
      and the title section;
    - in the k3s role's `CLAUDE.md`: *Autonomous-role contract*, *Layout*, *Notable* and the
      title section;
    - in the render_records role's `CLAUDE.md`: all three sections;
    - in the renovate_agent role's `CLAUDE.md`: *Autonomous-role contract*;
    - in root `CLAUDE.md`: *After a PR Merges — Pull, Deploy, Verify*.

  The `facts-lint-changed` hook runs `vanished-identifier`, which fails a commit while any
  section still names an identifier the branch deleted.
- **The docs pages that describe deleted behaviour:**
    - `docs/gitops-pipeline.md` (1,534 lines), whose sections include *A deploy-plane change
      is narrowed before it is applied* and *The `k8s_deferred` and `k8s_unapplied` markers,
      in full*;
    - `docs/landing.md` and `docs/deploying.md`;
    - `docs/k3s-node-plane-crons-and-incidents.md`, which holds the drift and prune checks'
      detail;
    - `docs/k8s-manifest-cycle-record.md`, which holds the release record's reader rules;
    - `docs/monitor-bridge-checks.md`, `docs/monitor-bridge-internals.md` and
      `docs/claude-tooling.md`, which name the k8s ledger markers;
    - `docs/renovate-agent-bounds-and-digest.md`, which explains the denylist marker.
- **How ADRs work.**
    - `docs/adr/index.md` says an ADR is never rewritten. A superseding ADR sets the old one's
      status to `Superseded by ADR-NNNN`.
    - `ansible/tests/repo/test_adr_links.py` checks the `governs:` markers in both directions,
      the index row, and that the nav lists every ADR.
    - The highest number at `70591c307` is 0019. G2 writes an ADR amending 0019, and D3 or H4
      amends 0003, so D6 takes the next free number when it merges.
- **Root `CLAUDE.md` describes the old path.** The sections are *After a PR Merges — Pull,
  Deploy, Verify*, *The procedure* and *When to wait*. G5 shrinks root `CLAUDE.md`, so D6
  edits the sections as they stand when it runs.

## Design

### What D6 deletes, what R4 and H7 delete, what stays

- **D6 deletes the callers and the decision logic:**
    - the deployer's k8s modules, fields and config, `_rollback_k8s`, `reconcile_denylist`,
      and the denylist line in `/etc/gitops-deploy/config.env`;
    - `filter_plugins/k8s_autodeploy.py`, after moving its two live functions;
    - `land_lib`'s k8s deploy arm, `land_rerolls.py`, and `deploy.sh`'s k8s arm;
    - `shared_role_callers.py`, `shared_role_reach.py`, `digest_provable.py`,
      `narrow_lib/broad.py` and `narrow_lib/containers.py`;
    - the k8s readings in `reach.py` and `deploy_cross_role.py`;
    - the three drift checks with their timers and M6 B1's rules for them,
      `manifest_declares.py`, and the six `releases*` modules. M6's B1 retires their Kuma
      tiles and tokens (#4609);
    - every reader of the k8s ledger classes, and arm 1 of D4's `cross_plane_order.py`.
- **R4 deletes the Ansible k8s mechanics:**
    - the manifests role's render, apply, prune and drain tasks;
    - the k8s play in `ansible/deploy.yml`, with its pre_tasks and post_tasks;
    - `render_records`, and `render_targets.py` with D2's skip in it;
    - the render halves of the role task files, and about 70 Ansible-mechanics tests,
      including the `test_k8s_autodeploy_*_gates.py` family;
    - the `ansible_owned` filter, because the play it guards is gone;
    - the retirement arm of the render-diff gates, with `k8s_retired_services`.
- **H7 deletes the rest:**
    - the deployer;
    - setup narrowing, meaning `narrow_setup.py`, `setup_routing.py` and the seven
      `narrow_lib` modules listed in the Baseline;
    - `land_reach.py`, the shared rest of `reach.py`, and `land.sh`'s setup and Pi arms;
    - `deploy_cross_role.py`. H7 moves `k8s_lookup_map` out first.
- **Stays after D6:**
    - the deployer's setup arm and its fast-forward, and `deploy_git.py:ci_verdict`;
    - `promote.py`, `promote.yml` and the renderer, plus `render_targets.py` and
      `prune_releases.py`;
    - the `owner` flag. H1 removes it with `containers_list`;
    - `ChangeSet.flux`, as the route of every k8s role path, until H7 deletes the deployer,
      and `tag_owners` with the readers that H1.9 rewrites (*D2's ownership readers after
      D6*);
    - the tasks page and the secrets page, which D6.1 moves into `deploy_handlers.py` (*The
      host-plane pages after D6.1*);
    - the render-diff gates in `scripts/validate/render_diff_gates.py`, which D6.1 renames from
      `cross_plane_order.py`: D4's arm 2, D5.1d's suspended-tag gate and D5.1f's two removal
      arms, plus D6.7b's tag rule (*The render-diff gates after D6*);
    - D2's skip in `secret_rotation.py --deploy`, which passes only Pi tags to `deploy.sh`
      until H1 removes `containers_list`;
    - `deploy.sh`'s refusal of a k8s tag, which prints the Flux landing path until R4 removes
      the play.

### Order: an importer goes before what it imports

Scripts import deployer modules, and the deployer runs scripts as subprocesses. A deletion
breaks CI or a timer whenever a kept module still imports the deleted one, and several
imports are lazy:

- `deploy_lib/playbook.py:discharge_owed_k8s` imports `deploy_k8s_owed` and `deploy_release`;
- `probe_lib/health.py` and `land_rerolls.py` import `RELEASE_DIR`;
- `deploy_lib/run.py` imports `shared_role_callers`;
- `deploy_lib/tags.py` and `probe_lib/releases.py` import `narrow_lib.broad`;
- `scripts/lib/k8s_roles.py` imports the denylist plugin.

A test suite can run green while one of these fails only on the path that imports it. So
each PR runs a grep over every kept module for imports of what it deletes, lazy ones
included, and quotes the output in its body. The grep covers symbols as well as modules.
A PR that removes a name from a kept module, such as one in `deploy_logic.py`'s re-export
list, runs `git grep -n -w <name> -- ansible scripts` for it. A module-name grep cannot
see `from deployer_lib.deploy_logic import (...)`, which names only the symbol, and
`land_tags.py` and `deploy_lib/tags.py` import `k8s_remediation` that way. Each PR that
touches `land_lib`, `deploy_lib`, `reach.py` or `deploy_logic.py` also quotes
`uv run python scripts/deploy_tools/land.py --help` and
`uv run python scripts/deploy_tools/deploy_tags.py changed origin/master`. The merge
order follows from the import edges, and from one runtime edge:

- D6.5b deletes `releases.py`, so it merges before D6.4, because `releases.py` imports
  `narrow_lib/broad.py`.
- The release-staleness cron runs `probe.py releases` from the primary checkout (Baseline).
  D6.5 removes that cron, and its hand apply runs, while `releases.py` is still in the tree.
  D6.5b deletes the code only after that apply.

### The route of a deploy-plane path after D6

This section answers R4's open question 7. From the first tick that runs D6.1's code, the
tick applies nothing for a `_BROAD_DEPLOY_PREFIXES` path. The tick that fast-forwards D6.1
still runs the old code, so D6.1 carries no such path:

- **The cluster half** reaches the cluster through the renderer and `promote`, because the
  renderer reads `ansible/inventory/` and `ansible/templates/`.
- **The Pi half** stays a hand deploy with `-e target=daniel-pi`, as `log_pi_changes` records
  at the baseline.
- **The setup plane** is unchanged. No setup role reads an inventory path through the tick at
  the baseline, so an inventory key that a setup role reads needs a named hand apply, unless
  the same PR changes a file of that role. D6.5a changes the key and a test, and no file of
  `initial_setup`, so it names a hand apply of `--tags setup_drift` on daniel-box.

`ChangeSet.broad_deploy` stays, because `deploy_lib/tags.py` and `land_tags.py` read it for
the Pi. The tick fast-forwards past such a path the way it passes a docs-only one. It writes
no ledger line, because no apply is owed on this host. A deployer test asserts that an
`ansible/inventory/` change runs no `deploy.yml`.

### `land.sh`'s k8s tags after D6

`land.sh` hands `promote.py land-wait` the k8s tags it derived. The health gate and the
`source=promote` marker read the same list (D2 *How `land.sh` waits*). At `70591c307`
three sources fill that list, and D6 deletes the k8s half of each:

- **The path map.** `land_tags.derive` maps a changed path to its role's tag through
  `reach.Reach.tags`, which also reads `deploy_cross_role`'s two k8s lookups. D6.4 deletes
  those lookups.
- **The shared-role expansion.** `land_lib/classify.py` adds the callers of a shared role
  through `land_shared.shared_caller_tags`, which imports `shared_role_callers.py`. D6.3
  removes that import, and D6.4 deletes the module.
- **The diff fallback.** For a PR over `gh`'s 100-file page,
  `land_lib/deploy.py:derive_from_diff` runs `deploy_tags.py changed`. When `changed`
  refuses a broad range, `_narrowed` runs `deploy_tags.py narrow`, which is
  `narrow_lib/broad.py:narrow_cmd`. D6.3 narrows `changed` to the Pi and removes `narrow`.

A path under `ansible/templates/`, or a renderer-read key under `ansible/inventory/`, maps to
no tag in the first two sources. At `70591c307` the tick then applies `deploy.yml`, and
`land_tags.self_applied_playbooks` adds `ansible/deploy.yml` for `cs.broad_deploy`, so the
landing waits on the tick's receipt for it. From D6.1 the tick applies no `deploy.yml` for
such a path (*The route of a deploy-plane path after D6*). Its receipt never names
`ansible/deploy.yml`, so `land_lib/landing.py:Landing.tick_applied` would read False for
every such landing.

**From D2.3, `land-wait` reads the changed set, and from D6.1 that set is the whole k8s
list.** C1's `digests.json` records one `footprint` digest for each deploy tag and each
member of `SHARED_KINDS`. The renderer computes it in the render that `promote` publishes.
C6 has `land.sh` wait on C2's changed set between M's `digests.json` and the one on the
newest published ancestor of M's first parent, and D2.3's `land-wait` reads both files from
GHCR. D2 *How `land.sh` waits* owns how `land-wait` finds that ancestor and what each read
costs. A tag's footprint covers every file that C1 *Cluster output* publishes for it:
`cluster/<tag>/`, `cluster/<tag>-deferred/`, `cluster/<tag>-snapshot/`,
`cluster/<tag>-verify/` and `flux/<tag>.yaml`. That last file holds the Kustomization
`<tag>` and its suffixed kinds, with their `dependsOn`. This set is wider than the one the
gates count. D4's *What counts as changed* counts a tag only when its `cluster/<tag>/`
output differs, so an edit that moves only an edge, a deferred apply or a shared kind counts
nothing there. The kept arms keep their own count (*The render-diff gates after D6*).

- **What D6.1 changes.** Until D6.1, `land.sh` also derives k8s tags from paths, and a broad
  path waits on the tick's `deploy.yml` receipt. D6.1 removes both. Every landing then
  reaches `land-wait`, and its changed set is the list that the health gate and the
  `source=promote` marker read. `land.sh` derives no k8s tag itself (C6). D6.3 and D6.4
  delete the code behind the path-derived tags. If D2.3 calls `land-wait` only for a PR
  with a flux-owned tag, D6.1 removes that condition.
- **D3.4's interim rule retires in D6.1.** D3.4 adds an interim `land-wait` rule for the
  `cluster-vars` files, to retire in the PR that switches `land.sh` to reading
  `digests.json`. D6.1 is that PR, because from D6.1 the changed set alone fills the k8s
  list.
- **What a footprint counts.** C1 cites this bullet by its earlier name, *What the mode
  counts*. The renderer applies these rules when it writes `digests.json`, from D2's first
  publishing PR (C1). D2 owns that code, and the census that fails a member of
  `SHARED_KINDS` with no rule (C3). D6 keeps the rules as their definition. Every bundle
  path must classify.
    - **A tag's own files count for that tag.** The renderer strips `-deferred`,
      `-snapshot` or `-verify` from a directory name to find its tag. C3 *Reserved names*
      makes the strip unambiguous, because no deploy tag ends in a reserved suffix.
      pihole's second instance in `cluster/pihole-deferred/` therefore counts for
      `pihole`. An edit to one entry's `depends_on:` under `ansible/inventory/` moves only
      the dependent's `flux/<tag>.yaml`, and it changes only the dependent tag's footprint.
    - **`cluster-vars` counts for every substituting tag.** A substituting tag is one with a
      directory whose placeholder key set is non-empty, the set C1 calls *the one source*.
      Its `flux/<tag>.yaml` carries `spec.postBuild` exactly then, and D3.3b gives each
      such Kustomization the `cluster-vars` edge.
    - **`homelab-namespace` counts for every flux-owned tag.** It owns the Namespace and the
      LimitRange that admit every pod.
    - **`soak` counts for every flux-owned tag only when no tag's own files moved.** Its
      `dependsOn` names every flux-owned tag, so it also moves when a tag joins, leaves or is
      suspended, and that tag's own files then name the tag. `land-wait` reads `flux/soak`
      on every wait from D5.3b3 (contracts.md, *The soak in `land.sh`'s wait*). A PR that
      moves `soak` alone, such as an edit to the soak Job, widens, because the soak's verdict
      reads every tag's alerts.
    - **Index files count for no tag.** `flux/kustomization.yaml` lists the files under
      `flux/`, and it moves only when one of them joins or leaves. `images/` from D5.4a1
      sits where no Kustomization's path reaches. C1 names these files, and `digests.json`
      itself, as excluded from every footprint. A tag that joins or leaves moves its own
      `flux/<tag>.yaml`, so a landing that adds or retires a tag waits on that tag alone.
    - **Anything else widens.** A shared kind with no rule, or a path that no rule claims,
      lands under `_unclassified` (C1).
- **The `soak` rule cannot run inside one render.** Its condition compares two bundles, and
  the renderer sees one. The soak Job also embeds the commit it verifies (R4 *The
  render-diff gate*), so `soak`'s footprint differs on every commit. A rule that fanned
  `soak` out to the tags would therefore widen every landing. The rule's text stays as
  written, and Open question 15 holds it.
- **A tag that only the base renders is left out of the wait.** Its Kustomization no longer
  exists at M, so `land-wait` could never read it Ready. This rule belongs to the reader,
  not to the renderer. C2's changed set does not state it, so a cross-slice note asks C2 to
  leave such a name out of `verify`'s expected set too.
- **A role change that moves no bundle file leaves the list.** `promote` has nothing to
  apply for it. A PR that moves no bundle file waits on `flux/homelab` alone (C2, C6).
- **The reads cost no `api.github.com` call.** `land-wait` reads both files from ghcr.io
  through GHCR's anonymous pull token, as D2's `last-good` read does. That token works only
  while D1 Open question 2 keeps the package public. *Risks* names what a private package
  does to every landing.
- **The Pi half stays a hand deploy.** A broad path can also reach a Pi Compose role. At
  `70591c307` the tick's `log_pi_changes` records that half and does not gate a verdict on
  it. From D6.3, `derive_from_diff` does the same. `changed` prints its own line naming
  `deploy.sh -e target=daniel-pi`, in place of `broad_remediation`'s deploy-plane text,
  which names a full `ansible/deploy.yml` run. `derive_from_diff` prints that line
  beside the verdict, and the verdict rests on `land-wait`.
- **`self_applied_playbooks` loses its `deploy.yml` term in D6.1**, in the PR that stops
  the tick's apply. A setup-role change keeps its term, because the tick still applies it.
- **Rejected: every flux-owned tag for every broad PR.** It needs no read. But it gates
  the health checks of about 50 services on a one-template change, and it writes a marker
  naming every service. It stays as the failure fallback only.
- **Rejected: the `--moved-tags` mode.** An earlier draft of D6.1 rendered both commits on
  daniel-box, each in a `git archive` scratch tree with its own uv environment, in about
  240 lines. It cost two renders per landing, a scratch layout kept out of the deployer's
  checkout, and a second copy of the classification. C1's footprint classifies once, in the
  render that `promote` publishes, so the reader cannot disagree with what `promote`
  applies.
    - **No other reader needs the scratch layout.** D4 Open question 12 asked whether the
      layout function survives. It does not, and D6.1 adds none. The PR-time render-diff
      gates run in CI, where the runner's checkout and a worktree of the merge base hold
      the two trees and no deployer reads the checkout. R4's `render-diff` workflow lays
      out its base the same way.
- **Rejected: read the CI render-diff job's result with `gh`.** That costs an artifact
  download per landing, and the job runs on the PR head, not on the merge commit.
  `digests.json` on the published artifact describes the merge commit itself.

### The host-plane pages after D6.1

D6.1 deletes `deploy_k8s_owed.py` and `deploy_broad_k8s.py`, which carry the only path to
two host-plane pages (Baseline). So D6.1 first moves those calls into `deploy_handlers.py`:

- **`alert_deferred` loses its k8s arm.** It keeps the tasks channel. The k8s arm, the
  `declared_k8s` parameter and `deploy_alert_text.k8s_deferred_alert` go. That arm reads
  `cs.k8s_consumers`, which D6.1 deletes. Its condition, `cs.k8s`, is empty after D5 for
  every role with an entry (*What D2, D4 and D5 hand to D6*).
- **`deploy_remediation.k8s_remediation` stays until D6.4.** Two scripts outside the
  deployer call it. `land_lib/land_tags.py` imports it at module level from
  `deployer_lib.deploy_logic`, and `land_tags.py:plane_note` calls it for its shared-role
  note. `deploy_lib/tags.py:_load_deploy_logic` imports it lazily, and
  `deploy_lib/tags.py:changed` calls it. That is the path behind `deploy.sh --changed`
  and behind `land_lib/deploy.py:derive_from_diff`. D6.3 removes both calls (*`land.sh`'s
  k8s tags after D6*). D6.4 then deletes the function and its `deploy_logic.py`
  re-export, with the `deploy_cross_role.py` k8s symbols that the function reads.
- **The ledger write goes with `alert_and_record_deferred`.** No apply is owed on this host
  for a k8s role, so nothing replaces it.
- **Three call sites call `deploy_alerts.alert_deferred` directly**, with `deployed=set()`:
    - `handle_no_services`, after its `alert_secrets_deferred` call, as at the baseline;
    - `handle_broad`'s failure arm, in place of `alert_and_record_deferred` over
      `deploy_broad_k8s.fold_deferred`. Its `alert_secrets_deferred` call stays;
    - a new tail of `handle_broad`, after the plan loop, in place of `return
      deploy_broad_k8s.apply_broad_k8s(...)`. The tail fires the tasks page and then
      `alert_secrets_deferred`, and returns 0.
- **The contention arms still send nothing.** The `DECIDED` in `handle_broad`'s failure arm
  (#2459) keeps the secrets page off the arm that resets the tree. The new tail is reached
  only with `local == origin`, as `apply_broad_k8s`'s last exit was.
- **The annotation goes.** `emit_deploy_annotation` annotated only k8s services (Baseline).
  After D6.1 neither of its callers, `handle_k8s` and `apply_broad_k8s`, exists. So D6.1
  deletes `deploy_io.emit_deploy_annotation`, its `deploy_toolbox` field, and the deployer
  half of `test_deploy_annotations.py`. *Grafana's deploy markers after D6* says what the
  stream holds afterwards.
- **Rejected: keep `alert_and_record_deferred` in a kept module.** Its ledger half writes
  `k8s_unapplied`, which D6.1 stops writing. What remains of it is a call to
  `alert_deferred`.

### Grafana's deploy markers after D6

**After D6, two sources mark a cluster deploy.** `promote.py land-wait` writes a line for
every landing, and from D6.1b a query draws a marker from kustomize-controller's own log
line for every apply. At the baseline the dashboards draw a marker only for a
`deploy-annotation` line in host syslog (Baseline). D6 deletes one emitter that no k8s role
reaches any more, and keeps the one that replaced it. D6 owns the `deploy-annotation`
stream from D6.1 on, as D2 *Risks* hands it over, and D6.1b closes the gap below.

- **The Ansible emitters stop role by role, at each flip, not at D6.1.** C4 makes the
  deployer's k8s arms and `deploy.sh` skip a flux-owned entry. D2's Baseline records that
  neither emitter runs for bento-pdf after D2.7, and after D5's last wave neither runs for
  any k8s role. The emitter D6.1 deletes therefore has no reachable k8s service by the time
  D6 starts.
- **`land-wait` is the cluster's emitter from D2.3 on.** When a landing settles,
  `promote.py land-wait` writes one `logger -t deploy-annotation` line with
  `event=deploy`, the PR's published tags, the Ready revision and `source=promote`. It runs
  inside `land.sh` on the host, so the line takes the same syslog path as
  `playbook.py:annotate`. `deploy-failed` and `gave-up` write none (D2 *Design*). D6 keeps
  `promote.py` and the promotion wait. D6.3 deletes the Ansible deploy arm of `land_lib`,
  and the wait and its annotation stay.
- **`deploy.sh`'s emitter covers the Pi only.** After D6.3, `deploy.sh` refuses every k8s
  tag, so `deploy_lib/playbook.py:annotate` fires only for a Pi deploy. After H5, no
  `deploy.sh -e target=daniel-pi` runs, and H7.19b deletes `playbook.py`. `land-wait` is
  then the stream's one emitter.
- **The gap D2 records closes with a query, not an emitter.** A promotion that no
  `land-wait` follows draws no `deploy-annotation` line. An automerged bump, a merge made
  outside `land.sh`, and a promotion after the operator clears `DEPLOY_HOLD` with no re-run
  are three such promotions (D2 *Risks*). The `event=landing` line does not close the gap,
  because it marks the merge rather than the apply and only the Landings board reads it
  (Baseline). D6.1b adds a second annotation query beside
  `observability_deploy_annotation_expr`, and `scripts/grafana/inject_dashboard_annotations.py`
  adds both to every dashboard.
    - **The source is kustomize-controller's own log.** loki-homelab's Alloy DaemonSet ships
      every pod's logs, `flux-system`'s included. The query matches the line that
      kustomize-controller writes for an apply that changed objects, which names the
      Kustomization and its revision. It takes its `job` label from `loki_streams`, as
      every selector does.
    - **One marker per apply, not per reconcile.** kustomize-controller reconciles each
      Kustomization on its interval and logs each run. The query matches only the line
      that reports a changed object, so a reconcile that changed nothing draws no marker.
    - **The exact line is upstream's.** D6.1b measures it on production with `probe.py
      loki-query` before it writes the matcher. It pins one captured apply line and one
      captured no-change line as the test's fixtures.
    - **A landing draws both markers.** `land-wait`'s line names the PR's changed set, and
      the controller query draws one marker per Kustomization the promotion changed.
      `land-wait`'s line stays, because only it names the PR.
    - **No write credential.** The query reads Loki, as the existing one does, so it needs
      no Grafana API call and no standing write credential.
    - **Fallback: `gotk_resource_info`'s `revision` label.** C6 states that every promotion
      adds one series per Kustomization through that label. If no kustomize-controller
      line tells an apply from a no-change reconcile, D6.1b draws the marker from each
      series' first sample through the Prometheus data source instead.
- **Rejected: an emitter in `promote` itself.** `promote` runs on a GitHub-hosted runner,
  so a `logger` call there writes to the runner's syslog, which no Alloy ships (Baseline).
  A POST to Grafana's HTTP API instead would need the standing write credential that the
  comment above `observability_deploy_annotation_expr` records the reason to avoid. D2
  refutes the same option in its own review log.
- **D6.1 rewrites two texts that name the deleted emitter.** The comment above
  `observability_deploy_annotation_expr` names `deployer_lib/deploy_io.py` and "the
  emit_deploy_annotation comments". The rewrite names `deploy.sh`'s emitter, for the Pi,
  and `promote.py land-wait`, for the cluster. The change is a comment, so the
  observability render does not move. `test_deploy_annotations.py`'s docstring draws the
  chain from `deploy.sh / gitops_deploy.py`, and its rewrite names the same two emitters.
  `test_deploy_annotations.py:test_both_deploy_paths_annotate` and
  `test_the_query_matches_what_the_deployers_actually_log` drop `deploy_io.py` and keep
  `deploy.sh` and `land-wait`, which D2.3 adds to both.

### D2's ownership readers after D6

After D5, every k8s entry is flux-owned, and D6.3's census refuses a commit that drops
`owner: flux`. Each D2 reader therefore goes with the arm it guards, or stays until H1.9
removes `owner`:

- **`ChangeSet.flux` stays until H7, and the deployer stops reading `owner`.** D6.4 deletes
  `ChangeSet.k8s`. The `at.plane == "k8s"` branch of `services_from_changed_paths` then
  adds every role to `cs.flux`, and D6.4 deletes `deploy_inventory.service_owners`. Without
  a destination, a k8s path would fall into the next branch, which handles setup paths. D2's
  drops in `deploy_narrow.py` and `deploy_k8s_owed.py` go with that code in D6.1.
- **`tag_owners` readers that go in D6:**
    - D6.3: `deploy_lib/tags.py` refuses every k8s tag by its entry's platform, so its exit
      5 no longer reads `tag_owners`. `land_lib/deploy.py`'s split goes with `land_lib`'s
      k8s deploy arm, because every k8s tag waits on promotion;
    - D6.4: `smoke_caller`, with `shared_role_callers.py`;
    - D6.5b: `probe_lib/health.py`'s skip, with the release-record branch, and
      `probe_lib/releases.py`'s skip, with the module.
- **`tag_owners` readers that stay until H1.9:** `secret_rotation.py --deploy`,
  `gen_role_glance.py`, `service_catalog.py`, D6.3's census row and `land_lib/merge.py`'s
  handback refusal. The census refuses the handback in CI first, so the merge-time refusal
  is a second guard for the same commit. It reads one Kustomization only for a PR that makes
  a tag Ansible-owned, and H1.9 deletes it with `owner`.
- **The glance blocks keep D2's Flux line.** D6.2b does not put the Renovate stance back.
  The role's `defaults/main.yml` comment beside `k8s_autodeploy` states it, D6.2b rewords
  that comment, and `renovate.json`'s derived rule is its reader. A second statement in 57
  generated blocks would restore the line D2 replaced on purpose.

### The render-diff gates after D6

D6.1 deletes one of the four arms of `cross_plane_order.py`, not the module.

- **Arm 1 goes.** Its subject is an edge between a flux-owned and an Ansible-owned role, and
  after D5 every k8s role is flux-owned. It goes with its use of
  `deploy_k8s.py:is_image_only_diff`, which D6.1 deletes.
- **Three subjects outlive D5, so their arms stay.**
    - Arm 2 compares two flux-owned tags.
    - D5.1d's gate keeps a suspended role still. `flux_suspend` stays the way to stop a role
      (*The `k8s_autodeploy` key keeps its Renovate readers*). Without the gate, a change to a
      suspended tag merges and then trips `decide`'s `suspended-changed` hold, which stops all
      promotion.
    - D5.1f's dropped-object arm is the only PR-time check that a prune is meant. Its
      retirement arm stays until R4 deletes `k8s_retired_services` with the play.
- **The kept arms count a tag whenever its render moves.** The image-only exception goes with
  arm 1. An image bump to a suspended tag is refused like any other change, which D5.1d's rule
  needs anyway. Arm 2 now also refuses an image bump that lands with a new edge on the same
  tag, and the author lands the edge first, as arm 2's message already says.
- **The module is renamed for what it checks.** After D6.1 it orders no planes, so D6.1 renames
  it to `scripts/validate/render_diff_gates.py`, with its tests in `scripts/validate/tests/`
  and its CI step. The step keeps its pull-request-only condition.
- **D6.7b changes the dropped-object arm twice.** It deletes the handback exemption,
  because D6.3's census refuses a handback, and from D6.7 the default policy deletes what the
  exemption assumed `Orphan` would keep. It also counts a tag that leaves the render whole,
  judged by the tag's ownership at the base. A retirement PR therefore carries one
  `Removes-Object:` trailer per object it deletes. D6.7b adds the tag rule's pull-request arm to the same module.

### The `k8s_autodeploy` key keeps its Renovate readers

After D6 the key means only one thing: Renovate may automerge an image bump for that role,
and the merge deploys through `promote`. The key keeps its name. Renaming it in 62 defaults
and in `renovate.json` would hide the deletion inside a rename.

- D6.2a moves `is_leftover_dir` into `scripts/lib/k8s_roles.py`, so one definition remains.
- D6.2a moves the denylist derivation (`k8s_autodeploy_denylist` and `SHARED_ROLES`) into the
  same module, and repoints `ansible/tests/deploy/_autodeploy.py` and
  `scripts/dev/k8s_autodeploy_counts.py` at it.
- The Renovate readers keep their meaning. `renovate.json`, `pending_logic.py` and
  `land_renovate_pr.py:DENIED_SLUG` stay as they are.
- **The lander changes its source, not its meaning.** D6.2a deletes the `config.env` line
  that `land_renovate_pr.py:denied_roles` reads, so the same PR repoints the lander.
    - The lander runs `uv run python scripts/dev/k8s_autodeploy_counts.py --denylist` in
      `RENOVATE_AGENT_REPO_DIR`, the checkout from which it already runs `land.sh`. The new
      flag prints the moved derivation's list, comma-separated. So the lander still adds no
      parser of its own, and renovate.json's rule and the lander read one derivation.
    - Fail-closed stays. A non-zero exit, a timeout or empty output refuses the landing, as a
      missing `config.env` line does at `70591c307`. The derivation already raises rather
      than return an empty list.
    - The checkout's list is as fresh as the rendered one was. The tick fast-forwards that
      checkout before it applies anything, and `reconcile_denylist` re-rendered `config.env`
      on the same tick.
    - **Rejected: a list that renovate_agent renders into its own env file.** A role that
      flips `k8s_autodeploy` changes no renovate_agent path, so nothing would re-render that
      list, and the lander would land a newly denied role's PR. That staleness is what
      `reconcile_denylist` exists to repair for `config.env`. The option would also need the
      filter plugin that D6.2a deletes.
    - **Rejected: importing `scripts/lib/k8s_roles.py` into the lander.** The lander's
      interpreter has only its own directory on `sys.path`, and `k8s_roles.py` imports `yaml`
      and `lib.*`.
    - **Rejected: keeping the `config.env` line as a shim.** H7 deletes `config.env`, so the
      shim would move the same break to H7.
- D6.2b changes the docs readers' wording from "Auto-deploy" to the Renovate meaning. Those
  readers are `gen_role_glance.py`, `catalog_backup.py`, `service_catalog.py`,
  `fragment_renderers.py`, the scaffold, and the two skills.
- `estate.py` and `render_context.py` read the key's presence, not the deployer's use, so
  they need no change. The PR body quotes the grep that confirms this.
- A role stops through D5's `flux_suspend`.

### The setup-drift reader covers daniel-box

Arms 2 and 3 of manifest-prune-check are host-plane drift detectors for daniel-box. D6.5a adds
daniel-box to `setup_drift_check_hosts` before D6.5 disarms manifest-prune-check.

- `setup-drift-check.sh` reads the same `setup-drift-lib.sh` against the same stamp
  directories. The box keeps one detector throughout.
- **By D6, setup drift is a textfile check, not a Kuma tile.** D6 opens only after M6's
  C-2b deletes Kuma (M6 *When D5 flips a role M6 edits*). M6's B2 has by then made
  `setup-drift-check.sh` write `setup-drift-check.prom` on each armed host, and given it
  M3's two rules, `<Check>Failed` and `<Check>NotReporting`. Each rule names one `origin`.
  B2 ran while daniel-server was the only armed host, so its rules name daniel-server
  alone. B2's token PR retired `setup_drift_push_token`.
- **The series is per host, so one host cannot mask the other.** The `node` job labels each
  host's series with its own `origin` (M3 *The producer metric contract*). The shared tile
  and token that D6's first drafts worked around are gone. A rule that dropped its `origin`
  matcher would bring the masking back, because daniel-server's sample would satisfy it
  while the box wrote none.
- **D6.5a arms the reader, and D6.5 adds the box's rule arm.** A `NotReporting` arm for
  daniel-box fires as soon as `promote` applies it, unless the box has already written a
  sample inside the deadline, and the timer runs once a day. So D6.5a arms the box and
  checks its series, and D6.5 adds the box's arm once that series exists. Between the two,
  B1's manifest-prune rules still page drift on the box, so the box keeps one paging
  detector throughout.
- Both checkers run on the box between D6.5a and D6.5. The `group_vars` comment calls that
  redundancy "the safe direction".

### The deletion policy, and the end of the handback

D2's open question 3 asked D6 to choose the deletion policy from its own count of handbacks
and retirements. D2 to D5 run the per-tag Kustomizations with `deletionPolicy: Orphan`.

**After D6.1 and D6.3, the handback cannot work under either policy.** A handback removes
`owner: flux`, and by then no Ansible path applies a k8s tag. A reverted D5 role PR is the
same handback. So D6 retires the handback as a rollback, and D6.3 adds a census row that
refuses a k8s `containers_list` entry without `owner: flux`.

**D6.7 then switches each `<tag>` Kustomization except `flux` to the default deletion
policy, which follows `prune`.** Removing an entry from `containers_list` then deletes the
service's objects, which is how a service retires. The `k8s_retired_services` teardown in the
play has no job left, and R4 deletes it with the play.

**Every other Kustomization keeps `deletionPolicy: Orphan`.**

- **`flux`.** Its inventory holds the Flux CRDs (*What D2, D4 and D5 hand to D6*). Deleting
  the `flux` Kustomization under the default policy deletes those CRDs, and Kubernetes then
  deletes every Kustomization and OCIRepository with them. Whether kustomize-controller runs
  those finalizers before its own pod goes depends on timing. If it does, each finalizer
  deletes its tag's workloads. So `flux` keeps `Orphan`, and the tag rule below never lets it
  leave the bundle.
- **Every `<tag>-deferred`, `flux-deferred` among them.**
    - `flux-deferred` holds the OCIRepository `homelab` and the root. Deleting them under the
      default policy cuts Flux off from its source, and the way back is the **operator-only**
      recovery runbook.
    - A `-deferred` also leaves the bundle while its tag stays, when a refactor stops a role
      deferring a file. Examples are the flux role dropping `sync.yaml.j2` from
      `manifests_deferred_files`, netpol-baseline moving its observability NetworkPolicies
      back, or pihole moving instance 2's Deployment back
      (`ansible/roles/k8s/pihole/tasks/main.yml`).
      Under the default policy, the `-deferred` finalizer would race `<tag>`'s adoption of the
      same objects. A deleted allow policy opens a deny window, which breaks policies-first
      ordering, and a deleted pihole Deployment is a DNS outage. Under `Orphan`, `<tag>` adopts
      them with no gap.
    - The cost: a retired tag with deferred files leaves its deferred objects behind.
      `docs/flux-deploy-plane.md`'s retirement procedure removes them first, by emptying the
      deferred templates in a PR of their own, so the `-deferred` inventory prune deletes
      them while its Kustomization still exists.
- **Every shared kind: `cluster-vars`, `soak` and `homelab-namespace`.** C3 names them in
  the renderer's `SHARED_KINDS`. `cluster-vars` holds the Secret `homelab-cluster-vars`, which
  every substituting tag reads. D3's rollback of D3.3a relies on `Orphan` to leave that
  Secret in place when the root prunes the `cluster-vars` Kustomization. Under the default
  policy, a `cluster-vars` that left the bundle would delete the Secret, and every
  substituting tag would then fail to build. C3 gives Secrets no prune exemption.
- **The root `homelab`, `<tag>-snapshot` and `<tag>-verify`.** The last two hold only Jobs,
  which D5.1a3's TTL census already expires.

**The switched set is defined positively, and the kept set is everything else.** D6.7
switches the per-tag `<tag>` Kustomizations, one per deploy tag, except `flux`. Every other
Kustomization the renderer emits keeps `Orphan`. A later shared kind therefore keeps `Orphan`
without an edit to this list.

D5.1a1's renderer test fails on any Kustomization without `Orphan`. D6.7 narrows it to the
kept set. The test iterates the root, every `SHARED_KINDS` member, every suffixed kind and
`flux`, and never an enumerated list. Its named members are `flux`, `flux-deferred` and
`cluster-vars`, so a test that finds none of them fails. D6.7 adds the inverse: a `<tag>`
other than `flux` that still carries `Orphan` fails. C3's reserved-names rule keeps a deploy
tag from equalling a `SHARED_KINDS` member, so the inverse test can take its tags from the
deploy-tag set.

**What the default policy may not delete.**

- **PVCs and Namespaces** keep `prune: disabled`, so a deleted Kustomization leaves its data.
  Under `Orphan`, only the inventory prune reads that marker, when an object leaves a tag's
  render. The default policy adds a second deleting path, the Kustomization's finalizer.
  D6.7a proves that both paths honour the marker before D6.7 merges.
- **PersistentVolumes and StorageClasses join the exemption.** media-volume renders a static
  PersistentVolume that seven roles mount (*What D2, D4 and D5 hand to D6*). Under the
  default policy, deleting the `media-volume` Kustomization, by a rename or a render bug, deletes that
  PersistentVolume. `pv-protection` keeps a bound volume terminating, and a re-apply does not
  bring back a terminating object. So the mounters lose the volume at their next restart.
  D6.7b stamps the prune marker on every PersistentVolume and StorageClass the renderer
  emits, once the C3 amendment in Open question 12 lands, which also names the fallback. A
  renderer test pins the marker on media-volume's PersistentVolume.
- **No CRD sits in a tag the default policy can delete.** No k8s role renders a CRD at
  `70591c307`, H1's plumbing tags render `prune: false`, and `flux` keeps `Orphan`. D6.7b's
  CRD guard, a renderer test, fails on a CustomResourceDefinition in any other tag that
  renders `prune: true`. A later role that ships CRDs therefore sets C3's per-role
  `prune: false` first.

**Three paths still drop a tag by accident.**

- **A render bug.** D2's publish census fails when a flux-owned entry renders no
  `flux/<tag>.yaml`.
- **A handback or a D5 revert.** D6.3's census row refuses it.
- **A tag rename.** It is a retirement plus an addition. The old Kustomization's deletion
  removes the objects that the new one applies under the same names, and the new one
  re-creates them at its next reconcile, so the service is down in between. The prune-exempt
  kinds are never deleted, and the new Kustomization adopts them. `docs/flux-deploy-plane.md`
  states the rename procedure, and it refuses a rename of `flux`.

**Two checks guard all three paths.**

- **The tag rule.** One function takes the tags of a reference bundle, the tags of the head
  render and the role directories at the head. It returns every tag in the reference that
  the head lacks while its role directory stays, and `flux` whenever the head lacks it.
    - **Its guard runs in the publish job**, beside the publish census in `promote.py
      check-bundle`. The reference is the bundle at `prod`, which the job pulls from GHCR,
      never a re-render of the parent commit. `prod` moves only on promotion, so a refusal
      persists until the tag returns or its role directory goes. A later commit with an
      unrelated change still fails, and a failed pull fails the publish, with nothing pushed.
    - **Its early warning runs on pull requests** in `render_diff_gates.py`, which already
      renders the PR's base. The reference there is the base render. A PR whose checks ran on
      an old base can still merge, and the publish arm catches it.
- **The dropped-object arm.** From D6.7b it counts a tag that leaves the render whole (*The
  render-diff gates after D6*). A retirement must name each object it deletes in a
  `Removes-Object:` trailer, so an accidental drop fails at pull-request time.

D6.7's PR body records the count of handbacks, retirements and renames from D2 to D5 with
`git log -S 'owner: flux' -- ansible/inventory/host_vars/`. If any handback in that count was
a live rollback that the census would now refuse, the operator decides in that PR's review
whether D6.7 merges or D6 ends with `Orphan` and the two-step retirement.

### The deletion drill runs in a throwaway kind cluster

No stage cluster exists. `docs/archive/staging-cluster.md` records that the stage cluster
was retired on 2026-09-29 (#2941). `scripts/lib/kubectl.py:RETIRED_CLUSTER_NODES` keeps
its node name only so that a `--cluster stage` request refuses. The behaviour D6.7 relies on
belongs to kustomize-controller and the renderer, not to k3s or Longhorn. A throwaway kind
cluster on a CI runner can therefore prove it before D6.7 merges, with no production blast
radius.

D6.7a adds one CI job, `flux-deletion-drill`, in its own workflow under `.github/workflows/`.
Its drill script sits beside the renderer in `scripts/renderer/`.

- **Where it runs.** The job runs on a GitHub-hosted runner. It creates a kind cluster at
  production's Kubernetes minor, which D1 measured as `v1.37.1+k3s1`. If kind publishes no
  node image for that minor yet, the job uses the newest one, and the PR body names the gap.
  A `registry:2` container on the kind network serves the bundle. The flux CLI installs
  Flux at `flux_version`, read from the flux role's defaults as D1's CI jobs read it.
- **What it never touches.** The job holds no secret and no age key (C3). It never pushes
  to `ghcr.io/danielh2018/homelab-manifests`, and it never reaches a homelab host.
- **What it renders.** A fixture tree in `scripts/renderer/tests/` holds three fixture tags,
  as plain manifests. Each PVC names no StorageClass, so kind's default class serves it.
    - `prune-drill` and `prune-drill-red` have the same shape. Each holds its own Namespace,
      a PVC and a Deployment of a pinned pause image that mounts the PVC.
    - `prune-drill-keep` holds its own Namespace, a Deployment that mounts the PVC
      `keep-marked`, and a second PVC, `keep-unmarked`, that a second pause Deployment,
      `keep-unmarked-holder`, mounts.
    - **Every fixture PVC has a pod that mounts it.** kind's default class, `standard`,
      binds a PVC only once a pod that mounts it is scheduled (`WaitForFirstConsumer`).
      Flux's health check counts a PVC as ready only when it is `Bound`, and C3's per-tag
      Kustomizations set `wait: true`. An unmounted PVC would leave its tag not Ready, and
      revision 1 would time out on every run. This is upstream behaviour that this plan did
      not reproduce. The revision-1 readiness wait checks it on every run, and D6.7a's PR
      body quotes that wait.
    - **Rejected: a drill StorageClass that binds `Immediate`.** Whether kind's local-path
      provisioner provisions a volume with no selected node is unverified, and a consumer
      Deployment needs no new object kind.
    - The drill does not run the `homelab-render cluster` CLI. C1 gives that CLI no way to
      read a tree other than the repo's, and a render of the repo would put production roles
      into kind.
    - Instead, the drill builds the bundle through the renderer's own library, as D2's
      fixture-tag tests do. It passes the fixture manifests through the function that stamps
      the prune marker on every PVC and Namespace, and it writes each tag's Kustomization and
      the root's `resources` with the function that writes them for production.
    - So the renderer writes the prune marker and the `deletionPolicy`, not the fixture. The
      drill therefore also proves that the form the renderer writes is one that Flux
      honours. D5 calls the marker a label and C3 says only that the objects carry it, and a
      green drill shows which form works.
    - If R1, D2 and D5 leave either step inline in the CLI path, D6.7a moves it into a
      function first, within its 210 lines (Open question 11).
- **Its source is not production's.** The drill writes its own OCIRepository `homelab`: the
  local registry's URL, `insecure: true`, and no `spec.verify`. It never reuses the flux
  role's sync objects, which D5's flux PR pins to D1's URL and cosign identity. A drill
  bundle cannot carry that signature, because only the `promote` workflow on master holds
  the identity. Dropping verification does not weaken the proof. Verification decides only
  whether source-controller accepts an artifact, and the three deleting paths the drill
  exercises run after acceptance and never read the signature: the root's prune of a dropped
  Kustomization, that Kustomization's finalizer, and the inventory prune.
- **How it removes objects.** The drill pushes the bundle as revision 1. It applies the root
  `homelab` Kustomization in the shape the bootstrap writes, against its own source, and waits
  for all three tags to be Ready. It then pushes revision 2, built the same way:
    - without `prune-drill`'s and `prune-drill-red`'s `flux/<tag>.yaml`, so the root prunes
      those Kustomizations as it does on production;
    - with `prune-drill-keep` still present and its two Deployments unchanged, but with both
      its PVCs gone from its render, so its inventory prune acts on them.

  The drill never deletes a Kustomization or an object by hand.
- **Three cases in every run.**
    - **As rendered, on `prune-drill`.** The expectation follows the policy that the
      rendered Kustomization declares. Under `Orphan` the Deployment stays, and under the
      default it goes. Under both, the PVC and the Namespace stay.
    - **The red half, on `prune-drill-red`.** Before revision 1, the drill makes two edits
      to that tag's rendered output: it strips the prune marker from the PVC, and it strips
      `deletionPolicy` from the Kustomization. The drill must find that PVC deleted or
      terminating, and the job fails if it does not. This case proves that the assertion can
      see a lost PVC, whatever policy the renderer writes.
    - **The inventory prune, on `prune-drill-keep`.** `keep-marked` must survive with no
      deletion timestamp. Its red half is `keep-unmarked`, whose prune marker the drill strips
      before revision 1: it must be gone or terminating. `keep-unmarked-holder` still mounts
      it, so `pvc-protection` holds it terminating, with a deletion timestamp. This is the
      path that runs under either policy whenever an object leaves a tag that stays, so
      D6.7a's baseline run under `Orphan` already tests the marker's form here.
- **What it asserts.**
    - The drill waits until each removed tag's Kustomization object is gone, and until
      `prune-drill-keep` is Ready at revision 2.
    - Under the default policy, it also waits until `prune-drill`'s Deployment and pods are
      gone.
    - It then reads each PVC and Namespace. A kept object must exist with an empty
      `.metadata.deletionTimestamp`.
    - The PVC's phase is not the check. While a pod still mounts a deleted PVC,
      `pvc-protection` holds the PVC with a deletion timestamp, and its phase still reads
      `Bound` (failure class 2).
    - The log prints one `case=as-rendered`, one `case=red-half` and one
      `case=inventory-prune` line, each with its verdict.
- **When it runs.** On a pull request it scopes itself, as image-smoke does. The drill runs
  only when the diff touches `scripts/renderer/`, the fixture tree, the flux role's defaults
  or the workflow itself. Otherwise the job is a fast green that prints `skipped`. A
  `flux_version` bump touches the flux role's defaults, so its PR runs the drill, and that
  bump is when upstream deletion behaviour could change. The job also takes
  `workflow_dispatch`, and a dispatch always runs the drill. It never self-scopes, unlike
  image-smoke's dispatch, which skips when it names no images. From D6.7c the scope also
  covers `docs/break-glass.md` and the flux role's OCIRepository template (*Break-glass
  promotion*).
- **What it does not cover.** kind is not k3s, and its default class is not Longhorn. The
  Longhorn half follows from the PVC object. Both Longhorn classes set
  `reclaimPolicy: "Delete"` (`ansible/roles/setup/k3s/files/longhorn-storageclass.yaml` and
  `longhorn-storageclass-nobackup.yaml`), so a volume goes only when its PVC is deleted, and
  the drill proves that the PVC is not. *Risks* names this limit.

### Break-glass promotion

C2's normal path needs GitHub, GHCR and Sigstore at once. From D5's last flip, C4 makes
`deploy.sh` skip every flux-owned entry, and D6.3 deletes its k8s arm. So no local path
writes the cluster while any of the three is down. D6.7c adds C2's break-glass promotion,
which is **operator-only**, and drills it on kind.

- **The key pair.** The operator generates a cosign key pair for break-glass alone
  (**operator-only**). The private half is password-protected and lives in the break-glass
  kit beside item 8's repo bundle, never in the repo, in CI or on a host. The public half is
  committed: the flux role carries it as a Secret in `flux-system` that holds that public
  half alone. Normal operation reads it nowhere, because the production OCIRepository
  verifies keyless against the `promote` identity alone (D1).
- **The source form.** The flux role's OCIRepository template takes a second form of its
  source settings: a registry URL and the break-glass Secret, in place of GHCR and the
  keyless identity. The default stays GHCR, and a renderer test pins the production render
  unchanged.
- **The runbook** is a scenario in `docs/break-glass.md`, *GitHub, GHCR or Sigstore is
  unreachable*. The operator:
    1. clones item 8's repo bundle and commits the change to apply on top of it;
    1. commits the break-glass source form, which names the in-cluster registry;
    1. checks that the commit to render equals or descends from the commit that the
       cluster's OCIRepository status names in `org.opencontainers.image.revision`. Item
       8's bundle is refreshed only with the age-key backup, so it can predate `prod`. A
       render from an older tree would prune every tag added since and revert every change
       since. If the bundle fails the check, the operator renders from a surviving host
       checkout that holds that commit;
    1. renders with `homelab-render cluster` in placeholder mode, as the `render` job does;
    1. pushes the bundle to the in-cluster registry with the pinned `flux` CLI, and signs
       it with `cosign sign --key` and the kit's private half;
    1. applies the rendered OCIRepository by hand. The bundle it serves carries the same
       source, so whichever Kustomization applies the OCIRepository keeps that source
       rather than reverting it.
- **A fresh or rebuilt cluster needs two more steps.** When H7's bring-up runs offline, or
  the cluster was rebuilt, it holds neither the in-cluster registry nor the Flux
  controllers. The runbook then applies
  the in-cluster registry before the artifact push. It installs the Flux controllers from
  images that the break-glass kit carries, not from GHCR. Open question 17 holds how the
  kit carries them, beside H7 Open question 14.
- **What fires meanwhile.** `ProdMovedOutsidePromote` fires, because `prod`'s live digest no
  longer matches `PROD_DIGEST` (C2), and the runbook says so. The break-glass path neither
  reads nor clears `DEPLOY_HOLD`, which needs GitHub.
- **The way back to GHCR.** Re-pointing at GHCR first would apply `prod`, which predates the
  emergency change, and silently revert it. So the runbook orders the steps:
    1. push the break-glass commits to master, with the source form reverted;
    1. wait until `promote` verifies a commit that contains them and moves `prod` there
       (C2);
    1. apply `prod`'s OCIRepository by hand, as the flux role renders it;
    1. confirm that every Kustomization is Ready at `prod`'s origin revision and that
       `ProdMovedOutsidePromote` has cleared.
- **The drill.** D6.7c extends `flux-deletion-drill` rather than adding a second job. That
  job already serves its bundle from a `registry:2` through its own OCIRepository. Two
  cases join it:
    - `case=break-glass`: the job generates a throwaway key pair, stores the public half as
      the drill's Secret, signs a bundle with the private half, and points the
      OCIRepository at it with `spec.verify`. The tag must reach Ready at that revision.
      The kit's private half never reaches CI.
    - `case=break-glass-red`: a bundle signed with a second throwaway key that the cluster
      does not hold. source-controller must refuse it, and the OCIRepository must not
      reach Ready at that revision.
- **The in-cluster registry stays in service.** Break-glass promotion is a standing
  consumer, which changes Open question 3's answer. Its precondition is D5 Open question 2,
  which rules whether the registry stays as a pull-through mirror before nut's Wave C PR.
  If that ruling retires the registry, D6.7c stops and this design picks another push
  target.

### The ADR

The ADR states what was decided and what it cost:

- CI renders with `homelab-render`, Flux applies an OCI artifact, and `promote` gates on
  commit statuses and the soak. Promotion holds on failure.
- **Context** carries ADR-0010's three premises and how each moved, citing the measurements
  in D1's and D3's baselines:
    - the deployer grew to 9,499 lines;
    - Flux decrypts age natively, through a cluster-only key;
    - daniel-box's memory was 47% when D1 measured it.
- **Consequences** come from what the migration paid, never from prediction:
    - the 40 `Recreate` rolls;
    - the stateful hold that no longer resets automatically;
    - the operator's step for every cluster secret;
    - GHCR as a dependency;
    - the end of the handback as a rollback.

  It names variables, not values, as `docs/adr/template.md` requires.
- **Every GitHub dependency, with its fallback.** The ADR lists each place the cluster
  deploy path needs GitHub, and what its absence stops:
    - the workflow triggers that run CI, `render`, `publish` and `promote`;
    - the repository variables `DEPLOY_HOLD` and `PROD_DIGEST`;
    - the check-runs that give `decide` its CI verdict;
    - the commit statuses that notification-controller posts and `verify` reads;
    - GitHub's OIDC token, on which cosign's keyless signature rests;
    - GHCR: the registry that serves the artifact, the anonymous pull that D1 Open
      question 2 keeps open, and the Packages API that D1.7's retention reads.

  Without any one of them, no tag moves and Flux keeps applying `prod`. The fallback for a
  change that cannot wait is C2's break-glass promotion (*Break-glass promotion*), which is
  **operator-only**.
- **Fixed premises.** The ADR names k3s and Flux as premises that the design builds on
  rather than abstracts over, and records what leaving each would cost. Leaving Flux means
  replacing C3's Kustomizations, C6's commit statuses and `revision` label, `promote`'s
  verification and D5's soak status. Leaving k3s means replacing the bring-up playbook, its
  bundled Traefik and its embedded network-policy controller, and every role that assumes
  them.
- **No tracing, by decision.** The ADR records that the deploy pipeline carries no traces.
  The deploy timeline comes from commit-status timestamps and the deploy-pipeline
  exporter's ages, as C7 *Traces* states, and `design.md` records why.

If D1's operator ruling produced a `Proposed` ADR, D6 completes and accepts that one rather
than writing a second.

### Tree diff

```diff
- ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_k8s.py
- ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_broad_k8s.py
- ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_k8s_owed.py
- ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_release.py
  ansible/roles/setup/gitops_deploy/files/deployer_lib/{deploy_changes,deploy_narrow,
    deploy_handlers,deploy_phases,deploy_state,deploy_config,deploy_alerts,...}.py
                                                    k8s halves removed
  ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_handlers.py
                                                    - handle_k8s; three direct alert_deferred
                                                      calls; a new handle_broad tail
  ansible/roles/setup/gitops_deploy/files/deployer_lib/{deploy_alert_text,deploy_remediation,
    deploy_io}.py                                   - k8s_deferred_alert, k8s_remediation,
                                                      emit_deploy_annotation
  ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_inventory.py
                                                    - service_owners; cs.flux takes every
                                                      k8s role path
  ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_cross_role.py
                                                    - K8S_ROLES_IMPORTING_SETUP_FILES,
                                                      k8s_lookup_readers
  ansible/roles/setup/gitops_deploy/files/{gitops_markers,gitops_ledger}.py
                                                    - the k8s ledger classes
  ansible/roles/setup/gitops_deploy/tasks/code.yml  copy loop shrinks; absent task grows
- ansible/filter_plugins/k8s_autodeploy.py
  scripts/lib/k8s_roles.py                          + is_leftover_dir, the denylist rule
- scripts/deploy_tools/{shared_role_callers,shared_role_reach,digest_provable}.py
  scripts/deploy_tools/reach.py                     k8s readings removed
  scripts/deploy_tools/land_lib/{classify,deploy,land_tags,land_shared}.py
                                                    - the path-derived k8s tags (D6.1);
                                                      - shared_caller_tags, _narrowed,
                                                      the k8s_remediation note (D6.3)
  scripts/deploy_tools/deploy_lib/tags.py           the Pi's tags only; - narrow (D6.3)
- scripts/deploy_tools/land_lib/land_rerolls.py
- scripts/deploy_tools/narrow_lib/{broad,containers}.py
- scripts/validate/cross_plane_order.py               renamed; arm 1 and the image-only exception go
+ scripts/validate/render_diff_gates.py               arm 2, D5.1d, D5.1f;
                                                      + the tag rule's PR arm (D6.7b)
  scripts/deploy_tools/promote.py                     + the tag rule's publish arm, against prod;
                                                      - D3.4's interim cluster-vars rule (D6.1);
                                                      - the object-loss check's handback
                                                      exemption (D6.7b)
  scripts/secrets_mgmt/consumers.py                   CROSS_HOST_PUSH_TOKENS - three tokens
  scripts/docs/{gen_role_glance,service_catalog}.py   - the Ansible-owned auto-deploy stance
- ansible/roles/setup/k3s/templates/{manifest-prune-check,release-staleness-check}.sh.j2
- ansible/roles/setup/k3s/templates/live-drift-check.env.j2
- ansible/roles/setup/k3s/files/{live_drift_check,manifest_declares}.py
- scripts/diagnostics/probe_lib/releases{,_render,_diff,_format,_consumers,_retired}.py
  scripts/diagnostics/probe_lib/health.py           - release_expected_restarts
  ansible/inventory/group_vars/all.yml              setup_drift_check_hosts + daniel-box
  scripts/renderer/                                 deletionPolicy default for <tag> except flux;
                                                    prune marker on PersistentVolume, StorageClass;
                                                    + the drill script and its prune-drill fixture tree
+ .github/workflows/flux-deletion-drill.yml         the kind deletion drill (D6.7a);
                                                    + two break-glass cases (D6.7c)
  ansible/roles/k8s/flux/                           + the break-glass public-key Secret and
                                                    source form (D6.7c)
  docs/break-glass.md                               item 8; + the GitHub-unreachable
                                                    scenario (D6.7c)
  ansible/roles/k8s/observability/defaults/main.yml + the kustomize-controller marker
                                                    query (D6.1b)
  scripts/grafana/inject_dashboard_annotations.py   adds both queries (D6.1b)
+ docs/adr/<next free>-the-cluster-deploys-from-a-ci-rendered-artifact-through-flux.md
  docs/adr/0010-pull-based-gitops-over-argo-and-flux.md   status: Superseded by ADR-NNNN
```

## Steps, as PRs

Each deletion PR removes one coherent subject and states the census rows it removes.

- **Size.** Line counts are approximate at `70591c307`, and each PR re-measures its own. A
  deletion PR may run past 200 lines, because review effort scales with what is kept, not
  what is removed. Open question 6 records this as a departure from the contract.
- **Tests.** Each PR deletes the tests whose subject it removes. It drops their `ENFORCED`
  citations in the same PR (`.claude/rules/python-layout.md`, *A check retires when its
  subject does*).
- **Failure classes.** A deleted test cited in `docs/failure-classes.md` forces an edit to
  that table, because `ansible/tests/repo/test_failure_class_detectors.py` fails until the
  edit is made.
- **Facts.** Each PR that edits a section recorded in `docs/facts.lock` runs
  `uv run python scripts/dev/fact_status.py verify '<doc>#<heading>'` for that section, and
  commits the lock. If G2 has retired the lock, this step drops.
- **The two-writer rule (C4).** No PR here changes ownership of a live object, so the rule has
  nothing to check. D6.3's census enforces the end state.

The PRs merge in this order.

1. **D6.0 — Preconditions** (no code). The executing session runs four checks and quotes
   each.
    - contracts.md's C1 or C7 names the label that carries an object's deploy tag. M2's
      Open question 1 gates D6 on this amendment, and the register lists it as *The label
      that names a deploy tag*. An unapplied amendment stops the session like any other
      failed check, and the contracts owner applies it. D6 changes no label itself: R4
      deletes the wrapper that stamps `homelab/role`.
    - The artifact at `prod` carries a `digests.json` with a `footprint` digest for every
      deploy tag and every member of `SHARED_KINDS` (C1). The session reads it the way
      D2.3's `land-wait` does and quotes its names. From D6.1 the changed set alone fills
      `land.sh`'s k8s list, so a missing footprint would widen every landing.
    - `uv run python scripts/diagnostics/probe.py gitops-state` must show no `hold_sha`, no
      `hold_plane` entry naming `ansible/deploy.yml`, and no `k8s_deferred` or
      `k8s_unapplied` line.
    - The producer `render_records` must be disarmed, which D5's disarm PR does (R4's open
      question 5). `probe.py gitops-state` cannot show this (Baseline). The session runs
      `systemctl list-unit-files 'kuma-check-render-records*'`, which must list no unit, and
      reads `ansible/roles/setup/render_records/defaults/main.yml:render_records_state`,
      which must pin `absent`.

   If any check fails, the session stops. For a hold or a ledger line, the operator applies
   what the line names, or clears it with `gitops_state.py clear-hold <sha>` or
   `gitops_state.py clear-owed <class> <subject>`. An armed producer means D5's disarm PR is
   missing, so D5's exit 1 is unmet. D6 waits for D5 rather than disarming the producer
   itself, because the producer imports `deploy_config` and `deploy_toolbox`, which D6.1
   edits, and runs `deploy.sh --dry-run`, which D6.3 changes.
2. **D6.1 — The tick's k8s arms** (about 2,000 lines of code removed, about 60 lines moved
   into `deploy_handlers.py`, plus about 19 test files).
    - **Move the host-plane pages first** (*The host-plane pages after D6.1*). Before the
      two mixed modules go:
        - delete `alert_deferred`'s k8s arm, its `declared_k8s` parameter and
          `deploy_alert_text.k8s_deferred_alert`, with `deploy_alert_text.py`'s import of
          `k8s_remediation` and the docstring lines in `deploy_alert_text.py` and
          `deploy_alerts.py` that name it. Keep `deploy_remediation.k8s_remediation` and
          its `deploy_logic.py` re-export: `land_tags.py` and `deploy_lib/tags.py` call
          them until D6.3, and D6.4 deletes them;
        - call `deploy_alerts.alert_deferred` directly, with `deployed=set()`, from
          `handle_no_services`, from `handle_broad`'s failure arm and from a new tail of
          `handle_broad`. The failure arm drops `deploy_broad_k8s.fold_deferred`. The tail
          replaces `return deploy_broad_k8s.apply_broad_k8s(...)`: it fires the tasks page,
          then `alert_secrets_deferred`, and returns 0;
        - delete `deploy_io.emit_deploy_annotation` and its `deploy_toolbox` field, and the
          deployer half of `ansible/tests/deploy/test_deploy_annotations.py`. Rewrite that
          module's docstring, which names `gitops_deploy.py` as an emitter, and the comment
          above `ansible/roles/k8s/observability/defaults/main.yml:observability_deploy_annotation_expr`,
          which names `deploy_io.py`. Both then name `deploy.sh` and `promote.py land-wait`
          (*Grafana's deploy markers after D6*).
    - **Add two tick tests** to
      `ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_main_branches.py`, which
      already has the tick harness and the `_alerted` helper:
        - a range whose only change is a Pi container role's `tasks/main.yml`, such as
          wg-easy's, fast-forwards, sets the `tasks` slot to the origin and posts one Discord
          message naming the role. A second tick at the same origin posts nothing. The clean
          half changes the same role's `templates/` instead, and pages no tasks channel;
        - a broad setup range, a setup role's template plus `ansible/vars/secrets.yml`, sets
          the `secrets` slot to the origin and posts one secrets message. The test is
          parametrized over the success exit and the failure arm, so both call sites are
          pinned. The clean half is the same range without `secrets.yml`, which posts no
          secrets message.

      These replace the coverage that D6.1 deletes with
      `test_a_secrets_change_bundled_with_an_image_bump_is_still_flagged` and with the
      broad-k8s tests. The red proof of each is the plan as first written: run against a
      tree in which the call is missing from the handler, the test fails. The PR body quotes
      that failing run.
    - **Delete the four modules:** `deploy_k8s.py`, `deploy_broad_k8s.py`,
      `deploy_k8s_owed.py` and `deploy_release.py`.
    - **Delete what only those modules run:**
        - `digest_provable.py`;
        - the deploy-plane plan in `deploy_narrow.plan`, with `narrow_deploy_plane`,
          `shared_role_callers` and `digest_provable`, and D2's flux-owned drop in it;
        - `handle_k8s`, its dispatch, and `_rollback_k8s`;
        - D2's test that a flux-owned image bump lands in `cs.flux` loses its promotion half,
          because `cs.k8s_deploy` goes. The `cs.flux` half stays until D6.4 rewrites it;
        - `_promote_k8s_auto_deploys`, and the `k8s_lookup_readers` call in `deploy_phases.py`;
        - the `k8s_deploy`, `k8s_origins` and `k8s_consumers` fields, together with the
          `cs.k8s_deploy` term of `scripts/deploy_tools/reach.py:Reach.touched`. Without that
          edit, `deploy.sh`'s staleness refusal raises AttributeError on the Pi path, and
          `test_reach.py` fails;
        - the writers of the two k8s ledger classes in `deploy_state.py` and `deploy_alerts.py`;
        - `declared_k8s_services`.
    - **Rewrite the prose that names what this PR deletes** (Baseline, *Kept files that name
      the deleted symbols only in prose*):
        - the docstrings in `deploy_defer.py`, `deploy_inventory.py`, `deploy_state.py` and
          `deploy_alert_text.py`, and `deploy_alerts.py`'s module docstring, which names
          "the tasks/meta/k8s channels";
        - in `handle_broad`, the `DECIDED` comment of the failure arm (#2459), which names
          "the two in `apply_broad_k8s`", and the `broad.deferred` comment, which names
          `apply_broad_k8s`. The decision itself stays: the secrets page rides every exit
          that leaves the range merged, and no contention arm;
        - the docstrings of the functions `deploy_narrow.py` keeps and of `narrow_setup.py`,
          which explain their subprocess boundary by `narrow_deploy_plane`;
        - the comment above `RENDER_PROOF_SHARED_ROLES` in `gitops_markers.py`. Then run
          `uv run python scripts/dev/gen_gitops_markers.py`, so that
          `test_gitops_markers_copies.py` stays green. The edit also re-applies deploy_ui
          and renovate_agent through `SETUP_FILES_SHIPPED_BY_OTHER_ROLES`, which the tick
          does itself.
    - **Delete the k8s config that only the deleted modules read.** That covers the
      `deploy_config.py` fields `k8s_autodeploy_max_per_tick`,
      `k8s_autodeploy_max_claim_services_per_tick`, `k8s_deploy_timeout_s` and
      `k8s_rollback_timeout_s`, their `config.env` lines, their `gitops_deploy_k8s_*`
      defaults, the constants `gitops_deploy.py` reads them into, and the k8s terms in
      `gitops-deploy.service.j2`'s timeout.
    - **Keep the denylist path until D6.2a.** `gitops_deploy.py` calls
      `deploy_phases.py:reconcile_denylist` on every tick, before the noop check. That function
      reads `config.k8s_autodeploy_enabled_in_file` and `config.k8s_autodeploy_denylist`. So
      D6.1 keeps these, and D6.2a deletes exactly them:
        - in `deploy_config.py`, `k8s_autodeploy_enabled`, `k8s_autodeploy_enabled_in_file`,
          `k8s_autodeploy_denylist` and `k8s_autodeploy_pilot`, with their `config.env` lines.
          The pilot field stays unread until D6.2a deletes it with the host key that sets it;
        - the defaults `gitops_deploy_k8s_autodeploy_enabled`,
          `gitops_deploy_k8s_autodeploy_pilot` and `gitops_deploy_k8s_autodeploy_denylist`, and
          the two `ansible/inventory/host_vars/daniel-box.yml` keys that override the first two;
        - in `gitops_deploy.py`, `K8S_AUTODEPLOY_ENABLED`, `K8S_AUTODEPLOY_ENABLED_IN_FILE`,
          `K8S_AUTODEPLOY_DENYLIST` and the fail-closed disarm;
        - `reconcile_denylist`, its callee `deploy_io.py:k8s_declarations_at`, and the
          `denylist_rendered` slot.
    - **Move `declared_denylist` out of `deploy_k8s.py`.** `reconcile_denylist` calls it, and
      it reads `_DECLARATION_RE`, `_TRUE_VALUES` and `SHARED_K8S_ROLES` from the same module.
      All four move into `deploy_phases.py` beside their caller. `deploy_logic.py`'s
      re-export and `ansible/tests/deploy/test_denylist_parsers_agree.py` follow them.
    - **Its own tick runs the old deployer, so the PR carries no deploy-plane path.** The tick
      that fast-forwards D6.1 still runs the code D6.1 replaces, which applies `deploy.yml`
      for a `_BROAD_DEPLOY_PREFIXES` path and writes a hold on failure. At `70591c307`,
      `deploy_changes.services_from_changed_paths(['ansible/inventory/host_vars/daniel-box.yml'])`
      returns `broad=True, broad_deploy=True`. That is why the `daniel-box.yml` keys wait for
      D6.2a. The PR body quotes `services_from_changed_paths` over the PR's own changed paths,
      with `broad_deploy=False`.
    - **Delete `scripts/deploy_tools/deploy_lib/playbook.py:discharge_owed_k8s`** and its
      callers in `under_locks.py` and `detach_run.py`. Every `deploy.sh` run would otherwise
      print a swallowed ImportError.
    - **Delete arm 1 of `cross_plane_order.py`, and rename the module.** Arm 1's subject, an
      edge between a flux-owned and an Ansible-owned k8s role, has been gone since D5. It goes
      with its tests and its use of `deploy_k8s.py:is_image_only_diff`. Arm 2, D5.1d's gate
      and D5.1f's two arms stay, with their fixtures, and the module becomes
      `scripts/validate/render_diff_gates.py` (*The render-diff gates after D6*). The CI step
      follows the rename. `docs/landing.md` and `docs/flux-deploy-plane.md` name the new
      module.
    - **`land.sh`'s k8s list becomes the changed set** (*`land.sh`'s k8s tags after D6*).
      This PR stops the tick's `deploy.yml` apply, so it also replaces what a broad landing
      waits on:
        - stop joining path-derived k8s tags into `land.sh`'s list, in
          `land_lib/classify.py` for a PR's own file list and in
          `land_lib/deploy.py:derive_from_diff` for the fallback. Hand every landing to
          `land-wait`, whose changed set the health gate and the marker read;
        - retire D3.4's interim `cluster-vars` rule in `land-wait`, with its test;
        - drop the `cs.broad_deploy` term from `land_tags.self_applied_playbooks`;
        - add four landing tests in `scripts/deploy_tools/tests/`:
            - a PR whose only path is under `ansible/templates/` reaches `land-wait`, and
              its `source=promote` line names the changed set of a fixture pair of
              `digests.json` files;
            - a PR whose only path is in the shared `manifests` role does the same;
            - a PR whose only path is one entry's `depends_on:` under
              `ansible/inventory/` waits on the dependent tag, whose footprint differs in
              the fixture pair through its `flux/<tag>.yaml` alone;
            - a failed `digests.json` read makes `land-wait` wait on every flux-owned tag
              and say so on the verdict line. If D2.3 already pins this, the PR quotes
              D2.3's test instead.

          The red half of the first three is the tree before this PR: each PR waits on a
          `deploy.yml` receipt and never reaches `land-wait`, and the PR body quotes that
          failing run.
    - **Keep for later PRs:** `ChangeSet.k8s` and the k8s symbols of `deploy_cross_role.py`,
      which `reach.py` and `land_lib` read until D6.3 and D6.4. Also keep `ChangeSet.flux`
      with `service_owners` until D6.4, and the k8s ledger class names and their readers,
      which go in D6.6.
    - **`tasks/code.yml`.** The file lists each runtime module in three places, and its own
      comment says the copy loop and `stamp_deployed_pairs` "must both name every runtime
      module". D6.1 edits all three, and `test_gitops_deploy_ship_list.py` checks each:
        - Shrink the copy loop.
        - Remove the four modules' `live`/`src` pairs from the `stamp_deployed_name:
          gitops-deploy` task's `stamp_deployed_pairs`.
          `test_stamp_pairs_point_at_the_installed_path` already asserts that every pair's
          `src` exists, so a stale pair fails CI. No new test is needed.
        - Move the four flat names out of the task "Remove the deployer libraries that moved
          into deployer_lib". `test_the_removed_flat_files_are_ones_the_role_no_longer_ships`
          requires each name in that loop to exist under `files/deployer_lib/`.
        - Add the four modules to the "Remove deployer modules folded into another" task,
          both as `deployer_lib/<name>` and as the flat name. That task removes
          `/opt/gitops-deploy/{{ item }}`, so the `deployer_lib/` form removes the live copies,
          and the flat form keeps the removal the other task did. The task stays until H7
          deletes the role, as the tree keeps its three entries at the baseline.
        - The next apply rewrites the `gitops-deploy` fragment under
          `setup-deployed-manifest.d/` without the four pairs. manifest-prune-check's
          deployed-code arm is still armed on daniel-box at this point, and
          `test_a_source_deleted_from_the_repo_is_drift_not_an_exemption` shows that a pair
          whose source is gone reads as drift. *Verification* reads the fragment after the
          first tick.
    - **Route.** Implement *The route of a deploy-plane path after D6*, and add the deployer
      test that an `inventory/` change runs no `deploy.yml`, with its red half.
    - **Docs.** Edit the gitops_deploy role's `CLAUDE.md` *Safety*, *Autonomous-role
      contract* and *Which apply clears a hold*: `hold_sha` covers the host plane only.
    - **Root `CLAUDE.md`.** From this PR on, the deployer applies neither an image-pin bump
      nor a deploy-plane change. So the PR edits the sentence in *After a PR Merges — Pull,
      Deploy, Verify* that says the deployer applies three planes, and the bullets of *When
      to wait* that name a k8s health gate or a deploy-plane apply. It runs
      `fact_status.py verify` for both sections. Every session reads this section after a
      merge (failure class 8), so it cannot wait for D6.8a.
    - **Before merging,** the PR body quotes four things: the import grep with its symbol
      half (*Order: an importer goes before what it imports*), an import of
      `gitops_deploy.py` from a copy of `files/` laid out as `tasks/code.yml` installs it,
      `uv run python scripts/deploy_tools/land.py --help`, and
      `uv run python scripts/deploy_tools/deploy_tags.py changed origin/master`. `land.py`
      imports `land_lib.tools`, which imports `land_tags` at module level, before argparse
      runs. `changed` is the derivation `deploy.sh --changed` runs, and it reaches the lazy
      import in `_load_deploy_logic` before it reads the diff. It passes with no traceback.
      Exit 3 is expected for a diff that holds a broad path, as D6.1's own diff does.
      The suite cannot see an import that only an entry
      point reaches (`.claude/rules/python-layout.md`).
    - **Apply and watch.** The deployer applies this setup-plane change itself. The
      executing session watches the next three ticks with `probe.py gitops-state` and the
      age of `/var/lib/gitops-deploy/last_run`. *Rollback* has the hand path.
    - **Repeat D6.0's ledger check after the first tick.** That tick ran the old code over a
      range that can hold other sessions' commits, after D6.0 ran. The session repeats
      D6.0's `probe.py gitops-state` check. The new code has no deploy-plane apply that clears
      a `hold_plane` entry naming `ansible/deploy.yml`, and no discharge for a k8s line. If
      the check finds either, or a `hold_sha`, the operator applies what it names, or runs
      `gitops_state.py clear-hold <sha>` or `gitops_state.py clear-owed <class> <subject>`
      (**operator-only**). D6.2a does not open until the check is clean.
3. **D6.1b — Deploy markers from kustomize-controller's log** (about 80 lines). It needs
   nothing from D6 and can merge any time after D1.6 (*Grafana's deploy markers after D6*).
    - **Measure first.** From daniel-box, read kustomize-controller's lines for a recent
      promotion with `probe.py loki-query` over loki-homelab. The PR body quotes one line
      for an apply that changed objects and one for a reconcile that changed nothing.
    - Add the second annotation query beside `observability_deploy_annotation_expr`, with
      its `job` label from `loki_streams`, and have
      `scripts/grafana/inject_dashboard_annotations.py` add both queries.
    - Add a clean and flagged pair to `ansible/tests/deploy/test_deploy_annotations.py`
      over the two captured lines. The apply line matches once, and the no-change line
      matches nothing. The red half is the matcher without its changed-object clause,
      which matches the no-change line.
    - Rewrite the comment above `observability_deploy_annotation_expr` to name three
      sources: `deploy.sh` for the Pi, `land-wait` for a landing, and kustomize-controller
      for every apply. If D6.1 has merged, this replaces its two-emitter wording.
4. **D6.2a — The denylist's deployer readers** (about 300 lines removed, about 120 moved,
   and about 60 changed in renovate_agent).
    - Move `is_leftover_dir` and the denylist derivation into `scripts/lib/k8s_roles.py`, and
      repoint `_autodeploy.py` and `k8s_autodeploy_counts.py`.
    - **Repoint the Renovate lander in the same PR** (*The `k8s_autodeploy` key keeps its
      Renovate readers*). The PR deletes the `config.env` line that
      `land_renovate_pr.py:denied_roles` reads, so a later PR would leave every unattended
      landing refusing in between.
        - `k8s_autodeploy_counts.py` gains `--denylist`, which prints the derived list
          comma-separated and exits non-zero when the derivation raises.
        - `land_renovate_pr.py` drops `DEPLOYER_CONFIG` and the `deployer_config` parameter
          of `main`. `main` runs that command through `tools.run` with
          `cwd=RENOVATE_AGENT_REPO_DIR`, and `denied_roles` parses its output. A non-zero
          exit or empty output refuses, with a reason that names the derivation.
        - `test_land_renovate_pr.py` replaces the `deployer_config` fixture with a `_Run`
          answer for the command. The empty-list test keeps its red half and gains one for a
          non-zero exit. A test pins the command's argv and `cwd`.
        - `test_renovate_automerge_follows_the_autodeploy_denylist.py` gains a test that the
          `--denylist` output equals `_denylist()`, so renovate.json's rule and the lander
          read one list. Its red half is a fixture role whose stance flips to `false`: the
          printed list must then name it.
        - The renovate_agent role's `CLAUDE.md` *Scope / exclusions* bullet and
          `docs/renovate-agent-bounds-and-digest.md`'s lander paragraph name the new source.
    - **The tick applies both roles.** The PR changes `renovate_agent/files/`, so the tick
      applies `initial_setup.yml --tags renovate_agent` beside `gitops_deploy`. The
      fast-forward runs before either apply, so the checkout holds `--denylist` before the
      new lander is installed. Between the `config.env` re-render and the renovate_agent
      apply, or if that apply fails and leaves a `manual_plane` line, the old lander refuses
      every landing with "the deployer's denylist is empty". That refusal fails closed, so
      it is harmless for a tick and visible in the verdict file.
    - Delete `ansible/filter_plugins/k8s_autodeploy.py`, its row in
      `SETUP_ROLES_CALLING_FILTER_PLUGINS`, and its path in `gen_doc_fragments.py`'s
      `_autodeploy_coverage` sources.
    - Delete the denylist path that D6.1 kept, and nothing else of the deployer's config:
        - `reconcile_denylist`, `declared_denylist` with the three names that moved with it,
          and `deploy_io.py:k8s_declarations_at`;
        - in `gitops_deploy.py`, `K8S_AUTODEPLOY_ENABLED`, `K8S_AUTODEPLOY_ENABLED_IN_FILE`,
          `K8S_AUTODEPLOY_DENYLIST` and the fail-closed disarm;
        - in `deploy_config.py`, the four `k8s_autodeploy_*` fields D6.1 kept, with their
          `config.env` lines;
        - the three `gitops_deploy_k8s_autodeploy_*` defaults D6.1 kept, and the two
          `daniel-box.yml` keys.
    - Delete the deployer-denylist tests, such as `test_k8s_autodeploy_denylist.py`,
      `test_denylist_parsers_agree.py`, `test_k8s_autodeploy_guard.py` and
      `test_denylist_reconcile.py`. Keep the Renovate tests and R4's gate tests, whose
      imports this PR repoints.
    - Delete the `denylist_rendered` slot of `gitops_markers.MARKERS`, with its comment and
      its row in `test_deployer_state.py`, because `reconcile_denylist` was its only reader.
      Run `gen_gitops_markers.py`, so the monitor-bridge copy follows. Rewrite the prose
      that names `reconcile_denylist`: the comments in `gitops_deploy.py` and
      `deploy_config.py`, the module docstring of `deploy_phases.py`, and the docstring in
      `scripts/deploy_tools/land_lib/tools.py`.
    - The `config.env` template change routes the PR to `initial_setup.yml --tags
      gitops_deploy`, which the tick applies itself. That apply renders `config.env` from the
      host vars at the PR's head, so the removed `daniel-box.yml` keys take effect. The plugin
      path and the `daniel-box.yml` path take the deploy-plane route. That route is a no-op,
      because the tick that applies D6.2a runs D6.1's code.
5. **D6.2b — The key's wording** (about 190 changed lines, almost all one-line edits).
    - **Measured against D5's end state, not `70591c307`.** D2 replaced the auto-deploy
      stance with the Flux line in every flux-owned glance block (*What D2, D4 and D5 hand to
      D6*), so this PR rewrites no glance block.
    - Delete the stance from the Ansible-owned k8s branch of `gen_role_glance.py` and of
      `service_catalog.py:build_rows`. After D5 no k8s entry reaches that branch, so the
      `regen-role-glance` hook rewrites nothing and `docs/reference/services.md` changes no
      row. The Renovate stance does not return to the blocks (*D2's ownership readers after
      D6*).
    - Rewrite the "Auto-deploy stance — read by gitops_deploy" line in the 62 defaults. Each
      role's own reason stays.
    - Also update `catalog_backup.py`, `service_catalog.py`, `fragment_renderers.py`, the
      scaffold in `new_k8s_service.py` with its test, and the `new-k8s-service` and
      `renovate-prs` skills.
    - This PR is separate from D6.2a so that the deletion diff stays readable.
6. **D6.3 — The k8s arms of `land.sh` and `deploy.sh`** (about 700 lines removed).
    - Delete `land_lib`'s k8s deploy arm and `land_rerolls.py`. `promote.py land-wait`
      and its `source=promote` annotation stay, because they serve every flux-owned tag
      (*Grafana's deploy markers after D6*). With the `tag_owners` split gone, `land.sh` hands
      `land-wait` no k8s tag of its own. From D6.1 the changed set is the whole k8s list
      (*`land.sh`'s k8s tags after D6*), and the census row below makes each of its tags
      flux-owned. So a PR that moves a k8s render waits on a non-empty list, whether its path is a role's own, a shared role's, a
      template or an inventory key, and the marker's `services=` field reads that list.
      D2.3's test that a settled landing writes one line keeps passing on this PR.
    - Remove the bindings of `land_rerolls` and `shared_role_reach` from `land_lib/tools.py`,
      and the `shared_role_callers` import from `land_shared.py`. With that import go
      `land_shared.shared_caller_tags` and `land_shared.narrowed_roles`, and every reader:
        - the caller expansion and the smoke narrowing in `land_lib/classify.py`, and
          their two bindings and comment in `land_lib/tools.py`. The smoke narrowing
          deployed one caller to exercise a shared role's Ansible tasks, which no Flux
          apply runs;
        - in `land_tags.py`, the re-export, the CLI's read, the module docstring and
          `plane_note`'s docstring, and the comment in `land_lib/landing.py`;
        - `scripts/dev/findings_lib/precomputed.py:deploy_line`, which predicts a PR's
          deploy from its paths with no git. It reads no artifact, so for a k8s
          path it says that `promote` applies the change and that `land.sh` waits on
          the changed set. Its module docstring changes with it.
      The changed set already holds every tag whose footprint a shared role moves.
    - **Remove the last two callers of `k8s_remediation`.** `land_tags.py:plane_note` loses
      its shared-role note, and `land_tags.py` drops `k8s_remediation` from its
      `deployer_lib.deploy_logic` import. `deploy_lib/tags.py:_load_deploy_logic` stops
      importing it, and `deploy_lib/tags.py:changed` loses the shared-role arm that called
      it. Rewrite the comments in both modules that name it. D6.4 deletes the function.
    - Narrow `deploy_lib/tags.py` to the Pi's tags plus the refusal message, which removes
      the lazy `narrow_cmd` import. The refusal keys on the entry's `platform: k8s`, so it
      no longer reads `tag_owners`, and its exit stays 5. `changed` then derives the Pi's
      tags only, and its `DEPLOY_BROAD` refusal concerns the Pi half alone. Delete
      `expand_shared_tags`'s call in `deploy_lib/run.py`, and the shared-role reading in
      `deploy_lib/staleness.py`.
    - **`derive_from_diff` stops asking a hand for the cluster half.** `_narrowed` goes
      with the `narrow` subcommand. `derive_from_diff` takes the k8s tags from the render
      diff over `--since`, as D6.1 wired it. `changed`'s broad refusal stops passing the
      deploy-plane half to `broad_remediation`, whose text names a full
      `ansible/deploy.yml` run, and prints its own line naming
      `deploy.sh -e target=daniel-pi`. `deploy_defer.py` keeps calling
      `broad_remediation`. `derive_from_diff` treats a `DEPLOY_BROAD` from `changed` as
      the Pi half: it prints that line beside the verdict, and the verdict rests on
      `land-wait`. Before this PR such a range
      ended in `needs-manual-apply` with "deploy it by hand", and from this PR on
      `deploy.sh` refuses every k8s tag, so nobody could follow that instruction.
    - Delete D2's `tag_owners` split in `land_lib/deploy.py` with the k8s deploy arm, because
      every k8s tag waits on promotion. `land_lib/merge.py`'s handback refusal stays until
      H1.9 (*D2's ownership readers after D6*).
    - Keep `land_reach.py` and its note in `land_tags.py`.
    - Add the census row: every k8s `containers_list` entry carries `owner: flux`. It goes in
      the census tables under `ansible/tests/repo/`, with a red and a green fixture. It finds
      its subject by pattern, so it also names members it must find, `bento-pdf` and `flux`,
      and a floor on the count of k8s entries, the count D5 ends with
      (`.claude/rules/python-layout.md`, *A check that finds its subject by pattern names a
      member it must find*). Without
      them, a selector that stops matching passes on nothing and removes the guard D6.7
      relies on. H1.9 moves the list to `cluster/services.yml` and must repoint the row
      (cross-slice note).
    - **Root `CLAUDE.md`.** Edit the `deploy.sh` exit-3 bullet of *When to wait*, because
      `deploy.sh` now refuses every k8s tag, and run `fact_status.py verify` for that
      section.
    - **Test.** In `scripts/deploy_tools/tests/`, a range over `gh`'s 100-file page that
      holds an `ansible/templates/` path reaches `land-wait` with the changed set of a
      fixture pair of `digests.json` files, and ends without `needs-manual-apply`. Its red half is
      the tree before this PR, where `derive_from_diff` dies with "deploy it by hand".
      D6.1's four landing tests stay green, and the PR body quotes them.
    - **Before merging,** the PR body quotes the symbol grep for `k8s_remediation`, which
      prints only `deploy_remediation.py`, `deploy_logic.py` and the comments D6.4
      rewrites, plus `land.py --help` and `deploy_tags.py changed origin/master`.
7. **D6.5a — Arm the setup-drift reader on daniel-box** (about 40 lines).
    - Add daniel-box to `setup_drift_check_hosts`, and rewrite the comment above it.
    - Rewrite `test_the_reader_is_armed_where_no_manifest_prune_check_runs` so that it
      asserts every host that renders setup templates is armed. It names daniel-box and
      daniel-server.
    - **No tile, no message and no rule.** Kuma is gone before D6 opens, and the series
      carries the host as `origin`, so the template's message and the Kuma tile need no
      edit. B2's setup-drift rules stay daniel-server's alone in this PR, and D6.5 adds the
      box's arm (*The setup-drift reader covers daniel-box*).
    - **The PR names a hand apply on daniel-box.** It changes `ansible/inventory/group_vars/all.yml`
      and a test, and no file of `initial_setup`.
      `deploy_changes.services_from_changed_paths` reads that pair as `broad=True,
      broad_deploy=True` with no setup role, so the tick applies nothing on the box
      (D6-R1-05's correction). After the merge tick has fast-forwarded, the session runs
      `uv run ansible-playbook ansible/initial_setup.yml --tags setup_drift > /tmp/d6-5a.log 2>&1 < /dev/null`
      on daniel-box, **operator-attended**, and quotes the recap. If the tick records an
      `initial_setup` line in `manual_plane` instead, the session runs the apply that the
      line names and clears it.
    - **daniel-server owes nothing.** Its armed state and its render of
      `setup-drift-check.sh.j2` do not change, so its reader reads nothing stale.
    - **The check before D6.5.** On both hosts the setup-drift timer is active, under either
      name (`systemctl list-timers '*-check-setup-drift*'`, before or after M6's C-3b
      rename). After the box's first scheduled run, `homelab_check_success{check="setup-drift-check"}`
      reads 1 for `origin="daniel-box"` and for `origin="daniel-server"`. D6.5 does not open
      until that holds, because D6.5's `NotReporting` arm for the box needs a sample inside
      its deadline.
    - **The red proof** is the fixture pair in `ansible/tests/deploy/test_setup_drift_check.py`
      that runs the same `setup-drift-lib.sh` (a red verdict on a live host would page through
      B2's rule):
      `test_a_stale_deployed_copy_is_reported` and `test_a_changed_template_is_reported_stale`.
      A red push on the live tile would page.
8. **D6.5 — Disarm the drift checks** (about 200 lines changed). It removes every unit and
   alert rule and keeps every line of the checks' code, because the release-staleness cron
   runs `probe.py releases` from the primary checkout until this PR's hand apply removes it
   (Baseline).
    - **The Kuma side is M6's, not D6's (#4609).** M6's B1 converts the three checks to
      textfile checks and retires their tiles, their push tokens and every consumer of the
      tokens. M6's C-2b then deletes Kuma. D6 opens only after C-2b, because uptime-kuma
      never becomes flux-owned (M6 *When D5 flips a role M6 edits*). So D6.5 removes no
      tile, no token and no cluster-file placeholder. Before merge, the PR body quotes
      `git grep -n -E 'manifest_prune_push_token|release_staleness_push_token|live_drift_push_token'`
      at its base, expected to print nothing. A hit means B1 left a consumer. The session
      then stops and hands the hit to M6 rather than removing it here.
    - **Remove B1's rules for the three checks.** B1 gives each check a `<Check>Failed` and a
      `<Check>NotReporting` rule (M6 *Recipes*, M3's recipe). A disarmed cron writes no
      textfile, so its NotReporting rule would fire at its deadline. D6.5 deletes the six
      rules in this PR. Flux applies the rule removal at merge, before the hand apply below
      stops the crons, so no rule reads a check that has stopped.
    - **Tasks become `absent` tasks, and only these:**
        - the two timers, through the state variable of their timer imports
          (`kuma_check_state`, or its name after M6's C-3b);
        - the three checks' `.prom` files under `/var/lib/node-exporter-textfile/`, which B1
          added;
        - the release-staleness cron;
        - the three release groups' `/opt/homelab/releases/<group>`,
          `/opt/homelab/current/<group>` and `/usr/local/bin/<name>` links;
        - `/opt/live-drift-check`, the live-drift env file, and
          `/var/lib/homelab/release-staleness`;
        - the four stamp fragments: `k3s-manifest-prune` and `k3s-release-staleness` in
          `setup-render-manifest.d/`, and `k3s-live-drift` and `k3s-manifest-declares` in
          `setup-deployed-manifest.d/`.
    - **Shared tasks lose a tag, not their state.** A task that also carries `backup-health`,
      `disk-health` or `etcd-snapshot` only drops `manifest-prune` or `release-staleness` from
      its tags. Every absent task carries the drift tag of the task it replaces.
    - **Add daniel-box's arm to B2's setup-drift rules.** In the same rules file, B2's
      `<Check>Failed` and `<Check>NotReporting` for `check="setup-drift-check"` gain a copy
      that names `origin="daniel-box"`, with the box as `service`. Each new arm gets a
      promtool case that fires on a box series alone, and the `NotReporting` case fires when
      only daniel-server reports. This PR opens only after D6.5a's check reads the box's
      series, so the `NotReporting` arm finds a sample at `promote`. The box's arm replaces
      B1's manifest-prune rules, which this PR removes, as its paging drift detector.
    - **The narrowing gate.** The tick records the `setup/k3s` half in `manual_plane`, with
      the tags that `scripts/deploy_tools/narrow_setup.py` derives for the range. When the
      derivation refuses, the line names the whole role, and its only apply is the maximal
      `--tags k3s` (`deploy_remediation.py:maximal_tag_warning`, #2294). So D6.5 merges only
      once `uv run python scripts/deploy_tools/narrow_setup.py k3s <merge-base> <PR head> --playbook ansible/k3s-bringup.yml`
      exits 0, and the PR body quotes its stdout and stderr. Three shapes refuse, so D6.5
      carries none of them:
        - **A deleted file under `templates/` or `files/`.** `path_tags` asks
          `RoleIndex.readers_of` at the head, and no task there names a deleted file. This is
          why `live-drift-check.env.j2` waits for D6.5b.
        - **A `defaults/` key that is deleted, or changed while only absent tasks read it.**
          Measured: `narrow_setup.py k3s 3188cdb98^ 3188cdb98`, a range that deleted a k3s
          template and edited `kuma-push.env.j2`, prints `cannot narrow k3s (nothing in this
          role reads k3s_remember_log_max_age_days)`.
        - **An untagged task in `tasks/health-crons.yml`.** `task_walk.py:file_tags` returns
          None for a file with one untagged task.

      A path that refuses moves to D6.5b.
    - **The line carries six tags, not three.** `path_tags` narrows a task file to the union
      of every tag in it, not to the tasks the diff touched. Measured:
      `narrow_setup.py k3s a9e0483c6^ a9e0483c6` prints `tasks/health-crons.yml ->
      backup-health,disk-health,etcd-snapshot,live-drift,manifest-prune,release-staleness`.
    - **This PR owes a hand apply.** After the tick has fast-forwarded, `probe.py
      gitops-state` prints the line's command. The session runs it as printed,
      operator-attended. The expected command is
      `uv run ansible-playbook ansible/k3s-bringup.yml --tags backup-health,disk-health,etcd-snapshot,live-drift,manifest-prune,release-staleness > /tmp/d6-5.log 2>&1 < /dev/null`.
      It then runs `gitops_state.py clear-owed manual_plane k3s --applied <the same tags>`,
      which must print `cleared k3s from ...`. A `kept k3s` or `STILL pending` message means
      that the line differs from what was applied, and the session stops. It never runs
      `--tags k3s`. If the line names the whole role anyway, the session follows D6.5b's
      whole-role procedure.
    - **No secrets page on the merge tick.** D6.5 leaves `ansible/vars/secrets.yml` alone,
      because B1 already removed the three tokens. An earlier draft removed them here, which
      sent the tick through `handle_broad` and D6.1's secrets page.
    - **Until the apply,** the old units keep running against code that is still in the tree.
      They write `.prom` files that no rule reads after the merge, so they page nothing.
    - **The apply re-runs the shared tasks.** The six tags also re-run the install tasks of
      the three kept heartbeat families, whose sources D6.5 does not change. The session
      quotes the recap and names every `changed` task in the PR thread.
    - **The setup-drift check stays green.** After the apply, the box's setup-drift check,
      which M6's B2 made a textfile check, still reads green. A fragment left behind would
      read as drift (failure class 5). D6.5 changes no source that a remaining fragment
      stamps.
9. **D6.5b — Delete the drift checks' code and the absent tasks** (about 2,700 lines
   removed). It opens only after D6.5's hand apply, because the cron it removed ran
   `releases.py` from the primary checkout.
    - **Delete the checks' code:** the two check templates, `live-drift-check.env.j2`,
      `live_drift_check.py`, the checks' `defaults/` keys, and `manifest_declares.py` with
      `test_manifest_declares.py`.
    - **Delete the `releases` family** and its subcommand. Delete
      `health.py:release_expected_restarts` in the same PR, because it imports
      `releases.RELEASE_DIR`, and every workload is flux-owned. D2's `tag_owners` skips in
      `health.py` and `releases.py` go with them.
    - **Edit failure class 5's row.** Drop the `test_manifest_declares.py` citation, and name
      the replacement: Flux's `prune: true` removes an object the bundle stops declaring, and
      D2's publish census and D6.7b's tag rule guard the bundle. Class 3 gets no new row,
      because nothing is left that reads green with nothing to inspect.
    - **Delete D6.5's absent tasks,** which its hand apply already ran.
    - **No fragment names a deleted source.** D6.5's apply removed the four fragments, so the
      box's setup-drift reader reads no drift when these sources go.
    - **It lands a whole-role line, by construction.** Each deleted template, file and
      `defaults/` key refuses the narrowing, as D6.5's gate describes. Before merge, the PR
      body quotes `narrow_setup.py k3s <merge-base> <PR head> --playbook
      ansible/k3s-bringup.yml`, expected to exit 1 with `cannot narrow k3s`. The line's only
      apply is `--tags k3s`, and the session never runs it, because it reaches three gated
      control-plane tasks (`ansible/roles/setup/k3s/CLAUDE.md`,
      `deploy_remediation.py:maximal_tag_warning`). Instead, all of it operator-attended:
        1. Before merge, `probe.py gitops-state` shows no `manual_plane` line for k3s.
        2. After the tick, the session reads the range the tick fast-forwarded. If that
           range holds any commit besides D6.5b's, the line may also cover that commit's
           work. The session then stops and hands the line to the operator.
        3. It applies the tags that `tasks/health-crons.yml` declares at the head, expected to
           be `backup-health,disk-health,etcd-snapshot`, with the same log redirect as D6.5's
           apply. It quotes the recap and names every `changed` task.
        4. It verifies the state that D6.5's apply made. `systemctl list-unit-files
           '*-check-manifest-prune*' '*-check-live-drift*'` lists no unit under either
           timer name (before or after M6's C-3b rename), `/etc/cron.d` and root's crontab
           hold no release-staleness entry, the four fragments and the three `.prom` files are
           absent, and the setup-drift check reads green.
        5. It clears the line with a bare `gitops_state.py clear-owed manual_plane k3s`.
    - **The bare clear is an exception to "apply, then clear".** The rule exists so that a
      clear never drops work that is merged and unapplied. D6.5b removes only files that no
      task names and absent tasks whose effect step 4 verified. Every remaining task in the
      file it edits ran in step 3, and no other task file changes. So the whole-role line
      holds no unapplied work, and the one apply it accepts would reach the control plane for
      nothing.
    - **Rejected: leave the role's files to H7.** Keeping the templates, the two modules and
      their `defaults/` keys until H7 deletes the role avoids the whole-role line. It also
      keeps code whose subject D6.5 removed, against `.claude/rules/python-layout.md`'s *A
      check retires when its subject does*, for the rest of the migration.
10. **D6.4 — Reach and narrowing** (about 1,500 lines removed). It merges after D6.5b.
    - Delete `shared_role_callers.py`, `shared_role_reach.py`, `narrow_lib/broad.py` and
      `narrow_lib/containers.py`.
    - Delete `reach.py`'s k8s readings, `ChangeSet.k8s`, and the k8s symbols of
      `deploy_cross_role.py` except `k8s_lookup_map`.
    - Delete `deploy_remediation.py:k8s_remediation`, its `deploy_logic.py` re-export, and
      its test in `test_deploy_remediation.py`. D6.3 removed its last two callers, and the
      function reads the `deploy_cross_role.py` symbols this PR deletes, so it cannot
      outlive this PR. Rewrite the docs that still name it: the comment in
      `deploy_changes.py` beside `k8s`, `deploy_remediation.py`'s module docstring, and
      `narrow_setup.py`'s module docstring. Exit 5's grep then counts 0 for both symbols.
    - **Route every k8s role path to `cs.flux`.** The `at.plane == "k8s"` branch of
      `services_from_changed_paths` adds the role to `cs.flux` with no ownership read, and
      `deploy_inventory.service_owners` goes. D2's `cs.flux` test becomes: a path under any
      `ansible/roles/k8s/<role>/` lands in `cs.flux` and in no other set. Its red half runs
      the test against a branch that adds nothing. The path then lands in no set, and the
      test fails (*D2's ownership readers after D6*).
    - Rewrite the prose that names those symbols: `deploy_cross_role.py`'s module docstring,
      `reach.py`'s docstrings, and the sentence in `ansible/roles/setup/common/CLAUDE.md`
      that names `deploy_cross_role.K8S_ROLES_IMPORTING_SETUP_FILES`, with `fact_status.py
      verify` for its section if the lock records it.
    - `ansible/tests/setup/test_setup_cross_role_files.py` loses its k8s table.
    - **Test.** In `scripts/deploy_tools/tests/`, a PR whose only path is under
      `ansible/roles/k8s/bento-pdf/templates/` reaches `land-wait` with `bento-pdf` after
      `ChangeSet.k8s` is gone. Its red half is this PR without the `cs.flux` route, where
      the path maps to no tag. D6.1's and D6.3's landing tests stay green, and the PR body
      quotes all of them, with `land.py --help` and `deploy_tags.py changed origin/master`.
    - The changes to the deployer's files apply through the tick, as D6.1's did.
11. **D6.6 — The k8s ledger readers** (about 350 lines removed). It merges after D6.0
    found the ledger empty, and after D6.1 stopped the writers.
    - **Repeat the ledger check immediately before merge.** D6.6 removes `gitops_state.py`'s
      last `clear-owed` path for the two k8s classes. So the session repeats D6.0's
      `probe.py gitops-state` check just before the merge. A `k8s_deferred` or
      `k8s_unapplied` line found then is cleared first with `gitops_state.py clear-owed
      <class> <subject>` (**operator-only**).
    - Delete `k8s_deferred_entries`, `k8s_unapplied_entries` and the two class names from
      `gitops_ledger.py` and `gitops_markers.py`. Then regenerate the copies in monitor-bridge
      with `uv run python scripts/dev/gen_gitops_markers.py`.
    - Delete the readers: monitor-bridge's `checks/gitops.py` arms, `deploy_ui_reads.py`,
      `agent_logic.py`, and `session-health.py` with its two tests.
    - Also delete the readers in `deployer_park.py`, `narrow_git.py`, `gitops_view.py`,
      `staleness.py`, and `gitops_state.py`'s two `clear-owed` classes.
    - The source change re-applies deploy_ui and renovate_agent through
      `SETUP_FILES_SHIPPED_BY_OTHER_ROLES`, which the tick does itself. monitor-bridge reaches
      the cluster through `promote`.
12. **D6.7a — The deletion drill** (about 230 lines: the workflow, the drill script and the
    fixture tree). It merges before D6.7, because D6.7 quotes the job's run on its own PR.
    - Add the job, the drill script and the fixture tree with `prune-drill`,
      `prune-drill-red` and `prune-drill-keep`, as *The deletion drill runs in a throwaway
      kind cluster* describes.
    - **The PR's own run is the baseline.** The renderer still writes `Orphan`. The
      as-rendered case therefore keeps the Deployment, the PVC and the Namespace, and the
      red-half case loses the PVC. The inventory-prune case keeps `keep-marked` and loses
      `keep-unmarked`, which already tests the marker's form under `Orphan`. The PR body
      quotes all three cases, and the revision-1 wait that shows all three tags Ready, so
      every fixture PVC bound under kind's `WaitForFirstConsumer` class.
    - **Not a required check.** Making the job a required check changes GitHub settings, so
      it is **operator-only** (Open question 10). Until then, D6.7's merge waits on the job
      because its PR body must quote a green run.
    - **The two-writer rule (C4)** has nothing to check. The drill's cluster is not
      production, and no `containers_list` entry changes.
13. **D6.7c — Break-glass promotion** (about 190 lines). It merges after D6.7a, whose drill
   it extends, and as early as that allows, because the gap it closes opened at D5's last
   flip (*Break-glass promotion*).
    - **Precondition:** D5 Open question 2 has kept the in-cluster registry. Otherwise the
      session stops.
    - **Operator-only, before the PR:** the operator generates the key pair and stores the
      private half in the break-glass kit. The PR carries only the public half.
    - Add the public-key Secret to the flux role, and the break-glass source form to its
      OCIRepository template. A renderer test pins the production render unchanged.
    - Add the `break-glass` and `break-glass-red` cases to `flux-deletion-drill`, and widen
      its path scope to `docs/break-glass.md` and the flux role's OCIRepository template.
    - In `docs/break-glass.md`, update item 8: the repo bundle is also the render source for
      break-glass promotion, and the kit holds the break-glass private half beside it. Add
      the scenario *GitHub, GHCR or Sigstore is unreachable*, with the runbook, its
      fresh-cluster steps and the way back to GHCR.
    - **The pre-merge proof.** The PR body quotes the drill run with both cases, and the
      answer to Open question 16 that it gives.
    - **The two-writer rule (C4)** has nothing to check. No `containers_list` entry changes.
14. **D6.7b — The guards the default policy needs** (about 200 lines). It merges before
    D6.7, so that every guard is live before a deletion can happen. Each guard is harmless
    under `Orphan`.
    - **The tag rule**, as *The deletion policy, and the end of the handback* describes: one
      function, its publish arm in `promote.py check-bundle` against the bundle at `prod`,
      and its pull-request arm in `render_diff_gates.py` against the base render. Fixtures,
      each an `_is_clean` and `_is_flagged` pair:
        - flagged: a tag in the reference that the head lacks while its role directory stays;
          clean: the tag dropped together with its directory;
        - flagged: `flux` missing at the head, whatever its directory;
        - flagged: a refused commit followed by an unrelated commit, with `prod` unmoved.
          The second commit must still fail;
        - flagged: a pull from GHCR that fails, so the publish pushes nothing.
    - **The dropped-object arm** loses its handback exemption and counts a tag that leaves
      the render whole, judged by its ownership at the base (*The render-diff gates after
      D6*). Fixtures: a whole-tag removal without trailers is flagged, and the same removal
      with one trailer per workload is clean.
    - **`decide`'s object-loss check loses its handback exemption.** D2.6c's check refuses a
      candidate whose bundle loses an object that no `Removes-Object:` trailer declares. It
      exempts a Kustomization dropped at a handback, because `Orphan` keeps that handback's
      objects live. From D6.7 a dropped Kustomization deletes its objects, so the exemption
      goes here, before D6.7 switches the policy. D6.3's census already refuses a handback,
      so the change is harmless under `Orphan`. Fixture: a dropped Kustomization with no
      trailer is flagged, whether or not it has the handback's shape.
    - **The prune marker on PersistentVolumes and StorageClasses**, once the C3 amendment in
      Open question 12 lands. A renderer test pins the marker on media-volume's
      PersistentVolume and `media-local`.
    - **The CRD guard.** A renderer test fails on a CustomResourceDefinition in a tag other
      than `flux` that renders `prune: true`, with a red fixture tag.
    - **The contract gate.** D6.7b does not merge until contracts.md holds Open question
      12's request: either C3's *Prune exemptions* line names PersistentVolumes and
      StorageClasses, or *Requested amendments not yet applied* holds the row with the gate
      "before D6.7b merges". The PR body quotes that line. If C3 is amended, the PR ships
      the marker test. If only the row exists, it ships Open question 12's fallback
      instead: the tag rule refuses to remove a tag whose render at `prod` holds a
      PersistentVolume. With neither line, D6.7b waits, because the next edit to C3 can
      drop a guard that the contract never stated.
15. **D6.7 — The deletion policy** (about 100 lines).
    - **The contract gate.** D6.7 does not merge until C3's *Deletion policy* line names
      the kept set (`flux`, every `<tag>-deferred` and every shared kind keep `Orphan`).
      The register row *The deletion policy's kept set* does not satisfy the gate on its
      own. The register's preamble says each row "stays as written until its amendment
      lands, before the gate", and C3 as written reads as switching `-deferred` and
      `flux` too (Open question 7). The PR body quotes the amended line. Without it, D6
      ends at `Orphan`, which Open question 7 already allows.
    - **The operator's decision (operator-only).** C3 makes the switch conditional on the
      operator's approval, and a tag removed after D6.7 loses its objects for good
      (*Rollback*). The executing session does not run `land.sh` on D6.7 until the
      operator has written the decision in the PR thread, after reading the count. The
      PR body quotes that comment. Root `CLAUDE.md` lets a brief override its rule to merge
      one's own PR through `land.sh`, so the executing session's brief for D6.7 must say
      not to merge until the decision is quoted.
    - The renderer drops `deletionPolicy: Orphan` from every `<tag>` except `flux`. D5.1a1's
      renderer test narrows to the kept set, every Kustomization but a per-tag `<tag>`, plus
      `flux`. It iterates `SHARED_KINDS` rather than an enumerated list, with `flux`,
      `flux-deferred` and `cluster-vars` as named members. A second renderer test fails on a
      `<tag>` other than `flux` that still carries `Orphan`.
    - `docs/flux-deploy-plane.md` replaces the two-step retirement with the one-step one,
      adds the first step that removes a retired tag's deferred objects, and states the
      rename procedure. That procedure says the service is down between the deletion and the
      re-creation, that prune-exempt kinds stay and are adopted, and that `flux` is never
      renamed.
    - The PR body records the count from *The deletion policy, and the end of the handback*.
    - **The pre-merge proof.** The PR touches `scripts/renderer/`, so `flux-deletion-drill`
      runs on it. The PR body quotes that run: the as-rendered case deletes the Deployment
      and keeps the PVC and the Namespace, the red-half case loses the PVC, and the
      inventory-prune case keeps `keep-marked` and loses `keep-unmarked`. D6.7 does not merge
      on a red run, or on a run that printed `skipped`.
16. **D6.8a — The root docs and the archive** (about 400 lines changed, most of them moved).
    - Rewrite *After a PR Merges* and *When to wait* in full, on top of D6.1's and D6.3's
      edits, run `fact_status.py verify` for each edited section, and commit
      `docs/facts.lock`.
    - Move the k8s sections of `docs/gitops-pipeline.md` to `docs/archive/`, with a pointer.
17. **D6.8b — The ADR** (about 150 lines).
    - Write or accept the ADR, set ADR-0010's status, and add both rows to
      `docs/adr/index.md` and the nav.
    - Approving the ADR is **operator-only**, and only this PR waits on it.

## Verification

```bash
# Exit 1, after D6.4. Run the grep from the repo root; the deletion list is in the tree diff.
# tasks/code.yml is excluded: its folded-module absent task names the deleted modules until H7.
git grep -n -w -e deploy_k8s -e deploy_broad_k8s -e deploy_k8s_owed -e deploy_release \
  -e k8s_deploy -e k8s_origins -e k8s_consumers -e emit_deploy_annotation \
  -- ansible scripts .claude ':!ansible/roles/setup/gitops_deploy/tasks/code.yml' \
  | grep -v '/tests/' | grep -c .                      # 0, the observability comment included
./scripts/deploy.sh --tags bento-pdf; echo "exit=$?"   # exit=5, names promote
uv run pytest ansible/tests/repo -k census             # D6.3's owner row passes
# After D6.3, the first landing of a flux-owned tag that settles: land-wait's marker.
uv run pytest ansible/tests/deploy/test_deploy_annotations.py   # deploy.sh and land-wait
journalctl -t deploy-annotation --since today | grep -c 'source=promote'   # 1 or more
# After D6.4, the first settled landing of a direct role PR and of a broad PR (a template
# or a shared role): each marker names a non-empty services= list.
journalctl -t deploy-annotation --since today | grep 'source=promote' \
  | grep -c 'services=[a-z]'                                  # 2 or more

# Exit 2, after the next setup-plane PR lands.
journalctl -u gitops-deploy --since today | grep -i 'initial_setup'
uv run python scripts/diagnostics/probe.py gitops-state
journalctl -t setup-drift-check --since today | tail -3   # status=up, on each host
uv run python scripts/diagnostics/probe.py metric 'homelab_check_success{check="setup-drift-check"}'   # 1 per origin
# Exit 2, the two host-plane pages, from D6.1 on: the two tick tests pass.
uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_main_branches.py \
  -k 'tasks or secrets'

# D6.5 and D6.5b, on the PR before merge: the tags the tick records for setup/k3s.
uv run python scripts/deploy_tools/narrow_setup.py k3s <merge-base> <PR head> \
  --playbook ansible/k3s-bringup.yml; echo "exit=$?"   # D6.5: exit=0, six tags. D6.5b: exit=1
# After D6.1's first tick, after each k3s clear, and just before D6.6 merges:
# no hold_sha, no deploy.yml hold_plane entry, no k8s line, no k3s manual_plane line.
uv run python scripts/diagnostics/probe.py gitops-state
# After D6.1's first tick, on daniel-box: the stamp fragment names no deleted module (0),
# and manifest-prune-check still reports. M6's B1 made it a textfile check, and Kuma is
# gone before D6 opens, so the observable is the series, not a tile.
grep -c -e deploy_k8s -e deploy_broad_k8s -e deploy_release \
  /var/lib/homelab/setup-deployed-manifest.d/gitops-deploy   # 0
uv run python scripts/diagnostics/probe.py metric 'homelab_check_success{check="manifest-prune"}'
# D6.1, before merge: the four landing tests. The red half of the first three runs on the
# tree before the PR.
uv run pytest scripts/deploy_tools/tests
# D6.1b, before merge: one captured apply line and one no-change line from kustomize-controller,
# through probe.py loki-query (its --help gives the selector flags), then the marker pair.
uv run pytest ansible/tests/deploy/test_deploy_annotations.py
# D6.2a, in the primary checkout after its tick: the lander's source prints the list.
uv run python scripts/dev/k8s_autodeploy_counts.py --denylist   # non-empty, comma-separated
# D6.2a, after the next agent run that reaches a Renovate PR: each verdict written since
# the new lander was installed is a VERDICT line or a REFUSED reason that is not about the
# denylist.
find /var/lib/renovate-agent-land -name '*.verdict' -newer /opt/renovate-agent/land_renovate_pr.py \
  | xargs grep -H .

# Exit 3 and 4.
uv run pytest ansible/tests/repo/test_adr_links.py ansible/tests/repo/test_documented_paths_exist.py
uv run python scripts/dev/fact_status.py status | grep -v ' IN '
uv run mkdocs build --strict --site-dir /tmp/d6-site

# Exit 5.
git ls-files scripts/deploy_tools/narrow_lib/broad.py scripts/diagnostics/probe_lib/releases.py \
  ansible/filter_plugins/k8s_autodeploy.py | grep -c .   # 0, and so on for the tree diff
git grep -n -w -e K8S_ROLES_IMPORTING_SETUP_FILES -e k8s_lookup_readers \
  -e narrow_deploy_plane -e _rollback_k8s -e reconcile_denylist -e k8s_remediation \
  -e shared_caller_tags -- ansible scripts \
  | grep -v '/tests/' | grep -c .                      # 0
git grep -n -w -E '(from|import) [A-Za-z_. ]*(deploy_k8s|deploy_broad_k8s|deploy_k8s_owed|deploy_release|digest_provable|shared_role_callers|shared_role_reach|land_rerolls|k8s_autodeploy)' \
  -- ansible scripts .claude | grep -c .               # 0
uv run pytest

# D6.7c, on its PR before merge: both break-glass cases ran, with their verdicts.
gh run view <databaseId> --log | grep -E 'case=break-glass(-red)?'
# Exit 6, at D6's last PR.
wc -l scripts/deploy_tools/promote.py                   # at most 900
git ls-files scripts/deploy_tools/land.sh scripts/deploy_tools/land.py \
  'scripts/deploy_tools/land_lib/*.py' | grep -v /tests/ | xargs cat | wc -l   # at most 6,400

# D6.7, on its PR before merge: the drill ran (not skipped) and passed all three cases.
gh run list --workflow flux-deletion-drill.yml --branch <D6.7 branch> --limit 1 \
  --json conclusion,databaseId                         # conclusion: success
gh run view <databaseId> --log | grep -E 'case=(as-rendered|red-half|inventory-prune)'
```

**Each new check can go red.** D6 adds these checks, and each ships with its red half:

- **The deployer test that an `inventory/` change runs no `deploy.yml` (D6.1).** Its red half
  feeds the same range to a stub tick whose plan still includes the deploy-plane arm, and
  fails.
- **The tasks-page tick test (D6.1).** A Pi container role's `tasks/` change pages the tasks
  channel once. Its red half is a handler without the direct `alert_deferred` call, the plan
  as first written, where the test fails. Its clean half, a `templates/` change of the same
  role, pages nothing.
- **The secrets-page tick test (D6.1).** A broad setup range carrying `ansible/vars/secrets.yml`
  pages the secrets alert once, on the success exit and on the failure arm. Its red half is
  a `handle_broad` tail without `alert_secrets_deferred`, where the test fails. Its clean
  half, the same range without `secrets.yml`, pages nothing.
- **The four landing tests (D6.1).** The red half of the first three is the tree before
  D6.1, where each landing waits on a `deploy.yml` receipt and never reaches `land-wait`.
  The fourth's red half is a `land-wait` that empties its list on a failed read.
- **The controller marker (D6.1b).** Its clean half is the captured no-change line, which
  draws no marker. Its red half is the matcher without the changed-object clause, which
  matches that line.
- **The break-glass case (D6.7c).** Its red half, `case=break-glass-red`, signs with a key
  the cluster does not hold, and the job fails unless source-controller refuses it.
- **The object-loss check without its exemption (D6.7b).** Its red half is a dropped
  Kustomization in the handback's shape with no trailer, which the exemption passed.
- **The `cs.flux` routing test (D6.4).** Every `ansible/roles/k8s/<role>/` path lands in
  `cs.flux`. Its red half is a k8s branch that adds nothing, which leaves the path in no
  set.
- **The lander's denylist source (D6.2a).** The agreement test's red half flips a fixture
  role's stance to `false` and requires the `--denylist` output to name it. The lander's
  tests refuse on empty output and on a non-zero exit, so a broken derivation cannot read
  as "nothing is denied".
- **The `owner: flux` census row (D6.3).** Its red fixture is a k8s entry without the flag.
  Its named members and its floor fail when the selector finds nothing.
- **The tag rule (D6.7b).** `_is_flagged` drops a tag whose role directory stays, drops
  `flux`, and repeats a refused drop in a later commit while `prod` has not moved.
  `_is_clean` drops a tag together with its directory.
- **The widened dropped-object arm (D6.7b).** A whole-tag removal without trailers is
  flagged, and the same removal with them is clean.
- **The PersistentVolume marker and the CRD guard (D6.7b).** The marker test fails on
  media-volume's PersistentVolume without the marker. The CRD guard's red fixture is a tag
  that renders a CustomResourceDefinition with `prune: true`.
- **The deletion drill (D6.7a).** Two red halves run in every drill. The PVC whose prune
  marker the drill stripped must be lost when its tag goes, and `keep-unmarked` must be lost
  when it leaves a tag that stays. Otherwise the job fails. A run that printed `skipped`
  proves nothing, so D6.7 requires a run that printed all three cases (failure class 3).

The deletions have red proofs of their own:

- Before D6.1, the exit 1 grep prints a count above zero, and after D6.4 it prints 0. At
  `70591c307` it matches 25 files besides `tasks/code.yml`, and exit 5's symbol grep matches
  18. A named PR deletes or edits each of them, as the round 10 review log lists.
- Exit 2 is the regression proof that the remaining deployer still applies the host plane. A
  setup-role change the tick fails to apply leaves a `manual_plane` line, which `probe.py
  gitops-state` prints.
- **D6.7 in the kind drill.** On D6.7a's PR, under `Orphan`, removing `prune-drill` from the
  bundle leaves the Deployment, the PVC and the Namespace. On D6.7's PR, the same removal
  deletes the Deployment and leaves the PVC and the Namespace with no deletion timestamp.
  Both PR bodies quote the job's output. No step of D6 needs the retired stage cluster or a
  drill on production.

## Rollback

- **D6.1 and the other self-applied deployer PRs (D6.2a, D6.4, D6.6).** These rewrite the
  live deployer, so a revert can fail to run.
    - **A crash.** If the new code fails at import or on a path the tests missed, the tick
      that would apply a revert cannot run. The operator applies the revert by hand on
      daniel-box from the reverted commit, with
      `uv run ansible-playbook ansible/initial_setup.yml --tags gitops_deploy > /tmp/d6-rollback.log 2>&1 < /dev/null`.
      Then `probe.py gitops-state` and the age of `last_run` confirm that the next tick ran.
    - **A partial apply.** The copy loop installs one file at a time, so an apply that fails
      partway leaves a mix of versions in `/opt/gitops-deploy`. For example, a new
      `deploy_changes.py` can sit beside an old `deploy_handlers.py`. The tick then runs
      that mix, which can crash. The same hand apply restores one version. This is failure
      class 5, a sequence treated as a transaction.
    - **A clean revert.** Restoring the k8s arms brings back code with nothing to act on,
      because every role is flux-owned and D6.0 found the ledger empty. A revert of D6.1
      also restores the two host-plane pages' old path through `alert_and_record_deferred`
      and `apply_broad_k8s`, so neither page goes silent in either direction. It restores
      `emit_deploy_annotation` and the old comment above
      `observability_deploy_annotation_expr` too. The emitter has no k8s service to mark,
      and `land-wait` keeps marking cluster deploys either way. The revert also restores
      `self_applied_playbooks`' `deploy.yml` term with the tick's `deploy.yml` apply,
      and the path-derived k8s tags, so the landing and the tick agree again. D6.1 writes no
      state on the host, so nothing else needs undoing.
- **D6.1b.** A revert removes the controller query. Markers then come from `land-wait`
  alone, as before.
- **D6.7c.** A revert removes the two drill cases, the public-key Secret, the source form
  and the scenario. While the cluster follows the in-cluster registry, revert nothing until
  the way back has completed.
- **D6.2a's lander change.** A revert restores the `config.env` line and the lander's read
  of it together, and the tick applies both `gitops_deploy` and `renovate_agent`. If the
  renovate_agent apply fails, the lander refuses every landing until it is applied, which
  fails closed. The hand apply is `uv run ansible-playbook ansible/initial_setup.yml --tags
  renovate_agent > /tmp/d6-lander.log 2>&1 < /dev/null`.
- **D6.3, D6.4 and D6.6** restore dead script code when reverted. The exception is D6.3's
  census row: reverting it also allows a handback again. D6.3 leaves `land-wait` and its
  annotation in place, so its revert changes no deploy marker. Before D6.4 merges, D6.3
  reverts alone. It restores `_narrowed`, the caller expansion and `k8s_remediation`'s
  two callers beside the changed set, and the join only widens `land-wait`'s list. Once
  D6.4 has merged, revert D6.4 first and D6.3 second. D6.4 deletes `k8s_remediation` and
  `shared_role_callers.py`, which D6.3's revert imports at module level, so a D6.3 revert
  on its own would stop every `land.sh` run at import.
- **D6.5a.** Revert it only while D6.5 is unmerged, or after reverting D6.5, because
  D6.5's arm for the box pages `NotReporting` once the box stops writing. The revert changes
  the key and a test, so the tick applies nothing. The timer's state variable
  (`kuma_check_state`, or its name after M6's C-3b) follows the list, so the box loses the
  timer once the session runs the same operator-attended `--tags setup_drift` apply as the
  forward PR.
- **D6.5 and D6.5b.** D6.5 removed host units, B1's six rules and the `.prom` files. To
  restore them, revert D6.5b first,
  then D6.5. The revert of D6.5b restores files that no task names at its head, so it lands
  a whole-role line, which the session clears by D6.5b's procedure. The revert of D6.5
  narrows to the six tags of `tasks/health-crons.yml`, which the session applies and clears
  as D6.5 does. Flux re-applies the six rules at the revert's merge, and the restored crons
  write the `.prom` files again on their next run. No token or tile comes back, because M6's
  B1 retired them and D6.5 never held them (#4609). D6.5 alone reverts cleanly while D6.5b is not
  merged, because the code is still in the tree.
- **D6.7a.** The job touches no cluster and no host, so a revert removes the job, the drill
  script and the fixture tree, and leaves nothing live. While D6.7 is live, the job is the
  only check that re-proves the default policy on a `flux_version` bump, so revert D6.7
  first.
- **D6.7b.** Its guards are inert under `Orphan`, so a revert removes checks and changes
  nothing live. Revert D6.7 first, because D6.7 relies on them.
- **D6.7.** Reverting it restores `Orphan`. A tag removed while D6.7 was live has already lost
  its objects, and a revert does not bring them back. Re-adding the tag re-creates them, and
  the PVCs, Namespaces and PersistentVolumes survive.
- **D6.1's rename of the render-diff gates.** A revert of D6.1 restores arm 1 and the old
  module name. The kept arms work under either name.
- **The handback is gone as a rollback.** From D6.3 on, a D5 role PR does not revert. A bad
  role change reverts its own commit through `promote`, or holds through `DEPLOY_HOLD`.
- **D6.8b.** An accepted ADR is never rewritten. A reversal is a new ADR that supersedes this
  one, as `docs/adr/index.md` says.

## Risks

Each risk names the failure class from `docs/failure-classes.md` where one fits.

- **A kept module still imports a deleted one** (class 2: a green suite stands in for "every
  entry point imports"). The lazy imports listed under *Design* fail only on the path that
  reaches them. Mitigation: the import grep per PR, quoted in the body, and the merge order
  above.
- **A host-plane page rides a deleted k8s module** (class 3: empty is read as
  clean). `deploy_k8s_owed.py` and `deploy_broad_k8s.py` carry the tasks page and the
  broad-range secrets page. Deleting them with their call sites would ff-merge a Pi role's
  `tasks/` change, or a rotated secret beside a setup change, with no page. D6.5 is such a
  range. Mitigation: D6.1 moves the three calls into `deploy_handlers.py` first, and its two
  tick tests pin both pages. D6.5 no longer changes a secret, so its merge tick checks
  nothing here (#4609).
- **A dashboard with no deploy markers looks like a quiet week** (class 3: empty is read as
  clean). `test_deploy_annotations.py`'s docstring names this: a query that matches
  nothing renders like one with no deploys. After D6, a cluster deploy draws a marker only
  through `promote.py land-wait`, so a promotion that no `land.sh` follows draws none (D2
  *Risks*). D6.3 deletes D2's `tag_owners` split in `land_lib/deploy.py`, beside the path
  that reaches the promotion wait. Mitigation: D6.3 keeps
  `land-wait` and its annotation and quotes D2.3's settled-landing test passing.
  *Verification* reads one `source=promote` line after D6.3's first settled landing, and
  after D6.4 one each for a direct role PR and a broad PR.
  *Grafana's deploy markers after D6* records the remaining gap, and a cross-slice note asks
  design.md to state it.
- **A broad PR hands `land-wait` no tag** (class 3: empty is read as clean). From D6.1 the
  tick applies no `deploy.yml`, and D6.3 and D6.4 delete the shared-role expansion,
  `narrow` and the path map's k8s lookups. With no replacement, a template or shared-role
  PR reaches `land-wait` with an empty list, so no health gate runs and no marker is
  written while `promote` applies the change. The same holds for a PR that moves only an
  edge, a deferred apply or a shared kind, which no `cluster/<tag>/` comparison sees.
  Mitigation: D6.1 makes the changed set the source, with four landing tests, D6.3 and
  D6.4 each add one, and each PR quotes all of them. The footprint covers every bundle file
  of a tag, each shared kind has a stated rule, and a path that no rule claims lands under
  `_unclassified`, which widens to every name. A failed read widens the list to every
  flux-owned tag rather than emptying it.
- **The changed set depends on GHCR and on the package staying public** (class 3 if a
  failed read were taken as empty). `land-wait` reads both `digests.json` files through
  GHCR's anonymous pull token, which works only while D1 Open question 2 keeps the package
  public. Mitigation: a failed read widens to every flux-owned tag and says so on the
  verdict line, and D6.1's fourth landing test pins it.
- **An excluded index file hides a real change.** C1 leaves `flux/kustomization.yaml`,
  `digests.json` and `images/` out of every footprint. Mitigation: each of the three only
  mirrors files that do count. The index lists `flux/<tag>.yaml` files, and a consumer's
  image ref sits in its own `cluster/<tag>/`. A change that reaches a Kustomization
  therefore moves a counted file too.
- **A stale repo bundle reverts the cluster** (class 5). Item 8's bundle can predate
  `prod`, and a break-glass render from it would prune and revert everything since.
  Mitigation: the runbook's ancestry check against the OCIRepository's revision, with a
  surviving host checkout as the source when the bundle fails it.
- **The break-glass private half is a second signing identity.** Mitigation: the operator
  holds it only in the kit, password-protected. The production OCIRepository verifies
  keyless against the `promote` identity alone, so the cluster accepts a key-signed
  artifact only after the operator re-points the source by hand.
- **A k8s path falls into the setup branch after D6.4** (class 3). Deleting `cs.k8s` without
  a destination would hand a k8s path to the branch that handles setup paths. Mitigation:
  D6.4 routes it to `cs.flux`, and the routing test's red half proves the test can fail.
- **The self-applied deployer change crashes, or applies partway** (class 5). Mitigation:
  D6.1's pre-merge import of the laid-out code, the watched first ticks, and the hand path
  under *Rollback*.
- **A hold or a k8s ledger line outlives its writer.** It would block the setup plane, or page
  with no clear path left. The last writer is the old code that runs the tick fast-forwarding
  D6.1. Mitigation: D6.0's precondition, D6.1's lack of a deploy-plane path, the repeated
  check after D6.1's first tick, and the repeated check just before D6.6 removes the last
  `clear-owed` path for the k8s classes.
- **A `manual_plane` line that the named clear leaves in place.** A refused narrowing records
  the whole role, a clear with `--applied` then keeps the line, and GitOps Deploy — Status
  pages later. Mitigation: D6.5's narrowing gate before merge, the apply taken from the line
  as printed, and a clear that must print `cleared k3s`. D6.5b's line is whole-role by
  construction, so D6.5b verifies the host state and clears it bare, operator-attended, and
  never runs `--tags k3s`.
- **The box loses its host-plane drift detector** (class 3: no reader reads as no drift).
  Mitigation: D6.5a arms the setup-drift reader before D6.5 disarms manifest-prune-check,
  D6.5 adds the box's arm to B2's setup-drift rules, and D6.5's stamp fragments go with their
  tasks.
- **A merged deletion breaks a unit that runs code from the primary checkout** (class 5).
  The release-staleness cron runs `probe.py releases` from the checkout the tick
  fast-forwards. Mitigation: D6.5 removes the cron and its hand apply runs before D6.5b
  deletes the code.
- **A setup-drift rule without an `origin` matcher lets one host mask the other** (class 2).
  daniel-server's sample would satisfy a host-free `NotReporting` while the box wrote none.
  Mitigation: D6.5 adds a separate arm per host, each naming its `origin`, with a promtool
  case in which only daniel-server reports.
- **A tag leaves the bundle by accident after D6.7** (class 5: a revert or a rename is not a
  transaction). Mitigation: D6.3's census row, the tag rule's publish arm against `prod`, the
  widened dropped-object arm, and the prune exemptions. The kind drill proves the PVC and
  Namespace exemption on both deleting paths before D6.7 merges.
- **Deleting the `flux` Kustomization cascades through the Flux CRDs** (class 5). Under the
  default policy it deletes every Kustomization and OCIRepository, and each finalizer may
  delete its tag's workloads before kustomize-controller stops. Mitigation: `flux` keeps
  `Orphan` as a named member of the renderer test, and the tag rule refuses any bundle that
  lacks it.
- **Deleting `flux-deferred` cuts Flux off from its source** (class 5). It holds the
  OCIRepository and the root, and recovery is the **operator-only** runbook. Mitigation:
  every `-deferred` keeps `Orphan`, `flux-deferred` as a named member.
- **A `-deferred` leaves while its tag stays.** Under the default policy its finalizer would
  race `<tag>`'s adoption, opening a deny window for netpol-baseline or a DNS gap for pihole.
  Mitigation: `-deferred` keeps `Orphan`. The cost is that a retired tag leaves its deferred
  objects, which the retirement procedure removes first.
- **A rename or a render bug deletes a static PersistentVolume** (class 5). media-volume's
  PersistentVolume is not prune-exempt under C3 as written, and `pv-protection` keeps a
  deleted bound volume terminating, which no re-apply undoes. Mitigation: D6.7b stamps the
  prune marker on PersistentVolumes and StorageClasses once the C3 amendment lands. Without
  the amendment, the tag rule refuses to drop a tag that renders a PersistentVolume (Open
  question 12). D6.7 merges only after one of the two is in place.
- **The Renovate lander loses its denylist source** (class 3: empty is read as clean). D6.2a
  deletes the `config.env` line that the armed lander reads. Mitigation: the same PR
  repoints the lander at the checkout's derivation, and both the old and the new lander
  refuse on a missing or empty list rather than read it as clean. Between the two role
  applies, the lander refuses for a tick and lands nothing. *Verification* reads the verdict
  files after the next agent run.
- **A shared kind loses `Orphan`.** An enumerated keep-list in D6.7 would have left
  `cluster-vars` under neither renderer test, and a later drop of it would delete
  `homelab-cluster-vars`, which every substituting tag reads. Mitigation: the kept set is
  every Kustomization but the per-tag `<tag>`s, the narrowed test iterates `SHARED_KINDS`,
  and `cluster-vars` is a named member.
- **A tag with CRDs comes under the default policy.** Mitigation: D6.7b's renderer test fails
  on a CRD in a tag other than `flux` that renders `prune: true`.
- **The drill's cluster is not production** (class 2: the check observes a proxy, not the
  property). kind differs from k3s, and its default class is not Longhorn. Mitigation: the
  drill exercises only kustomize-controller and the renderer, which run unchanged in kind.
  The Longhorn half follows from `reclaimPolicy: "Delete"`, which acts only on a deleted PVC.
  The drill asserts on the PVC's deletion timestamp, never on its `Bound` phase. kind's
  default class binds only a mounted PVC, so every fixture PVC has a pod that mounts it, and
  the revision-1 readiness wait fails the run if one does not bind.
- **The drill skips and reads as green** (class 3: empty is read as clean). The job is a fast
  green on a PR that touches nothing it scopes. Mitigation: it prints `skipped`, D6.7's PR
  body must quote a run that printed all three cases, and a dispatch always runs the drill.
- **The drill's source is unsigned** (class 2). Production verifies the artifact's
  signature, and the drill does not. Mitigation: verification gates only acceptance, and the
  deleting paths the drill proves run after acceptance and never read the signature.
- **A deleted check was the last detector for a failure class.** Mitigation: the edit to
  class 5's row in D6.5b, which `test_failure_class_detectors.py` forces.
- **Root `CLAUDE.md` describes a deployer that no longer applies k8s** (class 8). Mitigation:
  D6.1 and D6.3 edit the sentences their behaviour changes falsify, and D6.8a rewrites the
  section in full.
- **Docs and facts drift** (class 8). Mitigation: each PR edits the docs pages and the
  `CLAUDE.md` sections that describe what it deletes. `vanished-identifier` fails a commit
  that leaves a deleted name in a section.
- **Coordination with G5 and R4.** All three edit root `CLAUDE.md` and the manifests role's
  docs. Mitigation: D6.1 and D6.3 edit only the sentences their behaviour falsifies, as the
  sections stand when each runs. D6.8a runs after G5 if G5 has started, and R4 starts after
  D6.8a.
- **ADR numbers collide.** D3 or H4, G2 and D6 all write ADRs. Mitigation: take the number
  when the PR merges, not when it is drafted.

## Tests and docs that change in the same PR

- **D6.1:**
    - the four landing tests in `scripts/deploy_tools/tests/`, and the removal of D3.4's
      interim `cluster-vars` rule with its test;
    - the gitops_deploy role's `CLAUDE.md` (*Safety*, *Autonomous-role contract* and *Which
      apply clears a hold*), with their `facts.lock` rows;
    - in `docs/gitops-pipeline.md`, *Broad changes* and *A deploy-plane change is narrowed
      before it is applied*;
    - in `docs/landing.md`, the cross-plane gate, which now describes the render-diff gates
      without arm 1, and in `docs/flux-deploy-plane.md`, the module's new name;
    - root `CLAUDE.md`'s *After a PR Merges — Pull, Deploy, Verify* and *When to wait*, with
      their `facts.lock` rows;
    - the deleted tests under `ansible/roles/setup/gitops_deploy/tests/` and
      `ansible/tests/deploy/`, arm 1's tests in `scripts/validate/tests/`, and the kept arms'
      tests renamed with the module;
    - `scripts/deploy_tools/tests/test_reach.py`, for `Reach.touched`;
    - `ansible/tests/deploy/test_denylist_parsers_agree.py`, whose import follows
      `declared_denylist` into `deploy_phases.py`;
    - the two tick tests in `test_gitops_deploy_main_branches.py`, the promotion half of
      D2's `cs.flux` test, and the deployer half of
      `ansible/tests/deploy/test_deploy_annotations.py`, with its docstring;
    - the comment above
      `ansible/roles/k8s/observability/defaults/main.yml:observability_deploy_annotation_expr`,
      which names the two emitters that remain (*Grafana's deploy markers after D6*);
    - the regenerated monitor-bridge copy of `gitops_markers.py`.
- **D6.1b:** the marker pair in `ansible/tests/deploy/test_deploy_annotations.py`,
  `scripts/grafana/inject_dashboard_annotations.py` and its test, and the comment above
  `observability_deploy_annotation_expr`.
- **D6.2a:**
    - the deployer-denylist tests, and the `denylist_rendered` row of
      `test_deployer_state.py`;
    - `test_land_renovate_pr.py`, for the lander's new source and its two refusals, and the
      agreement test in `test_renovate_automerge_follows_the_autodeploy_denylist.py`;
    - the renovate_agent role's `CLAUDE.md` *Scope / exclusions*, with its `facts.lock` row,
      and the lander paragraph of `docs/renovate-agent-bounds-and-digest.md`;
    - the import lines of the Renovate tests and R4's gate tests;
    - `scripts/lib/tests/` for the moved functions;
    - the gitops_deploy role's `CLAUDE.md` *Safety*;
    - the regenerated monitor-bridge copy of `gitops_markers.py`.
- **D6.2b:** the 62 defaults comments, the tests of the two generators' Ansible-owned
  branch in `scripts/docs/tests/`, `docs/renovate-agent-bounds-and-digest.md`, and the two
  skills. No glance block changes.
- **D6.3:**
    - `docs/landing.md` and `docs/deploying.md`;
    - the `land-after-merge`, `deploy` and `gitops-tick` skills;
    - the tests in `scripts/deploy_tools/tests/` for the deleted arms;
    - the census row's fixtures;
    - the fallback landing test in `scripts/deploy_tools/tests/`;
    - root `CLAUDE.md`'s *When to wait*, with its `facts.lock` row.
- **D6.5a:**
    - the `group_vars` comment and `test_setup_drift_check.py`;
    - the initial_setup role's `CLAUDE.md` *Notable*;
    - `docs/host-baseline-record.md`'s setup-drift section.
- **D6.5:**
    - the k3s role's `CLAUDE.md` (*Autonomous-role contract*, *Notable*, and its generated
      `## At a glance` block), with their `facts.lock` rows;
    - `docs/k3s-node-plane-crons-and-incidents.md`, for the units it removes;
    - `scripts/secrets_mgmt/` tests for `CROSS_HOST_PUSH_TOKENS`, `ansible/secret_rotation.yml`
      and D3's cluster file.
- **D6.5b:**
    - the k3s role's `CLAUDE.md` *Layout*, with its `facts.lock` row;
    - `docs/k8s-manifest-cycle-record.md`;
    - the render_records role's `CLAUDE.md`, which names `probe.py releases` as its reader;
    - `docs/failure-classes.md`;
    - the tests in `ansible/roles/setup/k3s/tests/`, `ansible/tests/k8s/` and
      `scripts/diagnostics/tests/`.
- **D6.4:** the tests in `scripts/deploy_tools/tests/` for the deleted modules,
  `test_setup_cross_role_files.py`, D2's `cs.flux` test rewritten as the routing test, and
  `ansible/roles/setup/common/CLAUDE.md`.
- **D6.6:**
    - `docs/monitor-bridge-checks.md`, `docs/monitor-bridge-internals.md` and
      `docs/claude-tooling.md`;
    - in `docs/gitops-pipeline.md`, *The `k8s_deferred` and `k8s_unapplied` markers, in full*;
    - the monitor-bridge, deploy_ui, renovate_agent and `.claude/hooks/tests/` tests.
- **D6.7a:** the workflow, the drill script and the three-tag fixture tree. In
  `docs/flux-deploy-plane.md`, a paragraph that names the job, what it proves and its limit.
- **D6.7c:** the drill's two cases, the flux role's Secret and source form with their
  renderer test, the flux role's `CLAUDE.md`, `docs/break-glass.md` item 8 and its
  scenario, and the drill paragraph in `docs/flux-deploy-plane.md`.
- **D6.7b:**
    - the tag rule's tests beside `promote.py` and beside `render_diff_gates.py`;
    - the object-loss check's fixture beside `promote.py`;
    - the dropped-object arm's fixtures;
    - the renderer tests for the PersistentVolume and StorageClass marker and for the CRD
      guard;
    - `docs/landing.md`, for the tag rule's pull-request arm.
- **D6.7:**
    - `docs/flux-deploy-plane.md`: the one-step retirement, the deferred objects' first step
      and the rename procedure;
    - D5.1a1's renderer test for `Orphan`, narrowed to the kept set over `SHARED_KINDS`
      with `flux`, `flux-deferred` and `cluster-vars` named, and the renderer test that no
      `<tag>` but `flux` carries `Orphan`.
- **D6.8a:** root `CLAUDE.md`, `docs/facts.lock`, and `docs/archive/` for the moved sections.
- **D6.8b:** the ADR, ADR-0010, `docs/adr/index.md` and the `mkdocs.yml` nav.

## Complexity delta

- **Removed:** about 7.4k lines of code at `70591c307` counts, plus the tests that read only
  that code:
    - the deployer's k8s arms, about 2.1k with `digest_provable.py`;
    - the deployer's denylist, about 300 net, after about 120 lines move to
      `scripts/lib/k8s_roles.py`. The Renovate lander's switch to the checkout's derivation
      changes about 60 lines and adds no module;
    - the k8s arms of `land.sh` and `deploy.sh`, about 700;
    - the drift checks, `manifest_declares.py`, and the `releases` family, about 2.7k;
    - reach and narrowing, about 1.5k;
    - the k8s ledger readers, about 350.

  Also removed: one hold (`hold_sha` for k8s), the k8s owed ledger, the handback, the
  two-step retirement, the handback exemption of D2.6c's object-loss check, and the
  deployer's `deploy-annotation` emitter. That leaves `deploy.sh`'s emitter for the Pi,
  D2.3's `land-wait` for a landing and D6.1b's query for every apply.
- **Added:** about 80 lines in D6.1 for `land.sh`'s k8s list, tests included. Every
  landing reaches `land-wait`, whose changed set replaces the path-derived k8s tags. It
  replaces about 900 lines of tag derivation that D6.3 and D6.4 delete. The footprint
  classification lands in D2's renderer and the reads in D2.3's `land-wait`, so D6 adds no
  render on the host. The `--moved-tags` mode it replaces was about 240 lines.
- **Kept, against the earlier count of this plan:** `land_reach.py` 563, `prune_releases.py`
  123, four `narrow_lib` modules (550), and most of `deploy_cross_role.py`. All of them serve
  the host plane until H7. D4's and D5's render-diff gates also stay, renamed, less arm 1 and
  the image-only exception, because their subjects outlive D5.
- **Added:**
    - one ADR;
    - the checks listed under *Each new check can go red*, each with a red proof: the
      inventory routing test, the two tick tests for the tasks and secrets pages, the
      `cs.flux` routing test, the `owner: flux` census row, the tag rule's two arms, the
      widened dropped-object arm, the PersistentVolume marker test, the CRD guard and the
      kind deletion drill;
    - about 60 lines in `deploy_handlers.py`: three direct `alert_deferred` calls and the new
      tail of `handle_broad`, which replace the paths through the two deleted modules;
    - one GHCR pull per publish, for the tag rule's reference bundle;
    - the drill's CI job, about 230 lines with its script and fixture tree. It runs only on a
      PR that touches the renderer, its fixture tree, the flux role's defaults or the
      workflow, or on a dispatch, and costs one kind cluster per such run;
    - daniel-box's setup-drift timer and its arm in B2's two rules, which replace a reader
      D6 deletes;
    - D6.1b's marker query over kustomize-controller's log, about 80 lines with its
      fixtures, and no emitter;
    - D6.7c's break-glass promotion, about 190 lines: a runbook scenario, a public-key
      Secret and a second source form in the flux role, and two drill cases. It adds one
      operator-held key pair and keeps the in-cluster registry in service;
    - two size bounds in exit 6, read once at D6's last PR;
    - one exception among the per-tag Kustomizations, `flux`. Every other Kustomization,
      every `-deferred` and every shared kind among them, keeps `Orphan`.

## Open questions

1. **Does `deploy_ui` retire with the deployer?** It starts `land.sh` and `deploy.sh` and
   clears `hold_sha`. After D6 it serves the host plane only, and D2's open question 4 asked
   whether it should clear `DEPLOY_HOLD`. Resolution: the operator decides in D6.3's review
   (**operator-only**). The design's open question 4 names this.
2. **Do ADR-0011, ADR-0012 and ADR-0017 need amendments?** `gitops-deploy.service` still takes
   the tree lock for the host plane, and `deploy.sh` still takes it for the Pi. design.md
   leaves the amendments to the operator, and R4's open question 8 asks the same about 0012
   (**operator-only**).
3. **Does the in-cluster registry retire?** This is D5's open question 2. Resolution: D6.6
   counts its remaining consumers. With none, a retirement PR follows D6, outside this slice.
   From D6.7c, break-glass promotion is a standing consumer, so the registry stays, and
   D6.6's count names it.
4. **Does the tick outlive D6?** Yes. The tick's setup arm stays until H7, and D6 deletes no
   tick. Resolution: recorded here. design.md's slice index says "The deployer is gone"
   against D6, which needs a correction (cross-slice note).
5. **Who ships the timer that replaces the deployer's fast-forward?** D6 ships none, because
   the deployer and its fast-forward stay. H7 ships `homelab-checkout-sync` when it retires
   the deployer, in its section *The deployer retires last, and checkout-sync replaces its
   fast-forward*. Resolution: recorded here.
6. **May a deletion PR exceed the contract's 100–200 lines?** D6.1, D6.4 and D6.5b do.
   Resolution: a contract question. contracts.md's *Slice document format* either records an
   exemption for pure deletions, or D6 splits those PRs by module, at the cost of more
   self-applied deployer changes. contracts.md's *Requested amendments not yet applied*
   lists the request under *Requests on the slice document format*, *Deletion PRs*, with
   D6.5b, and names no gate.
7. **Does D6.7 merge, and which Kustomizations does it switch?** That depends on the count of
   handbacks, retirements and renames from D2 to D5. Resolution: D6.7's PR body records the
   count, and the operator decides in its review whether D6 ends with `Orphan` instead. If
   D6.7 merges, it switches every `<tag>` except `flux`. Every other Kustomization keeps
   `Orphan`: `flux`, every `<tag>-deferred` and every shared kind, `cluster-vars` among them,
   for the reasons in *The deletion policy, and the end of the handback*. C3's
   deletion-policy line says "D6.7 switches the per-tag Kustomizations", which reads as
   covering `-deferred` and `flux`. A cross-slice note asks for C3 to name these exceptions,
   and for a row in contracts.md's *Requested amendments not yet applied* with the gate
   "before D6.7 merges". The register lists it as *The deletion policy's kept set*, under
   *Requests on C3*. D6.7's *contract gate* step holds the merge until the amended C3 line
   exists, because the row alone leaves C3 as written. D6.7's operator-only step records
   the operator's decision in the PR thread before `land.sh` runs.
8. **Does daniel-box get its own setup-drift tile?** Resolved (#4632): no. Kuma is gone
   before D6 opens, and M3's per-host series makes a second tile and its token
   unnecessary. D6.5 gives the box its own arm in B2's two rules instead.
9. **Does G2 retire `docs/facts.lock` before D6?** No dependency edge orders them. Resolution:
   each PR runs `fact_status.py verify` only while the lock exists.
10. **Does `flux-deletion-drill` become a required check?** As a required check, it would
    hold a `flux_version` bump that changes deletion behaviour, not only D6.7. Resolution: the
    operator decides in D6.7a's review (**operator-only**, a GitHub settings change). The job
    scopes itself, so a required check never sticks at "Expected".
11. **Can the drill reach the renderer's prune stamping and Kustomization writer as
    functions?** C1 defines only the CLI, which renders the repo's own tree. Resolution: the
    drill imports both steps from the renderer's library, which leaves C1 unchanged. A
    cross-slice note asks R1 and D5 to keep each step a function that takes a manifest list
    or a tag. If either is inline when D6.7a starts, D6.7a factors it out first.
12. **Does C3's prune exemption cover PersistentVolumes and StorageClasses?** C3 names only
    PVCs and Namespaces. Under the default policy, a deleted `media-volume` Kustomization
    deletes its static PersistentVolume, and `pv-protection` leaves it terminating where no
    re-apply restores it. Resolution: amend C3's *Prune exemptions* line to "PVCs,
    PersistentVolumes, StorageClasses and Namespaces carry
    `kustomize.toolkit.fluxcd.io/prune: disabled`". Gate: before D6.7b merges. Without the
    amendment, D6.7b's tag rule instead refuses any removal of a tag whose render at `prod`
    holds a PersistentVolume, and the rename procedure excludes media-volume. A cross-slice
    note asks for this amendment's row in contracts.md's *Requested amendments not yet
    applied*, with the gate and the fallback named, because nothing else tracks it. The
    register lists it under *Requests on C3*, *Wider prune exemptions*, and D6.7b's
    *contract gate* step holds the merge until that row or the amended C3 line exists.
13. **Withdrawn: what gates the commit input before D6.1?** C1's footprint digests replaced
    D6.1's `--moved-tags` mode, so D6.1 renders nothing and needs no commit input. D6.0
    checks `digests.json` instead. A cross-slice note tells the register that the row *The
    commit input* can gate on R4's PR 1 alone.
14. **Does a parked range still name a full `deploy.yml` run after D6.1?** At `70591c307`,
    `deploy_remediation.py:broad_remediation` appends `ansible-playbook ansible/deploy.yml`
    whenever `broad_deploy` is set, and `deploy_defer.py` keeps calling it after D6.3. After
    D6.1 a deploy-plane path applies nothing, yet the park alert would still tell the
    operator to run a full `deploy.yml`. Resolution: D6.1 drops that command from
    `broad_remediation` or gates it, and the Pi half can reuse the
    `deploy.sh -e target=daniel-pi` line that D6.3 prints. D6.1's review also decides whether
    the whole `initial_setup.yml` clause is still the right advice. R4's PR 6 tells the
    operator to run none of the alert's commands, so R4 does not depend on the answer.
15. **How does the `soak` rule become a rule over one render?** *What a footprint counts*
    says that `soak` counts for every flux-owned tag only when no tag's own files moved.
    That condition compares two bundles, and the renderer writes one bundle's digests. The
    soak Job also embeds the commit it verifies, so `soak`'s footprint differs on every
    commit, and a rule that fanned it out to the tags would widen every landing.
    Resolution: the rule's text stays as written until the contracts owner, D2 and D5
    restate it over one render. The gate is D5.3b2, which adds `soak` to `SHARED_KINDS`
    with its footprint rule (C3). A cross-slice note carries it.
16. **Does source-controller verify a key-signed artifact from a plain-HTTP registry?** The
    drill's `registry:2` serves plain HTTP, and the in-cluster registry may too. Whether
    source-controller's cosign check reads the signature through `insecure: true` is
    upstream behaviour that this plan did not reproduce. Resolution: D6.7c's
    `case=break-glass` answers it on kind. If it fails, the runbook requires TLS on the
    in-cluster registry, and the drill serves TLS too.
17. **How does break-glass promotion bring up a fresh cluster offline?** H7 Open question
    14 records that H7's offline bring-up needs the in-cluster registry applied before the
    artifact push, and the Flux controller images from the break-glass kit rather than
    GHCR. Resolution: D6.7c's runbook names both steps, and the kit carries the controller
    images at `flux_version`, refreshed with item 8's bundle. The drill gains a
    fresh-cluster case once H7 Open question 14 rules that the offline bring-up runs
    through this runbook. Until then, D6.7c's two cases cover a running cluster only.

## Review log

### Round 1 (per-slice adversarial review)

Each finding below is listed with its severity, its verdict and what changed. The
investigation at `70591c307` also turned up four defects the review did not raise. They are
listed after the findings.

- **D6-R1-01 (critical): D6.1 deleted `deploy_cross_role.py` whole, though it holds the
  setup routing tables.** Fixed. The Baseline classifies the module as mixed and names its
  setup readers. D6.4 deletes only `K8S_ROLES_IMPORTING_SETUP_FILES` and
  `k8s_lookup_readers`. Every PR quotes the import grep, and the complexity delta is
  corrected.
    - **Deviation from the required fix:** `k8s_lookup_map` stays.
      `scripts/secrets_mgmt/consumers.py:_census_corpus` calls it for the secret-consumer
      census.
    - The deletion moves from D6.1 to D6.4, because `scripts/deploy_tools/reach.py` reads
      both symbols until then.
- **D6-R1-02 (critical): D6.3 and D6.4 deleted `land_reach.py` and `reach.py`, which the
  setup and Pi arms use.** Fixed. `land_reach.py` stays until H7. `reach.py` loses only its
  k8s readings in D6.4. The import grep covers every kept module.
- **D6-R1-03 (critical): deleting the denylist plugin broke setup routing and the Renovate
  test.** Fixed.
    - D6.2a moves `is_leftover_dir` and the denylist derivation into
      `scripts/lib/k8s_roles.py`, and repoints `_autodeploy.py` and
      `k8s_autodeploy_counts.py`.
    - The Baseline lists every reader of the key and the plugin, and *Design* gives each
      reader's fate.
    - `SETUP_ROLES_CALLING_FILTER_PLUGINS` loses the plugin's row.
- **D6-R1-04 (high): three `narrow_lib` modules called deploy-plane are setup imports.**
  Fixed, and widened. `filters.py` imports `templates.py`, so four modules stay with setup
  narrowing. Only `broad.py` and `containers.py` go. The Baseline records the import grep,
  and the census and the delta are recomputed.
- **D6-R1-05 (high): deleting manifest-prune-check left daniel-box with no host-plane drift
  detector.** Fixed in D6.5a. That PR also covers the arming test, the tile and the shared
  token, and D6.5 removes the four stamp fragments.
    - **Correction to the required fix:** the tick does not apply a `setup_drift_check_hosts`
      edit through `initial_setup`.
      `deploy_changes.services_from_changed_paths(['ansible/inventory/group_vars/all.yml'])`
      returns `broad=True, broad_deploy=True` with no setup role at `70591c307`. D6.5a
      therefore names a hand apply of `--tags setup_drift`.
- **D6-R1-06 (high): deleting D2's skips put flux tags back into the old paths.** Fixed by
  the alternative the review offered.
    - The skips in `render_targets.py` and `secret_rotation.py` stay, until R4 deletes
      `render_targets.py` and H1 removes `containers_list`.
    - `health.py:release_expected_restarts` goes in D6.5 with the `releases` family it
      imports.
    - D2.4's test for the `secret_rotation` skip stays.
- **D6-R1-07 (high): the rollback assumed that the new deployer code runs.** Fixed.
  *Rollback* names the operator-attended hand apply, and D6.1 watches the first three ticks.
  The absent task names the four modules, and the partial-apply paragraph covers a mixed
  `/opt`.
    - **Deviation from the required fix:** the absent task stays until H7. The tree keeps the
      same pattern in "Remove deployer modules folded into another".
- **D6-R1-08 (high): no precondition on held SHAs or k8s ledger lines.** Fixed by D6.0. The
  Goal states that after D6 `hold_sha` covers the host plane only, and `DEPLOY_HOLD` covers
  the cluster.
- **D6-R1-09 (high): D6.5 dropped three tokens without the operator-only `secrets.yml`
  removal.** Fixed. D6.5 marks the `sops` step **operator-only**, then runs
  `secret_rotation.py sync`, all on one branch so that CI stays green.
- **D6-R1-10 (high): after D6.7, a revert of a D5 role PR deletes live objects.** Fixed.
    - The Goal and *Rollback* state that D6 retires the handback.
    - D6.3 adds the `owner: flux` census row, and D6.7 adds the tag rule, records the count
      of handbacks, retirements and renames, and gains a stage proof.
    - Risks name renames and reverts.
- **D6-R1-11 (medium): `releases.py` went before `health.py`, and the sibling modules were
  orphaned.** Fixed. D6.5 deletes all six modules together with
  `release_expected_restarts`. D6.0 requires that `render_records` is disarmed, and the
  cross-slice note to D5 repeats R4's open question 5.
- **D6-R1-12 (medium): D6.1 omitted about ten files that reference the deleted modules.**
  Fixed.
    - The Baseline lists them, and each PR names its files and an approximate line count.
    - `discharge_owed_k8s` moves into D6.1, and `deploy_release.py` goes in D6.1.
- **D6-R1-13 (medium): the ledger readers outside monitor-bridge were missing, and two
  monitor-bridge modules are generated copies.** Fixed by D6.6. It edits the sources, runs
  `gen_gitops_markers.py`, lists every reader, and relies on the re-apply through
  `SETUP_FILES_SHIPPED_BY_OTHER_ROLES`. D6.0 drains the ledger before any reader goes.
- **D6-R1-14 (medium): the ADR stance contradicted design.md, R4 and H7.** Fixed as Open
  questions 4 and 5. Part of the evidence no longer holds at the time of this round:
    - R4's review log already fixed "nothing applies a setup role after D6" (R4-H4).
    - H7's section *The deployer retires last, and checkout-sync replaces its fast-forward*
      already ships the timer in H7.19a.
    - design.md's index row remains, and goes to the cross-slice notes.
- **D6-R1-15 (medium): the failure classes were mislabelled, and the class-5 detector was
  missing.** Fixed. Classes 2, 3, 5 and 8 are named correctly. `manifest_declares.py` and its
  test are in D6.5, which edits class 5's row.
- **D6-R1-16 (medium): `facts.lock` was re-verified only in D6.8.** Fixed. Each PR verifies
  the recorded sections it edits, and the Baseline lists them. Open question 9 covers G2.
- **D6-R1-17 (medium): exit 5 could not be checked.** Fixed. Exit 5 is now a `git ls-files`
  absence check, a per-symbol grep and the import grep.
- **D6-R1-18 (medium): the route of `_BROAD_DEPLOY_PREFIXES` paths was undefined.** Fixed.
  *The route of a deploy-plane path after D6* gives the route, and D6.1 adds the deployer
  test. This also answers R4's open question 7.
- **D6-R1-19 (medium): rewording the key regenerates 57 glance blocks, which the diff size
  omitted.** Fixed. D6.2b is a separate PR for the wording, with its real size.
- **D6-R1-20 (medium): D6.5's hand apply left `manual_plane` uncleared and endangered shared
  tasks.** Fixed. D6.5 and D6.5b name their `clear-owed` commands. Only drift-only tasks
  become `absent`, shared tasks lose a tag, and the four stamp fragments are named.
- **D6-R1-21 (medium): `is_image_only_diff` went before the cross-plane gate.** Fixed. D6.1
  deletes the gate.
- **D6-R1-22 (medium): PR sizing departed from the contract, and D6.8 tied docs to the ADR
  approval.** Fixed. Each PR states a size, Open question 6 records the exemption as a
  contract question, and D6.8 splits into D6.8a and D6.8b.
- **D6-R1-23 (medium): the same-PR docs list missed pages.** Fixed. Each page appears under
  the PR that removes its behaviour.

The defects the investigation found beyond the review, each with its evidence:

- **`prune_releases.py` is host-plane.** The plan deleted it in D6.4, but
  `ansible/roles/setup/common/tasks/release_bin.yml` ships it into every host-script release
  group. It stays.
- **`land_lib` imports the reach scripts at top level.** `land_shared.py` imports
  `shared_role_callers`, and `land_lib/tools.py` imports `shared_role_reach` and
  `land_rerolls`. D6.3 removes those imports before D6.4 deletes the modules.
- **`probe_lib/releases.py` imports `narrow_lib/broad.py`.** So D6.5 merges before D6.4.
- **D6.5's absent pass must remove the stamp fragments.** Otherwise the box's new setup-drift
  reader reports drift for sources that are gone, as
  `test_a_source_deleted_from_the_repo_is_drift_not_an_exemption` shows.

### Round 2 (integration review)

One finding was raised against this slice. It is fixed for D6, and the investigation found
one more defect, listed after it.

- **X-08 (high): R4, D6 and H2 rest pre-merge proofs on a stage cluster that was retired on
  2026-09-29.** Fixed for D6. The finding holds at `70591c307`:
  `docs/archive/staging-cluster.md` opens "ARCHIVED 2026-09-29 (#2941). The staging cluster
  is retired", and `scripts/lib/kubectl.py:RETIRED_CLUSTER_NODES` names only the retired
  node. No workflow in `.github/workflows/` creates a cluster.
    - D6.7a adds `flux-deletion-drill`, a CI job that runs the drill in a throwaway kind
      cluster. It replaces "D6.7 on stage" in *Verification*. *The deletion drill runs in a
      throwaway kind cluster* gives the design.
    - The drill builds its fixture bundle through the renderer's library, not the CLI,
      because C1's CLI renders only the repo's tree (Open question 11). It removes the tags by
      pushing a bundle without their `flux/<tag>.yaml` files, so the root Kustomization
      prunes them as on production.
    - The drill asserts on the PVC's deletion timestamp, not on its `Bound` phase.
      `pvc-protection` keeps a deleted PVC `Bound` while a pod mounts it, so the stage check
      as written would have read green on a lost PVC.
    - Each run carries its red half: the same removal, with the prune marker stripped from
      the PVC, must lose it.
    - *Steps*, *Rollback*, *Risks*, *Tests and docs*, *Complexity delta* and Open questions
      10 and 11 change with it.
    - **Rejected alternatives.** A fixture role on production joins every census that a new
      k8s role joins, and its kept PVC and Namespace need an **operator-only** `kubectl
      delete` afterwards. A hand-applied scratch Kustomization on production needs operator
      `kubectl` writes and a GHCR push. Both put the drill on the cluster the proof exists to
      protect. kind needs neither, and it reruns on a `flux_version` bump.
    - The parts of X-08 that target R4, H2, D2, contracts.md and design.md are outside this
      file. They go to the cross-slice notes.
- **Beyond the finding: D6.7 did not name which Kustomizations lose `Orphan`.** D5's
  *Prune exemptions, digests and the collision census* sets `Orphan` on `-deferred`,
  `-snapshot`, `-verify`, `soak` and `homelab-namespace`. Its renderer test fails on any
  Kustomization without `Orphan`, so D6.7 as written would have failed CI. Fixed: D6.7
  switches `<tag>` and `<tag>-deferred`. The root `homelab` and the other four keep
  `Orphan`, and D6.7 narrows that test to them.

### Round 10 (per-slice adversarial review)

Thirteen findings were raised: three high and ten medium. All thirteen are fixed, and none
is refuted. Each claim was re-measured at `70591c307` before the fix. Round 10 supersedes
round 2's choice to switch `<tag>-deferred`.

- **D6-R10-01 (high): D6.1 deleted `cross_plane_order.py` whole, with gates whose subjects
  outlive D5.** Fixed. D4's *The cross-plane gate* defines arm 2 between flux-owned tags, and
  D5's D5.1d and D5.1f put the suspended-tag gate and both removal arms in the same module.
  `grep -rn cross_plane_order docs/target-architecture/` finds no other slice that keeps them.
    - D6.1 deletes only arm 1 and its use of `is_image_only_diff`, and renames the module
      `render_diff_gates.py`. The new section *The render-diff gates after D6* states what
      each kept arm guards and why the image-only exception goes.
    - The retirement arm stays until R4 deletes `k8s_retired_services` (cross-slice note).
    - D6.7b widens the dropped-object arm to whole-tag removals and drops its handback
      exemption, whose premise, that `Orphan` deletes nothing, D6.7 ends. The arm joins the
      checks that guard an accidental drop.
    - The Goal, *What D6 deletes*, *Stays after D6*, the tree diff and the complexity delta
      are corrected.
- **D6-R10-02 (high): D6.7 put `flux` and `flux-deferred` under the default policy, and the
  tag rule missed a `-deferred` leaving alone.** Fixed by the finding's second option.
  D6.7 switches every `<tag>` except `flux`. `flux` and every `<tag>-deferred` keep `Orphan`,
  with `flux` and `flux-deferred` as named members of the narrowed renderer test.
    - Evidence for keeping `-deferred` on `Orphan`: pihole defers instance 2's Deployment
      (`ansible/roles/k8s/pihole/tasks/main.yml`), and netpol-baseline defers its
      observability NetworkPolicies (`ansible/roles/k8s/netpol-baseline/CLAUDE.md`). A
      finalizer racing `<tag>`'s adoption would cause a DNS gap or a deny window.
    - The tag rule refuses any bundle without `flux`, and the rename procedure refuses
      `flux`.
    - *Risks* gains the CRD cascade, `flux-deferred`, the `-deferred` race and a CRD guard.
      Open question 7 records the exceptions, and a cross-slice note asks C3 to name them.
- **D6-R10-03 (high): the greps of exit 1 and exit 5 could not print 0.** Fixed. Measured at
  `70591c307`: exit 1's grep matches 16 lines in `tasks/code.yml`, four of them in the absent
  task "Remove the deployer libraries that moved into deployer_lib", and D6.1 adds four more
  to the other absent task. Exit 5's grep matches 18 files.
    - Exit 1 excludes `tasks/code.yml` with a pathspec, and says why.
    - The 25 other files of exit 1: D6.1 deletes `deploy_k8s.py`, `deploy_broad_k8s.py`,
      `deploy_k8s_owed.py`, `deploy_release.py` and `digest_provable.py`. D6.1 edits the
      gitops_deploy defaults, `deploy_alerts.py`, `deploy_changes.py`, `deploy_defer.py`,
      `deploy_handlers.py`, `deploy_inventory.py`, `deploy_io.py`, `deploy_logic.py`,
      `deploy_narrow.py`, `deploy_phases.py`, `deploy_state.py`, `deploy_toolbox.py`,
      `gitops_deploy.py`, both copies of `gitops_markers.py`, `deploy_lib/playbook.py` and
      `reach.py`. D6.4 deletes `shared_role_callers.py`, and D6.5b deletes
      `releases.py` and `releases_render.py`.
    - The 18 files of exit 5: D6.1 edits the gitops_deploy defaults, `deploy_alert_text.py`,
      `deploy_handlers.py`, `deploy_narrow.py`, `deploy_toolbox.py` and `narrow_setup.py`,
      and deletes `deploy_broad_k8s.py`. D6.1 and D6.2a both edit the gitops_deploy
      `CLAUDE.md` and `deploy_phases.py`. D6.2a edits both copies of `gitops_markers.py`,
      `deploy_config.py`, `gitops_deploy.py` and `land_lib/tools.py`. D6.4 edits
      `deploy_changes.py`, `deploy_cross_role.py`, `reach.py` and
      `ansible/roles/setup/common/CLAUDE.md`.
    - The Baseline lists the prose-only matches, and each PR names its own.
- **D6-R10-04 (medium): the drill could not bootstrap against production's source shape,
  and never tested the inventory prune.** Fixed. The drill writes its own OCIRepository with
  the local URL, `insecure: true` and no `spec.verify`, and the design says why that leaves
  the proof intact. A third fixture tag, `prune-drill-keep`, drops a marked and an unmarked
  PVC from a tag that stays. A dispatch always runs the drill. `.github/workflows/image-smoke.yml`
  confirms that its own dispatch skips when it names no images.
- **D6-R10-05 (medium): the tag rule's base was undefined and bypassable on master.** Fixed.
  The guard runs in the publish job against the bundle at `prod`, pulled from GHCR, so a
  refusal persists until the tag returns or its directory goes. An early-warning arm runs on
  pull requests against the base render. D6.7b's fixtures include a refused commit followed
  by an unrelated one.
- **D6-R10-06 (medium): the static media PersistentVolume was not prune-exempt.** Fixed.
  `ansible/roles/k8s/media-volume/templates/pv.yaml.j2` renders it with `Retain`, and C3
  exempts only PVCs and Namespaces. Open question 12 requests the C3 amendment for
  PersistentVolumes and StorageClasses, with a fallback refusal in the tag rule. *Risks* and
  the rename procedure name the case.
- **D6-R10-07 (medium): D6.1 deleted `ChangeSet.k8s_deploy` while `Reach.touched` read it.**
  Fixed. `scripts/deploy_tools/reach.py:Reach.touched` returns a union that includes
  `cs.k8s_deploy`, and `deploy_lib/staleness.py` calls `touched`. D6.1 removes the term and
  lists `reach.py` and `test_reach.py`.
- **D6-R10-08 (medium): D6.5 deleted `probe.py releases` while the live cron ran it from the
  primary checkout.** Fixed by the reorder. `release-staleness-check.sh.j2` sets
  `REPO_DIR=/home/{{ sys_user }}/server` and runs `probe.py releases` there. D6.5 now
  disarms units, tiles and tokens and keeps the code. D6.5b deletes the code and the absent
  tasks only after D6.5's hand apply, and D6.4 follows D6.5b.
- **D6-R10-09 (medium): the token removal left a shared template and
  `CROSS_HOST_PUSH_TOKENS`.** Fixed. D6.5 edits `kuma-push.env.j2` and its `no_log` comment
  ("renders five Kuma push tokens"), deletes `live-drift-check.env.j2`, and drops the three
  names from `consumers.py:CROSS_HOST_PUSH_TOKENS`. The cluster file step is D3's
  `cluster_vars.py write`, marked **operator-only**.
- **D6-R10-10 (medium): D6.5a's route claim was wrong, and daniel-server needed its own
  apply.** Fixed. Measured: `services_from_changed_paths` on the template plus
  `group_vars/all.yml` gives `broad_setup=True, setup_roles={'initial_setup'}`. The tick
  applies `initial_setup` on the box, with no parallel hand apply there. The operator runs
  the apply on daniel-server itself. The check before D6.5 waits on both hosts. This
  corrects round 1's D6-R1-05 correction, which measured `group_vars/all.yml` alone.
- **D6-R10-11 (medium): D6.0 read the producer's state from a probe that does not report
  it.** Fixed. `scripts/diagnostics/probe_lib/gitops_view.py` holds no reference to
  `render_records`. D6.0 checks the unit list and the role default, and stops when the
  producer is armed. The deferral to D6.5 is deleted.
- **D6-R10-12 (medium): root `CLAUDE.md`'s merge section stayed false from D6.1 to D6.8a.**
  Fixed. D6.1 edits the three-planes sentence and the k8s bullets of *When to wait*, and
  D6.3 edits the `deploy.sh` exit-3 bullet. Each verifies its section's lock row
  (`docs/facts.lock` records `CLAUDE.md#When to wait` and the merge section).
- **D6-R10-13 (medium): the `owner: flux` census row named no member and no floor.** Fixed.
  The row names `bento-pdf` and `flux` and a floor, and a cross-slice note asks H1.9 to
  repoint or retire it.

The investigation found these defects beyond the findings:

- **More prose that names deleted symbols.** The kept docstrings in `deploy_narrow.py` that
  explain their subprocess boundary by `narrow_deploy_plane`, `deploy_phases.py`'s module
  docstring, and the docstring in `land_lib/tools.py`. Each PR now names its own.
- **The `denylist_rendered` slot dies with `reconcile_denylist`.** `deploy_phases.py` is its
  only reader. D6.2a deletes it with its `test_deployer_state.py` row.
- **The hand apply cannot re-render the shared push file.** The shared task loses the
  deleted tags, so D6.5's `--tags` apply leaves two stale lines that nothing reads. D6.5
  says so.
- **D6.7 carried too many guards for one PR.** The guards move to D6.7b, which merges first
  and is inert under `Orphan`, so every guard is live before a deletion can happen.

### Round c1 (per-slice adversarial review)

Three findings were raised: one high and two medium. All three are fixed, and none is
refuted. Each claim was re-measured at `70591c307` before the fix. This round supersedes
round 1's D6-R1-20 clear commands, and round 10's bullet "The hand apply cannot re-render the
shared push file".

- **D6-C1-01 (high): the `manual_plane` clears in D6.5 and D6.5b did not clear the k3s line,
  and D6.5b's "run the apply the ledger names" could be the maximal `--tags k3s`.** Fixed,
  with one correction to the mechanism.
    - **Confirmed.** `narrow_setup.py:path_tags` asks `RoleIndex.readers_of` at the head for a
      `templates/` or `files/` path, and raises `CannotNarrow` when no task names it.
      `deploy_defer.py:narrow_tags_for` turns any refusal into None, which records the whole
      role. `deploy_state.py:clear_manual_plane_tags_applied` keeps such a line, and
      `gitops_state.py` prints "kept k3s ...: its line names no narrow tags".
    - **Measured.** `narrow_setup.py k3s 3188cdb98^ 3188cdb98` covers a range that deleted a
      k3s template and edited `kuma-push.env.j2`. It exits 1 with `cannot narrow k3s (nothing
      in this role reads k3s_remember_log_max_age_days)`, so a deleted `defaults/` key also
      refuses.
    - **Correction to point (3).** Narrowing works per task file, not per task.
      `task_walk.py:file_tags` returns the union of every task's tags, and
      `narrow_setup.py k3s a9e0483c6^ a9e0483c6` prints `tasks/health-crons.yml ->
      backup-health,disk-health,etcd-snapshot,live-drift,manifest-prune,release-staleness`.
      So even a narrowed D6.5 line carries all six tags, not only the kuma-push task's three.
      The finding's conclusion holds, and more strongly.
    - **D6.5.** It gains a narrowing gate: `narrow_setup.py` must exit 0 on the PR's range
      before merge, and the PR body quotes it. D6.5 carries no deleted template or file, no
      deleted or orphaned `defaults/` key, and no untagged task, so `live-drift-check.env.j2`
      moves to D6.5b. The hand apply is the line's command as `probe.py gitops-state` prints
      it, expected to be the six tags, and the clear must print `cleared k3s`. That apply
      re-renders `kuma-push.env`, so the two stale token lines go in D6.5.
    - **D6.5b.** Its line is whole-role by construction. The plan forbids `--tags k3s`,
      applies the tags `tasks/health-crons.yml` declares, verifies that the units, the cron
      and the four fragments are gone, and clears with a bare `clear-owed manual_plane k3s`,
      operator-attended. The step states why the bare clear is an exception to "apply, then
      clear", and stops when the tick's range holds another commit. The sentence "That apply
      changes nothing on the host" is deleted.
    - **Rejected:** leaving the role's files to H7. That avoids the whole-role line but keeps
      checks whose subject is gone.
    - *Verification*, *Rollback*, *Risks* and the tree diff change with it.
- **D6-C1-02 (medium): D6.1 deleted the k8s config fields that the kept `reconcile_denylist`
  and the fail-closed disarm read until D6.2a.** Fixed, and widened.
    - **Confirmed.** `deploy_phases.py:reconcile_denylist` reads
      `config.k8s_autodeploy_enabled_in_file` and `config.k8s_autodeploy_denylist`, and
      `gitops_deploy.py` reads the three denylist fields at module scope.
    - **Beyond the finding.** `reconcile_denylist` imports `declared_denylist` from
      `deploy_k8s.py`, which D6.1 deletes whole. That function reads `_DECLARATION_RE`,
      `_TRUE_VALUES` and `SHARED_K8S_ROLES` from the same module. As written, D6.1 would have
      broken the import of `deploy_phases.py` on every tick, not only one field read.
    - D6.1 now names everything that stays until D6.2a: four `deploy_config.py` fields with
      their `config.env` lines, three defaults, the two `daniel-box.yml` keys, the three
      `gitops_deploy.py` constants with the disarm, `reconcile_denylist`,
      `deploy_io.py:k8s_declarations_at` and the `denylist_rendered` slot. It moves
      `declared_denylist` and its three names into `deploy_phases.py`. D6.2a's list is exactly
      that remainder.
    - **Rejected:** moving the whole denylist deletion into D6.1. That adds the denylist's
      tests and its plugin row to the largest self-applied deployer PR.
- **D6-C1-03 (medium): D6.1's own merge tick ran the pre-D6.1 deployer on a deploy-plane
  path, after D6.0's check.** Fixed by both of the finding's remedies.
    - **Confirmed.**
      `deploy_changes.services_from_changed_paths(['ansible/inventory/host_vars/daniel-box.yml'])`
      returns `broad=True, broad_deploy=True` at `70591c307`.
    - The two `daniel-box.yml` keys move to D6.2a, whose tick runs D6.1's code. D6.1's PR
      body quotes `services_from_changed_paths` over its own paths with `broad_deploy=False`.
    - D6.1 repeats D6.0's ledger check after its first tick, because that tick can apply
      other sessions' commits with the old code. The step names the operator's `clear-hold`
      and `clear-owed` commands. D6.6 repeats the check just before merge, because it removes
      the last `clear-owed` path for the k8s classes.
    - *The route of a deploy-plane path after D6* now dates the change from the first tick
      that runs D6.1's code. The *Risks* entry on a line that outlives its writer names the
      last writer.

### Round c2 (per-slice adversarial review)

Five findings were raised: one high and four medium. All five are fixed. One sub-request of
D6-C2-02 is refuted with evidence. Each claim was re-measured at `70591c307` before the fix.

- **D6-C2-01 (high): D6.2a removed `K8S_AUTODEPLOY_DENYLIST` from the deployer's
  `config.env`, which the armed Renovate lander reads and refuses without.** Fixed.
    - **Confirmed.** `land_renovate_pr.py:denied_roles` reads the key from
      `land_renovate_pr.py:DEPLOYER_CONFIG`, `land_renovate_pr.py:main` refuses with "the
      deployer's denylist is empty", and `daniel-box.yml` sets `renovate_agent_enabled:
      true`. The plan said `land_renovate_pr.py` stays as it is, which was wrong.
    - **Measured.** `git grep` outside the gitops_deploy role finds no other reader of the
      key. `detach_notify.py:CONFIG_ENV_PATH` reads the same file for `DISCORD_WEBHOOK` only.
      The lander's unit runs `uv run --no-project` from `/opt/renovate-agent/`, so the lander
      cannot import `scripts/lib/k8s_roles.py`, which imports `yaml` and `lib.*`.
    - **Fix.** D6.2a repoints the lander, in the same PR, at
      `k8s_autodeploy_counts.py --denylist`, run in `RENOVATE_AGENT_REPO_DIR`. Fail-closed is
      kept for empty output and a non-zero exit. The PR updates the two test modules, the
      role's `CLAUDE.md` and the docs page. The step states that the tick applies
      renovate_agent and that a one-tick refusal fails closed.
    - *Baseline*, the Renovate-readers design section, *Verification* (the verdict files
      after the next agent run), *Each new check can go red*, *Rollback*, *Risks*, the tests
      list and *Complexity delta* change with it.
    - **Rejected:** a list rendered into renovate_agent's own env file. Nothing re-renders it
      when a role's stance flips, so it would fail open. Also rejected: a `config.env` shim,
      which H7 deletes.
- **D6-C2-02 (medium): D6.1 shrank the copy loop but not `stamp_deployed_pairs`.** Fixed;
  the request for a new reverse test is refuted.
    - **Confirmed.** `tasks/code.yml`'s `stamp_deployed_name: gitops-deploy` task holds pairs
      for the four deleted modules. D6.1 now removes them.
    - **Refuted: the reverse check is missing.**
      `test_gitops_deploy_ship_list.py:test_stamp_pairs_point_at_the_installed_path` asserts
      `(REPO / pair["src"]).is_file()` for every pair. A stale pair already fails CI, so no
      new test or red fixture is added.
    - **Beyond the finding.** The task "Remove the deployer libraries that moved into
      deployer_lib" lists the four flat names, and
      `test_the_removed_flat_files_are_ones_the_role_no_longer_ships` requires each to exist
      under `files/deployer_lib/`. D6.1 would have turned that test red. The plan's "add the
      four deleted modules to the folded task" also named the flat path, which is not where
      the live modules sit. D6.1 now moves the flat names into the folded task and adds the
      `deployer_lib/` paths there.
    - *Verification* reads the `gitops-deploy` fragment after D6.1's first tick and checks
      the manifest-prune-check tile.
- **D6-C2-03 (medium): the drill's `keep-unmarked` PVC had no consumer, so it could not bind
  under kind's `WaitForFirstConsumer` class.** Fixed by the first remedy.
    - The kind and kstatus behaviour is upstream and was not reproduced here. The revision-1
      readiness wait now checks it on every run, and D6.7a's PR body quotes that wait.
    - A second pause Deployment, `keep-unmarked-holder`, mounts `keep-unmarked` and stays
      unchanged in revision 2. The inventory prune then leaves the PVC terminating under
      `pvc-protection`, which the "gone or terminating" assertion accepts.
    - **Rejected:** a drill StorageClass that binds `Immediate`, because nobody has checked
      that kind's provisioner serves it.
- **D6-C2-04 (medium): the Orphan keep-set omitted the `cluster-vars` shared kind.** Fixed.
    - **Confirmed.** contracts.md C3 *Shared kinds* reads "D3.3 adds `cluster-vars`", and
      D3's rollback of D3.3a relies on `Orphan` for `homelab-cluster-vars`.
    - The switched set is now the per-tag `<tag>`s except `flux`, and the kept set is every
      other Kustomization. The narrowed test iterates `SHARED_KINDS`, with `flux`,
      `flux-deferred` and `cluster-vars` as named members. The prose list, D6.7, the tests
      list, *Risks*, *Complexity delta* and Open question 7 name `cluster-vars`.
- **D6-C2-05 (medium): the C3 amendments that D6.7b and D6.7 gate on had no row in
  contracts.md's *Requested amendments not yet applied*.** Fixed by cross-slice notes,
  because this round edits only this file.
    - **Confirmed.** The list holds one D6 row, and it names D6.5 where Open question 6
      names D6.5b.
    - The notes ask for two rows, PV and StorageClass prune exemptions before D6.7b and the
      deletion-policy exceptions before D6.7, and for the Open question 6 row's correction.
      Open questions 6, 7 and 12 point at them.

### Round c3 (per-slice adversarial review)

Two findings were raised: one high and one medium. Both are fixed. One sub-request of
D6-C3-01 is refuted with evidence. Each claim was re-measured at `70591c307` before the fix.

- **D6-C3-01 (high): D6.1 deletes `deploy_k8s_owed.py` and `deploy_broad_k8s.py` whole, but
  they hold the only path to two host-plane pages.** Fixed; the annotation half of the
  required fix is refuted.
    - **Confirmed.** `git grep 'alert_deferred('` over the role's `files/` finds one caller,
      `deploy_k8s_owed.alert_and_record_deferred`. `handle_no_services`, `handle_broad`'s
      failure arm and `apply_broad_k8s` reach the tasks page only through it.
      `handle_broad`'s success exit is `return deploy_broad_k8s.apply_broad_k8s(...)`, whose
      last exit sends the secrets page.
    - **Beyond the finding.** The finding named two call sites, and there are three:
      `handle_broad`'s failure arm also calls `alert_and_record_deferred` and
      `deploy_broad_k8s.fold_deferred`. D6.1 also never named `handle_k8s`, a third reader of
      `cs.k8s_deploy`. `alert_deferred`'s k8s arm reads `cs.k8s_consumers`, which D6.1
      deletes, and `k8s_remediation` has no production reader but `k8s_deferred_alert`.
      Round i1 corrected this: `land_tags.py` and `deploy_lib/tags.py` also call it, so
      it moved back to D6.4.
      `handle_no_services`'s own secrets call is direct, and
      `test_handle_no_services_merges_and_flags_a_rotated_secret` pins it.
    - **Fix.** The Baseline names the two modules as mixed. A new design section, *The
      host-plane pages after D6.1*, moves the three calls into `deploy_handlers.py` and
      deletes the k8s arm with `declared_k8s`, `k8s_deferred_alert` and `k8s_remediation`
      (moved from D6.4). D6.1 deletes `handle_k8s` and rewrites the `DECIDED` and
      `broad.deferred` comments that name `apply_broad_k8s`. Two tick tests in
      `test_gitops_deploy_main_branches.py` pin the tasks page and the secrets page, the
      second over the success exit and the failure arm. D6.5 expects one secrets page on its
      merge tick. Exit 2, *Verification*, *Each new check can go red*, *Rollback*, *Risks*,
      the tests list, the tree diff and *Complexity delta* change with it.
    - **Refuted: keep `emit_deploy_annotation(applied, origin)` for the setup applies.** In
      `deploy_broad_k8s.apply_broad_k8s`, `applied = cs.k8s_deploy - bumps`, which is a set
      of k8s services. `deploy_io.emit_deploy_annotation`'s docstring reads "Only the k8s
      auto-deploy path and the broad arm call this". The setup applies are recorded by
      `state.record_receipt` and were never annotated. D6.1 deletes the emitter, its
      toolbox field and the deployer half of `test_deploy_annotations.py`. `deploy.sh`'s
      `playbook.py:annotate` keeps the `deploy-annotation` stream.
- **D6-C3-02 (medium): D6 ignored D2's ownership branches, so D6.2b's glance-block rewrite
  rested on the pre-D2 tree.** Fixed.
    - **Confirmed.** D2's entry-point table gives `owner: flux` roles the Flux line "in place
      of the deploy tag and the auto-deploy stance", and adds `cs.flux`, `service_owners`
      and `tag_owners`. The plan named none of them.
    - **Fix.** The hand-off section is now *What D2, D4 and D5 hand to D6*, with a D2 entry.
      A new design section, *D2's ownership readers after D6*, states each fate.
      `ChangeSet.flux` stays until H7 as the route of every k8s role path, and D6.4 deletes
      `service_owners` so that the deployer reads no `owner`. Without that destination, a k8s path would fall
      into the setup branch of `services_from_changed_paths`. The `tag_owners` readers go in
      D6.3, D6.4 and D6.5b with their arms, or stay until H1.9.
    - **Design choice.** The glance blocks keep D2's Flux line, and the Renovate stance does
      not return to them. D6.2b removes the stance from the generators' Ansible-owned branch,
      which no entry reaches after D5, and rewrites no block. Its size falls from about 250
      to about 190 lines.
    - Exit 1's field list names `flux` as the field that stays.

### Round integration-2 (integration review)

Two findings named D6, both medium: X2-11 and X2-19. Each claim was re-measured at
`70591c307`. Both are fixed for D6's part. X2-11's rows for other slices are outside D6's
scope, and this round refutes none of it.

- **X2-11 (medium): the register and slice index lack about 20 requested rows and
  sub-edges that slices name as merge gates.** Fixed for D6's two rows.
    - **Confirmed.** As reviewed, contracts.md's *Requested amendments not yet applied* held
      nine rows. None was Open question 12's PersistentVolume and StorageClass prune
      exemption, gated before D6.7b, or Open question 7's kept set, gated before D6.7.
      Earlier rounds asked for both in cross-slice notes. The deletion-PR row named D6.5
      where Open question 6 names D6.5b.
    - **Since the review.** The contracts revision of this round adds both rows under
      *Requests on C3*, with the gates and the fallback D6 names, and corrects the
      deletion-PR row to D6.5b. Read in the working tree while this entry was written.
    - **Fix.** D6.7b and D6.7 each gain a *contract gate* step. Neither PR merges until its
      row or the amended C3 line exists, and the PR body quotes that line. D6.7b with only
      the row ships Open question 12's fallback rather than the marker test. D6.7 with
      neither line leaves D6 at `Orphan`. Open questions 6, 7 and 12 name the rows.
    - **A gate D6 did not name.** The same register gates D6 on M2's *The label that names
      a deploy tag* ("Gate: before D6 merges", M2 Open question 1). D6.0 gains a third
      check: C1 or C7 names that label, or the session stops.
    - **Index.** No D6 step waits on a PR of another slice. A grep of *Steps, as PRs* for
      another slice's PR id finds only "until H7". D6's index row, "D5", is complete.
    - **Outside D6.** The rows and sub-edges for D1, D5, H1 to H4, H6, M5, M6 and R2 belong
      to those slices and to contracts.md.
- **X2-19 (medium): after D6, Grafana deploy annotations come only from `deploy.sh`.**
  Fixed.
    - **Confirmed.** *The host-plane pages after D6.1* said the `deploy-annotation` stream
      "keeps `deploy.sh`'s emitter". After D6.3, `deploy.sh` refuses every k8s tag, so that
      emitter covers the Pi only, and after H5 it has no deploy to mark. The Ansible
      emitters stop for each role at its flip, under C4, not at D6.1.
    - **Changed since the review.** D2's revision for the same finding adds a third
      emitter in D2.3: `promote.py land-wait` writes one `source=promote` line when a
      landing settles. It runs inside `land.sh` on the host, so the line takes the host
      syslog path that the dashboards' query reads.
    - **Fix.** A new design section, *Grafana's deploy markers after D6*, makes D6 the
      stream's owner from D6.1 on and names `land-wait` as the cluster's emitter. It
      records D2's remaining gap, a promotion that no `land-wait` follows, as a lasting
      limit. D6.3 keeps `land-wait` and its annotation, and states that `land-wait` then
      receives every k8s tag the PR touches. D6.1 rewrites the comment above
      `ansible/roles/k8s/observability/defaults/main.yml:observability_deploy_annotation_expr`,
      which names the deleted `deploy_io.py` emitter.
    - **Ripple.** The Baseline gains the syslog chain, the `event=landing` stream and the
      GitHub-hosted runners. The changes also touch D6.1's and D6.3's steps, Exit 1's grep
      (now with `emit_deploy_annotation`), a `source=promote` check after D6.3, *Rollback*,
      a new *Risks* entry and D6.1's tests-and-docs list.
    - **Rejected: an emitter in `promote`.** Every `runs-on` under `.github/workflows/`
      names `ubuntu-26.04`, so a `logger` call in `promote` writes to the runner's syslog,
      which no Alloy ships. A POST to Grafana instead needs the standing write credential
      that the comment above `observability_deploy_annotation_expr` records the reason to
      avoid.

### Round i1 (per-slice adversarial review)

Three findings: two high and one medium. Each claim was re-measured at `70591c307`, and all
three are fixed. One adjacent defect on the same path is fixed too. None is refuted.

- **D6-I1-01 (high): D6.1 deleted `k8s_remediation` and its `deploy_logic` re-export while
  `land.sh` and `deploy.sh --changed` still import it.** Fixed.
    - **Confirmed.** `git grep -n k8s_remediation 70591c307 -- scripts` lists
      `land_lib/land_tags.py`, which imports it at module level from
      `deployer_lib.deploy_logic` and calls it in `plane_note`. It also lists
      `deploy_lib/tags.py`, where `_load_deploy_logic` imports it and `changed` calls it.
      `land.py` imports `land_lib.tools`, which imports `land_tags` at module level, so
      every `land.sh` run would have died at import. The c3 claim of no other production
      reader was wrong.
    - **Fix.** D6.1 keeps the function and its re-export. D6.3 removes both callers, and
      D6.4 deletes the function, because it reads `deploy_cross_role.py` symbols that D6.4
      deletes. A new design bullet under *The host-plane pages after D6.1* names both callers.
      The order section's per-PR grep now covers symbols, with `git grep -w <name>` for a
      name removed from a kept module. Exit 5 greps `k8s_remediation` and
      `shared_caller_tags`. D6.1, D6.3 and D6.4 each quote `land.py --help` and
      `deploy_tags.py changed origin/master`. The second is the derivation `deploy.sh
      --changed` runs, and it reaches the lazy import. The c3 line carries the correction.
    - **Every site assigned.** `git grep -n -w -e k8s_remediation -e shared_caller_tags
      -e narrowed_roles -e smoke_narrowed_roles 70591c307 -- ansible scripts` outside
      `tests/` lists 38 lines. D6.1 owns the `deploy_alert_text.py` and `deploy_alerts.py`
      lines, D6.3 the `land_lib`, `deploy_lib/tags.py` and `findings_lib` lines, and D6.4
      `deploy_logic.py`, `deploy_remediation.py`, `deploy_changes.py` and
      `narrow_setup.py`. D6.4 deletes `shared_role_callers.py`. Exit 5's count can
      therefore reach 0.
    - **Rollback.** Once D6.4 has merged, D6.4 reverts before D6.3. A D6.3 revert on its
      own would restore module-level imports of what D6.4 deleted.
- **D6-I1-02 (high): after D6.3 and D6.4, nothing defines where `land-wait` gets its tags
  for a broad or shared-role PR.** Fixed.
    - **Confirmed.** At `70591c307` the three sources are `land_tags.derive` over
      `reach.Reach.tags`, the shared-role expansion in `land_lib/classify.py` through
      `land_shared.shared_caller_tags`, and `land_lib/deploy.py:derive_from_diff` over
      `deploy_tags.py changed` with `_narrowed`'s `narrow` fallback. D6 deletes the k8s
      half of each. A template or inventory path maps to no tag in any of them. No other
      slice under `docs/target-architecture/` names a replacement: a grep for
      `derive_from_diff`, `shared_caller_tags` and `self_applied_playbooks` outside D6
      finds none.
    - **Adjacent, fixed too.** `land_tags.self_applied_playbooks` adds `ansible/deploy.yml`
      for `cs.broad_deploy`, and `land_lib/landing.py:Landing.tick_applied` reads the
      tick's receipt for it. From D6.1 the tick applies no `deploy.yml`, so every
      deploy-plane landing would read as unapplied from D6.1, before D6.3.
    - **Fix.** A new design section, *`land.sh`'s k8s tags after D6*, makes the render diff
      the source from D6.1. D6.1 adds a `--moved-tags` mode to `render_diff_gates.py`.
      `land.sh` runs it on daniel-box over the merge commit and its first parent, or
      `--since` for the fallback, and joins its tags with the path map's. A failed render
      widens the list to every flux-owned tag. D6.1 also drops the `deploy.yml` term.
      `changed` prints its own Pi line, because `broad_remediation`'s deploy-plane text
      names a full `ansible/deploy.yml` run.
    - **Beyond the finding.** `scripts/dev/findings_lib/precomputed.py:deploy_line`
      also calls `land_shared.shared_caller_tags`, and `land_shared.narrowed_roles`
      feeds a smoke narrowing in `classify.py`. D6.3 repoints the first and deletes the
      second.
      D6.3 replaces "deploy it by hand" for a broad range: the Pi half prints
      `changed`'s refusal beside the verdict, and the verdict rests on `land-wait`.
    - **Tests.** D6.1 adds a template PR test, a shared-role PR test and a failed-render
      test. D6.3 adds a test for a range over the 100-file page. D6.4 adds a direct-path
      test. Each test has a red half, and each later PR quotes the earlier ones passing.
      *Verification* reads `source=promote` markers with a non-empty `services=` list after
      D6.4, for a direct PR and a broad PR.
    - **Ripple.** The changes reach the tree diff, *Rollback* (D6.1 and D6.3), two *Risks*
      entries (the empty list, and the time of two renders), the tests-and-docs lists and
      the complexity delta, which adds about 150 lines.
    - **Rejected options.** Every flux-owned tag for every broad PR is rejected: it gates
      about 50 health checks on one template edit. It stays as the failure fallback.
      Reading CI's render-diff result with `gh` is rejected: it costs an artifact download
      and renders the PR head, not the merge commit. It stays as the fallback if the renders
      are too slow.
- **D6-I1-03 (medium): D6.7's contract gate let it merge on an unapplied register row, and
  the operator's approval was not marked operator-only.** Fixed.
    - **Confirmed.** contracts.md's register preamble keeps each row "as written until its
      amendment lands, before the gate". The row *The deletion policy's kept set* names no
      fallback, yet D6.7's gate accepted the row alone.
    - **Fix.** D6.7's gate now requires the amended C3 *Deletion policy* line. A new
      **operator-only** step holds `land.sh` on D6.7 until the operator writes the decision
      in the PR thread, and the PR body quotes it. Open question 7 says the same. D6.7b's
      gate is unchanged, because its row names the fallback.

### Round f0 (per-slice adversarial review)

One high finding. It was re-measured at `70591c307` and is fixed. Nothing is refuted.

- **D6-I2-01 (high): D6.1's `--moved-tags` mode had no defined way to render two commits on
  daniel-box, and the checkout the plan named is the production primary checkout.** Fixed.
    - **Confirmed.** `scripts/deploy_tools/land_lib/landing.py:Landing.git` is documented as
      "git in the PRIMARY checkout, never raising" and runs with `cwd=self.opts.primary`, and
      `Landing.fetch_branch` fetches there. C1's CLI takes no commit to lay out, and this
      plan's Round 2 entry already records that it renders only the repo's tree. The plan said
      "in the checkout it already fetches" and named no layout, lock or cleanup.
    - **Precedents measured.** `scripts/deploy_tools/setup_role_chains.py:tree_at` already
      reads a merge commit's tree for `land.sh` with `git archive` into a temporary directory.
      Its docstring records that the primary checkout can sit at the pre-merge HEAD while
      `land.sh` runs. `deploy_lib/under_locks.py:make_snapshot` lays out `deploy.sh --at`
      with `git worktree add --detach`, and `reap_dead_snapshots` reaps those entries only
      under the tree lock. `.gitattributes` sets no `export-ignore`. The one submodule,
      `Email-to-RSS`, is no renderer input.
    - **Fix.** A new design subsection, *How the mode lays out a commit*, states the
      mechanism:
        - `git -C <repo> archive <sha>` for each SHA, extracted into one `moved-tags-`
          temporary directory outside the repository, with both `--out` directories in it;
        - a refusal of any scratch or output path under the repository root;
        - each commit's own renderer, run by `uv run --frozen` in its own tree, so a renderer
          or `uv.lock` change counts as moved. The invoking checkout's `render_diff_gates.py`
          does the comparison;
        - one commit value, the head's, passed to both renders through C1's commit input, as
          R4's render-diff job does;
        - no git-tree lock, because the mode reads object-database content by SHA only;
        - cleanup by the context manager on return and on error. A killed run leaves one
          directory under `/tmp`, which daniel-box's `tmp.conf` ages out after 30 days.
    - **Rejected options, recorded in the subsection.** `git worktree add --detach` needs the
      owner lock and tree-locked reaper of `under_locks.py`, and a careless prune deregisters
      other sessions' worktrees. A checkout in place moves the deployer's HEAD. Rendering the
      working tree can predate the merge and races the tick.
    - **Tests and evidence.** D6.1 adds a no-write test in `scripts/validate/tests/` against
      a two-commit fixture repository. It asserts an empty `git status --porcelain
      --ignored`, an unchanged HEAD and `git worktree list`, and an empty scratch parent after
      a failed render. Its flagged half refuses a scratch path inside the repository before
      creating anything. D6.1's PR body quotes `git status --porcelain` printing nothing on
      the primary checkout around a timed run on daniel-box, and a `git worktree list` there
      that names no `moved-tags` entry.
    - **Ripple.** D6.0 gains a fourth check, that C1 names the commit input. The changes also
      reach *A failed render widens the list* (a timeout now widens too), *Verification*
      (the timed run and the git lines, and a red-proof entry), *Rollback* (D6.1 leaves no host
      state), *Risks* (the timing entry and a new checkout-safety entry), the tests-and-docs
      list and the complexity delta, which grows from about 150 to about 190 lines.

### Round f1 (per-slice adversarial review)

One high and three medium findings. Each was re-measured at `70591c307` and is fixed.
Nothing is refuted.

- **D6-F1-01 (high): the moved set counted only `cluster/<tag>/`.** Fixed.
    - **Confirmed.** C1 *Cluster output* also publishes `cluster/<tag>-deferred/`, the
      `-snapshot` and `-verify` directories, `flux/<tag>.yaml` and each shared kind's files.
      C3 *Ordering* takes `dependsOn` from `ansible/filter_plugins/toposort.py`, and an
      ordering no template carries sits in `depends_on:` on the inventory entry. D4's *What
      counts as changed* counts `cluster/<tag>/` only, so an edge-only PR, which D4's arm 2
      requires, reached `land-wait` with an empty list. So did a `-deferred`-only,
      `cluster-vars`-only or `flux/`-only change.
    - **Fix.** *`land.sh`'s k8s tags after D6* now defines the set over the whole bundle and
      stops calling it D4's counted set. A new bullet, *What the mode counts*, classifies
      every differing path:
        - a tag's own files fold into the tag through C3's reserved suffixes;
        - `cluster-vars` counts for every tag whose head `flux/<tag>.yaml` carries
          `spec.postBuild`, the field C1's *projection* names;
        - `homelab-namespace` counts for every flux-owned tag, and `soak` does too when it
          moves alone;
        - index files count for no tag;
        - anything else widens, and a census test fails on a `SHARED_KINDS` member with no
          rule;
        - a tag only the base renders is left out, because it can never read Ready.
    - **Ripple.** "Empty by right" now reads "moves no bundle file". D6.1's pair gains three
      flagged fixtures, whose red half is the old `cluster/<tag>/`-only count. A fourth
      landing test covers an inventory `depends_on:` edit, and every "three landing tests"
      now reads four. *Risks* and the complexity delta follow.
- **D6-F1-02 (medium): the scratch parent followed `TMPDIR`.** Fixed.
    - **Confirmed.** `printenv TMPDIR` prints `/tmp/user/1000` on daniel-box.
      `land_lib/tools.py:gate_snapshot` pins `GATE_TMP_ROOT = "/tmp"` with the override
      `LAND_GATE_TMPDIR`, under the marker `DECIDED the parent directory is PINNED to
      GATE_TMP_ROOT`, for this trap.
    - **Fix.** The mode pins its parent to `/tmp`, with the override variable
      `MOVED_TAGS_TMPDIR`, which only the no-write test sets. That test also points `TMPDIR`
      at a second directory, and a mode without the pin fails it. *Verification* plants
      `/tmp/moved-tags-redproof` to show the count go to 1. *Cleanup* and *Rollback* name
      `/tmp/moved-tags-*`.
    - **The rejected worktree option is restated.** It cites `gate_snapshot` and its marker
      `DECIDED NO tree lock is taken`. `git archive` is chosen because a killed run leaves no
      registration to prune, which the docstring's "A SIGKILL or a host reboot still leaks
      the registration" shows a worktree does.
- **D6-F1-03 (medium): the commit input had no gate before D6.1.** Fixed.
    - **Confirmed.** The register row *The commit input* reads "Gate: before R4's PR 1
      merges", and R4 waits on D6.
    - **Fix.** D6.0 now also runs the renderer's `cluster --help` at `origin/master`,
      requires C1's flag and quotes the output. Open question 13 and a cross-slice note ask
      for a gate before D6.1 merges. The R4 reference reads as a convention R4 later adopts.
      A base older than the flag fails its render and widens.
    - **The *Risks* fallback is now the widening**, since no other producer of the moved set
      exists at D6.1. The rejected `gh` bullet no longer calls itself the fallback. A
      measured time with no margin under the promote timeout stops D6.1 before merge.
- **D6-F1-04 (medium): the uv child inherited `UV_PROJECT_ENVIRONMENT`.** Fixed.
    - **Confirmed.** `gate_snapshot` sets that variable process-wide to the primary's
      `.venv`. `deploy_lib/playbook.py`, `deploy_lib/detach_run.py` and
      `deploy_lib/under_locks.py` set it for their children.
    - **Fix.** The mode removes `VIRTUAL_ENV` and sets `UV_PROJECT_ENVIRONMENT` to the
      extracted tree's `.venv`. The no-write test sets the variable to the fixture's path
      first, and its stub records what it receives. *Rollback* names the uv cache as the one
      write outside the scratch directory, and a lock bump that cannot download widens.
    - **The host check goes past the finding's wording.** On the primary checkout,
      `git status --porcelain --ignored` is never empty, because `.venv/` is ignored. Git
      also prints that directory collapsed, so a sync into it leaves the listing unchanged.
      *Verification* therefore diffs the ignored listing before and after the run, and adds
      `find <primary>/.venv -newer <stamp>`, the one line that sees a write into `.venv`.

### Cross-slice follow-up (2026-10-12)

- Cross-slice note from R4: Open question 14 asks D6.1 to drop or gate the `deploy.yml`
  command in `broad_remediation`. Confirmed at `70591c307`: the function appends it whenever
  `broad_deploy` is set.
- **#4609 (medium): D6.5 and M6's B1 both retired the three drift checks' tiles and
  tokens.** M6's B1 row converts manifest-prune, release staleness and live-drift to textfile
  checks, and D6.5 removed the same tiles, tokens and cluster-file placeholders. Neither page
  cited the other. **Fixed by giving the Kuma side to M6 and the checks to D6.** D6 opens
  after M6's C-2b, so by D6.5 the tiles are gone and B1's token PR has removed the tokens.
  D6.5 now cites B1, quotes an empty token grep instead of removing tokens, and owes no
  secrets page. It gains the removal of B1's six rules and absent tasks for the three `.prom`
  files. D6.5b's unit check reads either timer name, because M6's C-3b renames the timers.
  X2-13's remedy, dropping the checks from M6, would leave three tiles alive at C-1, which
  M6's one-series gate refuses.

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review round covers it.

- **F-05 with C1 and C6 (`--moved-tags` replaced by footprint digests).** *`land.sh`'s k8s
  tags after D6* drops the mode and the whole of *How the mode lays out a commit*. D2.3's
  `land-wait` reads the changed set from `digests.json`, and D6.1 makes that set the whole
  k8s list. The classification stays as *What a footprint counts*, which D2's renderer
  applies. Also changed: D6.0's commit-input check, D6.1's step and tests, the tree diff,
  *Verification*, *Rollback*, *Risks*, *Tests and docs* and the delta, from about 240 to
  about 80 lines. Open question 13 is withdrawn. Stop note: the `soak` rule compares two
  bundles, so its text stays as written and Open question 15 holds it. The index-file rule
  follows C1's `_unclassified` and widens.
- **F-03 (no deploy path without GitHub).** *Break-glass promotion* and step D6.7c add the
  operator-only runbook as a scenario in `docs/break-glass.md`, the key pair, the source
  form, the item 8 update, the way back to GHCR, and two cases in the existing
  `flux-deletion-drill`. Open question 3 keeps the in-cluster registry. Open question 16
  holds the plain-HTTP verification question.
- **F-02 / O3 (deploy annotations).** *Grafana's deploy markers after D6* replaces the
  lasting limit with D6.1b's query over kustomize-controller's log in loki-homelab, with
  `gotk_resource_info`'s `revision` label as the fallback.
- **F-16 / P8 / O4 (the superseding ADR).** *The ADR* lists every GitHub dependency with
  its fallback, names k3s and Flux as fixed premises with their exit cost, and records no
  tracing by decision.
- **F-05 (end sizes).** Exit 6 bounds `promote.py` at 900 lines and the landing code at
  6,400, and *Verification* reads both.
- **Coordinator note from D2.** D6.7b removes the handback exemption from D2.6c's
  object-loss check in `decide`, so the guard is live before D6.7 switches the policy.
- **Coordinator note from D3.** D6.1 is the PR in which D3.4's interim `cluster-vars` rule
  retires.
- **Coordinator note from D4.** The rejected `--moved-tags` bullet answers D4 Open question
  12: no layout function survives, because the PR-time gates render in CI.
- **Coordinator note from H7.** *Break-glass promotion* names the fresh-cluster steps: the
  in-cluster registry before the push, and Flux controller images from the kit. Open
  question 17 holds the kit's images and the drill's fresh-cluster case, beside H7 Open
  question 14.
- **Follow-up check on F-03.** The runbook gains an ancestry check before the push, and
  *Risks* names the stale-bundle revert. A cross-slice note asks C2 to allow a host
  checkout newer than the bundle as the render source.
- **Coordinator note from D5.** D6.7c and *Break-glass promotion* cite D5 Open question 2
  as a precondition, and the fresh-cluster steps name the rebuilt-cluster case.

### Issue follow-up (2026-10-12)

- **#4632 (medium): D6.5a still edited Kuma after M6's C-2b.** D6.5a renamed the
  setup-drift tile, put the host name in the message for the shared tile, and checked the
  `kuma-check-setup-drift` timer and the tile. D6 opens after C-2b, so Kuma is gone and
  M6's B2 has made setup drift a textfile check with per-host series. **Fixed.** D6.5a now
  edits no tile and no template. *The setup-drift reader covers daniel-box* records why the
  box's arm in B2's rules lands in D6.5, not D6.5a: an `absent_over_time` arm fires at
  `promote` unless the box has already written a sample. Without the template edit, the PR
  routes to no setup role, so D6.5a names a hand apply on daniel-box, as D6-R1-05's
  correction first said, and daniel-server owes nothing. D6.5a's check reads the timer
  under either name, because M6's C-3b renames it. Open question 8 is resolved. The C-3b
  and D6.5 order, part 1 of the issue, is #4609's (PR #4634).
