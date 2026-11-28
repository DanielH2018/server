---
id: "0043"
title: The vault is exposed to Claude through an OAuth-protected remote MCP server, read-only
status: Accepted
date: 2026-11-28
governs: []
---

# ADR-0043: The vault is exposed to Claude through an OAuth-protected remote MCP server, read-only

## Status

Accepted on 2026-11-28. Slices 0 to 3 of the vault connector are built. The operator ruled the
same day that the connector starts read-only and may gain write access later. Slice 4
([#10535](https://github.com/DanielH2018/server/issues/10535), edits with no delete) is
deferred, not rejected. The `livesync-data` claim is on the weekly B2 tier
([#10532](https://github.com/DanielH2018/server/issues/10532)).

The plan this record replaces is
[archived](../archive/obsidian-vault-connector-plan.md). The role's traps are in
`k8s/roles/vault-mcp/CLAUDE.md`.

## Context

Claude should search and read the Obsidian vault from every Claude surface: Claude Duet,
claude.ai, the mobile app, Desktop and Claude Code. The vault syncs between devices through
the LiveSync plugin and a CouchDB database, with end-to-end encryption, so the server holds
only ciphertext.

## Decision

One remote MCP server exposes the vault, added once as a claude.ai custom connector. Five
choices shape it.

- **A remote connector, not a desktop route.** Duet runs on a cloud VM and carries over the
  connected apps of the user's Claude account
  ([Introducing Claude Duet](https://claude.com/blog/claude-duet)). A route tied to a desktop,
  such as Obsidian's Local REST API plugin or a local filesystem MCP, cannot be reached while
  the laptop sleeps. claude.ai's connectors reach a server from Anthropic's range
  `160.79.104.0/21`, over public HTTPS on port 443.
- **`livesync-cli daemon` over `livesync-bridge`.** `livesync-cli` comes from the LiveSync
  plugin's own repository, reuses the plugin's sync core (E2EE, conflict handling) and
  publishes `ghcr.io/vrtmrz/livesync-cli`. `vrtmrz/livesync-bridge` calls itself unreleased
  and has open data-loss issues: deletions that never reach CouchDB, late chunks dropped for
  good, and silent stalls.
- **An in-repo FastMCP server.** It is a small Python server built as a custom image, the way
  `k8s/roles/homelab-mcp/` builds its own. The remote-capable Obsidian MCP servers found had
  one to three stars, and the popular ones need a running Obsidian app.
- **A static Authelia OIDC client, with JWT access tokens checked against Authelia's JWKS.**
  Authelia has no Dynamic Client Registration, and claude.ai's "Use your own OAuth client"
  option makes one unnecessary. The route carries no Authelia forward-auth middleware, because
  claude.ai's connector cannot pass it, so the app enforces OAuth itself.
- **The plaintext mirror is unbacked and the ciphertext is backed up.** The vault keeps
  LiveSync's end-to-end encryption, and the daemon holds the passphrase, so `/vault` is
  plaintext. A backup of `/vault` would put the decrypted vault on B2. `livesync-data` holds
  only ciphertext, so it is the copy that goes on the weekly tier.

The server is read-only. The `vault-mcp` container mounts the vault claim with
`readOnly: true`, so no tool can write a note. The server also hides every path segment that
starts with `.` and each entry of `vault_mcp_exclude_paths`, which the operator ruled on
2026-11-28.

```text
Obsidian devices --LiveSync--> livesync (CouchDB :5984)
                                   ^  netpol_from: vault-mcp
pod vault-mcp --+-- livesync-cli daemon --> /vault (plaintext, no backup)
                |                           /data  (PouchDB, no backup)
                +-- vault-mcp (FastMCP, Streamable HTTP :8000)
                                   |
        Traefik public route, no Authelia middleware;
        the app enforces OAuth and serves /.well-known/oauth-protected-resource
                                   |
         claude.ai custom connector (static client, Authelia 2FA)
```

## Consequences

- **Writes need a second decision.** Slice 4 adds write tools. The daemon already syncs both
  ways, so a file written under `/vault` becomes a vault edit on every device. The read-only
  mount is the control that slice 4 would change.
- **The public route trusts the app's token check.** The server checks each token offline
  against the JWKS, so an issued access token stays valid for up to its lifespan after the
  client is cut off.
- **CrowdSec gets no allowlist for Anthropic's range.** `160.79.104.0/21` is shared by every
  claude.ai user's connectors, and the trusted-remote whitelist keeps to `/32` exits.
- **Two open PRs that add a secret always conflict.** SOPS keeps one MAC over the whole of
  `secrets/secrets.yml`, so git cannot merge two such edits even when their keys differ.
- **The plaintext lives in a PVC without a backup.** A lost `vault-mcp-vault` claim rebuilds
  from CouchDB, and a lost CouchDB rebuilds from an Obsidian device.
- **The static client is public, with no secret.** As a confidential client, claude.ai's token
  calls failed, although the stored secret validated against its digest. Authelia answered "The
  provided client secret did not match the registered client secret." The operator ruled on
  2026-11-28 to make the client public. Explicit consent and the operator-only `two_factor` policy are then
  the guards against a tricked approval, and S256 PKCE binds each code to its flow. A
  dynamic-registration proxy would be no stronger. `k8s/roles/authelia/CLAUDE.md` (*OIDC
  clients*) holds the evidence.

The remaining traps the build hit are in `k8s/roles/vault-mcp/CLAUDE.md`.

## Governs

No `# DECIDED:` marker carries this record. The decisions are spread over the role's
templates and defaults, and `k8s/roles/vault-mcp/CLAUDE.md` names each.
