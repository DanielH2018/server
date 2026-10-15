# D2 — One stateless leaf end to end on production

Status: IN PROGRESS

D2.1a, the flag's script reader and the narrowing: [#4956](https://github.com/DanielH2018/server/pull/4956).

D2.1b, `deploy.sh` refuses, drops and marks: [#4961](https://github.com/DanielH2018/server/pull/4961).

D2.1c, an added Flux-born entry reaches only the readers it can move:
[#4977](https://github.com/DanielH2018/server/pull/4977).

D2.1d, the Ansible play skips a non-Ansible-owned entry:
[#4958](https://github.com/DanielH2018/server/pull/4958).

D2.2, the deployer skip: [#4965](https://github.com/DanielH2018/server/pull/4965).

D2.3, `land.sh` waits on promotion: [#4997](https://github.com/DanielH2018/server/pull/4997).

D2.4a, the two drift checks: [#4981](https://github.com/DanielH2018/server/pull/4981). The
operator ran its `k3s-bringup.yml` hand apply on 2026-10-14 and cleared the owed line.

D2.4b, the script readers and the two generators:
[#4988](https://github.com/DanielH2018/server/pull/4988).

D2.5, verification, `last-good` and the hold:
[#5025](https://github.com/DanielH2018/server/pull/5025). Its first `promote` run passed
`verify` and `settle`, and seeded `last-good` on prod's digest. Both holds read `none`.

D2.6b, the bundle writer, `digests.json`, its census and the reserved names:
[#5010](https://github.com/DanielH2018/server/pull/5010). The first `promote` run after it
reported 0 published tags, and `flux/homelab` read `success` on `6c26fb6f5`.

Depends on: [D1](d1-flux-installed.md), [R1](../render/r1-shadow-render.md),
[R2](../render/r2-secret-placeholders.md) (its PR 1, placeholder mode, and its PR 2, the five
rules). PR D2.7 also waits on PR D4.0 of [D4](d4-ordering.md) (D4 Open question 3), on
[M1](../monitoring/m1-alertmanager.md), and on domain PR 2b of the *Publish `domain`* item in
R2's *Steps, as PRs*, which makes `domain` a published key (operator ruling of 2026-10-14).
D2.7 waits on no PR of [D3](d3-secrets.md). The contracts slice index states the same three
sub-edges. PR D2.6d waits on M1 too, because its rules join M1's rules directory.

Unblocks: [D3](d3-secrets.md), [D4](d4-ordering.md), H1, H6

## Goal and exit criterion

After D2, Flux reconciles one service, `bento-pdf`, from the promoted artifact, and nothing
else writes it. Its `containers_list` entry carries `owner: flux`. Every Ansible-side path
that could apply it refuses or skips it, and `land.sh` follows a bento-pdf change through
`promote`. The `promote` job verifies each revision through the commit statuses of the
Kustomizations that revision changed, moves `last-good` on success, and on failure writes
`DEPLOY_HOLD` and resets `prod`. The deploy pipeline pages through Alertmanager from D2.7.

No stage cluster exists at `70591c307` (see *Baseline*), so D2 runs on production. Its leaf
is a service whose failed rollout keeps the old pod serving. D2.8 is C8's production drill.
The operator accepted it on 2026-10-14, and the session merges D2.8 itself (Open question 1).

**Exit criterion.** Four checks, each under *Verification*:

1. **Flux is the only writer.** bento-pdf's Deployment, Service and IngressRoutes carry the
   label `kustomize.toolkit.fluxcd.io/name: bento-pdf`. The Deployment has no
   `last-applied-configuration` annotation, and `kustomize-controller` is its only apply
   manager. The `.local.` IngressRoute's route rules hash the same after the flip as before
   it, so the renderer wrote the same `domain` that Ansible rendered.
2. **Every entry point refuses or skips.** `./scripts/deploy.sh --tags bento-pdf` exits 5 and
   names Flux. The deployer's journal logs a bento-pdf path as owned by Flux. `land.sh` on a
   bento-pdf PR waits on promotion and prints `VERDICT: settled`.
3. **A broken probe holds.** A merged commit D points bento-pdf's readiness probe at a
   missing path. Let T be the commit that `decide` promotes next: D itself, or a later master
   commit that contains D. The drill ends with these six facts:
    - a `flux/bento-pdf` status of `failure` on T;
    - `DEPLOY_HOLD` equal to `<T> failed=bento-pdf`;
    - `prod` on the same digest as `last-good`;
    - the old bento-pdf pod serving throughout;
    - the `DeployHoldActive` alert firing while the hold stands;
    - the `ProdMovedOutsidePromote` alert silent through the reset, because the reset writes
      `PROD_DIGEST`.
4. **The hold clears.** After the revert R merges, the operator clears `DEPLOY_HOLD` and
   dispatches `promote`, and `last-good` moves to a revision that contains R. The revert
   rule's flag stays off through D2, so the clear is by hand. The proof reads
   the commit behind `last-good`, that commit's `flux/bento-pdf` status, and the bento-pdf
   Kustomization's origin revision.

## Baseline at `70591c307`

- **No stage cluster exists.** `docs/archive/staging-cluster.md` records that the
  `daniel-stage` guest was retired on 2026-09-29 (#2941). `scripts/lib/kubectl.py` keeps
  `stage` only so that a `--cluster stage` request refuses rather than answering for
  production; `scripts/lib/kubectl.py:RETIRED_CLUSTER_NODES` names the guest. The operator
  ruled in #2944 that a stage cluster may return, and none is scheduled.
- **bento-pdf, the leaf.**
    - The entry in `ansible/inventory/host_vars/daniel-box.yml` declares `hostname`
      `bento-pdf`, port 8080, `use_authelia: true` and `auth_tier: one_factor`.
    - The role has one task, an include of `k8s/manifests`, and one template,
      `deployment.yaml.j2`. The Service and IngressRoute come from
      `ansible/templates/service-default.yaml.j2` and
      `ansible/templates/ingressroute-default.yaml.j2`.
    - `ansible/roles/k8s/bento-pdf/defaults/main.yml` sets `k8s_autodeploy: true`. The role
      has no Secret and no PVC. Its template declares a readiness and a liveness probe.
    - Live: `RollingUpdate`, one replica, a `last-applied-configuration` annotation, and a
      pod-template annotation `kubectl.kubernetes.io/restartedAt` from 2026-10-04.
    - Other files mention it only in comments or as examples: authelia's deployment
      template, texbrain's defaults, `scripts/dev/new_k8s_service.py`,
      `scripts/infra_map/groups.py` and `scripts/deploy_tools/shared_role_callers.py`.
- **littlelink was rejected as the leaf.** It is the public link page. netpol-baseline's probe
  Job uses it as the liveness target through Traefik, and
  `ansible/roles/k8s/netpol-baseline/defaults/main.yml` lists it as a `refused` row of
  `netpol_baseline_probe_targets`.
- **The entry has readers, so it stays in `containers_list`.** The survey's D2 summary says the
  leaf leaves the list. C4 keeps it, with `owner: flux`, and this plan follows C4. The readers
  are the filters in `scripts/deploy_tools/narrow_lib/containers.py:FILTER_FIELDS`, among
  them `authelia_service_rules`, `kuma_ingress_monitors` and `homepage_href`, plus the
  shared-default Service and IngressRoute that render from the entry.
- **No template reads an `owner` field, but an edit that changes no read field still reaches
  three templates.** `FILTER_FIELDS` has no `owner` row.
  `narrow_lib/containers.py:reader_reads` returns None for a template that hands
  `containers_list` to a filter outside `FILTER_FIELDS`, and `reader_paths` keeps every hit
  whose reads are None, whatever `changed` holds. Five templates are such readers. The
  count comes from running `reader_reads` over every k8s role template and shared template
  that `renders_containers_list` accepts:
    - pihole's `templates/config/pihole-dnsmasq.conf.j2` (`filter_by_platform`);
    - uptime-kuma's `templates/deployment.yaml.j2` and monitor-bridge's
      `templates/env-secret.yaml.j2` (both `selectattr`);
    - artifacts' `templates/configmap.yaml.j2` (`map(attribute='name')`);
    - netpol-baseline's `templates/networkpolicy-callers.yaml.j2`.

  `narrow_lib/broad.py:_containers_list_tags` adds their tags to the entry's own, and
  `deploy_narrow.py:_deploy_plane` applies the result forward-only, the denylist ignored
  (the DECIDED marker on `denylisted_in`).
- **An added entry reaches every reader, and one reader reads its name alone.**
    - `narrow_lib/containers.py:changed_fields` returns None when the two lists' names
      differ. An added entry therefore reaches every template that renders the list, however
      few fields it carries: 23 tags of 59, traefik, authelia and pihole among them (D1
      *Baseline*). Each of those three declares `k8s_autodeploy: false`.
    - A fixture entry carrying only `name`, `platform: k8s`, `owner: flux` and `depends_on`
      moves one rendered template: artifacts' `configmap.yaml.j2`, whose
      `known_services.json` lists every entry's name. Measured with
      `ansible/tests/_k8s_render.py:render_texts`, the entry appended to daniel-box's list in
      the role context and in `hostvars`. The comparison is against a control render through
      the same override with the list unchanged, because that override alone moves pihole's
      ConfigMap and uptime-kuma's Deployment.
    - Adding `hostname` to the same fixture also moves uptime-kuma's
      `static-monitors.yaml.j2`, which reads the list through `kuma_ingress_monitors`.
- **An empty tag list means a whole run at two sites.**
    - `deploy_narrow.py:_deploy_plane` returns `NARROWED_TO_NOTHING` only from its
      `if not tags` check, because an empty `--tags` value runs the whole playbook. Its
      DECIDED marker cites a 776-second run on 2026-10-10 whose hold parked every landing.
    - `land_lib/deploy.py:deploy_by_host` hands `ln.resolved_tags` to `deploy.sh` as given.
    - `deploy_lib/run.py:derive_changed_tags` is already safe: `--changed` that derives
      nothing ends the run with `Refused(0)`.
- **bento-pdf is the cheapest shared-role smoke caller.**
  `scripts/deploy_tools/shared_role_callers.py:smoke_caller` picks the candidate with the
  fewest hosts, then the fewest templates, then the first name, from `render_targets`. Its
  DECIDED comment names five callers tied at one template, bento-pdf first by name, so a
  `tasks/`-only change to `manifests` smoke-deploys bento-pdf. `expand_shared_tags` turns
  `deploy.sh --tags manifests` into every caller, through `caller_tags`.
- **`render_targets` has three consumers.** `scripts/deploy_tools/render_targets.py:main`
  prints `render_targets(host)` unfiltered, and
  `ansible/roles/setup/render_records/files/render_records.py` runs that CLI with the host as
  its only argument. `shared_role_callers.py` imports the function, and C1 has the renderer
  reuse it.
- **The role glance and the service catalogue read no `owner`.**
  `scripts/docs/gen_role_glance.py` prints the `Deploy tag` line from `entry_tags` and the
  `Auto-deploy` line from `k8s_autodeploy`. bento-pdf's block reads `--tags "bento-pdf"` and
  "eligible". `scripts/docs/service_catalog.py:build_rows` reads the same declaration for
  `docs/reference/services.md`.
- **The paths that apply a k8s role**, which is C4's census:
    1. **The Ansible play.** `ansible/deploy.yml` loops "Deploy k8s workloads in batches" over
       `ansible/tasks/k8s_batch.yml`, whose "Run k8s roles" task includes each entry's role.
    2. **`deploy.sh`.** `scripts/deploy_tools/deploy_lib/run.py:validate_tags` calls
       `scripts/deploy_tools/deploy_lib/tags.py:validate`. The `DEPLOY_*` codes in
       `scripts/lib/exit_codes.py` use 0, 2, 3, 4, 20, 64 and 75 to 79. Code 5 is free. A
       `deploy.sh` code lives in three places: its row in `exit_codes.py:CONTRACTS`,
       `exit_codes.py:DEPLOY_SH_NO_VERDICT` for a code that refuses before any apply, and
       the literal `_REFUSALS` in `.claude/hooks/auto-mode-bridge.py`. In
       `.claude/hooks/tests/test_auto_mode_bridge.py`,
       `test_the_codes_the_hook_holds_are_exactly_the_ones_the_contract_declares` holds
       `_REFUSALS` equal to `DEPLOY_SH_NO_VERDICT`.
    3. **The GitOps deployer.**
        - `deploy_changes.py:services_from_changed_paths` puts every path under
          `ansible/roles/k8s/<role>/` into `cs.k8s`.
        - `deploy_k8s.py:split_k8s_auto_deploy` promotes an image-only bump of a role not on
          the denylist, which bento-pdf qualifies for.
        - `deploy_narrow.py:narrow_deploy_plane` narrows an inventory or template change to
          tags.
        - `deploy_k8s_owed.py` records unapplied k8s roles.
        - `deploy_inventory.py:declared_k8s_services` parses host_vars without YAML.
    4. **`land.sh`.** `scripts/deploy_tools/land_lib/deploy.py:deploy_by_host` runs
       `deploy.sh` once per host.
    5. **Three more `deploy.sh` callers.**
        - `scripts/deploy_tools/render_targets.py` lists the tags for the hourly
          `render_records` dry run. `deploy.sh` refuses the whole tag list when one tag is
          refused.
        - `scripts/secrets_mgmt/secret_rotation.py:cmd_rotate` runs `--deploy` from the
          weekly cron, which reverts the tree when that deploy fails.
        - `ansible/roles/setup/deploy_ui/` starts `land.sh` and `deploy.sh`.
- **Readers that would raise false alarms once Flux owns a role:**
    - `scripts/diagnostics/probe_lib/health.py:release_expected_restarts` reads the last
      Ansible release record and fails a workload as NOT ROLLED when its `restartedAt` is
      older. Flux strips that annotation.
    - `scripts/diagnostics/probe_lib/releases.py`, run by the release-staleness cron through
      `probe.py releases --stale-only`, reads a role whose record has stopped moving as
      stale.
    - `ansible/roles/setup/k3s/templates/manifest-prune-check.sh.j2` compares live objects
      with staged directories that Ansible no longer writes.
    - `ansible/roles/setup/k3s/files/live_drift_check.py` raises none.
      `live_drift_check.py:is_server_side_applied` already takes every object with an
      `Apply` entry in `managedFields` out of scope, and kustomize-controller applies
      server-side. That entry survives a later client-side apply, so an object handed back
      to Ansible would stay out of scope.
- **The two drift checks need a hand apply.** `ansible/roles/setup/k3s/` is applied by
  `ansible/k3s-bringup.yml`, one of the `_BROAD_MANUAL_PREFIXES` playbooks that the deployer
  never runs. The checks carry the tags `manifest-prune` and `live-drift` in
  `ansible/roles/setup/k3s/tasks/health-crons.yml`.
- **The takeover rolls workloads once.** Flux's first server-side apply takes over every
  `kubectl-*` field manager and strips `last-applied-configuration` (survey appendix, claim
  25). Live, 59 of 71 Deployments (40 of them `Recreate`) and 5 DaemonSets carry
  `restartedAt`. bento-pdf is one of them and rolls once.
- **R1's renderer writes `DIR/cluster/<tag>/` only.**
  `scripts/renderer/homelab_render.py cluster --out DIR [--role TAG ...] --secret-stubs FILE`
  writes each role's directory and its `kustomization.yaml`. R1 leaves `DIR/flux/<tag>.yaml`
  unwritten, and without `--secret-stubs` its `cluster` exits 2 (R1 *Design*).
- **R2 decides the render mode by one flag.** With `--secret-stubs FILE` the render is R1's
  stub mode. Without it, the render is in placeholder mode, and each secret variable `K` renders
  as `${K}` (R2 *Mode selection*). Stub mode writes R1's `homelab_secret_stub` form,
  `stub-` plus the key with underscores turned to hyphens, so `domain` becomes
  `stub-domain`.
- **bento-pdf's render names one variable that the baseline holds as a secret, `domain`.**
    - `ansible/templates/ingressroute.yml.j2:ingressroute` builds the `.local.` object's Host
      match as `hostname ~ '.local.' ~ domain`, and the `-public` object's as
      `hostname ~ '.' ~ domain`.
    - `ansible/inventory/group_vars/all.yml:k8s_tls_cert_resolver` is `cloudflare`, so each
      object's `tls.domains` block renders too, with `domain` as `main` and in both `sans`.
    - `domain` is a SOPS key at the baseline: `grep -c '^domain: ENC\['` on
      `ansible/vars/secrets.yml` prints 1. It is in R2's `PLACEHOLDER_ANYWHERE`, so R2's rule
      3 accepts `${domain}` outside a Secret. The operator ruled on 2026-10-14 that `domain`
      is published. Domain PR 1 defines it in `ansible/inventory/group_vars/all.yml`, and
      domain PR 2b takes it out of `PLACEHOLDER_ANYWHERE` and out of the stub set (*Where
      `domain` comes from*).
    - Neither `deployment.yaml.j2` nor `ansible/templates/service-default.yaml.j2` names any
      key of `secrets.yml`.
- **Flux records the source commit in one field only.** In kustomize-controller v1.9.6, the
  version D1 pins, `api/v1/kustomization_types.go` defines two status fields:
    - `lastAppliedRevision` equals the source artifact's revision, which for the
      OCIRepository reads `<tag>@sha256:<digest>`;
    - `lastAppliedOriginRevision` holds the artifact's `org.opencontainers.image.revision`,
      which D1's `flux push artifact --revision master@sha1:<sha>` sets.

  `KustomizationReconciler.reconcile` sets both only after `checkHealth` passes, so an
  advanced origin revision means applied and healthy.
- **D1's `decide` picks its own target.** It walks `origin/master` and returns `promote` with
  a target T, `held`, `current` or `error` (D1 *Decisions*). T need not be the commit whose
  CI run triggered the job.
- **D1's bundle root is `flux/kustomization.yaml` with `resources: []`.** The root
  Kustomization `homelab` applies `./flux`, so a file under `flux/` reaches the cluster only
  when that list names it.
- **After D1, `promote` only moves `prod`.** No `last-good` tag exists, and no step writes
  `DEPLOY_HOLD`.
- **Grafana's deploy annotations are host syslog lines.**
  `scripts/deploy_tools/deploy_lib/playbook.py:annotate`, for `deploy.sh`, and
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_io.py:emit_deploy_annotation`,
  for the deployer, each run `logger -t deploy-annotation` with an `event=deploy` line. Alloy
  ships syslog to `loki-homelab`, and
  `ansible/roles/k8s/observability/defaults/main.yml:observability_deploy_annotation_expr`
  matches `event=deploy` and parses the rest as logfmt. The emitter's docstring rejects a POST
  to Grafana, because that needs a pinned address or a standing write credential through
  Traefik. `ansible/tests/deploy/test_deploy_annotations.py` holds the query to both emitters.
  After D2.7 neither emitter runs for bento-pdf.
- **D1 splits deciding from writing.**
    - D1's `promote` job checks out the workflow's commit. It runs `promote.py decide` with
      `contents: read` and `checks: read`. It sets `PROMOTE_TOKEN` as `GH_TOKEN` only on
      the step that reads `DEPLOY_HOLD`.
    - D1's `tag` job checks out no repository code and holds `packages: write`. It moves
      `prod` to the target that `decide` printed.
    - D2.5 keeps that split for `verify`. A read-only `verify` job runs
      `promote.py verify T` and prints a verdict.
    - A `settle` job applies that verdict. It checks out no repository code, holds
      `packages: write` and sets `PROMOTE_TOKEN`. It moves `last-good`, or it writes the
      hold and then resets `prod`. With the revert rule on, it also clears the hold. Its
      concurrency group is set by an expression: `promote` on a promote run, and
      `promote-reset` on D5's `reset_to_last_good` dispatch. One job then serves both
      without serialising a reset behind a waiting promotion.
    - The D1.4 workflow test asserts each job's permission map exactly. D1's registry client speaks the OCI distribution API to
  `ghcr.io` with a token from GHCR's token endpoint (D1 *Decisions*).
- **`DEPLOY_HOLD` has no empty value.** D1 creates the variable as `none`, because `gh`
  cannot set an empty value, and reads `none` as no hold (D1 *Decisions* and Open question
  9). C2 and C6 still say "empty". This plan writes "clear" for setting `none`.
- **C3 names the source of `domain`.** The operator ruled on 2026-10-14 that `domain` is
  published, so the renderer writes it as a literal and D2.7 waits on no PR of D3. C3
  rejects a Secret made by hand. D3.3a still publishes the `cluster-vars` Kustomization that
  generates `homelab-cluster-vars`, and D3.3b still gives every substituting Kustomization a
  `dependsOn` edge on it (D3 *Which Kustomizations substitute*). bento-pdf is not one of
  them.
- **R2 adds no render-mode flag.** R2 *Mode selection* reads "C1 *Modes* fixes the call,
  and R2 adds no flag". R2's Round 10 entry R2-10-2 records that `--placeholders` was
  removed.
- **R2 and D3 already check which keys a substituting directory references.** R2's rule 1
  requires every Flux reference there to name a key of `secrets.yml`, rule 2 requires every
  reference to be a placeholder, and rule 4 bars `SOPS_MANIFEST_KEYS`. D3's
  `cluster_vars.py check` holds the key names of the file behind `homelab-cluster-vars`
  equal to R2's placeholder set, less `SOPS_MANIFEST_KEYS` (D3 *Where the values come
  from*).

## Design

### The ownership flag and its three readers

`owner` on an entry takes one of two values:

- `flux`: Flux applies the tag. The renderer publishes it, and every Ansible path skips it.
- `released`: the first PR of a handback (*Rollback*). The renderer leaves the tag out of the
  bundle, and every Ansible path still skips it.

An entry without `owner` is Ansible-owned. So there are two predicates, and no reader mixes
them:

- **Ansible-owned** means the entry has no `owner`. Every skip and every drop below reads
  this predicate.
- **Published** means `owner: flux`. Only the publish job's tag list and the census read it.

Three readers answer both predicates:

- the Ansible filter `ansible_owned`, added to `ansible/filter_plugins/toposort.py` beside
  `filter_by_platform`, which keeps the Ansible-owned entries;
- `tag_owners(host_vars)`, added to `scripts/lib/k8s_roles.py` for every script, which maps
  each tag whose entry sets `owner` to its value;
- `service_owners(hostvars_text)`, added to
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_inventory.py`, which returns
  the same mapping. It is a stdlib-only regex parse like `declared_k8s_services`, because the
  deployer runs under `uv run --no-project`.

A census test in `ansible/tests/deploy/` asserts that the three agree on both predicates,
on the live inventory and on a fixture with one `flux` entry and one `released` entry. Each
reader fails on any other value, and the census holds a fixture for that.

`owner` is a deploy-mechanics field. D2.1a names the set in `narrow_lib/containers.py` as
`DEPLOY_MECHANICS_FIELDS`, holding `owner`, and `changed_fields` leaves its members out. D4.1
adds `depends_on` to the same set. Two changes keep such an edit away from every template
reader:

- `reader_paths` returns no hit when `changed` holds no entry. At `70591c307` that also
  dropped the five whole-entry readers under *Baseline*. Since 8e79e95d6 no template reads
  every field, so D2.1a's tests prove this with fixture readers in those shapes.
- The entry's own tag still comes from `entry_change_tags`, which compares whole entries, so
  an `owner`-only edit maps to that tag alone.

The first change also drops any whole-entry reader for an edit that changes no field at all,
such as a comment in the list. `changed_fields` compares every field of every entry, so such
an edit changes no reader's output, and the redeploy it triggers today buys nothing.

A `Census` row in `ansible/tests/repo/test_census_rows_roles.py` reads
`DEPLOY_MECHANICS_FIELDS` and asserts that no Jinja expression, inside `{{ }}` or `{% %}`,
under `ansible/roles/k8s/*/templates/` or `ansible/templates/` reads any member. The predicate
looks inside Jinja code only: at `70591c307`, 21 template files use the word `owner` in
comments or JSON, and none inside a Jinja expression. `owner` is the member the row must
find in the set. Its red fixture is a template that reads `entry.owner`, as the census
harness requires.

**An added or removed entry that is not Ansible-owned (D2.1c).** H1, H2 and H6 add Flux-born
entries before D6.1 removes the deployer's k8s arms. Each such addition reaches the 23 reader
tags today. D2.1c records such an entry by its fields instead of returning None:

- `changed_fields` records the entry under its name, with every field it carries other than
  `name` and the `DEPLOY_MECHANICS_FIELDS` members, even when that set is empty. It adds the
  marker `PRESENCE` to that set.
- `reader_paths` then keeps every whole-entry reader, whose read is None, because
  `changed` holds an entry. *Baseline* names five at `70591c307`, and the tree has none
  since 8e79e95d6. It keeps a `FILTER_FIELDS` reader only when the entry carries a
  field that the reader reads, or when the reader's filter is in `EVERY_ENTRY`.
- `changed_fields` still returns None for an added or removed Ansible-owned entry, for a
  repeated name, and when the entries that both lists hold change their relative order.

`name` is left out because every `FILTER_FIELDS` row but one holds it to look an entry up, or
to label an entry the filter kept for another field. The exception is `entry_names`, which
lists every entry's name, so any added entry moves its output. `EVERY_ENTRY` names that filter,
and `reader_reads` adds `PRESENCE` to its read, so the marker on the added entry meets it.
`test_narrow_filter_tables.py` holds the set to the filters' behaviour: a bare entry appended
to a list moves exactly the `EVERY_ENTRY` filters.

The set exists because the design above was written against `70591c307`. There, artifacts
read the list through `map`, a whole-entry reader. 8e79e95d6 moved it onto `entry_names` before
D2.1c, and leaving `name` out would then drop artifacts, the one reader the bare-entry render
under *Baseline* moves. The entry's own tag still comes from `entry_change_tags`, and D2.2
drops it because Flux owns it.

The same commit moved the other four whole-entry readers onto named filters. So on the tree
of 2026-10-14 a bare Flux-born entry applies four reader tags: artifacts through
`entry_names`, and blackbox-exporter, pihole and uptime-kuma through `lan_hostnames`, which
reads the entry's `platform`. pihole reads it in its dnsmasq include. blackbox-exporter and
uptime-kuma read it through the shared `ingress-host-aliases.yml.j2`. Only artifacts' render
moves. An entry that carries `hostname` adds the readers of `hostname`: authelia's access
rules and observability's Prometheus config, beside uptime-kuma's monitors. Those readers must apply,
because their output then names the new service.

The entry's own tag alone, which X2-06 proposed, is not sound, because the bare-entry render
moves artifacts' `known_services.json`. So the narrowing shrinks the apply but does not empty
it. Every PR that adds or removes a k8s entry before D6.1 still quotes its narrow output and
runs D1.2's stop rule over the reader tags that output names, as M4 does. A failed apply of
those tags takes D1's way out of a hold (D1 *Rollback*). The slices that add
the entries own that step: H1, H2 and H6. Open question 10 states the output they
should expect.

### What each entry point does with a tag that is not Ansible-owned

Every drop happens before the site's own empty-list check, so a list that a drop empties
applies nothing and says so. No site ever hands an empty `--tags` value on.

| Entry point | Behaviour for a tag T that is not Ansible-owned |
|---|---|
| `deploy.yml` k8s play | The batch loop runs over `containers_list \| ansible_owned`. The `containers_list` fact keeps every entry, because templates read it. |
| `deploy.sh --tags T` | Exits `DEPLOY_FLUX_OWNED` (5) before any lock and prints that T lands through `promote`. |
| `deploy.sh --changed` | Drops T before `derive_changed_tags`'s empty check, so a derivation left empty ends as `Refused(0)`, with a note naming T. |
| `deploy.sh --tags manifests` | `expand_shared_tags`'s expansion drops T with a note. A typed T still refuses. An expansion left empty deploys nothing. |
| `deploy.sh --list-services` | Marks T with its owner. |
| GitOps deployer | Routes T's paths to a new `cs.flux` set: fast-forwarded, logged, never deferred, never alerted, never owed. `_deploy_plane` drops T before its `if not tags` check, so a list left empty is `NARROWED_TO_NOTHING`. Ownership is read at the commit being applied. |
| `land.sh` | Splits the PR's tags before `deploy_by_host`. T waits on promotion (below), and `deploy.sh` runs only when an Ansible-owned tag remains. Before it merges, it refuses a PR that makes T Ansible-owned while T's Kustomization exists (*Rollback*). |
| `smoke_caller` | Leaves T out of the candidates. `land.sh` would drop T from its `deploy.sh` call, so a flux-owned smoke caller would exercise nothing. |
| `render_targets.py` CLI | `main` prints a new `render_record_targets(host)`, which leaves T out of the hourly record list. `render_targets()` stays the full universe for the renderer, R1's `compare` and R2. |
| `secret_rotation.py --deploy` | Leaves T out of its redeploy and runs no `deploy.sh` when nothing remains. D3 gives a rotated cluster secret its path. |
| `probe.py health T` | Ignores the release record when T is not Ansible-owned in the tree (`tag_owners`). |
| `probe.py releases --stale-only` | Skips T, read the same way. |
| `manifest-prune-check.sh` | Skips an object labelled `kustomize.toolkit.fluxcd.io/name=<n>` only while a Kustomization `<n>` exists in `flux-system`. It reads that list once per run with the read-only kubeconfig. |
| `live_drift_check.py` | Needs no skip, because `is_server_side_applied` already excludes kustomize-controller's objects. D2.4a narrows that function: a `kustomize-controller` `Apply` entry does not count on an object that carries `last-applied-configuration`. Flux strips that annotation on every apply, so its presence means Ansible applied the object after a handback. |
| `gen_role_glance.py`, `service_catalog.py` | For `owner: flux`, print that Flux applies the role through `promote` and that `deploy.sh` refuses it with exit 5, in place of the deploy tag and the auto-deploy stance. |
| `deploy_ui` | Inherits `deploy.sh`'s refusal and shows its message. |

No skip reads the Flux label alone. After a handback the orphaned objects keep the labels
`kustomize.toolkit.fluxcd.io/name` and `/namespace`, and kustomize-controller's
`managedFields` entry. The two host checks above therefore read the label together with a
live Kustomization or a fresh annotation, and the scripts read the tree.

### The per-tag Kustomization

D2 writes the `DIR/flux/<tag>.yaml` half of C1 that R1 left open. The renderer emits one per
tag passed with `--role`:

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: bento-pdf
  namespace: flux-system
spec:
  sourceRef: {kind: OCIRepository, name: homelab}
  path: ./cluster/bento-pdf
  interval: 10m
  retryInterval: 2m
  timeout: 10m          # manifests_rollout_timeout_default, read from the manifests role
  prune: true
  wait: true
  deletionPolicy: Orphan
  postBuild:            # only for a tag whose render holds a placeholder (C1)
    substituteFrom:
      - {kind: Secret, name: homelab-cluster-vars}
```

`deletionPolicy: Orphan` holds from D2 to D6. Removing a tag from the bundle, by a handback or
by a render bug that drops the tag, then leaves its objects serving instead of deleting them.
D6 decides whether to switch to the default. `postBuild` names C3's one substitution Secret
from D2.6b on. D3.3b adds the `dependsOn` edge on `cluster-vars`, and D4 adds the toposort
edges.

### Where `domain` comes from

The operator ruled on 2026-10-14: "domain is not secret, it's a var for flexibility just in
case of future changes." C3 *The source of `domain`* records the ruling, and D2 follows it.
The plan for the move is the *Publish `domain`* item in R2's *Steps, as PRs*, whose PRs this
section calls domain PR 1, 2a, 2b and 3.

- **The renderer writes a literal.** `domain` stays an Ansible variable, defined in
  `ansible/inventory/group_vars/all.yml`, so it can change in one place. After domain PR 2b,
  bento-pdf's render holds the literal in its Host rules and its `tls.domains`, and holds no
  placeholder. Its Kustomization therefore carries no `postBuild`.
- **D2.7 waits on domain PR 2b, and on no PR of D3.** `homelab-cluster-vars` no longer holds
  `domain`, so bento-pdf needs neither that Secret nor the `cluster-vars` edge.
- **The host gate holds the edge mechanically.** Before domain PR 2b, `domain` is still a
  placeholder, so bento-pdf's render holds `${domain}` in its Host rules. D2.6c's host gate
  accepts only the literal, so it refuses the bundle in every ordering of D2 and D3. The
  source gate refuses it too, but only until D3.3a publishes `cluster-vars`. A D2.7 merged
  too early publishes nothing, and `land.sh` reports `promote-timeout` (*Risks*).
- **Rejected: `homelab-cluster-vars` as the source.** It was the former default. It made
  D2.7 wait on D3.3 and put every routed tag behind decryption, for a value that is not
  secret.
- **Withdrawn: the Ansible-rendered Secret `homelab-identifiers`.** D2.6a added it before
  round 10. C3 names it as no source. Retiring it would have needed a delete, because
  `ansible/roles/k8s/manifests/defaults/main.yml:manifests_prune_allowlist` names no Secret.
- **Withdrawn: the Ansible-rendered Secret `homelab-identifiers`.** D2.6a added it before
  round 10. C3 names it in neither branch. Retiring it would have needed a delete, because
  `ansible/roles/k8s/manifests/defaults/main.yml:manifests_prune_allowlist` names no Secret.

### Publication and verification

```text
render job (one run per green master commit X; a dispatched sha passes D1's ancestry check first):
  published tags (owner: flux), none ─▶ write out/flux/kustomization.yaml, resources: []
  published tags, some ─▶ homelab_render.py cluster --out out --role <tag> ...
                          placeholder mode, selected by passing no --secret-stubs;
                          R2's five rules run inside the render and fail it, and so does
                          a deploy tag named homelab or a SHARED_KINDS member;
                          the render writes out/digests.json (own, footprint, _unclassified)
  promote.py check-bundle out, failing the job before any push:
    census    out/flux/ holds kustomization.yaml plus one <name>.yaml per published tag and
              per SHARED_KINDS member; kustomization.yaml lists exactly those files
    source    a tag without postBuild holds no ${K} for a key name of secrets.yml;
              a tag with postBuild names only Secrets that the bundle generates
    host      every Host(...) in an IngressRoute ends in .<domain>, the literal that
              ansible/inventory/group_vars/all.yml defines
    no-stub   no R1 stub value, stub-<key>, anywhere under out/
  gitleaks over out/ ─▶ hand out/ on as a workflow artifact
publish job (no checkout): push sha-X and sign
promote job (concurrency: promote):
  promote.py decide: pull prod's artifact once; per candidate T, pull sha-T;
                     skip a T that loses an object no Removes-Object trailer declares
                     ─▶ promote T | held | current | error
  promote T ─▶ a pull error exits non-zero, nothing moves
  held or current ─▶ nothing moves;   error ─▶ exit non-zero, nothing moves
tag job (no checkout; packages: write): tag prod = sha-T, write PROD_DIGEST
verify job (read-only, statuses: read): promote.py verify T
     expected statuses = flux/homelab + flux/<name> for every name in T's changed set:
       footprint differs from prod's digests.json, plus dependsOn dependents,
       minus suspended; a dependent already failing on prod's commit is reported only
     every one success                ─▶ verdict good
     any failure, or timeout          ─▶ verdict failed=<names>
settle job (no checkout; packages: write; PROMOTE_TOKEN):
     good   ─▶ tag last-good = sha-T
     failed ─▶ set DEPLOY_HOLD = "<T> failed=<names>",
               then tag prod = last-good, write PROD_DIGEST
```

C2 splits the render from the signing, and the decisions from the writes.

- The render job holds read-only permissions.
- Only jobs that check out no repository code hold `packages: write`: `publish`, `tag` and
  `settle`.
- Only `publish` holds `id-token: write`.

D1 builds the split. D2 adds its steps to the render job, and it adds the `verify` and
`settle` jobs.

- **The render mode is stated by the absent flag.** C1 requires the publish command to say
  that it renders in placeholder mode. R2 offers no flag for that mode: R2 *Mode selection*
  reads "R2 adds no flag", and R2's review entry R2-10-2 records that `--placeholders` was
  removed. So the command states its mode by passing no `--secret-stubs`, and a test on
  `.github/workflows/promote.yml` asserts that absence. The no-stub and host gates catch a
  stub render that reaches `out/` by any other route.
- **An empty tag list never reaches the renderer.** A `cluster` call with no `--role` renders
  every tag, so the publish step writes D1's empty root itself. The census would refuse a
  full render in any case.
- **A gate refusal moves nothing.** The gates run before the push. With no `sha-X` tag,
  `decide` cannot choose X, and `prod` stays where it was. The next commit that passes them is
  promoted as usual.
- **The source gate is scoped by tag.** C1 makes a tag's Kustomization the one source for
  whether the tag substitutes, so the gate reads `spec.postBuild` there.
    - *A tag without `postBuild`* must hold no `${K}` whose K is a key name of
      `ansible/vars/secrets.yml`. That is C1's refusal. SOPS leaves the key names in
      plaintext, and the gate reads them the way R2's rule 1 does. A shell `${HOME}` in such
      a tag passes, because Flux never substitutes there.
    - *A tag with `postBuild`* may name in `substituteFrom` only a Secret that a
      Kustomization in the same bundle generates through a `secretGenerator`. Under C3's
      kind that is `cluster-vars`, which D3.3a adds. Until then the gate refuses every
      substituting published tag. bento-pdf does not substitute after domain PR 2b, so D2.7
      needs none of this, and a later tag that substitutes waits for D3.3a.
    - *Which keys that Secret holds* is not checked a third time. R2's rules 1, 2 and 4 and
      D3's `cluster_vars.py check` already cover it (*Baseline*). Every reference in a
      substituting tag is then a key that `homelab-cluster-vars` carries.
    - The gate scans as R2's rule 2 does, pairing each `$$` first, so an escaped `$${` is
      not a reference. It asserts that it examined every published tag's directory, and the
      census fails a published tag with no directory, so it never passes vacuously.
- **The host gate is the red proof for the domain.** Every Host rule must end in
  `.<domain>`, where the value is the literal that `ansible/inventory/group_vars/all.yml`
  defines. The gate reads it from the tree location the renderer reads it from. A
  `${domain}`, a `stub-domain` and a hand-written host each fail.

  bento-pdf's `.local.` route is the gate's named member, so a gate that stops matching
  IngressRoutes fails rather than passing vacuously. The gate's tests use a fixture tree
  that copies the route template, because the real render carries `${domain}` until domain
  PR 2b lands. D2.7 runs the gate over the real tree.
- **`verify` checks T and reads T's own bundle.** `decide` may choose a T newer than X, the
  commit whose CI triggered the run (D1 *Decisions*). So `verify` waits for statuses on T. It
  derives the expected set from the `sha-T` artifact, pulled with the flux CLI the job already
  installs, and never from the job's own `out/`, which is X's render. Verifying X when T is
  not X would wait on statuses that Flux never posts, time out, and write a false hold. The
  pull happens before `prod` moves, so a registry error follows D1's rule that an error
  promotes nothing.
- **`decide` makes two kinds of artifact pull.** It pulls `prod`'s artifact once per run,
  before `prod` moves, for that bundle's `digests.json` and its object set. It also pulls
  `sha-T` for each candidate it weighs, for the object check below, and `verify` reuses the
  last of those pulls. A run that weighs one candidate therefore makes one more pull than
  D1's design. A failed pull is an `error`, and nothing moves.
- **Expected statuses come from names, not files.** Each document of apiVersion
  `kustomize.toolkit.fluxcd.io/v1` and kind `Kustomization` under the bundle's `flux/` names
  one Kustomization that `verify` may expect. The kustomize `kustomization.yaml` in that
  directory is not one of them. D3's `cluster-vars` and D4's `-deferred` documents are then
  candidates with no change to `verify`, which settles D4's Open question 5.
- **`verify` expects only T's changed set (C2 *Verification*).** Every Kustomization
  reconciles on each revision it receives, so expecting all of them gated a promotion on
  the whole fleet. A crashloop that predated the change, in one unrelated leaf, then held
  every promotion and reset `prod`, rolling back other PRs' healthy changes. `verify`
  expects `flux/homelab` plus `flux/<name>` for each name in T's changed set:
    - the names whose `footprint` digest differs between T's `digests.json` and `prod`'s,
      read before the move;
    - every Kustomization whose `dependsOn` chain in T's bundle reaches one of those names.
      A tag's `-deferred` and `-verify` Kustomizations join through that rule;
    - minus each Kustomization whose document in T's bundle sets `spec.suspend: true`.
      `verify` cannot see a suspend made live outside the bundle, so such a Kustomization
      stays expected, and its missing status times out into a hold. That is the safe
      direction.

  Any change under `_unclassified` puts every name in the set. A bundle with no
  `digests.json`, published before D2.6b, counts as every name changed. A dependent whose
  `flux/<name>` status on `prod`'s commit already read `failure` is reported in the job log,
  not expected, which costs one more status read per run. An empty changed set expects
  `flux/homelab` alone. A failure outside the set never holds, and the Flux rule group (C7)
  pages it instead.
- **A non-tag Kustomization has a place from the start.** A shared kind, such as D3's
  `cluster-vars`, is written as `out/flux/<name>.yaml`, with its manifests in
  `out/cluster/<name>/`. The census computes its allowlist from inputs, never from the
  render: the published tags plus `SHARED_KINDS`, a closed literal set that is empty at D2.
  The PR that adds a kind adds its name, with a fixture that passes. D4 Open question 5 agrees
  that D2 owns this layout, the census and the name-based statuses. C2 and C3 credit D2.5
  and D2.6b, as Open question 6 records.
- **The renderer refuses a reserved tag name, as C3 places it.** D2.6b's render fails on a
  deploy tag named `homelab`, the root Kustomization's name, or named after a member of
  `SHARED_KINDS`. D4.2 adds the `-deferred` suffix to the same check, and D5.1a1 adds
  `-snapshot` and `-verify`. `check-bundle` repeats none of these.
- **`digests.json` is written by the render (C1).** D2.6b, D2's first publishing PR, adds
  it at the bundle root. It maps each deploy tag and each `SHARED_KINDS` member to an `own`
  and a `footprint` digest, and records under `_unclassified` every path that counts for no
  name. The renderer computes the digests in process, and `decide`, `verify` and `land-wait`
  read them without recomputing. Each `SHARED_KINDS` member needs a footprint rule that
  names the tags its files count for, and a census in `scripts/renderer/tests/` fails a
  member without one (C3).
- **`decide` refuses a candidate that silently loses an object.** A render that drops a
  NetworkPolicy, IngressRoute, CronJob, Service or RBAC object gets it pruned, while its
  Kustomization stays Ready, so `verify` passes. `decide` therefore compares the set of
  objects, as kind, namespace and name, in candidate T's `cluster/` with the set in
  `prod`'s. T is eligible only when every lost object is declared by a
  `Removes-Object: <kind>/<namespace>/<name>` trailer on some commit in (`prod`, T]. A
  cluster-scoped object leaves the namespace empty, as in `ClusterRole//<name>`. `decide`
  reads the trailers with `git log` over that range in the promote job's checkout of master,
  which therefore fetches history back to `prod`'s commit.
    - The check runs in `decide`, not in `check-bundle`, because only `decide` knows the
      `prod` that T replaces. A reset or a later promotion can move `prod` after X was
      published.
    - An ineligible T is skipped with a log line that names each undeclared object, and
      the walk goes on to older candidates. When every candidate loses the object, `decide`
      prints `current`. The `PromotionStalled` alert pages after 2 hours, and a later
      commit that carries the trailer releases the walk.
    - A tag whose whole Kustomization leaves the bundle is exempt, such as at a handback's
      `owner: released`. `deletionPolicy: Orphan` prunes none of its objects. The exemption
      holds while C3's Orphan policy does, and D6.7's switch must remove it.
    - D5 generalises its PR-time `Removes-Workload` check to `Removes-Object` in parallel,
      with the same trailer grammar. The PR-time check stops the PR, and `decide` stops a
      commit that reaches master without one.
    - A squash merge keeps only the message it is given, so `land.sh` copies every
      `Removes-Object:` trailer from the PR's commits into the squash commit's body. D2.6c
      adds the copy, with a test whose red half drops one trailer. A merge that bypasses
      `land.sh`, such as Renovate's automerge, carries no trailers. Such a bump that drops
      an object therefore stalls at `decide`, and `PromotionStalled` pages. That is the
      intended direction (D5.1f).
- **The hold names what failed.** `verify` writes `<T> failed=<names>` (C2), where `<names>`
  comma-joins the expected names that read `failure` or had no status at the timeout. At the
  drill that is `<T> failed=bento-pdf`.
- **The revert rule is code behind a flag that stays off (C2).** D2.5 writes the rule and a
  module constant in `promote.py` that disables it. Turning it on is an **operator-only**
  ruling (Open question 11), made in a PR of its own. While the flag is off, `decide` treats
  a `failed=` hold like any other hold, and every hold clears only by hand. With the flag
  on:
    - `decide` may promote a candidate U that descends from the held T when, for every name
      in `<names>`, U's `footprint` digest equals `last-good`'s. Reading `last-good`'s
      `digests.json` costs one more pull, made only while a `failed=` hold stands;
    - when U verifies, `verify` sets `DEPLOY_HOLD` to `none`;
    - a hold in any other form, and a bundle with no `digests.json`, never qualify.
- **`PROD_DIGEST` records each move of `prod` (C2).** Each time `promote.py` moves `prod`,
  on a promotion or a reset, it writes the digest it moved to into the repo variable
  `PROD_DIGEST` with `PROMOTE_TOKEN`. The deploy-pipeline exporter (D2.6d) compares it with `prod`'s live
  digest. The first move after D2.5 creates the variable.
- **The hold is written before the reset**, the order `deploy_handlers.py:_rollback_k8s`
  uses for `hold_sha`. If the reset fails, the hold still stops the next promotion. If the
  hold write fails, `verify` still attempts the reset and then exits non-zero, naming both
  results. The reset puts `prod` back on a verified revision. Without a hold, the next run
  may promote T again, which fails `verify` again and retries the hold. That costs one more
  failed rollout per run while the old pod serves. Skipping the reset instead would leave
  Flux retrying T, and the next run could build newer commits on it.
- **The verify step's permissions and token.** D2.5 adds `statuses: read` to the `promote`
  job, for the commit-status read with `GITHUB_TOKEN`. The `last-good` and `prod` tag moves
  use `GITHUB_TOKEN` logged in to `ghcr.io`, as D1's tag moves do. `PROMOTE_TOKEN` is set as
  `GH_TOKEN` on the verify step as well, because only that token writes `DEPLOY_HOLD`
  (D1 *Decisions*).
- **The timeout is derived, not chosen.** It is the largest Kustomization `timeout` in the
  bundle, plus the OCIRepository interval, plus the Kustomization `retryInterval`.
- **The first verified run seeds `last-good`.** When `last-good` is absent, `promote.py`
  tags the current `prod` as `last-good` before it moves `prod`.
- **`DEPLOY_HOLD` has a way out and a way to see it.** Clearing it is **operator-only**:
  `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`, the no-hold value D1
  uses. Then `gh workflow run promote.yml --ref master -f sha=<master tip>` re-runs
  promotion after D1's CI re-check of that commit, and `decide` promotes the newest eligible
  commit. The deploy UI's Clear button covers `hold_sha` only, and Open question 4 asks
  whether it should cover `DEPLOY_HOLD`.
- **`probe.py landing` prints both holds while they coexist.** Until D6 deletes the
  deployer, every run prints a `hold_sha` line and a `DEPLOY_HOLD` line, each naming its
  source and the plane it holds, even when one reads `none`. A `failed=` hold prints its
  names. `prod` and `last-good` follow (below).
- **The `sha` input passes D1's ancestry check before the render.** D2.5 adds the dispatch
  input `sha`, and the render job's checkout reads it. Before rendering, the job runs D1's
  `git merge-base --is-ancestor` check of that commit against `origin/master`, then D1's CI
  re-check. A commit that is not on master stops the run before any render, so a dispatch
  publishes only a master commit (C2 *Only master commits publish*).

### What `probe.py landing` reads, and what it costs

daniel-box has no flux CLI. A host that polls `api.github.com` without a token exhausts the
per-host anonymous quota, and the deployer's CI gate then reads as pending. So each fact
comes from a named source:

- **`DEPLOY_HOLD`** comes from `gh variable get`, one authenticated `api.github.com` call.
- **`prod`** comes from the OCIRepository `homelab`'s `status.artifact`, read with the
  read-only `kubectl`. Its `revision` gives the digest. Its `metadata` entry
  `org.opencontainers.image.revision` gives the commit, which source-controller copies from
  the manifest (D1 *Baseline*). This costs no GitHub call. It lags the tag by at most the
  OCIRepository's interval.
- **`last-good`** comes from one manifest fetch for the tag `last-good` from `ghcr.io`,
  through `promote.py`'s own registry client (D1 *Decisions*). It needs one token request
  and one fetch, and neither is an `api.github.com` call. The token is GHCR's anonymous pull
  token, which works only while D1 Open question 2 keeps the package public. The manifest's
  `org.opencontainers.image.revision` annotation names the commit. Listing package versions
  is rejected, because its cost grows with the version count, as D1 found for
  `flux list artifacts`.

One run therefore costs one `api.github.com` call, two `ghcr.io` requests and one cluster
read. When the `last-good` read fails or times out, the line reads `last-good: unknown`, and
the probe never reports `prod` equal to `last-good`. The failure downgrades the line, not
the run. **Precondition for merging D2.5:** its PR body times each of the three reads three
times against the live system, and each timeout is set from that measurement. D1's artifact
exists by then, so each read has a subject.

### How `land.sh` waits

C6 *`land.sh`* states the rule: after merging M, wait until M is covered, never on one
`promote` run, because GitHub may evict a queued run and a run may promote a newer T. The
promotion that covered M is whichever one applied a commit that contains M, read from the
cluster rather than from a run. `promote.py land-wait M` takes M's changed set (C6): the
changed set of C2, taken between M's `digests.json` and the one on the newest published
ancestor of M's first parent. It reads both files from GHCR and derives no tags itself. A
missing `digests.json`, or a read that fails, counts as every name changed, and the verdict
line says it widened. It then polls until one of these holds:

- Every Kustomization in that set, and `homelab`, is Ready, and its origin revision names a
  commit R that descends from M or equals it. The change is applied and healthy, and the
  result is `settled`. From D5 on, `flux/soak` must also read `success` on R.
- `DEPLOY_HOLD` holds a value other than `none`. The result is `deploy-failed` with a new
  `Cause`, `promote-held`, and the message names the held SHA and, for a `failed=` hold, the
  failed names. That SHA may contain M or predate it. A hold set before M merged ends the
  wait at the first poll.
- The wait reaches twice the promote timeout. The result is `gave-up` with a new `Cause`,
  `promote-timeout`.

**Where each fact comes from, and what it costs.** daniel-box has no flux CLI. A host that
polls GitHub without a token exhausts the per-host anonymous quota, which makes the
deployer's CI gate read as pending. So `land-wait` reads:

- each Kustomization's `status.lastAppliedOriginRevision` and Ready reason with the
  read-only `kubectl`, which costs no GitHub call. `land-wait` takes R from the
  `master@sha1:<40-hex>` form. An absent value, or any other form, keeps the wait going and
  never reads as `settled`. `status.lastAppliedRevision` is not read, because it holds the
  `<tag>@sha256:<digest>` form, which no git command takes;
- ancestry with `git merge-base --is-ancestor M R` in the checkout that `land.sh` already
  fetches. Exit 0 is yes and exit 1 is no. Any other exit, such as 128 for an R the checkout
  has not fetched yet, re-fetches and keeps waiting;
- `DEPLOY_HOLD` with `gh variable get`, one authenticated call per poll;
- from D5 on, the combined status of R, once, when the tags are Ready.

It polls every 30 seconds, so a landing spends at most 120 GitHub calls an hour, well inside
an authenticated token's 5,000. The land user's `gh` token must be able to read repository
variables. D2.3 checks that as that user before merging, and a missing scope is an
**operator-only** token change. `probe.py landing` and the deploy UI read the hold the same
way, and they read `prod` and `last-good` as *What `probe.py landing` reads* says.

On `settled`, `land.sh` runs `probe.py health <tag>` for each published tag in the PR, as it
does for an Ansible-owned one.

On `settled`, `land-wait` also writes the deploy annotation that `deploy.sh` and the deployer
write for an Ansible apply. It is one `logger -t deploy-annotation` line, `event=deploy
services=<the PR's published tags> sha=<R, 8 characters> result=ok source=promote`, and it is
fire-and-forget, as `playbook.py:annotate` is. `deploy-failed` and `gave-up` write none.
`promote` cannot write the line itself: it runs on a GitHub runner, and the line reaches
Grafana only through the host's syslog. A promotion that no `land.sh` follows draws no
annotation (*Risks*).

**The split comes before any `deploy.sh`.** `land.sh` removes every tag that is not
Ansible-owned from the list before `deploy_by_host` sees it. When nothing remains,
`deploy_by_host` is never called, because it hands its list to `deploy.sh` as given. So a PR
whose only service is flux-owned, D2.7 among them, waits on promotion and runs no
`deploy.sh` at all.

**`land.sh` refuses a handback merge while Flux still owns the tag.** Before it merges a PR,
`land_lib/merge.py` compares `tag_owners` at the PR's base and head. For each tag that the
PR makes Ansible-owned, it reads
`kubectl get kustomizations.kustomize.toolkit.fluxcd.io -n flux-system <tag>` with the
read-only kubeconfig. It refuses the merge while any such Kustomization exists, and
`NotFound` lets the merge proceed. That one read, made only for a handback PR, is the guard
on every path that the merge would otherwise start (*Rollback*).

### The deploy-pipeline exporter and its alerts

Without it, `promote`'s verdicts live only in Actions logs. D2.6d adds the deploy-pipeline
exporter as a monitor-bridge family (C7), and D2.7 waits on M1, so the pipeline pages
through Alertmanager from the first real flip.

- **Four gauges, one series each, refreshed each 300-second cycle:**
    - `homelab_deploy_hold_active` reads 1 while `DEPLOY_HOLD` reads anything but `none`;
    - `homelab_deploy_hold_age_seconds` is the age of the variable's last update while a
      hold stands;
    - `homelab_deploy_unpromoted_age_seconds` is the age of the oldest green master commit
      that `prod`'s commit does not contain, and 0 when there is none;
    - `homelab_deploy_prod_digest_match` reads 1 when `prod`'s digest in GHCR equals
      `PROD_DIGEST`. D1 creates `PROD_DIGEST` as `none`. While it reads `none` and no `prod`
      tag exists, the gauge reads 1, because nothing has moved `prod` yet.

  A hold's failed names stay out of the labels, so the series count never grows with them.
- **Where it reads, per cycle.** One variables call returns `DEPLOY_HOLD` and `PROD_DIGEST`.
  One call lists master's successful CI runs. One compare call checks the oldest run's
  commit against `prod`'s. `prod`'s digest and commit come from one GHCR manifest fetch, the
  way `probe.py landing` reads `last-good`. That is three `api.github.com` calls per cycle,
  or 36 an hour.
- **Its token is read-only.** The family needs its own fine-grained token, limited to this
  repository, with Actions read and Variables read and no other scope. It holds no write
  scope, so the exporter can neither clear a hold nor move a tag. The operator mints it
  (**operator-only**). It joins monitor-bridge's env-secret through `/add-secret` and the
  rotation registry. D2.6d's PR body quotes one compare call made with it, which shows the
  two scopes suffice. If the call fails, adding Contents read is an **operator-only** token
  change.
- **Exposition.** monitor-bridge pushes its checks to Uptime Kuma. D2.6d adds the scrape
  endpoint this family needs, and a scrape job in the observability role's
  `prometheus.yaml.j2`. The scrape adds the four gauges and five automatic series, nine in
  all, against M1's series budget (C7).
- **The rule group `deploy-pipeline`**, in M1's rules directory, with a promtool test file
  beside M1's:
    - `DeployHoldActive`, severity `warning`, fires when the hold gauge reads 1 for 15
      minutes. Its Alertmanager route repeats it every 24 hours while the hold stands.
    - `PromotionStalled`, severity `warning`, fires when the unpromoted age exceeds 2 hours
      and the hold gauge reads 0.
    - `ProdMovedOutsidePromote`, severity `critical`, fires when the match gauge reads 0 for
      15 minutes. The 15 minutes cover the gap between a `prod` move and its `PROD_DIGEST`
      write, and one exporter cycle.
    - `DeployPipelineExporterAbsent` is the zero-series case that M1's rule 2 requires of
      every family. It fires when the four gauges are absent. The bridge's scrape job
      carries no `origin` label, so, under M1's convention 5, the `absent()` is conjoined
      with `up == 1` until M6's batch A3. Kuma's `Cluster Scrape Targets` tile pages a
      down bridge meanwhile.

  Each rule carries `service="deploy-pipeline"`, the literal C7 *Rule labels* admits (Open
  question 12).

### Tree diff

```diff
  ansible/filter_plugins/toposort.py                     + ansible_owned              (PR D2.1d only)
  ansible/deploy.yml                                     batch loop over              (PR D2.1d only)
                                                           containers_list | ansible_owned
  scripts/lib/k8s_roles.py                               + tag_owners
  scripts/lib/exit_codes.py                              + DEPLOY_FLUX_OWNED = 5: CONTRACTS row,
                                                           DEPLOY_SH_NO_VERDICT member
  .claude/hooks/auto-mode-bridge.py                      _REFUSALS gains 5
  scripts/deploy_tools/deploy_lib/tags.py, run.py        refuse, drop or mark a tag
  scripts/deploy_tools/shared_role_callers.py            expansion and smoke_caller drop it
  scripts/deploy_tools/narrow_lib/containers.py          + DEPLOY_MECHANICS_FIELDS; an edit
                                                           with no changed field reaches no reader;
                                                           an added Flux-born entry is recorded
                                                           by its fields (D2.1c)
  ansible/roles/setup/gitops_deploy/files/deployer_lib/
    deploy_inventory.py                                  + service_owners
    deploy_changes.py                                    + ChangeSet.flux
    deploy_narrow.py                                     drop before the empty check
    deploy_k8s_owed.py                                   drop
  scripts/deploy_tools/land_lib/deploy.py                split before deploy_by_host
  scripts/deploy_tools/land_lib/merge.py                 refuse a handback while the
                                                           Kustomization exists
  scripts/deploy_tools/land_lib/outcome.py               + Cause promote-held, promote-timeout
  scripts/deploy_tools/promote.py                        + verify T over the changed set,
                                                           land-wait, check-bundle, the
                                                           object-loss check in decide, the
                                                           revert rule (flag off), PROD_DIGEST;
                                                           land-wait annotates a settled landing
  scripts/deploy_tools/render_targets.py                 + render_record_targets, used by main
  scripts/secrets_mgmt/secret_rotation.py                drop in --deploy
  scripts/diagnostics/probe_lib/{health,releases}.py     skip by tree ownership
  scripts/diagnostics/probe_lib/landing_blockers.py      print both holds, prod, last-good
  scripts/docs/gen_role_glance.py, service_catalog.py    print the Flux owner
  ansible/roles/setup/k3s/templates/manifest-prune-check.sh.j2   skip while the Kustomization
                                                                   exists
  ansible/roles/setup/k3s/files/live_drift_check.py              a handed-back object counts
  scripts/renderer/                                      + DIR/flux/<name>.yaml and root writer,
                                                           postBuild, SHARED_KINDS (empty),
                                                           reserved tag names, digests.json
                                                           with its footprint rules
  .github/workflows/promote.yml                          render, check-bundle, gitleaks, verify T,
                                                           dispatch with the ancestry check;
                                                           statuses: read
  ansible/roles/k8s/monitor-bridge/                      + deploy-pipeline family, scrape
                                                           endpoint, read-only token (D2.6d)
  ansible/roles/k8s/observability/                       + scrape job, deploy-pipeline rule
                                                           group and its promtool tests (D2.6d)
  ansible/vars/secrets.yml, ansible/secret_rotation.yml  + the exporter's token (D2.6d)
  ansible/inventory/host_vars/daniel-box.yml             bento-pdf: owner: flux   (PR D2.7 only)
```

## Steps, as PRs

PRs D2.1a to D2.6d change how no service is deployed: no entry carries `owner` until D2.7,
and each PR tests its skip with a fixture entry. D2.1d is the exception at its landing. It
edits two paths that every play reads, so the deployer refuses to narrow it and records
every declared k8s service as owed. D2.1d therefore carries its own landing procedure. D2.7
is the one commit in which both writers change, which is how C4's "in that same PR" holds.
D2.6a is withdrawn in round 10. D2.6b and D2.6c keep their IDs, because D3 and D4 cite them.
"D2.1" in other plans means D2.1a to D2.1d, and "D2.4" means both of its halves. D2.1d keeps
its letter after D2.1c, although it needs neither D2.1b nor D2.1c and lands before D2.2.
D2.6d waits on M1. D3's sub-edge on "D2.1 to D2.6" excludes D2.6d, because D3 reads nothing
that D2.6d adds.

1. **D2.1a — The flag's script reader and the narrowing** (about 120 lines).
    - Add `tag_owners`, with both values of `owner`. Add `DEPLOY_MECHANICS_FIELDS`, leave
      its members out of `changed_fields`, and make `reader_paths` return no hit when
      `changed` holds no entry. The Ansible filter is D2.1d's, so that no other change
      shares its landing.
    - Tests in `scripts/deploy_tools/tests/`, an `_is_clean` and `_is_flagged` pair with
      five fixture readers in the *Baseline* whole-entry shapes present
      (`_narrow_fixtures.py:WHOLE_ENTRY_READERS`). No live template has that shape since
      8e79e95d6. An `owner`-only edit maps to the entry's own tag alone. A `hostname` edit on
      the same entry still reaches all five fixture readers.
    - The `owner` census row, reading `DEPLOY_MECHANICS_FIELDS`.
2. **D2.1b — `deploy.sh` refuses, drops and marks** (about 150 lines).
    - Add the `tags.py` and `run.py` behaviour from the table, and the drop in
      `expand_shared_tags`'s expansion.
    - Register exit 5 in three places. `exit_codes.py:CONTRACTS` gains the `scripts/deploy.sh`
      row `DEPLOY_FLUX_OWNED`, whose meaning reads "the tag is applied by Flux through
      `promote`, so NOTHING was deployed" and whose verdict is `flux-owned`.
      `DEPLOY_SH_NO_VERDICT` gains 5, because it refuses before any apply. The `_REFUSALS`
      literal in `.claude/hooks/auto-mode-bridge.py` gains 5.
    - Tests in `scripts/deploy_tools/tests/`: an explicit `flux` or `released` tag exits 5,
      and an Ansible-owned tag still passes. `--changed` that derives only such a tag ends
      as `Refused(0)` and names it. `--tags manifests` drops it and keeps the other callers.
      The contract-driven tests in `test_deploy_exit_codes.py` print exit 5's meaning, and
      the hook test that holds `_REFUSALS` equal to `DEPLOY_SH_NO_VERDICT` passes with 5 in
      both. Add the `deploy.sh` callers census row (*Risks*).
    - Edit `docs/deploying.md` for exit 5, and `.claude/skills/deploy/SKILL.md`.
3. **D2.1c — An added Flux-born entry reaches only the readers it can move** (about 120
   lines). It needs D2.1a.
    - Record an added or removed entry that is not Ansible-owned in `changed_fields`, as
      *The ownership flag and its three readers* says. Log one `narrow:` line naming the
      entry and the fields it counted. The `changed_fields` docstring states the new case
      beside the cases that still return None.
    - Tests in `scripts/deploy_tools/tests/`, an `_is_clean` and `_is_flagged` pair with
      D2.1a's five fixture whole-entry readers present:
        - an added `owner: flux` entry that carries only `name` and `platform` reaches its
          own tag and the five fixture readers, and no reader of `port`, `tier` or
          `metrics`. Its removal reaches the five and no own tag. On the live tree the same
          entry reaches the four readers that Open question 10 names;
        - the same entry with `hostname` also reaches the readers of `hostname`;
        - an added Ansible-owned entry with the same fields still returns None, and so does
          a bare `owner: flux` entry added in an edit that swaps two other entries.
    - An `EVERY_ENTRY` pair in `test_narrow_filter_tables.py`: a bare entry appended to a
      list moves exactly the filters `EVERY_ENTRY` names, `entry_names` among them, and a
      filter with a row per entry is moved.
    - A render census in `ansible/tests/k8s/`, through `_k8s_render.py:render_texts`. It
      appends a fixture entry to daniel-box's `containers_list`, in the role context and in
      `hostvars`, and compares with a control render through the same override and the list
      unchanged. The census compares at tag level, never by template path:
        - each moved `(role, template)` pair that `render_texts` returns maps to its role's
          deploy tag;
        - every such tag must be among the tags that `narrow_lib/broad.py:_list_reader_tags`
          returns for that edit's `changed_fields`. That function maps a hit to a tag
          through `_sort_hits`, `template_importers` and `_role_tags`, as the deployer does.

      A path comparison would fail on pihole. Its `configmap.yaml.j2` reads the list only
      through `{% include 'config/pihole-dnsmasq.conf.j2' %}`, so
      `containers.py:renders_containers_list` is False for it and `reader_paths` keeps the
      included file, while `render_texts` reports `('pihole', 'configmap.yaml.j2')` as moved.
      Its named members:
        - the artifacts tag, whose `configmap.yaml.j2` moves for the bare fixture;
        - the uptime-kuma tag, whose `static-monitors.yaml.j2` moves for the `hostname`
          fixture;
        - the pihole tag, whose `configmap.yaml.j2` moves for the `hostname` fixture. pihole
          reads the list through `hostvars` and through an include, so this member fails
          the census if the override never reaches a `hostvars` reader or if the mapping
          loses an included reader.

      Its red half hands the census an own-tag-only `changed`, for which `_list_reader_tags`
      returns no reader, so the census must fail on artifacts.
    - Inert for bento-pdf: D2.7 adds a field to an entry and adds no entry.
4. **D2.1d — The Ansible play skips a non-Ansible-owned entry** (about 60 lines). It needs
   nothing else in D2, and it lands before D2.2, whose three-reader census reads all three
   readers. The PR holds only these two play edits and their tests, so the refusal it draws
   is its own.
    - Add `ansible_owned` to `ansible/filter_plugins/toposort.py` beside
      `filter_by_platform`. Batch `containers_list | ansible_owned` in `ansible/deploy.yml`.
    - Tests in `ansible/tests/deploy/`:
        - the batch loop skips a fixture `flux` entry and a fixture `released` entry, while
          a template still sees both in `containers_list`;
        - on every host's live `containers_list`, the entries that `ansible_owned` drops are
          exactly the entries that carry `owner`. While no entry carries `owner`, that set
          is empty, so the play batches the same entries in the same order and visits
          every role it visits today. The test stays true after D2.7, when the dropped set
          is bento-pdf. Its red fixture is a filter that drops an entry without `owner`.
    - **Before the PR**, the session quotes `uv run python scripts/deploy_tools/deploy_tags.py
      narrow origin/master HEAD`. Expect one refusal, not a tag list, because `narrow`
      stops at its first `CannotNarrow`. It names either path:
        - `ansible/deploy.yml`, a `PLAY_PREFIXES` path, for which
          `narrow_lib/broad.py:broad_path_tags` raises "is read by every deploy";
        - `toposort.py`, whose filters `deploy.yml` calls. `_filter_tags` reaches
          `_sort_hits`, which raises "which every deploy runs".

      A tag list instead means the deployer would apply it, so the session stops and finds
      out why. The PR body also quotes the equality test's output.
    - **Landing.** Land with `land.sh`. The tick refuses the narrowing and applies nothing.
      Neither refusal names a tag, so `deploy_broad_k8s.py:fold_deferred` records a
      `k8s_unapplied` line for every declared k8s service, the whole fleet. No `hold_sha`
      is written, and no other landing parks. `land.sh` reports `needs-manual-apply`.
      `deploy_k8s_owed.py:discharge_k8s_unapplied` drops a line on its own once that
      service deploys at or after the recorded SHA. The lines that remain are discharged
      one of two ways. The choice was **operator-only**, and the operator ruled for the
      clear on 2026-10-13 (Open question 9):
        - **Default, the clear.** The operator clears each remaining line with `uv run
          python scripts/deploy_tools/gitops_state.py clear-owed k8s_unapplied <svc>`, one
          service per call. The grounds are the equality test and the diff: the play
          batches the same entries, and no role or template changed, so no service renders
          or applies differently. `deploy_k8s_owed.py:log_k8s_unapplied` names the lines in
          the journal.
        - **Alternative, a full deploy.** The session runs D1.2's stop rule over every
          declared k8s service, then `./scripts/deploy.sh` over every such tag from a
          checkout at `origin/master`. That run applies traefik, authelia and pihole
          forward-only, with any change of theirs that is on master and not yet applied.
    - **A revert of D2.1d** edits the same two paths, so it refuses the same way and takes
      the same procedure. After D2.7, revert it only after the handback (*Rollback*).
5. **D2.2 — The deployer skip** (about 150 lines).
    - Add `service_owners`, `ChangeSet.flux`, and the drops in `deploy_narrow.py` and
      `deploy_k8s_owed.py`. `_deploy_plane` drops before its `if not tags` check and logs one
      line per dropped tag naming its owner. No module is added, so the copy loop in
      `ansible/roles/setup/gitops_deploy/tasks/code.yml` is unchanged.
    - Tests in `ansible/roles/setup/gitops_deploy/tests/`:
        - a flux-owned role's image-only bump lands in `cs.flux` and never in
          `cs.k8s_deploy`, and an Ansible-owned one still promotes;
        - a narrow output that holds only a flux-owned tag returns `NARROWED_TO_NOTHING` and
          applies nothing, and one that also holds an Ansible-owned tag applies that tag
          alone;
        - the three-reader census test.
    - The deployer applies this setup-plane change itself.
6. **D2.3 — `land.sh` waits on promotion** (about 250 lines).
    - Add `promote.py land-wait`, which waits on M's changed set from the two
      `digests.json` files (C6), the split before `deploy_by_host`, the handback refusal in
      `land_lib/merge.py`, the two `Cause` values, and the annotation that `land-wait` writes
      on `settled`.
    - Tests in `scripts/deploy_tools/tests/`:
        - the three outcomes, including an evicted run whose descendant settles, and a hold
          set before the merge that ends the wait at the first poll;
        - the changed set: a fixture pair whose `footprint` differs for one tag waits on that
          tag and `homelab` alone, while an unchanged tag that is not Ready does not block
          `settled`. A fixture ancestor with no `digests.json` waits on every name;
        - the revision read: an origin revision of `master@sha1:<R>` with R descending from
          M settles, while an absent origin revision, or a `lastAppliedRevision` of
          `prod@sha256:<digest>` with no origin revision, keeps waiting;
        - the split: a PR whose only tag is flux-owned never reaches `deploy.sh`, and one
          that also holds an Ansible-owned tag deploys that tag alone;
        - the handback: a PR that removes `owner` while a fake `kubectl` reports the
          Kustomization present is refused before the merge and never reaches `deploy.sh`,
          and the same PR with `NotFound` merges;
        - the annotation: `settled` writes one `deploy-annotation` line with
          `source=promote` and the PR's published tags, and `deploy-failed` and `gave-up`
          write none.
    - `ansible/tests/deploy/test_deploy_annotations.py` gains `promote.py` as a third
      emitter, in the test that holds the query to what the emitters log and in the test
      that every deploy path annotates.
    - Edit `docs/landing.md` and `.claude/skills/land-after-merge/SKILL.md`.
      `docs/flux-deploy-plane.md` states the handback order (*Rollback*), which later
      handbacks and retirements reuse.
7. **D2.4a — The two drift checks** (about 90 lines).
    - Change `manifest-prune-check.sh.j2` and `live_drift_check.py:is_server_side_applied`
      as the table says.
    - Tests in `ansible/roles/setup/k3s/tests/`, each a pair:
        - the prune check skips a labelled object whose Kustomization exists, and checks a
          labelled object whose Kustomization is gone, as after a handback;
        - the prune check still flags an unlabelled orphan;
        - the drift check keeps a kustomize-controller object with no
          `last-applied-configuration` out of scope, and compares the same object once it
          carries one, as a handed-back object does.
    - **This PR owes a hand apply.** The `setup/k3s` change lands in the deployer's
      `manual_plane` ledger. The executing session applies
      `uv run ansible-playbook ansible/k3s-bringup.yml --tags manifest-prune,live-drift`,
      then clears the ledger line, as the `land-after-merge` skill describes.
8. **D2.4b — The script readers and the two generators** (about 190 lines).
    - Change `render_targets.py`, `shared_role_callers.py:smoke_caller`,
      `secret_rotation.py`, `health.py`, `releases.py`, `gen_role_glance.py` and
      `service_catalog.py` as the table says. `render_targets()` keeps its signature and
      result, and only `main` calls the new `render_record_targets`, so `render_records.py`
      changes nothing. `smoke_caller` drops a published tag for the reason the table gives.
    - Tests, each a pair:
        - in `scripts/deploy_tools/tests/`, a fixture flux-owned tag is absent from
          `render_targets.py`'s CLI output and present in `render_targets()`;
        - in `scripts/deploy_tools/tests/`, with bento-pdf flux-owned in a fixture,
          `smoke_caller` picks the next cheapest caller;
        - in `scripts/renderer/tests/`, the fixture flux-owned tag still renders with
          `cluster --role`;
        - in `scripts/diagnostics/tests/`, a workload whose tag is flux-owned in the tree
          passes with a stale release record, and an Ansible-owned one with a Flux label
          still fails NOT ROLLED;
        - in `scripts/docs/tests/`, a flux-owned fixture role's glance and catalogue row
          print the Flux owner and exit 5, and an Ansible-owned one prints its deploy tag.
9. **D2.5 — Verification, `last-good` and the hold** (about 300 lines).
    - Add `promote.py verify T`, which pulls `sha-T` and expects the statuses of T's changed
      set. One function computes the changed set. `verify`, `land-wait` and D4.3's timeout
      derivation all call it, and none of them derives the set a second time.
    - Add the `last-good` seeding and the hold-then-reset order with the
      `<T> failed=<names>` value. Add the `PROD_DIGEST` write on every `prod` move, the
      revert rule behind its disabled flag, and `decide`'s pull of `prod`'s artifact. Add
      the `workflow_dispatch` input `sha` behind D1's ancestry check, and the
      `probe.py landing` lines for both holds.
    - Add a read-only `verify` job with `statuses: read`. Add a `settle` job that checks out
      no repository code, holds `packages: write`, and sets `PROMOTE_TOKEN` as `GH_TOKEN`.
    - The D1.4 workflow test's expected permission map gains both jobs. It has a red fixture
      in which `settle` checks out code, and one in which `verify` holds `packages: write`.
    - **Preconditions, quoted in the PR body:** the three reads under *What `probe.py
      landing` reads*, each timed three times against the live system, with the timeouts
      set from them. The C2 attribution in Open question 6 is amended.
    - Tests in `scripts/deploy_tools/tests/`, each a pair:
        - all statuses `success` moves `last-good` to `sha-T`;
        - one `failure` writes the hold, `<T> failed=<name>`, before the reset, asserted by
          call order, and a timeout names each expected name that had no status;
        - the changed set: a fixture T whose `footprint` differs from `prod`'s for one tag
          expects that tag, its fixture `-deferred` dependent and `flux/homelab`, and an
          unchanged tag whose status reads `failure` neither holds nor blocks
          `last-good`. Its red half expects every name and holds on that unrelated
          failure. A suspended fixture Kustomization is not expected, an `_unclassified`
          change expects every name, an empty set expects `flux/homelab` alone, and a
          dependent already `failure` on `prod`'s commit is reported and not expected;
        - the revert rule: with the flag off, a `failed=` hold holds a candidate whose
          footprints match `last-good`'s. With the flag on, that candidate promotes, its
          success sets `DEPLOY_HOLD` to `none`, and a candidate whose footprint for a failed
          name differs stays held. A hold in another form stays held with the flag on;
        - each `prod` move, the reset included, writes `PROD_DIGEST`, and a failed write
          exits non-zero after the move;
        - `probe.py landing` prints a `hold_sha` line and a `DEPLOY_HOLD` line when both are
          set, when one is set and when both read `none`. Its red half drops either line;
        - an empty status list keeps waiting and never reads as success;
        - a timeout holds;
        - `decide` returns a T newer than the triggering SHA X. `verify` waits on T's
          statuses with the expected set read from a fixture `sha-T` bundle, and a fixture
          that posts statuses on X alone times out rather than passing;
        - a fixture bundle holding a shared kind adds `flux/<name>`, and its kustomize
          `kustomization.yaml` adds no status;
        - a failed pull of `sha-T` exits non-zero with `prod` unmoved, and a good pull
          proceeds to the tag;
        - a hold write that fails still calls the reset and exits non-zero naming both, and
          a reset that fails after a written hold exits non-zero with the hold in place;
        - `probe.py landing` prints `last-good: unknown` when the registry read fails, and
          then never reports `prod` equal to `last-good`.
    - The D1.4 workflow test in `ansible/tests/repo/` gains the `sha` input's order: D1's
      ancestry step reads the dispatched commit and runs before the render step. Its red
      fixtures are a render step placed before the ancestry step, and an ancestry step that
      reads `github.sha` instead of the input.
10. **D2.6b — The bundle writer, `digests.json`, its census and the reserved names** (about
    230 lines). This is D2's first publishing PR (C1).
    - Add the `DIR/flux/<name>.yaml` and `DIR/flux/kustomization.yaml` writers in
      `scripts/renderer/`, the `postBuild` rule that names `homelab-cluster-vars`, an empty
      `SHARED_KINDS`, and the renderer's reserved-name check for `homelab` and the set's
      members. Add the publish job's render step over the published tags, with its
      empty-list rule, and `promote.py check-bundle` with the census.
    - Add `DIR/digests.json`, with `own` and `footprint` digests per tag and per shared
      kind, the `_unclassified` key, and the footprint-rule table that every `SHARED_KINDS`
      member needs (C1, C3). The renderer computes the digests in the render's process.
    - With no published tag the bundle is the root alone, exactly D1's, so `flux/homelab`
      stays `success` on the next commit.
    - **Precondition:** the C1 and C3 amendments of Open question 6 are applied.
    - Tests in `scripts/renderer/tests/`, each a pair where it can fail:
        - the YAML above for a fixture tag, with `postBuild` for a fixture that holds
          `${email}` and without it for one that holds no placeholder;
        - the root's `resources` list every file in `flux/`;
        - a fixture tag named `homelab`, and one named after a fixture `SHARED_KINDS`
          member, fail the render, and an ordinary tag renders;
        - `digests.json`: two renders of one tree write identical files. An edit under a
          fixture tag's `cluster/<tag>/` moves its `own` and its `footprint`, an edit to its
          `flux/<tag>.yaml` moves only its `footprint`, and an edit to another tag moves
          neither. A fixture path that counts for no name lands under `_unclassified`. A
          fixture shared kind's file moves the footprint of each tag its rule lists and of
          no other tag;
        - the footprint-rule census: every `SHARED_KINDS` member has a footprint rule. The
          set is empty at D2, so the census runs over a fixture member, and its red half is
          that member without a rule.
    - Tests in `scripts/deploy_tools/tests/`, each a pair: the census fails when a
      published tag renders no file, when an extra file appears, and when the root's list
      omits a file. A fixture `SHARED_KINDS` member with its own file passes. A fixture
      `released` tag is absent from the bundle, and the census passes.
11. **D2.6c — The publish gates and the object-loss check** (about 220 lines).
    - Add the source, host and no-stub gates to `check-bundle`, and the gitleaks step.
    - Add `decide`'s object-loss check with its `Removes-Object` trailer and its handback
      exemption, and the promote job's history fetch back to `prod`'s commit.
    - Tests in `scripts/deploy_tools/tests/`, each a pair, for the object-loss check. Its
      named member is bento-pdf's `.local.` IngressRoute, because the check exists for kinds
      that no workload rollout covers:
        - a fixture `prod` bundle that holds that IngressRoute and a candidate bundle that
          lacks it, with no trailer in the range, makes the candidate ineligible, and the
          log line names `IngressRoute/homelab/<name>`. The same pair with a
          `Removes-Object` trailer on one commit in the range promotes;
        - a trailer on a commit outside (`prod`, T] does not count;
        - a cluster-scoped fixture `ClusterRole` lost without a trailer is refused, and its
          `ClusterRole//<name>` trailer admits it;
        - a candidate whose bundle drops the whole `bento-pdf` Kustomization, as a handback
          does, is eligible;
        - when every candidate is ineligible, `decide` prints `current` and moves nothing.
    - Tests in `scripts/deploy_tools/tests/`, each a pair, with a fixture tree that copies
      bento-pdf's route template as the clean half and as each gate's named member:
        - source, no `postBuild`: a shell `${HOME}` passes, and `${email}` fails;
        - source, with `postBuild`: a fixture bundle with a `cluster-vars` kind whose
          `secretGenerator` makes `homelab-cluster-vars` passes, and the same tag without
          that kind fails;
        - host: a fixture route that ends in the fixture inventory's `domain` passes. A
          stub-mode render, whose Host rule ends in `stub-domain`, fails. So do a fixture
          route with another literal host and one that ends in `${domain}`;
        - no-stub: a fixture ConfigMap value `stub-domain` fails.
    - The D1.4 workflow test in `ansible/tests/repo/` gains two assertions: the render step
      passes no `--secret-stubs`, and `check-bundle` and gitleaks run before the push step.
      Each has a red fixture.
    - Edit `docs/flux-deploy-plane.md` for the `Removes-Object` trailer and its grammar.
12. **D2.6d — The deploy-pipeline exporter and its rules** (about 260 lines). It needs D2.5,
    which writes `PROD_DIGEST`, and M1, whose rules directory and routing it joins. It lands
    before D2.7, so the first flip pages.
    - Add the monitor-bridge family with its four gauges and scrape endpoint, the scrape job
      in `prometheus.yaml.j2`, the `deploy-pipeline` rule group, its promtool test file and
      the Alertmanager route that repeats `DeployHoldActive` daily. Add the read-only token
      through `/add-secret`, then run `secret_rotation.py sync`.
    - **Preconditions, quoted in the PR body:** the operator has minted the token
      (**operator-only**); one compare call made with it succeeds; Open question 12's label
      is settled; and the PR states its series delta against M1's budget.
    - Tests in monitor-bridge's `tests/`, each a pair: a fixture `DEPLOY_HOLD` of `none`
      exports 0 and a `failed=` value exports 1; a `PROD_DIGEST` equal to the fixture
      manifest's digest exports 1 and a different one exports 0; a green run that `prod`
      contains is not unpromoted, and one that `prod` does not contain sets the age. A
      missing variable exports no series rather than a 0.
    - promtool cases, one firing case per alert with its `service` and `severity` labels
      asserted, as M1's rule 2 requires:
        - `DeployHoldActive` fires after 15 minutes at 1, and stays silent at 0;
        - `PromotionStalled` fires at 3 hours with the hold gauge at 0, and stays silent at
          3 hours with the hold gauge at 1;
        - `ProdMovedOutsidePromote` fires after 15 minutes at 0, and stays silent after 10;
        - `DeployPipelineExporterAbsent` fires with the input series absent.
    - The 24-hour repeat joins M1's routing test, with a red fixture whose route omits it.
    - Edit monitor-bridge's role `CLAUDE.md` for the family and `docs/flux-deploy-plane.md`
      for the three alerts and what each one means.
    - Land with `land.sh`. monitor-bridge and observability are Ansible-owned and
      denylisted, so `land.sh` deploys both.
13. **D2.7 — The flip** (about 20 lines, plus test fixtures).
    - Add `owner: flux` to bento-pdf's entry.
    - **Preconditions**, each checked and quoted in the PR body:
        - D2.1a to D2.6d, D4.0 and M1 have landed, and the four deploy-pipeline gauges
          read in Prometheus;
        - domain PR 2b has landed, so `domain` is a published key and bento-pdf's render
          holds the literal. The body quotes `grep -c '^domain:'` on
          `ansible/inventory/group_vars/all.yml` printing 1, and the host gate passing over
          the real tree. D2.7 needs no part of D3;
        - D2.4a's hand apply is done, and no `k8s_unapplied` line from D2.1d's landing
          remains;
        - `probe.py landing` shows no tick in flight and `DEPLOY_HOLD` reading `none`;
        - no Renovate PR for `bento_pdf_k8s_image` is open (`gh pr list --search bento`);
        - the operator has accepted the drill, which Open question 1 records;
        - the hash of the `.local.` IngressRoute's route rules, taken for Exit 1.
    - **Two-writer check (C4).** The PR body quotes, from a worktree at the PR's head:
        - `./scripts/deploy.sh --tags bento-pdf` exiting 5;
        - `--list-services` marking bento-pdf;
        - the three-reader census passing;
        - `render_targets.py daniel-box` omitting bento-pdf;
        - `uv run python scripts/deploy_tools/deploy_tags.py narrow origin/master HEAD`
          naming bento-pdf alone, with no reader tag of `containers_list`;
        - the publish job's render and `promote.py check-bundle` passing on that tree.

      After the merge, the deployer's journal for that SHA must log bento-pdf dropped as
      flux-owned, then `NARROWED_TO_NOTHING`.
    - Land with `land.sh`. `owner` reaches no template reader, so the deployer narrows the
      inventory edit to bento-pdf's own tag (`narrow_lib/containers.py:entry_change_tags`),
      and D2.2 drops that tag because Flux owns it. `land.sh` runs no `deploy.sh`, and
      `promote` publishes and verifies the first bundle that holds bento-pdf.
    - **From D2.7 on, bento-pdf's Renovate PRs land without `land-wait`.** In
      `renovate.json`, the rule for `ansible/roles/k8s/**` digest and `pinDigest` updates
      sets `automerge: true` and `platformAutomerge: false`, so Renovate merges such a PR
      itself once CI is green.
      `ansible/roles/k8s/bento-pdf/defaults/main.yml:bento_pdf_k8s_image` is
      digest-pinned, so its bumps take that rule. Such a merge goes through no `land.sh`.
      It therefore draws no `land-wait`, no `deploy-annotation` line and no `probe.py
      health` run. `promote`'s `verify` still gates it on Flux's health wait and the commit
      statuses, and a failure holds and resets `prod`. Before D2.7, the deployer's
      image-only arm applied these bumps through the play's health gate and annotated
      them. *Risks* names the gap, and D6 owns the emitter that closes it.
14. **D2.8 — The drill** (one line). This is C8's production drill. The operator accepted it
    on 2026-10-14 (Open question 1), and the session merges D2.8 itself.
    - Point bento-pdf's `readinessProbe` path at `/flux-drill-not-found`. CI is green,
      because no test checks a probe path. Land with `land.sh`. The expected verdict is
      `deploy-failed` with cause `promote-held`. The hold reads `<T> failed=bento-pdf`,
      where T is the commit `decide` promoted, which is D unless another commit merged
      first. `DeployHoldActive` fires 15 minutes later.
    - **The PR body states the blast radius**, and the session posts it in the deploy
      channel before merging:
        - *Objects touched:* Deployment `bento-pdf` in `homelab` only. Flux applies its new
          pod template, and the new ReplicaSet never becomes ready. The Service and both
          IngressRoutes are unchanged.
        - *What keeps serving:* the old ReplicaSet's pod, under `RollingUpdate` with a
          600-second `progressDeadlineSeconds`.
        - *Rollback:* `promote`'s hold-then-reset returns `prod` to `last-good` on its own,
          and the D2.9 revert is prepared before the merge.
        - *Why this substitute suffices:* the claim under test is the production chain
          itself. Flux's health wait, notification-controller's status on this repo,
          `verify`, `DEPLOY_HOLD`, the GHCR tags and the reset act together only here. A
          kind cluster in CI would exercise the health wait and none of the rest.
15. **D2.9 — Revert the drill.** Revert D2.8 and land it. The hold from D2.8 is still set
    when the revert merges, so `land.sh` ends at its first poll with `deploy-failed` and
    cause `promote-held`. The revert rule's flag is off, so `decide` holds the revert too.
    The operator then clears `DEPLOY_HOLD` and dispatches `promote` (**operator-only**).
    `decide` promotes the newest eligible commit, which contains the revert. The session
    re-runs `uv run python scripts/deploy_tools/promote.py land-wait <R>` for the revert's
    merge commit R, and it settles.

## Verification

```bash
# Exit 1: Flux labels every bento-pdf object, and nothing else applies it.
kubectl -n homelab get deploy,svc -l kustomize.toolkit.fluxcd.io/name=bento-pdf
kubectl -n homelab get ingressroutes.traefik.io -l kustomize.toolkit.fluxcd.io/name=bento-pdf
kubectl -n homelab get deploy bento-pdf -o jsonpath='{.metadata.managedFields[*].manager}'
kubectl -n homelab get deploy bento-pdf -o jsonpath='{.metadata.annotations}' \
  | grep -c last-applied-configuration          # prints 0
# Before D2.7 merges and again after it settles; equal hashes mean the renderer wrote the
# same domain that Ansible rendered.
kubectl -n homelab get ingressroutes.traefik.io bento-pdf \
  -o jsonpath='{.spec.routes[*].match}' | sha256sum
# The field land-wait reads.
kubectl -n flux-system get kustomizations.kustomize.toolkit.fluxcd.io bento-pdf \
  -o jsonpath='{.status.lastAppliedOriginRevision}'   # master@sha1:<40-hex>

# Exit 2: each entry point refuses or skips.
./scripts/deploy.sh --tags bento-pdf; echo "exit=$?"   # exit=5
./scripts/deploy.sh --list-services | grep bento-pdf
journalctl -u gitops-deploy --since today | grep bento-pdf
uv run python scripts/deploy_tools/render_targets.py daniel-box | grep -c bento-pdf   # 0
# After a bento-pdf landing settles: land-wait's annotation.
journalctl -t deploy-annotation --since today | grep 'source=promote'

# Exit 3 (after D2.8 merges as commit D): the hold and the reset. T is the SHA that
# DEPLOY_HOLD's value opens with.
uv run python scripts/diagnostics/probe.py landing  # hold_sha line; DEPLOY_HOLD=<T> failed=bento-pdf;
                                                    # prod == last-good
git merge-base --is-ancestor <D> <T>; echo "contains=$?"           # contains=0
gh api repos/DanielH2018/server/commits/<T>/statuses \
  --jq '.[] | select(.context == "flux/bento-pdf") | .state'      # failure
uv run python scripts/diagnostics/probe.py health bento-pdf        # the old ReplicaSet is ready
# On daniel-box, 15 minutes after the hold: one firing DeployHoldActive, and no
# ProdMovedOutsidePromote at any point through the reset.
curl -s 127.0.0.1:9090/api/v1/query --data-urlencode \
  'query=ALERTS{alertname=~"DeployHoldActive|ProdMovedOutsidePromote",alertstate="firing"}'

# Exit 4 (after D2.9 and the operator's dispatch). R is the revert's merge commit, and L is
# the commit that probe.py landing prints for last-good.
uv run python scripts/diagnostics/probe.py landing                 # DEPLOY_HOLD=none, last-good=<L>
git merge-base --is-ancestor <R> <L>; echo "contains=$?"           # contains=0
gh api repos/DanielH2018/server/commits/<L>/statuses \
  --jq '.[] | select(.context == "flux/bento-pdf") | .state'      # success, newest first
kubectl -n flux-system get kustomizations.kustomize.toolkit.fluxcd.io bento-pdf \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status} {.status.lastAppliedOriginRevision}'
# True master@sha1:<L>, or a descendant of L if a newer promotion has applied since
```

Through the drill, the `homelab-ui` MCP server loads `bento-pdf.local.<domain>` through Traefik
once before D2.8 merges, once while `flux/bento-pdf` is `failure`, and once after the reset.
The page must render each time. A Ready pod alone does not prove that the UI renders.

**Measurements, quoted in the PR bodies of D2.7 and D2.9.** D2 records three facts that
later slices size against:

- **Merge-to-Ready time for the leaf.** It is the time from the PR's `mergedAt` to the
  `created_at` of the `flux/bento-pdf` status of `success` on the first promoted commit
  that contains the merge. The measurement checks C7's 2-hour `PromotionStalled` threshold
  and the `land-wait` timeout against a real figure.
- **The commit-status burst per promotion.** The session counts the statuses on T with
  `gh api --paginate repos/DanielH2018/server/commits/<T>/statuses`, and the span between
  the first and the last `created_at`. It divides the count by the number of Kustomizations
  in the bundle and projects that rate to D5's Kustomization count. The PR body compares
  the projection with GitHub's secondary limits of 80 content-creating requests a minute
  and 500 an hour. A projection above either limit goes to Open question 13.
- **What the status descriptions contain.** Commit statuses on this public repository are
  public, so a substituted value in a description would publish it. The PR body records
  the description shapes on T and on the drill's failed T, such as a health-check message
  or a reconcile error. The session also checks that no description holds a substituted
  value. D2 substitutes none, because `domain` is published, so the check guards the flips
  that follow. It compares the descriptions with the host in the live IngressRoute's match
  rule and prints only the count of matches, which must read 0. D1 asks D2 for this
  check. A non-zero count stops D2 before D2.8.

**Each new check can go red.**

- Every skip in D2.1a to D2.4b is tested as an `_is_clean` and `_is_flagged` pair against a
  fixture entry. The C4 census also names each of the six entry points as a member it must
  find, so a renamed entry point fails the census instead of passing vacuously (failure
  class 4).
- The narrowing pair in D2.1a fails if an `owner`-only edit reaches any of its five fixture
  whole-entry readers, and its other half fails if a `hostname` edit stops reaching them.
- D2.1c's render census fails when a role whose render an added entry moves maps to a tag
  that `_list_reader_tags` does not return for that entry. Its red half, an own-tag-only
  `changed`, fails on artifacts. Its named members fail the census if the render stops
  reaching artifacts, uptime-kuma's monitors, or pihole's ConfigMap through `hostvars` and
  its include.
- D2.1d's equality test fails on a filter that drops an entry without `owner`, which is
  the case where the play would stop visiting a role that the clear assumes it still
  visits.
- D2.3's annotation pair fails if `deploy-failed` or `gave-up` writes a line, and the query
  test fails if `promote.py`'s line stops matching `event=deploy`.
- Each drop site has an empty-list pair: the deployer's `NARROWED_TO_NOTHING` (D2.2),
  `land.sh`'s split (D2.3) and `--changed` (D2.1b). Each fails if the dropped list reaches
  `deploy.sh` or the playbook empty.
- The handback refusal in D2.3 fails if a PR that removes `owner` merges while its
  Kustomization exists.
- `promote.py verify` has its red halves in D2.5, including the empty list that must not read
  as success (failure class 3), a T that is not the triggering SHA, and a failed hold write.
- The changed-set pair in D2.5 fails if `verify` expects every name and holds on a failure
  in an unchanged tag, and its other half fails if a changed tag's `-deferred` dependent
  drops out of the expected set.
- The revert rule's flag-off pair in D2.5 fails if a `failed=` hold lets any candidate
  through while the flag is off.
- `probe.py landing`'s pair in D2.5 fails if either hold line goes missing.
- The `sha` input's workflow test in D2.5 fails on a render step placed before D1's
  ancestry step, and on an ancestry step that reads `github.sha`.
- `land-wait`'s revision read has its red half in D2.3: an absent origin revision, or a
  digest-form revision alone, never settles. Its changed-set pair fails if an unchanged tag
  that is not Ready blocks `settled`.
- The `digests.json` tests in D2.6b fail if a `flux/<tag>.yaml` edit leaves the tag's
  `footprint` unmoved, or if an edit to one tag moves another's. The footprint-rule census
  fails a fixture `SHARED_KINDS` member with no rule.
- The object-loss check in D2.6c fails a candidate that lost bento-pdf's `.local.`
  IngressRoute with no `Removes-Object` trailer, its named member. Its other halves fail if
  a trailer outside the range counts, or if a handback's dropped Kustomization is refused.
- Each deploy-pipeline alert in D2.6d has a promtool case that makes it fire, and a case
  that keeps it silent. `DeployPipelineExporterAbsent` is the family's zero-series case.
  The routing test fails on a route without the 24-hour repeat.
- The drill is the live red proof of the whole chain. It is the only check here that
  exercises Flux's health wait, the commit status, `verify`, the hold and the reset together.
- The publish census fails on a fixture where a published tag renders no file, on an extra
  file, and on a root list that omits a file. The renderer's reserved-name check fails a
  fixture tag named `homelab`.
- The host gate fails a stub-mode render, a host with another literal and a `${domain}`
  before any push, so a Host rule without the published `domain` never reaches `prod`. The
  source gate's two halves and the no-stub gate each fail their own fixture. Each gate names
  a fixture copy of bento-pdf's route as the member it must find.

## Rollback

- **D2.1a to D2.6d** change how no service is deployed, and each reverts on its own before
  D2.7. After D2.7, undo D2.7 first, through the handback below. A plain revert of D2.7 is
  the one-commit move that the handback exists to avoid. A revert of D2.6d after D2.7
  leaves the pipeline paging only through notification-controller's Discord alerts.
- **The revert rule's flag** turns off in a one-line PR, after which every hold clears only
  by hand again.
- **D2.1d** reverts like the others for the play's behaviour, but its revert edits
  `ansible/deploy.yml` and `toposort.py` again. The tick refuses to narrow it and records a
  `k8s_unapplied` line for every declared k8s service, as at D2.1d's landing. The revert
  takes the same procedure: the session quotes the refusal before the PR, and the
  operator's 2026-10-13 ruling for the clear covers it (Open question 9).
  After D2.7, a revert of D2.1d alone would put bento-pdf back in the play while Flux still
  applies it, so it waits until the handback has removed `owner`.
- **A partial failure at D2.1d's landing.** Nothing is applied, so nothing on the cluster
  changes. The fleet's `k8s_unapplied` lines stay until they are discharged or cleared, and
  they write no `hold_sha`, so other landings continue. If the full-deploy alternative
  fails partway, the services it already applied stay applied. `deploy.sh`'s last two lines
  say what its exit means, and `docs/deploying.md`'s *Exit codes are resume points* says
  where the session resumes. The lines of the services it did not reach stay in the ledger.
- **D2.1c** reverts on its own at any time, before or after D2.7. An added entry then
  reaches every reader again, which applies more and skips nothing.
- **D2.3's annotation** reverts with no effect on a landing. Only the board's markers for
  Flux-applied changes go.
- **D2.7, the handback.** Two PRs, in this order, which `docs/flux-deploy-plane.md` states
  for every later handback and retirement:
    1. **PR H1 sets `owner: released`.** Every Ansible path still skips bento-pdf. The next
       bundle omits `flux/bento-pdf.yaml`, the root prunes the `bento-pdf` Kustomization,
       and `deletionPolicy: Orphan` leaves the objects serving. That bundle reaches the
       cluster only when `promote` moves `prod`, which needs `DEPLOY_HOLD` to read `none`.
       A hold is the likeliest reason to hand back. In that case the operator clears it
       once H1 is on master (**operator-only**), and the promotion that follows publishes
       a bundle without bento-pdf, so the held change no longer reaches it.
    1. **Wait for `NotFound`.**
       `kubectl get kustomizations.kustomize.toolkit.fluxcd.io -n flux-system bento-pdf`
       must return `NotFound`.
    1. **PR H2 removes `owner`.** Its body quotes the `NotFound`. `land.sh` refuses to merge
       it while the Kustomization exists (D2.3). Once it merges, the deployer narrows the
       edit to bento-pdf's tag and applies it, and `land.sh` deploys it client-side. That
       apply writes `last-applied-configuration` back.
    - Between H1 and H2, bento-pdf has no writer and keeps serving its last spec. The two
      writers never overlap, however long a hold lasts.
    - The Flux labels and kustomize-controller's `managedFields` entry stay on the objects.
      No skip reads them alone, so they need no removal. Removing them would need an
      Ansible task, because Ansible is the only write path.
    - **The guard on H2's deployer path is the merge refusal, and nothing in the play.** A
      merge made outside `land.sh` bypasses it, and then the PR body's `NotFound` line is
      the only guard. A Kustomization read in the play would cover that case, but a play
      that fails on the deployer's broad plane writes `hold_sha` and parks every landing
      (the DECIDED marker on `_deploy_plane`). That trades a two-writer window on one
      stateless leaf for a fleet-wide stop, so the play reads nothing.
- **D2.8.** Revert it (D2.9).
- **A partial failure after D2.7.** Suppose the flip merges but `promote` fails, for example
  on a render error or a publish gate. Ansible skips bento-pdf and Flux has not applied it,
  so bento-pdf runs its last applied spec with no writer. It keeps serving, but it is frozen.
  `land.sh` reports the failure, and the next green promotion resumes it. If the
  `cluster-vars` Kustomization fails, for example on a missing `sops-age` Secret, bento-pdf
  is unaffected. It substitutes nothing and has no edge on `cluster-vars`.
- **A partial failure inside the drill.** If the revert cannot land, `DEPLOY_HOLD` stays set,
  and every flux-owned change waits, which at D2 is bento-pdf alone. With `RollingUpdate`
  and a 600-second `progressDeadlineSeconds`, the old ReplicaSet stays available while the
  new one never becomes ready.

## Risks

- **An entry point outside the census** (failure class 4). A path that applies a k8s role and
  is missing from the table writes bento-pdf behind Flux's back. Mitigation: the census names
  its six members. A `Census` row in `ansible/tests/repo/test_census_rows_text.py` selects
  every tracked file that invokes `deploy.sh` and fails on one that is neither in the table
  nor exempt with a reason.
- **A tick in flight at the flip.** A tick that started before D2.7 merged works from the
  older SHA. If its range holds a bento-pdf image bump, it deploys bento-pdf through Ansible
  once more. Mitigation: the D2.7 preconditions require no open bento-pdf Renovate PR and no
  tick in flight.
- **The takeover rolls bento-pdf once.** `RollingUpdate` keeps a pod serving.
- **A dropped file deletes an object** (failure class 2). If a render bug drops bento-pdf's
  Deployment, or a NetworkPolicy, IngressRoute, CronJob, Service or RBAC object, prune
  deletes it, and the Kustomization still reads Ready, because nothing is left to wait on.
  `verify` cannot see it. Mitigation: `decide`'s object-loss check (D2.6c) refuses a
  candidate that lost an object no `Removes-Object` trailer declares, and D5's PR-time
  check stops such a PR before it merges. `probe.py health bento-pdf`, which `land.sh`
  runs, still reports a missing Deployment.
- **The object-loss check stalls promotion until a trailer lands.** A removal merged
  without a trailer makes every later candidate ineligible, and `decide` prints `current`.
  Mitigation: `decide` logs each undeclared object, `PromotionStalled` pages after 2
  hours, and a follow-up commit that carries the trailer releases the walk.
- **A failure outside the changed set does not hold.** An unchanged Kustomization that
  fails, such as a crashloop that predates the change, lets other changes promote.
  Mitigation: the Flux rule group (C7) pages a Kustomization that is not Ready for 15
  minutes, so the failure still reaches a person.
- **The commit-status burst grows with the fleet.** notification-controller posts at least
  one status per Kustomization on each promoted commit. At D5's Kustomization count that
  can approach GitHub's secondary limit of 80 a minute. A rejected status reads as a
  timeout and holds, which is the safe direction but a false hold. Mitigation: D2 measures
  the burst and projects it (*Verification*), and Open question 13 carries the result to
  D5.
- **Two holds exist until D6.** The deployer's `hold_sha` and `DEPLOY_HOLD` answer for
  different roles. Mitigation: `probe.py landing` prints a line for each on every run, even
  when one reads `none`, and `docs/landing.md` names which role each one covers.
  `DeployHoldActive` pages a standing `DEPLOY_HOLD` daily.
- **The drill puts a known-bad commit on master** (failure class 5). Mitigation: the revert is
  prepared before D2.8 merges, and at D2 only bento-pdf waits on the hold.
- **Orphaned objects on retirement.** Under `deletionPolicy: Orphan`, retiring a flux-owned
  service leaves its objects live. Mitigation: until D6, a retirement first empties the
  service's templates, which prunes its objects, and removes the entry in a second PR. The
  first PR carries a `Removes-Object` trailer for each object it empties.
  `docs/flux-deploy-plane.md` states the order.
- **`promote` sees only GitHub.** A status that notification-controller cannot post reads as
  a timeout and holds. That is the safe direction.
- **A publish refusal reads as a timeout to `land.sh`.** When a gate refuses X and no later
  commit passes, no `sha-X` exists, so `land-wait` waits out twice the promote timeout and
  reports `promote-timeout`. Nothing moves, which is the safe direction. Mitigation: the
  `promote-timeout` message names the `promote` workflow's failed `publish` jobs as the first
  place to look.
- **A handback could open a two-writer window** (failure class 1). Removing `owner` in one
  commit hands bento-pdf to Ansible at merge, while Flux keeps reconciling until `promote`
  publishes a bundle without it, which a hold delays without bound. Mitigation: the
  two-PR handback under *Rollback*, `land.sh`'s refusal to merge H2 while the Kustomization
  exists, and the `NotFound` line in H2's body. A hand merge of H2 bypasses the refusal.
- **A D2.7 merged before domain PR 2b publishes nothing.** Until then bento-pdf's render
  holds `${domain}`, so the host gate refuses the bundle. `prod` stays put, Ansible
  already skips bento-pdf, and the leaf runs frozen until domain PR 2b lands. Mitigation:
  the D2.7 preconditions name domain PR 2b, and `land.sh`'s `promote-timeout` message names
  the failed `publish` job.
- **A Flux-born entry still applies Ansible-owned readers unattended** (failure class 4).
  D2.1c cuts an added bare entry's reach from 23 reader tags to four, but three of the four
  remain denylisted: blackbox-exporter, pihole and uptime-kuma each declare
  `k8s_autodeploy: false`. Applying them forward-only also
  applies any change of theirs that is on master and not yet applied. Mitigation: each adding
  PR in H1, H2 and H6 runs D1.2's stop rule over the reader tags its narrow output names, and
  a failed apply takes D1's way out of a hold (D1 *Rollback*). Open question 10 states the
  output those PRs should expect after D2.1c. The
  render census fails if the rule ever drops a template whose render the entry moves.
- **D2.1d's landing owes the whole fleet** (failure class 3). Its two play edits make the
  deployer refuse to narrow, so every declared k8s service gets a `k8s_unapplied` line and
  `land.sh` reports `needs-manual-apply`. A full deploy to discharge them would apply
  traefik, authelia and pihole unattended, with any of their own changes still on master.
  Mitigation: D2.1d holds nothing else, so the refusal is its own. The session quotes the
  refusal before the PR. The equality test proves that the play batches the same entries.
  The default discharge is the operator's clear, and the full deploy runs only under D1.2's
  stop rule. A revert of D2.1d takes the same procedure.
- **Cluster deploys draw annotations only when `land.sh` follows them** (failure class 3).
  Three paths write no line, so the board shows no deploy:
    - Renovate's own automerge. It merges bento-pdf's digest bumps without `land.sh` from
      D2.7 on (D2.7). Once D5 moves them, it merges the digest bumps of every Flux-owned
      role the same way, except the roles whose `defaults/main.yml` the later
      `k8s image {{depName}} (manual ...)` rule in `renovate.json` lists with
      `automerge: false`. Those still land through `land.sh`.
    - A merge made by hand outside `land.sh`.
    - A promotion after the operator clears `DEPLOY_HOLD` that no landing waits on.

  Two landings that settle on the same R also write two lines for one promotion.
  Mitigation inside D2: D2.9's re-run of `land-wait` annotates the promotion that follows a
  cleared hold, and D2.7 states that bento-pdf's Renovate PRs land without `land-wait`. The
  emitter that closes the gap observes cluster state on daniel-box, not a landing. It reads
  each Kustomization's `status.lastAppliedOriginRevision` with the read-only `kubectl` and
  writes one `source=promote` line per revision change, which also removes the duplicate.
  D2 hands that emitter to D6, because D6.1 deletes the deployer that could host it, and it
  should land before D5 moves the bulk. Until it lands, `land-wait`'s line covers only the
  landings that `land.sh` makes.
- **A placeholder render could mislead a reviewer** (failure class 2). A green `check-bundle`
  shows that each placeholder has a declared source, not that the cluster holds its value.
  Mitigation: Exit 1 compares the live route's hash before and after the flip, and the
  homelab-ui load proves that the route matches.

## Tests and docs that change in the same PR

- D2.1a: tests in `scripts/deploy_tools/tests/`, and the census row in
  `ansible/tests/repo/test_census_rows_roles.py`. The `narrow_lib/containers.py` module
  docstring names `DEPLOY_MECHANICS_FIELDS`.
- D2.1d: tests in `ansible/tests/deploy/`. `docs/gitops-pipeline.md`'s *Broad changes* says
  that the play skips an entry that carries `owner`. The PR body quotes the narrow refusal,
  the equality test's output and the operator's ruling on the discharge.
- D2.1c: tests in `scripts/deploy_tools/tests/`, the `EVERY_ENTRY` pair among them, and the
  render census in `ansible/tests/k8s/`. The `changed_fields` docstring states the new case, and
  `docs/gitops-pipeline.md`'s *Broad changes* says that an added Flux-born entry reaches only
  the readers it can move.
- D2.1b: tests in `scripts/deploy_tools/tests/` and `.claude/hooks/tests/`.
  `docs/deploying.md`, `.claude/skills/deploy/SKILL.md`.
- D2.2: tests in `ansible/roles/setup/gitops_deploy/tests/`. `docs/gitops-pipeline.md`
  gains the `flux` bucket beside `cs.k8s`, and the gitops_deploy role `CLAUDE.md`'s
  *Safety* list gains it.
- D2.3: tests in `scripts/deploy_tools/tests/`, and the third emitter in
  `ansible/tests/deploy/test_deploy_annotations.py`. `docs/landing.md`,
  `.claude/skills/land-after-merge/SKILL.md`. Root `CLAUDE.md`'s *After a PR Merges* gains
  one sentence on flux-owned services. Until G2 retires `docs/facts.lock`, and if the new
  sentence cites an atom, run `uv run python scripts/dev/fact_status.py verify 'CLAUDE.md#After
  a PR Merges — Pull, Deploy, Verify'` and commit the lock. `docs/flux-deploy-plane.md`
  gains the handback order.
- D2.4a: tests in `ansible/roles/setup/k3s/tests/`. The k3s role's `CLAUDE.md` names the
  prune check's skip and the drift check's handback rule.
- D2.4b: tests in `scripts/deploy_tools/tests/`, `scripts/renderer/tests/`,
  `scripts/diagnostics/tests/` and `scripts/docs/tests/`. The render_records role's
  `CLAUDE.md` says the list leaves out flux-owned services.
- D2.5: tests in `scripts/deploy_tools/tests/`, and the D1.4 workflow test in
  `ansible/tests/repo/`. `docs/flux-deploy-plane.md` gains verification of T over its
  changed set, the `failed=` hold, its way out, the revert rule and its disabled flag,
  `PROD_DIGEST`, and the sources `probe.py landing` reads. `docs/landing.md` shows both
  hold lines.
- D2.6b, D2.6c: tests in `scripts/renderer/tests/` and `scripts/deploy_tools/tests/`, and
  the D1.4 workflow test in `ansible/tests/repo/`. `docs/flux-deploy-plane.md` gains the
  render mode, the bundle layout, `digests.json` and its footprint rules, `SHARED_KINDS`,
  the reserved names, the census, the three gates, the object-loss check and the
  `Removes-Object` trailer.
- D2.6d: tests in monitor-bridge's `tests/`, the promtool test file beside M1's, and M1's
  routing test. monitor-bridge's role `CLAUDE.md` names the family and its token, and
  `docs/flux-deploy-plane.md` names the three alerts. `ansible/secret_rotation.yml` gains
  the token through `secret_rotation.py sync`.
- D2.7: `ansible/roles/k8s/bento-pdf/CLAUDE.md` says Flux owns it. Its generated
  `## At a glance` block regenerates through the `regen-role-glance` hook, and D2.4b's
  generator change makes that block name Flux and exit 5.

## Complexity delta

- **Added:** an ownership flag with two values, three readers and their census; a named set
  of deploy-mechanics fields; one exit code, registered in three places; two land causes and
  one pre-merge refusal; about 550 lines in `promote.py`, up from about 350, including
  `check-bundle` with its census and three gates, the changed-set `verify`, the object-loss
  check in `decide`, the revert rule behind its disabled flag and the `PROD_DIGEST` write;
  the renderer's Kustomization and root writers, `digests.json` with its footprint rules
  and their census, an empty `SHARED_KINDS` and the reserved names; skips in ten modules
  and two generators; a per-entry narrowing record for a Flux-born entry, with its render
  census; a third `deploy-annotation` emitter; one commit trailer, `Removes-Object`; one
  repo variable, `PROD_DIGEST`; a monitor-bridge family of about 150 lines with a scrape
  endpoint, one scrape job, a rule group of four alerts with its promtool cases, and nine
  series against M1's budget.
- **Removed:** nothing yet. Each skip exists only until D6 deletes the path it guards. The
  withdrawn `homelab-identifiers` Secret leaves nothing to retire. `digests.json`
  replaces D6.1's `--moved-tags` mode before D6 writes it (C1).
- **Net, for an operator:** one more hold to clear, `DEPLOY_HOLD`, beside `hold_sha` until
  D6. A hold names what failed, and it pages daily while it stands. One more token to
  mint and rotate, read-only. Three more alerts to read. One ruling on the revert rule,
  which would let a merged revert clear its own hold. At D2.1d's landing, and again at any
  revert of it, one ruling, then one `clear-owed k8s_unapplied` call per remaining service
  or a full deploy instead.

## Open questions

1. **Resolved: does the operator accept a drill on production?** The stage cluster was
   retired on 2026-09-29 (#2941), so the slice runs on production, and D2.8 deliberately
   merges a failing probe there. D2.8 is C8's production drill, and its PR states the blast
   radius. The operator ruled on 2026-10-14 to accept the production drill, and the session
   may merge D2.8 itself. The alternative, a revived stage guest, would be a slice of its
   own with its own inventory and secrets (#2944). It is not taken.
2. **Resolved: does C6 adopt the waiting rule above?** C6 *`land.sh`* now states it: wait
   until M is covered, never on one run. C6 *Which revision field names a commit* names
   `status.lastAppliedOriginRevision`.
3. **`deletionPolicy`.** Orphan until D6, then the default `MirrorPrune` with the publish
   census as the guard? Resolution: D6 decides, using its own count of handbacks and
   retirements.
4. **Should the deploy UI clear `DEPLOY_HOLD`?** That needs `PROMOTE_TOKEN` on daniel-box.
   Resolution: the operator decides (**operator-only**). Until then the UI says that
   `DEPLOY_HOLD` is cleared from the CLI.
5. **Resolved by the operator's ruling of 2026-10-14: where does `domain` come from?** The
   operator ruled that `domain` is published: "domain is not secret, it's a var for
   flexibility just in case of future changes." The renderer writes it as a literal, and it
   stays an Ansible variable in `ansible/inventory/group_vars/all.yml`. D2.7 waits on domain
   PR 2b of R2's *Publish `domain`* item and on no PR of D3. D2.6c's host gate has one rule,
   the literal. The former default, `domain` from `homelab-cluster-vars`, is rejected.
6. **Contract amendments still needed for the bundle.** C2's promotion target and C1's
   shared-kind output are applied. The C3 *Reserved names* and C1 *Cluster output*
   amendments below were applied on 2026-10-14. Proposed wording for the rest, owned by the
   contracts.md maintainer:
    - **Applied on 2026-10-14.** C2 *Verification* credits D2.5 for the name-based expected
      statuses. C3 *Shared kinds* and *Publish census* credit D2.6b for the empty
      `SHARED_KINDS` and the allowlist census. D4 Open question 5 asked for the same change.
      The slice index carries neither "D4.3a needs only D2.5 and D2.6" nor "D3.3 also
      D4.3a". The gates before D2.5 and D2.6b merge are clear.
    - **Applied on 2026-10-14.** C3 *Reserved names*: "The render fails on a deploy tag
      named `homelab`, the root's name," beside the `SHARED_KINDS` members and the
      `-deferred` suffix. D2.6b adds the check in the renderer, and D4.2 adds the suffix to
      it. The gate before D2.6b merges is clear.
    - **Applied on 2026-10-14.** C1 *Cluster output*: "`DIR/flux/kustomization.yaml` lists
      every file in `DIR/flux/`." The gate before D2.6b merges is clear.
7. **Resolved: C4 gains a second `owner` value for the handback.** C4 *Values of `owner`*
   states it, applied on 2026-10-14: "`owner` takes `flux` or `released`. `released` leaves
   the tag out of the bundle while every Ansible path still skips it. A handback is two PRs:
   `released`, then the removal of `owner` once the tag's Kustomization reads `NotFound`."
   D2.1a, D2.1b, D2.1d and D2.2 already implement `released` in code, so the contract now
   matches the code. The gate before D2.7 merges is clear.
8. **Resolved: `DEPLOY_HOLD`'s no-hold value.** D2 reads and writes `none`, as D1 does,
   because `gh` cannot set an empty value. C2 *`DEPLOY_HOLD`'s values* states `none`.
9. **Resolved: how D2.1d's `k8s_unapplied` lines are discharged.** The operator ruled on
   2026-10-13 for the default, the clear: one `gitops_state.py clear-owed k8s_unapplied
   <svc>` per line left after the self-discharge, on the grounds of the equality test and
   the diff. The operator runs each clear themselves: "I can also run the "gitops_state.py
   clear-owed k8s_unapplied <svc>" if you give me the command". The alternative, a full
   deploy under D1.2's stop rule, is not taken. D2.1d's PR body quotes the ruling. The
   same ruling covers a revert of D2.1d.
10. **What narrow output should H1, H2 and H6 expect for a Flux-born entry?** Their steps
    still branch on an own-tag-only outcome and expect 23 reader tags. D2.1c changes both
    for an added entry that carries `owner`:
    - **A bare entry** (`name`, `platform`, `owner`, and `depends_on` until D4.1 makes it a
      deploy-mechanics field). `deploy_tags.py narrow` names the entry's own tag plus the
      reader tags. On the tree of 2026-10-14 those are artifacts, blackbox-exporter, pihole
      and uptime-kuma, as *The ownership flag and its three readers* derives. The tick
      applies those four, because D2.2 drops the own tag.
    - **An entry with `hostname`**, or with `port`, `tier` or `metrics`. The output adds
      every `FILTER_FIELDS` reader of each field the entry carries. For `hostname` on the
      same tree that is authelia's access rules and observability's Prometheus config. At
      `a5abf8581`, `port` adds netpol-baseline and observability, and `tier`
      adds observability. The PR measures that set; D2 does not fix a count for it.
    - **A field edit to an entry that exists**, such as a `netpol_from` label, names the
      entry's own tag and the readers of that field. At `a5abf8581` a
      `netpol_from` edit reaches netpol-baseline alone.
    - **An added Ansible-owned entry** still makes `changed_fields` return None, so it
      reaches the full reader set, 23 tags at `70591c307`. D1.2's 24-tag expectation is
      for the `flux` entry, which carries no `owner`.

    D2.1c's render census in `ansible/tests/k8s/` is the proof of the expected shape, and
    the H slices cite it by what it asserts. Resolution: H1, H2 and H6 delete their
    own-tag-only branch and the 23-tag expectation for Flux-born entries, and run D1.2's
    stop rule over the tags their narrow output names. Gate: before H1's first PR that adds
    a `containers_list` entry, and before H2's and H6's.
11. **Should the revert rule turn on?** C2 proposes it, and it replaces D5 Open question 17.
    D2.5 ships it behind a module constant in `promote.py` that disables it. With it on, a
    merged revert whose footprints for the failed names equal `last-good`'s promotes past a
    `failed=` hold and clears it, with no operator step. The cost is an automatic clear
    that a human did not read. Recommendation: keep it off through D2.9, then turn it on,
    because the drill shows that a `failed=` hold names the right Kustomization. Resolution:
    the operator rules (**operator-only**), and a one-line PR flips the constant. Gate:
    none for D2, because every D2 step assumes the flag is off.
12. **Which `service` label do the deploy-pipeline alerts carry?** C7 requires a deploy tag
    or a host name. `service="flux"` names the pipeline's role, but `probe.py health flux`
    reads every firing alert with that label. A standing hold would then fail the flux
    role's own health gate and couple `DEPLOY_HOLD` into the deployer's `hold_sha`. A host
    name names a machine, not the pipeline. Recommendation: C7 admits one literal,
    `service="deploy-pipeline"`, that names no deploy tag and no host, so no health gate
    reads it. Resolved in the pillar review fold: C7 *Rule labels* admits
    `service="deploy-pipeline"` as its one literal outside the rule.
13. **Does the commit-status burst fit GitHub's secondary limits at D5's scale?** D2
    measures the statuses per promotion and projects them (*Verification*). At D2 the
    bundle holds a handful of Kustomizations, so D2 itself stays far below 80 a minute.
    Recommendation: if the projection exceeds either limit, D5 batches its flips so that
    no promotion changes more Kustomizations than the limit allows. The changed set does
    not reduce the burst, because every Kustomization reconciles and posts on each
    revision. Resolution: D2.9's PR body quotes the projection, and D5 sizes its waves
    from it. Gate: before D5's first wave.

## Review log

### Round i1 (per-slice adversarial review)

Raised: D2-I1-01 at high, and D2-I1-02, D2-I1-03 and D2-I1-04 at medium. Each claim was
checked against the tree at `70591c307`. All four hold, D2-I1-02 for bento-pdf only. All
four are fixed in D2, and none is refuted. D2-I1-01, D2-I1-02 and D2-I1-04 also need edits
in other documents, which this round hands to their owners.

- **D2-I1-01 (high): D2.1a edited the play and `toposort.py`, so its landing owed every k8s
  service, and the plan called it inert.** Confirmed:
    - `deploy_changes.py:_BROAD_PLAY_PREFIXES` holds `ansible/deploy.yml` and
      `ansible/filter_plugins/`;
    - `narrow_lib/broad.py:broad_path_tags` raises `CannotNarrow` for a `PLAY_PREFIXES`
      path, and its module docstring says that `toposort.py`'s caller in `deploy.yml`
      refuses;
    - `deploy_broad_k8s.py:fold_deferred` adds every declared service when a refusal names
      no tags.

  Fixed: the play and filter edits move to a new PR, D2.1d, which holds nothing else.
  D2.1a keeps its ID for `tag_owners` and the narrowing, which other plans cite. D2.1d
  quotes the expected refusal before the PR. It adds an equality test that survives D2.7:
  the entries `ansible_owned` drops are exactly those that carry `owner`. Its landing
  discharges the fleet's lines by the operator's clear or a full deploy under D1.2's stop
  rule (**operator-only**, Open question 9), and a revert takes the same procedure. The
  *Steps* preamble no longer calls every PR inert. *Tree diff*, *Verification*,
  *Rollback*, *Risks*, *Tests and docs*, *Complexity delta* and the D2.7 preconditions
  changed. The finding's count, "about 59", is not repeated, because `fold_deferred`
  intersects with `declared` and D2 did not measure that set.
- **D2-I1-02 (medium): the X2-19 fix assumed every merge goes through `land.sh`.**
  Confirmed in `renovate.json`: the rule for `ansible/roles/k8s/**` digest and `pinDigest`
  updates sets `automerge: true` and `platformAutomerge: false`. bento-pdf's pin escapes
  both later rules that set `automerge: false`: the `annotated-pin` rule, and the
  `k8s image {{depName}} (manual ...)` rule, whose file list omits bento-pdf.
  `ansible/roles/k8s/bento-pdf/defaults/main.yml:bento_pdf_k8s_image` is digest-pinned.
  The finding holds for bento-pdf. Its claim that most cluster deploys after D6 are
  automerges is not confirmed: the manual-image rule overrides the digest rule for every
  role in its list, about 37 `defaults/main.yml` files, and those land through `land.sh`.
  Fixed: *Risks* names Renovate's automerge, a hand merge and a cleared hold as paths
  without a line, and names the duplicate. D2.7 states that bento-pdf's Renovate PRs land
  without `land-wait`. The emitter that reads each Kustomization's
  `status.lastAppliedOriginRevision` from daniel-box is handed to D6, to land before D5
  moves the bulk. The required fix's second point, an emitter built in D2, is not taken.
  Its deployer-tick candidate would die with the deployer at D6.1, and a daniel-box timer
  is D6's design to make.
- **D2-I1-03 (medium): D2.1c's census compared template paths, but pihole's member reads
  the list only through an include.** Confirmed:
  `ansible/roles/k8s/pihole/templates/configmap.yaml.j2` includes
  `config/pihole-dnsmasq.conf.j2`, and only that file loops over
  `hostvars[k8s_primary_node].containers_list`. Fixed: the census maps each moved
  `(role, template)` to its role's tag and compares with `_list_reader_tags`. pihole stays
  the named member for both the `hostvars` path and the include path. *Verification*
  changed to match.
- **D2-I1-04 (medium): H1 and H2 still branch on the own-tag-only outcome and expect 23
  readers.** Confirmed by `grep -rn 'own tag\|23 such\|23 tags'` over the H plans. Fixed:
  Open question 10 states the narrow output to expect after D2.1c for a bare entry, for an
  entry with `hostname`, and for an Ansible-owned entry. It asks H1, H2 and H6 to delete
  the branch and the expectation before their first entry-adding PR. *Risks* points to it.

Refuted: none.

### Round integration-2 (cross-slice adversarial review)

Raised: X2-06 at high and X2-19 at medium. Each claim was checked against the tree at
`70591c307`. Both hold. The plan fixes both. Part of X2-06's preferred fix is refuted by a
measurement, and part of X2-19's is refuted by the emitter's own docstring. Both hand work
to other slices.

- **X2-06 (high): H1, H2.3b and H6 add `containers_list` entries while the deployer still
  narrows, which triggers D1's 23-tag apply.** Confirmed:
  `narrow_lib/containers.py:changed_fields` returns None when the two lists' names differ.
  `reader_paths` then keeps every template that renders the list.
    - Fixed in D2: the new PR D2.1c records an added or removed entry that is not
      Ansible-owned by its fields, less `name` and `DEPLOY_MECHANICS_FIELDS`. A render census
      in `ansible/tests/k8s/` holds the rule to the tree. A bare Flux-born entry then
      reaches five reader tags rather than 23. *Design*, *Steps*, *Verification*,
      *Rollback*, *Risks*, *Tests and docs* and *Complexity delta* all changed.
    - Refuted, the "own tag only" outcome. A fixture entry carrying only `name`,
      `platform: k8s`, `owner: flux` and `depends_on` moves artifacts'
      `templates/configmap.yaml.j2`, because its `known_services.json` is
      `containers_list | map(attribute='name')`. The render went through
      `ansible/tests/_k8s_render.py:render_texts`. The control render, through the same
      override with the list unchanged, printed `CONTROL diffs: [('pihole',
      'configmap.yaml.j2'), ('uptime-kuma', 'deployment.yaml.j2')]` against the plain
      render. Against that control, the bare entry moved one template only, `('artifacts',
      'configmap.yaml.j2')`. Mapping the entry to its own tag would leave that ConfigMap
      stale. The fallback half of the fix therefore still applies. Each adding PR in H1, H2
      and H6 runs D1.2's stop rule over the reader tags that its narrow output names. Those
      steps are handed to the owning slices.
    - Also fixed: *Baseline* named three whole-entry readers. Running `reader_reads` over
      every template that renders the list printed `WHOLE-ENTRY 5`, so there are five: artifacts' `configmap.yaml.j2` and
      netpol-baseline's `networkpolicy-callers.yaml.j2` were missing. D2.1a's pair, D2.7's
      two-writer check and *Verification* now name all five.
- **X2-19 (medium): after D6, cluster deploy annotations come only from `deploy.sh`.**
  Confirmed: both emitters write a syslog line, and D2.7 already stops both for bento-pdf.
  Fixed in D2.3: `land-wait` writes a `source=promote` line on `settled`, and
  `ansible/tests/deploy/test_deploy_annotations.py` gains it as a third emitter. Refuted:
  `promote` emitting the line itself. `deploy_io.py:emit_deploy_annotation`'s docstring
  rejects a POST to Grafana, because it would need a pinned address or a standing write
  credential through Traefik. `promote` runs on a GitHub runner, so it would need that
  credential. A promotion that no `land.sh` follows still draws no annotation (*Risks*). The
  D6 and design.md wording is handed to their owners.

Raised: X-01, X-02, X-03, X-04 and X-08 at high, and X-21 at medium. Each claim was checked
against the tree at `70591c307`, or against kustomize-controller v1.9.6 read through `gh`, and
all six hold. All six are fixed in D2, and none is refuted. X-01, X-02, X-03, X-04 and X-08
also need edits in other documents, which this round hands to their owners.

- **X-01 (high): the publish job could not render bento-pdf, and `domain` had no source
  before D3.** Fixed in D2, contract part handed over. Confirmed: R1's `cluster` exits 2
  without `--secret-stubs`, `ansible/templates/ingressroute.yml.j2:ingressroute` builds the
  Host from `domain`, and `grep -c '^domain: ENC\['` on `ansible/vars/secrets.yml` prints 1.
    - D2 depends on R2's PRs 1 and 2. The publish renders in placeholder mode, never with
      `--secret-stubs`, and D2.6c's workflow test pins that.
    - D2.6a adds `homelab-identifiers`, rendered by Ansible from SOPS, as the source before
      D3. Open question 5 proposes the C3 wording and marks the plaintext alternative
      **operator-only**.
    - The red proof is D2.6c's host gate. A stub render or a literal host fails before any
      push, so `prod` cannot move.
- **X-02 (high): D2 verified the triggering SHA, not D1's target T.** Fixed in D2, contract
  part handed over. The diagram, the verdict set (`promote T`, `held`, `current`, `error`),
  `verify T`, the held value and Exit 3 follow T. `verify` takes its expected set from the
  pulled `sha-T` bundle, because the job's `out/` is X's render. D2.5 gains the T-not-X pair.
  `land-wait` already keys on cluster state and ancestry rather than on a run, which is the
  mapping C6 needs. Open questions 2 and 6 carry the C6 and C2 wording.
- **X-03 (high): `land-wait` read `lastAppliedRevision`, which holds the digest.** Fixed in
  D2, H1 and D4 parts handed over. Confirmed in kustomize-controller v1.9.6
  `api/v1/kustomization_types.go`. `KustomizationReconciler.reconcile` sets both fields only
  after `checkHealth`. `land-wait` reads `status.lastAppliedOriginRevision`, parses
  `master@sha1:<sha>`, and keeps waiting on an absent value or any other form. D2.3 tests that
  red half, and *Verification* shows the read. The finding cited v1.6.0, and D2 cites v1.9.6,
  the version D1 pins.
- **X-04 (high): D3's non-tag `cluster-vars` broke D2's census and verify set.** Fixed in D2,
  D3, D5 and contract parts handed over. D2 now owns the rule from D2.6b on: a closed
  `SHARED_KINDS` set, empty at D2, the `out/flux/<name>.yaml` layout, a census computed from
  inputs, and a reserved-name check. `verify` expects one status per Kustomization name,
  which settles D4's Open question 5. D3.3 adds `cluster-vars` to the set, and D5.1a1
  extends the rule rather than introducing it.
- **X-08 (high): the title said "on stage", and that cluster was retired on 2026-09-29.**
  Fixed in D2, index and design.md rows handed over. D2's body already ran on production. The
  title now says so, and Open question 1 asks the operator to accept a production drill.
  The stage gates in R4, D6 and H2 are outside this file.
- **X-21 (medium): D2.3 ran `fact_status.py verify` with no G2 condition.** Fixed with R4's
  wording, "Until G2 retires `docs/facts.lock`".

Also fixed while making these edits:

- D2.7's preconditions list D4.0, as D4's Open question 3 asks.
- The bundle writer writes the `resources` list of `flux/kustomization.yaml`, and the census
  checks it. D1's root names its resources explicitly, so a file it omits never reaches the
  cluster.
- `verify` pulls `sha-T` before `prod` moves, so a registry error promotes nothing.

Refuted: none.

### Round 10 (per-slice adversarial review)

Raised: D2-R10-01 to D2-R10-04 at high, and D2-R10-05 to D2-R10-16 at medium. Each claim was
checked against the tree at `70591c307` and against contracts.md, D1, D3, D4 and R2 as they
stand. All four highs are fixed. Eleven mediums are fixed. D2-R10-07's required fix is
refuted, and only its citation is corrected.

- **D2-R10-01 (high): `homelab-identifiers` contradicted C3, and D2.7 lacked the D3.3
  edge.** Confirmed: C3 names that Secret in neither branch, and
  `manifests_prune_allowlist` holds no Secret kind. Fixed: D2.6a and
  `IDENTIFIER_SOURCE_KEYS` are withdrawn. The header and the D2.7 preconditions name D3.3a
  and D3.3b. D2.6b's `postBuild` names `homelab-cluster-vars`. The source gate refuses a
  substituting tag until the bundle generates that Secret, which enforces the edge. The
  key check is left to R2's rules 1, 2 and 4 and D3's `cluster_vars.py check`, so no
  D2-local key list remains. Open question 5 is resolved by C3.
- **D2-R10-02 (high): a one-commit handback opened an unbounded two-writer window.**
  Confirmed against C2's hold rule. Fixed: the handback is two PRs, `owner: released` and
  then the removal of `owner` once the Kustomization reads `NotFound`. `land_lib/merge.py`
  refuses to merge the second while the Kustomization exists, with a test that it never
  reaches `deploy.sh`. *Rollback* states plainly that a hand merge bypasses the refusal,
  and why the play reads no Kustomization. `docs/flux-deploy-plane.md` carries the order.
  Open question 7 requests the C4 wording.
- **D2-R10-03 (high): an `owner`-only edit deployed pihole, uptime-kuma and
  monitor-bridge.** Confirmed: `containers.py:reader_paths` keeps every hit whose
  `reader_reads` is None, whatever `changed` holds, and the three templates use
  `filter_by_platform` or `selectattr`. Fixed: D2.1a names `DEPLOY_MECHANICS_FIELDS`, and
  `reader_paths` returns no hit when `changed` holds no entry. The plan states the same
  effect on a comment-only edit. D2.1a adds the pair test, and D2.7's body quotes
  `deploy_tags.py narrow`.
- **D2-R10-04 (high): D2.4's `render_targets` filter named no call site.** Confirmed:
  `render_targets.py:main` prints the function's result, and `render_records.py` runs that
  CLI with the host alone. Fixed: `render_targets()` stays the full universe, and `main`
  prints a new `render_record_targets`, so `render_records.py` is unchanged. A renderer test
  keeps a flux-owned tag renderable. Also found: `smoke_caller` would pick bento-pdf, the
  first name among five tied callers, so it now drops published tags. `expand_shared_tags`
  drops them too, instead of letting `deploy.sh` refuse the whole list.
- **D2-R10-05 (medium): `SHARED_KINDS`, the census and name-based verify had two owners.**
  Confirmed in contracts.md. D4 Open question 5 already says D2 owns them. Fixed: D2 keeps
  them, and Open question 6 requests the C2 and C3 attribution change with gates. The
  reserved-name check moves into the renderer, as C3 places it, and D4.2 adds `-deferred`
  to the same check.
- **D2-R10-06 (medium): the host gate blocked C3's literal branch.** Fixed: the host gate
  and the source gate each state a rule per branch, and the literal branch reads the value
  that the operator's ruling records.
- **D2-R10-07 (medium): the publish command does not state its mode.** The citation is
  fixed, from R2-10 to R2-10-2 and R2 *Mode selection*. The required `--placeholders` flag
  is refuted, with the evidence under *Refuted* below.
- **D2-R10-08 (medium): verify's permissions, token and failed hold write were unnamed.**
  Fixed: D2.5 adds `statuses: read` and updates D1.4's expected map with a red fixture. It
  sets `PROMOTE_TOKEN` as `GH_TOKEN` on the verify step. A failed hold write still resets
  and exits non-zero, pinned by a test, with the reason in *Publication and verification*.
- **D2-R10-09 (medium): the generated glance ignored `owner`.** Confirmed in
  `gen_role_glance.py` and `service_catalog.py:build_rows`. Fixed: D2.4b teaches both
  generators, with pair tests in `scripts/docs/tests/`.
- **D2-R10-10 (medium): label-keyed skips outlived Flux ownership.** Confirmed in part. The
  scripts now key on `tag_owners`, and the prune check keys on the label only while the
  Kustomization exists. `live_drift_check.py:is_server_side_applied` already excludes
  kustomize-controller's objects, so it needs no label skip. Its real gap was the
  surviving `managedFields` entry after a handback, which D2.4a closes with the
  annotation rule. The labels stay, because no skip reads them alone. *Rollback* says why
  the plan adds no Ansible task to strip them.
- **D2-R10-11 (medium): a drop could leave an empty `--tags`.** Confirmed in
  `_deploy_plane`. Fixed and generalised: every drop happens before its site's empty
  check. That covers the deployer, `land.sh`'s split before `deploy_by_host`, `--changed`
  and the shared-role expansion, each with a pair test.
- **D2-R10-12 (medium): the drill did not meet C8.** Fixed: D2.8 is marked
  **operator-only**, its body states the objects, what keeps serving and the rollback, and
  it names the substitute and why it suffices.
- **D2-R10-13 (medium): Exit 4 proved nothing, and D2.9 misread `land.sh` under a hold.**
  Fixed: Exit 4 reads the commit behind `last-good`, its ancestry with R, its
  `flux/bento-pdf` status and the Kustomization's origin revision. D2.9 ends at
  `deploy-failed`, and the session re-runs `promote.py land-wait <R>` after the operator's
  dispatch.
- **D2-R10-14 (medium): exit 5 was unregistered.** Confirmed: `CONTRACTS`,
  `DEPLOY_SH_NO_VERDICT` and the hook's `_REFUSALS` literal each hold the codes. Fixed:
  D2.1b registers 5 in all three, lists them in the tree diff and names their tests.
- **D2-R10-15 (medium): the source gate was stricter than C1.** Fixed: a tag without
  `postBuild` is refused only for a `${K}` naming a `secrets.yml` key, with the `${HOME}`
  and `${domain}` fixture pair.
- **D2-R10-16 (medium): `probe.py landing`'s transport was unnamed.** Fixed: *What
  `probe.py landing` reads* names each source and its cost. `prod` comes from the
  OCIRepository's `status.artifact.metadata`. `last-good` comes from one manifest fetch
  through D1's registry client, while the package stays public. The finding's example of
  listing package versions is rejected, as D1 rejected `flux list artifacts`. The live
  timing is a precondition of D2.5, because the artifact does not exist at `70591c307`.

Also fixed while making these edits:

- The clear command sets `DEPLOY_HOLD` to `none`, D1's no-hold value, and Open question 8
  records the pending C2 and C6 amendment.
- *How `land.sh` waits* no longer calls C6 a gap, and Open question 2 is resolved. Open
  question 6 drops C2's promotion-target wording, which is applied. Open question 1 drops
  the stale "on stage" sentence.
- D2.1 and D2.4 are each split in two to stay within the size guide.

Refuted:

- **D2-R10-07, the `--placeholders` flag.** R2 *Mode selection* reads "C1 *Modes* fixes the
  call, and R2 adds no flag". R2's Round 10 entry R2-10-2 reads "`--placeholders` is gone
  from the design". The finding quotes R2's integration-round entry, which R2-10-2
  superseded. C1 *Modes* lists only `--out`, `--role` and `--secret-stubs`. Passing
  `--placeholders` would call a flag that no plan adds. The command therefore states its
  mode by the absent `--secret-stubs`, as R2 reads C1, and the D1.4 workflow test pins that
  absence with a red fixture.

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review
round covers it.

- **F-01, promotion gated on the whole fleet.** *Design* states that `verify` expects T's
  changed set from `digests.json` `footprint`, plus `dependsOn` dependents, minus suspended
  Kustomizations, with an already-failing dependent reported only and an empty set
  expecting `flux/homelab` alone. The hold reads `<T> failed=<names>`. The revert rule is
  code behind a disabled flag, and turning it on is Open question 11 (**operator-only**).
  `decide`'s pull of `prod`'s artifact is stated as one extra pull. `digests.json`, its
  `_unclassified` key and the footprint-rule census join D2.6b, D2's first publishing PR.
  `land-wait` in D2.3 waits on M's changed set (C6). D2.3, D2.5 and D2.6b grow, with added
  red halves under *Verification*. *Goal* and Exit 3 name the `failed=` form.
- **F-06, silent prune.** `decide` refuses a candidate whose object set lost an entry that
  no `Removes-Object` trailer in (`prod`, T] declares. A handback's dropped Kustomization
  is exempt. The check joins D2.6c with its red proof, its named member being bento-pdf's
  `.local.` IngressRoute. *Risks* updates the dropped-file entry and adds the stall it can
  cause.
- **F-02, deploy alerts.** An added PR, D2.6d, adds the deploy-pipeline exporter as a
  monitor-bridge family with a read-only token and the rules `DeployHoldActive`,
  `PromotionStalled` and `ProdMovedOutsidePromote`. Each has promtool cases. M1's rule 2
  adds `DeployPipelineExporterAbsent` as the zero-series case. D2.5 writes `PROD_DIGEST`
  on every `prod` move. Exit 3 checks two of the alerts live. The `service` label went to
  Open question 12.
- **F-08 and RA-10, two holds.** `probe.py landing` prints a line for each hold on every
  run while both exist, with a red pair in D2.5.
- **F-13, latency and volume.** *Verification* gains three measurements: merge-to-Ready
  time, the commit-status burst against the secondary limits, and the status descriptions
  checked for a substituted value. The scaling question went to Open question 13.
- **S5, the `sha` input.** The render job runs D1's ancestry check on the dispatched commit
  before rendering, and D2.5's workflow test pins the order with two red fixtures.
- **Contract alignment.** The header and D2.7's preconditions name M1. The diagram follows
  C2's render and publish split. Open question 8 is resolved, because C2 states `none`.

### Operator ruling fold (2026-10-14)

The operator ruled that `domain` is published, and that the production drill is accepted.

- **`domain` is published.** Open question 5 is resolved. *Where `domain` comes from* replaces
  *Where `${domain}` comes from before D3*. D2.7 waits on domain PR 2b of R2's *Publish
  `domain`* item and on no PR of D3. The header, the D2.7 preconditions, the host gate and
  its tests, *Risks* and *Rollback* follow. Review-log entries above keep their wording as
  history.
- **The drill is accepted.** Open question 1 is resolved. The session merges D2.8 itself.
- **Three contract amendments are applied.** C1 *Cluster output*, C3 *Reserved names* and C4
  *Values of `owner`* now state what Open questions 6 and 7 requested.

### Reader-set follow-up (2026-10-15)

- **#4955: the design counted five whole-entry readers that 8e79e95d6 had moved onto named
  filters.** Confirmed. The D2.1c PR (#4977) already restated *The ownership flag and its
  three readers* and Open question 10 against the tree after 8e79e95d6. Six sites still
  relied on the five as live readers: the two `reader_paths` bullets and the sentence on
  an edit that changes no field, the D2.1a and D2.1c test steps, D2.7's two-writer check
  and *Verification*. Re-measured at `a5abf8581` with
  `deploy_tags.py narrow` over a scratch commit adding an `owner: flux` entry with
  `platform` and `depends_on`. It printed
  `artifacts,blackbox-exporter,longhorn-restore-drill,pihole,uptime-kuma`. Fixed: those
  sentences now name the five as `70591c307` readers and D2.1a's fixture readers in their
  shapes. Open question 10 gains the `port`, `tier` and `netpol_from` sets measured on the
  same tree.
