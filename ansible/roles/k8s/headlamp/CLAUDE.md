# headlamp — read-only Kubernetes dashboard

Headlamp, browsing the cluster with the built-in `view` ClusterRole plus a handful of CRD
groups it doesn't cover. See repo-root `CLAUDE.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "headlamp"`
- **Images:** `ghcr.io/headlamp-k8s/headlamp` (`headlamp_k8s_image`), `alpine`
  (`headlamp_k8s_netpol_probe_image`)
- **Route:** `headlamp.<domain>` · `headlamp.local.<domain>`, Authelia one_factor
- **Claims:** none (no PVC)
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **Eligible for auto-deploy because** it is a stateless RollingUpdate Deployment with a
  readinessProbe and a digest-pinned image.
- **RBAC:** `templates/rbac.yaml.j2` grants the built-in `view` ClusterRole plus
  `headlamp_k8s_crd_api_groups` (`traefik.io`, `metallb.io`, `longhorn.io`, `helm.cattle.io`,
  `k3s.cattle.io`) — nothing aggregates a CRD group into `view` automatically, so a group
  missing from that list degrades silently: the UI loads, that resource list is just empty.

## Two identities, one grant
Each binding in `templates/rbac.yaml.j2` names two subjects: the ServiceAccount, and the
`headlamp_k8s_oidc_group` Group. They are deliberately the same grant twice.

- **The Group is what the dashboard uses since 2026-09-10.** Under OIDC, Headlamp does not
  authorise: it forwards the browser's `id_token` and the **API server** decides. The login
  arrives as a `User` in a `Group` carrying none of the SA's RBAC, which is why the Group is
  bound everywhere the SA is. The API server accepts those tokens from an
  `AuthenticationConfiguration` trusting Authelia — `roles/setup/k3s`, applied by hand.
- **The SA is the fallback the other branch uses.** With `headlamp_k8s_oidc_enabled: false`,
  `-unsafe-use-service-account-token` makes the pod browse as its own account and the Authelia
  forward-auth Middleware on the IngressRoute is the perimeter in front of it. Keep the SA
  bound: it is what the dashboard falls back to, and the Prometheus proxy Role is the SA's.
- **A binding the Group is missing from is an empty resource list**, behind a login that
  succeeded, with nothing logged. `test_headlamp_oidc_group_is_bound_wherever_the_serviceaccount_is`
  in `ansible/tests/k8s/test_k8s_manifests_rbac.py` is the guard; adding a fourth binding for
  the SA alone is what it exists to catch.
- **`headlamp_k8s_oidc_group` is two things concatenated** — the API server's groups prefix
  plus the Authelia group — so it is not a free choice. The prefix stops an Authelia group
  name from reading as a built-in `system:` group.

## OIDC login (on)
`headlamp_k8s_oidc_enabled: true` since 2026-09-10. Three pieces are live: the API server's
trust in Authelia (`roles/setup/k3s`), the Authelia client, and these defaults.
`docs/headlamp-oidc-and-plugins.md` carries the measurements each rule below came from.

- **This switch is not independently safe to flip.** It depends on state in `roles/setup/k3s`,
  which the k8s deploy play never runs and a person applies through `k3s-bringup.yml`. Turning
  it on without that trust leaves a dashboard that logs in and Forbids every call; if the trust
  is ever removed, turn this off in the same change.
- **On REPLACES the ServiceAccount identity, it does not add to it.** The Deployment branches
  between the two argument sets rather than appending, because Headlamp accepts both at once
  and then authorises every call as the SA behind a sign-in button.
- **A wrong value fails silently.** A bad issuer leaves the API server up and rejecting only
  OIDC logins, since the root kubeconfig and every ServiceAccount authenticate by other means.
  Verify a change here by logging in, not by a healthy pod.
- **Three values must agree across three places**, and none of the disagreements produces an
  error: the client id (here, Authelia's client, and the `audiences` of each issuer in the API
  server's authentication config), the issuer URL (here, and one of that config's `jwt`
  issuers), and the group (`headlamp_k8s_oidc_group`, and that config's groups prefix plus the
  Authelia group).
- **The issuer is the PUBLIC name**, `auth.<domain>`, and one value serves both hostnames:
  Headlamp builds one provider at server start while Authelia's `iss` follows the request host.
  The API server trusts both Authelia issuers (`k3s_oidc_issuer_urls`), so the choice is not a
  lockout.
- **The callback is NOT pinned, and that is the working state.**
  `headlamp_k8s_oidc_callback_url` is empty, so one instance serves both hostnames; pinning it
  sends every login to one hostname's callback.
- **The client secret is one credential in two forms** — `headlamp_oidc_client_secret` and
  `headlamp_oidc_client_secret_hash`. Rotate them together.
- **`headlamp_k8s_oidc_scopes` omits `openid` on purpose**; Headlamp prepends it. `groups` is
  the scope that carries the RBAC subject.

## Plugins
`docs/headlamp-oidc-and-plugins.md` is the full account. Three rules stay here:

- **The image bundles the Prometheus plugin.** Read `GET /plugins` on the live pod before
  adding one; the first cut of this role installed a second copy of a plugin already shipped.
- **Extra plugins are one entry each in `headlamp_k8s_plugins`** (name, version, sha256). The
  `fetch-plugins` init container renders only when the list is non-empty, and a bad digest or
  an unreachable GitHub fails the pod rather than starting a plugin-less dashboard.
- **The Prometheus charts read `No Data` for CPU, network and filesystem; memory works.** The
  plugin hardcodes `rate(...[1m])` against a 1m cadvisor scrape, so the window holds one
  sample. Charts are off per browser too, in localStorage; no IaC can pre-enable them.

## Notable
- Ships a **negative** self-test: `templates/netpol-probe-job.yaml.j2` is a Job that must
  FAIL to reach headlamp from a non-traefik pod, proving `networkpolicy.yaml.j2` fences it. It
  probes a traefik control connection first, so a failure is attributable to the policy rather
  than to DNS or a dead pod.
- `headlamp_k8s_session_ttl: 86400` — how long a browser session survives before Headlamp
  re-reads the ServiceAccount token.

## Editing
- Manifests: everything under `templates/`. Two carry a rule of their own —
  `rbac.yaml.j2` holds the cluster identity plus the Prometheus proxy Role, and
  `oidc-secret.yaml.j2` renders under `no_log` through the manifests role's secret list.
