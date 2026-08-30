# Uptime Robot — the off-premises reachability monitors

Uptime Robot is the third alerting system in this homelab, alongside Uptime Kuma (on-prem, the
spine) and Healthchecks.io (off-site, dead-man's switches). It watches the public hostnames from
outside the house, which is the one thing neither of the others can do.

**Nothing here is managed by Ansible.** There is no provisioning task, no Terraform, no API key in
SOPS — every monitor was created in the console by hand, and this file is the only record of what
they are set to. That is a real gap and it is stated here rather than hidden: a monitor deleted or
reconfigured in the console leaves no trace in git, and the repo will keep reading as though it exists.
The vendored `community.general.uptimerobot` module is not a way out; it speaks the retired v1 API
and no playbook uses it.

Changing anything below means opening <https://dashboard.uptimerobot.com> and editing it there.

## What is configured

| Monitor | Target | Type | Records |
|---|---|---|---|
| Homepage dead-man | `https://homepage.daniel-hunter.com` | HTTP | monitor `803270234`, the total-monitoring-death backstop (`docs/longhorn-disaster-recovery.md`) |
| Jellyfin Monitor | Jellyfin's public hostname | HTTP | — |
| Home Assistant Keyword | Home Assistant's public hostname | Keyword | — |
| Littlelink | Littlelink's public hostname | HTTP | — |
| Auth Keyword | Authelia's public hostname | Keyword | — |

The four unrecorded rows are what the 2026-08-30 restart alerted on. Fill in their exact URLs,
keywords and intervals from the console the next time you are in it — an un-auditable monitor is
only marginally better than no monitor.

## Why one restart produced four alerts

All four sit behind the same Traefik ingress, so they do not report four facts. They report one:
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

**Run fewer monitors.** Four probes through one ingress buy no coverage a single one does not
already have; what they buy is four notifications per edge event. Which four become which two is
below, after the fact that decides it.

## Which of these actually sit behind Authelia

Only the homepage dead-man does, and that inverts the obvious reading of this set. From
`containers_list` in `ansible/inventory/host_vars/daniel-box.yml`:

| Monitor | Host | `use_authelia` | What a probe gets |
|---|---|---|---|
| Homepage dead-man | `homepage` | **true** | a 302 from the middleware; the backend is never reached |
| Auth Keyword | `auth` | false | a real 200 — it *is* Authelia |
| Jellyfin Monitor | `jellyfin` | false | a real 200 from Jellyfin, which owns its own auth |
| Home Assistant Keyword | `home-assistant` | false | a real 200 from HA, likewise |
| Littlelink | `www` | false | a real 200 from a static page |

So the recorded backstop is the **weakest** monitor in the set, not the strongest: Uptime Robot
counts Authelia's 302 as up, so it proves the edge is routing and nothing beyond it. That is the
trap recorded in `docs/kopia-disaster-recovery.md`, which already suggests aiming it elsewhere.

## What to collapse to

**Two monitors, from five.** Every one of these services already has an on-prem Kuma tile at a
60-second interval — `k3s Authelia Portal`, `k3s littlelink`, `k3s Home Assistant`,
`k3s Jellyfin (VIP)`. Kuma answers whether the service is healthy. The only thing Uptime Robot
adds that none of them can is **whether the outside world can reach the house** — DNS, Cloudflare,
the public route, the WAN link. That is one fact, and it was being reported four times.

1. **Repoint monitor `803270234` from `homepage` to `auth.<domain>`, as a keyword monitor**
   asserting on the login portal's own text. This is the edge probe: it exercises DNS, Cloudflare,
   Traefik, TLS and Authelia in one check, and unlike its current target it validates a rendered
   page rather than a redirect. Editing it rather than replacing it keeps the monitor id the two
   disaster-recovery docs cite, and keeps its uptime history.
2. **Keep `Jellyfin Monitor`.** Jellyfin is the one service whose *external* reachability is the
   point — streaming from outside the house — so "reachable from the internet" is a distinct fact
   for it rather than a second copy of the edge's.
3. **Delete `Auth Keyword`** (folded into 1), **`Littlelink`** and **`Home Assistant Keyword`**.

**What that gives up, stated plainly:** if a single service's *public* route breaks while the edge
stays healthy, nothing external notices any more. That is a narrow case — every route is rendered
from the same `ingressroute.yml.j2` macro, so they break together far more often than singly — and
both dropped services keep their 60-second on-prem tile. Jellyfin is the exception kept precisely
because remote use is what it is for.

A restart then costs two Uptime Robot alerts instead of four, and one of the two is a strictly
better check than the monitor it replaced.

## If this should stop being un-auditable

The way out is an `uptimerobot_api_key` in SOPS and a reconcile script under
`scripts/diagnostics/`, in the shape of the AutoKuma static-monitor files: monitors declared in the
repo, the script asserting the console agrees. Uptime Robot's v3 API can list and update monitors,
so the read half alone — a drift check that pages when the console stops matching this file — would
close most of the gap without any write credential leaving the repo.

That is not built. Adding it is a decision about whether a fifth externally held credential is
worth the audit trail, and it has not been made.
