# Secret rotation — audit, tiers, and runbooks

Secrets in `ansible/vars/secrets.yml` are tracked for rotation by a plaintext registry
(`ansible/secret_rotation.yml`) and the tool `scripts/secrets_mgmt/secret_rotation.py`. A daily server
timer (`secret-rotation-audit.sh`, initial_setup) pushes the **"Secret Rotation"** Uptime
Kuma monitor — it goes **down** when any secret is past its per-tier window, or when a
secret exists in `secrets.yml` but not the registry.

Due dates are **staggered** by a deterministic per-name offset, so the registered secrets
come due a few at a time across the year — never all on one day. The offset lives in the
due-date calculation rather than in `last_rotated`, which is what makes it survive a
rotation: `rotate --commit` stamps the same date on every secret in a batch, and a batch
that shared its stamp would otherwise share its next due date too. The offset only ever
pulls a due date **earlier** than the tier cadence below, never later. The
[Secrets reference](reference/secrets.md) lists them, with each one's tier and due date.

## Daily use

```bash
uv run python scripts/secrets_mgmt/secret_rotation.py sync     # after adding/removing a secret — registers it
uv run python scripts/secrets_mgmt/secret_rotation.py audit    # what's due / overdue, by tier
uv run python scripts/secrets_mgmt/secret_rotation.py rotate            # dry-run: due auto-tier secrets
uv run python scripts/secrets_mgmt/secret_rotation.py rotate --commit   # actually rotate due auto secrets
```

`sync` edits the (git-tracked) registry — **commit it**. The audit cron never writes the
registry, so git stays the source of truth.

## Renaming a secret (not a rotation, but the audit cannot tell)

SOPS binds each value's ciphertext to its key path, so renaming a key re-encrypts it even
though the plaintext never changes. `ciphertext_rotation_dates` compares stored ciphertext,
so a plain rename reads as a fresh rotation and resets that secret's clock — which is how a
genuinely overdue credential goes green without anyone touching it. Rename in four steps:

1. Read what git says the secret's date is now:
   `uv run python scripts/secrets_mgmt/secret_rotation.py audit | grep '<old name>'`.
2. Rename in the store: `EDITOR="sed -i s/^<old>/<new>/" sops ansible/vars/secrets.yml`.
3. Add `"<new name>": "<old name>"` to `RENAMED_FROM` in
   `scripts/secrets_mgmt/git_dates.py`.
4. Carry the date over:
   `uv run python scripts/secrets_mgmt/secret_rotation.py record --key <new name> --last-rotated <date from step 1>`.
   `record` moves the old name's registry row to the new name, tier and all, and sets its
   `last_rotated`. A `sync` run before it does no harm: `record` still takes the old row over
   the seeded one, and its fictional backdate goes with it.

Prove it with `secret_rotation.py audit --check` (registry drift) and `audit` (the date
held). `scripts/secrets_mgmt/tests/test_departed_secrets_are_accounted_for.py` is the
gate behind step 3: every key the store's git history holds and the store no longer does
must be a `RENAMED_FROM` source or sit in `RETIRED` beside it, so a rename that skips the
table fails CI rather than resetting the clock. A key dropped outright goes in `RETIRED`.

Rename every consumer of the variable in the same commit. Every value is unchanged, so a
deploy cannot detect a half-done rename — the template render is the only signal, and it
fails on the undefined variable.

## The cluster file — the values Flux substitutes

Flux cannot read `secrets.yml`. It reads its own copies, which `cluster_vars.py write`
derives from `secrets.yml`:

- `ansible/vars/cluster-vars.sops.env` holds every key that the cluster render references
  as `${key}`. The `cluster-vars` Kustomization decrypts it into the Secret
  `homelab-cluster-vars`, and each substituting Kustomization reads that Secret.
- `ansible/vars/cluster-manifests/` holds one SOPS-encrypted Secret manifest per key set
  that Flux must not substitute.
- `ansible/vars/cluster-vars.source` holds the SHA-256 of each copied key's ciphertext in
  `secrets.yml`. It is evidence that the copies are fresh, not a store.

Only `scripts/secrets_mgmt/cluster_vars.py write` writes these files. It decrypts, so the
operator runs it (**operator-only**). The weekly rotation cron also runs it in process for
each key it rotates. A hand edit breaks the file's SOPS MAC. CI's `cluster_vars.py check`
refuses a hand-edited file, and a `.source` hash that no longer matches `secrets.yml`. A
value changed with `sops set` therefore reaches the cluster only after `write` runs.

`docs/target-architecture/deploy/d3-secrets.md`, *What one secret costs*, lists every store a
secret can touch.

