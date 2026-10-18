# blackbox-exporter — HTTP probes of the ingress names, for Prometheus to scrape

`prom/blackbox-exporter` on `:9115`, in the `homelab` namespace. It probes a URL when
Prometheus asks it to on `/probe`. It replaced Kuma's generated ingress tiles in M4's PR 4
(#5625), and `docs/target-architecture/monitoring/m4-blackbox-ingress.md` is the plan. Prometheus
scrapes it through observability's `blackbox-ingress` job, one target per routed entry, and the
`ingress-probes` rule group alerts on the result. See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "blackbox-exporter"`
- **Image:** `prom/blackbox-exporter` (`blackbox_exporter_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — observability — the ingress pager
  once M4 cuts over; a broken deploy cannot page about being broken. Its readinessProbe
  (/-/healthy) proves the process runs, not that a probe succeeds
<!-- /generated_from -->

## Notable

- **Both modules send `Accept: */*`.** Authelia answers an SSO-gated name with 302 only to a
  request whose Accept header admits it, and with 401 to one that carries none. blackbox sends
  no Accept unless the module sets one. Without the header, `http_sso_302` reads all 23 SSO
  targets as down while Kuma reads them up. ENFORCED:
  `ansible/tests/services/test_blackbox_exporter_modules.py`.
- **Names resolve exactly as Kuma's do.** The pod's `hostAliases` come from the shared
  `ingress_host_aliases()` macro in `ansible/templates/ingress-host-aliases.yml.j2`, which
  uptime-kuma's Deployment calls too. Every ingress name is pinned to the ingress VIP, so a
  CoreDNS or Pi-hole fault stays out of the probe path. A routed `containers_list` change
  therefore rolls this pod as well as Kuma's.
- **No deploy-order edge against `observability`.** The entry carries no `depends_on`, and no
  prose states an order between this role and Prometheus. An edge onto it would move
  `observability` last in the derived order, and under Flux it would hold every pager change
  behind this role's readiness. The plan's *No deploy-order edge* has the full reasoning. The
  entry sits above `netpol-baseline` in `containers_list` only because that role must stay
  last.
- **Auto-deploy is denylisted** because this role becomes the ingress pager. `/-/healthy`
  proves the process answers, not that a module still probes correctly, so an image bump
  lands by hand through `land.sh`.
- **`netpol-baseline: enforced`, with no route.** Its only caller is the `prometheus` pod,
  which the baseline admits. The probes are egress, which the baseline does not fence.

## Editing
- Modules: `files/blackbox.yml`, carried verbatim by `templates/configmap.yaml.j2`, so an
  edit rolls the pod through the shared rollout gate. Check one with
  `blackbox_exporter --config.check --config.file=files/blackbox.yml`.
- Manifests: `templates/`. The Service renders from `ansible/templates/service-default.yaml.j2`, off the containers_list entry. No IngressRoute: callers reach it in-cluster by Service name.
- Image pin, auto-deploy stance, resources and uid: `defaults/main.yml`.
