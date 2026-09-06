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

## Plugins
- The upstream image ships no plugins. `headlamp_k8s_plugins` (defaults) lists each one by
  name, version and tarball sha256; the `fetch-plugins` init container in
  `templates/deployment.yaml.j2` downloads them from `headlamp-k8s/plugins` releases on every
  pod start, verifies the digest, and unpacks into the `/headlamp/plugins` emptyDir. A bad
  digest or an unreachable GitHub fails the pod rather than starting a plugin-less dashboard.
- **Adding one is one entry in that list.** Renovate opens version bumps (the `headlamp
  plugins` manager in `renovate.json`, `automerge: false`); the sha256 is not published with
  the release, so the operator finishes the PR with the commands in the defaults header.
- **prometheus** — charts on workload detail pages. It finds Prometheus by the
  `headlamp-prometheus: "true"` label on the Service in `roles/k8s/claude-otel`, and queries it
  through the API server's service proxy, so `templates/rbac.yaml.j2` carries a Role in
  `observability` granting `get` on `services/proxy` pinned to `prometheus:9090`. The network
  hop is the API server's, admitted by `netpol_baseline_obs_node_cidrs`, not by anything on
  the headlamp pod. Label, Service name/port and the Role's `resourceNames` must agree;
  `ansible/tests/k8s/test_k8s_manifests_rbac.py` checks they do, because a mismatch shows as
  empty charts with no error.

## Notable
- Ships a **negative** self-test: `templates/netpol-probe-job.yaml.j2` is a Job that must
  FAIL to reach headlamp from a non-traefik pod, proving `networkpolicy.yaml.j2` actually
  fences it. It probes a traefik control connection first as a sanity check, so a failure is
  attributable to the policy rather than to DNS or a dead pod.
- `headlamp_k8s_session_ttl: 86400` — how long a browser session survives before Headlamp
  re-reads the ServiceAccount token.

## Editing
- Manifests: `templates/deployment.yaml.j2`, `templates/rbac.yaml.j2` (cluster identity plus the Prometheus proxy Role),
  `templates/networkpolicy.yaml.j2`, `templates/netpol-probe-job.yaml.j2`,
  `templates/ingressroute.yaml.j2`, `templates/service.yaml.j2`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "headlamp"`.