### Rotating a value read by both planes

A value can have an Ansible-owned consumer and a consumer Flux applies, as `peanut_password`
does for homepage and peanut. Every step that handles the value is **operator-only**.

1. Set the new value with `sops set` (**operator-only**).
2. Run `uv run python scripts/secrets_mgmt/cluster_vars.py write` (**operator-only**). Commit
   `secrets.yml`, the cluster files it rewrote and the registry together, then open the PR.
3. Run `uv run python scripts/secrets_mgmt/secret_rotation.py consumers <key>`. It prints a
   `deploy.sh` line for the Ansible-owned consumers. For each `owner: flux` consumer it
   prints the Flux path instead, because `deploy.sh` exits 5 on such a tag and deploys none
   of the others.
4. Land the PR with `land.sh`. A PR that changes either cluster file waits on the
   `cluster-vars` Kustomization and on every Kustomization that depends on it, then ends
   `needs-manual-apply` naming the Ansible half.
5. Run the `deploy.sh` line from step 3.

The weekly cron needs no `write` step: `rotate --commit` runs it before it deploys. For an
`owner: flux` consumer, `rotate` prints that Flux applies the value once the commit lands.

### Rotating the cluster key

The cluster keypair decrypts only the cluster files. It is the one key in this setup that
`secrets.yml` does not hold. Each step is **operator-only**, and the order keeps a working key
in the Secret `flux-system/sops-age` throughout.

1. Generate a replacement keypair with `age-keygen`, and store its private half in the
   password manager. Add it to `sops-age` as a second entry, `age-next.agekey`.
   Flux's `kustomize-controller` imports every entry in that Secret, so either key decrypts.
2. On a branch, replace the cluster recipient in both cluster creation rules in
   `ansible/.sops.yaml`. Run `sops updatekeys` and `sops rotate` on the cluster file and on
   each cluster manifest, from `ansible/` or with `--config ansible/.sops.yaml`. The
   ciphertext in `secrets.yml` does not move, so no `.source` hash moves and no pod
   restarts. `check` reads the recipients from the rules, so it passes.
3. Land the PR with `land.sh`, which waits on `cluster-vars` for a PR that changes the
   cluster file. Confirm that `flux/cluster-vars` reads `success` on the promoted revision:
   `gh api repos/DanielH2018/server/commits/<sha>/statuses --jq '.[] | select(.context ==
   "flux/cluster-vars") | .state'`.
4. Remove the old entry from `sops-age`, and the old private half from the password manager.

**A suspected compromise needs more than this procedure.** Rotating the key protects future
revisions only. Every past revision in git and every published `sha-<sha>` artifact holds a
copy of the cluster file and of each cluster manifest, all encrypted to the old key. Anyone
holding that key can still read every value in those copies. After a suspected compromise,
also rotate every key that `ansible/vars/cluster-vars.source` lists (**operator-only**). That
list holds the cluster file's keys and the keys the cluster manifests hold, which carry the
manifest's name in a third column: the Authelia OIDC signing key, the Google service-account
key, the pi backup SSH key and the Mullvad account. For each key, set the new value with
`sops set`, run `cluster_vars.py write`, and redeploy the key's consumers as
`secret_rotation.py consumers <key>` prints them. The cluster key never decrypts
`secrets.yml`, so the keys that only Ansible reads stay safe.

The registry in `ansible/secret_rotation.yml` does not track the cluster key. The key lives
outside `secrets.yml`, so `registry_drift` would report its row as stale. A lost cluster key
needs no recovery: this procedure replaces it from any host that decrypts `secrets.yml`.
`docs/break-glass.md` lists the key in its recovery kit.

## Per-host SOPS files — the values a host decrypts itself

A host that runs from a package does not hold a key for `secrets.yml` (ADR-0022). It decrypts
its own file, `ansible/vars/hosts/<host>.sops.yaml`, with its own age key. The file holds a
copy of each key that the host's rendered tree names. `secrets.yml` stays the only source of
a value.

**The copy rule: a commit that changes a key's value in `secrets.yml` also changes that key in
every host file that carries it.** `scripts/secrets_mgmt/host_secrets.py check` enforces it in
the pytest job, without a key. The check fails when the newest commit that changed a key in a
host file does not equal or descend from the newest commit that changed it in `secrets.yml`. It
also fails when the file's keys differ from the keys the host's tree names, when a value is not
ciphertext, and in a shallow clone. Two commits on one day still order, because the check
compares commits and not dates.

To bring a host file in step, run `sync` (**operator-only**, because it decrypts
`secrets.yml`):

