# authelia — SSO / forward-auth for the cluster

Authelia guards most public routes as a Traefik forward-auth middleware. The second factors, the
redis incident, the notifier's history and the OIDC derivations are in
`docs/authelia-sessions-and-crowdsec-init.md`; the CrowdSec seeding containers are in
`docs/crowdsec-waf-record.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=authelia` gates the rollout
- **Images:** `authelia/authelia` (`authelia_k8s_image`), `redis` (`authelia_k8s_redis_image`)
- **Route:** `auth.<domain>` · `auth.local.<domain>`, no Authelia
- **Claim:** `authelia-config` (daily -> R2)
<!-- /generated_from -->

- **`use_authelia: false` on its own route**, since it is the middleware every other route calls.
- **`authelia-config` is on the daily R2 tier.** Sessions live in redis, not on the claim.
- **Routes name the `authelia` Middleware, which is a chain, not the forwardAuth.** Its first member
  clears X-Forwarded-Host/-Uri/-Method so forwardAuth rebuilds them, and no route names
  `authelia-forwardauth` directly; the same unit is copied into the longhorn-ui and observability
  namespaces. ENFORCED: `tests/k8s/edge/test_forwardauth_rebuilds_request_target.py`.

## Access control comes from containers_list

A service's policy is the `auth_tier: one_factor | two_factor` beside its `use_authelia: true` in
`hosts/<host>/vars.yml`, not a rule in this role. `templates/config-secret.yaml.j2` renders
one rule per tier from those entries through `template_filters/authelia_access.py`, plus one scoped
bypass, Karma's pair and two wildcards. A generated rule names only the LAN name, so
the public name keeps the `*.<domain>` wildcard's `two_factor` whatever the entry declares (#2057).

**`karma.local.<domain>` and `karma.<domain>` are the `subject:`-scoped rules.** Karma can silence a
page, so each host takes `two_factor` for `group:admins`, and a `deny` rule for the same domain
follows it. Authelia skips a rule whose subject does not match, so without the `deny` a user
outside `admins` would fall through to a wildcard: `*.local` at `one_factor`, or `*.<domain>` at
`two_factor`. ENFORCED:
`tests/services/authelia/test_authelia_access_tiers.py::test_a_user_outside_admins_is_denied_a_silence_ui`
and `tests/services/authelia/test_authelia_access_tiers.py::test_the_public_silence_ui_admits_admins_only_from_any_source`.

**A `use_authelia: true` entry without `auth_tier` fails the render** — in
`validate/render_lib/k8s.py`, in the test render and in a real deploy alike — so a new service cannot ride
the wildcard by omission. ENFORCED:
`tests/services/authelia/test_authelia_access_tiers.py`.

## OIDC clients

Four relying parties, in `templates/config-secret.yaml.j2` behind `authelia_k8s_manage_oidc`:
`jellyfin`, `grafana`, `headlamp` and `vault-mcp`. **Grafana must stay `one_factor`** — `two_factor` stalls the
headless `-m ui` browser on a TOTP prompt no test can answer. **Headlamp's relying party is the KUBERNETES API SERVER**, not the app, so its
`claims_policy` serves the API server's mappings in `hosts/components/k3s-server/templates/authentication-config.yaml.j2` and both of this portal's
issuers must be trusted there. Each confidential client's secret is stored hashed, so one that
needs the plaintext too takes two SOPS keys that rotate together.

**`vault-mcp` is the claude.ai custom connector, and its relying party is the vault-mcp
server.** The server accepts only an RS256 access token whose `aud` holds
`k8s/vars.yml:vault_mcp_connector_url`, the client's only `audience`. Its `vault_mcp`
authorization policy admits the operator alone, at `two_factor`, and every sign-in shows the
consent page (`consent_mode: 'explicit'`).
`tests/services/authelia/test_authelia_vault_mcp_client.py` holds the public client type, the
policy, PKCE, the redirect URI, grant types and consent.
`k8s/roles/vault-mcp/tests/test_oauth_resource.py::test_authelia_grants_the_audience_and_issuer_the_rendered_server_requires`
holds the client and the server to the same URL and issuer.

**`vault-mcp` is a public client, with no secret.** The operator ruled so on 2026-11-28. As a
confidential client, its token calls from claude.ai failed with "The provided client secret did
not match the registered client secret." The operator showed that the server side was right in
three ways. The SOPS plaintext validated against its PBKDF2 digest. The digest Authelia loaded
was byte-identical to the one in SOPS. A hand-made `client_secret_post` call with that secret
got invalid_grant, not invalid_client. So claude.ai sent the wrong secret. Its docs say that
a connector added with the secret left blank uses the client ID as a public client.

