# blackbox-exporter — HTTP probes of the ingress names, for Prometheus to scrape

`prom/blackbox-exporter` on `:9115`, in the `homelab` namespace. It probes a URL when
Prometheus asks it to on `/probe`. `docs/target-architecture/monitoring/m4-blackbox-ingress.md` is the plan. Prometheus
scrapes it through observability's `blackbox-ingress` job, one target per routed entry, and the
`ingress-probes` rule group alerts on the result. See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in ansible/roles/k8s/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote` (`platform: k8s`); `land.sh --tags=blackbox-exporter`
  gates the rollout
- **Image:** `prom/blackbox-exporter` (`blackbox_exporter_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

## Notable

- **Every http module sends `Accept: */*`.** Authelia answers an SSO-gated name with 302 only to a
  request whose Accept header admits it, and with 401 to one that carries none. blackbox sends
  no Accept unless the module sets one. Without the header, `http_sso_302` reads all 23 SSO
  targets as down while they serve. The same holds for M6 batch B8's `http_401` and
  keyword modules. ENFORCED:
  `ansible/tests/services/test_blackbox_exporter_modules.py`.
- **Ingress names resolve pinned to the VIP.** The pod's `hostAliases` come from the
  `ingress_host_aliases()` macro in `ansible/templates/ingress-host-aliases.yml.j2`, and this
  role's Deployment is its only caller. Every ingress name is pinned to the ingress VIP, so a
  CoreDNS or Pi-hole fault stays out of the probe path. A routed `containers_list` change
  therefore rolls this pod.
- **No deploy-order edge against `observability`.** The entry carries no `depends_on`, and no
  prose states an order between this role and Prometheus. An edge onto it would move
  `observability` last in the derived order, and under Flux it would hold every pager change
  behind this role's readiness. The plan's *No deploy-order edge* has the full reasoning. The
  entry sits above `netpol-baseline` in `containers_list` only because that role must stay
  last.
- **Flux applies it, not Ansible** (D5, Wave A's last PR). Its `containers_list` entry
  is `platform: k8s`, so nothing applies it by hand, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. The
  Kustomization's `wait` replaces
  the rollout gate, and the restart checksum the renderer stamps on the Deployment replaces
  the shared restart.
- **Renovate does not automerge its image bumps** (`k8s_autodeploy: false`), because this role
  is the ingress pager. `/-/healthy` proves the process answers, not that a module still probes correctly.
  The PR is reviewed and merged by hand, and `promote` applies it after the merge.
- **`netpol-baseline: enforced`, with no route.** Its only caller is the `prometheus` pod,
  which the baseline admits. The probes are egress, which the baseline does not fence. The
  targets of the `blackbox-extra` job (M6 batch B8) are ingress to the probed pods, so
  mosquitto's and nut's policies and ical-proxy's `netpol_from` admit `blackbox-exporter`.

## Editing
- Modules: `files/blackbox.yml`, carried by `templates/configmap.yaml.j2` with the domain placeholder
  in the `dns_a_homepage` query name replaced by the LAN domain, so an
  edit, a comment included, moves the restart checksum and rolls the pod. Check one with
  `blackbox_exporter --config.check --config.file=files/blackbox.yml`.
- Manifests: `templates/`. The Service renders from `ansible/templates/service-default.yaml.j2`, off the containers_list entry. No IngressRoute: callers reach it in-cluster by Service name.
- Image pin, automerge stance, resources and uid: `defaults/main.yml`.