```bash
cd /home/ubuntu/server && uv run python scripts/secrets_mgmt/host_secrets.py sync <host>
```

`sync` rewrites the whole file with the keys the host's tree names. Commit the file in the same
PR as the change that needs it: a PR that adds a key to a host's tree, or one that rotates a
key a host file carries. No cron calls `sync`.

`uv run python scripts/secrets_mgmt/host_secrets.py check` lists what is out of step. A key
that a host file carries is also skipped by the unattended `rotate --commit`, which prints
`skip (manual: a host file carries it)`. Rotate such a key by hand, then run `sync`.

## Tiers

<!-- Cadences and counts are generated from TIER_DAYS and the registry; edit those. -->
--8<-- "assets/generated/fragments/secret-tiers.md"

- `auto` — locally generated push tokens with no external coupling. `rotate --commit`, then
  redeploy the consumer.
- `assisted` — app passwords, API keys and OIDC secrets. An app-side step, below.
- `external` — provider-managed (Cloudflare, Discord, Mullvad, SMTP, LLM). Mint in the
  provider console.
- `external-90d` — provider-managed like `external`, minted with a 90-day expiry, such as
  the promote workflow's `PROMOTE_TOKEN`. The audit flags the copy before the token expires.
- `pinned` — **must not be naively swapped**. The special procedure, below.
- `ignore` — not a secret: usernames, the R2 bucket name, static addresses. `domain` left
  the store on 2026-10-15 and is an inventory variable in `ansible/inventory/group_vars/all.yml`.

`sync` gives a newly registered key its tier from two name rules in
`scripts/secrets_mgmt/secrets_lib/secret_classify.py`: `*_push_token` is `auto`, `*_user` and
`*_username` are `ignore`, and every other key is `assisted`. An `external` or `pinned` key gets its tier
by an edit to its `tier` in the registry after that `sync`, and `sync` preserves the edit.

## `auto` — automated

`rotate --commit` generates a new 32-char token, writes it via `sops set`, and records the
date. Then every reader must render again. `secret_rotation.py consumers <key>` prints the
`deploy.sh` line for an Ansible-owned reader. A Flux-owned reader, such as monitor-bridge,
takes the value once the commit lands, and `deploy.sh` exits 5 on its tag.
Uptime Kuma honours the new push token on the next push — no Kuma
UI step. Because only **coming-due** secrets rotate (due within `ROTATE_LEAD_DAYS` = 8 —
one weekly cron interval, so a token rotates the Sunday *before* it goes overdue and the
daily audit never pages DOWN on a rotation the cron was about to do), runs stay staggered.

