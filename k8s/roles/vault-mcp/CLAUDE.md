# vault-mcp — the Obsidian vault as plain markdown, for Claude's vault connector

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=vault-mcp` gates the rollout
- **Images:** `ghcr.io/vrtmrz/livesync-cli` (`vault_mcp_k8s_livesync_cli_image`),
  `ghcr.io/danielh2018/vault-mcp` (`vault_mcp_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** `vault-mcp-db` (no backup (StorageClass longhorn-nobackup)), `vault-mcp-vault`
  (no backup (StorageClass longhorn-nobackup))
<!-- /generated_from -->

## Notable
- **`livesync-cli daemon` syncs both ways.** It mirrors livesync's `obsidian` database into
  `/vault` as decrypted markdown, and pushes any change made under `/vault` back to CouchDB
  and every Obsidian device. A file written there is a vault edit.
- **The encryption settings must equal the clients'.** The
  `k8s/roles/vault-mcp/defaults/main.yml:vault_mcp_livesync_settings` map carries them. A
  wrong value leaves the bridge unable to decrypt, or writing documents the clients cannot
  read. The values come from the plugin's E2EE dialog. The two secrets reach `settings.json`
  through the `write-settings` init container, because a raw Flux substitution into JSON
  breaks on a `"` in the passphrase.
- **The pod logs in to CouchDB with livesync's admin password, read from livesync's
  `livesync` Secret.** `manifests_outside_secrets` in `defaults/main.yml` declares that key,
  so a `livesync_db_password` rotation rolls this pod too. The entry's
  `depends_on: [livesync]` makes Flux apply livesync first. The passphrase is a SOPS
  manifest of its own (`templates/passphrase-secret.yaml.j2`), because Flux can type
  free text as a non-string.
- **`/vault` is plaintext, so neither claim is backed up.** Both rebuild from CouchDB. A
  backup of the vault belongs on `livesync-data`, whose documents are ciphertext.
- **livesync's entry lists `vault-mcp` under `netpol_from`.** Without that fence the
  default-deny baseline drops the bridge's connections to CouchDB. Renaming this role or its
  pod label means moving that entry in the same change.
- **The `vault-mcp` container serves the vault read-only over MCP on port 8000.** It mounts
  the vault claim with `readOnly: true`, so no tool can write a note, and it reads what the
  daemon writes. Its tools are `search`, `read_note`, `list_folder`, `recent_notes` and
  `backlinks` (`k8s/roles/vault-mcp/files/vault_server.py`). The image builds from
  `templates/Dockerfile.j2` and runs `files/vault_server.py` as `app.py`.
- **`k8s/roles/vault-mcp/files/vault_notes.py:Vault` owns what Claude may not see.** A path
  segment that starts with `.` is hidden at any depth, whatever the list says. Each
  `k8s/roles/vault-mcp/defaults/main.yml:vault_mcp_exclude_paths` entry hides its folder by
  whole path segments, ignoring case. A hidden path raises the same `NoteNotFound` as a
  missing one, and no walk follows a symlink. Every tool reads through `Vault`, so a tool that
  read `/vault` directly would skip the rule.
- **The route is `vault-mcp.local.<domain>`, LAN-only, behind a bearer token.** The
  `vault-mcp-local` router lives in traefik's file-provider gate Secret
  (`k8s/roles/traefik/templates/livesync-gate-secret.yaml.j2`), as homelab-mcp's does, so the
  token stays out of CRD objects the readonly kubeconfig can list. The app checks the same
  token again with homelab-mcp's `safe_reads.bearer_token_valid`, which the image carries.
- **A `vault_mcp_token` rotation crosses two Kustomizations.** traefik's gate Secret matches
  the token, and this role's `vault-mcp-token` Secret hands it to the app. Each copy applies
  when its own Kustomization reconciles, so bearer calls fail until both have applied.
- **The daemon has no probes; the server has two.** The daemon serves nothing, so a crash
  shows as a restart. The server's readiness probe calls `/health`, which the bearer check
  exempts.

## Editing
- Manifests: `templates/`. The Service renders from `k8s/templates/service-default.yaml.j2`,
  off the containers_list entry. No IngressRoute: the route is traefik's gate Secret.
- Image pins, Renovate stance, resources, uid, the exclude list and the livesync-cli settings:
  `defaults/main.yml`.
- App: `files/vault_notes.py` (tests in `tests/`) and the MCP wiring in
  `files/vault_server.py`. Image: `templates/Dockerfile.j2`.
