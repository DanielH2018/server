# traefik — the cluster ingress edge

Traefik terminates TLS and installs the IngressRoute/Middleware CRDs every other k8s
role depends on. See repo-root `CLAUDE.md` for shared conventions, and the "Where to
Look" table's note that this role must render before anything referencing its CRDs.

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
  racing to write it would corrupt it).

## Notable
- **A startupProbe, not `/ping`, is what proves this pod has routers.** `/ping` answers 200
  from the moment the process starts. On 2026-09-06 the boot-time download of the CrowdSec
  bouncer plugin timed out, the `crowdsec` Middleware the https entrypoint names resolved to
  "invalid middleware type", every router on that entrypoint was rejected, and the pod sat
  3/3 Ready serving 404 to the whole fleet for 3.5 hours (#1322). Traefik does not retry the
  download. The probe targets `edge-selfcheck-ingressroute.yaml.j2`, a router of Traefik's own
  on the https entrypoint backed by `ping@internal`, so the same failure that kills every app
  route kills it too and kubelet restarts the container instead. Three constraints hold it
  together, all ENFORCED by `ansible/tests/services/test_traefik_edge_selfcheck.py`: the route
  stays on an entrypoint whose chain names the crowdsec Middleware, the probe path stays the
  route's own, and the route matches on a path rather than a Host (a kubelet probe sends no
  SNI, and Traefik answers a matched `Host()` router with 421 when SNI and Host disagree).
