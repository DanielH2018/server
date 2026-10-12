# D1 — Flux installed, owning nothing

Status: PLANNED

Depends on: none

Unblocks: [D2](d2-first-leaf.md)

## Goal and exit criterion

After D1, three Flux controllers run in the `flux-system` namespace: source-controller,
kustomize-controller and notification-controller. They follow
`oci://ghcr.io/danielh2018/homelab-manifests:prod`. A GitHub Actions workflow named `promote`
moves that tag to the newest published master commit. The artifact holds one empty root
Kustomization, so Flux applies no object outside its own namespace, and Ansible still deploys
every service.

**Exit criterion.** Four facts, each checked under *Verification*. X is a docs-only master
commit merged after PR D1.5. T is the commit the root Kustomization reports applied, which
Exit 2 reads. T is X, or a newer master commit when a later publish superseded X before a
`promote` run decided.

1. GitHub shows a commit status with context `flux/homelab` and state `success` on T.
   notification-controller posted it, although revision T changed nothing.
2. The Kustomization `homelab` in `flux-system` reports `lastAppliedOriginRevision`
   `master@sha1:T`, and T is X or descends from X.
3. `homelab` is the only Kustomization, and its inventory is present and names no object
   outside `flux-system`.
4. The flux role's `CLAUDE.md` records the controllers' working-set memory beside the 192Mi
   the three controllers request.

## Baseline at `70591c307`

The tree was read at `70591c307`. Live facts were read on 2026-10-11 with read-only `kubectl`,
and upstream facts through `gh`.

### The cluster and Flux upstream

- **No Flux is installed.** `kubectl get ns flux-system` returns `NotFound`, and no CRD name
  contains `fluxcd`. ADR-0010 deferred Flux
  ([ADR-0010](../../adr/0010-pull-based-gitops-over-argo-and-flux.md)).
- **Plain `kubectl` cannot read a Flux kind.** Plain `kubectl` authenticates as a read-only
  ServiceAccount (root `CLAUDE.md`). `ansible/roles/setup/k3s/templates/readonly-rbac.yaml.j2`
  binds it to `view`, adds `get`, `list` and `watch` on `customresourcedefinitions`, and
  grants `resources: ["*"]` read on each group in
  `ansible/roles/setup/k3s/defaults/main.yml:k3s_readonly_crd_api_groups`. That list holds
  `traefik.io`, `metallb.io`, `longhorn.io` and `k3s.cattle.io`, and no Flux group, so a
  `kubectl get` on any Flux kind returns Forbidden.
  `ansible/tests/k8s/test_k8s_manifests_rbac.py:test_readonly_role_covers_the_crd_groups_this_homelab_deploys`
  checks only the Traefik groups and the k3s group. Its docstring says a missing group
  "degrades silently".
- **A change to that list is a hand apply.** `ansible/roles/setup/k3s/tasks/kubeconfig.yml`
  renders and applies `readonly-rbac.yaml.j2` under the tag `kubeconfig`, and
  `scripts/deploy_tools/tests/test_narrow_setup.py:test_the_real_k3s_role_still_maps_readonly_rbac_to_kubeconfig`
  maps both the template and `k3s_readonly_crd_api_groups` to that tag. `ansible/k3s-bringup.yml`
  applies the role, and it is one of
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_changes.py:_BROAD_MANUAL_PREFIXES`.
  So the tick fast-forwards a change to the role and records `k3s` as a `manual_plane` line,
  as [GitOps pipeline](../../gitops-pipeline.md), *A role only a hand can apply is recorded,
  not parked*, describes. That section prints the narrowed pair:
  `k3s-bringup.yml --tags kubeconfig`, then
  `gitops_state.py clear-owed manual_plane k3s --applied kubeconfig`.
- **The cluster runs Kubernetes `v1.37.1+k3s1`.** The newest Flux release is v2.9.6
  (2026-10-01), and no v2.10 release exists. The v2.9.0 release notes list compatibility with
  Kubernetes 1.34, 1.35 and 1.36 only.
- **Memory.** `kubectl top node` reads daniel-box at 13,725Mi (47%) and daniel-server at
  10,804Mi (34%). ADR-0010's premise was 65%.
- **The Flux CRDs fit a client-side apply.** In the v2.9.6 `install.yaml`, the largest CRD is
  `helmreleases` at 76,373 bytes and `kustomizations` is 40,655 bytes. Both are under the
  262,144-byte limit that `last-applied-configuration` puts on a client-side apply, which is
  how the manifests role applies (`ansible/roles/k8s/manifests/tasks/main.yml`, task "Apply
  manifests for ...").
- **What the unmodified install carries.** flux2 v2.9.6 `manifests/rbac/kustomization.yaml`
  includes `resourcequota.yaml`, a ResourceQuota named `critical-pods`, in every install. The
  controller Deployments set `priorityClassName: system-cluster-critical`. Upstream
  `config/manager/deployment.yaml` gives each pod the label `app: <controller>`. Its
  containers assert `runAsNonRoot` and pin no `runAsUser`, and the kustomize-controller
  v1.9.6 `Dockerfile` declares `USER 65534:65534`. source-controller v1.9.6 states
  `strategy: Recreate`. kustomize-controller v1.9.6 and notification-controller v1.9.4 state
  no strategy.
- **The controllers' volumes and inputs.** source-controller v1.9.6
  `config/manager/deployment.yaml` mounts two `emptyDir` volumes named `data` and `tmp`.
  kustomize-controller v1.9.6 and notification-controller v1.9.4 each mount one named `temp`.
  No controller reads a Secret or a ConfigMap through `env`, `envFrom` or a `subPath`: a
  search of the three files for `secret`, `configmap` and `subPath` finds nothing.
- **An empty inventory is a present field.** kustomize-controller v1.9.6
  `api/v1/inventory_types.go` declares `ResourceInventory.Entries` without `omitempty`, and
  `internal/inventory/inventory.go:New` starts it as an empty list. A root that applied
  nothing therefore reports `status.inventory` as `{"entries":[]}`, and a Kustomization that
  never reconciled reports no `status.inventory` at all.
- **Flux keeps two applied revisions.** kustomize-controller v1.9.6
  `api/v1/kustomization_types.go` documents `LastAppliedRevision` as the revision of the
  applied artifact. For an OCI source that is a tag and a digest:
  source-controller v1.9.6 `OCIRepositoryReconciler.getRevision` returns `<tag>@<digest>`, so
  the root would read `prod@sha256:<digest>`. `LastAppliedOriginRevision` carries the
  artifact's `org.opencontainers.image.revision` annotation. flux2 v2.9.6
  `cmd/flux/push_artifact.go` writes `--revision` into that annotation (fluxcd/pkg
  `oci/constants.go:RevisionAnnotation`). So `master@sha1:X` appears only in the origin
  revision. The OCIRepository keeps the same split. Its `status.artifact.revision` is
  `<tag>@<digest>`, and source-controller v1.9.6
  `internal/controller/ocirepository_controller.go` copies the manifest's annotations into
  `status.artifact.metadata`, where the commit sits under `org.opencontainers.image.revision`.
  Only kustomize-controller's own `checkDependencies` compares the `<tag>@<digest>` forms: a
  dependency's `lastAppliedRevision` against the source's artifact revision.
- **How notification-controller v1.9.4 posts commit statuses**, read through `gh`:
    - `internal/notifier/github.go` skips Progressing events. It maps severity `info` to
      `success` and `error` to `failure`. It skips a status whose context, state and
      description already match the newest one on that SHA.
    - The default context comes from the Provider's UID.
      `api/v1beta3/provider_types.go` defines `spec.commitStatusExpr`, a CEL expression that
      replaces it. C2's context `flux/<kustomization>` therefore needs that field.
    - `internal/server/event_handlers.go` drops, for a git provider, every event that is
      neither marked as a commit-status update nor of severity `error`. The same file reads
      the Provider's Secret through the API for each event (`getSecret`), so a rotated token
      needs no pod restart.
    - kustomize-controller v1.9.6 marks only its "Reconciliation finished" event as a
      commit-status update, and emits that event only when the Kustomization is Ready
      (`internal/controller/kustomization_controller.go`). Its "Dependencies do not meet
      ready condition" event is `info`, unmarked, and is therefore dropped. A `success`
      status on X means that Kustomization was Ready at revision X.
    - The status lands on the commit the artifact carries. kustomize-controller v1.9.6
      `KustomizationReconciler.event` puts both revisions in the event's metadata, under its
      API group prefix. notification-controller v1.9.4 `EventServer.combineEventMetadata`
      strips that prefix before the notifier runs. fluxcd/pkg
      `apis/event/v1beta1/event.go:Event.GetRevision`, at the `apis/event/v0.27.2` tag that
      the controller's `go.mod` requires, returns `originRevision` before `revision`. So a
      status for an artifact pushed with `--revision master@sha1:T` goes to T, never to a
      triggering commit that T superseded.
- **The cluster already pulls from GHCR without a pull secret.** `texbrain_k8s_image` in
  `ansible/roles/k8s/texbrain/defaults/main.yml` is `ghcr.io/danielh2018/texbrain`, built by
  the fork's CI. [ADR-0018](../../adr/0018-the-repository-stays-public-and-ci-stays-hosted.md)
  keeps the repository public and CI hosted.
- **cosign v3 stores a signature as an untagged referrer.** `gh release list -R
  sigstore/cosign` names v3.1.3 (2026-08-06) as the newest release. Its
  `cmd/cosign/cli/options/sign.go` registers `--new-bundle-format` with default `true` and
  marks the flag deprecated: "this will be the only supported format in future versions".
  cosign v3.1.3 `pkg/oci/remote/write.go:WriteReferrer` writes the Sigstore bundle as its own
  manifest whose `subject` names the signed digest. That manifest carries no tag. A registry
  without the OCI 1.1 referrers API lists it instead in an index tagged `sha256-<digest>`.
  source-controller v1.9.6 `internal/oci/cosign/cosign.go:CosignVerifier.Verify` looks for
  bundles "via the OCI 1.1 referrer API or an OCI 1.0 referrer tag", and falls back to a
  cosign v2 signature when it finds none.

### How a k8s role is deployed

- **A role is reachable only through its `containers_list` entry.** `ansible/deploy.yml`
  includes each k8s role in a loop over `containers_list`, with tags defaulting to the entry's
  name. `ansible/inventory/host_vars/daniel-box.yml` declares 56 `platform: k8s` entries, and
  `ansible/roles/k8s/` holds 63 roles.
- **The scaffolder writes a one-image service.** `uv run python
  scripts/dev/new_k8s_service.py --help` shows that `--image` and `--port` are required.
  `scripts/dev/new_k8s_service.py:entry_lines` always writes `port:`, and
  `scripts/dev/new_k8s_service.py:deployment_template` writes one Deployment.
- **The shared role queues its rollout in `homelab`.** In
  `ansible/roles/k8s/manifests/tasks/main.yml`, the task "Queue the batch drain for the
  rollout of ..." appends `manifests_rollout | default(manifests_service)` with namespace
  `k8s_namespace`, which `ansible/inventory/group_vars/all.yml:k8s_namespace` sets to
  `homelab`. `ansible/roles/k8s/manifests/tasks/drain.yml` runs `rollout status` on it and
  fails with "rollout did not complete". `ansible/roles/k8s/observability/tasks/main.yml`
  shows the way out: `manifests_rollout: ''`, `manifests_self_rollouts` with a namespace, and
  the role's own wait and stabilise-gate snapshot.
- **A self rollout expects a restart unless it narrows its signals.**
  `ansible/roles/k8s/manifests/defaults/main.yml:manifests_restart_triggers_default` is
  `[config, secret, image]`. In `ansible/roles/k8s/manifests/tasks/release_stamp.yml`, the
  task "Work out which workloads this apply must roll" applies that default to every
  `manifests_self_rollouts` entry that names no `restart_on`. The `secret` signal fires
  whenever `manifests_secret_render` changed. Only a change to that workload's own pod
  template, which `manifests_rolled_by_apply` records, then clears `restart: true`.
  `scripts/diagnostics/probe_lib/health.py:release_expected_restarts` and
  `format_role_health` fail such a workload as NOT ROLLED when it has no newer
  `restartedAt`. `ansible/roles/k8s/observability/defaults/main.yml:observability_stabilise_workloads`
  narrows each of its entries with `restart_on`, and
  `ansible/tests/deploy/test_restart_on_narrows_the_restart_signals.py:_EXPECTED_NARROWERS`
  names observability as the one narrowing role.
- **A dropped document stays live.** `kubectl apply` deletes nothing it no longer sees, and
  `manifests_prune_allowlist` holds no Flux kind. The prowlarr task "Retire the flaresolverr
  config PVC" in `ansible/roles/k8s/prowlarr/tasks/main.yml` deletes a retired object by name
  with `--ignore-not-found`, under `when: not k8s_no_mutate`.
- **A server dry run cannot create a namespace's contents.** Under `--dry-run`, the task
  "Apply manifests for ..." applies with `--dry-run=server`, which persists nothing, the
  Namespace document included. kubernetes
  `staging/src/k8s.io/apiserver/pkg/admission/plugin/namespace/lifecycle/admission.go:Lifecycle.Admit`
  returns NotFound for a create in a namespace that does not exist, and has no dry-run
  exemption.
- **The manifests role prunes one namespace.** A role outside `homelab` passes its own
  `manifests_prune_namespace`, as `ansible/roles/k8s/observability/tasks/main.yml`,
  `ansible/roles/k8s/longhorn-ui/tasks/main.yml` and
  `ansible/roles/k8s/dri-device-plugin/tasks/main.yml` do. The pruned kinds are
  `ansible/roles/k8s/manifests/defaults/main.yml:manifests_prune_allowlist`.
- **The shared apply is one directory apply.** The task "Apply manifests for ..." runs
  `kubectl apply -f <dir>/` once. A CRD and an object of that CRD in one directory fail on a
  cluster that lacks the CRD. `manifests_deferred_files` and `manifests_deferred_dir_name`
  stage a file in a sibling directory that the role applies itself, as
  `ansible/roles/k8s/headlamp/tasks/main.yml` and
  `ansible/roles/k8s/netpol-baseline/tasks/main.yml` do.

### What the deployer does with an inventory change

- **An inventory change is a deploy-plane change, applied by the tick.**
  `ansible/roles/setup/gitops_deploy/CLAUDE.md`, *Safety*, routes `inventory/` to
  `deploy.yml`. `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_narrow.py:_deploy_plane`
  narrows the range with `deploy_tags.py narrow`. `scripts/deploy_tools/narrow_lib/containers.py:entry_change_tags`
  maps an added entry to its own tag, and `scripts/deploy_tools/narrow_lib/broad.py:_list_reader_tags`
  adds the roles that read the list.
- **An added entry reaches every reader of the list, whatever it reads.**
  `scripts/deploy_tools/narrow_lib/containers.py:changed_fields` returns None when the entry
  names differ, and its docstring says an added entry "moves every reader that iterates the
  list". `containers.py:reader_paths` then keeps every template that renders the list, and
  `broad.py:_list_reader_tags` adds the importers of each shared template among them.
  Measured at `70591c307`, `_list_reader_tags` with `changed` None returns 23 tags of 59
  declared: artifacts, authelia, autofix-bridge, cloudflare-ddns, crowdsec, game-stats,
  gpu-exporter, home-assistant, homepage, monitor-bridge, mosquitto, netpol-baseline, nut,
  nut-exporter, observability, pi-peer-backup, pihole, pihole-exporter, registry, traefik,
  uptime-kuma, wg-easy and zigbee2mqtt. With `flux` added, D1.2's range reaches 24 of 60.
  `broad.py:narrow` refuses only a list covering more than half the declared services, so
  this range narrows and does not refuse.
- **That plane ignores `k8s_autodeploy`.** The marker
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_narrow.py:DECIDED: the broad plane ignores`
  records it, and `_deploy_plane` logs denied tags as "applied here regardless". traefik,
  authelia and pihole are among the 23 readers and declare `k8s_autodeploy: false`. Every
  broad arm is forward-only: a failure writes `hold_sha` and a `hold_plane` entry naming
  `ansible/deploy.yml` and the whole tag list.
- **A refused narrowing applies nothing and records owed lines.**
  `deploy_narrow.py:_deferred` records one `k8s_unapplied` line per service the refusal
  printed. The fleet-coverage refusal in `broad.py:narrow` prints the tags it reached, and
  any other refusal prints none, so every declared service gets a line.
  `docs/gitops-pipeline.md`, *Broad changes*, says `land.sh` then reports
  `needs-manual-apply`. A successful `deploy.sh` discharges each line whose service it
  deployed (`scripts/deploy_tools/deploy_lib/playbook.py:discharge_owed_k8s`).
