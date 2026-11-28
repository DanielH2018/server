# Obsidian vault connector plan

Claude can search, read and later edit the Obsidian vault from every Claude surface: Claude
Duet, claude.ai, the mobile app, Desktop and Claude Code. Edits reach every Obsidian device
through the existing LiveSync sync. The work is tracked in the
[Obsidian vault connector milestone](https://github.com/DanielH2018/server/milestone/46),
one issue per slice.

## Status

| Slice | What it delivers | State |
|---|---|---|
| 0 | Duet calls a claude.ai custom connector | Operator check, [#10531](https://github.com/DanielH2018/server/issues/10531) |
| 1 | The vault mirrored into the cluster as markdown | Landed: [#10329](https://github.com/DanielH2018/server/pull/10329), [#10529](https://github.com/DanielH2018/server/pull/10529) |
| 1b | `livesync-data` backed up to weekly B2 | [#10532](https://github.com/DanielH2018/server/issues/10532) |
| 2 | A read-only MCP server on the LAN | [#10533](https://github.com/DanielH2018/server/issues/10533) |
| 3 | Authelia OAuth and the public route, so claude.ai and Duet connect | [#10534](https://github.com/DanielH2018/server/issues/10534), blocked by #10533 |
| 4 | Write tools, with no delete | [#10535](https://github.com/DanielH2018/server/issues/10535), blocked by #10534 |
| 5 | An ADR, and this plan archived | [#10537](https://github.com/DanielH2018/server/issues/10537), blocked by #10534 |

Each issue carries its slice's steps and its done-when check. This page holds the decisions
and the shape they share.

## Decisions

- **One remote MCP server, added once as a claude.ai custom connector.** Duet runs on a
  cloud VM and "carries over your memory, skills, and connected apps from your Claude
  account" ([Introducing Claude Duet](https://claude.com/blog/claude-duet)). A route tied to
  a desktop, such as Obsidian's Local REST API plugin or a local filesystem MCP, cannot be
  reached while the laptop sleeps. claude.ai's connectors reach a server from Anthropic's
  range `160.79.104.0/21`, over public HTTPS on port 443.
- **`livesync-cli daemon` turns the vault into files.** It comes from the LiveSync plugin's
  own repository, reuses the plugin's sync core (E2EE, conflict handling), and publishes
  `ghcr.io/vrtmrz/livesync-cli`. `vrtmrz/livesync-bridge` was rejected: it calls itself
  unreleased and has open data-loss issues, including deletions that never reach CouchDB, late
  chunks dropped for good, and silent stalls.
- **The MCP server is a small Python FastMCP server in the role**, built as a custom image
  the way `k8s/roles/homelab-mcp/` builds its own. The remote-capable Obsidian MCP servers
  found had one to three stars, and the popular ones need a running Obsidian app.
- **Auth is a static Authelia OIDC client, with JWT access tokens checked against Authelia's
  JWKS.** Authelia has no Dynamic Client Registration, and claude.ai's "Use your own OAuth
  client" option makes it unnecessary.
- **The vault keeps LiveSync's end-to-end encryption.** The plugin uses E2EE with the V2
  algorithm, `encryptInternalMetadata` and `usePathObfuscation`, and the bridge's settings
  carry the same values. The bridge holds the passphrase, so the mirror under `/vault` is
  plaintext.
- **The plaintext mirror is not backed up; the ciphertext is.** A backup of `/vault` would
  put the decrypted vault on B2. `livesync-data` holds only ciphertext, so it is the copy
  that goes on the weekly tier (#10532).

## Shape

```text
Obsidian devices ──LiveSync──▶ livesync (CouchDB :5984)
                                   ▲  netpol_from: vault-mcp
                                   │  two-way
pod vault-mcp ─┬─ livesync-cli daemon ──▶ /vault (plaintext, no backup)
               │                         /data  (PouchDB, no backup)
               └─ vault-mcp (FastMCP, Streamable HTTP :8000)       ◀── slice 2
                                   │
           Traefik public route, use_authelia: false; the app
           enforces OAuth and serves /.well-known/oauth-protected-resource  ◀── slice 3
                                   │
          claude.ai custom connector (static client, Authelia 2FA)
                                   │
             Duet · claude.ai · mobile · Desktop · Claude Code
```

## What slice 1 taught the later slices

- **Two open PRs that add a secret always conflict.** SOPS keeps one MAC over the whole of
  `secrets/secrets.yml`, and `cluster_vars.py write` rewrites the cluster-vars files, so git
  cannot merge two such edits even when their keys differ. Land a PR that adds a secret
  soon after the operator's `sops` step. Slices 2 and 3 each add one.
- **A free-text secret ships as a SOPS manifest, not a Flux placeholder.**
  `cluster_vars.py write` refused the passphrase as a placeholder, because Flux can type
  free text as a non-string. `templates/passphrase-secret.yaml.j2` in `k8s/roles/vault-mcp/`
  is the pattern.
- **A secret inside a structured file is inserted by code that escapes it.** Flux pastes a
  value's raw text, so the `write-settings` init container builds `settings.json` with
  `JSON.stringify`.
- **A new role and the fence that admits it cannot share a PR.** The render-diff gate
  refuses netpol-baseline gaining a `dependsOn` edge while its render moves. The fence
  cannot land first, because a fence may only name a pod label some rendered pod carries.
  The role shipped at 0 replicas, and the fence PR started it.
- **The bridge restarts until netpol-baseline applies.** netpol-baseline applies after every
  other tag, so on first start the pod ran before its fence existed. It failed initial
  replication three times, then synced.

## Folders hidden from Claude

The operator ruled on 2026-11-28: "hide any hidden folders from Claude, and the "94
Docs/superpowers/" folder." Slice 2 holds the ruling in two places.

- A path segment that starts with `.`, such as `.obsidian/` or `.trash/`, is hidden at any
  depth. `vault_notes.py` builds the rule in, so a hidden folder added later is hidden too,
  and no list entry can switch it off.
- `94 Docs/superpowers/` is the one entry of `vault_mcp_exclude_paths` in
  `k8s/roles/vault-mcp/defaults/main.yml`.

No tool, search result or `backlinks` result returns a note under either.

## Open questions for the operator

1. Does Duet get write access in slice 4, or does the vault stay read-only?

## See also

- `k8s/roles/vault-mcp/CLAUDE.md`, the role's traps.
- [Authentication for connectors](https://claude.com/docs/connectors/building/authentication)
  and
  [Get started with custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp),
  the connector requirements slice 3 meets.
