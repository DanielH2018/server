# traefik-crds — the Traefik CRDs, in a tag of their own

The IngressRoute, Middleware, TLSOption and the rest of upstream Traefik's
CustomResourceDefinitions. traefik's Kustomization depends on this tag, and every routed role
reaches the CRDs through its edge to `traefik`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=traefik-crds` with exit 5
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **The only content is `files/upstream/kubernetes-crd-definition-v1.yml`**, upstream's file
  byte for byte, pinned by `traefik_crds_k8s_version` and the `traefik_crds_vendored_manifests`
  item in `defaults/main.yml`. `scripts/dev/vendor_manifest.py` checks the pin and refreshes
  the copy. The renderer attaches the file to the tag's bundle verbatim, and the include sets
  `manifests_files: []`, so the render holds the kustomization and the vendored file alone. No
  render may fetch a URL (D5, C1).
- **A minor traefik image bump needs a refresh of the copy.**
  `ansible/tests/k8s/test_traefik_crds.py` fails when traefik's image minor differs from
  `traefik_crds_k8s_version`'s, and when traefik renders a `traefik.io` kind the copy lacks.
  The refresh command sits above the pin.
- **Why a tag of its own** (#5666). `scripts/renderer/renderer_lib/cluster.py:attach_vendored`
  refuses a tag that vendors files while its render holds a placeholder, so no `${` in
  upstream bytes reaches Flux's substitution. traefik's render holds six, and `email` is read
  by the static-config ConfigMap and the Deployment, so no move of traefik's Secrets could
  empty it. This tag renders no template, so it holds no placeholder.
- **The tag renders `prune: false`** (`flux_prune: false`). Under `prune: true`, a render
  that dropped a CRD file, such as a `traefik_crds_k8s_version` bump whose upstream drops a
  kind, would make Flux delete that CRD, and the API server would then delete every
  IngressRoute or Middleware of the kind: the whole edge goes down. `deletionPolicy` does not
  help, because it governs deleting the Kustomization, not pruning an object the render
  dropped. `scripts/renderer/tests/test_renderer_crd_census.py` fails a CRD in any tag that
  renders `prune: true`, with this tag as its named member.
- **The cost of `prune: false`:** a CRD a later upstream release retires stays live until
  the operator deletes it (**operator-only**). Never revert `flux_prune: false` on its own
  while the tag is flux-owned; a handback reverts traefik's flip PR whole.
- **`tier: home-edge`**, traefik's tier, keeps the tag out of the lost-node shed set without
  a `NEVER_SHED` entry (`ansible/filter_plugins/service_tier.py`).
- **Flux applies it, not Ansible** (D5, traefik's own window). Its `containers_list` entry
  carries `owner: flux` from its first commit. `tasks/main.yml` holds the include of
  `k8s/manifests` and no Flux path runs it. To hand it back, follow the two-PR handback in
  `docs/flux-deploy-plane.md` together with traefik's.
