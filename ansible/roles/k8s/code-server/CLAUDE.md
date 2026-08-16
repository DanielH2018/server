# code-server — Browser-based VS Code

Moved to k3s on 2026-08-10 (slice-7 Phase C). This role owns both the workload and the image
it is built from (`templates/Dockerfile.j2` + `files/extensions.sh`); the inventory entry is in
`daniel-box.yml`.

## At a glance
- **Image:** built in-cluster by k8s/image-builder from `templates/Dockerfile.j2`
- **Host:** daniel-box (k3s) · **Port:** 8443 · **URL:** `code-server.<domain>` /
  `code-server.local.<domain>` (Authelia: yes)
- **Ported WITHOUT docker plumbing** (operator decision 2026-08-10): no DOCKER_HOST, so the
  in-IDE docker CLI and devcontainers are gone; docker-proxy-codeserver and the `codeserver`
  net dissolved with the Docker copy (`has_code_server: false` in daniel-server host_vars).

## Notable
- Extensions are downloaded at build time (Open VSX + MS Marketplace) into `/opt/vsix`;
  `extensions.sh` installs them into /config on container start.
- **Two claims, and only the small one is backed up** (2026-08-16). `code-server-config`
  (10 Gi) holds derived state and is in `k3s_longhorn_nobackup_volumes`;
  `code-server-workspace` (5 Gi) holds `workspace`, `.ssh`, `.config` and the git identity,
  mounted by subPath, and is what the weekly B2 tier backs up. A first-boot initContainer
  copies them across and is a no-op afterwards. Nothing was deleted — the config volume still
  persists everything across restarts, it just is not backed up.
  Two consequences worth knowing before you touch this:
  - **The git identity lives at `/config/.config/git/config`**, not `~/.gitconfig`, which the
    migration renames to `.gitconfig.pre-split`. Mounting `~/.gitconfig` by subPath would
    break `git config --global`: it writes a `.lock` and `rename()`s it over the target, and
    a rename onto a bind-mounted file fails EBUSY.
  - **Anything new worth keeping must go under one of those four paths.** Dropping a file
    elsewhere in `/config` leaves it live but unbacked, which looks identical until a restore.
- `/config/.local/share/code-server/extensions` held 5.8 G as of 2026-08-16 and the server
  never reads it — it runs with `--extensions-dir /config/extensions`. Orphaned, and the
  reason the config volume sits near its 10 Gi ceiling. Confirm what writes it before
  deleting, so it does not regrow.
- Because the image is built, bump it by redeploying the k8s role — there is no registry tag
  for Renovate to track (`code_server_k8s_image` is in the REGISTRY_BUILT_IMAGES carve-out).

## Editing
- Image: `templates/Dockerfile.j2`, `files/extensions.sh` · Workload: `ansible/roles/k8s/code-server/`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "code-server" -e target=daniel-box`
