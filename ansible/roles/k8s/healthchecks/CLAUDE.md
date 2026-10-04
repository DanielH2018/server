# healthchecks — dead-man's-switch monitoring for host crons

healthchecks.io (self-hosted), pinged by fleet crons so a cron that stops running gets
noticed instead of silently going quiet.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "healthchecks"`
- **Image:** `lscr.io/linuxserver/healthchecks` (`healthchecks_k8s_image`)
- **Route:** `healthchecks.<domain>` · `healthchecks.local.<domain>`, Authelia one_factor
- **Claim:** `healthchecks-config` (weekly -> B2 (default target))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — observability — cron
  dead-man's-switch monitor. ALSO Recreate + RWO volume-claim PVC (migrating-state shape) — two
  independent reasons. COUPLING NOTE for a future promotion: check UUIDs here are baked into
  ping URLs in unrelated crons fleet-wide; a revert past a check's creation leaves those crons
  pinging a dead UUID, silently dropped
<!-- /generated_from -->

- **Digest-pinned image**, with the tag kept alongside the digest for Renovate's k8s-defaults
  manager.
- **Port:** 8000.
- **Storage:** `healthchecks-config` PVC (`longhorn`, 1Gi) — check definitions and ping
  history, created from `k8s_claims` in `defaults/main.yml`.
- **Secrets** (SOPS keys, not values): `smtp_notify_app_password` (outbound mail, shared with
  Uptime Kuma and monitor-bridge), `healthchecks_password` (the seed superuser),
  `healthchecks_discord_webhook_url` (the notification channel), `healthchecks_secret_key`
  (Django's session and password-reset signing key). `healthchecks_smtp_user` was removed on
  2026-09-10 (#1453) when `EMAIL_HOST_USER` moved to `email`, whose value it duplicated.

## Notable
- **Alerts leave over Discord, and the channel is declared rather than clicked.** A
  Healthchecks integration is a `Channel` row in hc.sqlite on the PVC, so a hand-made one
  disappears with a Longhorn restore and nothing says so.
  `files/seed_discord_channel.py` ADOPTS the row here — kind `webhook`, name `Discord` —
  rather than adding one, and rewrites it to the declared spec on every deploy.
  `tasks/main.yml` waits for the rollout, then pipes the script into `manage.py shell` in the
  pod. The webhook URL reaches the script through the pod's environment
  (`HOMELAB_DISCORD_WEBHOOK_URL` in `templates/secret.yaml.j2`), never on a command line, so
  no transcript can capture it. The script assigns the channel to every check in every
  project, additively — a check assigned by hand keeps its assignment.
- **`CHANNEL_KIND` and `CHANNEL_NAME` are the match key, and changing either adopts
  nothing.** A second channel at the same webhook is two Discord messages per flip. Both
  halves of the spec are written for the same reason: `Channel.webhook_spec` reads
  `method_`/`url_`/`body_`/`headers_` for the status it is asked about, and `sendalerts`
  asks for `up` on the recovery flip.
- **Outbound email rides the homelab's shared Gmail app password.** The Secret fills
  `EMAIL_HOST_PASSWORD` from `smtp_notify_app_password`, the same key Uptime Kuma's email
  notification and monitor-bridge's `email_backstop` authenticate with, and `EMAIL_HOST_USER`
  is `email` — Gmail authenticates the account the app password was minted for, and the
  Deployment already sends as `DEFAULT_FROM_EMAIL: {{ email }}`.
  `ansible/tests/services/test_smtp_wiring.py` holds those two to each other. Discord is the
  channel alerts actually leave over; this is the reset/report path Django uses.
- **`/config/local_settings.py` on the PVC beats everything this role renders.**
  `hc/settings.py` ends with `if (BASE_DIR / "hc/local_settings.py").exists(): from
  .local_settings import *`, and that import runs AFTER the environment is read. The image
  wrote this instance's copy on first run in January 2023, pinning every email setting, so
  what the Deployment and the Secret carried was decorative and nothing said so.
  **A `535 Username and Password not accepted` here means the Secret is not being read, not
  that the credential is bad**, and `EMAIL_USE_TLS/EMAIL_USE_SSL are mutually exclusive` means
  the file's `EMAIL_USE_TLS = True` met the Deployment's `EMAIL_USE_SSL: True`.
  `files/strip_local_settings_owned.py` deletes the assignments it owns on every deploy, and
  `tasks/main.yml` restarts the pod when it removes one. **It deletes rather than comments**,
  because the file held a plaintext Gmail password on the PVC.
- **`SECRET_KEY` came out of that file and into SOPS (#1491, 2026-09-10).** The image
  generated it on first boot in 2023 and it existed nowhere else, so nothing this repo ran
  could rotate it and a lost PVC lost the key with it. The Secret renders a fresh
  `healthchecks_secret_key`, which cost one round of logged-out sessions and dead
  password-reset links. `SECRET_KEY` is in the strip's `OWNED` tuple, and
  `tests/test_strip_local_settings_owned.py` asserts the survivors byte-for-byte — the half a
  truncating script would otherwise pass.
- **The env var is what stops the image minting a replacement key.**
  `init-healthchecks-config/run` appends a random `SECRET_KEY` to `local_settings.py` only
  when `${SECRET_KEY}` is EMPTY *and* the file has no `^SECRET_KEY`. With the Secret rendering
  it the first test fails, so the strip is a genuine one-shot. Removing the Secret's
  `SECRET_KEY` line while the name stays in `OWNED` is the failure to avoid: the image would
  rewrite the assignment on every boot and the strip would delete it again, reporting
  `changed` and rolling the pod on every deploy, forever.
- **Transport is implicit TLS on 465** — `EMAIL_PORT: 465`, `EMAIL_USE_SSL: True`,
  `EMAIL_USE_TLS: False` — matching Authelia, Uptime Kuma and monitor-bridge's
  `email_backstop`. **The `False` is load-bearing**: Django raises when both switches are
  true, and healthchecks reads each through `envbool`, which accepts only `""`, `True` and
  `False`. 465 is kept because one transport across four consumers beats a revert, not
  because 587 was ever shown to be at fault.

## Editing
- Manifests: `templates/deployment.yaml.j2`, `templates/ingressroute.yaml.j2`,
  `templates/secret.yaml.j2`. The Service comes from the shared
  `ansible/templates/service-default.yaml.j2`, which this role ships no template for.
- Settings the PVC used to own: `files/strip_local_settings_owned.py`, tested by
  `tests/test_strip_local_settings_owned.py`.
- Notification channel: `files/seed_discord_channel.py`, tested by
  `tests/test_seed_discord_channel.py` (`uv run pytest ansible/roles/k8s/healthchecks/tests`).
