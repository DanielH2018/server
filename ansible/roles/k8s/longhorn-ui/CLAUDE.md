# longhorn-ui — the door in front of the upstream Longhorn UI

Route-only role. Longhorn itself (namespace, Deployments, Services) is installed by
`roles/setup/k3s` from the upstream manifest — this role adds only the IngressRoute and the
middlewares it references, nothing else.

## At a glance
- **Deploy tag:** `--tags "longhorn-ui"`.
- **Route:** `longhorn.local.<domain>` — LAN only, Authelia.
- **Persists:** nothing — renders no Deployment of its own.
- **`manifests_rollout: ''`** — there's no Deployment to wait on, so the shared rollout gate is
  told explicitly there's nothing to roll.
- **`k8s_autodeploy: false`** — the Longhorn UI it fronts has no auth of its own, so a bad
  route change (auth dropped, wrong Service targeted) is a platform-class exposure risk even
  though this role has no workload to roll back.

## Editing
- Route/middlewares: `templates/ingressroute.yaml.j2`, `templates/middlewares.yaml.j2`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "longhorn-ui"`.
