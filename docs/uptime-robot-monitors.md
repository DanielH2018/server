# Uptime Robot — the off-premises reachability monitors

Uptime Robot is the third alerting system in this homelab, alongside Uptime Kuma (on-prem, the
spine) and Healthchecks.io (off-site, dead-man's switches). It watches the public hostnames from
outside the house, which is the one thing neither of the others can do.

**Nothing here is managed by Ansible.** There is no provisioning task, no Terraform, no API key in
SOPS — every monitor was created in the console by hand, and this file is the only record of what
they are set to. That is a real gap and it is stated here rather than hidden: a monitor deleted or
reconfigured in the console leaves no trace in git, and the repo keeps reading as though it exists.
The vendored `community.general.uptimerobot` module is not a way out; it speaks the retired v1 API
and no playbook uses it.

Changing anything below means opening <https://dashboard.uptimerobot.com> and editing it there.

## What is configured

**Two monitors, live since 2026-08-30.** Both confirmed up by the account holder that day.

| Monitor | Id | Target | Keyword |
|---|---|---|---|
| Auth Health | `803868101` | `https://auth.daniel-hunter.com/api/health` | `"status":"OK"` must exist |
| Jellyfin Health | `803868270` | `https://jellyfin.daniel-hunter.com/health` | `Healthy` must exist |

Both are **keyword** monitors with **case sensitivity ON**, and for Jellyfin that setting is
load-bearing rather than a preference — see *Why Jellyfin's keyword must be case-sensitive* below.

Measured against the live public routes on 2026-08-30:

| Endpoint | Status | Content type | Body |
|---|---|---|---|
| `auth…/api/health` | 200 | `application/json` | `{"status":"OK"}` |
| `jellyfin…/health` | 200 | `text/plain` | `Healthy` — 7 bytes, no trailing newline |

Neither response is edge-cached (`cf-cache-status: DYNAMIC`, and Jellyfin's carries
`cache-control: no-store, no-cache`), so both monitors read the origin rather than a Cloudflare
copy that could stay green through an outage.

**Two were retired the same day**, `Littlelink` and `Home Assistant Keyword`. Both probed through
the same Traefik edge as the two above and so reported nothing the edge probe does not, and both
keep a 60-second on-prem Kuma tile (`k3s littlelink`, `k3s Home Assistant`). The reasoning is under
*What was collapsed, and why* below; the 2026-08-30 restart is what prompted it, when four monitors
sent four alerts for one 8m35s outage.

### Why Jellyfin's keyword must be case-sensitive

**`Unhealthy` contains `healthy`.** Jellyfin's `/health` is an ASP.NET health-check endpoint, so
its body is one of `Healthy`, `Degraded` or `Unhealthy` — and a case-INSENSITIVE keyword of
`Healthy`, set to alert when the word is missing, matches the `healthy` inside `Unhealthy` and
holds the monitor green through exactly the fault it was created to catch.

Case sensitivity is a per-monitor toggle in the console and it is ON for this monitor. Turning it
off does not degrade the check, it inverts it. If Uptime Robot ever removes the toggle, this
endpoint stops being a safe target for a must-exist keyword and the monitor should move to
`Unhealthy` **must not exist**, which has no such collision.

`Auth Health` does not share the hazard: Authelia answers a failure with `{"status":"KO"}`, and
`"status":"OK"` is not a substring of that in any casing. Its case sensitivity is a consistency
choice rather than a correctness one.

### Why `/health` rather than the root

`https://jellyfin.daniel-hunter.com/` answers **302** to `web/`, which resolves to 200 — Jellyfin's
own redirect, not an auth gate, so a plain HTTP monitor there was honest. `/health` is better on
two counts: it survives a change to that root redirect, and it asserts the application's own health
checks passed rather than that something returned a status code.

### The recorded disaster-recovery backstop is gone

`docs/kopia-disaster-recovery.md` and `docs/longhorn-disaster-recovery.md` both named monitor
`803270234`, probing `https://homepage.daniel-hunter.com`, as the ONE backstop for a total
in-house monitoring death. **It no longer exists.** The two live ids are `803868101` and
`803868270`, neither of which is it, and it was not among the four monitors that preceded them
either — so it was deleted at some point nothing recorded, and both runbooks were promising a
safety net that was not there.

`Auth Health` (`803868101`) is the backstop now, and it is a better one than the id it replaces.
The kopia runbook's own requirement was that the backstop must not be a generic Traefik-served
URL, because a host and Traefik staying up while the alert brain dies would still return 200. The
old target was `homepage`, the one service that IS behind Authelia, so an external probe there
only ever saw the middleware's 302 — the runbook recorded that as a known residual. The new target
returns Authelia's own `{"status":"OK"}`.

**This is the failure the top of this file describes, arriving.** A monitor was deleted, nothing in
the repo could notice, and two runbooks kept citing it for an unknown number of months. The id
column in the table above exists so the next occurrence is one console glance to detect.

## Why one restart produced four alerts (the 2026-08-30 event)

All four sat behind the same Traefik ingress, so they did not report four facts. They reported one:
the edge was not answering.

Measured on 2026-08-30 — hosts went down at 07:36:31, Traefik's pod reached Ready at 07:43:57,
and the last workload (Authelia) at 07:45:06. That is **8m35s of edge downtime**, comfortably past
any 5-minute check interval, so every monitor pointed through that edge was guaranteed to fire.

### There is no failure threshold to raise

Uptime Robot exposes no "alert after N consecutive failures" setting, on any plan. Its
confirmation logic is fixed and internal: a connection failure is retried up to 3 times 20 seconds
apart, an HTTP-status or keyword failure up to 3 times 10 seconds apart, and an SSL error not at
all. That absorbs a blip of **seconds**. It cannot absorb minutes.

So on the free plan the arithmetic has no slack in it. The interval floor is 5 minutes, the
restart costs 8m35s, and at least one check is therefore guaranteed to fail and alert. Raising the
interval is not a fix either — it degrades detection of a real outage without reliably stepping
over a restart, which can land anywhere in the cycle.

**The paid-plan lever is a postponed notification**, not a failure count: a per-alert-contact
delay in minutes, set under *show advanced options* on a monitor (the settings icon beside each
alert contact), and settable in bulk. A 15-minute delay would have silenced all four of these
monitors, since the outage ended at 8m35s. It is a paid feature and worth knowing about before
concluding this plane cannot be quieted — but it is a subscription decision, not a config change.

Which leaves one lever that works on the plan in use today.

**Run fewer monitors**, which is what was done. Four probes through one ingress buy no coverage
one of them does not already have; what they buy is four notifications per edge event.

## None of the four sat behind Authelia

Worth stating because the opposite is the natural assumption, and it is what decided which two
survived. From `containers_list` in `ansible/inventory/host_vars/daniel-box.yml`:

| Monitor | Host | `use_authelia` | What a probe gets |
|---|---|---|---|
| Auth Keyword | `auth` | false | a real 200 — it *is* Authelia |
| Jellyfin Monitor | `jellyfin` | false | a real 200 from Jellyfin, which owns its own auth |
| Home Assistant Keyword | `home-assistant` | false | a real 200 from HA, likewise |
| Littlelink | `www` | false | a real 200 from a static page |

The service that *is* behind Authelia is `homepage` — the one the DR docs name and which is not
live. An Uptime Robot probe there counts Authelia's 302 as up, so it proves the edge is routing
and nothing beyond it: the weakest possible check, and the trap already recorded in
`docs/kopia-disaster-recovery.md`. If the backstop is restored, do not restore it there.

## What was collapsed, and why

**Four monitors became two on 2026-08-30.** Every one of the four services already held an on-prem
Kuma tile at a 60-second interval — `k3s Authelia Portal`, `k3s littlelink`, `k3s Home Assistant`,
`k3s Jellyfin (VIP)`. Kuma answers whether the service is healthy. The only thing Uptime Robot
adds that none of them can is **whether the outside world can reach the house** — DNS, Cloudflare,
the public route, the WAN link. That is one fact, and it was being reported four times.

- **`Auth Keyword` stayed, as the edge probe**, aimed at Authelia's health API. It exercises DNS,
  Cloudflare, Traefik, TLS and Authelia in one check, and Authelia is the single sign-on gate every
  authed service depends on.
- **`Jellyfin Monitor` stayed.** Jellyfin is the one service whose *external* reachability is the
  point — streaming from outside the house — so "reachable from the internet" is a distinct fact
  for it rather than a second copy of the edge's.
- **`Littlelink` and `Home Assistant Keyword` were deleted.**

**What that gave up, stated plainly:** if a single service's *public* route breaks while the edge
stays healthy, nothing external notices. That is a narrow case — every route is rendered from the
same `ingressroute.yml.j2` macro, so they break together far more often than singly — and both
dropped services keep their 60-second on-prem tile. Jellyfin is the exception kept precisely
because remote use is what it is for.

A restart now costs two Uptime Robot alerts instead of four.

## The keyword for the auth monitor

**URL:** `https://auth.daniel-hunter.com/api/health`
**Keyword:** `"status":"OK"` — exists, not does-not-exist.

Verified 2026-08-30 against the live public route: `HTTP 200`, body `{"status":"OK"}`.
`/api/state` is the richer alternative on the same host, returning the same `"status":"OK"`
alongside the session fields and `default_redirection_url`.

**Do not point a keyword monitor at the portal page itself.** Authelia's login UI is a
JavaScript app and Uptime Robot fetches raw HTML without executing it, so the response is a shell
containing `<noscript>You need to enable JavaScript to run this app.</noscript>`, an empty
`<div id="root">` and — measured, not assumed — an **empty `<title>`**. There is no login text in
it to match. Every visible string a person would think to use as the keyword is absent from what
the monitor actually receives.

The other candidates in that shell are worse than the API for a reason each: the script filename
carries a build hash (`index.CXslS62G.js`) that changes on every Authelia release, and the
`csp-nonce` value is regenerated per request. The `data-` attributes on `<body>` are stable but
prove only that Authelia served a file. `{"status":"OK"}` is Authelia answering a question.

## If this should stop being un-auditable

The way out is an `uptimerobot_api_key` in SOPS and a reconcile script under
`scripts/diagnostics/`, in the shape of the AutoKuma static-monitor files: monitors declared in the
repo, the script asserting the console agrees. Uptime Robot's v3 API can list and update monitors,
so the read half alone — a drift check that pages when the console stops matching this file — would
close most of the gap without any write credential leaving the repo.

That is not built. Adding it is a decision about whether a fifth externally held credential is
worth the audit trail, and it has not been made.
