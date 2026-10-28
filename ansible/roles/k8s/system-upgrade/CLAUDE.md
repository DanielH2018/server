# system-upgrade — system-upgrade-controller and the two k3s Plans

The controller (SUC), its Plan CRD and two Plans, `k3s-server` and `k3s-agent`, in the
`system-upgrade` namespace. SUC replaces the k3s binary on the nodes the Plans select. Nothing
else upgrades k3s since H6.3c and H6.3d landed. The slice plan is
`docs/target-architecture/host/h6-k3s-suc.md`, and the decision is
`docs/adr/0025-suc-upgrades-k3s-and-homelab-k3s-restart-restarts-it.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=system-upgrade` with exit 5
- **Images:** `rancher/system-upgrade-controller` (`system_upgrade_controller_image`), `python`
  (`system_upgrade_gate_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (H6.1b). Its `containers_list` entry carries `owner: flux`
  from its first commit, so `deploy.sh` refuses `--tags=system-upgrade` with exit 5.
  `tasks/main.yml` holds the include of `k8s/manifests` and no Flux path runs it.
- **The Plans select the nodes in `system_upgrade_nodes`.** The list in `defaults/main.yml` holds
  `daniel-box` since H6.3c and `daniel-server` since H6.3d. Both Plans render an empty list as
  `no-node-selected`, a hostname no node carries, and setting the list back to empty is the
  durable pause.
  `ansible/roles/k8s/system-upgrade/tests/test_system_upgrade_role.py` holds that an empty
  list matches no node.
- **The k3s pin lives in `defaults/main.yml`.** `k3s_version`, `k3s_install_script_url` and
  `k3s_install_script_sha256` moved here from `setup/k3s`, so a pin change routes to this
  Flux-owned tag and the deployer writes no `manual_plane` line. `setup/k3s` reads the file
  with `include_vars` for the fresh-host install. `SYSTEM_UPGRADE_DEFAULTS` in
  `scripts/lib/repo_paths.py` names it for the Python readers.
- **The controller and the CRD are vendored** under `files/upstream/`, byte for byte, pinned by
  `system_upgrade_controller_version` and the `system_upgrade_vendored_manifests` items in
  `defaults/main.yml`. `scripts/dev/vendor_manifest.py` checks the pin and refreshes the copies.
  No render may fetch a URL (D5, C1).
- **Two patches change upstream objects**, both under `files/patches/`. A kustomize patch is
  copied verbatim and cannot read a variable, so
  `ansible/roles/k8s/system-upgrade/tests/test_system_upgrade_role.py` holds each literal equal
  to its key in `defaults/main.yml`.
    - `system-upgrade-controller.yaml` pins the controller image by digest and sets the pod's
      `priorityClassName` to the platform tier's class.
    - `system-upgrade-controller-env.yaml` sets the Job deadline bounds (3600 and 5400) and the
      Job backoff limit (`system_upgrade_job_backoff_limit`).
      The controller reads its environment at start, so a later change to either bound or to
      the backoff limit needs a restart of the controller pod, which the Kustomization does not
      trigger. A change to this patch lands with every selected node paused through
      `homelab/k3s-upgrade-paused`. The operator restarts the controller, waits for its
      rollout, and only then removes the label, so no Job starts under the old environment.
- **The tag renders `prune: false`** (`flux_prune: false`, with the `DECIDED:` marker beside it
  in `defaults/main.yml`), because its render holds the Plan CRD and
  `scripts/renderer/tests/test_renderer_crd_census.py` refuses a CRD under prune. A Plan
  removed from the render stays live until the operator deletes it (**operator-only**):
  `kubectl -n system-upgrade delete plan <name>`.
- **The tag is forward-only** (`flux_forward_only: true`). A failed or timed-out revision holds
  without a reset, so `promote` never lowers the Plans below a version a node already runs.
- **The Kustomization judges the Plans.** `flux_health_check_exprs` and `flux_timeout` in
  `defaults/main.yml` render as `spec.healthCheckExprs` and `spec.timeout`
  (`scripts/lib/k8s_roles.py:role_fields`). The expressions compare `status.latestVersion` with
  the pin after SUC's `+` to `-` mapping, because a Plan has no `observedGeneration`.
  A selector-only change keeps the version and the hash, so for H6.3c, H6.3d and exit 1 the
  evidence is each node's `plan.upgrade.cattle.io/<plan>` label against the Plan's
  `status.latestHash`, never the Kustomization.
- **The gates' identity is `k3s-plan`** (`templates/rbac.yaml.j2`): a ClusterRole that reads
  nodes, Longhorn volumes and backups and Plans, and a Role in `kube-system` that reads the
  `coredns` Deployment and the `kube-controller-manager` Lease by `resourceNames`. SUC hashes
  `serviceAccountName` into a Plan's `latestHash`, so the account landed while no node is
  selected.
- **The namespace denies all ingress** (`templates/networkpolicy.yaml.j2`). The controller
  declares no container port, so no netpol-baseline probe row can prove the policy live, and
  the render test is the whole proof (H6 *Open questions* 13).
- **`prepare` runs a script no host ships yet.** `/host/usr/lib/homelab/k3s_gates.py` arrives
  with H6.3a. A Plan that selected a node before then would fail `prepare`, the closed
  direction.

## Autonomous-role contract

SUC and `homelab-k3s-restart` change state with no human in the loop. This section is their
contract (H6 *Autonomous-role contract*); `homelab-k3s-restart` arrives with H6.3b.

- **Scope.** SUC replaces the k3s binary with the pinned version on a selected node, and never
  drains, cordons or downgrades. The restart unit restarts one k3s unit once per changed config
  hash, inside the window.
- **Mode and arming.** `system_upgrade_nodes`, the node pause label `homelab/k3s-upgrade-paused`,
  and the restart timer's mask.
- **Abort valves.** The `prepare` gates, the Job deadline, the restart unit's gates with their
  SUC and Flux in-flight checks, its deploy lock, and one attempt per hash, a killed attempt
  included.
- **Required evidence.** The Plans' `Complete` condition and `flux/system-upgrade`, the restart
  unit's notice lines and textfile metrics, and the five alerts. `K3sUpgradeJobFailed` is in
  `ansible/roles/k8s/observability/files/rules/k3s-upgrade.yaml`.
- **Next-run review.** Read the last bump's Plan conditions and the restart unit's journal
  before widening the scope or narrowing the window.
