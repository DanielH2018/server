# archive/ — Disabled / parked container roles

**Nothing in this folder is deployed.** These are services that were removed from (or
never added to) the active `containers_list`. They're kept for reference and possible
reactivation. See the repo-root `CLAUDE.md` for shared conventions.

## Status
- None of these appear (uncommented) in `ansible/inventory/host_vars/*.yml` →
  `containers_list`, so `deploy.yml` never touches them.
- Most predate the `meta/deps.yml` dependency system (only `file-browser` has a `meta/`),
  so they have **no dependency declarations**.

## Reactivating one
1. Uncomment (or add) its block in the relevant `host_vars/<host>.yml` `containers_list`
   — set `port`, `networks`, `use_authelia`, `tags`.
2. **Add a `meta/deps.yml`** (e.g. `role_deps: [traefik, authelia]`) so the toposort in
   `deploy.yml` orders it correctly — without it the dep map may not resolve.
3. Move the role folder up to `ansible/roles/containers/<name>/` (out of `archive/`).
4. Deploy: `ansible-playbook ansible/deploy.yml --tags "<name>"`.

Per-service files in each subfolder note what the service is, its image, and the
intended `containers_list` settings recovered from the old commented-out entries.
