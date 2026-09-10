# authelia — SSO / forward-auth for the cluster

Authelia guards most public routes as a Traefik forward-auth middleware. See repo-root
`CLAUDE.md` for shared conventions.

**Deploy tag:** `--tags "authelia"`. Denylisted from GitOps auto-deploy (platform — SSO/OIDC
gate; a failed deploy locks out access to everything behind it, including the tools to fix it).

## OIDC clients

Two relying parties, both in `templates/config-secret.yaml.j2` behind
`authelia_k8s_manage_oidc`:

- `jellyfin` — `two_factor`, `client_secret_post` (the SSO plugin's PAR asymmetry, see the
  comment at the block).
- `grafana` — `one_factor`, `client_secret_basic`, `consent_mode: implicit`,
  `claims_policy: with_groups` (Authelia keeps `groups` out of the ID token by default, and
  Grafana reads its role mapping from the ID token before it looks at userinfo). The policy
  matches the `*.local.<domain>` access_control rule the Grafana route already falls under;
  `two_factor` here would stall the headless `-m ui` browser on a TOTP prompt no test can
  answer. Grafana's half of the wiring, and why `root_url` pins the callback to the LAN name,
  is in `roles/k8s/claude-otel/CLAUDE.md`.

Each client's secret is stored hashed. The digest is minted once with Authelia's own CLI —
`authelia crypto hash generate pbkdf2 --variant sha512` — and the plaintext goes to the app,
so a client that needs both (Grafana does; Jellyfin's plaintext lives in its plugin config)
takes two SOPS keys that rotate together.

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

**A credential is bound to the origin it was registered at.** An rp id must be a registrable
suffix of the origin, and `auth.local.<domain>` and `auth.<domain>` share none that covers
both — so a key enrolled on the LAN portal does not work on the public one. A user who wants
both enrols twice. This is a WebAuthn spec constraint, not a setting to fix.

**Do not add a `webauthn` key from the docs site.** Authelia refuses to start on a key it does
not recognise, this pod rolls under `Recreate` in front of most public routes, and
`validate/k8s_manifests.py` only asks whether the YAML parses. The pinned version's own
`config.template.yml` is the source —
`https://raw.githubusercontent.com/authelia/authelia/v<tag>/config.template.yml` — and
`ansible/tests/services/test_authelia_webauthn.py` holds the rendered block to the keys someone
checked against that source — deliberately narrower than what 4.39.21 accepts, so adding a key
means editing the frozenset as well, which is where the check belongs.

Enrolment is a browser action at the portal's security settings and needs a physical
authenticator, so it stays an operator task; the headless UI tier is unaffected, because
`ui_login.py` posts to the first-factor API and drives TOTP as `claude-ui` rather than touching
the portal's login page.

## Sessions live in redis

`session.redis` in `templates/config-secret.yaml.j2` points at the `authelia-redis` Deployment
this role also deploys. Without that block Authelia's provider is **in-memory** — 4.39.21's own
`config.template.yml` says "Memory is the provider unless redis is defined" — so every roll of
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
/ Redis Sentinel", so until the redis block existed it encrypted nothing.

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
`nc -z` the sibling gates in crowdsec and n8n use: that precedent rests on their own image
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

**daniel-stage rehearses this branch on a credential that authenticates nothing.** It rendered
the filesystem notifier until #1464, which left the SMTP branch with no boot check anywhere.
The startup check being off is what makes the rehearsal possible: Authelia opens no SMTP
connection at boot, so a literal stand-in in `host_vars/daniel-stage.yml` proves the half that
bites — the config parses and the pod comes up on it. Staging sends nothing, and its `email` is
a generated fake. `ansible/tests/staging/test_staging_rehearses_the_smtp_notifier.py` holds
both facts.

If mail is down and you need the break-glass path, the file notifier is one edit away in
`templates/config-secret.yaml.j2`; reading it back needs `sudo k3s kubectl` on **daniel-box**
(the readonly SA cannot `exec`, and k3s does not run on daniel-server).

### `authelia_session*` is a cookie name, not a secret name

The session secret is **`authelia_secret`**. That one SOPS key fills `session.secret` on both
portals — the retired Docker one (the `configuration.yml` template under
`ansible/roles/containers/archive/authelia/templates/`, at its line 92) and this one
(`templates/config-secret.yaml.j2:110`). Every `authelia_session*` string in the tree is a
**cookie** name instead: `authelia_session` on the Docker portal, `authelia_session_k8s` here
(`defaults/main.yml:50`). Searching the rotation registry for `authelia_session*` therefore
finds nothing, which reads as an untracked credential.

That is what the open item standing here until 2026-09-05 had found. It said the Docker
portal's session keys were never rotated after this portal took over `auth.<domain>`, and that
the key the note meant was recorded nowhere. Both halves are settled:

- `authelia_secret` is in `ansible/secret_rotation.yml` at tier `assisted`, and
  `secret_rotation.py audit` reads it `ok`. `sync` adds nothing.
- The tier is right. Rotating this key re-signs session cookies, so every user is logged out
  and no data is lost. It needs none of the `pinned` care `authelia_storage` takes
  (`docs/secret-rotation.md`).
- Its ciphertext last changed on 2026-08-30, two weeks after this portal took over
  `auth.<domain>`.

**The registry records `last_rotated: '2025-12-15'` for this key, and that is not drift.**
`audit` advances the date in memory to the day git shows the ciphertext last changed, then
writes nothing back — git is the source of truth, which is why `sync` leaves an existing date
alone. Read the audit line before concluding that a registry date means a key is stale.

### The crowdsec-agent sidecar seeds /etc/crowdsec from an init container, not its entrypoint

The CrowdSec image entrypoint opens with a "Populating configuration directory" step — an
`rsync -a --ignore-existing /staging/etc/crowdsec/* /etc/crowdsec` — that runs under `set -e`
and only while `/etc/crowdsec/config.yaml` is absent. About twenty staged files are root-only
(the LAPI and online credentials, the bundled hub tree), so the non-root sidecar exits 23 on
it and the kubelet restarts the container. The restart finds `config.yaml` present, skips the
block, and the pod settles at 2/2 Running with one restart on the clock.

That one restart failed every authelia deploy's health gate. `probe.py health` fails closed on
any container restart inside its 180s window, so `land.sh` read `VERDICT: unhealthy` while
nothing was actually wrong (#1173). Traefik hit the same thing first (#976).

`crowdsec-config-install` therefore runs that rsync itself, before its own `install` steps, so
the seeds win over the staged copies of the same names and the sidecar's entrypoint finds
`config.yaml` already there. **Exit 23 is the only status tolerated.** Authelia rolls under
`Recreate`, so a failed init container means the old pod is already gone and SSO is down — but
a blanket `|| true` would trade that for an agent started on a half-populated config with
nothing saying so. `ansible/tests/services/test_crowdsec_config_install_seeds_staged_tree.py`
holds both pods to this.

`crowdsec-data-install` is the second init container, and it fixes a different half of the
same image's staging behaviour. The image ships its datafiles at
`/staging/var/lib/crowdsec/data` mode 0600 root:root and the entrypoint SYMLINKS them into the
data volume rather than copying, so the non-root agent cannot read through the link. GeoIP then
never initialises — `unable to open GeoLite2-City.mmdb: permission denied` — and the
`geoip-enrich` parser is dead behind a pod that reads 2/2 Running (#1177; traefik hit the same
thing first as #990). Copying the files in world-readable defeats the symlink, because the
entrypoint's `[ ! -e ]` guard skips a name that already exists. It runs as root with
`DAC_READ_SEARCH` — the read-only half of root's permission-bit override, which is what reaches
the 0600 sources — and ends in `exit 0`, so an unreadable file leaves that one name on the
symlink path instead of taking SSO down under `Recreate`.
`ansible/tests/services/test_crowdsec_optional.py` holds both pods to this.

`crowdsec-hub-install` runs **first**, ahead of `crowdsec-config-install`, and it fixes the
level above the datafiles. The rsync skips the image's root-only staged **hub tree** — that is part of
the exit 23 it tolerates — while copying the parser *configs*, which are symlinks into that
tree. `/etc/crowdsec/parsers/s02-enrich/geoip-enrich.yaml` therefore resolved to a hub file
that was never staged, and the agent dropped the parser once per parser-load pass:
`Ignoring file … lstat /etc/crowdsec/hub/parsers/s02-enrich/crowdsecurity/geoip-enrich.yaml:
no such file or directory`. GeoIP enrichment stayed dead behind a 2/2 Running pod even with
the datafiles installed and the enrichers registered (#1211; traefik logged the identical
warning, so this was never an authelia gap). It copies the tree as root with `DAC_READ_SEARCH`
and `CHOWN`, hands it to uid 1000 — the agent's entrypoint installs parsers into it on every
start, and a root-owned copy fails that with `permission denied` and exits the sidecar —
then `chmod -R a+rX,u+w`, and ends in `exit 0` for the same Recreate reason as
`crowdsec-data-install`.

**Running it after the rsync would no-op silently.** rsync recurses from the parent listing,
so it creates `/etc/crowdsec/hub` owned by uid 1000 before it fails to read into it, and root
with `ALL` dropped cannot write into another uid's directory — `DAC_READ_SEARCH` is the read
half of the override, `DAC_OVERRIDE` the write half. The copy would fail, `exit 0` would
swallow it, and the pod would come up 2/2 with the warning intact. Going first, it creates the
directory root-owned and world-readable while the emptyDir is still empty, and the rsync's own
`--ignore-existing` then leaves those files alone.
`ansible/tests/services/test_crowdsec_hub_install_stages_the_hub_tree.py` holds both pods to
this.

**The `DECIDED:` marker on the rsync tolerance was amended, not reversed.** Its reasoning
against `|| true` stands; what #1211 disproved is the half claiming the skipped files are all
re-downloaded by `cscli hub update` on start. They are not, or not before the parser load.

Verify a deploy of this role by the sidecar's restart count, not just by pod readiness:

```
kubectl get pod -n homelab -l app=authelia -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}'
```

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
