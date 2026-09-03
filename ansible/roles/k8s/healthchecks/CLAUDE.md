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
  (the seed superuser).

## Notable
- Outbound email is broken on purpose right now: `healthchecks_smtp_password` was retired
  2026-08-30 after the credential turned up in transcript plaintext, but Django still reads
  `EMAIL_HOST`/`PORT`/`USE_TLS` from the Deployment, so it attempts SMTP auth and fails
  rather than skipping send — the same broken state as before the retirement, not a new one.
- **A revert-past-creation coupling for any future auto-deploy promotion:** check UUIDs here
  are baked into ping URLs in unrelated fleet crons. A Longhorn revert past a check's
  creation leaves those crons pinging a dead UUID, silently.

## Editing
- Manifests: `templates/deployment.yaml.j2`, `templates/ingressroute.yaml.j2`,
  `templates/secret.yaml.j2`, `templates/service.yaml.j2`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "healthchecks"`.
