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

Two consequences worth acting on, in the console:

**Raise the confirmation threshold past a normal restart.** A monitor that alerts on the first
failed check cannot distinguish a restart from an outage. Set each monitor's interval to 5 minutes
and require **two consecutive failed checks** before alerting, which puts the alerting floor at
~10 minutes — above the 8m35s a full restart costs, and still well inside a real outage.

**Collapse the four backend probes into one edge probe.** Four monitors through one ingress buy no
coverage the ingress monitor does not already have; what they buy is four notifications per edge
event. Keep one monitor on the edge — the homepage dead-man already is one — and drop or pause the
rest unless a specific service has a failure mode its own probe would catch and the edge probe
would not. Jellyfin is the plausible candidate, since it can serve a 200 while its library is
broken; Littlelink is static and behind the same edge, so its monitor reports only what the edge
monitor reports.

## The keyword monitors and Authelia

`Auth Keyword` and `Home Assistant Keyword` probe hostnames behind Authelia, which answers an
unauthenticated request with a 302 to the login portal. Uptime Robot counts that 302 as up, so a
plain HTTP monitor there proves the edge is routing and **nothing about the backend** — the same
trap recorded in `docs/kopia-disaster-recovery.md`. A keyword monitor asserting on the login page's
own text is a genuine improvement over that, but be clear about what it still cannot see: it
confirms Authelia rendered a portal, not that the service behind Authelia is alive.

Pair each with the on-prem Kuma tile for the same service, which probes past the middleware. Kuma
answers whether the service is healthy; Uptime Robot answers whether the outside world can reach the house.

## If this should stop being un-auditable

The way out is an `uptimerobot_api_key` in SOPS and a reconcile script under
`scripts/diagnostics/`, in the shape of the AutoKuma static-monitor files: monitors declared in the
repo, the script asserting the console agrees. Uptime Robot's v3 API can list and update monitors,
so the read half alone — a drift check that pages when the console stops matching this file — would
close most of the gap without any write credential leaving the repo.

That is not built. Adding it is a decision about whether a fifth externally held credential is
worth the audit trail, and it has not been made.
