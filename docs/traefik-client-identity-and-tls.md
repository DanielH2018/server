# Traefik client identity and TLS at the edge

`ansible/roles/k8s/traefik/CLAUDE.md` is the role doc, and it keeps the rules: which entry of
`X-Forwarded-For` a reader may trust, that every public router names the origin-pull TLSOption,
and that a 421 is cured by a redial. This page is the working-out behind them — the incidents
that fixed each rule, the verification commands, and the source trail for
`allowEmptyServices`. A session reads it when it edits routing or TLS wiring (#2989).

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