- **Only the deployer clears a broad hold.** `gitops_hold.py:broad_hold_cleared_by` clears a
  held tag list only on an apply whose tags are a superset of it, and
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_handlers.py` is the only
  caller of the broad clear. A hand `deploy.sh` clears no hold. After a hand apply, the
  operator runs `gitops_state.py clear-hold <sha>`.
- **The release record names a reader carrying someone else's change.**
  `uv run python scripts/diagnostics/probe.py releases --stale-only` prints one line per
  service whose role paths moved past its applied commit on `origin/master`, or that has no
  record. Its exit code covers the whole fleet, so only the names it prints answer a
  question about particular services. The plain `probe.py releases` table also flags
  `unmerged`, a service running a commit that never landed, and `dirty`.

### CI, the fleet guards and the registries

- **CI.** `.github/workflows/ci.yml` is the workflow named `CI`. It runs on every push to
  master, in a concurrency group per SHA. Its comment records a measurement: GitHub keeps at
  most one pending run per concurrency group, and a newer run evicts the queued one. Actions
  are pinned by commit SHA, for example `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1`.
  Every job runs on `ubuntu-26.04`, and the `workflow-runners-are-pinned` row of
  `ansible/tests/repo/test_census_rows_text.py` refuses a floating label. The workflows at
  this commit are `ci.yml`, `image-smoke.yml` and `renovate-config-canary.yml`.
- **The deployer reads CI through `deploy_git.py:ci_verdict`.**
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_git.py:_CI_NO_VERDICT_CONCLUSIONS`
  treats `cancelled` as no verdict. `ci_verdict` takes the set of required check-run names and
  returns `pass` when that set is empty, which its docstring calls the gate being disarmed. The
  deployer's set is `ansible/roles/setup/gitops_deploy/defaults/main.yml:gitops_deploy_ci_contexts`,
  rendered into a host-only `config.env`.
- **A `workflow_run` workflow runs master's copy.** For that trigger, GitHub sets
  `github.sha` to the default branch's newest commit and reads the workflow file from it,
  whatever commit the triggering run built. D1's *Risks* names this failure class 7.
- **Master's merge rate.** The comment above
  `ansible/roles/setup/gitops_deploy/defaults/main.yml:gitops_deploy_ci_ancestor_walk_max`
  records about 124 merges a day.
- **Listing artifacts costs two fetches per tag.** fluxcd/pkg `oci/list.go:Client.List`, the
  code behind `flux list artifacts`, lists every tag and then fetches each non-cosign tag's
  manifest and digest in sequence. One failed fetch fails the whole listing.
- **`gh` cannot set an empty variable.** cli/cli `pkg/cmd/variable/set/set.go:getBody` uses
  `--body` only when it is non-empty. Otherwise it prompts, or reads standard input.
- **Fleet guards the unmodified install trips:**
    - `ansible/tests/k8s/test_namespace_resource_defaults.py:test_no_resourcequota_is_declared_anywhere`
      runs `git grep` for `kind: ResourceQuota` over the `ansible/` source tree.
    - `ansible/tests/k8s/test_pod_template_hygiene.py:_SYSTEM_TIER` allows a class outside
      the four homelab tiers for dri-device-plugin only.
    - `ansible/tests/k8s/test_container_security_context.py:test_every_asserting_container_pins_a_uid`
      requires a uid pin beside `runAsNonRoot`.
    - `ansible/tests/k8s/test_deploy_strategy.py:test_every_deployment_states_a_strategy`
      requires a stated strategy, and `_RECREATE` must list every `Recreate` Deployment.
    - The `pods-disable-service-link-env-vars` and `rendered-kinds-have-vendored-schemas` rows
      of `ansible/tests/k8s/_workload_property_rows.py` read every rendered pod and kind.
      `scripts/validate/refresh_vendored_schemas.py:CORE_GROUP_FILES` holds no
      apiextensions.k8s.io file, and `scripts/validate/refresh_vendored_schemas.py:VENDORED`
      holds no fluxcd group.
    - `ansible/tests/k8s/test_workload_shell_uses_the_macros.py:shell_offences` keys on a
      literal `kind:` line in a template's source.
    - `ansible/tests/k8s/test_volume_names_descriptive.py:GENERIC` holds `data`, `tmp` and
      `temp`, and `ALLOWED_UNPREFIXED` exempts only `media`.
      `test_volume_names_name_their_workload` reads a rendered `- name:` line textually, so a
      render that sorts `mountPath` before `name` hides the names from it (failure class 4).
    - `ansible/tests/k8s/test_deferred_manifest_dir_is_reserved.py:test_the_census_finds_the_known_deferred_directories`
      asserts that the deferred directories equal `KNOWN_DEFERRED_DIRS`, an exact map.
