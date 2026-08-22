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
