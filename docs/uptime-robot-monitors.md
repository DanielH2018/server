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

One half is checkable from here. `ansible/tests/repo/test_uptime_robot_keywords.py` parses the
monitor table below and asserts, against the live public endpoint, that each documented keyword
rule holds; it is marked `ui`, so it runs on demand (`uv run pytest -m ui -k uptime_robot`) and
never in CI. It cannot see the console, so a monitor deleted there stays invisible to it.

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
*Why two monitors* below; the 2026-08-30 restart is what prompted it, when four monitors
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

`docs/archive/kopia-disaster-recovery.md` and `docs/longhorn-disaster-recovery.md` both named monitor
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

## Why two monitors

On 2026-08-30 one restart produced four Uptime Robot alerts for one fact: the edge was not
answering. Hosts went down at 07:36:31, Traefik reached Ready at 07:43:57 and Authelia at
07:45:06, so the edge was down for 8m35s. That is past every 5-minute check interval, and
all four monitors sat behind that one ingress, so each was certain to fire.

- **No failure threshold exists to raise.** Uptime Robot has no "alert after N failures"
  setting on any plan. It retries a connection failure 3 times 20 seconds apart, an
  HTTP-status or keyword failure 3 times 10 seconds apart, and an SSL error not at all. That
  absorbs seconds, not minutes. A longer interval degrades detection of a real outage without
  reliably stepping over a restart.
- **The paid-plan lever is a postponed notification**: a per-alert-contact delay in minutes
  under *show advanced options*. A 15-minute delay would have silenced all four. It is a
  subscription decision, not a config change.
- **The free-plan lever is fewer monitors**, and that is what changed: four became two.

Each of the four services already held an on-prem Kuma tile at a 60-second interval, and Kuma
answers whether the service is healthy. Uptime Robot adds the one fact no tile can report:
whether the outside world reaches the house (DNS, Cloudflare, the public route, the WAN link).

- **`Auth Health` stayed as the edge probe** (then named `Auth Keyword`). It exercises DNS,
  Cloudflare, Traefik, TLS and Authelia in one check, and Authelia is the sign-on gate that
  every authed service depends on.
- **`Jellyfin Health` stayed** because remote streaming is Jellyfin's purpose, so external
  reachability is a distinct fact for it.
- **`Littlelink` and `Home Assistant Keyword` were deleted.**

None of the four sat behind Authelia (recorded 2026-08-30 from `containers_list`), so each
probe got a real 200. The service that does sit behind Authelia is `homepage`, where a probe
sees only Authelia's 302. That proves the edge routes and nothing beyond it, which is the trap
`docs/archive/kopia-disaster-recovery.md` records, so do not restore the backstop there.

The collapse gives up one case: if a single service's public route breaks while the edge stays
healthy, nothing external notices. The case is narrow, because every route renders from the
same `ingressroute.yml.j2` macro and breaks together far more often than singly. A restart now
costs two alerts instead of four.

**Do not aim a keyword monitor at Authelia's portal page.** The login UI is a JavaScript app
and Uptime Robot fetches raw HTML without executing it, so the body is a shell with an empty
`<title>` and no login text. The script filename carries a build hash and the `csp-nonce`
changes per request. `/api/health` answers a question instead, and `/api/state` on the same
host returns the same `"status":"OK"` beside the session fields.

## If this should stop being un-auditable

The way out is an `uptimerobot_api_key` in SOPS and a reconcile script under
`scripts/diagnostics/`, in the shape of the AutoKuma static-monitor files: monitors declared in the
repo, the script asserting the console agrees. Uptime Robot's v3 API can list and update monitors,
so the read half alone — a drift check that pages when the console stops matching this file — would
close most of the gap without any write credential leaving the repo.

That is not built. Adding it is a decision about whether a fifth externally held credential is
worth the audit trail, and it has not been made.
