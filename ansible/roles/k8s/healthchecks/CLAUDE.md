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
- **Secrets** (SOPS keys, not values): `healthchecks_smtp_user`, `healthchecks_password`
  (the seed superuser), `healthchecks_discord_webhook_url` (the notification channel).

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
- Outbound email is broken on purpose: `healthchecks_smtp_password` was retired
  2026-08-30 after the credential turned up in transcript plaintext, but Django still reads
  `EMAIL_HOST`/`PORT`/`USE_TLS` from the Deployment, so it attempts SMTP auth and fails
  rather than skipping send — the same broken state as before the retirement, not a new one.
- **A revert-past-creation coupling for any future auto-deploy promotion:** check UUIDs here
  are baked into ping URLs in unrelated fleet crons. A Longhorn revert past a check's
  creation leaves those crons pinging a dead UUID, silently.

## Editing
- Manifests: `templates/deployment.yaml.j2`, `templates/ingressroute.yaml.j2`,
  `templates/secret.yaml.j2`, `templates/service.yaml.j2`.
- Notification channel: `files/seed_discord_channel.py`, tested by
  `tests/test_seed_discord_channel.py` (`uv run pytest ansible/roles/k8s/healthchecks/tests`).
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "healthchecks"`.
