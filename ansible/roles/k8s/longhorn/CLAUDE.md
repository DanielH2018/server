# longhorn — the storage control plane and the `longhorn` StorageClass

Longhorn's manager DaemonSet, its CRDs, its admission webhook and driver deployer, and the
ConfigMap that tells the manager which `longhorn` StorageClass to create. Every PVC in the
cluster binds through it. A bad apply here detaches volumes under every stateful service while
host probes stay green.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=longhorn` with exit 5
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (H1.5a). Its `containers_list` entry carries `owner: flux`
  from its first commit. `tasks/main.yml` holds the include of `k8s/manifests` and no Flux path
  runs it. A rebuilt cluster gets it from the `longhorn` Kustomization at step 6 of
  `docs/flux-deploy-plane.md`'s *Bootstrapping Flux on a rebuilt cluster*. The `longhorn-backup` Kustomization
  depends on it and creates the backup targets once it reads `Ready`.
- **The workloads are `files/upstream/longhorn.yaml`**, upstream's file byte for byte, pinned by
  `k3s_longhorn_version` and the `longhorn_vendored_manifests` item in `defaults/main.yml`.
  `scripts/dev/vendor_manifest.py` checks the pin and refreshes the copy. The refresh command
  sits above the pin. No render may fetch a URL (D5, C1).
- **Merging a bump of `k3s_longhorn_version` is the upgrade** (H1 Open question 6). `land.sh`
  runs the `longhorn-upgrade` gates before it merges, Longhorn supports no downgrade and one
  minor per hop, so read `docs/longhorn-upgrade.md` before you bump the pin.
- **Two patches change upstream objects**, both under `files/patches/`. The renderer lists them
  as the Kustomization's `patches:`, and each names its target by kind, name and namespace.
    - `longhorn-manager.yaml` adds a memory request and no limit to the manager container.
      A kustomize patch is copied verbatim and cannot read a variable, so
      `ansible/tests/longhorn/test_longhorn_control_plane_resources.py::test_manager_patch_holds_the_value_of_the_defaults_key`
      holds the literal equal to `k3s_longhorn_manager_memory_request`.
    - `longhorn-storageclass.yaml` replaces the class inside upstream's `longhorn-storageclass`
      ConfigMap, which longhorn-manager reads to create the `longhorn` class. The class omits
      `numberOfReplicas`, because a StorageClass parameter beats the `default-replica-count`
      setting. `ansible/tests/longhorn/test_longhorn_storageclass.py::test_the_class_differs_from_upstreams_only_by_the_replica_count`
      holds the class equal to upstream's minus that one parameter, so a release that adds a
      parameter fails until the patch carries it.
- **Three templates hold the objects `setup/k3s` used to write** (H1.5b).
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
  `ansible/tests/longhorn/test_snapshot_data_integrity_patch_is_a_json_map.py::test_a_bare_string_value_is_flagged`
  holds the shape.
- **The CSI sidecars' memory request is a Setting, not a patch** (#5958).
  `system-managed-csi-components-resource-limits` carries
  `ansible/roles/k8s/longhorn/defaults/main.yml:k3s_longhorn_csi_sidecar_memory_request` for
  `csi-attacher`, `csi-provisioner`, `csi-resizer` and `csi-snapshotter`. longhorn-manager
  treats the Setting as the only source of those four containers' `resources` and clears
  anything else hourly, so a second writer makes each Deployment roll on every tick. The value
  names those four components with a `requests` block alone.
  `ansible/tests/longhorn/test_longhorn_control_plane_resources.py::test_the_csi_setting_requests_the_defaults_value_for_the_four_sidecars_only`
  holds the shape.
- **The engine-upgrade limit stays above 0.** Upstream ships `0`, which leaves engines on the old
  image after an upgrade, and this repo sets `3`.
  `ansible/tests/longhorn/test_engine_upgrade_limit_is_positive.py::test_a_zero_limit_is_flagged`
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
- **`manifests_rollout_timeout` is `10m`** (`defaults/main.yml`). The DaemonSet rolls with
  `maxUnavailable: 100%`, so both manager pods are replaced at once, and a cached restart
  measured 36s on 2026-10-22 (#5922). A version bump also cold-pulls the image on every node,
  and the Kustomization's `wait: true` waits for the driver deployer and both Deployments too.
  A timeout on this forward-only tag writes `DEPLOY_HOLD`, so the value carries that headroom.
- **`NEVER_SHED` names the tag** in `ansible/filter_plugins/service_tier.py`. The role is
  untiered, and the lost-node break-glass must not scale the storage control plane down.
  `ansible/tests/k8s/test_shed_set_is_derived_from_tier.py::test_never_shed_matches_the_untiered_roles_that_never_prune`
  holds the set equal to the untiered roles that set `flux_prune: false`.

## Host tasks and the backup plane
The host tasks `setup/k3s` keeps for Longhorn (`open-iscsi`, `nfs-common` and the multipathd
blacklist) carry the tag `longhorn_host` alone. No `setup/k3s` task writes a Longhorn object that
Flux owns: this role owns the Settings and classes, the CSI sidecar requests among them
(#5958), `longhorn-reconcile` the labels, and `longhorn-backup` the credentials, the backup
targets and the RecurringJobs. A fourth `longhorn_host` task removes the manifests that retired
tasks staged under /etc/rancher/k3s, these and the CoreDNS, MetalLB, cluster-base and traefik
ones, so no file remains for a hand `kubectl apply -f` to write a Flux-owned object from. The stale-checkout guard holds no Longhorn row, and the first
bring-up run skips only `kubeconfig`. `docs/flux-deploy-plane.md`, *Bootstrapping Flux on a
rebuilt cluster*, has the steps.
