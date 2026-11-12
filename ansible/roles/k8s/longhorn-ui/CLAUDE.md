# longhorn-ui — the door in front of the upstream Longhorn UI

Route-only role. Longhorn itself (namespace, Deployments, Services) is the `longhorn` role's
vendored upstream manifest (H1.5a), which Flux applies — this role adds only the IngressRoute
and the middlewares it references, nothing else.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`platform: k8s`); `land.sh --tags=longhorn-ui` gates
  the rollout
- **Route:** `longhorn.local.<domain>` (LAN only), Authelia two_factor
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (D5.6, the second Wave A PR). Its `containers_list` entry
  is `platform: k8s`, so nothing applies it by hand, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle.
- **Where the task went.** Its three vars map onto the render:
    - The prune namespace is gone from the apply. Every object carries
      `namespace: longhorn-system` in its template, and kustomize-controller applies them
      there; its `cluster-reconciler-flux-system` binding grants `cluster-admin`
      (`ansible/roles/k8s/flux/files/gotk-components.yaml`). Flux's prune follows its own
      inventory, not a namespace.
    - `manifests_rollout: ''` has nothing to replace. There is no Deployment of ours to roll,
      so the Kustomization's `wait` checks only that the route and middlewares applied.
    - The render honours `manifests_exclude_files: [service.yaml]`, so the bundle holds no
      Service. The route targets the upstream `longhorn-frontend` Service, as before.
- **A bad route change** (auth dropped, wrong Service targeted) is a platform-class exposure
  risk even though this role has no workload to roll back. Under Ansible a route change
  reached the cluster only through a hand deploy. Under Flux every merged change applies on
  the next promotion, so review is the gate. `k8s_autodeploy: false` stays in
  `defaults/main.yml` with its reason, but it gates nothing here: the deployer skips the tag,
  and the role has no image for Renovate to automerge.

## Editing
- Route/middlewares: `templates/ingressroute.yaml.j2`, `templates/middlewares.yaml.j2`.
