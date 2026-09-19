# longhorn-ui — the door in front of the upstream Longhorn UI

Route-only role. Longhorn itself (namespace, Deployments, Services) is installed by
`roles/setup/k3s` from the upstream manifest — this role adds only the IngressRoute and the
middlewares it references, nothing else.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry. -->
- **Deploy tag:** `--tags "longhorn-ui"`
- **Route:** `longhorn.local.<domain>` (LAN only), Authelia two_factor
- **Claims:** none (no PVC)
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — renders no Deployment — route-only
  (IngressRoute + middlewares) in front of the Longhorn UI, which has no auth of its own; a bad
  route change risks exposing the storage control plane
<!-- /generated_from -->

- **`manifests_rollout: ''`** — there's no Deployment to wait on, so the shared rollout gate is
  told explicitly there's nothing to roll.
- **A bad route change** (auth dropped, wrong Service targeted) is a platform-class exposure
  risk even though this role has no workload to roll back — the denylist reason above.

## Editing
- Route/middlewares: `templates/ingressroute.yaml.j2`, `templates/middlewares.yaml.j2`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "longhorn-ui"`.
