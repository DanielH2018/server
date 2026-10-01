# headlamp — plugins, and the measurements behind the OIDC login rules

The role's operating rules are `ansible/roles/k8s/headlamp/CLAUDE.md`: the two-identities
grant, and the OIDC values that must agree. This page holds the plugin mechanics and the
measurements each login rule was derived from, kept off the file the inject hook loads on
every touch of the role (#2985).

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
  label on the Service in `roles/k8s/observability`, and queries it through the API server's
  service proxy, so `templates/rbac.yaml.j2` carries a Role in `observability` granting `get`
  on `services/proxy` pinned to `prometheus:9090`. The network hop is the API server's,
  admitted by `netpol_baseline_obs_node_cidrs`, not by anything on the headlamp pod. Label,
  Service name/port and the Role's `resourceNames` must agree;
  `ansible/tests/k8s/test_k8s_manifests_rbac.py` checks they do, because a mismatch shows as
  empty charts with no error.
- **Charts are off per browser.** The "Show Prometheus metrics" button on a workload's detail
  page toggles them, stored in that browser's localStorage; nothing in IaC can pre-enable it.
- **CPU, network and filesystem charts read `No Data`; memory works.** The plugin hardcodes
  `rate(...[1m])` for the pod-level counters, and `kubernetes-cAdvisor` scrapes at 1m (the
  retention note at the kube-state-metrics job in `observability/templates/prometheus.yaml.j2`),
  so the window holds one sample. Measured 2026-09-06 through the proxy: `[1m]` returned 0
  series for the headlamp pod, `[2m]` and `[5m]` returned 1, `container_memory_working_set_bytes`
  returned 1. A 30s cAdvisor interval would fix it at +357 samples/s (cAdvisor was 357 of
  1,659 samples/s that day), roughly a fifth less retention window.

## The issuer is the PUBLIC name, and Headlamp's code is what forces one value

Headlamp reads `-oidc-idp-issuer-url` once at server start and builds one provider for every
request (`oidcAuthConfig.IdpIssuerURL`, `backend/cmd/headlamp.go` at v0.45.0) — there is no
per-request issuer. Authelia's `iss` meanwhile follows the host the request arrived on
(measured 2026-09-10: `auth.local.<domain>` on the LAN name, `auth.<domain>` publicly), so
this one value decides where logins on BOTH hostnames go. The LAN name sent public-route
logins to a host an off-LAN browser cannot resolve; the public name works from either side.
The cost is that a LAN login now traverses Cloudflare, so an edge outage takes it out.

The API server trusts both Authelia issuers, which is why the issuer choice is no longer a
lockout. `k3s_oidc_issuer_urls` (`roles/setup/k3s`) lists both, and an
`AuthenticationConfiguration` file is the only kube-apiserver mechanism that accepts more
than one — the `--oidc-*` flags took exactly one and are mutually exclusive with the file.

## The callback is NOT pinned, and that is the working state

`headlamp_k8s_oidc_callback_url` is empty, so Headlamp derives the `redirect_uri` from each
request (`getOidcCallbackURL` reads the request host and `X-Forwarded-Proto`) and one instance
serves both hostnames. Both URIs are registered on the Authelia client to match. Pinning it
sends every login to one hostname's callback, which is half of what broke the public route.

## Why the switch replaces the ServiceAccount identity

The Deployment's two argument sets are alternatives: on, the four `-oidc-*` flags render and
`-unsafe-use-service-account-token` is gone. Headlamp accepts both at once — it puts a
TokenFile and an OidcConf on the same context — and the result is the SA still authorising
every API call behind a sign-in button, which is why the template branches rather than
appends.

`automountServiceAccountToken` stays true even with the SA-token flag gone: `-in-cluster`
reads the API server address and the cluster CA from that same projected mount, and errors
without it.

## Why the client secret is an env var, and why `openid` is omitted

`headlamp_oidc_client_secret` is the plaintext, rendered into the `headlamp-oidc` Secret and
read as `HEADLAMP_CONFIG_OIDC_CLIENT_SECRET`; `headlamp_oidc_client_secret_hash` is the pbkdf2
digest in Authelia's config. It is an env var rather than a seventh flag because arguments are
part of the pod spec, and the cluster's read-only ServiceAccount can read Deployments.

`headlamp_k8s_oidc_scopes` omits `openid` because Headlamp prepends it
(`backend/cmd/headlamp.go` at v0.45.0), so listing it sends it twice. `groups` is the scope
that carries the RBAC subject.