### Auto-rotate contract (the weekly cron changes secrets unattended)
The `rotate --commit` weekly cron is autonomous and state-changing — its authority, bounded
(harness-engineering's versioned-contract pattern for a change-producing role):
- **Scope / exclusions:** only `auto`-tier, locally generated push tokens with **no external
  coupling**, and only those **coming due** within `ROTATE_LEAD_DAYS` (8). `assisted` / `external` /
  `pinned` are **never** touched by the cron — a `pinned` swap loses data (see below).
- **Mode:** change-producing — writes via `sops set`, commits the registry, and the operator
  redeploys the consumer. Not report-only; there is no dry-run cron (the bare `rotate` is the dry run,
  run by hand).
- **Required evidence:** the daily "Secret Rotation" Kuma monitor must return green after a rotation.
  An `auto`-tier secret still overdue after a cron window means the **cron broke** — investigate it,
  don't hand-rotate around it.
- **Host-file keys:** the cron skips every key a per-host SOPS file carries, because the copy
  needs an operator-only `sync` in the same commit (*Per-host SOPS files*, above).
- **Abort/escalation:** the cron only ever *narrows* to coming-due `auto` secrets; anything it can't
  classify stays untouched and surfaces in `audit`, never a blind rotate.

## `assisted` — app-issued (regenerate in the app, then update SOPS)

General shape: rotate/regenerate the credential **in the app**, `sops set
ansible/vars/secrets.yml '["<name>"]' '"<new>"'`, update the registry date (`sync` won't,
since the value already existed — `secret_rotation.py record --key <name> --last-rotated
<date>` sets it), then
run `host_secrets.py sync <host>` (**operator-only**) for each host file that carries the key
(*Per-host SOPS files*, above), then
redeploy the app **and** every consumer (for example, Homepage, monitor-bridge, configarr). A
date left behind is not fatal: `audit` and `rotate` both advance it to the commit that changed
the ciphertext before deciding what is due, so an undated hand rotation of an `auto` token is
not rotated again by the weekly cron. Examples:
- `*_api_key` (sonarr/radarr/jellyfin/prowlarr): Settings → General → regenerate API key.
  **Bazarr is a consumer of `sonarr_api_key` and `radarr_api_key` that no deploy reaches.** It
  stores both in its own config on the `bazarr-config` PVC, entered through its UI, so there is
  no Ansible template holding them and no redeploy that updates them. After rotating either key,
  set it in Bazarr → Settings → Sonarr → API Key and Settings → Radarr → API Key, then restart
  Bazarr from System → Restart. The restart is required, not cosmetic: Bazarr's SignalR client
  raises `UnAuthorizedHubError` and its thread exits, so it does not retry on a key change alone.
  Missing this on 2026-08-29 left Bazarr reconnecting in a loop that leaked 173 MiB → its 1Gi cap
  in 90 minutes and OOM-killed it; the only visible signal was the "k3s Container OOM" monitor,
  which self-clears one hour after the kill while subtitle fetching stays silently broken.
- `mqtt_password_hash`: `mosquitto_passwd` writes a `username:hash` line. Store only the part
  after the first `:`, because the template prepends `mqtt_username` itself. The mosquitto
  deploy refuses a stored hash that holds a colon, before the apply.
- **CrowdSec bouncer keys** (`crowdsec_k8s_bouncer_api_key`, the cluster edge's — the only
  bouncer since E7 retired the Docker edge and its `dockertraefik` key) and the agent password
  (`crowdsec_k8s_agent_password`, shared by all four agents and the `crowdsec-verify` machine).
  Since slice-6 B2 the single LAPI lives in the cluster. Bouncer registration is DECLARATIVE —
  the engine reads `BOUNCER_KEY_*` from its env rather than running `cscli`. Agent
  registration is not: the LAPI pod's `lapi-register` initContainer runs `cscli machines add`
  on every pod start, and adds a machine only when it is absent (#5741). It passes no
  `--force` and has no update path, so a rotation that skips the deletes leaves every machine on
  the old password. A deleted machine comes back only when the engine pod next starts.
  Flux owns the `crowdsec`, `traefik` and `authelia` roles, so `deploy.sh` exits 5 on each tag.
  Rotation is therefore: `sops set` the new value → `cluster_vars.py write` and open the PR →
  delete the old registrations on the engine → land the PR with `land.sh`, without `--tags`.
  Flux applies all three roles, and the `checksum/restart` annotation the renderer stamps
  rolls each pod that reads the key, the sidecars included (`docs/flux-deploy-plane.md`, *Restart
  triggers*). The delete is required because re-registration never UPDATES an existing key.
  For the bouncer key that is one command:
  `kubectl -n homelab exec deploy/crowdsec -c crowdsec -- cscli bouncers delete --ignore-missing k8straefik`.
  The hourly bouncer-prune cron usually deletes the base `k8straefik` row first, and then this
  command deletes nothing. The old key's `k8straefik@<pod IP>` rows cannot be deleted by name,
  so they stay valid until the prune removes them, within two hours of the Traefik roll.
  The engine restart registers the new key either way, because the base name is missing. The
  bouncer-prune section of `ansible/roles/k8s/crowdsec/CLAUDE.md` has the mechanism.
  For the agent password it is one `cscli machines delete <name>` per machine, and there are
  five: `k8s-traefik-agent`, `k8s-authelia-agent`, `k8s-node-agent-daniel-box`,
  `k8s-node-agent-daniel-server` and `crowdsec-verify`. The two node agents come from
  `crowdsec_k8s_node_agent_machines` in the role's `defaults/main.yml`; a node joining the
  cluster adds a name there. The landing restarts the node-agent DaemonSet, because the role
  names `crowdsec-node-agent` in `manifests_extra_rollouts`, which makes it a restart target.
  That restart is what rotates them: `AGENT_PASSWORD` arrives through a `secretKeyRef`, and
  env resolves once at pod start, so only a new pod reads the new value. Expect the node agents
  to fail their LAPI login from the delete until the new engine pod's `lapi-register` runs.
  The delete comes before the landing, so that window spans CI, the merge and Flux's apply.
  The node agents restart while the engine pod is still rolling, so the machines are deleted and not yet re-added until the new
  engine pod's `lapi-register` runs. The changed Secret is what rolls the engine pod. The
  agents converge without help: each agent's liveness probe converges it, restarting the container until registration
  lands. **Do NOT skip the bouncer delete.** Without it the LAPI keeps the old key's hash, and
  the rolled Traefik pod presents a key the LAPI rejects. The edge then refuses every
  request with a 403 from the first one. The bouncer sends its LAPI key to AppSec too,
  because `ansible/roles/k8s/traefik/templates/dynamic.yaml.j2` sets no `crowdsecAppsecKey`.
  AppSec checks that key against the LAPI and answers 401, and the bouncer turns any AppSec
  answer other than 200 or 500 into a 403. The plugin's `bouncer.go` at
  `traefik_k8s_bouncer_plugin_version` has this, and so does CrowdSec's AppSec
  `config.go` at the `crowdsec_k8s_image` tag. Traefik's startupProbe is what catches it.
  Its target router carries the `crowdsec` middleware, so the probe reads 403. Kubelet then
  restarts the container, and the pod never turns Ready. Flux's `traefik` Kustomization
  waits on that pod and goes red. That guard is an outage, not a prevention: the roll is
  `Recreate`, so no old pod serves meanwhile. The delete step and the reads below prevent it.
  The `crowdsec-verify` Job does not prevent it either. It waits on the `traefik` Kustomization
  as well as on `crowdsec` (`flux_verify_after` in the `crowdsec` role's defaults), so it
  probes the rolled Traefik pod. It therefore fails, or never starts, after the outage
  has begun.
  Verify after: `kubectl -n homelab exec deploy/crowdsec -c crowdsec -- cscli bouncers list`
  (fresh `last_pull`) and a `kubectl -n homelab logs deploy/traefik` free of LAPI 403s.
  (`crowdsec_bouncer_api_key` — the retired local LAPI's legacy key — and
  `crowdsec_bouncer_docker_traefik_key` were both RETIRED 2026-08-13 with the Docker edge.)

<!-- Generated from periodSeconds on the node-agent DaemonSet's livenessProbe; edit that. -->
--8<-- "assets/generated/fragments/node-agent-liveness.md"

- `grafana_admin_password`, `*_password`: change in the app (or its env on first run).
- `google_assistant_service_account`: create a new JSON key for the service account in the
  Google Cloud console, then store the whole downloaded JSON with `sops set`. The value is
  multi-line, so it has a Secret of its own, `home-assistant-google-sa`, rendered from
  `ansible/roles/k8s/home-assistant/templates/google-sa-secret.yaml.j2`. A rotation changes
  that one small Secret. Flux applies it once `cluster_vars.py write` has updated the cluster file. Delete the old key in the
  console only after HA has restarted on the new one. `google_assistant_project_id` changes
  only with a new Google project.
- `pi_peer_backup_ssh_key`: generate a new ed25519 keypair, store the private half with
  `sops set`, and commit the public half over
  `ansible/roles/setup/k8s_host_files/files/pi-peer-backup.pub`. The private key is multi-line,
  so it has a Secret of its own, `pi-peer-backup-ssh-key`, rendered from
  `ansible/roles/k8s/pi-peer-backup/templates/ssh-key-secret.yaml.j2`. Flux owns pi-peer-backup,
  so that Secret reaches the cluster as the SOPS manifest `cluster_vars.py write` regenerates
  (operator-run), which `promote` applies once the change merges. The public half is in the
  setup plane: the GitOps tick applies `initial_setup.yml --tags k8s_host_files`, whose
  `authorized_key` task authorizes it on the Pi. That task adds the new line and leaves the
  old one, so remove the old key from the Pi's `authorized_keys` after the next nightly run
  succeeds.
- `authelia_oidc_rsa_key_content`: generate a new RSA private key in PEM form, such as with
  `openssl genrsa 4096`, and store it with `sops set`. The value is multi-line, so it has a
  Secret of its own, `authelia-oidc-jwk`, rendered from
  `ansible/roles/k8s/authelia/templates/oidc-jwk-secret.yaml.j2`. A rotation changes that one
  small Secret. Flux owns authelia, so run `cluster_vars.py write` and land the PR as in step
  4 of *Authelia user passwords* below, not with `--tags authelia`. Authelia reads the key from
  the mounted file through its `template` config filter, so the landing must roll the pod. The
  `checksum/restart` annotation the renderer stamps does, because it hashes the key's
  `.source` digest. The key's `kid` changes with it: each relying party re-fetches the JWKS, and tokens signed with the old key stop
  verifying, so users sign in again. Rotate in an SSO window.
- `authelia_secret` / `authelia_jwt`: rotating forces all users to re-login (not breaking).
- `authelia_oidc_hmac_secret` / `*_password_hash`: re-issues OIDC — re-pair jellyfin (the
  live OIDC client; beszel's client is provisioned but parked in `archive/`, re-pair only
  if reactivated).

### Authelia user passwords — mint the digest, then store both

Each Authelia user has two SOPS keys: the plaintext password and its argon2 digest.
`authelia_password` pairs with `authelia_password_argon2`, and `authelia_claude_password` and
`authelia_agent_password` pair with their `_argon2` keys the same way.
`ansible/roles/k8s/authelia/templates/config-secret.yaml.j2` reads only the digests. A changed
plaintext alone therefore changes no login. `scripts/secrets_mgmt/cluster_vars.py write`
refuses a malformed or duplicated `_argon2` digest and a `:` in `mqtt_password_hash`, and names
the key only. It cannot check that a digest matches its plaintext. This procedure does.

Each step handles a plaintext password, so the whole procedure is **operator-only**. Run it in a
worktree on a branch, never in the primary checkout. Below, `KEY` stands for the plaintext's
SOPS name.

1. Set the new plaintext with `sops set`.
2. Mint and validate the digest in a one-shot pod, because daniel-box has no Docker. The
   plaintext travels over stdin, so it never reaches the pod spec. The flags match the hashing
   block in `config-secret.yaml.j2`, and
   `ansible/tests/services/test_authelia_argon2_parameters_agree.py` holds the two equal. The
   image is the one `authelia_k8s_image` names in the role's `defaults/main.yml`.

   ```bash
   sops -d --extract "[\"$KEY\"]" ansible/vars/secrets.yml \
     | sudo k3s kubectl -n homelab run authelia-mint --rm -i --attach --quiet \
         --restart=Never --image="<authelia_k8s_image>" --command -- sh -c '
       IFS= read -r pw
       d=$(authelia crypto hash generate argon2 --password "$pw" --iterations 3 \
             --memory 65536 --parallelism 4 --salt-size 16 --key-size 32 \
           | grep -o "\$argon2id\$[^[:space:]]*")
       authelia crypto hash validate --password "$pw" -- "$d" \
         | grep -qx "The password matches the digest." && printf "%s\n" "$d"' \
     | grep -x '\$argon2id\$v=19\$m=65536,t=3,p=4\$[A-Za-z0-9+/]*\$[A-Za-z0-9+/]*' \
     | jq -R . \
     | sops set --value-stdin ansible/vars/secrets.yml "[\"${KEY}_argon2\"]"
   ```

   The pipeline prints nothing on success. A failed validation emits no digest, so `sops set`
   receives nothing and changes nothing.
3. Run `uv run python scripts/secrets_mgmt/cluster_vars.py write`. The three `_argon2` keys
   are in the cluster file, so a changed digest leaves its `.source` hash stale, and CI's
   `cluster_vars.py check` refuses the PR until `write` runs. `write` also refuses a malformed
   or duplicated digest, naming the key only. Commit both SOPS values and the cluster files
   `write` rewrote in one commit, then open the PR.
4. Run `land.sh` without `--tags`. Flux owns authelia, and `deploy.sh` exits 5 on a
   flux-owned tag. The PR changes the cluster file, so `land.sh` waits on the `cluster-vars`
   Kustomization and on every Kustomization that depends on it, as step 4 of *Rotating a
   value read by both planes* describes. The renderer stamps a `checksum/restart` annotation
   on the authelia pod, which hashes the `.source` line of each key its Secret references.
   The portal therefore rolls in the revision that carries the new value. D3's exit
   criterion 3 showed the same path for peanut
   (`docs/target-architecture/deploy/d3-secrets.md`).
5. A `claude-agent` rotation also needs the `claude_code` apply, which writes the new
   plaintext into the agent user's home. Run it only after authelia has converged on the new
   digest: step 4's landing reads `settled`. If the authelia deploy failed, the old digest is
   still live, and the old plaintext in the agent's home still matches it. `secret_rotation.py
   consumers authelia_agent_password` prints the apply command. The agent's login fails from
   the first of steps 4 and 5 until both have run.
6. Log in as the rotated user.

**To roll back a rotation,** revert the rotation commit, which restores both encrypted values
and the cluster files together. The revert is a secrets-only PR too, so land it as in step 4,
and repeat step 5 for a `claude-agent` rotation.

**`authelia_claude_totp_secret` follows the same path without the minting step.** Set it with
`sops set`, run `cluster_vars.py write`, and land as in step 4. The changed `authelia-config`
Secret rolls the portal, and the `seed-claude-totp` init container re-seeds claude-ui's TOTP row
on the new pod's start. Then run `uv run pytest -m ui -k two_factor`; a code derived from the
new secret has to pass. The seed fails open, so a portal that is up does not prove the row
changed.

### Record keys — `source: record` in the registry

<!-- The key list is generated from `source: record` in the registry; edit that. -->
--8<-- "assets/generated/fragments/record-keys.md"

For each of these keys SOPS holds a **copy** of a credential the app owns. Nothing in the tree writes
the value to the app, so `sops set` plus a deploy rotates nothing: the recap says `changed`, the
pod rolls, and the old credential still works. The registry marks them `source: record`, and
`secret_rotation.py rotate` refuses them in both of its paths (`--name`, and the unattended
batch) with a reason naming this section. `sync` never touches an existing entry, so the field
survives it. Each has a different mechanism:

- `authelia_password` — the role renders the digest `authelia_password_argon2`, never this
  plaintext, so a changed `authelia_password` is inert on its own. A rotation mints the new
  digest and stores both, as *Authelia user passwords* above describes.
- `bazarr_api_key` — the only reference in the tree is a reader,
  `roles/k8s/monitor-bridge/templates/env-secret.yaml.j2`. Regenerate it in Bazarr's
  Settings → General, `sops set` the new value, then redeploy monitor-bridge; setting SOPS
  alone breaks the checker with an HTTP 401 that reads as a monitoring fault.
- `promote_token` — the GitHub `PROMOTE_TOKEN` repository secret holds the credential: a
  fine-grained token on DanielH2018/server and DanielH2018/dotfiles with Variables read and write, minted on the
  operator's account with a 90-day expiry. Nothing in the tree reads the SOPS copy; it exists
  so the audit tracks the expiry on the `external-90d` tier. The operator re-mints the token
  and re-sets both the repository secret and the SOPS copy together.

Verify against the app, not the deploy: a login, an API call, a monitor past its grace window.
`audit` cannot tell the cases apart — it derives `last_rotated` from the git history of
`secrets.yml`, so committing the copy advances the date either way. Before calling any
rotation done, grep the key across `ansible/` and ask whether a hit *writes* it to the app or
only reads it; a single reader means SOPS is a record. Mark that key `source: record` in the
registry and add its mechanism bullet above.

## `external` — provider consoles (audit-only)

Mint a new value in the provider, then `sops set` + redeploy the consumer:
- `cloudflare_dns_token`: Cloudflare dashboard → API Tokens (keep it **zone-scoped**: DNS
  edit + Zone read for the one zone — audit this scope when rotating).
- `*_discord_webhook*`: Discord → channel → Integrations → Webhooks → regenerate.
    - `gitops_deploy_discord_webhook` has a second copy, the `DEPLOY_DISCORD_WEBHOOK`
      repository secret, which `promote.yml`'s `post` job reads for the stateful hold's post.
      A rotation re-sets that secret too, with `gh secret set DEPLOY_DISCORD_WEBHOOK -R
      DanielH2018/server` reading the new value from stdin (**operator-only**). A stale copy
      fails the next stateful hold's post red, with the hold already written.
- `mullvad_account`: Mullvad account panel. The value is all digits, so it has a Secret of its
  own, `qbittorrent-mullvad-account`, rendered from
  `ansible/roles/k8s/qbittorrent/templates/mullvad-account-secret.yaml.j2`. A rotation changes
  that one small Secret. Flux owns qbittorrent, so it takes the new Secret once
  `cluster_vars.py write` has updated the cluster file and the change lands.
- `wireguard_peer_*`: Mullvad account panel / regenerate the WG key.
- `homelab_builder_github_token`: GitHub → Settings → Developer settings → Fine-grained
  tokens, owner DanielH2018, *Public repositories (read-only)*, no permissions. The
  host-package builder reads CI check runs with it, because its system user has no gh login
  and an anonymous read shares one quota per source IP with the deployer's CI gate. A leaked
  copy reads only public data. Redeploy with `initial_setup.yml --tags homelab_builder`,
  which the setup-plane tick runs.
- `monitor_bridge_deploy_pipeline_github_token`: GitHub → Settings → Developer settings →
  Fine-grained tokens, named `homelab-deploy-pipeline-exporter`, owner DanielH2018, only the
  `server` repository, with *Actions* read and *Variables* read and no other permission.
  GitHub caps the display name at 40 characters, so it differs from the SOPS key. The
  deploy-pipeline exporter in monitor-bridge (D2.6d in
  `docs/target-architecture/deploy/d2-first-leaf.md`) reads `DEPLOY_HOLD`, `PROD_DIGEST`
  and master's CI runs with it. It holds no write scope, so a leaked copy can neither clear a
  hold nor move a tag. Flux owns monitor-bridge, so run `cluster_vars.py write` and land the
  change without `--tags`.

## `pinned` — DANGER, never `sops set` blindly

These encrypt/anchor existing data; swapping the value alone **loses data**.

### The safe-cutover discipline for any pinned rotation
A pinned secret anchors existing data, so treat rotation as a **staged cutover with a live fallback**,
never a swap (harness-engineering's consequential-operation state machine — verify the new path
before you destroy the old one):
1. **Back up the anchored data first** (snapshot `authelia/config/db.sqlite3` — a Longhorn
   snapshot of the authelia PVC, or `kubectl cp` the file out). This backup is the recovery
   path — keep it until step 5 passes.
2. **Re-key through the tool, not `sops set`** (`authelia storage encryption change-key`) so the data is re-anchored to the
   new value.
3. **Prove the new value opens the data BEFORE overwriting the old one.** **Whether you still
   hold a live fallback here depends on the tool, so check before you rely on one.** Some
   re-key tools leave the original artifact intact, and the old value still opens it.
   Authelia's `change-key` does not: it re-encrypts the SQLite database **in place**, so the
   instant it succeeds the old key opens nothing and the step-1 backup is the only way back.
   Assume in-place unless the tool documents otherwise.
4. **Only then** `sops set` the new value and redeploy the consumer.
5. **Verify live** (audit resets green, Kuma monitor green, a real restore/login works) — and only
   after that delete the pre-rotation backup. If any step fails, restore the step-1 backup;
   see step 3 for why the old value itself may no longer be a fallback.

The failure this prevents: `sops set` first, redeploy, discover the data is now undecryptable — and
the only value that could open it has already been overwritten. The procedure below is a concrete
instance of this discipline, and the history entry shows the retirement variant:

- **HISTORY — `kopia_password`** was removed on 2026-08-13 (8edb11cd) and the kopia B2 repo it
  anchored was deleted after the first verified Longhorn-only nightly. The residual hidden
  object versions were hard-purged on 2026-08-14, so the value opens nothing
  ([ADR-0014](adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md)). It is the worked
  example of a pinned secret leaving the registry: the anchored data is destroyed first, and
  only then does the key go. The B2 account keys are a separate thing and still live: they are
  Longhorn's backup-target credential, renamed `kopia_b2_*` to `longhorn_b2_*` on 2026-09-09.
- **`authelia_storage`** — the Authelia DB encryption key. It encrypts the TOTP secrets and
  WebAuthn credentials in `/config/db.sqlite3` on the authelia Longhorn PVC. A raw swap makes
  that database undecryptable, and code-server, n8n and longhorn are `two_factor` — so a
  botched rotation locks those three behind a challenge nobody can satisfy until every
  enrolment is redone. Snapshot the PVC in the Longhorn UI first.

  The container reads the current key from its mounted config, so only the **new** value ever
  reaches a command line. Run these on daniel-box, and **not from a Claude Code session** — a
  bash-input is transcribed, and a transcribed key is the exposure this file exists to prevent.

  The conditions above are gates, and they run as one script before the first command below:
  the registry row is still `pinned`, a hand-taken Longhorn snapshot of `authelia-config` is
  younger than two hours, Authelia has an available replica, and this shell is a plain one on
  daniel-box. The exit code names the first gate that refused (#2216, the shape
  `docs/k3s-upgrade.md` set); exit 4 from a Claude Code session is the expected reading, and
  the three state gates still print their verdicts before it:

  ```bash
  uv run python scripts/deploy_tools/runbook_gates.py pinned-rotation
  ```

  ```bash
  NEW=$(openssl rand -hex 32)
  sudo k3s kubectl -n homelab exec deploy/authelia -c authelia -- \
    authelia storage encryption change-key -c /secrets/configuration.yml --new-encryption-key "$NEW"

  # must SUCCEED — the new key opens the data
  sudo k3s kubectl -n homelab exec deploy/authelia -c authelia -- \
    authelia storage encryption check -c /secrets/configuration.yml --encryption-key "$NEW"

  # must FAIL — the config still carries the old key, which should now open nothing
  sudo k3s kubectl -n homelab exec deploy/authelia -c authelia -- \
    authelia storage encryption check -c /secrets/configuration.yml
  ```

  Take both halves of that pair. A `check` observed only succeeding does not show that
  `change-key` did anything; the second command failing is the evidence that it did.

  Between `change-key` and the redeploy, the database is on the new key while the running pod's
  config still carries the old one, so TOTP verification is broken. Keep the gap short. The
  redeploy is also a real SSO outage for everything behind the Authelia middleware, because the
  Deployment is `replicas: 1` with `strategy: Recreate`.

  Verify with a **TOTP challenge, not a login** — a password login succeeds without the storage
  key ever being read.

After any rotation, run `audit` to confirm the secret's window resets (green), and watch the
"Secret Rotation" Kuma monitor.
