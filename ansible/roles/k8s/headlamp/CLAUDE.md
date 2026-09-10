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

## Two identities, one grant
Each binding in `templates/rbac.yaml.j2` names two subjects: the ServiceAccount, and the
`headlamp_k8s_oidc_group` Group. They are deliberately the same grant twice.

- **The SA is what the dashboard uses today.** `-unsafe-use-service-account-token` makes the
  pod browse as its own account, and the Authelia forward-auth Middleware on the IngressRoute
  is the perimeter in front of it.
- **The Group is for OIDC, and it grants nobody anything yet.** Under OIDC, Headlamp does not
  authorise: it forwards the browser's `id_token` and the **API server** decides. The login
  therefore arrives as a `User` in a `Group` carrying none of the SA's RBAC. Nothing in this
  cluster authenticates as a Group today, because the API server has no
  `--kube-apiserver-arg=oidc-*` flags — that is part 1 of #1390, which lives in
  `roles/setup/k3s` and is applied by hand through `k3s-bringup.yml`.
- **A binding the Group is missing from is an empty resource list**, behind a login that
  succeeded, with nothing logged. `test_headlamp_oidc_group_is_bound_wherever_the_serviceaccount_is`
  in `ansible/tests/k8s/test_k8s_manifests_rbac.py` is the guard; adding a fourth binding for
  the SA alone is what it exists to catch.
- **`headlamp_k8s_oidc_group` is two things concatenated** — the API server's
  `oidc-groups-prefix` plus the Authelia group — so it is not a free choice. The prefix is what
  stops an Authelia group name from being read as a built-in `system:` group.

## OIDC login (off)
`headlamp_k8s_oidc_enabled: false`. Everything the login needs is in the repo; the switch is
off because the third piece is not in this repo at all.

- **Turning it on REPLACES the ServiceAccount identity, it does not add to it.** The
  Deployment's two argument sets are alternatives: on, the five `-oidc-*` flags render and
  `-unsafe-use-service-account-token` is gone. Headlamp accepts both at once — it puts a
  TokenFile and an OidcConf on the same context — and the result is the SA still authorising
  every API call behind a sign-in button, which is why the template branches rather than
  appends.
- **It cannot work until the API server trusts Authelia.** Headlamp forwards the browser's
  `id_token`; the API server accepts or rejects it. With no `--kube-apiserver-arg=oidc-*` flags
  on daniel-box, arming this leaves a dashboard nobody can use. That is part 1 of #1390, in
  `roles/setup/k3s` (`k3s_oidc_*`), applied by hand through `k3s-bringup.yml`.
- **Three values must agree across three places**, and none of the disagreements produces an
  error: the client id (here, Authelia's client, the API server's `oidc-client-id`), the issuer
  URL (here, the API server's `oidc-issuer-url`), and the group (`headlamp_k8s_oidc_group`, the
  API server's `oidc-groups-prefix` plus the Authelia group).
- **Both URLs pin the LAN name, and that is a constraint rather than a preference.** Authelia's
  `iss` follows the host the authorization request arrived on — measured 2026-09-10, the
  discovery document returns `auth.local.<domain>` on the LAN name and `auth.<domain>` publicly
  — while `oidc-issuer-url` compares one value exactly. The callback is pinned for the same
  reason: Headlamp otherwise builds it from the request host, so a public-route login would
  send an unregistered `redirect_uri`.
- **`headlamp_k8s_oidc_scopes` omits `openid` on purpose.** Headlamp prepends it
  (`backend/cmd/headlamp.go` at v0.45.0), so listing it sends it twice. `groups` is the scope
  that carries the RBAC subject.
- **The client secret is one credential in two forms.** `headlamp_oidc_client_secret` is the
  plaintext, rendered into the `headlamp-oidc` Secret and read as
  `HEADLAMP_CONFIG_OIDC_CLIENT_SECRET`; `headlamp_oidc_client_secret_hash` is the pbkdf2 digest
  in Authelia's config. Rotate them together. It is an env var rather than a seventh flag
  because arguments are part of the pod spec, and the cluster's read-only ServiceAccount can
  read Deployments.
- **`automountServiceAccountToken` stays true** even with the SA-token flag gone: `-in-cluster`
  reads the API server address and the cluster CA from that same projected mount, and errors
  without it.

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
  `templates/oidc-secret.yaml.j2` (rendered under `no_log` through the manifests role's secret list),
  `templates/networkpolicy.yaml.j2`, `templates/netpol-probe-job.yaml.j2`,
  `templates/ingressroute.yaml.j2`, `templates/service.yaml.j2`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "headlamp"`.
