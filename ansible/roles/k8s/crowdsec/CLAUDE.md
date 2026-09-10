# crowdsec — WAF / behavioural bouncer in front of Traefik

CrowdSec LAPI plus remote agents; the role registers the agent machines on the LAPI after
applying the manifests. See repo-root `CLAUDE.md` for shared conventions.

## Traps

### A crowdsec deploy races its own rollout
`tasks/main.yml:18` ("Register the remote agent machines on the LAPI") runs
`k3s kubectl exec deploy/crowdsec -- cscli machines ...`. It sits immediately after
`k8s/manifests`' rollout-restart, which deliberately does not wait — the drain is queued for
the end of the batch. So whenever a crowdsec manifest actually changes, the exec lands on a
pod that is terminating or not yet ready, and the task fails.

Observed 2026-08-16: a one-line comment edit to `deployment.yaml.j2` changed the render,
triggered the roll, and the deploy came back `failed=1` with two loop items OK and two failed.
Re-running once the pod was `2/2` gave `failed=0` with no other change.

`no_log: true` on that task — it pipes the agent password over stdin — censors the error body,
so the failure reads as an opaque "Module failed: non-zero return code" with no hint that it
is a rollout race. Easy to misread as a credential or RBAC problem.

**FIXED** 2026-08-16 in PR #229: a `rollout status` gate now precedes the LAPI tasks, proven
by the deploy that shipped it (the gate blocked 61.83s, registration then succeeded,
`failed=0` on the first run). The signature above is kept because it is what makes a
regression recognisable if the gate is ever removed as apparent drift from `rollout-drain`'s
"never wait inline" rule — which is why the gate carries a comment naming itself the
deliberate exception. A naive `kubectl wait --for=condition=Available` would not help: the pod
is single-replica, so the old pod satisfies the condition.

If it recurs, a `--tags crowdsec` deploy that fails only on that task right after a manifest
change is this. Wait for `kubectl -n homelab get pods` to show crowdsec `1/1` (`2/2` before the
Metabase dashboard was removed on 2026-08-22), then re-run —
the second pass rolls nothing and succeeds. A deploy that changes no crowdsec manifest never
hits it.

### `--check` on this role always fails at the banned-Pi probe
Check mode skips the *ban* task but still runs `Probe the VIP from the banned Pi`, so the
probe fails. Pre-existing, confirmed by A/B, and not a sign of a broken change.

## The Metabase dashboard was removed (2026-08-22)

The engine pod carried a Metabase sidecar (plus a `metabase-seed` initContainer, a
`crowdsec-dashboard` Service and its Authelia-gated IngressRoute at `crowdsec.local.<domain>`).
It is gone. Two reasons:

- **It gated the edge WAF.** Pod Ready is the AND of all containers, so Metabase's startupProbe
  withheld the crowdsec Service endpoints that front LAPI and AppSec. Observed 2026-08-16: LAPI
  was serving while the pod sat 1/2 with no endpoints.
- **Its aggregate view was already Grafana's.** The four Security-folder boards
  (`roles/k8s/claude-otel/files/dashboards/Security/`) read the engine's `:6060` metrics.

**What was lost, and has no Grafana equivalent.** Metabase read the LAPI's `decisions` and
`alerts` tables directly, so it could show what Prometheus has no label for: per-IP identity
(`Top IPs`, `By Source IP`), geography (`Alerts Map`, `Top countries`), ASN (`Top AS`), decision
origin (`By Origin`), and row-level tables (`Actives Decisions List`, `Alerts Table`). Loki is
not a substitute — the engine's alert insertions never reach pod stdout, verified over 7 days
against a DB holding 424 alerts younger than that. Use `cscli decisions list` and
`cscli alerts list` for per-ban detail.

Upstream `crowdsecurity/grafana-dashboards` cannot fill the gap either: it is Prometheus-only,
was last touched 2023-06-20 targeting CrowdSec v1.5.x, and its `dashboards_v5` panel set is
already what this repo ships (identical titles, `instance` relabelled to `machine`).

## Which Prometheus job covers which agent

Four CrowdSec containers run in this cluster and a scrape job covers each, all defined in
`roles/k8s/claude-otel/templates/prometheus.yaml.j2`:

| Container | Job | Node dimension |
|---|---|---|
| the engine pod (LAPI + AppSec) | `crowdsec` | none — a singleton |
| the `crowdsec-node-agent` DaemonSet | `crowdsec-node-agents` | `node`, from the pod's node name |
| the traefik pod's `crowdsec-agent` sidecar | `crowdsec-traefik-agent` | none — a singleton |
| the authelia pod's `crowdsec-agent` sidecar | `crowdsec-authelia-agent` | none — a singleton |

Read the job before writing a CrowdSec query: `node` is not a CrowdSec label, and only the
DaemonSet job attaches one. A per-node selector on an engine or sidecar metric matches nothing,
which is how `Alerts per Scenario` and `Bucket pour time` sat dead on the per-machine board
(#1690). `ansible/tests/services/test_dashboard_queries_match_this_clusters_labels.py` enforces
that.

**A sidecar job needs two edits, not one.** Pod-role SD emits a target per *declared*
containerPort, so each sidecar declares 6060 (`roles/k8s/traefik/templates/deployment.yaml.j2`,
`roles/k8s/authelia/templates/deployment.yaml.j2`), and that pod's baseline NetworkPolicy admits
prometheus to that port (`roles/k8s/netpol-baseline/templates/networkpolicy-<pod>.yaml.j2`).
Either one missing gives a job that discovers nothing or reads `up == 0` forever — both
indistinguishable from the job nobody added.
`ansible/tests/k8s/test_crowdsec_sidecars_are_scraped.py` holds all three together, for both
sidecars.

**The netpol grant is a `from` item of its own, not a port appended to an existing rule.** A
NetworkPolicy rule ANDs its `from` with its `ports`, so adding 6060 to authelia's
traefik-to-9091 rule would have admitted *traefik* to the agent's metrics and prometheus to
nothing (#1706). It reads like a grant in a diff. traefik's own policy already had a prometheus
`from` item for :8080, which is why #1694 there was a one-line port addition and #1706 here was
not.

The overview board's agent tile is a RATIO — `sum(up{job=~"crowdsec.*"}) / count(...)` — so 1
means every agent reports whatever the fleet size. It plotted a raw count against a green step
at 10, sized by the upstream for ten machines, and so read red on a healthy fleet of three and
could never go green (#1709). The ratio has one blind spot, the same one `kuma-drift` names: a
job that DISAPPEARS from service discovery leaves the survivors at N/N, so the tile answers "is
an agent down", not "is an agent missing".

`Bucket pour time` on the per-machine board reads `{job="crowdsec"}` — the engine's AppSec
pours alone. The traefik sidecar's own pour series are excluded on purpose: the panel legends
by `le` only, so two datasources would collide into one set of bars.
