# vault-mcp — the Obsidian vault as plain markdown, for Claude's vault connector

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=vault-mcp` gates the rollout
- **Image:** `ghcr.io/vrtmrz/livesync-cli` (`vault_mcp_k8s_livesync_cli_image`)
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
- **The Service on port 8000 has no listener yet.** It is the port the MCP server takes.
- **No probes.** The daemon serves nothing, so there is nothing for a probe to call. A
  crash shows as a restart.

## Editing
- Manifests: `templates/`. The Service renders from `k8s/templates/service-default.yaml.j2`, off the containers_list entry. No IngressRoute: callers reach it in-cluster by Service name.
- Image pin, Renovate stance, resources, uid and the livesync-cli settings: `defaults/main.yml`.
