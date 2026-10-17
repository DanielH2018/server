# homelab-mcp — the read-only MCP server backing Claude's cluster tools

Serves `list_pods` / `workload_status` / `list_nodes` / `pod_logs` over its own narrow
read-only RBAC identity. Rehomed here from daniel-server (Phase E, 2026-08-13) — the last
routed tenant of the retired Docker edge. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=homelab-mcp` with exit 5
- **Image:** `<k8s_registry_pull_host>/homelab-mcp` (`homelab_mcp_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux's render pulls `ghcr.io/danielh2018/homelab-mcp:<content tag>`**, with
  `imagePullPolicy: IfNotPresent` (D5.4b). The *Image* line above prints the Ansible default,
  which names the in-cluster registry. promote's `images` job builds the image from
  `templates/Dockerfile.j2` and the files below.
- **No IngressRoute** — routed through the k8s edge's file-provider gate Secret (`roles/k8s/traefik/templates/livesync-gate-secret.yaml.j2`)
  instead, so its bearer token stays out of CRD objects the readonly kubeconfig can list.
  **Two routers there, not one.** `homelab-mcp-local` carries the bearer predicate and serves
  everything; `homelab-mcp-probe` is LAN-only, matches the single exact `Path(`/health`)`, and
  requires no token — `_BearerAuth` in `files/app.py` exempts that path, and it returns the bare
  string `ok`. It exists so the Kuma tile can prove the edge ROUTES: the old tile probed `/mcp`
  with no bearer and accepted the 404 that matches no router, which is byte-identical to what a
  router-less edge returns for every host, so it read green through the 3.5-hour outage of #1322
  (#1341). Changing that router to a PathPrefix would open more than `/health`; a test asserts it
  stays an exact Path.
- **A stateless `RollingUpdate` Deployment with one replica.** The default surge starts the
  new pod before the old one stops, so a roll keeps one pod serving.
- **RBAC:** `templates/rbac.yaml.j2` grants a dedicated `homelab-mcp` ServiceAccount
  `get`/`list` on pods, `pods/log`, nodes, deployments and daemonsets — no `watch`, no
  secrets, no exec. Deliberately narrower than the shell's own read-only ServiceAccount.
- **Secrets** (SOPS keys, not values): `homelab_mcp_token`, `claude_ha_token`.
- **Flux applies it, not Ansible** (D5.14, the first Wave C PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. To
  give it back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain
  revert of the flag leaves two writers on the objects. The Kustomization has no Ansible-owned
  dependency to record, because the role renders no route.
- **The token Secret substitutes from `homelab-cluster-vars`.** `homelab_mcp_token` and
  `claude_ha_token` are `${<key>}` placeholders in the published render. Flux substitutes the
  values from the `homelab-cluster-vars` Secret, which it decrypts from
  `ansible/vars/cluster-vars.sops.env`. The Kustomization depends on `cluster-vars`, so a
  rotated value is live before homelab-mcp reconciles. A rotation reaches the cluster only
  through `cluster_vars.py write`, which the operator runs after the hand rotation: both keys
  are `assisted`-tier in `ansible/secret_rotation.yml`. The rotation rolls the pod through the
  restart checksum.
- **A `homelab_mcp_token` rotation crosses two planes.** traefik's gate Secret matches the
  same token in the `homelab-mcp-local` router's `Authorization` header, and traefik stays
  Ansible-owned. So the GitOps deployer applies the edge's copy and `promote` applies the
  pod's, each on its own schedule. Bearer calls fail until both have applied.
- **Flux prunes the cluster-scoped RBAC.** The task's comment says the namespaced prune
  allowlist in `k8s/manifests` never selects the `homelab-mcp-read` ClusterRole and
  ClusterRoleBinding. Flux prunes by its own inventory instead, and that inventory holds both,
  so dropping one from `rbac.yaml.j2` deletes it live at the next promotion.
  `deletionPolicy: Orphan` applies only when the Kustomization itself is deleted.
- **Where the tasks went.** `tasks/main.yml` holds two includes, and no Flux path runs either.
  They stay for the handback. The `k8s/image-builder` include became promote's `images` job
  (D5.4a2, `scripts/deploy_tools/ci_images.py`) and the GHCR ref (D5.4b): the renderer writes
  the build context to `DIR/images/homelab-mcp/` and its content tag to `DIR/images/tags.json`,
  and the Deployment's ref reads the same tag. The `k8s/manifests` include's
  `manifests_rollout: homelab-mcp` named the Deployment for the stabilisation gate and the
  shared restart. The Kustomization's `wait` replaces the gate, and the restart checksum the
  renderer stamps replaces the restart. `k8s_autodeploy: true` no longer gates an apply,
  because the deployer skips the tag.

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
- The Dockerfile path is built from `playbook_dir`, not `role_path`: that render happens
  **inside** `k8s/image-builder`, where `role_path` resolves to the wrong role (bit the first
  deploy, 2026-08-13). It matters again only after a handback.

## Editing
- App: `files/app.py`, `files/safe_reads.py` (tests in `tests/`), the shared
  `scripts/diagnostics/probe_lib/obs_api.py` · Image:
  `templates/Dockerfile.j2` · RBAC: `templates/rbac.yaml.j2`.
