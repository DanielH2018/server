# authelia — SSO / forward-auth for the cluster

Authelia guards most public routes as a Traefik forward-auth middleware. The second factors, the
redis session incident and its init-container gate, the notifier's history, the OIDC derivations and
the three CrowdSec seeding containers are in `docs/authelia-sessions-and-crowdsec-init.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "authelia"`
- **Images:** `authelia/authelia` (`authelia_k8s_image`), `redis` (`authelia_k8s_redis_image`)
- **Route:** `auth.<domain>` · `auth.local.<domain>`, no Authelia
- **Claim:** `authelia-config` (daily -> R2)
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — platform — SSO/OIDC gate; a failed
  deploy locks out access to everything behind it, including the tools to fix it
<!-- /generated_from -->

- **`use_authelia: false` on its own route**, since it is the middleware every other route calls.
- **`authelia-config` is on the daily R2 tier.** Sessions live in redis, not on the claim.
- **Routes name the `authelia` Middleware, which is a chain, not the forwardAuth.** Its first member
  clears X-Forwarded-Host/-Uri/-Method so forwardAuth rebuilds them, and no route names
  `authelia-forwardauth` directly; the same unit is copied into the longhorn-ui and claude-otel
  namespaces. ENFORCED: `ansible/tests/k8s/test_forwardauth_rebuilds_request_target.py`.

## Access control comes from containers_list

A service's policy is the `auth_tier: one_factor | two_factor` beside its `use_authelia: true` in
`inventory/host_vars/<host>.yml`, not a rule in this role. `templates/config-secret.yaml.j2` renders
one rule per tier from those entries through `filter_plugins/authelia_access.py`, plus two scoped
bypasses and two wildcards. A generated rule names only the LAN name, so the public name keeps the
`*.<domain>` wildcard's `two_factor` whatever the entry declares (#2057).

**A `use_authelia: true` entry without `auth_tier` fails the render** — in
`validate/k8s_manifests.py`, in the test render and in a real deploy alike — so a new service cannot ride
the wildcard by omission. ENFORCED:
`ansible/tests/services/test_authelia_access_tiers.py`.

## OIDC clients

Three relying parties, in `templates/config-secret.yaml.j2` behind `authelia_k8s_manage_oidc`:
`jellyfin`, `grafana` and `headlamp`. **Grafana must stay `one_factor`** — `two_factor` stalls the
headless `-m ui` browser on a TOTP prompt no test can answer. **Headlamp's relying party is the KUBERNETES API SERVER**, not the app, so its
`claims_policy` serves the API server's mappings in `roles/setup/k3s` and both of this portal's
issuers must be trusted there. Each client's secret is stored hashed, so one that needs the
plaintext too takes two SOPS keys that rotate together.

## Sessions live in redis

`session.redis` in `templates/config-secret.yaml.j2` points at the `authelia-redis` Deployment this
role also deploys. Without it the provider is in-memory and every roll of the portal destroys every
session, remember-me included.

- **`authelia-redis` is deliberately NOT in `manifests_extra_rollouts`**: the central
  rollout-restart fires whenever this role's manifests change, and rolling the session store on that
  cadence hands back the original bug. `ansible/tests/services/test_authelia_redis_sessions.py`
  pins that, the provider block, the `wait-for-redis` gate and the 6379 agreement.
- **The store is an emptyDir and persists nothing**, so a node reboot or an image bump still logs
  everyone out — and a Longhorn volume here would be a backed-up disk of session credentials.
- **Rotating `authelia_redis_password` needs redis rolled by hand**, because changing
  `templates/redis-secret.yaml.j2` rolls the *authelia* Deployment while redis keeps the old
  password. **The rollback lever** is `authelia_k8s_redis_sessions: false` plus a redeploy.
- **Verify by surviving a restart, not by a health gate.** `probe.py health authelia` reads green on
  the in-memory version: log in with remember-me ticked, roll the Deployment, reload a route.

## Traps

- **`disable_startup_check: true` is deliberate, and it belongs to `notifier`, not to
  `notifier.smtp`** — one level deeper is valid YAML that renders and lints clean and then fails to
  boot. Authelia probes SMTP at boot and refuses to start when that fails, which under `Recreate`
  takes SSO down for the fleet. The `DECIDED:` marker sits at the line. ENFORCED:
  `ansible/tests/services/test_smtp_wiring.py`.
- **`authelia_session*` is a cookie name, not a secret name.** The session secret is
  `authelia_secret`, tracked in `ansible/secret_rotation.yml` at tier `assisted`; the
  `authelia_session*` strings are cookie names
  (`ansible/roles/k8s/authelia/defaults/main.yml:authelia_k8s_cookie_name`), so the registry holds
  no such key and that absence is not an untracked credential.
- **Three init containers seed the crowdsec-agent sidecar, not its entrypoint**, running hub →
  config → data. `crowdsec-hub-install` going FIRST is load-bearing, and **exit 23 is the only
  status the rsync tolerates**. Verify by the sidecar's restart count — one restart failed every
  deploy's health gate until #1173. ENFORCED:
  `ansible/tests/services/test_crowdsec_config_install_seeds_staged_tree.py`,
  `ansible/tests/services/test_crowdsec_hub_install_stages_the_hub_tree.py`.

## The `claude-ui` user

The users database holds the operator and `claude-ui`, the identity the headless UI tier logs in as
to reach the `two_factor` services without a code off a phone.
`authelia_k8s_claude_user` in `defaults/main.yml` has the reasoning, and the TOTP registration
lives in Authelia's SQLite database rather than the rendered `users_database.yml`, re-seeded from
the fixed `authelia_claude_totp_secret` every deploy.

- **Rotating either password needs `-e authelia_k8s_rehash_passwords=true`.** The role reuses each
  user's existing argon2 digest from the live Secret, so `sops set authelia_password` plus an
  ordinary deploy changes nothing while the recap reads green.
- **The hashing block must render the nested form with `variant: argon2id`** — the legacy flat form
  multiplied `memory` by 1024 and passed validation silently (#1621). ENFORCED:
  `ansible/tests/services/test_authelia_config_schema.py`.
- **Revoking Claude's two_factor reach** is deleting the `claude-ui` block from
  `templates/config-secret.yaml.j2` and redeploying; the rules are domain-scoped, never
  `subject:`-scoped.