**Without a secret, consent and 2FA are the only guards against a tricked approval.** Anyone can
add a claude.ai connector with the client ID `vault-mcp`. It shares the one redirect URI, so the
consent page looks the same whoever started the sign-in. The operator must approve only a
sign-in they started themselves. A browser that holds a live `two_factor` session, which
`remember_me: '1M'` keeps for a month, asks for no 2FA, so there the consent page is the only
guard. S256 PKCE binds each code to the client that started its flow.
A dynamic-registration proxy would be no stronger, because it would register any caller too.

**A refreshing connector never asks for 2FA again.** The `vault_mcp` lifespan gives a refresh
token 30 days, and Authelia 4.39.28's provider (authelia.com/provider/oauth2 v0.3.2) gives each
refreshed token a fresh 30 days from the refresh. The 30 days are therefore an idle limit, not a
login limit, and the provider has no cap on a grant's total age. A stolen refresh token works
without a secret until it idles out. To cut the connector off, set
`authelia_k8s_vault_mcp_client: false`. That stops new sign-ins and refreshes once it lands. An
access token already issued stays valid for up to its 1h lifespan, because the vault-mcp server
does not introspect.

The signing key has a Secret of its own, `authelia-oidc-jwk` from
`templates/oidc-jwk-secret.yaml.j2`, because Flux strips the newlines from a placeholder's value
(target slice R2, PR 7). The role lists that file in `render_sops_manifests`, so a placeholder
render leaves it out. The Deployment mounts it at `/oidc-jwk`, and `jwks[].key` reads it with a
`secret` expression under Authelia's `template` config filter, which `X_AUTHELIA_CONFIG_FILTERS`
turns on. All three sit behind the lever.

## Sessions live in redis

`session.redis` in `templates/config-secret.yaml.j2` points at the `authelia-redis` Deployment this
role also deploys. Without it the provider is in-memory and every roll of the portal destroys every
session, remember-me included.

- **`authelia-redis` is in `manifests_extra_rollouts` with `restart_on: ['secret']`**: the drain
  waits on its roll, and the restart checksum the renderer stamps on it hashes only the
  `authelia-redis` Secret its pod reads. Its pod must name none of the portal's ConfigMaps or
  Secrets, or a portal config change would roll the session store and hand back the original
  bug.
  `tests/services/authelia/test_authelia_redis_sessions.py` pins that, the provider block, the
  `wait-for-redis` gate and the 6379 agreement.
- **The store is an emptyDir and persists nothing**, so a node reboot or an image bump still logs
  everyone out — and a Longhorn volume here would be a backed-up disk of session credentials.
