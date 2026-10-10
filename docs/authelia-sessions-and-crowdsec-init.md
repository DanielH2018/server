# Authelia sessions, second factors and the SSO traps

Working-out moved off `ansible/roles/k8s/authelia/CLAUDE.md` (#2997), which a session reads on
every touch of the SSO gate. The role doc keeps the access-control rule, the OIDC client list and
the traps a session must not break; this page keeps the second-factor account, the redis session
incident and its init-container gate, the identity-verification notifier's history, the
cookie-name-versus-secret-name rule and the `claude-ui` user. The three CrowdSec seeding init containers
are in `docs/crowdsec-waf-record.md`.

## Second factors

TOTP and WebAuthn, both optional, neither required by any `access_control` rule — the policies
are `bypass` / `one_factor` / `two_factor`, and a `two_factor` rule accepts either factor.

**WebAuthn was never off.** Authelia's `webauthn.disable` defaults to false, so this portal has
offered a security key or passkey since it was built; what #1502 actually changed is that the
knobs are now pinned in `templates/config-secret.yaml.j2` instead of inherited. `display_name`
names this portal in the browser prompt, `selection_criteria.attachment: ''` is the one
non-default value (Authelia's default `cross-platform` asks for a dedicated key and excludes a
phone, Touch ID or Windows Hello), and `enable_passkey_login` stays **false** — that is
first-factor passwordless login, which would change what `one_factor` means for every rule
rather than adding a second-factor choice.

**A credential is bound to the origin it was registered at.** An RP ID must be a registrable
suffix of the origin, and `auth.local.<domain>` and `auth.<domain>` share none that covers
both — so a key enrolled on the LAN portal does not work on the public one. A user who wants
both enrols twice. This is a WebAuthn spec constraint, not a setting to fix.

**Do not add a `webauthn` key from the docs site.** Authelia refuses to start on a key it does
not recognise, this pod rolls under `Recreate` in front of most public routes, and
`validate/k8s_manifests.py` only asks whether the YAML parses. The pinned version's own
`config.template.yml` is the source —
`https://raw.githubusercontent.com/authelia/authelia/v<tag>/config.template.yml` — and
the `authelia-webauthn-is-a-checked-second-factor` row in
`ansible/tests/k8s/_config_property_rows.py` holds the rendered block to the keys someone
checked against that source — deliberately narrower than what 4.39.21 accepted when the keys were checked on 2026-09-29, so adding a key
means editing `WRITTEN_WEBAUTHN_KEYS` there as well, which is where the check belongs.

Enrolment is a browser action at the portal's security settings and needs a physical
authenticator, so it stays an operator task; the headless UI tier is unaffected, because
`ui_login.py` posts to the first-factor API and drives TOTP as `claude-ui` rather than touching
the portal's login page.

## Sessions live in redis

`session.redis` in `templates/config-secret.yaml.j2` points at the `authelia-redis` Deployment
this role also deploys. Without that block Authelia's provider is **in-memory** — the 4.39.21
`config.template.yml` read on 2026-09-29 says "Memory is the provider unless redis is defined" — so every roll of
the portal destroyed every session, including remember-me ones. `remember_me: '1M'` sets the
cookie's lifetime and overrides the inactivity timer; it does not change where the session is
stored. Three rolls in one day on 2026-09-10 is what surfaced it.

**A roll is cheap here, which is why this matters.** The central rollout-restart fires whenever
this role's rendered manifests change, and with several worktrees editing this role at once
that is several times a day. `authelia-redis` is deliberately **not** in
`manifests_extra_rollouts`: putting it there would roll the session store on that same cadence
and hand back the original bug. `ansible/tests/services/test_authelia_redis_sessions.py` pins
that, the redis provider block, and the 6379 agreement between the containerPort, the Service
and the NetworkPolicy.

**`authelia_secret` is load-bearing now.** The pinned template says it "is only used with Redis
/ Redis Sentinel," so until the redis block existed it encrypted nothing.

**The store is an emptyDir and persists nothing.** Sessions are a cache with a one-month
ceiling, and redis runs with `save ''` and `appendonly no`, so a node reboot, an eviction or a
Renovate bump of `authelia_k8s_redis_image` still logs everyone out. A roll of this role no
longer does, which is the whole point. A Longhorn volume here would be a backed-up disk holding
live session credentials.

**Rotating `authelia_redis_password` needs redis rolled by hand.** `templates/redis-secret.yaml.j2`
is a secret manifest, so changing it fires the rollout-restart against the *authelia*
Deployment. Redis keeps the old password in its running process and authentication then fails
until it is rolled:

```
kubectl -n homelab rollout restart deploy/authelia-redis
```

**The rollback lever is `authelia_k8s_redis_sessions: false` plus a redeploy.** Authelia goes
back to memory sessions; the redis Deployment and Service stay applied but unused, because
`kubectl apply` does not prune. Flipping it either way logs everyone out once.

**An unreachable redis at boot used to crashloop the portal, and the `wait-for-redis` init
container is what stops it.** If redis is unreachable at boot Authelia retries the session
backend 19 times at 500ms and then exits (`internal/session/provider.go`, `StartupCheck`) —
about 9.5s of tolerance. Deploying #1599 outran it: the portal restarted 7 times, blew the 300s
rollout wait twice, and SSO was down for about seven minutes.

**The cause is kube-router's source-pod lookup lagging pod creation, not an image pull.** This
paragraph and the comment beside the redis block in `templates/config-secret.yaml.j2` both said
image pull until #1626, and drew the wrong conclusion from it — that the race bites a first
deploy and never a restart, so it was an acceptable cost. The pod's log named an `i/o timeout`
rather than `connection refused`, and redis came up 1/1 on its first try and has never
restarted, so the packets were dropped by `networkpolicy-authelia-redis` not yet being
programmed for the new pod. A policy-programming race recurs on any recreation of the redis pod
— an eviction, a node reboot, a Renovate bump of `authelia_k8s_redis_image` — and under
`Recreate` each occurrence takes SSO down for the fleet, including the tools to fix it.

`wait-for-redis` converts that crashloop into a wait: 60 × 2s of `redis-cli -h authelia-redis
ping`, gated on `PONG` in the output rather than on redis-cli's exit code, which is 0 on a
NOAUTH reply. It runs **last**, after CrowdSec's three seeding steps — those write a local
emptyDir and need no network, so running them first overlaps their work with the policy lag and
leaves the hub → config → data ordering untouched. It uses `redis-cli` rather than the busybox
`nc -z` the sibling gates in `crowdsec` and `n8n` use: that precedent rests on their own image
being busybox-based, and under `Recreate` an init container failing on a missing applet is an
SSO outage rather than a retry. `ansible/tests/services/test_authelia_redis_sessions.py` pins
the gate.

**Neither the deploy's gates nor the repo tests could see the original failure.** The manifests
render and apply cleanly, `probe.py health authelia` reads green once it settles, and the
rollout failure surfaces as a timeout naming `rollout status` rather than the policy. Verify
this one by recreating the redis pod and reading the authelia pod's restart count.

**Verify by surviving a restart, not by a health gate.** `probe.py health authelia` and an
Authelia 302 both read green on the in-memory version — the redirect fires in the forward-auth
middleware before the backend is reached. Log in with remember-me ticked, `kubectl -n homelab
rollout restart deploy/authelia`, wait for Ready, then reload a protected route. Still
authenticated is the pass.

## Traps

### The one-time code arrives by email, and the startup check is off on purpose
Authelia used the **file** notifier until 2026-09-09, which put identity-verification codes in
`/config/notification.txt` inside the pod and made every password reset an operator task. It
now uses the SMTP notifier against Gmail on `submissions://smtp.gmail.com:465`, authenticating
with `smtp_notify_app_password` — the same app password Uptime Kuma and monitor-bridge use.
Codes go to the operator's inbox; nothing needs `exec`.

**`disable_startup_check: true` is deliberate, and it belongs to `notifier`, not to
`notifier.smtp`.** Authelia probes the SMTP server at boot and refuses to start when the probe
fails. This portal is the forward-auth gate in front of most public routes and rolls under
`Recreate`, so a Gmail blip would take SSO down for the fleet — including the tools to fix
it — to protect a mail nobody is waiting on. The credential is not unwatched: monitor-bridge's
`email_backstop` re-authenticates it on a throttle and pages through Uptime Kuma. The
`DECIDED:` marker sits at the line; `ansible/tests/services/test_smtp_wiring.py` holds the
nesting, because one level deeper is valid YAML that renders and lints clean and then fails to
boot.

Codes still expire in ~5 min, the Authelia elevated-session default. Resend in the browser if
one goes stale.

**A schema check covers the parse; nothing covers the boot.**
`ansible/tests/services/test_authelia_config_schema.py` validates the rendered `notifier` block's
values against the vendored schema for the pinned release (#2945). It rejects a wrong address scheme,
a quoted `true` or a malformed timeout. No check covers the pod starting on the config, so a notifier
change reaches production as its first boot. With the startup check off, Authelia opens no SMTP
connection at boot, so a literal stand-in credential proves the config parses and the pod comes up.

**HISTORY — a staging guest rehearsed notifier changes on such a stand-in credential until it was retired on 2026-09-29 (#2941).**

If mail is down and you need the break-glass path, the file notifier is one edit away in
`templates/config-secret.yaml.j2`; reading it back needs `sudo k3s kubectl` on **daniel-box**
(the readonly SA cannot `exec`, and k3s does not run on daniel-server).

### `authelia_session*` is a cookie name, not a secret name

The session secret is **`authelia_secret`**. That one SOPS key fills `session.secret` on both
portals — the retired Docker one (the `configuration.yml` template, deleted in #2385:
`git show 2460d0675fd748e70fcbcde87185371ffd62402b:ansible/roles/containers/archive/authelia/templates/`) and this one (the `session:` block of
`ansible/roles/k8s/authelia/templates/config-secret.yaml.j2`). Every `authelia_session*`
string in the tree is a **cookie** name instead: `authelia_session` on the Docker portal,
`authelia_session_k8s` here (`ansible/roles/k8s/authelia/defaults/main.yml:authelia_k8s_cookie_name`).
Searching the rotation registry for `authelia_session*` therefore
finds nothing, which reads as an untracked credential.

The rotation state of `authelia_secret` is settled:

- `authelia_secret` is in `ansible/secret_rotation.yml` at tier `assisted`, and
  `secret_rotation.py audit` reads it `ok`. `sync` adds nothing.
- The tier is right. Rotating this key re-signs session cookies, so every user is logged out
  and no data is lost. It needs none of the `pinned` care `authelia_storage` takes
  (`docs/secret-rotation.md`).

**The registry records `last_rotated: '2025-12-15'` for this key, and that is not drift.**
`audit` advances the date in memory to the day git shows the ciphertext last changed, then
writes nothing back — git is the source of truth, which is why `sync` leaves an existing date
alone. Read the audit line before concluding that a registry date means a key is stale.

### The `crowdsec-agent` sidecar is seeded by init containers

Three init containers seed the sidecar, and the traefik pod runs the same three. `docs/crowdsec-waf-record.md` (*Sidecar agent seeding*) owns the order, the tolerances and the tests. `wait-for-redis` runs after them, as the redis section above describes.

## The `claude-ui` user

The users database holds two users. The operator, and `claude-ui` — the identity the headless
UI tier logs in as so it can reach the `two_factor` services (code-server, n8n, longhorn)
without a code typed off a phone. `authelia_k8s_claude_user` in `defaults/main.yml` carries the
reasoning; `scripts/diagnostics/ui_login.py` is the consumer.

Its TOTP registration is **not** in the rendered `users_database.yml`. Authelia keeps
registrations in its SQLite database whatever the authentication backend is, so the deploy
seeds it with `authelia storage user totp generate --force` against the running pod, from
`authelia_claude_totp_secret`. The secret is fixed, so re-seeding writes an identical row.

**Rotating either password needs `-e authelia_k8s_rehash_passwords=true`.** The role reads each
user's existing argon2 digest back out of the live Secret and reuses it — that is what stops a
fresh random salt rewriting the Secret and rolling the pod on every deploy, and it is also why
`sops set authelia_password` followed by an ordinary deploy changes nothing. The recap is green
and the portal goes on accepting the old password:

```
./scripts/deploy.sh --tags authelia -e authelia_k8s_rehash_passwords=true
```

Rotating `authelia_claude_totp_secret` needs no flag — the seeding task re-seeds every deploy.

**The hashing parameters are configured twice, and only one of them verifies anything.**
`authentication_backend.file.password` in `templates/config-secret.yaml.j2` builds the hasher
Authelia uses to MINT digests — a portal password reset, nothing else. Verification reads the
algorithm off the stored `$argon2id$` PHC prefix instead (`crypt.Decode` in
`internal/authentication/file_user_provider_database.go`, reached from `CheckUserPassword`), so
changing that block cannot break an existing login. The digests in the Secret are minted by the
`authelia crypto hash generate argon2` task with its own explicit flags, which is the other copy.

**That block renders the canonical nested form, and the legacy flat form is a trap.** It said
`algorithm: argon2id` with `iterations`/`memory`/… as siblings until #1621. `argon2id` is a
recognised alias, but the same code path that aliases it carries the flat parameters across in
legacy units — `config.Argon2.Memory = config.Memory * 1024` — so `memory: 65536` meant 64 GiB
of effective argon2 memory, 1024× the CLI flag that mints the hashes. It passed validation
silently (`MemoryMax` is `math.MaxUint32`) and no login could show it. `variant: argon2id`, not
the short `id`: the parser takes either, the published v4.39 schema's enum takes only the long
spelling, and that schema is what `ansible/tests/services/test_authelia_config_schema.py`
validates the rendered block against.

**Revoking Claude's two_factor reach** is deleting the `claude-ui` block from
`templates/config-secret.yaml.j2` and redeploying. The access_control rules are domain-scoped
and never `subject:`-scoped, so there is no per-user rule to unpick.
