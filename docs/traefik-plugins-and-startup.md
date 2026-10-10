# Traefik record

`ansible/roles/k8s/traefik/CLAUDE.md` is the role doc, and it keeps the rules. This page is the working-out behind them, in two halves. The first half covers startup: the outage that bought the startupProbe, the measured red path, what Traefik's plugin manager does at every process start, and how to change the in-repo local plugin. The second half covers client identity and TLS: the incidents that fixed each `X-Forwarded-For`, origin-pull and 421 rule, the verification commands, and the source trail for `allowEmptyServices`. A session reads it when it edits the plugin, probe, routing or TLS wiring (#2989).

## The outage the startupProbe exists for

`/ping` answers 200 from the moment the process starts. On 2026-09-06 the boot-time download
of the CrowdSec bouncer plugin timed out, the `crowdsec` Middleware the `https` entrypoint names
resolved to `invalid middleware type`, every router on that entrypoint was rejected, and the
pod sat 3/3 Ready serving 404 to the whole fleet for 3.5 hours (#1322). Traefik does not retry
the download.

The probe targets `edge-selfcheck-ingressroute.yaml.j2`, a router of Traefik's own on the
`https` entrypoint backed by `ping@internal`, so the same failure that kills every app route
kills it too and kubelet restarts the container instead.

Three constraints hold it together, all ENFORCED by
`ansible/tests/services/test_traefik_edge_selfcheck.py`: the route stays on an entrypoint
whose chain names the `crowdsec` Middleware, the probe path stays the route's own, and the
route matches on a path rather than a Host. A kubelet probe sends no SNI, and Traefik
answers a matched `Host()` router with 421 when SNI and Host disagree.

## The red path is measured — do not re-run the outage

Measured 2026-09-10 on `traefik:v3.7.12`, with `traefik_k8s_bouncer_plugin_version: v1.7.999`
(nonexistent) and `failureThreshold: 6` deployed for four minutes:

- The pod never became Ready and the container restarted every ~30s, reaching restart 4 in
  2m10s. `deploy.sh --tags traefik` failed at the rollout wait rather than reporting green.
- `https://<podIP>:8443/.well-known/traefik-edge-selfcheck` returned 404 throughout, and every
  app router on the `https` entrypoint logged `invalid middleware
  "homelab-crowdsec@kubernetescrd" configuration: invalid middleware type or middleware does
  not exist` — the routing table was gone, not merely unhealthy.
- Restarts 3 and 4 each logged `Loading plugins...` followed by `unable to download plugin
  github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin: error: 500` one second later. Each
  restart makes a fresh HTTP request, so the shared `/plugins-storage` emptyDir does **not**
  short-circuit recovery and no per-container-start wipe is needed.
- Recovery: reverting both values and redeploying restored a 1/1 pod with `restarts=0`,
  `selfcheck=200`, and 302/200 on the LAN routes. Total edge outage 11:53Z–11:57Z.
- Expected boot noise, not a defect: the selfcheck route has no `Host()`, so Traefik logs
  `No domain found in rule PathPrefix(...)` and falls back to the default TLSOption for it.

**Why each restart re-downloads** (#1345), from Traefik v3.7.12's own `pkg/plugins`:
`NewManager` calls `resetDirectory` on the sources root at every process start;
`RegistryDownloader.Download` always issues the GET, using an existing archive only as an
`X-Plugin-Hash` validator that a partial file fails; and `SetupRemotePlugins` calls
`ResetAll()` — which empties both the GoPath and the archives directory — on any install
failure.

## Why the bouncer reports no usage metrics

Under plugin v1.7.1 the default 600s metrics ticker intermittently stops the 60s stream ticker
for 600s or 1200s, silently, and a ban or an unban made in that window does not reach the edge
(#2752). The `DECIDED: no usage-metrics ticker` comment in `templates/dynamic.yaml.j2` has the
evidence and upstream's issue.

The ticker is a package global inside the plugin, so a Middleware edit alone does not stop a
running one. The Traefik pod has to restart, and the manifests rollout-restart does that.

## Changing the in-repo local plugin

`cloudflare-realip` is an in-repo Traefik **local** plugin (`files/cloudflare-realip/`),
projected from the `traefik-cloudflare-realip` ConfigMap to `/plugins-local/src/<module>/`. It
is deliberately not a downloaded plugin, so it adds no second startup download beside the
bouncer's, and it is gated with the bouncer so the startupProbe covers a failed load of it
too.

A name mismatch between `experimental.localPlugins`, the Middleware, the ConfigMap items and
the manifest's `import` disables every plugin at startup, and `--dry-run` cannot see that.
`ansible/tests/k8s/test_traefik_cloudflare_realip.py` pins the names.

To change the Go source, run it first under the release binary of `traefik_k8s_image`'s
version with a local `plugins-local/` tree: a Yaegi error appears only at Traefik startup, as
`Plugins are disabled because an error has occurred`.

## The CrowdSec init containers

The traefik pod runs three CrowdSec init containers ahead of its agent sidecar, and their order is load-bearing. `docs/crowdsec-waf-record.md` (*Sidecar agent seeding*) holds what each one works around, for this pod and for authelia's.

## The access log's `ClientHost` is a chain, and readers take its rightmost entry

The access-log handler wraps outside the entrypoint middlewares, so it records `ClientHost`
before `cloudflare-realip` runs (#2446). From a Cloudflare source the field reads
`<client-sent>, <client>`: Cloudflare merges any client-sent header lines and appends the
address it saw, so only the rightmost entry is not client-chosen. From any other source
`forwardedHeaders` has already dropped the header, which leaves the connection address.

Two readers take that rightmost entry. The CrowdSec agent sidecar's
`crowdsecurity/traefik-logs` parser does from hub version 1.5 (hub PR #1665), and the
sidecar's `hub upgrade` floats that version at every start rather than pinning it.
`ansible/roles/k8s/crowdsec/files/remote_allowlist.py:authenticated_clients` takes the same
entry, so the allowlist holds the address CrowdSec bans.

On 2026-09-22 a scanner sent `X-Forwarded-For: 127.0.0.1` through Cloudflare, and Traefik
logged `127.0.0.1,195.178.110.72`. The agent's single-event alert on that line named
195.178.110.72.

**A harness that sends `X-Forwarded-For` straight from a trusted source proves nothing about
either reader**: it skips Cloudflare's append, so its leftmost entry is also its rightmost.

## Cloudflare origin-pull, and the two traps the guard exists for

`ansible/templates/origin-pull.yml.j2` renders the `cloudflare-origin-pull` TLSOption
(`modern` plus `clientAuth: RequireAndVerifyClientCert`) and the `cloudflare-origin-pull-ca`
Secret it verifies against, from `files/cloudflare-origin-pull-ca.crt` — Cloudflare's
published CA, public, provenance and expiry (2029-11-01) in the file header (#1990).

The zone has Authenticated Origin Pulls on, so Cloudflare presents the certificate on every
origin connection; a direct client with a public SNI fails the handshake even from inside
`cloudflare_ips`, which is all the #1974 netpol on :8443 checks. Traefik picks TLS options by
SNI, so the `ingressroute()` macro renders the public and `.local.` hosts as two objects
(`<name>-public` and `<name>`) and only the public one names the option; a LAN or WireGuard
client holds no certificate.

`ansible/tests/k8s/test_public_routes_require_cloudflare_origin_pull.py` exists for two traps:

- Every router on ONE public host must name the SAME option, or Traefik falls back to the
  default options, which require nothing — the bypass documents and healthchecks' ping twin
  share hosts with the main objects.
- The option and its Secret resolve in the ROUTE's namespace, so observability carries the
  observability copies for `grafana`.

A request whose Host header maps to a different option than its SNI (no SNI, or a `.local.`
SNI with a public Host) is answered 421 before any router sees it, so the certificate cannot
be dodged by handshaking as a LAN name.

To verify from the LAN: `curl -k --resolve <svc>.<domain>:443:<VIP>
https://<svc>.<domain>/` fails the handshake, `https://<svc>.local.<domain>/` answers without
a certificate, and the public name through Cloudflare answers.

## Why a 421 outlives the misconfiguration that caused it

Traefik's SNICheck records the TLS-options name once per CONNECTION, at handshake time, and
answers 421 to every later request on that connection whose router declares a different name
(#2747, #2749). A handshake whose SNI maps to no router records `default`, so a long-lived
client that connects while its host has no router gets 421 forever once the router returns
naming `modern`.

On 2026-09-27 that wedged the Pi's Alloy (`loki.write` dropping every batch) and both of the
kube-apiserver's OIDC discovery fetches, for 4h15m and 5h30m respectively, while a fresh
`curl` to the same URLs answered 200 throughout. The only recovery is to make the client
redial, which is why a restart fixes it and a config change does not.

The request never reaches a backend, so the per-service 5xx and latency checks are blind and
`check_traefik_404_flood` counts a different code. monitor-bridge's `check_traefik_421` reads
the per-router 421 rate instead and pages when one stays up for 15 minutes (#2757).

## The router-less window `allowEmptyServices` closes

The CRD provider drops a route whose Service has no endpoints (`no servers found for
homelab/loki-homelab`, 07:49:00Z–07:49:35Z on the reboot, 16s before the first 421 — #2747,
#2758). `static-config.yaml.j2` keeps the router instead, so the handshake records the
router's own option and the client retries through a 503; its `DECIDED: keep a router` marker
has the source trail.

Traefik itself serves nothing before its first CRD sync, because the startupProbe that gates
readiness targets an IngressRoute. A 421 can still arise when a router is rejected for another
reason, such as a missing Middleware, so the redial recovery above still applies. This is
reasoned from Traefik's source; the next weekly reboot is its measurement.
