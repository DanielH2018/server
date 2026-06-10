# uptime-kuma — Service uptime monitoring

Uptime Kuma with an AutoKuma sidecar that auto-creates monitors from container labels.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Images:** `louislam/uptime-kuma:2` + `ghcr.io/bigboot/autokuma:latest`
- **Host:** daniel-server · **Port:** 3001 · **URL:** `uptime-kuma.<domain>` (Authelia: yes)
- **Networks:** monitoring
- **Depends on:** traefik, authelia, **docker-proxy**
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **AutoKuma** reads the `kuma(...)` labels every service's compose emits (via
  `templates/autokuma.yml.j2`) and provisions monitors automatically — through the
  read-only `docker-proxy` socket. That's why almost every role imports the kuma macro.
- **Managed Discord notification:** this compose carries `kuma.discord.notification.*` labels
  that make AutoKuma create/own a Discord notification (id `discord` == `kuma_notification_id`
  in group_vars). The `kuma()` macro links every monitor to it via `notification_name_list`,
  so the whole fleet alerts to Discord without per-monitor UI config. Webhook is
  `monitor_discord_webhook_url` in `secrets.yml`. New monitors inherit it on (re)deploy; the
  existing 55 backfill on a tagless `deploy.yml`.
- **KNOWN QUIRK (AutoKuma 2.0.0):** label-defined notifications are flagged *experimental*,
  and AutoKuma logs `Updating notification: discord` every ~5s forever — it stores a redundant
  nested `config` so each sync sees a diff and re-syncs. Functionally harmless (the
  notification works, monitor links are stable); it's churn/log-noise only. Don't chase it as
  a deploy bug.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "uptime-kuma"`
