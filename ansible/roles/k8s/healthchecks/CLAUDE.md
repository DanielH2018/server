# healthchecks — dead-man's-switch monitoring for host crons

healthchecks.io (self-hosted), pinged by fleet crons so a cron that stops running gets
noticed instead of silently going quiet. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/healthchecks` (`healthchecks_k8s_image`), digest-pinned;
  tag kept alongside the digest for Renovate's k8s-defaults manager.
- **Deploy tag:** `--tags "healthchecks"`. Route: `healthchecks.<domain>` (Authelia), port
  8000.
- **Storage:** `healthchecks-config` PVC (`longhorn`, 1Gi) — check definitions and ping
  history (532K, 2 files at migration time).
- **Auto-deploy: denylisted**, for two independent reasons: it's the observability role
  fleet crons ping (a broken deploy stops noticing a stopped cron, with nothing else
  watching that), and it independently matches the migrating-state shape — Recreate + a real
  RWO PVC seeded through `k8s/volume-claim`.
- **Secrets** (SOPS keys, not values): `smtp_notify_app_password` (outbound mail, shared with
  Uptime Kuma and monitor-bridge), `healthchecks_password` (the seed superuser),
  `healthchecks_discord_webhook_url` (the notification channel). `healthchecks_smtp_user` is
  no longer read here — #1453 tracks whether it can go.

## Notable
- **Alerts leave over Discord, and the channel is declared rather than clicked.** A
  Healthchecks integration is a `Channel` row in hc.sqlite on the PVC, so a hand-made one
  disappears with a Longhorn restore and nothing says so. The row here — kind `webhook`,
  name `Discord` — was made by hand on 2024-09-22 and has always delivered;
  `files/seed_discord_channel.py` ADOPTS it rather than adding one, and rewrites it to the
  declared spec on every deploy. `tasks/main.yml` waits for the rollout, then pipes the
  script into `manage.py shell` in the pod. The webhook URL reaches the script through the
  pod's environment (`HOMELAB_DISCORD_WEBHOOK_URL` in `templates/secret.yaml.j2`), never on
  a command line, so no transcript can capture it. The script assigns the channel to every
  check in every project, additively — a check assigned by hand keeps its assignment.
- **`CHANNEL_KIND` and `CHANNEL_NAME` are the match key, and changing either adopts
  nothing.** A second channel at the same webhook is two Discord messages per flip. Both
  halves of the spec are written for the same reason: `Channel.webhook_spec` reads
  `method_`/`url_`/`body_`/`headers_` for the status it is asked about, and `sendalerts`
  asks for `up` on the recovery flip.
- **Outbound email works again, on the homelab's shared Gmail app password.**
  `healthchecks_smtp_password` was retired 2026-08-30 after the credential turned up in
  transcript plaintext, and outbound mail was dark from then until 2026-09-09 — Django kept
  reading `EMAIL_HOST`/`PORT`/`USE_TLS` from the Deployment and failed auth on every send.
  The Secret now fills `EMAIL_HOST_PASSWORD` from `smtp_notify_app_password`, the same key
  Uptime Kuma's email notification and monitor-bridge's `email_backstop` authenticate with.
  `EMAIL_HOST_USER` moved from `healthchecks_smtp_user` to `email` at the same time, because
  Gmail authenticates the account its app password was minted for and the Deployment already
  sends as `DEFAULT_FROM_EMAIL: {{ email }}`. `ansible/tests/services/test_smtp_wiring.py`
  holds those two to each other. Discord remains the channel alerts actually leave over;
  this is the reset/report path Django uses.
- **`/config/local_settings.py` on the PVC beat everything this role renders, for three
  years.** `hc/settings.py` ends with `if (BASE_DIR / "hc/local_settings.py").exists(): from
  .local_settings import *`, and that import runs AFTER the environment is read. The image
  wrote this instance's copy on first run in **January 2023**, pinning `EMAIL_HOST`,
  `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` and
  `DEFAULT_FROM_EMAIL`. Every email setting the Deployment and the Secret carried was
  therefore decorative — Django read the file instead, and nothing anywhere said so.
  Three fixes on 2026-09-09 each deployed green and changed nothing Django saw: restoring
  `EMAIL_HOST_PASSWORD` to the Secret, repointing `EMAIL_HOST_USER` at `email`, and moving
  the transport to 465. **A `535 Username and Password not accepted` here means the Secret is
  not being read, not that the credential is bad** — that was the first symptom, and the
  second was `EMAIL_USE_TLS/EMAIL_USE_SSL are mutually exclusive`, raised when the
  Deployment's new `EMAIL_USE_SSL: True` met the file's `EMAIL_USE_TLS = True`.
  `files/strip_local_settings_email.py` now deletes those assignments on every deploy, and
  `tasks/main.yml` restarts the pod when it removes one. **It deletes rather than comments**,
  because the file held a plaintext Gmail password on the PVC.
- **The strip leaves `SECRET_KEY` alone, deliberately.** It signs sessions and password-reset
  tokens, it lives in that 2023 file and nowhere else — not in SOPS, not in the rotation
  registry — so rewriting `local_settings.py` wholesale would mint a new one and log everyone
  out. Removing six lines is the smallest edit that makes this role's settings authoritative.
  `tests/test_strip_local_settings_email.py` asserts the survivors byte-for-byte, which is
  the half a truncating script would otherwise pass.
- **Transport is implicit TLS on 465** — `EMAIL_PORT: 465`, `EMAIL_USE_SSL: True`,
  `EMAIL_USE_TLS: False` — matching Authelia, Uptime Kuma and monitor-bridge's
  `email_backstop`. **The `False` is load-bearing**: Django raises when both switches are
  true, and healthchecks reads each through `envbool`, which accepts only `""`, `True` and
  `False`. 465 was chosen while the 587 failure was still misattributed to Gmail; it is kept
  because one transport across four consumers is worth more than a revert, not because 587
  was ever shown to be at fault.
- **A revert-past-creation coupling for any future auto-deploy promotion:** check UUIDs here
  are baked into ping URLs in unrelated fleet crons. A Longhorn revert past a check's
  creation leaves those crons pinging a dead UUID, silently.

## Editing
- Manifests: `templates/deployment.yaml.j2`, `templates/ingressroute.yaml.j2`,
  `templates/secret.yaml.j2`, `templates/service.yaml.j2`.
- Settings the PVC used to own: `files/strip_local_settings_email.py`, tested by
  `tests/test_strip_local_settings_email.py`.
- Notification channel: `files/seed_discord_channel.py`, tested by
  `tests/test_seed_discord_channel.py` (`uv run pytest ansible/roles/k8s/healthchecks/tests`).
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "healthchecks"`.
