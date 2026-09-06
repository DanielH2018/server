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
- **The image bundles the Prometheus plugin.** `container/build-manifest.json` in
  `headlamp-k8s/headlamp` names `prometheus-0.9.1` for v0.45.0, and `GET /plugins` on the
  live pod lists it as `static-plugins/prometheus (shipped)`. Read that endpoint before
  adding a plugin; the plugin's README does not say which image bundles it, and the first
  cut of this role installed a second copy.
- **Extra plugins are one entry each in `headlamp_k8s_plugins`** (name, version, sha256).
  The `fetch-plugins` init container in `templates/deployment.yaml.j2` renders only when the
  list is non-empty; it downloads each `headlamp-k8s/plugins` release tarball at pod start,
  verifies the digest, and unpacks into the `/headlamp/plugins` emptyDir. A bad digest or an
  unreachable GitHub fails the pod rather than starting a plugin-less dashboard. Nothing
  tracks the versions while the list is empty; the defaults header names the Renovate
  manager to restore with the first entry, and the commands that finish its sha256.
- **Prometheus charts.** The plugin finds Prometheus by the `headlamp-prometheus: "true"`
  label on the Service in `roles/k8s/claude-otel`, and queries it through the API server's
  service proxy, so `templates/rbac.yaml.j2` carries a Role in `observability` granting `get`
  on `services/proxy` pinned to `prometheus:9090`. The network hop is the API server's,
  admitted by `netpol_baseline_obs_node_cidrs`, not by anything on the headlamp pod. Label,
  Service name/port and the Role's `resourceNames` must agree;
  `ansible/tests/k8s/test_k8s_manifests_rbac.py` checks they do, because a mismatch shows as
  empty charts with no error.
- **Charts are off per browser.** The "Show Prometheus metrics" button on a workload's detail
  page toggles them, stored in that browser's localStorage; nothing in IaC can pre-enable it.
- **CPU, network and filesystem charts read `No Data`; memory works.** The plugin hardcodes
  `rate(...[1m])` for the pod-level counters, and `kubernetes-cadvisor` scrapes at 1m (the
  retention note at the kube-state-metrics job in `claude-otel/templates/prometheus.yaml.j2`),
  so the window holds one sample. Measured 2026-09-06 through the proxy: `[1m]` returned 0
  series for the headlamp pod, `[2m]` and `[5m]` returned 1, `container_memory_working_set_bytes`
  returned 1. A 30s cadvisor interval would fix it at +357 samples/s (cadvisor was 357 of
  1,659 samples/s that day), roughly a fifth less retention window.

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
