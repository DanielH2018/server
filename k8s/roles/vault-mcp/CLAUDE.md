# vault-mcp — the Obsidian vault as plain markdown, for Claude's vault connector

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=vault-mcp` gates the rollout
- **Images:** `ghcr.io/vrtmrz/livesync-cli` (`vault_mcp_k8s_livesync_cli_image`),
  `ghcr.io/danielh2018/vault-mcp` (`vault_mcp_k8s_image`)
- **Route:** `vault-mcp.<domain>` · `vault-mcp.local.<domain>`, no Authelia
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
  whole path segments, ignoring case and Unicode normal form. An entry with a `.` or `..`
  segment raises ValueError when `Vault` is built. A hidden path raises the same
  `NoteNotFound` as a missing one, and no walk follows a symlink. Every tool reads through
  `Vault`, so a tool that read `/vault` directly would skip the rule.
- **The server refuses to start without `VAULT_MCP_EXCLUDE_PATHS`.** An unset variable
  would show the excluded folders, so
  `k8s/roles/vault-mcp/files/vault_notes.py:exclude_paths_from_env` exits. An empty value
  hides no folder and logs a warning.
- **The route is `vault-mcp.<domain>`, with no Authelia middleware, and the app enforces
  OAuth.** claude.ai's custom connector cannot pass forward-auth, so it signs in through
  Authelia's `vault-mcp` OIDC client (`k8s/roles/authelia/templates/config-secret.yaml.j2`)
  with 2FA, and sends the RS256 access token Authelia issues.
  `k8s/roles/vault-mcp/files/oauth_resource.py:ResourceServer` checks each token's signature
  against Authelia's JWKS, its `typ`, issuer, audience, its `client_id` (`vault-mcp`) and its
  `exp`, `nbf` and `iat`, allowing 60 seconds of clock skew. Any error during the check refuses
  the token. A request without a valid token gets 401 with the `WWW-Authenticate` challenge
  that names the protected-resource metadata, which the app serves at
  `/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-protected-resource`.
  The challenge carries `error="invalid_token"` only when the request sent a bearer token.
- **Only a GET of `/health` or of either metadata path passes without a token.** The match is
  exact, so `/health/`, `/HEALTH` and `/health/../mcp` get the 401 too.
  `k8s/roles/vault-mcp/tests/test_vault_server.py` drives the real ASGI app over these paths.
- **To cut claude.ai off, set `authelia_k8s_vault_mcp_client: false` in
  `k8s/roles/authelia/defaults/main.yml`.** The client is public, so no secret rotation can
  revoke it. The switch stops new sign-ins and refreshes once it lands. An access token already
  issued stays valid for up to its 1h lifespan, because this server checks tokens offline
  against the JWKS and does not introspect. Without the switch, a connector that keeps
  refreshing never signs in again (`k8s/roles/authelia/CLAUDE.md`).
- **`k8s/vars.yml:vault_mcp_connector_url` is the one copy of the connector URL.** It is the
  metadata's `resource`, the `aud` every token must hold, and the Authelia client's only
  `audience`. claude.ai sends it as the RFC 8707 `resource`, and Authelia grants only an
  audience the client lists, so the URL the connector is added with must equal it exactly.
- **The pod fetches the JWKS from `auth.local.<domain>`, through Traefik, on the first token.**
  It caches the keys and refetches on an unknown `kid`, at most once per 30 seconds. A JWKS
  it cannot fetch refuses every token and logs a warning, and `/health` still answers.
- **The server refuses to start without `VAULT_MCP_RESOURCE_URL`, `VAULT_MCP_ISSUER` or
  `VAULT_MCP_JWKS_URL`**, because no token could pass.
- **The daemon has no probes; the server has two, and blackbox probes its route.** The daemon
  serves nothing, so a crash shows as a restart. The server's readiness probe calls `/health`,
  which the OAuth check exempts. blackbox-extra's `http_401` target calls `/mcp` on the LAN
  name and expects the 401, which proves the route reaches the app and the app refuses an
  unauthenticated call.
- **The decisions are in
  `docs/adr/0043-the-vault-is-exposed-through-an-oauth-remote-mcp-server.md`.** The server is
  read-only by the operator's ruling of 2026-11-28, and write tools are deferred.
- **No CrowdSec allowlist covers claude.ai.** Anthropic's range `160.79.104.0/21` is shared by
  every user's connectors, and the trusted-remote whitelist keeps to `/32` exits. A connector
  sends one unauthenticated request per sign-in, then a few metadata reads, so the first live
  sign-in is the check that CrowdSec and Cloudflare let it through.
- **The connector signs in as a public client, with no secret.** As a confidential client,
  claude.ai's token calls failed, although the stored secret and its digest agreed. Authelia
  answered "The provided client secret did not match the registered client secret." The
  operator ruled on
  2026-11-28 to make the client public (`k8s/roles/authelia/CLAUDE.md`, *OIDC clients*). To
  add the connector on claude.ai, give the Client ID `vault-mcp` and leave the secret blank.
- **The LAN bearer route is gone.** A client that sends the old static token gets 401 and signs
  in through OAuth.
- **Two open PRs that add a secret always conflict.** SOPS keeps one MAC over the whole of
  `secrets/secrets.yml`, and `cluster_vars.py write` rewrites the cluster-vars files, so git
  cannot merge two such edits even when their keys differ. Land a PR that adds a secret soon
  after the operator's `sops` step.
- **A secret inside a structured file is inserted by code that escapes it.** Flux pastes a
  value's raw text, so the `write-settings` init container builds `settings.json` with
  `JSON.stringify`.
- **A new role and the fence that admits it cannot share a PR.** The render-diff gate refuses
  netpol-baseline gaining a `dependsOn` edge while its render moves, and a fence may only name
  a pod label some rendered pod carries. Ship the role at 0 replicas, then start it in the
  fence PR.
- **The daemon restarts until netpol-baseline applies.** netpol-baseline applies after every
  other tag, so on first start the pod runs before its fence exists. It failed initial
  replication three times, then synced.

## Editing
- Manifests: `templates/`. The Service renders from `k8s/templates/service-default.yaml.j2`,
  off the containers_list entry, and the IngressRoute from
  `k8s/templates/ingressroute-default.yaml.j2`.
- Image pins, Renovate stance, resources, uid, the exclude list and the livesync-cli settings:
  `defaults/main.yml`.
- App: `files/vault_notes.py` and `files/oauth_resource.py` (tests in `tests/`), and the MCP
  wiring in `files/vault_server.py`. Image: `templates/Dockerfile.j2`.