- **A service's priority class comes from its tier.** The comment above the entries in
  `ansible/inventory/host_vars/daniel-box.yml` says a tiered role's templates take the class
  from `tier_priority_class` rather than writing it (#3452).
  `ansible/filter_plugins/service_tier.py:SERVICE_TIERS` maps `platform` to
  `homelab-critical`. `ansible/tests/k8s/test_priority_class_is_derived_from_tier.py:_CLASS_AT_DERIVATION`
  is compared with exact equality. `service_tier.py:shed_entries` sheds every untiered entry,
  and [Break-glass](../../break-glass.md), scenario *one node is down and the survivor runs
  short of memory*, scales `probe.py shed-set` to zero.
- **The tiers sit below the system classes on purpose.**
  `ansible/roles/setup/k3s/templates/priorityclass.yaml.j2` keeps all four homelab classes
  below `system-cluster-critical`. Tier 1, `homelab-critical`, holds the edge, auth, the
  image registry and the alert bridge.
- **kube-state-metrics takes its configuration as arguments.**
  `ansible/roles/k8s/observability/templates/kube-state-metrics.yaml.j2` renders no ConfigMap
  and no `checksum/config` annotation. The comment above its ClusterRole requires the RBAC
  rules and the `--resources` argument to change together. observability's *One config change,
  one restart* section in its `CLAUDE.md` rolls each workload through its pod template, and
  `ansible/tests/services/test_observability_config_rolls_one_workload.py` pins the data-key
  count of each ConfigMap the role renders. observability declares `k8s_autodeploy: false`.
- **Renovate.** In `renovate.json`, the manager for `# renovate:` annotations matches
  `ansible/roles/k8s/*/defaults/main.yml` and needs the annotation on the line above a
  `*_version:` key. No manager matches a role's `files/`. The `manual — k8s_autodeploy:
  false` rule is docker-only
  (`ansible/tests/deploy/test_renovate_automerge_follows_the_autodeploy_denylist.py:test_the_manual_rule_is_scoped_to_container_images`),
  while the non-major automerge rule names no datasource. A per-package manual rule for a pin
  that a denied role owns carries the `k8s_autodeploy: false` marker
  (`ansible/tests/deploy/test_renovate_per_package_manual_rules_carry_the_denylist_marker.py`).
- **The rotation registry tracks SOPS keys only.** `scripts/secrets_mgmt/secret_rotation.py`
  reconciles `ansible/secret_rotation.yml` against `ansible/vars/secrets.yml`, and `sync`
  prints "stale" for any other name. The daily audit turns the "Secret Rotation" monitor down
  when a key passes its tier window. The `external` tier is 365 days, and
  [Secret rotation](../../secret-rotation.md) defines `source: record` for a SOPS copy of a
  credential that something else owns.
- **ADRs are never rewritten.** [Architecture decisions](../../adr/index.md) says a reversed
  decision gets a new ADR, and the old one's status becomes `Superseded by ADR-NNNN`.
  `ansible/tests/repo/test_adr_links.py:VALID_STATUS` accepts `Proposed`.

## Design

### What D1 adds

```diff
+ ansible/roles/k8s/flux/
+   CLAUDE.md
+   defaults/main.yml               flux_version (Renovate-annotated), flux_pod_patch,
+                                   flux_controllers (restart_on: []), flux_sync_suspend,
+                                   flux_verify_drill, k8s_autodeploy: false
+   files/gotk-components.yaml      vendored install, ResourceQuota stripped, header names
+                                   the version and the pipeline that produced it
+   tasks/main.yml                  include k8s/manifests with no shared wait and three self
+                                   rollouts; own rollout wait; CRD wait; deferred sync apply;
+                                   delete ocirepository/verify-drill while the lever is off
+   templates/components.yaml.j2    the vendored documents with flux_pod_patch merged in, the
+                                   class from tier_priority_class, the volumes renamed
+   templates/sync.yaml.j2          deferred: OCIRepository, Kustomization, Providers, Alerts
+   templates/secret.yaml.j2        Secrets flux-github-status (token) and flux-discord (address)
  ansible/inventory/host_vars/daniel-box.yml
+   containers_list entry `flux`: platform k8s, tier platform, no port, no hostname, no owner
  ansible/tests/k8s/test_deploy_strategy.py                 _RECREATE gains source-controller
  ansible/tests/k8s/test_priority_class_is_derived_from_tier.py   _CLASS_AT_DERIVATION gains flux
  ansible/tests/k8s/test_deferred_manifest_dir_is_reserved.py     KNOWN_DEFERRED_DIRS gains flux-sync
  ansible/tests/deploy/test_restart_on_narrows_the_restart_signals.py   _EXPECTED_NARROWERS gains flux
  ansible/roles/setup/k3s/defaults/main.yml   k3s_readonly_crd_api_groups gains the three Flux
                                              groups (D1.2b, a hand apply)
  ansible/tests/k8s/test_k8s_manifests_rbac.py   the CRD-group coverage test derives the groups
                                                 of every CRD the flux role renders
  scripts/validate/refresh_vendored_schemas.py   apiextensions core file, four Flux kinds
+ scripts/validate/schemas/…                     the schemas it fetches
  renovate.json                     flux defaults path in the denylist rule, and a
                                    per-package manual rule for fluxcd/flux2
+ .github/workflows/promote.yml     name: promote; jobs publish and promote
+ .github/workflows/prune-artifacts.yml   daily retention of old sha-* versions (D1.7)
+ scripts/deploy_tools/promote.py   decide and prune, called by the two workflows
  ansible/roles/k8s/observability/  kube-state-metrics argument for Flux kinds, a scrape job
+ docs/flux-deploy-plane.md
+ docs/adr/<next free number>-…    Proposed: Flux applies the cluster from a promoted artifact
  docs/adr/0010-…                   a dated line in Status, linking the proposal
  docs/gitops-pipeline.md           one sentence: Flux runs and applies only flux-system
```

### How a commit reaches the cluster after D1

```text
merge X ─▶ CI (ci.yml) ── success ──▶ workflow_run ─▶ promote.yml
  publish job:  check out head_sha (X); build the bundle (D1: flux/kustomization.yaml
                with resources: [])
                flux push artifact oci://…/homelab-manifests:sha-X --revision master@sha1:X
                cosign sign the pushed digest (keyless, the workflow's own identity)
  promote job (needs: publish; concurrency: promote):  check out github.sha, the
                workflow's own commit; promote.py decide
                any read errors            ─▶ error: exit non-zero, no tag moves
                DEPLOY_HOLD other than none ─▶ held
                GET prod's manifest; its org.opencontainers.image.revision is prod's commit
                walk origin/master from its tip back to prod's commit; per candidate C,
                HEAD sha-<C>, then the CI verdict, then cosign verify; the first C with
                all three is T
                no such commit             ─▶ current
                otherwise                  ─▶ promote; step outputs verdict and target=T
  tag step:     flux tag artifact :sha-<target> --tag prod   (target from decide, not head_sha)
daniel-box:   source-controller polls :prod every minute and verifies the signature
              kustomize-controller builds ./flux (no objects) ─▶ Ready, origin revision T
              notification-controller ─▶ GitHub status flux/homelab = success on T
```

### Decisions

- **Three controllers only.** The vendored file is the output of `flux install --export
  --components=source-controller,kustomize-controller,notification-controller
  --network-policy=true --version=<flux_version>`, piped through a filter that drops the
  document of kind `ResourceQuota`. helm-controller and the image-automation controllers stay
  out, because nothing here uses them. The file's first line names the version and the whole
  pipeline, so a regeneration is reviewable as that pipeline plus the upstream diff.
- **The role meets the fleet guards by patching, with one exemption (operator-visible).** Each
  guard under *Baseline* gets one answer:
    - The `critical-pods` ResourceQuota is stripped when the file is vendored.
      `test_no_resourcequota_is_declared_anywhere` reads the source tree, so no render-time
      step can satisfy it. The quota exists so that `system-cluster-critical` pods admit
      outside `kube-system`, and the next patch removes that class.
    - The entry `flux` declares `tier: platform`, and `components.yaml.j2` sets each pod's
      `priorityClassName` from `containers_list | tier_priority_class('flux')`, which is
      `homelab-critical`. No template writes the class as a literal (#3452). The homelab
      tiers sit below the system classes on purpose, and the `platform` tier already holds
      the image registry and the alert bridge. From D2 on, Flux is the deploy path, so it
      belongs with them. A tier also keeps the entry out of `probe.py shed-set`, which
      [Break-glass](../../break-glass.md) scales to zero when a node is lost.
      `ansible/filter_plugins/service_tier.py:tier_priority_class` takes the list as its
      first argument, so this mention makes the flux role a `containers_list` reader from
      D1.2 on (*Risks*, and *D1 adds no root-only suspend* below).
    - `enableServiceLinks: false`, a pod-level `runAsUser` and `runAsGroup` pinned to the uid
      the controller images declare, and a `revisionHistoryLimit` are patched in.
    - The four generic volume names are renamed: `data` and `tmp` to
      `source-controller-data` and `source-controller-tmp`, and each `temp` to
      `kustomize-controller-tmp` or `notification-controller-tmp`. A merge patch cannot
      rename a list item, so `components.yaml.j2` rewrites `volumes` and every container's
      `volumeMounts` as lists, matching each mount by its old name. It renders each list
      item with `name` as its first key, so `test_volume_names_name_their_workload` reads
      the new names rather than missing them. Rejected: a second `ALLOWED_UNPREFIXED`
      entry, because that set exists for one name shared by a whole stack.
    - kustomize-controller and notification-controller get the `RollingUpdate` strategy they
      leave implicit. source-controller keeps upstream's `Recreate` and gets a `_RECREATE`
      row in `ansible/tests/k8s/test_deploy_strategy.py` with a reason. This is the one
      exemption.
    - `KNOWN_DEFERRED_DIRS` gains `"flux-sync": "flux"` in D1.5, where the deferred file
      arrives. That is a census member, not an exemption.
    - The schemas are vendored first, in D1.1.

  `flux_pod_patch` in the role's defaults holds the patch, and `components.yaml.j2` merges it
  into each parsed Deployment, then applies the class and the volume rewrite. The vendored
  file therefore stays the pipeline's output.
  Rejected: an exemption row per guard. Each row would be a lasting exception for the workload
  that deploys everything from D5 on, and the `_SYSTEM_TIER` comment calls a new entry "the
  decision the guard exists to force".
- **The role is written by hand, not scaffolded.** The scaffolder writes a one-image
  Deployment, a `port:` and an image pin, and this role keeps none of them. The two censuses
  the scaffolder names are for that Deployment and the shared Service.
- **The role waits on its own controllers, and restarts none.** The include sets
  `manifests_rollout: ''`, so the shared role queues no rollout in `homelab`. It passes
  `flux_controllers` from the role's defaults as `manifests_self_rollouts`: three entries
  (source-controller, kustomize-controller, notification-controller), each with
  `namespace: flux-system`, `kind: deploy` and `restart_on: []`. The role then runs
  `rollout status` on each in `flux-system`, snapshots their restart counts and hands them to
  the stabilise gate, as observability does.
    - `restart_on: []` is required, not optional. Without it the default
      `[config, secret, image]` applies. Any change to the secret render, and any applied
      change that leaves the controllers' pod templates alone, such as a NetworkPolicy edit,
      then stamps all three `restart: true`. No task restarts them, so `probe.py health flux`
      fails each as NOT ROLLED. That happens first at D1.5's landing, which adds
      `secret.yaml.j2`, and again at every rotation of `flux_github_status_token` or
      `gitops_deploy_discord_webhook`.
    - No signal applies. No controller reads a Secret or a ConfigMap through `env`, `envFrom`
      or a `subPath` (*Baseline*). notification-controller reads the Provider's Secret through
      the API on each event. A change to a controller's pod template rolls it through the
      apply, which `manifests_rolled_by_apply` records. No controller image is built here.
    - This follows `observability_stabilise_workloads`, which narrows the same way.
- **The Flux objects apply after the CRDs.** `sync.yaml.j2` is a `manifests_deferred_files`
  entry, staged in `manifests_deferred_dir_name: flux-sync`. The role's own task runs
  `kubectl wait --for=condition=Established` on the four Flux CRDs it uses, then applies
  that directory. A rebuild therefore applies the CRDs and the objects in one run. A
  `--dry-run` never shows the deferred file to the API server, as with headlamp.
- **The first install's dry run checks only cluster-scoped objects.** Before `flux-system`
  exists, a `--dry-run` refuses every namespaced document with NotFound (*Baseline*): the
  Deployments, ServiceAccounts, Services and NetworkPolicies. It still shows the API server
  the CRDs, the ClusterRoles and the ClusterRoleBindings. The namespaced documents are
  checked before the namespace exists by `prek run --all-files`, whose
  `validate-k8s-manifests` hook validates the render against the vendored schemas.
  Rejected: landing the Namespace in an earlier PR. It would add a PR and a deploy for a
  check that the schema validation already makes.
- **The read-only identity reads the Flux kinds (D1.2b).** Every check in *Verification* that
  names a Flux kind runs as the read-only ServiceAccount, which reads no Flux group
  (*Baseline*). D1.2b appends `source.toolkit.fluxcd.io`, `kustomize.toolkit.fluxcd.io` and
  `notification.toolkit.fluxcd.io` to `k3s_readonly_crd_api_groups`.
    - `resources: ["*"]` on the notification group grants read on Provider, Alert and
      Receiver. On the source group it grants read on OCIRepository. These objects hold
      Secret references, never values. The `discord` Provider takes its address from the
      Secret `flux-discord` and leaves `spec.address` empty. The `github-status` Provider's
      address is the public repository URL.
    - The change is a `setup/k3s` change, so the tick records it as `manual_plane` and never
      applies it (*Baseline*). The operator applies it (**operator-only**), as H2 does for
      its `k3s-bringup.yml` applies, because the change widens the identity every agent
      session reads the cluster as.
    - The coverage test derives the required groups from the `spec.group` of every
      CustomResourceDefinition the flux role renders, so a later Flux group fails CI rather
      than reading Forbidden. Rejected: listing the three groups in the test, which would
      miss a group that a `flux_version` bump adds.
- **The verify drill's object has a way out.** While `flux_verify_drill` is true,
  `sync.yaml.j2` renders a second OCIRepository, `verify-drill`. Dropping it from the render
  deletes nothing (*Baseline*), so while the lever is false the role runs
  `k3s kubectl -n flux-system delete ocirepository verify-drill --ignore-not-found
  --timeout=60s`, under `when: not k8s_no_mutate`. It runs after the CRD wait and the
  deferred apply, because `--ignore-not-found` covers a missing object but not a missing
  kind. The timeout bounds the wait on source-controller's finalizer. This follows
  prowlarr's "Retire the flaresolverr config PVC". C3 names one OCIRepository, so the drill
  object is a transient exception that Open question 7 asks C3 to name, gated before D1.5
  merges.
- **Ansible owns the flux role until D5's last PR.** The role is an ordinary k8s role with no
  `owner: flux`. D5's final PR moves it to Flux, after every other service.
- **The root Kustomization is `homelab`, as C3's *Root* row names it.** Its path is
  `./flux`, which is where C1 puts each tag's Kustomization. It has `prune: true`,
  `wait: false` and `deletionPolicy: Orphan`, the policy C3's *Deletion policy* row gives
  every Kustomization the bundle publishes.
    - `wait: false`, because the root applies only Kustomization objects. Each one reports
      its own health and its own commit status. With `wait: true` the root would wait for
      every child, and its one timeout would have to cover the longest chain that D4 builds.
    - Orphan means that deleting the root itself never cascades. *Risks* covers a child that
      a bundle omits.
- **The status context is `flux/<name>`.** Provider `github-status` has type `github`, address
  `https://github.com/DanielH2018/server`, and `commitStatusExpr: "'flux/' +
  event.involvedObject.name"`. Alert `commit-status` sends it every Kustomization event with
  `eventSeverity: info`, which includes error events, so a failure reaches GitHub too.
- **Errors also go to Discord.** Provider `discord` reads the webhook from
  `gitops_deploy_discord_webhook`, the same channel the deployer posts to. Alert
  `deploy-errors` sends it events of severity `error` from two sources only: every
  Kustomization, and the OCIRepository named `homelab`. The `verify-drill` OCIRepository is
  not a source, so the signature drill posts nothing to Discord.
- **The artifact is signed and verified.** OCIRepository `homelab` sets `spec.verify` to
  cosign keyless. The identity must match the `promote` workflow on `refs/heads/master` with
  issuer `https://token.actions.githubusercontent.com`. An artifact that anything else
  pushed fails verification and is never applied.
- **`promote` picks its target from master, not from the run that triggered it.** GitHub
  keeps one pending job per concurrency group, and CI for an older commit can finish last.
  Take merges X2 and then X3, where CI(X3) finishes first. promote(X3) queues behind a
  running job, and promote(X2) then arrives and evicts it. A decision keyed on the triggering
  SHA would promote X2 and strand X3. So `promote.py decide` walks `origin/master` from its
  tip back to `prod`'s commit, and takes the first commit T that has all three:
    - a published `sha-<T>` tag;
    - a signature that `cosign verify` accepts under the identity the OCIRepository checks;
    - a green CI verdict, read with the semantics of `deploy_git.py:ci_verdict`.

  No such commit gives `current`, and nothing moves. A pending job is evicted only by a job
  that arrived later. So the last promote job to run started after the last publish whose
  job queued, and the newest published commit is always promoted by some run. This is C2's
  *Promotion target* row, which the integration review adopted (X-02).
- **`decide` owns the verdict set and hands T to every later step.** The verdicts are
  `promote`, `held`, `current` and `error`. A run whose trigger an earlier run already
  covered promotes a newer published commit or reports `current`, so no `superseded`
  verdict exists.
  `decide` prints one line, `trigger=<sha> verdict=<verdict> target=<sha or empty>`, and
  writes `verdict` and `target` to `$GITHUB_OUTPUT`. The tag step reads
  `steps.decide.outputs.target`. No step after `decide` reads
  `github.event.workflow_run.head_sha`, so D2's verify step checks statuses on T, the
  commit Flux applies and the one notification-controller posts on. The D1.4 workflow test
  holds this. D2's *Publication and verification* adopts the verify side as
  `promote.py verify T`, as C2's *Verification* row states.
- **Every reader names the applied commit by its origin revision.** `lastAppliedRevision`
  and the OCIRepository's `status.artifact.revision` both read `prod@sha256:<digest>`. A
  digest names no commit, and `git merge-base` cannot take one. So Exit 2 and D1.6's metric
  label read `lastAppliedOriginRevision`, and a reader of the OCIRepository reads its
  `org.opencontainers.image.revision` metadata key. Each strips `master@sha1:` before any git
  command. C6's *Which revision field names a commit* row states the same rule for every
  slice.
- **Each read has three outcomes, and an error promotes nothing.** `decide` takes every
  external fact as a value, as absent, or as an error. Any error ends the job non-zero, and no
  tag moves.
    - `DEPLOY_HOLD`: the value `none` is "no hold". Any other non-empty value is a hold,
      which `decide` prints, so a mistyped hold still holds. An empty value or a missing
      variable is an error. D1.4 creates the variable as `none`, because `gh` cannot set an
      empty value (*Baseline*) and an empty read looks the same as a broken one. C2's
      *`DEPLOY_HOLD`'s values* row states this rule, and C6's *`land.sh`* row ends its wait
      on a value other than `none` (Open question 9, resolved).
    - The required check-runs: `decide` reads
      `ansible/roles/setup/gitops_deploy/defaults/main.yml:gitops_deploy_ci_contexts` from its
      own checkout, as the workflow reads `flux_version`. The value must be a non-empty list
      of non-empty strings. An empty or missing list is an error, because `ci_verdict` reads
      an empty set as `pass`. Any other shape is an error too, a string included. Read as
      one context name, a string would leave every candidate `pending`, and promotion would
      stall with no error. `promote.py` names the source path once, as a literal string, so
      a `git grep` for `gitops_deploy/defaults/main.yml` or `gitops_deploy_ci_contexts`
      finds it.
    - The list's source moves at H7.2e. That PR moves the key to
      `ansible/inventory/group_vars/all.yml` under the name `ci_required_contexts`, and the
      role default becomes a Jinja reference to the new key until H7.19a. H7.20a then
      deletes the role defaults. A plain YAML read of the role default after H7.2e returns
      the reference as a string, which the shape rule above turns into an `error`. So
      H7.2e repoints `decide` to the new key in the same PR, and D1.3's checkout test
      fails that PR if it does not.
    - `prod`: `decide` fetches `prod`'s manifest from GHCR and reads its
      `org.opencontainers.image.revision` annotation. HTTP 404, or the registry error code
      `MANIFEST_UNKNOWN`, is absent. That is the first run, and it promotes the newest
      eligible commit. Any other status, an auth failure, a timeout, or a manifest without
      the annotation is an error.
    - Each candidate C's tag: one `HEAD` request for the manifest `sha-<C>`. 200 is present.
      404 is absent, which makes C ineligible, and the walk moves on. Any other status or a
      timeout is an error.
    - Ancestry and the master walk: `git merge-base --is-ancestor` exits 0 for yes and 1 for
      no. Any other exit is an error, such as 128 for an object a shallow checkout lacks.
    - A signature check or a CI read that fails is an error, not "ineligible". A Sigstore
      outage therefore stops promotion rather than skipping to an older commit. A CI
      verdict of `pending` or `fail` makes C ineligible. A required check-run that is
      missing reads as `pending`, as in `ci_verdict`, and is never green.
- **`decide` lists no tags, so its cost does not grow with them.** `promote.py` speaks the
  OCI distribution API to `ghcr.io` itself, with a bearer token that `GITHUB_TOKEN` obtains
  from GHCR's token endpoint, and a timeout on each request. A run makes one token request
  and one manifest fetch for `prod`. Then, for each candidate from the tip, it makes one
  `HEAD`, one check-runs read and one `cosign verify`, and it stops at the first eligible
  candidate. The candidates are the commits between master's tip and `prod`'s commit,
  usually one. Rejected: `flux list artifacts`, which fetches each tag's manifest and digest
  in sequence (*Baseline*). At about 124 merges a day, that is about 7,400 requests per run
  after 30 days, and one failed fetch among them stops promotion.
- **Old artifact versions are pruned daily (D1.7).** A workflow, `prune-artifacts.yml`,
  runs `promote.py prune` once a day. A signature is not a tagged version of its own: under
  cosign v3 it is an untagged bundle whose `subject` names the signed digest, plus, on a
  registry without the referrers API, an index tagged `sha256-<digest>` (*Baseline*). The
  selection reads the bundle's `subject` rather than its age, so it holds whichever of the
  two referrer paths GHCR takes. It deletes a package version only in three cases:
    - a version whose only tag is `sha-<C>`, older than 14 days, where C is a strict
      ancestor of `prod`'s commit;
    - an untagged version older than 14 days whose manifest has no `subject`, such as a
      `verify-drill` bundle that a later drill replaced;
    - a signature of a digest that is deleted under the first two cases or that the listing
      no longer holds: the untagged bundle whose `subject` is that digest, and the index
      tagged `sha256-<digest>`.

  Every other version stays: the ones tagged `prod`, `last-good` (from D2) or
  `verify-drill`, every walk candidate, because each candidate descends from `prod`'s
  commit, and every bundle whose `subject` is a surviving version, however old. A tag
  `sha256-<digest>` never parses as `sha-<C>`. To read each `subject`, `prune` fetches the
  manifest of every untagged version older than 14 days through the registry client from
  D1.3. When `prod` is absent, or any listing, manifest or ancestry read fails, it deletes
  nothing. After its deletes, it runs `cosign verify` on `prod`'s digest, and on
  `last-good`'s once D2 has created that tag, under the identity the OCIRepository checks.
  A failure there fails the run, so a wrong selection shows as a red run on the day it
  happens rather than as a failed rollback later. It uses `GITHUB_TOKEN` with `contents: read` and `packages: write`, and
  Open question 8 asks whether GHCR accepts that token for a delete. Its concurrency group
  is `prune`, not `promote`, because a job queued in `promote`'s group would evict a pending
  promote job.
- **Signatures are cosign v3 Sigstore bundles.** Both workflows install cosign v3.1.3 and
  keep its default format, the Sigstore bundle, which source-controller v1.9.6 verifies
  (*Baseline*). The version is pinned as `cosign-release: v3.1.3` on every
  `sigstore/cosign-installer` step, beside the installer's commit SHA, so a bump of either
  is a reviewed edit. Rejected: `--new-bundle-format=false`, which keeps the cosign v2
  tagged `.sig` layout that an age-based prune could read, because upstream marks that flag
  deprecated.
- **`promote` never moves `prod` backwards.** Every candidate in the walk descends from
  `prod`'s commit, and `decide` refuses a target that does not.
- **Jobs, ordering and permissions.** Both jobs run on `ubuntu-26.04`. `promote` has
  `needs: publish`, so a failed push or sign step stops the run before any tag moves. Each
  job declares its own `permissions`, and nothing else is granted:
    - `publish`: `contents: read`, `packages: write`, `id-token: write`.
    - `promote`: `contents: read`, `packages: write`, `checks: read`.

  `promote` checks out `github.sha` with `fetch-depth: 0`, because the walk needs master's
  history. Both jobs install the flux CLI at the version `flux_version` names, read from the
  role's defaults in their own checkout, so the CLI and the controllers move together.
  cosign comes from `sigstore/cosign-installer`, pinned by commit SHA, with
  `cosign-release: v3.1.3`.
- **Which token each call uses.**
    - `flux push artifact`, `flux tag artifact`, `cosign sign`, `cosign verify`, and
      `promote.py`'s registry reads and prune deletes use `GITHUB_TOKEN`, logged in to
      `ghcr.io`. GitHub Packages accepts no fine-grained token, so no other token could move
      the tag.
    - The check-runs read uses `GITHUB_TOKEN`.
    - `gh variable get DEPLOY_HOLD` uses the repo secret `PROMOTE_TOKEN`, a fine-grained
      token with Variables read and write, set as `GH_TOKEN` on that one step. The step reads
      the variable through the API at the moment it decides, not through the `vars` context,
      so a hold that an earlier job wrote is always seen. Writing `DEPLOY_HOLD` arrives in
      D2. Whether `GITHUB_TOKEN` can write repository variables is Open question 3; this plan
      assumes it cannot.
    - The SOPS key `flux_github_status_token` is a fine-grained token with Commit statuses
      read and write on this repository, for notification-controller.
- **`PROMOTE_TOKEN`'s expiry has a signal.** SOPS holds a copy as `promote_token`, registered
  `source: record` with tier `external`. The audit then turns "Secret Rotation" down within
  365 days of the copy's last change, and the operator mints the token with the longest
  expiry GitHub allows. Nothing in the tree reads the copy.
- **The trigger is guarded.** `promote.yml` triggers on `workflow_run` of `CI` with
  `types: [completed]`. Both jobs run only when the run's conclusion is `success`, its event
  is `push`, its branch is `master`, and its head repository is this repository. `publish`
  checks out `github.event.workflow_run.head_sha || github.sha`, because its bundle must be
  the triggering commit's tree. A dispatch has no `workflow_run` event, so it publishes
  master's tip, and D2's `sha` input extends the expression. `promote` checks out
  `github.sha`, the workflow's own commit, so the `promote.py`
  that runs always matches the `promote.yml` that calls it (failure class 7). Neither job
  checks out a pull-request ref. zizmor reports
  `workflow_run` as a dangerous trigger, so the trigger line carries the repository's
  `# zizmor: ignore[dangerous-triggers] -- <reason>` form, naming these guards.
- **A dispatch passes the same gate.** The workflow also takes `workflow_dispatch` inputs:
  `verify_drill` here, `sha` in D2 and `soak_drill` in D5. Every job reached by a dispatch
  requires `github.ref == 'refs/heads/master'`, so a branch's copy of the workflow never runs
  with these tokens. The `sha` path re-reads that commit's CI verdict through the check-runs
  API with the semantics of `deploy_git.py:ci_verdict`, where `cancelled` is no verdict, and
  refuses anything but green before it publishes or promotes. The two drill inputs never
  touch `prod`.
- **The way out exists from the first sync PR.** `flux_sync_suspend: true` renders
  `suspend: true` on the OCIRepository and the root Kustomization, which stops all
  reconciliation without deleting anything. With the lever off, the render omits
  `spec.suspend` from both objects rather than writing `suspend: false`. The role applies
  `flux-sync` with client-side `kubectl apply`, as the manifests cycle applies its own
  files (`ansible/roles/k8s/manifests/tasks/main.yml` runs `k3s kubectl apply` with no
  `--server-side`). That apply's three-way merge sets every field the render names, so an
  explicit `suspend: false` would undo an operator's hand suspend at the role's next run.
  A field the render omits keeps its live value, unless the last apply rendered it. D5's
  flux PR retires the lever: from that PR on, its renderer refuses
  `flux_sync_suspend: true`.
- **D1 adds no root-only suspend (H1 Open question 12, declined), and the bring-up parks the
  deployer.** H1's bring-up order suspends the root `homelab` alone while the restores run,
  and asks D1's role for a variable that does it. D5's *Bootstrapping Flux on a rebuilt
  cluster* owns the bring-up and DR order from D5's flux PR on. A root-only suspend or
  resume there is a hand patch under the field manager `homelab-operator`, which D5 names.
  A role variable would be a second suspend lever, which D5's flux PR would have to retire
  as it retires `flux_sync_suspend`. A hand suspend made after the role has created a
  running root races the root's first reconcile, which provisions every claim before the
  restores. So while D1's role still installs Flux, H1's order creates the root suspended
  with a role run at `flux_sync_suspend: true`, then resumes the OCIRepository alone with a
  `homelab-operator` patch. That sequence holds only while nothing applies the role again:
    - From D1.2 on, the flux role is a `containers_list` reader. `components.yaml.j2` calls
      `containers_list | tier_priority_class('flux')`, and
      `scripts/deploy_tools/narrow_lib/containers.py:renders_containers_list` matches that
      mention. When `containers.py:changed_fields` returns None, `containers.py:reader_paths`
      keeps the role's templates, and the broad plane applies `flux` whatever
      `k8s_autodeploy` says (*Baseline*).
    - The root's suspend from that run lives in the role's last-applied annotation, because
      the render named it. A hand patch survives the role's runs only on a field that the
      last apply never rendered, so this suspend is not one.
    - Any later apply of the role with the lever off removes that suspend. The three-way
      merge deletes a field that the last-applied annotation holds and the new render
      omits. That includes an unattended tick that reaches `flux` as a reader, after another
      session lands an entry change for which `changed_fields` returns None, such as an
      added, removed or reordered Ansible-owned entry. The root then
      provisions fresh claims under the backed-up names before the restores. A tick that
      reaches `flux` before the role's first run creates the root running, with the same
      result.

  So H1's order parks the GitOps deployer, **operator-only**, from before its step 1 until
  its step 12. Stopping the timer alone does not park it:
  `scripts/deploy_tools/gitops_tick.sh` starts `gitops-deploy.service` directly, and
  `land.sh` kicks a tick through it (`scripts/deploy_tools/land_lib/tick.py:kick_tick`).
  The park masks the service for the running boot, stops the timer, and then waits until
  `systemctl show gitops-deploy.service -p ActiveState --value` no longer reads
  `activating`:

  ```bash
  sudo systemctl mask --runtime gitops-deploy.service
  sudo systemctl stop gitops-deploy.timer
  ```

    - The mask is `--runtime` because the role installs the unit file in
      `/etc/systemd/system` (`ansible/roles/setup/gitops_deploy/tasks/service.yml`), and
      `systemctl mask` refuses a unit whose file is already there. A reboot clears a runtime
      mask, and the timer's `OnBootSec=10min` then fires. After a reboot of daniel-box inside
      the window, the operator parks again before that.
    - While the park holds, `systemctl is-enabled gitops-deploy.service` reads
      `masked-runtime`. `last_run` goes stale, so GitOps-Alive reads down, and a `land.sh`
      in another session cannot start a tick.
    - The unpark at step 12 is `sudo systemctl unmask --runtime gitops-deploy.service`, then
      `sudo systemctl start gitops-deploy.timer`.
    - The park also stops the tick from applying an Ansible-owned stateful role before the
      restores, such as one whose image-pin bump the tick deploys on its own.

  Rejected: writing the root's suspend under `homelab-operator` from the lever-on run. It
  protects the root only, and a role that creates the root running before the patch
  reopens the race above. D1 owns the facts in this bullet. A cross-slice note hands H1 the
  step, and D5 the same step for its citation of H1's order before the flux PR.

## Steps, as PRs

No PR in D1 moves ownership of a live object, so the two-writer rule (C4) has nothing to
check. Flux owns no object outside `flux-system` until D2's flip PR.

0. **Operator ruling (no PR).** ADR-0010 deferred Flux. Before D1.1 merges, the operator
   approves starting D1 (**operator-only**). D1.1 records the ruling as an ADR in `Proposed`
   status, and D6 asks the operator to accept it.

1. **D1.1 — Flux schemas and the proposal** (about 30 hand-written lines, the vendored
   schemas, and an ADR of about 50 lines).
    - Add `apis__apiextensions.k8s.io__v1_openapi.json` to `CORE_GROUP_FILES`. Add four
      kinds to `VENDORED`: OCIRepository (source.toolkit.fluxcd.io), Kustomization
      (kustomize.toolkit.fluxcd.io), Provider and Alert (notification.toolkit.fluxcd.io).
      Run `uv run python scripts/validate/refresh_vendored_schemas.py` and commit what it
      writes. `VENDORED`'s own comment says a kind with no manifest is harmless.
    - Add the ADR at the next free number, `status: Proposed`. It states that D1 installs
      Flux owning nothing, and that ADR-0010 is superseded only when the operator accepts it.

2. **D1.2 — The flux role and its controllers** (about 240 hand-written lines, plus the
   vendored file). This is above the 200-line guide, because each guard's patch or exemption
   must land with the templates it covers.
    - Write the role by hand, with the include, the waits, `flux_controllers` with
      `restart_on: []` on each entry, and `flux_pod_patch` from *Decisions*. Append the
      entry `flux` with `platform: k8s` and `tier: platform`, and add "the deploy controller"
      to the `platform` list in the comment above the entries.
    - Add `files/gotk-components.yaml` and `templates/components.yaml.j2`, and set
      `manifests_prune_namespace: flux-system` at the include. `components.yaml.j2` takes
      the class from `tier_priority_class('flux')` and rewrites the volume lists.
    - Set `k8s_autodeploy: false` with the reason "platform: the deploy controller itself".
      A bump of `flux_version` must be regenerated by hand.
    - Annotate `flux_version` with `# renovate: datasource=github-releases
      depName=fluxcd/flux2`. Add the role's defaults path to the `manual — k8s_autodeploy:
      false` rule. Add a per-package rule for `fluxcd/flux2` with `automerge: false` and the
      marker, because the docker-only rule never sees a github-releases pin.
    - The vendored images stay tag-pinned. Each tag is the controller release that
      `flux_version` names, and the fleet digest rule reads `*_image` defaults only. The
      role's `CLAUDE.md` records this exemption and its reason.
    - Add the `_RECREATE` row for source-controller. Add `"flux": "homelab-critical"` to
      `_CLASS_AT_DERIVATION` in `ansible/tests/k8s/test_priority_class_is_derived_from_tier.py`,
      and `flux` to `_EXPECTED_NARROWERS` in
      `ansible/tests/deploy/test_restart_on_narrows_the_restart_signals.py`.
    - Add a test in `ansible/tests/k8s/` that the vendored file's header names
      `flux_version`, with a fixture of a mismatched header as its red half.
    - Add a self-rollout test in `ansible/tests/k8s/`, over the harness in
      `ansible/tests/_release_expectation.py`. A changed secret render with an unchanged
      apply expects none of the three controllers to roll. Its red half is the same render
      with `restart_on` removed from each entry, which expects all three.
    - Create `docs/flux-deploy-plane.md` with its `mkdocs.yml` nav entry. Add a dated line
      to ADR-0010's *Status* section that links the proposal and says Flux runs owning
      nothing. The line annotates the status and rewrites no decision.
    - Before the PR, check `gh release list -R fluxcd/flux2`. If a release lists Kubernetes
      1.37, pin it instead (Open question 1).
    - **Gate before merge:** the operator ruling that Open question 10 requests. Without
      it, D1.2 does not merge.
    - Before the PR, run `uv run python scripts/deploy_tools/deploy_tags.py narrow
      origin/master HEAD` and record the tag list in the PR body. Expect `flux` and the 23
      readers under *Baseline*, 24 of 60. Every tag in it applies unattended and
      forward-only at the landing. A list naming only `flux` contradicts
      `containers.py:changed_fields`, so the session finds out why before going on.
    - **Stop rule, before the PR and again right before `land.sh`.** Run `uv run python
      scripts/diagnostics/probe.py releases --stale-only` and `uv run python
      scripts/diagnostics/probe.py releases`. Read the service names, not the exit code.
      Do not merge while any service in the narrow output is named by the first command or
      flagged `unmerged` or `dirty` by the second. Each such service carries a change the
      tick would apply or revert as a side effect. If the change is this session's own,
      deploy it first and run the check again. If it is another session's, name the service
      and the session, and wait until its owner lands it. The PR body quotes both runs.
    - Before the PR, check the render. `prek run --all-files` runs
      `scripts/validate/k8s_manifests.py`, which validates every rendered document against
      the vendored schemas, the namespaced ones included. `./scripts/deploy.sh --tags flux
      --dry-run` shows the CRDs and the cluster RBAC to the API server, and refuses every
      namespaced document with NotFound because `flux-system` does not exist yet
      (*Decisions*). The PR body quotes that run and confirms that every refusal is a
      namespaced document in `flux-system`.
    - **Landing.** `land.sh` merges and waits on the tick. The tick reads the inventory edit
      as a deploy-plane change. Adding an entry makes `changed_fields` return None, so the
      tick applies `deploy.yml` narrowed to `flux` plus every role that renders
      `containers_list`: the 24 tags of the pre-PR narrow output, traefik, authelia,
      pihole and netpol-baseline among them. `k8s_autodeploy: false` does not stop that
      plane. The apply is forward-only, so a failure writes `hold_sha` and a `hold_plane`
      entry for the whole list (*Rollback*).
    - **If the narrowing refuses.** The tick applies nothing and `land.sh` reports
      `needs-manual-apply`. A fleet-coverage refusal records a `k8s_unapplied` line for each
      tag it reached, `flux` and every reader. The landing session runs the stop rule again,
      then `./scripts/deploy.sh --tags` with that list, from a checkout at `origin/master`,
      because `deploy.sh` renders the tree it runs in. The successful run discharges each
      line, and `probe.py releases --stale-only` then names none of those services. Any
      other refusal records a line for every declared service, which means the deployer's
      derivation disagreed with the pre-PR run. The session then stops and quotes the
      tick's `narrow: cannot narrow` journal line. The operator deploys the pre-PR list or
      rules otherwise, and clears the lines of services outside it with `gitops_state.py
      clear-owed k8s_unapplied <svc>` (**operator-only**).

3. **D1.2b — The read-only identity reads the Flux kinds** (about 10 lines of defaults and
   about 25 of test). It lands after D1.2, whose CRDs the test reads, and its apply must be
   done before D1.5 lands.
    - Append `source.toolkit.fluxcd.io`, `kustomize.toolkit.fluxcd.io` and
      `notification.toolkit.fluxcd.io` to
      `ansible/roles/setup/k3s/defaults/main.yml:k3s_readonly_crd_api_groups`. The comment
      above them follows the `k3s.cattle.io` paragraph's form: it names the deploy
      controller's kinds as the reason, and says that `resources: ["*"]` grants read on
      Provider, Alert, Receiver and OCIRepository, which hold Secret references, not values.
    - Extend
      `ansible/tests/k8s/test_k8s_manifests_rbac.py:test_readonly_role_covers_the_crd_groups_this_homelab_deploys`.
      It collects `spec.group` from every CustomResourceDefinition document that
      `ansible/tests/_k8s_render.py:rendered_docs` yields for role `flux`, asserts that the
      set contains `kustomize.toolkit.fluxcd.io`, so an empty derivation cannot pass, and
      asserts that every collected group is in `k3s_readonly_crd_api_groups`. Its red half
      runs the same check against a copy of the list with `source.toolkit.fluxcd.io`
      dropped and expects that group named as unreadable.
    - Before the PR, run `kubectl get kustomizations.kustomize.toolkit.fluxcd.io -A` and
      record in the PR body that it fails with Forbidden.
    - **Landing.** `land.sh` merges and waits on the tick, which fast-forwards the change
      and records `k3s` as a `manual_plane` line narrowed to `kubeconfig` (*Baseline*).
      `land.sh` then reads `needs-manual-apply`. **Operator-only:** run
      `uv run ansible-playbook ansible/k3s-bringup.yml --tags kubeconfig`, then
      `uv run python scripts/deploy_tools/gitops_state.py clear-owed manual_plane k3s
      --applied kubeconfig`. *Decisions* says why the operator applies it.
    - **Done when** the same `kubectl get` prints `No resources found` instead of Forbidden.
      D1.5 does not merge before that.

4. **D1.3 — The promotion decision** (about 220 lines, plus about 250 of tests). This is
   above the guide, because the registry client and its outcome mapping land with the
   decision that reads them.
    - **Contract gate, satisfied:** C2's *`DEPLOY_HOLD`'s values* row and C6's *`land.sh`*
      row state the `none` rule that `decide` implements (Open question 9, resolved).
    - `scripts/deploy_tools/promote.py` holds a pure function, `decide`. It takes the hold
      read, the `prod` read, the master walk and each candidate's tag, signature and CI reads,
      each as a value, absent or error. It returns `promote` with the target T, `held`,
      `current` or `error`.
    - A thin CLI, `promote.py decide`, makes the reads: `gh variable get` for `DEPLOY_HOLD`,
      `gitops_deploy_ci_contexts` from the checkout through the one literal path under
      *Decisions*, one manifest fetch for `prod` and one
      `HEAD` per candidate `sha-<C>` through its own registry client, `git rev-list` and
      `git merge-base --is-ancestor` for the walk, `cosign verify` for each signature, and
      the check-runs API for each verdict. It maps each response or exit to the three
      outcomes under *Decisions*. It prints the one line under *Decisions*, writes `verdict`
      and `target` to `$GITHUB_OUTPUT`, and exits non-zero on `error`.
    - Its tests go in `scripts/deploy_tools/tests/` as `_is_promoted` and `_is_refused`
      pairs, with the registry client behind a fake transport.
        - Promoted: `DEPLOY_HOLD` set to `none`; a `prod` manifest fetch that returns 404,
          the first run; the out-of-order case, where the trigger is X2, the walk finds X3,
          and both the printed line and the `target` output name X3; a tip whose `sha-<C>`
          `HEAD` returns 404, which is skipped for the next candidate.
        - Refused: a hold of any other value, such as a SHA or `hold-me`; an empty
          `DEPLOY_HOLD`; a missing `DEPLOY_HOLD`; a hold-read error; a `prod` fetch that
          returns 500; a candidate `HEAD` that returns 500, which is an `error` rather than
          a skip; an empty `gitops_deploy_ci_contexts`, which is an `error`; a
          `gitops_deploy_ci_contexts` whose value is the string
          `"{{ ci_required_contexts }}"`, which is an `error` and not one context name; a candidate
          whose check-runs lack a required name, which reads `pending` and is never
          promoted; an ancestry exit of 128; a non-descendant; an equal SHA; a trigger older
          than `prod`, which reports `current` with an empty target; and a signature-check
          error.
    - A checkout test runs the CLI's list read against the repository itself and asserts a
      non-empty list of strings whose named member is `prek (lint + validate + tests +
      secrets)`, the one context the role default lists at `70591c307`. A move of the
      key that leaves `decide` reading a Jinja reference or a deleted file fails it in CI,
      not at the next promote run. Its red half is the string fixture above.
    - A request-count test runs `decide` against the fake transport with 3,000 `sha-*` tags
      present and asserts the requests it made: one token request, one `prod` fetch, and
      one `HEAD` per candidate examined. Its red half counts a client that lists tags.

5. **D1.4 — The `promote` workflow** (about 140 lines).
    - **Operator-only before merge:** create the `PROMOTE_TOKEN` repository secret, and
      create the repository variable `DEPLOY_HOLD` with the value `none`:
      `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`. That form needs no
      prompt and no standard input. Store the token's copy with `sops set` as
      `promote_token`. The agent then runs `uv run python
      scripts/secrets_mgmt/secret_rotation.py sync`, sets that row to `source: record` and
      tier `external`, and commits `ansible/secret_rotation.yml`.
    - Add `.github/workflows/promote.yml` with the guards, jobs and permissions above.
      `publish` builds the empty bundle, pushes `sha-X` and signs it. `promote` runs
      `promote.py decide` and tags `prod`. A `workflow_dispatch` input, `verify_drill`,
      pushes an unsigned bundle to the tag `verify-drill` and skips the `promote` job.
    - Add a test in `ansible/tests/repo/` that parses the workflow and asserts:
        - the four trigger guards and the master-ref guard on every job a dispatch reaches;
        - the job-level `concurrency: promote` with `cancel-in-progress: false`;
        - `needs: publish` on `promote`, and `fetch-depth: 0` on its checkout;
        - that `publish` checks out `github.event.workflow_run.head_sha || github.sha` and
          `promote` checks out `github.sha`;
        - each job's permission map exactly as listed under *Decisions*;
        - that the flux CLI version comes from `flux_version`;
        - that every `sigstore/cosign-installer` step sets `cosign-release: v3.1.3`;
        - that the tag step takes its SHA from the `decide` step's `target` output, and that
          no step after `decide` reads `github.event.workflow_run.head_sha`;
        - no `pull_request` trigger.

      Its red halves are fixtures: one with a trigger guard removed, one with a
      dispatch-reachable job lacking the ref check, one with an extra permission, one
      without `needs`, one whose tag step reads `head_sha`, one whose `promote` job
      checks out `head_sha`, and one whose cosign installer step omits `cosign-release`.
    - **Operator-only after the first run:** set the `homelab-manifests` package to public,
      matching the texbrain package and ADR-0018 (Open question 2). D1.5 does not merge
      before this, and its *Gates before merge* checks it.

6. **D1.5 — Flux follows `prod`** (about 120 lines).
    - **Gates before merge:** D1.2b's apply is done, so the read-only identity can read the
      objects this PR creates, and the C3 amendment that Open question 7 requests has
      landed. The third gate is that Flux can pull `prod` with no credentials, because
      OCIRepository `homelab` carries no pull secret (Open question 2). This pipeline asks
      GHCR for an anonymous pull token, then sends a manifest `HEAD` for `prod`, and must
      print `200`:

      ```bash
      curl -s 'https://ghcr.io/token?scope=repository:danielh2018/homelab-manifests:pull' \
        | jq -r '"Authorization: Bearer " + .token' \
        | curl -s -o /dev/null -w '%{http_code}\n' -I -H @- \
            -H 'Accept: application/vnd.oci.image.manifest.v1+json' \
            https://ghcr.io/v2/danielh2018/homelab-manifests/manifests/prod
      ```

      Any other output refuses. A package that is still private prints `401` or `403`, and
      a `prod` tag that no promote run has written yet prints `404`. The PR body quotes the
      `200`.
    - **Operator-only before merge:** create the fine-grained status token and store it with
      `sops set` as `flux_github_status_token`. The agent then runs `uv run python
      scripts/secrets_mgmt/secret_rotation.py sync` and commits `ansible/secret_rotation.yml`.
    - Add `templates/sync.yaml.j2` as a deferred file, the CRD wait, the deferred apply, and
      `templates/secret.yaml.j2`, holding the objects from *Decisions*. `sync.yaml.j2`
      renders the `verify-drill` OCIRepository only while `flux_verify_drill` is true, and
      the `deploy-errors` Alert names only Kustomizations and the OCIRepository `homelab` as
      event sources. It renders `spec.suspend` on `homelab` and the root only while
      `flux_sync_suspend` is true, and omits the field otherwise (*Decisions*). A render
      test pins both halves: the lever on renders `suspend: true` on both objects, and the
      lever off renders no `suspend` key on either. Its red fixture renders
      `suspend: false`.
    - Add the task that deletes `ocirepository/verify-drill` while the lever is false, from
      *Decisions*.
    - Add `"flux-sync": "flux"` to `KNOWN_DEFERRED_DIRS` in
      `ansible/tests/k8s/test_deferred_manifest_dir_is_reserved.py`.
    - The self-rollout test from D1.2 gains a case over this PR's render: the new
      `secret.yaml.j2` changes the secret render, and no controller is expected to roll.
    - Land with `land.sh --tags flux`. Within two minutes the OCIRepository resolves `prod`.
      `probe.py health flux` then passes with no NOT ROLLED line.

7. **D1.6 — Flux in the metrics plane** (about 60 lines).
    - Add a kube-state-metrics custom-resource-state config for the Flux kinds to
      `ansible/roles/k8s/observability/`, with the read-only RBAC it needs. That config
      produces `gotk_resource_info`, the series C6 names. For a Kustomization, its `revision`
      label reads `status.lastAppliedOriginRevision`, so the label carries `master@sha1:<sha>`
      rather than the OCI tag and digest. The controllers do not export this series
      themselves.
    - The config travels as a container argument, not a ConfigMap.
      `kube-state-metrics.yaml.j2` passes `--custom-resource-state-config` with the config
      rendered from a defaults variable through `to_json`. The config then lives in the pod
      template, so a change to it rolls kube-state-metrics through the apply, which suits
      its `restart_on: []` entry. No ConfigMap is added, so the key-count table in
      `ansible/tests/services/test_observability_config_rolls_one_workload.py` stays as it
      is. Rejected: a ConfigMap with a `checksum/config` annotation, which adds a key-count
      row and a second roll path for the same result.
    - The ClusterRole gains `list` and `watch` on the two Flux kinds the config reads, and
      on `customresourcedefinitions`. The comment above it, which requires the RBAC and
      the collector's arguments to change together, gains the new argument.
    - Add a hand-written scrape job for the controllers' `http-prom` port in
      `templates/prometheus.yaml.j2`. `ansible/filter_plugins/scrape_jobs.py` says a job
      needing pod discovery stays hand-written.
    - **Landing.** observability is `k8s_autodeploy: false`, so the deployer fast-forwards
      this change and applies nothing. Land with `land.sh --tags observability`, then run
      `uv run python scripts/diagnostics/probe.py health observability` and the revision
      query under *Verification*.

8. **D1.7 — Retention for old artifact versions** (about 140 lines, plus about 140 of tests).
   Land it within 14 days of D1.4, before the first version is old enough to prune.
    - Add `promote.py prune`. A pure function selects the versions under *Decisions* from
      the package listing, `prod`'s commit, an ancestry answer per `sha-<C>` tag, and the
      `subject` of each untagged version older than 14 days, each read as a value, absent or
      error. The CLI lists versions through the GitHub Packages API, reads `prod`'s commit
      and each untagged manifest through the registry client from D1.3, and deletes the
      selected versions one by one. It then runs `cosign verify` on `prod`'s digest, and on
      `last-good`'s when that tag exists, and exits non-zero if either fails.
    - Add `.github/workflows/prune-artifacts.yml`, triggered by a daily `schedule` and by
      `workflow_dispatch`. Its one job runs on `ubuntu-26.04` with `concurrency: prune`,
      permissions `contents: read` and `packages: write`, a check of
      `github.ref == 'refs/heads/master'`, a checkout of `github.sha` with
      `fetch-depth: 0`, and cosign from the same pinned installer step as `promote.yml`.
    - Tests in `scripts/deploy_tools/tests/`, as `_is_pruned` and `_is_kept` pairs.
        - Pruned: a `sha-<C>` version past 14 days whose C is a strict ancestor of `prod`'s
          commit; an untagged version past 14 days with no `subject`; the untagged bundle
          and the `sha256-<digest>` index of a digest pruned in the same run; and a bundle
          30 days old whose `subject` the listing no longer holds.
        - Kept: a version tagged `prod`; one tagged `last-good`; one tagged `verify-drill`;
          a `sha-<C>` version whose C descends from `prod`'s commit, however old; a version
          13 days old; an untagged bundle 30 days old whose `subject` is `prod`'s digest; one
          whose `subject` is `last-good`'s digest; a `sha256-<digest>` index of `prod`'s
          digest, which never parses as a `sha-<C>` tag; and every version when `prod` is
          absent, the listing fails or one `subject` read fails.
    - The D1.4 workflow test gains the prune workflow: the master-ref check, the exact
      permission map, the `prune` concurrency group, the pinned `cosign-release`, and a red
      fixture that puts the job in the `promote` group.

## Verification

Run these after D1.5 and D1.6 have landed, and after a docs-only commit X has merged. Read T
from Exit 2 first, because Exit 1 and the revision label take it. Exits 2 and 3 and the
verify-drill check run `kubectl` on Flux kinds as the read-only identity, so each is gated on
D1.2b's apply. Before that apply, each fails with Forbidden.

```bash
# Exit 4: the three controllers are Ready, and their memory, beside 3 x 64Mi of requests.
kubectl -n flux-system get deploy
kubectl top pod -n flux-system
uv run python scripts/diagnostics/probe.py health flux

# Exit 2: the root applied T. Prints master@sha1:<T>.
kubectl -n flux-system get kustomizations.kustomize.toolkit.fluxcd.io homelab \
  -o jsonpath='{.status.lastAppliedOriginRevision}'
# T is X or descends from it. Exits 0 once a promote run has covered X, and 1 before.
git fetch origin master && git merge-base --is-ancestor <X> <T>

# Exit 1: the status notification-controller posted on T. Prints "success".
gh api repos/DanielH2018/server/commits/<T>/statuses \
  --jq '.[] | select(.context == "flux/homelab") | .state'

# Exit 3: the root is the only Kustomization, and its inventory is present and empty.
# The first prints one name. The second prints exactly {"entries":[]}.
kubectl get kustomizations.kustomize.toolkit.fluxcd.io -A -o name
kubectl -n flux-system get kustomizations.kustomize.toolkit.fluxcd.io homelab \
  -o jsonpath='{.status.inventory}'

# C6's revision label carries T. Prints exactly one series, with value 1.
uv run python scripts/diagnostics/probe.py metric \
  'gotk_resource_info{customresource_kind="Kustomization",name="homelab",revision="master@sha1:<T>"}'
```

Exit 3 reads the Kustomization's inventory, the record Flux prunes from, rather than a
label query over chosen kinds. The read is positive: the field must be present and hold an
empty list (*Baseline*), so an absent field or a misspelt path cannot pass as "owns nothing".
If the fallback ConfigMap under *Risks* is in use, the expected output is instead one entry,
`{"entries":[{"id":"flux-system_homelab-root__ConfigMap","v":"v1"}]}`, and any other output
fails. Exit 2 shows that the same object was read and reconciled.

**Each new check can go red.**

- *Exit 1.* The same `gh api` call on the merge commit of D1.4, which predates the sync
  objects, prints nothing. An empty status list is pending and never success, which is
  failure class 3, so the verification must print `success` and not merely exit 0.
- *The read-only identity.* Before D1.2b's apply,
  `kubectl get kustomizations.kustomize.toolkit.fluxcd.io -A` fails with Forbidden, and
  D1.2b's PR body records that. After the apply it prints `No resources found`. The D1.2b
  coverage test's red half, the group list with `source.toolkit.fluxcd.io` dropped, names
  that group as unreadable.
- *Exit 2.* Before D1.2 lands, the kind does not exist. Between D1.2's landing and D1.2b's
  apply, the command fails with Forbidden. After the apply and before D1.5 lands, it fails
  with `NotFound`. None of the three prints a revision, so each refuses. After D1.5 lands,
  `jsonpath='{.status.lastAppliedRevision}'` on the same object prints
  `prod@sha256:<digest>`, which is why Exit 2 reads the origin field. `git merge-base
  --is-ancestor <X> <X>~1` exits 1, so the ancestry check refuses a T older than X.
- *Exit 3.* The same command with a misspelt path, `-o jsonpath='{.status.inventroy}'`,
  prints the empty string, which is not the expected output, so the check refuses it.
  After D1.2b's apply and before D1.5 lands, the command fails with `NotFound`, and before
  that apply it fails with Forbidden. An inventory that names an object
  outside `flux-system` prints a non-empty `entries` list, which the check also refuses.
- *The revision label.* The same query for the commit before T returns no series. The check
  passes only on one series, never on an empty result.
- *`promote.py decide`.* The `_is_refused` tests in D1.3 cover every read error, the hold,
  the empty and missing `DEPLOY_HOLD`, the empty required set, the missing check-run, the
  non-descendant, the equal SHA and the trigger older than `prod`. The out-of-order
  `_is_promoted` test fails if the decision, the printed line or the `target` output keys
  on the triggering SHA. The request-count test fails on a client that lists tags. The
  string-shape `_is_refused` test fails if `decide` reads a Jinja reference as one context
  name. The checkout test fails if the real list stops resolving to a list that names
  `prek (lint + validate + tests + secrets)`, which is how a move of the key without the
  repoint shows in CI.
- *The suspend render.* The D1.5 render test fails if the lever off renders any `suspend`
  key on `homelab` or the root, and its `suspend: false` fixture is the red half. The lever
  on must render `suspend: true` on both.
- *`promote.py prune`.* The `_is_kept` tests in D1.7 fail if the selection ever takes a
  version tagged `prod`, `last-good` or `verify-drill`, a walk candidate, the untagged
  bundle 30 days old whose `subject` is `prod`'s or `last-good`'s digest, or the
  `sha256-<digest>` index of `prod`'s digest. Live, each prune run ends with `cosign verify`
  on `prod`'s digest, so a selection that took `prod`'s bundle fails that run.
- *The self rollouts.* The D1.2 self-rollout test's red half, the same render without
  `restart_on`, expects all three controllers to roll. At D1.5's landing,
  `probe.py health flux` prints no NOT ROLLED line.
- *The guard patches.* The D1.2 PR body quotes `uv run pytest ansible/tests/k8s` failing with
  `flux_pod_patch` emptied and the volume rewrite removed, naming each guard under
  *Baseline*, `test_volume_names_name_their_workload` included, and then passing with both.
- *The hold stops promotion live.* **Operator-only:** set `DEPLOY_HOLD` to any SHA and merge
  a docs-only commit. The `promote` job must log `held`, and `prod` must not move. Then set
  the variable back to `none` with `gh variable set DEPLOY_HOLD --body none -R
  DanielH2018/server`.
- *The signature check.* `promote.yml` takes a `workflow_dispatch` input, `verify_drill`,
  that pushes the bundle to the tag `verify-drill` without signing it and never touches
  `prod`. The flux role's lever `flux_verify_drill: true` renders a second OCIRepository,
  `verify-drill`, which follows that tag with the same `spec.verify` and which no
  Kustomization reads. Its Ready condition must be `False` with a verification error. No
  Discord post follows, because `deploy-errors` does not name `verify-drill` as a source.
  Set the lever back to `false` in the same session and land that PR with
  `land.sh --tags flux`. The role's delete task then removes the object, and
  `kubectl -n flux-system get ocirepositories.source.toolkit.fluxcd.io verify-drill` must
  fail with `NotFound`. Before the delete, the same command printed the object, so the
  check can go red. The check needs D1.2b's apply: without it the command fails with
  Forbidden, which proves no deletion.
- *The workflow guard test.* Each of its seven fixtures fails, and so does D1.7's
  prune-workflow fixture.
- *The D1.2 stop rule.* It refuses on a name, never on an exit code. A reader whose role
  moved on `origin/master` past its applied commit appears in the `--stale-only` lines, and
  one running a worktree's commit reads `unmerged` in the plain table. Either refuses the
  merge. The narrow output's expected size is measured, 24 tags, so a list naming only
  `flux` also stops the session.
- *After D1.2's landing.* `probe.py releases --stale-only` names none of the 24 tags, and
  `hold_sha` is empty. A failed apply leaves `hold_sha` set, and a refused narrowing leaves
  each owed service named as stale, so both checks can go red.
- *The D1.5 package gate.* The pipeline must print `200`. Before the operator makes the
  package public it prints `401` or `403`, and before the first promote run it prints `404`.

## Rollback

- **D1.1.** Reverting removes the schemas and the proposal. Nothing reads either yet.
- **D1.2.** Reverting removes the role and its `containers_list` entry. Removing an entry
  reconciles nothing (`scripts/deploy_tools/narrow_lib/containers.py:entry_change_tags`
  logs it as an orphan), so the controllers keep running. Deleting the `flux-system`
  namespace and the Flux CRDs is a write verb (**operator-only**).
- **A failed D1.2 apply.** The tick writes `hold_sha` and a `hold_plane` entry, which park
  every landing behind it. The entry names `ansible/deploy.yml` with all 24 tags, not `flux`
  alone. The role that failed may be a reader, such as traefik, or netpol-baseline with its
  probe Job, so the fix may not touch the flux role at all. Find the failing role in the
  tick's journal and fix the cause forward with a PR. Then apply the whole held list by hand
  with `./scripts/deploy.sh --tags` and the 24 tags, from a checkout at `origin/master` that
  includes the fix. `deploy.sh` renders the tree it runs in, so a checkout behind master
  reverts other sessions' changes on those readers. A hand `deploy.sh` clears no hold
  (*Baseline*), so the operator then runs `gitops_state.py clear-hold <sha>`
  (**operator-only**). `ansible/roles/setup/gitops_deploy/CLAUDE.md`'s *Which apply clears a
  hold* has the coverage rule.
- **D1.2b.** Reverting records `k3s` as a `manual_plane` line again, and the operator runs
  the same pair, `k3s-bringup.yml --tags kubeconfig` and the clear (**operator-only**).
  The task applies the whole ClusterRole with `kubectl apply`, and a ClusterRole's `rules`
  list has no merge key, so the reapply replaces the list and drops the three groups. Exits 2
  and 3 and the drill check then read Forbidden again.
- **D1.3 and D1.4.** Reverting stops publication. Tags already in GHCR stay, which is
  harmless because Flux does not read them until D1.5.
- **D1.5.** The way out is a PR that sets `flux_sync_suspend: true`, landed with `land.sh`.
  Reverting D1.5 instead leaves the OCIRepository and the Kustomization live, because Flux
  kinds are not in `manifests_prune_allowlist` and the deferred directory is not pruned.
  The lever lasts until D5's flux PR, whose renderer refuses it. From then on, stopping
  everything is the hand suspend in D5's recovery runbook.
- **D1.6.** Reverting removes the config and the scrape job. Nothing depends on them until D6.
- **D1.7.** Reverting stops pruning, and old versions accumulate. That costs storage only,
  because `decide` lists no tags. A version that a prune run deleted is gone. Only a version
  older than 14 days that no tag or walk can reach is ever deleted, together with the
  signature of a deleted digest, and never the bundle or `sha256-<digest>` index of a
  surviving version.
- **The verify drill.** A drill left on leaves `verify-drill` Ready=False, with no Discord
  post. The way out is the lever set back to `false`, whose landing deletes the object.
- **A partial failure leaves nothing at risk.** If D1.5's apply stops after the Secret, Flux
  has controllers and no source, and reconciles nothing. Every service is still Ansible's.

## Risks

- **Flux 2.9 does not list Kubernetes 1.37.** Mitigation: D1 runs Flux for a week before D2,
  watching the controllers' restarts and logs. Open question 1 asks the operator to accept
  the gap or wait.
- **The first install applies 24 services unattended.** Adding the `flux` entry makes the
  tick apply `flux` and all 23 roles that render `containers_list`, forward-only
  (*Baseline*). Among them are traefik, authelia and pihole, which declare
  `k8s_autodeploy: false`, and netpol-baseline. Three things can go wrong. A reader can
  carry another session's merged, undeployed change, which then ships as a side effect. A
  reader can run a worktree's unmerged commit, which the apply from master reverts. And any
  one failure holds every landing until the operator clears it. Mitigation: the D1.2 stop
  rule reads the release records for every tag in the narrow output before the PR and again
  right before `land.sh`. Open question 10 asks the operator to accept the apply before D1.2
  merges, and *Rollback* names the way out of a hold. Residual risk: a change another
  session merges between the second check and the tick is applied too. Rejected for D1:
  scoping an added entry to the fields it sets. The docstring of
  `containers.py:changed_fields` says an added entry "moves every reader that iterates the
  list, whatever it reads". D2.1c makes that deployer change for entries that are not
  Ansible-owned (X2-06). It would not cover D1.2 either way: the `flux` entry is
  Ansible-owned until D5's flux PR, and D1.2 merges before D2 exists.
- **Later slices that add or remove an entry inherit the same reach, and `flux` joins it.**
  From D1.2 on, the flux role is itself a reader (*Decisions*). So until D5's flux PR, any
  entry change for which `changed_fields` returns None applies the entry's tag, the 23
  measured readers and `flux`, unattended and forward-only. That flux PR gives the entry
  `owner: flux`, and D2.2's drop then removes `flux` from the deployer's plane. Reaching
  `flux` runs the role's CRD `Established` wait and, from D1.5 on, the deferred `flux-sync`
  apply. A failure in either writes `hold_sha` for the whole list. The *more-than-half*
  margin of `broad.py:narrow` also shrinks by one.
    - H1's six Flux-born roles, H2.3b's `longhorn-restore-drill` and H6's `system-upgrade`
      each add an entry. M4 and M6 cite D1.2's stop rule for theirs.
    - D2.1c, which needs D2.1a, records an added or removed entry that is not Ansible-owned
      by its fields instead of returning None. A bare Flux-born entry then reaches five
      reader tags: artifacts, monitor-bridge, netpol-baseline, pihole and uptime-kuma. An
      entry with `hostname` or `tier` reaches more. It never reaches `flux`:
      `tier_priority_class` is in `containers.py:NAMED_ENTRY`, and the role calls it with
      the literal `'flux'`, so it reads that one entry.
    - `changed_fields` still returns None for an added or removed Ansible-owned entry, for a
      repeated name, and for a change in the relative order of the entries both lists hold.
      Each of those reaches the 23 readers and `flux`.
    - D2.1c shrinks the apply but does not empty it. So every PR that adds or removes a k8s
      entry before D6.1 runs D1.2's stop rule over the tags its narrow output names, with
      its refusal and hold exits, and quotes that output and both release runs, as M4 does
      (D2, *An added or removed entry that is not Ansible-owned*). The rule does not end
      when D2.1c lands.
    - When the output names `flux`, the stop rule reads `flux`'s release record too. For a
      None case before D5's flux PR, `flux` is a named member that the expected narrow
      output must contain. A list without it contradicts the derivation, so the session
      finds out why before going on.

  D1 owns only that procedure, which stays as D1.2 states it.
- **An unattended apply of the flux role inside a DR window resumes the root** (failure
  class 5: the lever-on run, the hand resume and the restores are a sequence that any later
  apply can interleave with). H1's order creates the root suspended through
  `flux_sync_suspend: true`, and that suspend lives in the last-applied annotation. A tick
  that reaches `flux` as a reader with the lever off deletes it, and the root then
  provisions fresh claims before the restores. `k8s_autodeploy: false` does not stop that
  plane. Mitigation: H1's order parks the deployer, **operator-only**, from before its step
  1 until its step 12, with a runtime mask of `gitops-deploy.service` and a stop of its
  timer (*Decisions*). Residual risk: a reboot of daniel-box inside the window clears the
  mask, so the operator parks again before the timer's `OnBootSec` fires.
- **The first install's dry run is partial** (failure class 2). Before `flux-system` exists,
  `--dry-run` shows the API server only the CRDs and the cluster RBAC, and refuses every
  namespaced document with NotFound. Mitigation: `prek run --all-files` validates the
  namespaced documents against the vendored schemas, and the D1.2 PR body quotes the dry
  run, so each refusal is read as the missing namespace and not as a passing check. A
  namespaced document that only the API server would refuse first fails at the landing,
  as a hold with the way out under *Rollback*.
- **Every Flux read waits on a hand apply** (failure class 3, as in
  `readonly-sa-forbidden-on-secrets`). The read-only identity reads no Flux group
  until the operator applies D1.2b, and the tick never applies it (*Baseline*). Until then
  Exits 2 and 3 and the drill check print Forbidden, which an unwary reader could take for
  "not created yet". Mitigation: D1.5 does not merge before the apply, `land.sh` reads
  `needs-manual-apply` and the SessionStart banner names the pending `manual_plane` line,
  and the coverage test fails CI if a later Flux group is missing from the list.
- **The no-op status proves only the notification path** (failure class 2). It says nothing
  about apply. D2 is what proves apply, and D1 claims no more.
- **A reader that names the applied commit by the wrong value reports a false hold**
  (failure class 2). The triggering SHA and `lastAppliedRevision` both look like the applied
  commit, and neither is. A verify step keyed on the trigger waits for statuses that
  notification-controller posts on T instead, times out, and holds. A read of
  `lastAppliedRevision` hands a digest to `git merge-base`, which fails. Mitigation:
  *Decisions* fixes T as the one handoff and the origin revision as the one carrier, and the
  D1.4 test refuses a tag step that reads `head_sha`. C2's *Promotion target* and
  *Verification* rows and C6's *Which revision field names a commit* row state the same
  reads, and D2, D4 and H1 adopted them (Open questions 5 and 6, resolved).
- **A self rollout that keeps the default signals fails the health gate** (failure class 2).
  `probe.py health flux` would read a `restart: true` that no task honours as NOT ROLLED.
  Mitigation: `restart_on: []` on every entry, and the D1.2 self-rollout test with its red
  half.
- **A prune deletes a version still in use** (failure class 5). Listing and deleting are a
  sequence, and a tag can move between them. Mitigation: a prune deletes only a version
  older than 14 days whose commit is a strict ancestor of `prod`'s, which no walk can choose.
  A version that carries `prod` or `last-good` is never selected, and D2's reset moves
  `prod` only to the version tagged `last-good`.
- **An age-based prune would delete a kept digest's signature** (failure class 4: the
  selector protects tagged versions, and the hazard extends to their signatures). cosign
  v3's bundle is an untagged version that only its `subject` ties to the digest it signs
  (*Baseline*). A rule that deleted every untagged version past 14 days would strip the
  signature from `prod` or `last-good`. Every later verification of that digest would then
  fail, including D2's reset to a `last-good` older than 14 days after a long hold, and the
  deletion cannot be undone. Mitigation: the selection deletes an untagged version only when
  it has no `subject` or its `subject` is a deleted digest. The D1.7 `_is_kept` tests hold
  the bundles of `prod` and `last-good`. Each run ends with `cosign verify` on `prod`'s
  digest. The rule is safe whichever referrer path GHCR takes. If GHCR does not list the
  bundles as package versions at all, none is deleted, which costs storage only.
- **A textual guard cannot see the vendored Deployments** (failure class 4).
  `ansible/tests/k8s/test_workload_shell_uses_the_macros.py:shell_offences` keys on a
  literal `kind:` line, and
  `components.yaml.j2` has none. Mitigation: the rendered guards read the patched objects,
  and the D1.2 red run shows each of them firing on the unpatched render.
- **Push, sign and tag are a sequence, not a transaction** (failure class 5). A failed sign
  leaves an unsigned `sha-X` tag in GHCR. Mitigation: `needs: publish` stops that run's
  promote job, and `decide` treats a failed signature check as an error. The next merge
  publishes a newer commit, which the walk reaches first.
- **A bundle that omits a child Kustomization drops that child.** The root has
  `prune: true`, so a renderer bug, or a D1-style empty bundle surviving into D2, would make
  the root delete the child Kustomization object. C3's *Deletion policy* row gives every
  published Kustomization `deletionPolicy: Orphan`, so that deletion orphans the child's
  workloads, which keep serving with no owner. D1 has no children, so the hazard starts in
  D2, whose publish census refuses a bundle whose `flux/` files differ from the flux-owned
  tags (C3's *Publish census* row; Open question 4, resolved).
- **An empty bundle may not reconcile.** If kustomize-controller rejects an empty `resources:`
  list, D1.4's bundle gains one ConfigMap, `homelab-root`, in `flux-system`, and Exit 3 still
  holds.
- **The workflow file comes from the default branch** (failure class 7). A `workflow_run`
  workflow runs the version on master, so an edit to `promote.yml` affects runs after it
  merges, never the run of its own PR. A job that checked out the triggering commit would
  run an older `promote.py` under a newer `promote.yml`. Mitigation: `promote` checks out
  `github.sha`, the commit the workflow file came from, and only `publish` checks out the
  triggering commit. The D1.4 test reads the file statically and refuses a `promote` job
  that checks out `head_sha`, and the first live run is checked by hand.
- **GHCR, GitHub or Sigstore is down.** Keyless verification needs Sigstore's public
  services, and pulling needs GHCR. Flux keeps applying the last artifact it fetched and
  verified, so an outage only delays deploys. `decide` errors rather than promoting an older
  commit. A status that cannot be posted means a later promote waits, then times out (D2).
- **A token expires.** A fine-grained token expires within a year. When the status token
  expires, statuses stop and D2's promote job times out into a hold. When `PROMOTE_TOKEN`
  expires, `decide` errors and promotion stops. Mitigation: both tokens have a SOPS key in
  the rotation registry, so "Secret Rotation" goes down before the year is out. The role's
  `CLAUDE.md` holds the operator-only rotation steps for each.
- **Upgrading Flux is manual.** Renovate's hosted app cannot run `flux install` after a bump.
  Mitigation: the per-package rule keeps the bump out of automerge, the header test fails a
  bump that skipped regeneration, and the renovate-prs skill's "finish the half Renovate
  could not do" covers the regeneration step.
- **Two Discord streams.** The deployer and Flux both post to the deploy channel. Flux posts
  errors only, and D6 retires the deployer's k8s alerts.
- **The CI-context list moves out from under `decide`.** H7.2e moves the key to
  `ansible/inventory/group_vars/all.yml` as `ci_required_contexts`, and H7.20a deletes the
  role defaults `decide` reads. A `decide` left on the old path would print `error` on
  every run, and cluster promotion would stop. Mitigation: H7.2e repoints `decide` in the
  same PR (*Decisions*). D1.3's checkout test fails CI on a move without the repoint, the
  shape rule turns the interim Jinja reference into an `error` rather than a stalled walk,
  and the literal path in `promote.py` lets H7's derivation grep find the reader.

## Tests and docs that change in the same PR

- **D1.1:** the vendored schemas under `scripts/validate/schemas/`. The ADR, `Proposed`.
- **D1.2:**
    - `ansible/roles/k8s/flux/CLAUDE.md`, with its generated `## At a glance` block (the
      `regen-role-glance` hook). It records the guard decision, the tag-pinned images and
      `PROMOTE_TOKEN`'s rotation steps.
    - A header-version test in `ansible/tests/k8s/`.
    - A self-rollout test in `ansible/tests/k8s/`, with its red half without `restart_on`.
    - `ansible/tests/k8s/test_deploy_strategy.py:_RECREATE` gains source-controller.
    - `ansible/tests/k8s/test_priority_class_is_derived_from_tier.py:_CLASS_AT_DERIVATION`
      gains `"flux": "homelab-critical"`.
    - `ansible/tests/deploy/test_restart_on_narrows_the_restart_signals.py:_EXPECTED_NARROWERS`
      gains `flux`, so its signal check reads the three new entries.
    - The comment above the entries in `ansible/inventory/host_vars/daniel-box.yml` names
      the deploy controller in the `platform` tier.
    - The guards the patch satisfies, each green against the rendered role:
      `test_no_resourcequota_is_declared_anywhere`, `test_pod_template_hygiene.py`,
      `test_every_asserting_container_pins_a_uid`, `test_every_deployment_states_a_strategy`,
      `test_volume_names_name_their_workload`, and the two `_workload_property_rows.py` rows.
    - `renovate.json`: the denylist rule's path, and the per-package rule.
      `test_renovate_automerge_follows_the_autodeploy_denylist.py` and
      `test_renovate_per_package_manual_rules_carry_the_denylist_marker.py` hold both.
    - The `regen-doc-fragments` hook rewrites the auto-deploy coverage fragment.
    - `docs/flux-deploy-plane.md` and its `mkdocs.yml` nav entry. ADR-0010's dated line.
- **D1.2b:** the comment above the three new members of `k3s_readonly_crd_api_groups`.
  `test_readonly_role_covers_the_crd_groups_this_homelab_deploys` gains the derived Flux
  check and its red half. `docs/flux-deploy-plane.md` states that the read-only identity
  reads the Flux kinds from D1.2b's apply on.
- **D1.3:** tests in `scripts/deploy_tools/tests/`, the request-count test and the checkout
  test of the CI-context list among them. The comment above `gitops_deploy_ci_contexts` in
  the role defaults names `promote.py decide` as a reader, beside `fetch_ci_verdict`.
- **D1.4:** a workflow test in `ansible/tests/repo/`, with seven red fixtures, the seventh
  for the `cosign-release` pin.
  `docs/flux-deploy-plane.md` gains the publish and promote section, which names `none` as
  the value of `DEPLOY_HOLD` with no hold. The rotation registry gains `promote_token`.
- **D1.5:** the role `CLAUDE.md` records the measured memory, the status token's expiry,
  `flux_sync_suspend`, and the verify drill with its way out. It says that the next run
  with the lever off removes a suspend that a lever-on run wrote, and that the role is a
  `containers_list` reader, which is why H1's DR order parks the deployer. The rotation registry gains
  `flux_github_status_token`.
  `ansible/tests/k8s/test_deferred_manifest_dir_is_reserved.py:KNOWN_DEFERRED_DIRS` gains
  `flux-sync`. The D1.2 self-rollout test gains this PR's secret-render case.
  `docs/gitops-pipeline.md` gains one sentence and a link: Flux runs and applies nothing
  outside `flux-system`. Root `CLAUDE.md` stays true, because its "only write path" sentence
  is about the path an agent writes through.
- **D1.6:** `ansible/roles/k8s/observability/CLAUDE.md` names the Flux config and says it
  travels as an argument. The comment above the ClusterRole in `kube-state-metrics.yaml.j2`
  names the new argument beside `--resources`.
  `ansible/tests/services/test_observability_config_rolls_one_workload.py` stays green
  unchanged, because no ConfigMap is added.
- **D1.7:** tests in `scripts/deploy_tools/tests/`, the bundle cases among them, and the
  prune-workflow case in the D1.4 workflow test. `docs/flux-deploy-plane.md` gains the
  retention rule and the signature format it protects.

## Complexity delta

- **Added:**
    - one k8s role (about 240 hand-written lines and a vendored manifest);
    - four vendored Flux schemas and one core schema file;
    - two workflows (about 140 and 40 lines);
    - `promote.py` with `decide` and `prune` (about 320 lines and 390 of tests);
    - three members of `k3s_readonly_crd_api_groups`, one derived check in an existing test,
      and one operator hand apply of `k3s-bringup.yml --tags kubeconfig` (D1.2b);
    - a kube-state-metrics config (about 60 lines);
    - two GitHub tokens, two SOPS keys, one repository variable, two Renovate rule edits;
    - one `_RECREATE` row, one `_CLASS_AT_DERIVATION` row, one `KNOWN_DEFERRED_DIRS` row,
      one `_EXPECTED_NARROWERS` member, and one ADR in `Proposed` status;
    - one operator ruling (Open question 10) and one unattended apply of 23 reader roles
      at D1.2's landing, gated by two release-record reads before the merge;
    - from D1.2 until D5's flux PR, one more reader tag, `flux`, on every entry change for
      which `changed_fields` returns None, and one more release record for each such PR's
      stop rule to read;
    - one **operator-only** park of the GitOps deployer in H1's DR order, a runtime mask of
      `gitops-deploy.service` and a stop of its timer, undone at H1's step 12;
    - one anonymous registry check before D1.5 merges.
- **Requests per promote run.** `decide` makes one GHCR token request and one manifest
  fetch for `prod`, then, for each candidate it examines, one `HEAD`, one check-runs read and
  one `cosign verify`. With one new commit since `prod`, that is two registry requests, one
  `HEAD`, one GitHub API read and one signature check, whatever the number of tags. The
  prune adds, once a day, one paginated package listing, one manifest fetch for each
  untagged version older than 14 days, its deletes, and one or two `cosign verify` calls. At
  about 124 merges a day, that is about 124 manifest fetches a day once the backlog has
  drained, because each day's bundles cross the 14-day line once and are then deleted with
  their digests or kept.
- **Removed:** nothing. D6 removes the deployer's k8s arms.

## Open questions

1. **Accept Flux 2.9 on Kubernetes 1.37, or wait for a release that lists it?** Resolution:
   at D1.2, re-read `gh release list -R fluxcd/flux2`. If none lists 1.37, the operator
   decides (**operator-only**). The cluster has no stage guest to try it on first (D2's
   baseline).
2. **Is the `homelab-manifests` package public, or does Flux get a pull secret?** Default:
   public, as `ghcr.io/danielh2018/texbrain` is. After R2 the artifact holds placeholders, not
   secret values. Changing package visibility is a GitHub setting (**operator-only**).
   Gate: before D1.5 merges, checked by the anonymous pull in D1.5's *Gates before merge*.
   If the operator chooses a pull secret instead, D1.5 renders it and that check is dropped.
3. **Can `GITHUB_TOKEN` write a repository variable?** This plan assumes not and uses
   `PROMOTE_TOKEN`. Resolution: D1.4's first run logs the permission error, or not.
4. **Resolved: what guards the root's prune against a shrunken bundle, and does C3 adopt
   the root?** C3 now names the root in its *Root* row and gives every published
   Kustomization `deletionPolicy: Orphan` in its *Deletion policy* row. D2's *The per-tag
   Kustomization* sets that policy, and D2.6b's publish census implements C3's *Publish
   census* row, which refuses a bundle whose `flux/` files differ from the flux-owned tags.
   *Risks* restates the hazard under that contract.
5. **Resolved: does C2 accept a superseded promote job and a target chosen from master?**
   The integration review confirmed the gap (X-02) and fixed the contracts. C2's
   *Promotion target* row promotes the newest published master commit T, and its
   *Verification* row has `promote.py verify T` check T. C6's `land.sh` row waits until the
   merge is covered rather than on one run. D2's *Publication and verification* adopts the
   verify side, and its *How `land.sh` waits* adopts the coverage wait. D1 keeps the
   verdict set and T's handoff (*Decisions*).
6. **Resolved: which field carries the commit for C6, D2, D4 and H1?** The integration review
   confirmed the gap (X-03). C6's *Which revision field names a commit* row names
   `status.lastAppliedOriginRevision`, and D1.6 labels that field. D2's *How `land.sh`
   waits*, D4's *Verification* and H1's *Goal and exit criterion* and *Verification* read
   the origin field. D4 keeps `lastAppliedRevision` only where it describes
   `checkDependencies`, which compares digest forms, as C6 allows.
7. **Does C3 name the verify drill's OCIRepository?** C3's *Namespace* row names one
   OCIRepository, `homelab`, and the drill renders a second, `verify-drill`, in
   `flux-system`. It lives only between the two drill PRs, and the role deletes it while the
   lever is off (*Decisions*). Resolution: a requested amendment on C3 that names
   `verify-drill` as a transient exception, present only while `flux_verify_drill` is true.
   Gate: before D1.5 merges, because D1.5 adds the template that renders it. contracts.md
   lists the row under *Requested amendments not yet applied*, *Requests on C3*, with this
   gate. The gate stands until contracts.md applies the row to C3's *Namespace* row.
   The rejected alternative is a scratch namespace deleted afterwards, which adds a
   Namespace that the role must create and delete for one object.
8. **Does GitHub accept `GITHUB_TOKEN` to list and delete versions of this package?** The
   package is owned by the user account and linked to this repository by the push.
   Resolution: D1.7's first prune run logs a permission error on the listing or the delete,
   or not. If it does, the operator grants this
   repository the Admin role in the package's Actions access settings, which is a GitHub
   setting (**operator-only**). Until then the prune deletes nothing, which costs storage
   only. The same run shows whether GHCR lists cosign's untagged bundles as package
   versions: the prune log counts the untagged versions whose manifest has a `subject`. The
   selection is safe either way (*Risks*).
9. **Resolved: do C2 and C6 adopt `none` as the value of `DEPLOY_HOLD` with no hold?**
   Yes. The integration review applied the amendment (X2-01). C2's *Promotion target* row
   moves `prod` only while `DEPLOY_HOLD` reads `none`. Its *`DEPLOY_HOLD`'s values* row says
   that any other non-empty value is a hold and that an empty or missing value is an error.
   C6's *`land.sh`* row ends the wait on a value other than `none`. "Clearing" the hold
   means setting it to `none` with
   `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`. D1.3's contract gate is
   therefore satisfied. Re-read at round i1, D2, D4, D5, H1 and H6 write `none`: D5's
   `promote-reset` refuses on `none`, empty or missing, D4's checks and clear read `none`,
   H1's *The hold's values* reads it as D1 and D2 define it, and H6's clear steps name the
   `none` command. No slice is left to change.
10. **Does the operator accept an unattended apply of the edge, auth and DNS roles at D1.2's
    landing?** Adding the `flux` entry makes the tick apply 24 tags forward-only, traefik,
    authelia, pihole and netpol-baseline among them (*Baseline*). No ruling accepting that
    exists yet. Default: accept it under the D1.2 stop rule. Resolution: the operator states
    the ruling on the D1.2 PR (**operator-only**). Gate: before D1.2 merges. If the
    operator declines, D1.2 waits for a deployer change that lets an added entry reach only
    the readers of the fields it sets. D2.1c makes that change for entries that are not
    Ansible-owned only (*Risks*, X2-06). It would not cover the `flux` entry, and D2
    depends on D1, so a decline needs its own deployer PR ahead of D1.2. The ruling also
    covers the later landings that reach `flux` as a reader until D5's flux PR (*Risks*):
    each of them applies the 23 readers and `flux` under the same stop rule.

## Review log

### Round 1 (per-slice adversarial review)

Raised: D1-R1-01 to D1-R1-07 at high, and D1-R1-08 to D1-R1-14 at medium. All fourteen are
fixed. D1-R1-01, D1-R1-04 and D1-R1-06 each also carry one sub-claim refuted with evidence.

- **D1-R1-01 (high), Exit 2 read the OCI revision. Fixed.** Exit 2, its command and D1.6's
  label read `lastAppliedOriginRevision`. Confirmed upstream: kustomize-controller v1.9.6
  `api/v1/kustomization_types.go`, source-controller v1.9.6
  `OCIRepositoryReconciler.getRevision`, flux2 v2.9.6 `cmd/flux/push_artifact.go`. Open
  question 6 hands D2, H1 and C6 the switch. The finding's claim about D4 is refuted: D4's
  sentence describes kustomize-controller v1.9.6 `checkDependencies`, which compares a
  dependency's `lastAppliedRevision` with the source's artifact revision. Both sides are
  `<tag>@<digest>`, so D4 is correct as written.
- **D1-R1-02 (high), promote held no registry write. Fixed.** `promote` gets
  `contents: read`, `packages: write` and `checks: read`, with no `id-token`. *Decisions*
  names the token for each call, and the D1.4 test asserts both permission maps.
- **D1-R1-03 (high), an evicted promote job stranded prod. Fixed.** `decide` walks master
  for the newest commit with a tag, a verifying signature and a green verdict. The
  "harmless" sentence is gone, the out-of-order case is a D1.3 test, and Open question 5
  carries the C2 gap.
- **D1-R1-04 (high), the tick applies D1.2 unattended. Fixed, one alternative refuted.** The
  landing step describes the broad plane, the narrowing, the forward-only hold, the deferral
  and the clear. The PR body records the pre-PR narrow output. Refuted: landing the role
  first with no `containers_list` entry and applying it by hand. `ansible/deploy.yml`
  includes a k8s role only in its loop over `containers_list`, with tags from
  `container_item.tags | default([container_item.name])`. A role with no entry is reachable
  by no `--tags` value.
- **D1-R1-05 (high), the include queued `deploy/flux` in `homelab`. Fixed.** The include sets
  `manifests_rollout: ''` and three `manifests_self_rollouts` in `flux-system`, and the role
  waits on them itself. Upstream `config/manager/deployment.yaml` gives the pods the
  `app: <controller>` label that the snapshot selects on. The role is written by hand, and
  the plan says why.
- **D1-R1-06 (high), the raw install trips the fleet guards. Fixed, one guard refuted.**
  *Decisions* has one operator-visible answer per guard: the ResourceQuota is stripped at
  vendoring, five fields are patched, and source-controller gets one `_RECREATE` row. The
  schemas land first in D1.1. Refuted:
  `ansible/tests/k8s/test_workload_shell_uses_the_macros.py:shell_offences` would not refuse
  these Deployments. It keys on a literal `kind:` line in template source, which
  `components.yaml.j2` lacks. That blind spot is a Risk instead. The split order differs
  from the finding's: the `_RECREATE` row lands with the role, because
  `test_allowlist_has_no_stale_entries` fails a row with no Deployment behind it.
- **D1-R1-07 (high), a failed read could promote. Fixed.** Every read has three outcomes, an
  error exits non-zero, a missing `DEPLOY_HOLD` is an error, exit 128 from `merge-base` is
  an error, and `promote` checks out with `fetch-depth: 0`. D1.3 lists the refusal cases.
- **D1-R1-08 (medium), Renovate could not see `flux_version`. Fixed.** D1.2 adds the
  annotation, the denylist-rule path, and a per-package manual rule. The docker-only scope of
  the denylist rule means the path alone would not gate a github-releases bump. The vendored
  images' tag pins are a recorded exemption.
- **D1-R1-09 (medium), `PROMOTE_TOKEN`'s expiry had no signal. Fixed.** A SOPS copy,
  `promote_token`, is a `source: record` row of tier `external`, which the existing "Secret
  Rotation" monitor watches.
- **D1-R1-10 (medium), CRDs and CRs shared one apply. Fixed.** `sync.yaml.j2` is deferred to
  `flux-sync` and applied after an `Established` wait, following headlamp and
  netpol-baseline.
- **D1-R1-11 (medium), docs lagged the PRs. Fixed.** D1.2 creates
  `docs/flux-deploy-plane.md` and annotates ADR-0010. D1.1 adds the ADR in `Proposed`
  status, and D1.5 adds the sentence to `docs/gitops-pipeline.md`.
- **D1-R1-12 (medium), Exit 3 checked a proxy. Fixed.** Exit 3 reads the root's inventory
  and lists every Kustomization, and an offline filter proof shows it going red.
- **D1-R1-13 (medium), the root's prune can cascade. Fixed by hand-off.** *Risks* records the
  hazard, and Open question 4 hands the guard to D2 and C3 with two options.
- **D1-R1-14 (medium), workflow specification gaps. Fixed.** `needs: publish`, job-level
  permissions with `contents: read`, `fetch-depth: 0`, `ubuntu-26.04`, the flux CLI version
  from `flux_version`, and a pinned cosign. *Risks* names failure class 5, and the D1.4 test
  asserts `needs` and the permission maps.

### Integration round (cross-slice review)

Raised: X-02 and X-03, both high. Both are confirmed, and neither is refuted. D1 fixes its
own part of each. The parts in C2, C6, D2, D4 and H1 are handed to those documents through
Open questions 5 and 6, because this round edits D1 only.

- **X-02 (high), D2 verifies the triggering SHA while `decide` promotes T. Fixed in D1,
  handed on for the rest.** D1 carried the same defect itself: Exits 1 and 2 and the
  revision label read X, so a merge that superseded X before its promotion would fail them
  falsely. They now read T from the root's `lastAppliedOriginRevision`, and an ancestry
  check requires T to be X or a descendant of it. *Decisions* makes `decide` own the verdict
  set, with no `superseded`. It prints one line naming the trigger, the verdict and T, and
  writes `verdict` and `target` as step outputs. The D1.4 workflow test asserts that the
  tag step reads `target` and that no later step reads `head_sha`, with a fifth red
  fixture. D1.3 gains the trigger-older-than-`prod` case and asserts the printed target.
  The status really does land on T: kustomize-controller v1.9.6
  `KustomizationReconciler.event` sets `originRevision`, notification-controller v1.9.4
  `EventServer.combineEventMetadata` strips the group prefix, and fluxcd/pkg
  `Event.GetRevision` at `apis/event/v0.27.2` prefers `originRevision`. Open question 5
  carries the C2 and C6 wording, and lists D2's diagram, verify argument, hold value,
  `last-good` tag and test.
- **X-03 (high), D2, H1 and D4 read `lastAppliedRevision` as a commit. Fixed in D1, handed
  on for the rest.** Confirmed upstream: kustomize-controller v1.9.6
  `api/v1/kustomization_types.go` documents `LastAppliedOriginRevision` as the OCI
  `org.opencontainers.image.revision` value, and source-controller v1.9.6
  `internal/controller/ocirepository_controller.go` copies the manifest annotations into
  `status.artifact.metadata`. The baseline now states the OCIRepository side of the split.
  *Decisions* adds the rule that every reader names the commit by its origin revision.
  Open question 6 hands D4's Exit 4 and D4.6 readings the switch to
  `lastAppliedOriginRevision` and the metadata key, and hands C6 the carrier field.
  Round 1's refutation of D1-R1-01's D4 claim was too broad: it held for D4's
  `checkDependencies` sentence only, and Open question 6 no longer says that D4 keeps
  `lastAppliedRevision` anywhere else.

### Round 10 (per-slice adversarial review)

Raised: D1-R10-01 to D1-R10-03 at high, and D1-R10-04 to D1-R10-12 at medium. All twelve
were checked against the tree at `70591c307` or against upstream through `gh`, and all
twelve are fixed. None is refuted. Where a finding offered two fixes, the entry names the
one taken.

- **D1-R10-01 (high), self-rollout entries without `restart_on` read as NOT ROLLED.
  Fixed.** Confirmed: `release_stamp.yml` reads `manifests_restart_triggers_default`,
  `[config, secret, image]`, for an entry with no `restart_on`, and `secret.yaml.j2` in D1.5
  changes the secret render. The three entries move to `flux_controllers` in the role's
  defaults with `restart_on: []`, and *Decisions* gives the reason. Upstream's three
  `config/manager/deployment.yaml` files read no Secret or ConfigMap through `env` or a
  `subPath`, which *Baseline* now records. D1.2 adds a self-rollout test with a red half
  and adds `flux` to `_EXPECTED_NARROWERS`, and D1.5 adds its secret-render case.
- **D1-R10-02 (high), `decide` listed every tag with two fetches each. Fixed.** Confirmed:
  fluxcd/pkg `oci/list.go:Client.List` fetches each non-cosign tag's manifest and digest in
  sequence, and fails on any one. `decide` now fetches `prod`'s manifest and sends one
  `HEAD` per candidate through its own registry client, with a three-outcome mapping for
  each. D1.3 adds the 404-skip and 500-error cases and a request-count test. A new PR, D1.7,
  owns retention: a daily `prune-artifacts.yml` that never deletes a version tagged `prod`,
  `last-good` or `verify-drill`, or a walk candidate, and uses `GITHUB_TOKEN`. Open
  question 8 asks whether GHCR accepts that token for a delete. *Complexity delta* names the
  requests per run.
- **D1-R10-03 (high), the verify-drill OCIRepository outlived the drill. Fixed.** Confirmed:
  `manifests_prune_allowlist` holds no Flux kind and `kubectl apply` deletes nothing it no
  longer sees. The role deletes `ocirepository/verify-drill` with `--ignore-not-found`
  while the lever is off, following prowlarr's "Retire the flaresolverr config PVC". The
  `deploy-errors` Alert names only Kustomizations and the OCIRepository `homelab`, so the
  drill posts nothing to Discord. *Verification* proves the object is gone, and Open
  question 7 requests the C3 amendment.
- **D1-R10-04 (medium), the entry had no tier. Fixed.** The entry declares
  `tier: platform`, `components.yaml.j2` takes the class from `tier_priority_class('flux')`,
  and `_CLASS_AT_DERIVATION` gains `flux`. The tier also keeps the deploy controller out of
  `probe.py shed-set`.
- **D1-R10-05 (medium), two guards were missing. Fixed.** Confirmed upstream: volumes
  `data` and `tmp` in source-controller v1.9.6, and `temp` in kustomize-controller v1.9.6 and
  notification-controller v1.9.4. The plan takes the rename, as a list rewrite in
  `components.yaml.j2` with `name` first, and keeps one exemption. `KNOWN_DEFERRED_DIRS`
  gains `flux-sync` in D1.5. Both guards are in *Baseline*, and the volume guard is in the
  D1.2 red run.
- **D1-R10-06 (medium), the CI read named no required check-runs. Fixed.** Confirmed:
  `deploy_git.py:ci_verdict` returns `pass` on an empty set. `decide` reads
  `gitops_deploy_ci_contexts` from its checkout, and an empty list is an `error`. D1.3 adds
  the empty-set and missing-check-run cases.
- **D1-R10-07 (medium), `promote` ran the trigger's `promote.py` under master's workflow.
  Fixed.** `publish` alone checks out `head_sha`. `promote` checks out `github.sha` with
  `fetch-depth: 0`. The D1.4 test asserts both checkouts, with a sixth red fixture.
- **D1-R10-08 (medium), an empty `DEPLOY_HOLD` was unverified. Fixed.** Confirmed: cli/cli
  `pkg/cmd/variable/set/set.go:getBody` ignores an empty `--body`. The plan takes the
  sentinel `none`, created with `gh variable set DEPLOY_HOLD --body none`. Any other
  non-empty value holds, and an empty or missing value is an error. D1.3 adds the cases, and
  Open question 9 requests the C2 and C6 amendment.
- **D1-R10-09 (medium), Exit 3 counted empty output as clean. Fixed.** Confirmed upstream:
  `ResourceInventory.Entries` has no `omitempty`, and `inventory.go:New` starts it empty.
  Exit 3 prints `{.status.inventory}` and expects exactly `{"entries":[]}`. The red proof is
  the misspelt path, which prints the empty string.
- **D1-R10-10 (medium), the pre-PR dry run cannot pass before `flux-system` exists. Fixed.**
  Confirmed: kubernetes `Lifecycle.Admit` has no dry-run exemption. *Decisions* and *Risks*
  state the limit. D1.2's pre-PR check is `prek run --all-files` for the namespaced
  documents, and the PR body reads each NotFound from the dry run as the missing namespace.
  The finding's `kubectl apply --dry-run=client` was not added, because the schema
  validation already parses every document.
- **D1-R10-11 (medium), D1 described closed contract gaps. Fixed.** Open questions 4, 5 and 6
  are marked resolved, each pointing at its C2, C3 or C6 row and at the D2, D4 and H1
  sections. The two *Decisions* sentences cite the contract. The Risk now says that under
  C3's Orphan policy a dropped child orphans its workloads, and D2's census guards the drop.
- **D1-R10-12 (medium), D1.6 did not say how kube-state-metrics picks up the config.
  Fixed.** The config travels inline as `--custom-resource-state-config`, so it lives in
  the pod template and rolls through the apply. No ConfigMap is added, so
  `test_observability_config_rolls_one_workload.py` stays unchanged. The ClusterRole comment
  and its RBAC change together. D1.6 lands with `land.sh --tags observability` and
  `probe.py health observability`.

### Round c1 (per-slice adversarial review)

Raised: D1-C1-01 and D1-C1-02 at high, and D1-C1-03 at medium. All three were checked
against the tree at `70591c307` or against upstream through `gh`, and all three are fixed.
None is refuted in full. D1-C1-01 and D1-C1-03 each carry one sub-claim corrected below.

- **D1-C1-01 (high), plain `kubectl` could not read any Flux kind. Fixed.** Confirmed:
  `ansible/roles/setup/k3s/defaults/main.yml:k3s_readonly_crd_api_groups` holds
  `traefik.io`, `metallb.io`, `longhorn.io` and `k3s.cattle.io`, and
  `readonly-rbac.yaml.j2` grants read per listed group only.
  `test_readonly_role_covers_the_crd_groups_this_homelab_deploys` checks the Traefik groups
  and the k3s group only. A new PR, D1.2b, appends the three Flux groups and extends that
  test to derive the groups of every CRD the flux role renders, with a named member and a
  red half. *Baseline*, *Decisions*, *Verification*, *Rollback*, *Risks*, *Tests and docs*
  and *Complexity delta* follow it, D1.5 is gated on the apply, and the red proofs for Exits
  2 and 3 and the drill now name Forbidden, then NotFound. Corrected sub-claim: the k3s
  role's path is not in `_BROAD_MANUAL_PREFIXES`, which lists playbooks. A change to the
  role is fast-forwarded and recorded as a `manual_plane` line narrowed to `kubeconfig`
  (`docs/gitops-pipeline.md`, *A role only a hand can apply is recorded, not parked*;
  `scripts/deploy_tools/tests/test_narrow_setup.py:test_the_real_k3s_role_still_maps_readonly_rbac_to_kubeconfig`).
  The effect the finding names holds: the tick never applies it. The apply is
  **operator-only**, as in H2.
- **D1-C1-02 (high), the age rule deleted the signature bundles of kept digests. Fixed.**
  Confirmed: cosign v3.1.3 `cmd/cosign/cli/options/sign.go` defaults `--new-bundle-format`
  to `true` and marks it deprecated. cosign v3.1.3 `pkg/oci/remote/write.go:WriteReferrer`
  writes a manifest with a `subject`. source-controller v1.9.6
  `internal/oci/cosign/cosign.go:CosignVerifier.Verify` discovers bundles through the
  referrers API or the referrers tag. *Decisions* names the format and pins
  `cosign-release: v3.1.3`, and rejects `--new-bundle-format=false` as deprecated. The prune
  deletes an untagged version only when it has no `subject` or its `subject` is a deleted
  digest. The case that named a signature by tag now names the bundle and the
  `sha256-<digest>` index. D1.7 adds the 30-day bundle cases for `prod` and `last-good`, a
  post-delete `cosign verify` on `prod`'s digest, and a test that `sha256-` never parses as
  `sha-<C>`. *Risks*, *Rollback* and Open question 8 follow. The GHCR listing behaviour the
  finding left unverified no longer matters to safety, because the rule keys on `subject`.
- **D1-C1-03 (medium), the register lacked D1's two amendments and Open question 9 omitted
  D5. Fixed in D1, handed on for the rest.** Confirmed: the register lists rows for H1, D5,
  H6, M6 and D6 only, C2 reads "`DEPLOY_HOLD` is empty", and C6 reads "A non-empty
  `DEPLOY_HOLD`". D5's `promote-reset` refusal, its tests and one *Risks* entry, D4's and
  H1's verification checks and H6's two clear steps still read the old rule. Open question 9
  now names D5, D4, H1 and H6 with what changes in each, and both Open questions 7 and 9
  name their gates: before D1.5 and before D1.3. The two register rows go to contracts.md
  through this round's cross-slice notes, because this round edits D1 only. Corrected
  sub-claim: D2 needs no change. Its Open question 8 and its clear command already read and
  write `none`.

### Round c2 (per-slice adversarial review)

Raised: D1-C2-01 at high and D1-C2-02 at medium. Both were checked against the tree at
`70591c307`, and both are fixed. Neither is refuted. The measurement for D1-C2-01 refines
the finding's reader count.

- **D1-C2-01 (high), D1.2's landing applies every role that reads `containers_list`.
  Fixed.** Confirmed: `scripts/deploy_tools/narrow_lib/containers.py:changed_fields` returns
  None when the entry names differ, `containers.py:reader_paths` then keeps every template
  that renders the list, and `broad.py:_containers_list_tags` passes that None to
  `_list_reader_tags`. Measured: `_list_reader_tags` with `changed` None returns 23 tags of
  59 declared, named in *Baseline*. That is 24 of 60 with `flux`, under the more-than-half
  refusal in `broad.py:narrow`, so the range narrows and applies. *Baseline* states the
  reach, the refusal's `k8s_unapplied` lines and the rule that only the deployer clears a
  broad hold. D1.2 gains a gate on Open question 10, the expected narrow output, and a stop
  rule that reads `probe.py releases --stale-only` and the plain table for every tag before
  the PR and again before `land.sh`. Its *Landing* names the 24 tags and says what each kind
  of refusal records and who clears it. *Rollback* was wrong twice. The held entry names
  the whole list, not `flux`, and a hand `deploy.sh` clears no hold, so the way out is the
  whole list by hand and then the operator's `clear-hold`. *Risks*, *Verification* and
  *Complexity delta* follow. The measured set refines the finding's grep from 22 readers to
  23, with `artifacts` added, which the derivation reaches. Both hand deploys of the list
  run from a checkout at `origin/master`, because `deploy.sh` renders the tree it runs in.
- **D1-C2-02 (medium), D1.5 did not gate on the package being public. Fixed.** Confirmed:
  D1.4 left the visibility change as an operator step after the first run, and D1.5's gates
  named only D1.2b's apply and the C3 amendment. D1.5 gains a third gate: an anonymous GHCR
  pull token and a manifest `HEAD` on `homelab-manifests:prod` must print `200`. D1.4's
  operator step and Open question 2 name the gate, and *Verification* gives its red half:
  `401` or `403` while the package is private, and `404` before the first promote.

### Round integration-2 (cross-slice review)

Raised against D1: X2-02 (high), X2-06 (high), X2-07 (high) and X2-11 (medium). Each was
checked against the tree at `70591c307` and against the other slices' plans. D1's part of
each is fixed, and none is refuted. The parts that belong to H7, H3, D2, D5, H1, H2, H6 and
contracts.md are handed to them.

- **X2-02 (high), H7 moves and then deletes the CI-context list that `decide` reads by
  path. Fixed in D1, handed to H7 and H3.** Confirmed:
  `ansible/roles/setup/gitops_deploy/defaults/main.yml:gitops_deploy_ci_contexts` holds the
  list, and H7's *Design* moves it to `ansible/inventory/group_vars/all.yml` as
  `ci_required_contexts` at H7.2e, leaves the role default as a reference until H7.19a, and
  names three surviving readers without `promote.py`. The move breaks `decide` at H7.2e
  already, not only at H7.20a, because a plain YAML read of the reference returns a string.
  *Decisions* now requires a non-empty list of strings, so a string is an `error` rather
  than a context name that keeps every candidate `pending`. It records that H7.2e repoints
  `decide` in the same PR, and keeps the path as one literal in `promote.py` for H7's
  grep. D1.3 gains a string fixture and a checkout test over the real list, whose named
  member is `prek (lint + validate + tests + secrets)`. *Risks*, *Verification* and *Tests
  and docs* follow.
  H7's path-reader grep matches only `/(etc|var/lib|opt)/gitops-deploy`, so it needs a
  pattern for the repo-relative path or the key name.
- **X2-06 (high), slices add `containers_list` entries while the deployer still narrows.
  Fixed in D1, handed to D2, H1, H2 and H6.** Confirmed: `containers.py:changed_fields`
  returns None for an added entry, and D2's `DEPLOY_MECHANICS_FIELDS` covers only an
  `owner`-only edit. D1.2 keeps its stop rule under either fix, because the `flux` entry
  carries no `owner: flux` and merges before D2. *Risks* names the later slices, D2.1a or
  D4.1 as the requested owner, and the fallback that each such PR carries D1.2's stop rule. It also records
  that "own tag only" would leave Ansible-owned readers stale for an entry with a
  `hostname` or a `port`. Open question 10 no longer implies D1.2 could wait on D2.
  Round i1 corrects this entry: D2.1c owns the deployer change, not D2.1a or D4.1, and the
  stop rule does not end when D2.1c lands (D1-I1-03).
- **X2-07 (high), three incompatible bring-up and DR procedures after H1. Fixed in D1,
  handed to D5 and H1.** Confirmed: H1's bring-up order steps 4 and 11 rely on a root-only
  D1 role variable that Open question 12 requests, and D5's flux PR makes the renderer
  refuse `flux_sync_suspend: true`. D1 declines the variable in *Decisions* and names D5's
  *Bootstrapping Flux on a rebuilt cluster* as the owner. The way-out decision now omits
  `spec.suspend` when the lever is off, because client-side apply sets every field the
  render names and an explicit `suspend: false` would undo a hand suspend. D1.5 gains a
  render test for both halves, which *Verification* names. *Rollback* says D5's flux PR
  retires the lever. R4's bootstrap and DR PRs also relied on the variable, so R4 is handed
  the same change.
- **X2-11 (medium), the register lacks rows that slices gate on. Fixed in D1, handed to
  contracts.md.** Confirmed: *Requested amendments not yet applied* holds neither D1's C3
  row for `verify-drill` (Open question 7, gated before D1.5) nor its C2 and C6 row for
  `none` (Open question 9, gated before D1.3), and C2's and C6's bodies still read "empty".
  The finding named only the first. Both questions now say the rows are handed on.
  Round i1 records that contracts.md has since applied the C2 and C6 row and lists the C3
  row (D1-I1-04).

### Round i1 (per-slice adversarial review)

Raised: D1-I1-01 at high, and D1-I1-02, D1-I1-03 and D1-I1-04 at medium. Each was checked
against the tree at `70591c307` and against the other slices' text. All four are fixed, and
none is refuted. Two refine the finding: the park in D1-I1-01 blocks the service as well as
the timer, and the window in D1-I1-02 ends at D5's flux PR rather than D6.1.

- **D1-I1-01 (high), an unattended tick can apply the flux role during H1's DR window, and
  the lever-off render resumes the root before the restores. Fixed.** Confirmed:
  `ansible/filter_plugins/service_tier.py:tier_priority_class` takes the list as its first
  argument, `scripts/deploy_tools/narrow_lib/containers.py:renders_containers_list` matches
  any mention inside Jinja code, and `containers.py:reader_paths` keeps every such template
  when `changed` is None. `docs/gitops-pipeline.md` says the narrowed list is not filtered
  through `K8S_AUTODEPLOY_DENYLIST`. *Decisions* now states that the role is a reader from
  D1.2 on, that the lever-on suspend lives in the last-applied annotation, and that any
  later lever-off apply removes it, an unattended tick included. It drops the claim that
  the sequence holds while the role's runs continue, and hands H1 an **operator-only**
  park from before its step 1 until its step 12. The finding's example, stopping
  `gitops-deploy.timer`, is not enough on its own: `scripts/deploy_tools/gitops_tick.sh`
  runs `systemctl start --no-block` on the service, and
  `scripts/deploy_tools/land_lib/tick.py:kick_tick` reaches it from `land.sh`. No
  `gitops_state.py` subcommand writes a hold. So the park is a runtime mask of
  `gitops-deploy.service` plus a timer stop. `man systemctl` says a persistent mask fails
  for a unit file already in `/etc/systemd/system`, where
  `ansible/roles/setup/gitops_deploy/tasks/service.yml` installs it. The park starts
  before step 1, not step 4, because a tick that reaches `flux` before step 4 creates the
  root running. *Risks*, *Complexity delta* and D1.5's role `CLAUDE.md` follow.
- **D1-I1-02 (medium), later entry changes re-apply `flux` too. Fixed.** Confirmed by the
  same chain. *Risks*, *Complexity delta* and Open question 10 now say that a
  None-returning entry change applies the entry's tag, the 23 measured readers and `flux`,
  that the apply runs the CRD wait and the deferred apply, and that the refusal margin
  shrinks by one. Refined: the window ends at D5's flux PR, because D2's entry-point table
  says `_deploy_plane` drops a tag that is not Ansible-owned. D2.1c never reaches `flux`
  for another entry, because `tier_priority_class` is in `containers.py:NAMED_ENTRY` and
  the role calls it with a literal name. So `flux` is a named member of the expected narrow
  output only for an added or removed Ansible-owned entry, a repeated name or a reorder.
- **D1-I1-03 (medium), the X2-06 fix named the wrong deployer PR and ended the stop rule
  when it landed. Fixed.** Confirmed against D2's *An added or removed entry that is not
  Ansible-owned* and its step D2.1c. *Risks* and Open question 10 name D2.1c and state
  D2's rule: every entry add or remove before D6.1 runs the stop rule over the tags its
  narrow output names. The X2-06 log entry carries a correction.
- **D1-I1-04 (medium), Open questions 7 and 9 and the hold bullet misstated contracts.md.
  Fixed.** Confirmed: C2's *Promotion target* reads `none`, its *`DEPLOY_HOLD`'s values*
  row holds the rule, C6's *`land.sh`* row ends the wait on a value other than `none`, and
  *Requests on C3* lists the drill row gated before D1.5. Open question 9 is resolved, the
  hold bullet and D1.3's contract gate cite the rows, and Open question 7 names the listed
  row. The four-slice list was re-verified and dropped: D2, D4, D5, H1 and H6 all write
  `none`.
