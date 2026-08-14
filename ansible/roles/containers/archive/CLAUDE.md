# archive/ — Disabled / parked container roles

**Nothing in this folder is deployed.** These are services that were removed from (or
never added to) the active `containers_list`. They're kept for reference and possible
reactivation. See the repo-root `CLAUDE.md` for shared conventions.

## Status
- None of these appear (uncommented) in `ansible/inventory/host_vars/*.yml` →
  `containers_list`, so `deploy.yml` never touches them.
- **That invariant is not enforced by anything, and it has silently broken once.** From
  `7e6f4453` (2026-08-06) to 2026-08-14, `glances` sat here while `daniel-pi.yml` still
  listed it: the commit retired glances on *daniel-server* and archived the role, but the
  role is shared and the Pi's copy stayed deployed. `deploy.yml:110` resolves roles by
  **bare name** against `roles_path` (`ansible.cfg:16`), which does not recurse into
  `archive/` — so an untagged `-e target=daniel-pi` run would have failed "role not found".
  Tagged deploys narrow `containers_list` and skipped it, which is why it went unnoticed.
  **Before archiving a role, grep every host's `containers_list` for its name**, not just
  the host you are retiring it from.
- Most predate the `meta/deps.yml` dependency system (only `file-browser` and `minecraft`
  have a `meta/`), so the rest have **no dependency declarations**.

## Reactivating one
1. Uncomment (or add) its block in the relevant `host_vars/<host>.yml` `containers_list`
   — set `port`, `networks`, `use_authelia`, `tags`.
2. **Add a `meta/deps.yml`** (e.g. `role_deps: [traefik, authelia]`) so the toposort in
   `deploy.yml` orders it correctly — without it the dep map may not resolve.
3. Move the role folder up to `ansible/roles/containers/<name>/` (out of `archive/`).
4. Deploy: `ansible-playbook ansible/deploy.yml --tags "<name>"`.

Per-service files in each subfolder note what the service is, its image, and the
intended `containers_list` settings recovered from the old commented-out entries.
