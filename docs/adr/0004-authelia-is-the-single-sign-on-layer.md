---
id: "0004"
title: Authelia is the single sign-on layer, enforced at the edge
status: Accepted
date: 2026-08-01
governs: []
---

# ADR-0004: Authelia is the single sign-on layer, enforced at the edge

## Status

Accepted.

## Context

Most services here have either no authentication of their own or a weak per-app login, and
there are enough of them that per-app accounts are not a credible answer. Something had to
sit in front.

Traefik supports forward-auth as a middleware
([ADR-0005](0005-traefik-is-the-edge-with-ingressroute-crds.md)), so a gate at the edge costs
one middleware reference per route and requires nothing of the application behind it. That is
what makes it viable across roughly 30 services that were never written to be behind SSO.

Authelia also provides OIDC, so a service that *can* speak it gets real identity rather than
a proxy header.

## Decision

Authelia is the SSO layer. A route opts in through `use_authelia: true` on its inventory
entry, which makes the shared route macro attach the `authelia` forward-auth middleware.

Because everything else depends on it, Authelia is denylisted from auto-deploy: a failed
deploy of the gate locks out access to everything behind it, including the tools needed to
fix it.

## Consequences

**A 302 proves the edge is up and nothing about the backend.** The redirect fires in the
middleware before Traefik proxies, so any health check that accepts a 302 is checking
Authelia, not the service. This is the single most repeated trap in the repo and every
reference page that prints a route now says so.

**Authelia is a single point of failure for access.** Accepted, and mitigated by keeping it
off the auto-deploy path rather than by adding a bypass.

**A service that needs an unauthenticated path needs a second route.** Automated,
session-less callers do not fail loudly against a forward-auth gate — they get a redirect and
keep reporting success. Those paths are separate IngressRoute documents whose names must
appear in an allow-list with a written reason, so a new public no-auth path is a conscious
edit rather than a quiet template change.

**A sidecar inherits its pod's network identity.** The CrowdSec agent running inside the
Authelia pod arrives at other services as `app=authelia`, which matters when writing policy
that names it.

**Its storage encryption key is a pinned secret** — rotating it is a documented DANGER
procedure, not part of the automatic rotation tier.

## Governs

No single line. `governs:` is empty; the decision is expressed by `use_authelia` in the
inventory and by the middleware the route macro attaches.
