# coredns — cluster DNS, derived from k3s's bundled addon

CoreDNS's Deployment, the `kube-dns` Service, its RBAC and the `Corefile` key of the `coredns`
ConfigMap. Every pod resolves names through it, and so do Flux's own controllers, which fetch
the bundle from GHCR. A bad apply here stops name resolution, and host probes stay green.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=coredns`
  with exit 5
- **Image:** `rancher/mirrored-coredns-coredns` (`coredns_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (H1.4). Its `containers_list` entry carries `owner: flux`
  from its first commit. `tasks/main.yml` holds the include of `k8s/manifests` and no Flux path
  runs it. The host half stays in `roles/setup/k3s/tasks/coredns.yml`: a bounded read of
  Deployment `coredns`, then the `.skip` marker task that stops k3s reconciling its addon.
- **The templates are derived, not vendored.** They come from k3s's `manifests/coredns.yaml`
  at `k3s_version`, with each `%{...}%` placeholder written as the value k3s substitutes here.
  That file is not YAML until k3s substitutes it, so no byte-for-byte copy can render and no
  sha256 pin or `vendor_manifest.py` refresh applies. A k3s bump refreshes the templates by hand
  (`docs/k3s-upgrade.md`).
- **`coredns_image` is a pin, not a Renovate target.** The k3s addon stopped reconciling the
  image when the marker went in. `renovate.json` disables Renovate for this key.
  `ansible/tests/setup/test_coredns_image_follows_bundled_manifest.py` holds the pin to an
  excerpt of the bundled manifest, labelled with the k3s version it came from.
- **The ConfigMap declares only `Corefile`.** k3s's node controller writes `NodeHosts` and
  keeps writing it, so Flux must never declare that key.
  `tests/test_coredns_render.py` holds the one-key rule and the Corefile's named members.
- **The Corefile forwards to Pi-hole first.** `coredns_upstreams` lists the `pihole-dns` cluster
  IP first, then two public resolvers, under `policy sequential`. The cluster IP, not the LAN
  VIP, avoids `externalTrafficPolicy: Local` blackholing.
- **`clusterIP` is `coredns_cluster_ip`**, the address the cluster already holds, because the
  field is immutable. `ansible/tests/staging/test_staging_egress_fence.py` holds it inside
  `k3s_service_cidr`.
- **A Corefile edit never rolls the pod.** `tasks/main.yml` passes `manifests_rollout: ''`, so
  the renderer stamps no restart annotation on the pod template. The `reload` plugin re-reads
  the file.
- **The tag renders `prune: false` and is forward-only** (`flux_prune: false`,
  `flux_forward_only: true`, each with its `DECIDED:` marker in `defaults/main.yml`). A render
  that drops an object leaves it serving, and a failed revision holds rather than resets `prod`
  into a fetch that needs the DNS that just failed.
- **`NEVER_SHED` names the tag** in `ansible/filter_plugins/service_tier.py`. The role is
  untiered, because its pod keeps `system-cluster-critical`, which `tier: platform` would
  lower. CoreDNS at zero replicas stops cluster DNS, and Flux then cannot fetch the bundle that
  restores it. `_SYSTEM_TIER` in `ansible/tests/k8s/test_pod_template_hygiene.py` records the
  class.
- **The pod template differs from the k3s addon in four fields** (H1.4), each forced by a
  fleet guard: `enableServiceLinks: false`, the pod-level `runAsNonRoot: true` with
  `runAsUser: 65532`, `limits.cpu: "1"`, and `drop: [ALL]` where upstream spells `all`. The
  pinned image declares `USER 65532:65532`, so the uid pin asserts what it already does. The
  takeover rolls the single replica once, at the promote.
- **The break-glass is in `docs/k3s-upgrade.md`** (**operator-only**): annotate the CoreDNS
  objects `kustomize.toolkit.fluxcd.io/reconcile=disabled`, apply the last-good render by hand,
  then fix forward. `probe.py landing` lists any CoreDNS object still carrying the annotation.
