# traefik — the cluster ingress edge

Traefik terminates TLS and installs the IngressRoute/Middleware CRDs every other k8s role
depends on, so it must render before any role whose manifests reference those CRDs.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "traefik"`
- **Images:** `traefik` (`traefik_k8s_image`), `alpine` (`traefik_k8s_logrotate_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `traefik-acme` (daily -> R2)
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — platform — ingress edge; a failed
  deploy removes the ability to reach or fix anything else, and host probes stay green through
  that kind of outage. COUPLING NOTE for a future promotion: the traefik-acme PVC holds the
  ACME account key and issued certs; reverting past a real rotation reinstates stale cert state
<!-- /generated_from -->

- **The dashboard has its own IngressRoute** (`dashboard-ingressroute.yaml.j2`).
- **`traefik-acme` is the `acme.json` cert store** (`Recreate`, ReadWriteOnce — two Traefiks
  writing it corrupt it).

The working-out behind the rules below sits on two pages, split by subject:
`docs/traefik-plugins-and-startup.md` (the plugin download, the probe's red path, the init
containers) and `docs/traefik-client-identity-and-tls.md` (the forwarded-header chain,
origin-pull, the 421 incidents).

## Plugins and the startupProbe

- **A startupProbe, not `/ping`, is what proves this pod has routers.** `/ping` answers 200
  from the moment the process starts, and a failed bouncer-plugin download leaves every router
  on the https entrypoint rejected behind a 3/3 Ready pod — a 3.5-hour fleet outage on
  2026-09-06 (#1322). The probe targets `edge-selfcheck-ingressroute.yaml.j2`, a router of
  Traefik's own backed by `ping@internal`, so the failure that kills every app route kills it
  too. Three constraints hold it together — the entrypoint, the path and matching on a path
  rather than a Host — and `ansible/tests/services/test_traefik_edge_selfcheck.py` ENFORCES
  all three.
- **Do not re-run the outage to see the red path — it is measured** (2026-09-10, #1345).
  Every container restart re-downloads the plugin, so the shared `/plugins-storage` emptyDir
  short-circuits nothing.
- **The bouncer runs with `metricsUpdateIntervalSeconds: 0`** (#2752): the default ticker
  silently stops the 60s decision stream for 600s or 1200s at a time, and a ban made in that
  window never reaches the edge. The `DECIDED: no usage-metrics ticker` comment in
  `templates/dynamic.yaml.j2` has the evidence, and
  `ansible/tests/services/test_traefik_bouncer_metrics_ticker_off.py` pins the value. Changing
  it needs a pod restart, not just a Middleware edit.
- **`cloudflare-realip` is an in-repo LOCAL plugin** (`files/cloudflare-realip/`), projected
  from a ConfigMap rather than downloaded, and gated with the bouncer so the startupProbe
  covers it. A name mismatch across `experimental.localPlugins`, the Middleware, the ConfigMap
  items and the manifest's `import` disables every plugin at startup, which `--dry-run` cannot
  see; `ansible/tests/k8s/test_traefik_cloudflare_realip.py` pins the names.

## Client identity and TLS

- **The https entrypoint rewrites a Cloudflare request's X-Forwarded-For to
  CF-Connecting-IP, first in its chain.** Cloudflare appends the client address to any XFF the
  client sent, so without it the leftmost entry is client-chosen — and Authelia logs that
  entry as `remote_ip` while the CrowdSec agent bans on it.
- **The access log's `ClientHost` is the whole X-Forwarded-For chain, and every reader takes
  its rightmost entry** (#2446) — the access-log handler wraps outside the entrypoint
  middlewares, so it records the field before `cloudflare-realip` runs. A harness that sends
  the header from a trusted source proves nothing about either reader.
- **Only Cloudflare is a trusted forwarder** (`forwardedHeaders.trustedIPs`); `lan_subnet`
  was dropped 2026-09-24, and `static-config.yaml.j2`'s `DECIDED:` comment has why.
- **Every public `x.<domain>` router requires Cloudflare's origin-pull client certificate**
  (#1990) — the `cloudflare-origin-pull` TLSOption, rendered by
  `ansible/templates/origin-pull.yml.j2`. Traefik picks TLS options by SNI, so the
  `ingressroute()` macro renders public and `.local.` hosts as two objects and only the public
  one names the option.
  `ansible/tests/k8s/test_public_routes_require_cloudflare_origin_pull.py` holds two traps:
  every router on one public host names the SAME option, and the option resolves in the
  ROUTE's namespace.
- **SNICheck pins the TLS-options name per CONNECTION, so a 421 outlives the misconfiguration
  that caused it** (#2747, #2749). **The only recovery is to make the client redial**, which
  is why a restart fixes it and a config change does not. It wedged the Pi's Alloy and the
  kube-apiserver's OIDC fetches for hours on 2026-09-27 while fresh curls answered 200.
  monitor-bridge's `check_traefik_421` pages on a per-router rate that stays up for 15 minutes
  (#2757).
- **`allowEmptyServices` keeps a router whose backend has no ready endpoints** (#2747,
  #2758), which is what opened that router-less window on a reboot. `static-config.yaml.j2`
  carries the `DECIDED: keep a router` marker, and
  `ansible/tests/services/test_traefik_watched_namespaces.py::test_empty_services_keep_their_router_is_clean`
  pins the flag.

## Pod plumbing

- An initContainer runs `chmod 600 /data/acme.json` on every start: kubelet's `fsGroup`
  handling ORs group bits into every file on the volume at mount time, which flips
  Traefik's own `0600` back to `0660` and makes it refuse to load the ACME account.
- **Three more initContainers set the CrowdSec sidecars up, and their ORDER is load-bearing**:
  the hub-tree rsync runs before the config seed, or the agent drops `geoip-enrich` on every
  start (#1211). The second seeds the bouncer's config (`traefik_k8s_manage_crowdsec`), which
  is why traefik's `containers_list` entry declares `depends_on: [crowdsec]`. The third copies
  the image's datafiles world-readable; it and the first are the pod's two `runAsUser: 0`
  containers. All three and the agent sidecar render from `ansible/templates/crowdsec-agent.yml.j2`,
  which authelia's pod shares. `docs/traefik-plugins-and-startup.md` has what each one is
  working around.
- Ports are unprivileged inside the pod (`8000`/`8443`/`8082`); the Service maps the
  public `80`/`443` to them, avoiding `NET_BIND_SERVICE`. `runAsUser` is pinned to
  `traefik_k8s_uid` (65532).
