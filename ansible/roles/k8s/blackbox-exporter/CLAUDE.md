# blackbox-exporter — HTTP probes of the ingress names, for Prometheus to scrape

`prom/blackbox-exporter` on `:9115`, in the `homelab` namespace. It probes a URL when
Prometheus asks it to on `/probe`. It replaced Kuma's generated ingress tiles in M4's PR 4
(#5625), and `docs/target-architecture/monitoring/m4-blackbox-ingress.md` is the plan. Prometheus
scrapes it through observability's `blackbox-ingress` job, one target per routed entry, and the
`ingress-probes` rule group alerts on the result. See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=blackbox-exporter` with exit 5
- **Image:** `prom/blackbox-exporter` (`blackbox_exporter_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

## Notable

- **Every http module sends `Accept: */*`.** Authelia answers an SSO-gated name with 302 only to a
  request whose Accept header admits it, and with 401 to one that carries none. blackbox sends
  no Accept unless the module sets one. Without the header, `http_sso_302` reads all 23 SSO
  targets as down while Kuma reads them up. The same holds for M6 batch B8's `http_401` and
  keyword modules. ENFORCED:
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
- **Flux applies it, not Ansible** (D5, Wave A's last PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. To
  give it back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain
  revert of the flag leaves two writers on the objects. The Kustomization's `wait` replaces
  the rollout gate, and the restart checksum the renderer stamps on the Deployment replaces
  the shared restart. `tasks/main.yml` holds one task, the include of `k8s/manifests`, and no
  Flux path runs it. It stays for the handback.
- **Auto-deploy is denylisted** (`k8s_autodeploy: false`) because this role is the ingress
  pager. `/-/healthy` proves the process answers, not that a module still probes correctly.
  Renovate therefore does not automerge an image bump, and the PR is reviewed and merged by
  hand. `promote` applies it after the merge.
- **`netpol-baseline: enforced`, with no route.** Its only caller is the `prometheus` pod,
  which the baseline admits. The probes are egress, which the baseline does not fence. The
  targets of the `blackbox-extra` job (M6 batch B8) are ingress to the probed pods, so
  mosquitto's and nut's policies and ical-proxy's `netpol_from` admit `blackbox-exporter`.

## Editing
- Modules: `files/blackbox.yml`, carried by `templates/configmap.yaml.j2` with the domain placeholder
  in the `dns_a_homepage` query name replaced by the LAN domain, so an
  edit rolls the pod through the shared rollout gate. Check one with
  `blackbox_exporter --config.check --config.file=files/blackbox.yml`.
- Manifests: `templates/`. The Service renders from `ansible/templates/service-default.yaml.j2`, off the containers_list entry. No IngressRoute: callers reach it in-cluster by Service name.
- Image pin, auto-deploy stance, resources and uid: `defaults/main.yml`.
