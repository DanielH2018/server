# headlamp — read-only Kubernetes dashboard

Headlamp, browsing the cluster with the built-in `view` ClusterRole plus a handful of CRD
groups it doesn't cover. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/headlamp-k8s/headlamp` (`headlamp_k8s_image`), digest-pinned.
- **Deploy tag:** `--tags "headlamp"`. Route: `headlamp.<domain>` (Authelia).
- **Storage:** none — no PVC, stateless RollingUpdate Deployment.
- **Auto-deploy:** eligible (`k8s_autodeploy: true`) — stateless, readinessProbe,
  digest-pinned image.
- **RBAC:** `templates/rbac.yaml.j2` grants the built-in `view` ClusterRole plus
  `headlamp_k8s_crd_api_groups` (`traefik.io`, `metallb.io`, `longhorn.io`, `helm.cattle.io`,
  `k3s.cattle.io`) — nothing aggregates a CRD group into `view` automatically, so a group
  missing from that list degrades silently: the UI loads, that resource list is just empty.

## Notable
- Ships a **negative** self-test: `templates/netpol-probe-job.yaml.j2` is a Job that must
  FAIL to reach headlamp from a non-traefik pod, proving `networkpolicy.yaml.j2` actually
  fences it. It probes a traefik control connection first as a sanity check, so a failure is
  attributable to the policy rather than to DNS or a dead pod.
- `headlamp_k8s_session_ttl: 86400` — how long a browser session survives before Headlamp
  re-reads the ServiceAccount token.

## Editing
- Manifests: `templates/deployment.yaml.j2`, `templates/rbac.yaml.j2`,
  `templates/networkpolicy.yaml.j2`, `templates/netpol-probe-job.yaml.j2`,
  `templates/ingressroute.yaml.j2`, `templates/service.yaml.j2`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "headlamp"`.
