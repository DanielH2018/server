# code-server — Browser-based VS Code (build source only)

Moved to k3s on 2026-08-10 (slice-7 Phase C). This role no longer deploys a container — it
remains the **build source** for the cluster image (`templates/Dockerfile.j2` +
`files/extensions.sh`), same split as n8n/ical-proxy. The workload lives in
`ansible/roles/k8s/code-server/`; the inventory entry is in `daniel-box.yml`.

## At a glance
- **Image:** built in-cluster by k8s/image-builder from `templates/Dockerfile.j2`
- **Host:** daniel-box (k3s) · **Port:** 8443 · **URL:** `code-server.<domain>` via
  bridge_hostname, `code-server-k8s.local.<domain>` native (Authelia: yes)
- **Ported WITHOUT docker plumbing** (operator decision 2026-08-10): no DOCKER_HOST, so the
  in-IDE docker CLI and devcontainers are gone; docker-proxy-codeserver and the `codeserver`
  net dissolved with the Docker copy (`has_code_server: false` in daniel-server host_vars).

## Notable
- Extensions are downloaded at build time (Open VSX + MS Marketplace) into `/opt/vsix`;
  `extensions.sh` installs them into /config on container start.
- Because the image is built, bump it by redeploying the k8s role — there is no registry tag
  for Renovate to track (`code_server_k8s_image` is in the REGISTRY_BUILT_IMAGES carve-out).

## Editing
- Image: `templates/Dockerfile.j2`, `files/extensions.sh` · Workload: `ansible/roles/k8s/code-server/`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "code-server" -e target=daniel-box`
