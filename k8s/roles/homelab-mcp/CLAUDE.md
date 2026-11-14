# homelab-mcp — the read-only MCP server backing Claude's cluster tools

Serves `list_pods` / `workload_status` / `list_nodes` / `pod_logs` over its own narrow
read-only RBAC identity. Rehomed here from daniel-server (Phase E, 2026-08-13) — the last
routed tenant of the retired Docker edge. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=homelab-mcp` gates the rollout
- **Image:** `ghcr.io/danielh2018/homelab-mcp` (`homelab_mcp_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux's render pulls `ghcr.io/danielh2018/homelab-mcp:<content tag>`**, with
  `imagePullPolicy: IfNotPresent`, and the *Image* line above names that repository.
  The role's defaults still name the in-cluster registry. promote's `images`
  job builds the image from `templates/Dockerfile.j2` and the files below.
- **No IngressRoute** — routed through the k8s edge's file-provider gate Secret (`k8s/roles/traefik/templates/livesync-gate-secret.yaml.j2`)
  instead, so its bearer token stays out of CRD objects the readonly kubeconfig can list.
  **Two routers there, not one.** `homelab-mcp-local` carries the bearer predicate and serves
  everything; `homelab-mcp-probe` is LAN-only, matches the single exact `Path(`/health`)`, and
  requires no token — `_BearerAuth` in `files/app.py` exempts that path, and it returns the bare
  string `ok`. It exists so the `blackbox-extra` job's `http_body_ok` probe of
  `mcp.local/health` can prove the edge ROUTES, matching the body: a probe once tested `/mcp`
  with no bearer and accepted the 404 that matches no router, which is byte-identical to what a
  router-less edge returns for every host, so it read green through the 3.5-hour outage of #1322
  (#1341). Changing that router to a PathPrefix would open more than `/health`; a test asserts it
  stays an exact Path.
- **A stateless `RollingUpdate` Deployment with one replica.** The default surge starts the
  new pod before the old one stops, so a roll keeps one pod serving.
- **Pinned to `k8s_primary_node` (daniel-box), not preferred.** `read_file` and `list_files`
  serve the node's checkout of `k8s/`, `inventory/` and `tests/` through read-only hostPath
  mounts under `/srv/homelab-src` (`homelab_mcp_source_dirs`). Only daniel-box's package ships
  `checkout-sync`, so only its checkout stays at origin/master. A cordoned or down daniel-box
  leaves the pod Pending rather than serving a stale tree
  (`tests/services/test_homelab_mcp_source_mounts.py`).
- **RBAC:** `templates/rbac.yaml.j2` grants a dedicated `homelab-mcp` ServiceAccount
  `get`/`list` on pods, `pods/log`, nodes, deployments and daemonsets — no `watch`, no
  secrets, no exec. Deliberately narrower than the shell's own read-only ServiceAccount.
- **Secrets** (SOPS keys, not values): `homelab_mcp_token`, `claude_ha_token`.
- **Flux applies it.** Its `containers_list` entry is
  in `cluster/services.yml`, so nothing applies it by hand, and a merged change reaches the cluster when
  `promote` publishes and verifies the next bundle. Its Kustomization's `dependsOn` names no other
  role, because the role renders no route.
- **The token Secret substitutes from `homelab-cluster-vars`.** `homelab_mcp_token` and
  `claude_ha_token` are `${<key>}` placeholders in the published render. Flux substitutes the
  values from the `homelab-cluster-vars` Secret, which it decrypts from
  `secrets/cluster-vars.sops.env`. The Kustomization depends on `cluster-vars`, so a
  rotated value is live before homelab-mcp reconciles. A rotation reaches the cluster only
  through `cluster_vars.py write`, which the operator runs after the hand rotation: both keys
  are `assisted`-tier in `secrets/secret_rotation.yml`. The rotation rolls the pod through the
  restart checksum.
- **A `homelab_mcp_token` rotation crosses two Kustomizations.** traefik's gate Secret
  matches the same token in the `homelab-mcp-local` router's `Authorization` header. One
  promotion carries both copies, but traefik's Kustomization applies the edge's copy and
  homelab-mcp's applies the pod's, each when it reconciles. Bearer calls fail until both
  have applied.
- **Flux prunes the cluster-scoped RBAC.** Flux prunes by its own inventory, which holds the `homelab-mcp-read`
  ClusterRole and ClusterRoleBinding,
  so dropping one from `rbac.yaml.j2` deletes it live at the next promotion.
  Deleting the Kustomization deletes them too, because the tag takes the default deletion policy.
- **Its image build and rollout.** Promote's `images` job
  (`scripts/deploy_tools/ci_images.py`) builds the image and pushes it to GHCR. The renderer
  writes the build context to `DIR/images/homelab-mcp/` and its content tag to
  `DIR/images/tags.json`, and the Deployment's ref reads the same tag. The role's
  `manifests_rollout: homelab-mcp` names the Deployment that the Kustomization's `wait`
  holds and that the renderer stamps the restart checksum on.
  `k8s_autodeploy: true` gates no apply: Flux applies
  every merged change. It still decides whether Renovate automerges the image bump.

## Notable
- `image_builder_context_files` (not `image_builder_context`) carries `app.py`,
  `safe_reads.py`, `k8s_reads.py` and `obs_api.py` verbatim into the build — they embed PromQL
  whose `{{...}}` braces a Jinja render would otherwise eat. The renderer reads the same list
  for the context it writes.
- **Every Prometheus and Loki read goes through `obs_api`**, the client probe.py and
  postflight share (`scripts/diagnostics/probe_lib/obs_api.py`, #2860). The image carries it
  flat at `/app/obs_api.py`, so it stays stdlib-only with no first-party import;
  `scripts/diagnostics/tests/test_obs_api.py` imports it with `scripts/` off the path. The file
  is part of the build context, so a merged edit to it alone moves homelab-mcp's content tag,
  and the next promotion builds the image and rolls the pod.
- The build paths in `defaults/main.yml` come from `role_file()`. The render context resolves
  them against this role before image-builder reads them, so they name this role's files even
  though the build renders inside `k8s/image-builder`.

## Editing
- App: `files/app.py`, `files/safe_reads.py` (tests in `tests/`), the shared
  `scripts/diagnostics/probe_lib/obs_api.py` · Image:
  `templates/Dockerfile.j2` · RBAC: `templates/rbac.yaml.j2`.
