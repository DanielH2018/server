# Alertmanager's UI and the services that read or silence it

Status: DECIDED, pending open question 1 (spike [#6688](https://github.com/DanielH2018/server/issues/6688), 2026-10-31)

Alertmanager became the homelab's paging path when M6 began retiring Uptime Kuma. Nothing
outside the cluster can see it. During M6's cutovers on 2026-10-30 and 2026-10-31, every read of
alert state went through `probe.py metric` against Prometheus' `ALERTS` series. That series
shows neither silences nor inhibitions. This note answers the four questions the spike asked
and records the recommendation.

**The recommendation confirms the plan M6 already holds and adds three amendments.** M6's
Phase B item 7 (`docs/target-architecture/monitoring/m6-retire-kuma.md:1416`, PRs B-6a and B-6b
at `m6-retire-kuma.md:1930`) decides on a LAN-only `alertmanager.local.<domain>` route behind
Authelia `two_factor`. That item resolves M1's open question 2
(`docs/target-architecture/monitoring/m1-alertmanager.md:1499`). B-6a and B-6b are built,
with the amendments below; `docs/target-architecture/monitoring/m6-status/pr-b-6.md` records
them. The amendments are these:

1. The Authelia rule names a subject, `group:admins`, and a `deny` rule for the same domain
   follows it. Without the `deny`, a user outside `admins` falls through to the `*.local`
   wildcard and gets `one_factor`.
2. B-6b sets `--web.external-url` on Alertmanager, so the UI's links and any notification link
   resolve to the route.
3. A read path for `probe.py` lets it list silences and inhibitions, which `ALERTS` cannot show.

The Grafana Alertmanager datasource is rejected for now. The reason is in answer 1.

This PR ships the note only. The one change small enough to ship here, the Grafana datasource,
is the option the note rejects. It would also edit `grafana.yaml.j2`, which another open
worktree is editing.

## Live readings, 2026-10-31

These reads ran from daniel-server through `probe.py metric`, read-only:

```text
alertmanager_silences
  state=active = 0, state=expired = 0, state=pending = 0
alertmanager_alerts
  state=active = 8, state=suppressed = 0, state=unprocessed = 0
count(ALERTS{alertstate="firing"})
  7
grafana_alerting_rule_group_rules
  no data
```

No silence is active, and none expired within Alertmanager's retention window. The
Deployment sets no `--data.retention`, so the 120h default applies, and an older silence would
no longer be counted. The reading is consistent with the only silence path being an operator
`kubectl exec`.

Alertmanager holds 8 active alerts while Prometheus' `ALERTS` holds 7 firing. The difference
is consistent with the gap M5 records: the Loki ruler's alerts reach Alertmanager but never
reach `ALERTS` (`docs/target-architecture/monitoring/m5-loki-ruler.md:238`). The count alone
does not prove it. A resolved alert that Alertmanager has not yet garbage-collected counts as
active too. The ruler's own `/prometheus/api/v1/alerts` returned `404 page not found` over the
LAN route, because that route admits only `/loki/api/v1/` (`m5-loki-ruler.md:242`).

Nothing scrapes Grafana, so `grafana_alerting_rule_group_rules` returning no data shows
nothing. The tree provisions no Grafana-managed alert rules: `grafana.yaml.j2` mounts a
datasources and a dashboards provisioning file and no alerting one. Whether the live Grafana
database holds a rule created by hand is unverified.

## 1. UI options

| | (a) Grafana datasource | (b) `alertmanager.local` route | (c) both |
|---|---|---|---|
| IngressRoute | none | new `templates/alertmanager-ingressroute.yaml.j2` (B-6b) | as (b) |
| Authelia rule | none; Grafana's OIDC client stays `one_factor` | hand-written `two_factor` rule ahead of the generated block (B-6a) | as (b) |
| NetworkPolicy | none | none | none |
| Alertmanager args | none | `--web.external-url` | as (b) |
| Silence-capable at | `one_factor` (Grafana Admin) | `two_factor` | `one_factor`, the weaker of the two |
| `homelab-ui` / `-m ui` | Grafana's existing `-m ui -k grafana` path | the `claude-ui` tier only | both |

**No NetworkPolicy change is needed for either option.**
`ansible/roles/k8s/netpol-baseline/templates/networkpolicy-observability.yaml.j2:44` already
admits the traefik pod from the homelab namespace to every port of every observability pod.
Alertmanager carries `netpol-baseline: enforced`
(`ansible/roles/k8s/observability/templates/alertmanager.yaml.j2:29`), so the baseline selects
it. Grafana reaches Alertmanager inside the namespace, which the bare `podSelector: {}` at
`networkpolicy-observability.yaml.j2:68` admits. NetworkPolicies add up, so
`networkpolicy-alertmanager.yaml.j2` widens that baseline and never narrows it. The only stale
text is the comment at
`ansible/roles/k8s/netpol-baseline/templates/networkpolicy-alertmanager.yaml.j2:21`, "Alertmanager
has no route, so traefik never dials it". B-6b edits that comment in the same PR as the route.

**(a) The Grafana datasource gives silence power at `one_factor`.** Grafana's OIDC client is
`one_factor` by decision (`ansible/roles/k8s/authelia/templates/config-secret.yaml.j2:352`),
because the headless `-m ui` browser holds a one_factor session. The Authelia `admins` group
maps to Grafana's `Admin` role (`ansible/roles/k8s/observability/templates/grafana.yaml.j2:250`).
In Grafana OSS, an Editor or an Admin can create and expire silences on an external
Alertmanager datasource; a Viewer can only read. The pinned image was not exercised for this,
but OSS has no per-datasource read-only switch, so nothing in the tree could narrow it.
Adding the datasource therefore lets any `admins` member silence every page with a password
alone. That contradicts the reason M6 item 7 gives for `two_factor`: "because it can silence
every alert".

**(b) The native route keeps silences behind `two_factor`.** It is a hand-written IngressRoute
in the observability role, LAN-only in the shape of
`ansible/roles/k8s/observability/templates/prometheus-ingressroute.yaml.j2:54`, naming the
`authelia` chain Middleware that `middlewares.yaml.j2` already copies into the namespace. The
observability entry carries one `hostname`, `grafana`, so `authelia_service_rules` cannot
generate the rule. Without the hand-written rule the `*.local` wildcard at
`config-secret.yaml.j2:205` would give `one_factor`. B-6a therefore deploys before B-6b, and a
revert removes B-6b first.

The route also needs `--web.external-url=https://alertmanager.local.<domain>`. The Deployment's
args at `alertmanager.yaml.j2:44` set none, so Alertmanager builds its links from the pod's
hostname. The notification template renders only a `runbook_url` link
(`ansible/roles/k8s/observability/files/alertmanager/homelab.tmpl:35`). A silence link in the
Discord and email bodies becomes possible once the external URL exists; it is optional.

The service-reference row changes on its own. `docs/reference/services.md` is generated from
the tree, and the role's *At a glance* block lists routes from `templates/ingressroute.yaml.j2`
only, so the second route needs a sentence in the observability role's `CLAUDE.md`.

**`homelab-ui` coverage of (b) reaches the `claude-ui` tier only.** The `claude-agent` account
has no groups and no TOTP (`docs/claude-tooling.md:485`), so it cannot pass a `two_factor`
rule. The `claude-ui` user has a seeded TOTP and is in `admins`
(`ansible/roles/k8s/authelia/defaults/main.yml:150`). A `-m ui` test can therefore load the
Alertmanager UI. The same reach means a Claude-driven browser on that tier could create a
silence. Open question 1 asks the operator whether that is acceptable.

**To silence an alert during a planned window, the operator uses (b).** They open
`alertmanager.local.<domain>`, pass Authelia with a second factor, and create a silence with a
matcher and an end time. Alertmanager persists it on `alertmanager-data`.

**Recommendation: (b) alone, as M6 B-6 plans.** (a) is reconsidered only if Grafana's login
moves to `two_factor` for `admins`, which the `-m ui` constraint rules out today.

## 2. What reads or writes alert state

| Reader or writer | What it does | Gap |
|---|---|---|
| Prometheus | sends every rule's alert to `alertmanager:9093` | none |
| Loki ruler | sends to Alertmanager directly (`ansible/roles/k8s/loki-homelab/templates/configmap.yaml.j2:66`) | its alerts never reach `ALERTS`, so `probe.py health` and `monitors` cannot see a log rule firing (`m5-loki-ruler.md:238`, an M5 open question) |
| Grafana | three datasources over Prometheus, Loki and Tempo, plus two uid aliases (`grafana.yaml.j2:25`); no Alertmanager datasource | rejected in answer 1; the tree provisions no Grafana-managed rules, so nothing needs routing through Alertmanager |
| `probe.py health <tag>` | reads `ALERTS{alertstate="firing",service="<tag>"}` (`scripts/diagnostics/probe_lib/health_alerts.py:49`) | cannot say an alert is silenced or inhibited. A silenced alert still fails the gate, which is correct: a silence mutes a page, not a deploy's health. The gate should name the silence in its output |
| `probe.py monitors` | at this survey, Kuma's rollup plus firing ingress alerts from `ALERTS`; M6 B-2 moved it onto `ALERTS` (`scripts/diagnostics/probe_lib/monitors.py`) | it lists active silences, and still shows no inhibition |
| `probe.py alerts` | at this survey, DOWN history from Loki; M6 B-2 moved it onto `ALERTS` history (`scripts/diagnostics/probe_lib/alerts.py`) | the history of a silence lives only in Alertmanager |
| `monitor-bridge` | serves gauges that Prometheus scrapes; pushes the rest to Kuma | none for Alertmanager. Its gates move into Alertmanager `inhibit_rules` through M6, which makes inhibitions the thing an operator most needs to see |
| Kuma maintenance sync | keeps ONE Kuma window, the weekly reboot's (`ansible/roles/k8s/uptime-kuma/templates/maintenance-sync-cronjob.yaml.j2:4`) | none. C-1b deletes it, and nothing replaces it: Alertmanager already mutes the reboot |
| Weekly reboot window | the `weekly-reboot` time interval (`ansible/roles/k8s/observability/templates/alertmanager-secret.yaml.j2:60`) derives from the same `weekly_reboot_*` values as the cron | none. `ansible/tests/services/test_alertmanager_reboot_mute.py::test_moving_the_reboot_moves_the_mute_with_kumas_window` holds the two equal |
| Homepage | an Uptime Kuma widget (`ansible/roles/k8s/homepage/templates/config/services.yaml.j2:93`) | M6 B-4 replaces it with a `prometheusmetric` tile over `ALERTS`; that tile shows no silence count |

**A planned window other than the reboot is a silence, not a config change.** Kuma's sync
manages only the reboot window, so retiring Kuma loses no other planned window. A recurring
window belongs in `time_intervals`, as the reboot's does. A one-off belongs in a silence through
the route.

**`probe.py` needs a read path to Alertmanager's API.** Alertmanager's
`/api/v2/alerts` returns each alert's `status.silencedBy` and `status.inhibitedBy`, and
`/api/v2/silences` lists the silences. The host cannot reach either today: the Service is
ClusterIP, and the planned route sits behind `two_factor`. The recommended shape is a second
router on the same host name, in the form of `prometheus-ingressroute.yaml.j2:54`: the LAN
`ClientIP` guard, `Method(`GET`)`, and `PathPrefix(`/api/v2/alerts`) ||
PathPrefix(`/api/v2/silences`)`, with no Authelia. Every `/api/v2` write is a POST or a DELETE,
so the `GET` matcher keeps the door read-only. The read exposes alert labels and annotations to
the LAN, which the Prometheus door already exposes through `ALERTS`, plus silence comments and
their creators, which it does not. The operator decides whether that is acceptable (open
question 2). Without the door, `probe.py` stays blind to silences and inhibitions.

## 3. Security and blast radius

**A silence-capable UI needs `two_factor` and the `admins` group.** The `.local` name alone is
not enough. The `*.local` wildcard grants `one_factor` to any RFC1918 client
(`config-secret.yaml.j2:205`), and the `claude-agent` account holds exactly that session. A
`.local` route without its own rule would let the agent's browser mute every page. The rule
B-6a writes is therefore:

```yaml
- domain: "alertmanager.local.{{ domain }}"
  policy: two_factor
  subject:
    - "group:admins"
  networks: <the RFC1918 list the wildcard uses>
- domain: "alertmanager.local.{{ domain }}"
  policy: deny
```

Authelia evaluates the rules in order and applies the first rule whose every criterion matches,
the subject included. A user outside `admins` therefore skips the first rule. Without the
`deny` rule after it, that user would fall through to the `*.local` wildcard and get
`one_factor`. Both rules must sit ahead of the generated block and the wildcard. B-6a's render
test asserts that an `admins` member resolves to `two_factor` and that any other user resolves
to `deny`.

**Port 9093 has no authentication of its own, and that does not change.** Every pod in the
observability namespace, the node CIDRs that `netpol_baseline_obs_node_cidrs` admits, and the
loki-homelab pod can already reach `POST /api/v2/silences`. The route adds a door for a person
and leaves those in-cluster callers as they are. Fencing them would mean an
Alertmanager-specific policy that drops the intra-namespace and node-CIDR peers, which no caller
census has measured. It is out of scope here.

**A silence can hide the Watchdog.** A matcher such as `alertname=~".+"` silences the
Watchdog, and Healthchecks.io then reports Alertmanager down off-site. That page is the
backstop, so a broad silence is self-reporting. Nothing in the route needs to block it.

## 4. Interaction with M6

**B-6a and B-6b carry the UI and its amendments.** B-6a adds the subject to the rule it already
writes. B-6b adds `--web.external-url` and the netpol comment edit to the route it already
adds. Both deploy through the promotion, because the observability, authelia and
netpol-baseline roles are Flux-owned.

**The `probe.py` read path is independent of M6's order.** It needs the route's host name from
B-6b and nothing else. It touches no rule file under `ansible/roles/k8s/observability/files/rules/`
and no routing test. It should land before C-1, which retires the Kuma UI's pause button, so
the operator can see a silence they create during Phase C.

**The Loki ruler's invisibility to `ALERTS` stays with M5's open question.** It affects
`probe.py health` today, so it is not M6's to absorb.

## Open questions

1. **Can a Claude-driven browser silence a page?** The `claude-ui` user is in `admins` and has
   a seeded TOTP, so it passes B-6a's rule. The options are to accept it, which keeps `-m ui`
   coverage of the Alertmanager UI, or to name a narrower group that excludes `claude-ui` and
   cover the UI by hand. **Operator-only.**
2. **Is an unauthenticated LAN read of `/api/v2/alerts` and `/api/v2/silences` acceptable?**
   It exposes silence comments and creators beyond what the Prometheus door exposes.
   **Answered 2026-10-31: yes** (the operator's ruling on
   [#6689](https://github.com/DanielH2018/server/issues/6689)).
   `ansible/roles/k8s/observability/templates/alertmanager-read-ingressroute.yaml.j2` is the
   door, and `scripts/diagnostics/probe_lib/alertmanager_api.py` is its reader.

## Follow-ups filed

- [#6689](https://github.com/DanielH2018/server/issues/6689): M6 B-6a, the `two_factor`
    Authelia rule for the `admins` group and the `deny` after it. Waits on open question 1.
- [#6690](https://github.com/DanielH2018/server/issues/6690): M6 B-6b, the route,
    `--web.external-url` and the netpol comment.
- [#6691](https://github.com/DanielH2018/server/issues/6691): the GET-only door and
    `probe.py`'s read of silences and inhibitions. Built after open question 2 was answered.