- **Rotating `authelia_redis_password` or editing redis.conf rolls redis** through its
  restart checksum (#7575), which logs everyone out once. **The rollback lever** is `authelia_k8s_redis_sessions: false` plus a redeploy.
- **Verify by surviving a restart, not by a health gate.** `probe.py health authelia` reads green on
  the in-memory version: log in with remember-me ticked, roll the Deployment, reload a route.

## Traps

- **`disable_startup_check: true` is deliberate, and it belongs to `notifier`, not to
  `notifier.smtp`** — one level deeper is valid YAML that renders and lints clean and then fails to
  boot. Authelia probes SMTP at boot and refuses to start when that fails, which under `Recreate`
  takes SSO down for the fleet. The `DECIDED:` marker sits at the line. ENFORCED:
  `tests/services/alertmanager/test_smtp_wiring.py`.
- **With OIDC on, the config file is a Go template, so it may hold one `{{` only.** A secret value
  holding `{{` would stop Authelia starting. `cluster_vars.py write` refuses a `{{` in any
  value, naming the key and never the value. `oidc-jwk-secret.yaml` must stay in the `manifests_files` expression,
  because a passed list wins whole. Log level `trace` writes each filtered config, the key
  included, to the log as base64. ENFORCED:
  `tests/services/authelia/test_authelia_oidc_jwk_secret.py`.
- **`authelia_session*` is a cookie name, not a secret name.** The session secret is
  `authelia_secret`, tracked in `secrets/secret_rotation.yml` at tier `assisted`; the
  `authelia_session*` strings are cookie names
  (`k8s/roles/authelia/defaults/main.yml:authelia_k8s_cookie_name`), so the registry holds
  no such key and that absence is not an untracked credential.
- **The `local.` cookie must stay first in `session.cookies`.** Authelia takes the first cookie
  domain that matches the request, so with the public entry first a `.local` login lands on the
  public cookie. Each entry needs its own `name`, or the two sessions overwrite each other.
  `.local` one_factor works only because LAN requests arrive from internal IPs; a login through
  Cloudflare arrives from a public IP and is forced to two_factor.
- **A cookie whose `default_redirection_url` equals its `authelia_url` crash-loops Authelia**,
  which refuses that config at boot.
- **A 431 with Traefik's `OriginStatus: 0` behind SSO is Authelia's read buffer**, not the
  backend. forwardAuth sends Authelia the full URI and cookies, and fasthttp rejects a request
  larger than `server.buffers.read` (16384 in `templates/config-secret.yaml.j2`). The Pi-hole
  query log's long URLs hit it. Raise the buffer rather than chase the backend.
- **Three init containers seed the crowdsec-agent sidecar, not its entrypoint**, running hub →
  config → data. `crowdsec-hub-install` going FIRST is load-bearing, and **exit 23 is the only
  status the rsync tolerates**. Verify by the sidecar's restart count — one restart failed every
  deploy's health gate until #1173. ENFORCED:
  `tests/services/crowdsec/test_crowdsec_config_install_seeds_staged_tree.py`,
  `tests/services/crowdsec/test_crowdsec_hub_install_stages_the_hub_tree.py`.

## The `claude-ui` user

The users database holds the operator, `claude-ui` and `claude-agent`. `claude-ui` is the identity the
headless UI tier logs in as to reach the `two_factor` services without a code off a phone.
`claude-agent` is the agent user's login for the homelab-ui MCP server: no groups, no TOTP. The
`claude_code` role renders its SOPS password into the agent's home, so a rotation also needs a
`claude_code` apply. `authelia_k8s_agent_user` in `defaults/main.yml` has the reasoning.
`authelia_k8s_claude_user` in `defaults/main.yml` has the reasoning, and the TOTP registration
lives in Authelia's SQLite database rather than the rendered `users_database.yml`. The
`seed-claude-totp` init container re-seeds it from the fixed `authelia_claude_totp_secret` on every
pod start, and fails open so a seed failure never takes SSO down; the `DECIDED:` marker sits at
the container in `templates/deployment.yaml.j2`. ENFORCED:
`tests/services/authelia/test_authelia_claude_ui_user.py`.

- **Rotating a password means minting its digest too.** The template reads each user's digest
  from SOPS, `authelia_password_argon2` and its two siblings, never the plaintext. So
  `sops set authelia_password` plus an ordinary deploy changes nothing while the recap reads
  green. The operator-only procedure is *Authelia user passwords* in `docs/secret-rotation.md`.
- **`cluster_vars.py write` refuses a bad digest before it reaches the cluster.** It requires
  each of the three `_argon2` keys to be the hashing block's argon2id PHC form, and no two to be
  equal. A bad digest locks every user out of SSO. The refusal names the key and never the value.
  ENFORCED: `scripts/secrets_mgmt/tests/test_cluster_vars_write_refusals.py`, and `scripts/secrets_mgmt/tests/test_cluster_vars_write.py` for the `{{` refusal. A digest checks
  only when the placeholder render places it, which
  `tests/services/authelia/test_authelia_claude_ui_user.py` holds to the manage flags.
- **Flux applies authelia.** Its `containers_list`
  entry is in `k8s/services.yml`, so nothing applies it by hand, and
  a merged change reaches the cluster when `promote` publishes and verifies the next bundle.
  A rotation
  lands without `--tags`, as
  step 4 of *Authelia user passwords* in `docs/secret-rotation.md` describes.
- **The hashing block must render the nested form with `variant: argon2id`** — the legacy flat form
  multiplied `memory` by 1024 and passed validation silently (#1621). ENFORCED:
  `tests/services/authelia/test_authelia_config_schema.py`.
- **Revoking Claude's two_factor reach** is deleting the `claude-ui` block from
  `templates/config-secret.yaml.j2` and redeploying. The rules are domain-scoped except
  Alertmanager's, which `claude-ui` passes through its `admins` group by the operator's ruling
  of 2026-11-05 (#7249).
