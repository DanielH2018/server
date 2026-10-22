# metallb — the LoadBalancer announcer and its address pools

MetalLB's controller, its speaker DaemonSet and its CRDs, plus the two address pools and the
L2Advertisement that announce every LoadBalancer VIP (Traefik's edge, Pi-hole's DNS, Mosquitto,
Jellyfin's LAN address). A bad apply here takes the edge and DNS offline together while host
probes stay green.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses `--tags=metallb`
  with exit 5
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (H1.3). Its `containers_list` entry carries `owner: flux`
  from its first commit. `tasks/main.yml` holds the include of `k8s/manifests` and no Flux path
  runs it. A rebuilt cluster gets it from the `metallb` Kustomization at step (f) of
  `docs/longhorn-disaster-recovery.md`'s *Order from H1.2*.
- **The workloads are `files/upstream/metallb-native.yaml`**, upstream's file byte for byte,
  pinned by `k3s_metallb_version` and the `metallb_vendored_manifests` item in
  `defaults/main.yml`. `scripts/dev/vendor_manifest.py` checks the pin and refreshes the copy.
  The refresh command sits above the pin. No render may fetch a URL (D5, C1).
- **Merging a bump of `k3s_metallb_version` is the upgrade** (H1 Open question 6).
  `land.sh` runs the `metallb-upgrade` gates before it merges, and MetalLB's CRDs carry a
  conversion webhook, so the version never runs backwards.
- **The file declares the `prometheus.io/port` and `prometheus.io/scrape` pod-template
  annotations** on the controller and the speaker. Only `kubectl-client-side-apply` owns them
  live (the H1 plan's *Baseline*), so a vendored file without them would remove them at the
  takeover and roll every MetalLB pod at the promote.
  `ansible/tests/k8s/test_metallb_vendored_manifest.py::test_the_vendored_file_declares_the_prometheus_template_annotations`
  holds them.
- **The file declares no `caBundle` and no data for the `metallb-webhook-cert` Secret.** The
  controller writes both at runtime, and under server-side apply a field the render does not
  declare stays with the controller. The role carries no `files/patches/` for that reason.
- **The tag renders `prune: false`** (`flux_prune: false`, with the `DECIDED:` marker beside
  it in `defaults/main.yml`). A render that drops a CRD would make Flux delete every pool and
  advertisement of its kind. The cost: an object removed from the render stays live until the
  operator deletes it (**operator-only**).
- **The tag is forward-only** (`flux_forward_only: true` in `defaults/main.yml`). When a
  revision that changes it fails, `promote` writes `DEPLOY_HOLD` and leaves `prod` where it
  is, because a reset would re-apply an older vendored file over upgraded CRDs.
  `ansible/tests/k8s/test_forward_only_tags.py` holds the flagged roles equal to the H1
  plan's list.
- **`NEVER_SHED` names the tag** in `ansible/filter_plugins/service_tier.py`. The role is
  untiered, and the lost-node break-glass must not scale the announcer down.
  `ansible/tests/k8s/test_shed_set_is_derived_from_tier.py::test_never_shed_matches_the_untiered_roles_that_never_prune`
  holds the set equal to the untiered roles that set `flux_prune: false`.
- **`k3s_metallb_pool` is defined here**, and `ansible/tests/repo/test_lan_facts_have_one_source.py`
  exempts this file for it. `templates/metallb-pool.yaml.j2` moved from `setup/k3s` and keeps
  its `DECIDED:` and `PERMANENT` comments: the L2Advertisement announces from `k8s_primary_node`
  alone, and every VIP-backed workload's `nodeSelector` is the same variable.
  `ansible/tests/k8s/test_vip_pins.py::test_announcement_and_the_primary_node_variable_agree`
  holds the two halves together.
- **The pools apply through MetalLB's validating webhook** (`failurePolicy: Fail`). On a fresh
  cluster the first apply of the pools can fail until the controller serves the webhook, and
  Flux retries. The Ansible path used to wait for the controller to become Available.