- **That probe's red path is measured, and each container restart re-downloads the plugin
  (#1345). Do not re-run the outage.** Measured 2026-09-10 on `traefik:v3.7.12`, with
  `traefik_k8s_bouncer_plugin_version: v1.7.999` (nonexistent) and `failureThreshold: 6`
  deployed for four minutes:
  - The pod never became Ready and the container restarted every ~30s, reaching restart 4 in
    2m10s. `deploy.sh --tags traefik` failed at the rollout wait rather than reporting green.
  - `https://<podIP>:8443/.well-known/traefik-edge-selfcheck` returned 404 throughout, and every
    app router on the https entrypoint logged `invalid middleware
    "homelab-crowdsec@kubernetescrd" configuration: invalid middleware type or middleware does
    not exist` — the routing table was gone, not merely unhealthy.
  - Restarts 3 and 4 each logged `Loading plugins...` followed by `unable to download plugin
    github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin: error: 500` one second later. Each
    restart makes a fresh HTTP request, so the shared `/plugins-storage` emptyDir does **not**
    short-circuit recovery and no per-container-start wipe is needed.
  - Why, from Traefik v3.7.12's own `pkg/plugins`: `NewManager` calls `resetDirectory` on the
    sources root at every process start; `RegistryDownloader.Download` always issues the GET,
    using an existing archive only as an `X-Plugin-Hash` validator that a partial file fails;
    and `SetupRemotePlugins` calls `ResetAll()` — which empties both the GoPath and the archives
    directory — on any install failure.
  - Recovery: reverting both values and redeploying restored a 1/1 pod with `restarts=0`,
    `selfcheck=200`, and 302/200 on the LAN routes. Total edge outage 11:53Z–11:57Z.
  - Expected boot noise, not a defect: the selfcheck route has no `Host()`, so Traefik logs
    `No domain found in rule PathPrefix(...)` and falls back to the default TLSOption for it.
- **The https entrypoint rewrites a Cloudflare request's X-Forwarded-For to
  CF-Connecting-IP, first in its chain.** Cloudflare appends the client address to any XFF
  the client sent, so without it the leftmost entry is client-chosen. Authelia logs that
  entry as `remote_ip`, and the CrowdSec agent bans on it. The `cloudflare-realip`
  Middleware is an in-repo Traefik **local** plugin (`files/cloudflare-realip/`), projected
  from the `traefik-cloudflare-realip` ConfigMap to `/plugins-local/src/<module>/`. It is
  deliberately not a downloaded plugin, so it adds no second startup download beside the
  bouncer's. It is gated with the bouncer, so the startupProbe that catches a failed plugin
  load covers it too. A name mismatch between `experimental.localPlugins`, the Middleware,
  the ConfigMap items and the manifest's `import` disables every plugin at startup. `--dry-run`
  cannot see that, so `ansible/tests/k8s/test_traefik_cloudflare_realip.py` pins the names.
  To change the Go source, run it first under the release binary of `traefik_k8s_image`'s
  version with a local `plugins-local/` tree: a Yaegi error appears only at Traefik startup,
  as `Plugins are disabled because an error has occurred`.
- **The access log's `ClientHost` is the whole X-Forwarded-For chain, and every reader takes
  its rightmost entry (#2446).** The access-log handler wraps outside the entrypoint
  middlewares, so it records `ClientHost` before `cloudflare-realip` runs. From a Cloudflare
  source the field reads `<client-sent>, <client>`. Cloudflare merges any client-sent header
  lines and appends the address it saw, so only the rightmost entry is not client-chosen. From
  any other source forwardedHeaders has already dropped the header, which leaves the
  connection address. The CrowdSec agent sidecar's `crowdsecurity/traefik-logs` parser takes
  the rightmost entry from hub version 1.5 (hub PR #1665). The sidecar's `hub upgrade` floats
  that version at every start rather than pinning it.
  `ansible/roles/k8s/crowdsec/files/remote_allowlist.py:authenticated_clients` takes the same
  entry, so the allowlist holds the address CrowdSec bans. On 2026-09-22 a scanner sent
  `X-Forwarded-For: 127.0.0.1` through Cloudflare, and Traefik logged
  `127.0.0.1,195.178.110.72`. The agent's single-event alert on that line named
  195.178.110.72. A harness that sends X-Forwarded-For straight from a trusted source skips
  Cloudflare's append, so its leftmost entry is also its rightmost and proves nothing about
  either reader.
- **Only Cloudflare is a trusted forwarder** (`forwardedHeaders.trustedIPs`). `lan_subnet`
  was dropped on 2026-09-24; the `DECIDED:` comment in `static-config.yaml.j2` has why.
- **Every public `x.<domain>` router requires Cloudflare's origin-pull client certificate
  (#1990).** `ansible/templates/origin-pull.yml.j2` renders the `cloudflare-origin-pull`
  TLSOption (`modern` plus `clientAuth: RequireAndVerifyClientCert`) and the
  `cloudflare-origin-pull-ca` Secret it verifies against, from
  `files/cloudflare-origin-pull-ca.crt` — Cloudflare's published CA, public, provenance and
  expiry (2029-11-01) in the file header. The zone has Authenticated Origin Pulls on, so
  Cloudflare presents the certificate on every origin connection; a direct client with a
  public SNI fails the handshake even from inside `cloudflare_ips`, which is all the #1974
  netpol on :8443 checks. Traefik picks TLS options by SNI, so the `ingressroute()` macro
  renders the public and `.local.` hosts as two objects (`<name>-public` and `<name>`) and
  only the public one names the option; a LAN or WireGuard client holds no certificate. Two
  traps the guard (`ansible/tests/k8s/test_public_routes_require_cloudflare_origin_pull.py`)
  exists for: every router on ONE public host must name the SAME option, or Traefik falls back
  to the default options, which require nothing — the bypass documents and healthchecks'
  ping twin share hosts with the main objects; and the option and its Secret resolve in the
  ROUTE's namespace, so claude-otel carries the observability copies for grafana. A request
  whose Host header maps to a different option than its SNI (no SNI, or a `.local.` SNI with
  a public Host) is answered 421 before any router sees it, so the certificate cannot be
  dodged by handshaking as a LAN name. To verify from the LAN: `curl -k --resolve
  <svc>.<domain>:443:<VIP> https://<svc>.<domain>/` fails the handshake,
  `https://<svc>.local.<domain>/` answers without a certificate, and the public name through
  Cloudflare answers.
- An initContainer runs `chmod 600 /data/acme.json` on every start: kubelet's `fsGroup`
  handling ORs group bits into every file on the volume at mount time, which flips
  Traefik's own `0600` back to `0660` and makes it refuse to load the ACME account.
- An initContainer stages the CrowdSec image's bundled hub tree into `/etc/crowdsec`,
  owned by the pod uid, and it runs **before** the config seed below. That rsync runs as the
  pod's own uid and cannot read the root-only staged hub, so the parser configs it does
  copy — symlinks into that tree — resolved to nothing and the agent dropped
  `geoip-enrich` on every start (#1211). Ordering is the fix: after the rsync, the hub
  directory already exists owned by the pod uid, and root with `ALL` dropped cannot
  write into it.
- A second initContainer seeds the CrowdSec bouncer sidecar's config
  (`traefik_k8s_manage_crowdsec`) — `crowdsec` deploys **before** traefik in
  `containers_list` specifically so its LAPI machine credential exists before this
  sidecar starts.
- A third initContainer copies the CrowdSec image's bundled datafiles into the agent's
  data volume, world-readable. The image ships them `0600 root:root` and its entrypoint
  symlinks rather than copies them, so the non-root sidecar could not read through the
  link and GeoIP never initialised. This container is the role's only `runAsUser: 0`,
  and it reaches nothing but that volume.
- Ports are unprivileged inside the pod (`8000`/`8443`/`8082`); the Service maps the
  public `80`/`443` to them, avoiding `NET_BIND_SERVICE`. `runAsUser` is pinned to
  `traefik_k8s_uid` (65532).
