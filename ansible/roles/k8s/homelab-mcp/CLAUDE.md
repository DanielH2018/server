# homelab-mcp — the read-only MCP server backing Claude's cluster tools

Serves `list_pods` / `workload_status` / `list_nodes` / `pod_logs` over its own narrow
read-only RBAC identity. Rehomed here from daniel-server (Phase E, 2026-08-13) — the last
routed tenant of the retired Docker edge. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** built in-cluster by `k8s/image-builder` from `templates/Dockerfile.j2` and the
  files below — `imagePullPolicy: Always`, rebuilt every deploy rather than pulled from a
  vendor tag.
- **Deploy tag:** `--tags "homelab-mcp"`. No IngressRoute — routed through the k8s edge's
  file-provider gate Secret (`roles/k8s/traefik/templates/livesync-gate-secret.yaml.j2`)
  instead, so its bearer token stays out of CRD objects the readonly kubeconfig can list.
- **Storage:** none — no PVC, stateless RollingUpdate Deployment.
- **Auto-deploy:** eligible, but the promotion cannot actually fire — the image is a
  registry-built `:latest` ref with no upstream version for Renovate to compare against;
  only `templates/Dockerfile.j2`'s `FROM` line moves.
- **RBAC:** `templates/rbac.yaml.j2` grants a dedicated `homelab-mcp` ServiceAccount
  `get`/`list` on pods, `pods/log`, nodes, deployments and daemonsets — no `watch`, no
  secrets, no exec. Deliberately narrower than the shell's own read-only ServiceAccount.
- **Secrets** (SOPS keys, not values): `homelab_mcp_token`, `claude_ha_token`.

## Notable
- `image_builder_context_files` (not `image_builder_context`) carries `app.py` and
  `safe_reads.py` verbatim into the build — they embed PromQL whose `{{...}}` braces a Jinja
  render would otherwise eat.
- The Dockerfile path is built from `playbook_dir`, not `role_path`: that render happens
  **inside** `k8s/image-builder`, where `role_path` resolves to the wrong role (bit the first
  deploy, 2026-08-13).

## Editing
- App: `files/app.py`, `files/safe_reads.py` (tests in `tests/`) · Image:
  `templates/Dockerfile.j2` · RBAC: `templates/rbac.yaml.j2`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "homelab-mcp"`.
