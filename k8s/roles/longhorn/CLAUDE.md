# longhorn — the storage control plane and the `longhorn` StorageClass

Longhorn's manager DaemonSet, its CRDs, its admission webhook and driver deployer, and the
ConfigMap that tells the manager which `longhorn` StorageClass to create. Every PVC in the
cluster binds through it. A bad apply here detaches volumes under every stateful service while
host probes stay green.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=longhorn` gates the rollout
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (H1.5a). Its `containers_list` entry is in `cluster/services.yml`
  from its first commit. A rebuilt cluster gets it from the `longhorn` Kustomization at step 6 of
  `docs/flux-deploy-plane.md`'s *Bootstrapping Flux on a rebuilt cluster*. The `longhorn-backup` Kustomization
  depends on it and creates the backup targets once it reads `Ready`.
- **The workloads are `files/upstream/longhorn.yaml`**, upstream's file byte for byte, pinned by
  `k3s_longhorn_version` and the `longhorn_vendored_manifests` item in `defaults/main.yml`.
  `scripts/dev/vendor_manifest.py` checks the pin and refreshes the copy. The refresh command
  sits above the pin. No render may fetch a URL (D5, C1).
- **Merging a bump of `k3s_longhorn_version` is the upgrade** (H1 Open question 6). `land.sh`
  runs the `longhorn-upgrade` gates before it merges, Longhorn supports no downgrade and one
  minor per hop, so read `docs/longhorn-upgrade.md` before you bump the pin.
- **Nine patches change upstream objects**, all under `files/patches/`. The renderer lists them
  as the Kustomization's `patches:`, and each names its target by kind, name and namespace.
    - `longhorn-manager.yaml` adds a memory request and no limit to the manager container.
      A kustomize patch is copied verbatim and cannot read a variable, so
      `tests/longhorn/test_longhorn_control_plane_resources.py::test_manager_patch_holds_the_value_of_the_defaults_key`
      holds the literal equal to `k3s_longhorn_manager_memory_request`.
    - `longhorn-global-manager.yaml` gives the Deployment v1.13 added a memory request, no
      limit, and two replicas instead of upstream's three (#5968). The replicas elect one
      leader, so on two nodes a third adds no failover.
      `tests/longhorn/test_longhorn_control_plane_resources.py::test_global_manager_patch_requests_the_defaults_value_and_no_limit`
      and `::test_global_manager_patch_holds_the_replicas_of_the_defaults_key` hold the
      literals equal to `k3s_longhorn_global_manager_memory_request` and
      `k3s_longhorn_global_manager_replicas`.
    - `longhorn-ui.yaml`, `longhorn-driver-deployer.yaml` and `longhorn-manager-pre-pull.yaml`
      give requests and limits to the four containers upstream ships with no `resources`:
      longhorn-ui, longhorn-driver-deployer, its `wait-longhorn-manager` init container, and the
      manager DaemonSet's `pre-pull-share-manager-image` sidecar (#8326, #8543). None of them
      sits in the storage data path, so unlike the manager they take a limit. The sidecar has
      its own file because `longhorn-manager.yaml` names only the manager container.
      `tests/longhorn/test_longhorn_control_plane_resources.py::test_unsized_upstream_containers_get_the_defaults_requests_and_limits`
      holds each block equal to its `k3s_longhorn_*_resources` key in `defaults/main.yml`, and
      `tests/longhorn/test_longhorn_control_plane_resources.py::test_no_vendored_longhorn_workload_runs_besteffort`
      fails a render that leaves any upstream Deployment or DaemonSet without a request.
    - `longhorn-manager-update-strategy.yaml` rolls the manager DaemonSet one pod at a time
      instead of upstream's `maxUnavailable: 100%` (#6009). The manager pods serve the
      admission webhook every claim's dry run calls, so one pod keeps serving it through a
      roll. The patch header has why 100% is not load-bearing for an upgrade.
      `tests/longhorn/test_longhorn_control_plane_resources.py::test_the_manager_daemonset_rolls_one_pod_at_a_time`
      fails a render in which the DaemonSet keeps upstream's strategy.
    - `longhorn-storageclass.yaml` replaces the class inside upstream's `longhorn-storageclass`
      ConfigMap, which longhorn-manager reads to create the `longhorn` class. The class omits
      `numberOfReplicas`, because a StorageClass parameter beats the `default-replica-count`
      setting. `tests/longhorn/test_longhorn_storageclass.py::test_the_class_differs_from_upstreams_only_by_the_replica_count`
      holds the class equal to upstream's minus that one parameter, so a release that adds a
      parameter fails until the patch carries it.
    - `longhorn-csi-secret-bind.yaml` and `longhorn-csi-secret-role.yaml` are `$patch: delete`
      patches (#5969). They drop upstream's ClusterRoleBinding and ClusterRole that give
      `longhorn-csi-service-account` cluster-wide `get` on every Secret. Upstream needs that
      grant only for a StorageClass with a `csi.storage.k8s.io/*-secret-*` parameter, and no
      class here has one. A class that adds one needs both patches removed first, and
      `tests/longhorn/test_longhorn_csi_secret_grant.py::test_no_longhorn_class_needs_the_dropped_grant`
      fails until they are.
      `tests/longhorn/test_longhorn_csi_secret_grant.py::test_the_patched_render_is_clean`
      fails a render in which the account still reads Secrets cluster-wide. Under
      `prune: false` the live objects stay until the operator deletes them (**operator-only**).
- **Three templates hold the Settings, the no-backup class and the metrics policy** (H1.5b).
    - `templates/settings.yaml.j2` renders the eight `Setting` objects, each from one
      `k3s_longhorn_*` key in `defaults/main.yml`. Flux owns each Setting's `value`. longhorn-manager keeps its own
      annotation and status on the object, and the render declares neither, so Flux never takes
      them.
    - `templates/storageclass-nobackup.yaml.j2` renders the `longhorn-nobackup` class with
      `kustomize.toolkit.fluxcd.io/force: enabled`, so Flux deletes and recreates the class when
      an immutable parameter changes.
    - `templates/networkpolicy-metrics.yaml.j2` renders the policy that admits Prometheus to
      the manager's metrics port.
- **A DataEngineSpecific Setting's value is a JSON map serialised as a string.**
  `default-replica-count` and both `snapshot-data-integrity` Settings render
  `{"v1":"...","v2":"..."}` in the compact form the manager stores, so the render equals the
  live value.
  `tests/longhorn/test_snapshot_data_integrity_patch_is_a_json_map.py::test_a_rendered_setting_carries_its_default_as_a_json_map_string`
  holds the shape of all three Settings, and
  `tests/longhorn/test_snapshot_data_integrity_patch_is_a_json_map.py::test_a_bare_string_value_is_flagged`
  is its red half.
- **The CSI components' memory requests are a Setting, not a patch** (#5958, #8326).
  `system-managed-csi-components-resource-limits` carries
  `k8s/roles/longhorn/defaults/main.yml:k3s_longhorn_csi_sidecar_memory_request` for
  `csi-attacher`, `csi-provisioner`, `csi-resizer` and `csi-snapshotter`,
  `k8s/roles/longhorn/defaults/main.yml:k3s_longhorn_csi_plugin_memory_request` for the
  `longhorn-csi-plugin` container, and
  `k8s/roles/longhorn/defaults/main.yml:k3s_longhorn_csi_node_sidecar_memory_request` for
  `node-driver-registrar` and `longhorn-liveness-probe`. The driver deployer creates those
  objects at runtime, so no patch can reach them. longhorn-manager treats the Setting as the
  only source of those containers' `resources` and clears anything else hourly, so a second
  writer makes each object roll on every tick. Each entry is a `requests` block alone.
  Changing a plugin value rolls the plugin DaemonSet one pod at a time, and RWX shares stay
  mounted through it; the comment above the plugin key has the evidence.
  `tests/longhorn/test_longhorn_control_plane_resources.py::test_the_csi_setting_requests_the_defaults_values_for_every_csi_component`
  holds the shape.
- **Two kinds of Longhorn pod stay BestEffort, because Longhorn offers no knob for them.**
  longhorn-manager builds the engine-image DaemonSet with no `resources` and no setting that
  sets them (#8500). The backup and trim Job pods are short-lived, and the RecurringJob
  controller builds their CronJobs with no `resources` either.
- **The engine-upgrade limit stays above 0.** Upstream ships `0`, which leaves engines on the old
  image after an upgrade, and this repo sets `3`.
  `tests/longhorn/test_engine_upgrade_limit_is_positive.py::test_a_zero_limit_is_flagged`
  fails a render that sets `0`, and `docs/longhorn-upgrade.md` step 4 relies on it.
- **A change to a Setting is a change to the live value once the tag promotes.** A hand patch is
  reverted at the next reconcile. To fire the integrity check early, land a one-shot schedule
  and then its revert (the comment on `k3s_longhorn_snapshot_data_integrity_cronjob` has the
  reason).
- **The file declares no `caBundle`, no webhook configuration and no Secret.** Under
  server-side apply a field the render does not declare stays with the controller that writes
  it, so the role needs no patch for a runtime-written field.
- **The tag renders `prune: false`** (`flux_prune: false`, with the `DECIDED:` marker beside it
  in `defaults/main.yml`). A render that dropped a CRD would make Flux delete every Volume of
  its kind. The cost: an object removed from the render, or retired by a later release, stays
  live until the operator deletes it (**operator-only**).
- **A StorageClass whose parameters change needs an operator delete.** StorageClass parameters
  are immutable, and the Ansible task that deleted the class when its parameters differed is
  gone. If the manager does not replace a class whose ConfigMap changed, the fix is an
  **operator-only** delete of the class, which the manager recreates from the ConfigMap.
- **`manifests_rollout_timeout` is `10m`** (`defaults/main.yml`). The DaemonSet rolls one
  pod at a time, so a roll is two pod starts in sequence. Both pods restarting at once with the
  image cached measured 36s on 2026-10-22 (#5922), which is one pod's start. A version bump also cold-pulls the image on each node in turn,
  and the Kustomization's `wait: true` waits for the driver deployer and both Deployments too.
  A timeout on this forward-only tag writes `DEPLOY_HOLD`, so the value carries that headroom.
- **`NEVER_SHED` names the tag** in `scripts/renderer/template_filters/service_tier.py`. The role is
  untiered, and the lost-node break-glass must not scale the storage control plane down.
  `tests/k8s/test_shed_set_is_derived_from_tier.py::test_never_shed_matches_the_untiered_roles_that_never_prune`
  holds the set equal to the untiered roles that set `flux_prune: false`.

## Host tasks and the backup plane
Two host components install Longhorn's host half on each node: `k3s-server`
(`hosts/components/k3s-server/component.yml`) on daniel-box and `k3s-agent`
(`hosts/components/k3s-agent/component.yml`) on daniel-server. Each installs the `open-iscsi`,
`nfs-common` and `multipath-tools` packages and the multipathd drop-in
`/etc/multipath/conf.d/50-homelab-longhorn.conf`. No host file writes a
Longhorn object that Flux owns: this role owns the Settings and classes, the CSI sidecar
requests among them (#5958), `longhorn-reconcile` the labels, and `longhorn-backup` the
credentials, the backup targets and the RecurringJobs. `k3s-server`'s `legacy:` list removes
the manifests once staged under /etc/rancher/k3s, these and the CoreDNS, MetalLB and RBAC
ones, so no file remains for a hand `kubectl apply -f` to write a Flux-owned object from.
The first bring-up run skips only `kubeconfig`. `docs/flux-deploy-plane.md`, *Bootstrapping Flux on a
rebuilt cluster*, has the steps.
