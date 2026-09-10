# autoheal — Restarts unhealthy containers

Watches Docker healthchecks and restarts any container reporting `unhealthy`.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `willfarrell/autoheal:latest`
- **Hosts:** daniel-pi ONLY · **No web UI**, no Authelia
- **Networks:** lifecycle only (reaches the write-capable `docker-proxy-lifecycle`,
  not the broad networks)
- **Depends on:** docker-proxy
- **Config in:** `ansible/inventory/host_vars/daniel-pi.yml` → `containers_list`

> **The daniel-server instance is gone.** Docker was uninstalled there on 2026-08-14 as the k3s
> migration's end state, and `host_vars/daniel-server.yml` now sets `containers_list: []`. This
> role is still written host-agnostically, but daniel-pi is the only host that runs it. There is
> no k8s counterpart — pod restarts are Kubernetes' own job.

## Notable
- Runs with `AUTOHEAL_CONTAINER_LABEL=all`, so it monitors **every** container that defines
  a `healthcheck` (no `autoheal=true` label required) and restarts any that report
  `unhealthy`. Corollary: a service is only self-healing if its healthcheck actually fails
  when it's broken — e.g. qBittorrent's check probes external egress, not just loopback, so
  an orphaned VPN netns is caught (see the `qbittorrent` role).
- **Every restart posts to Discord, and the payload key is the load-bearing half.** The image
  POSTs `{"<WEBHOOK_JSON_KEY>": "<message>"}` and defaults that key to `text`; Discord reads
  `content` and rejects `text` with a 400, so `WEBHOOK_JSON_KEY=content` is what makes the
  notification arrive. Nothing else can see that failure — autoheal discards the response, the
  container stays healthy, and the deploy reads green. ENFORCED by
  `ansible/tests/services/test_autoheal_webhook_payload_key.py`. The channel is the shared
  `monitor_discord_webhook_url`.
- **`AUTOHEAL_START_PERIOD` is a startup sleep, not a per-container grace period.** Docker
  already suppresses `unhealthy` during a container's own `healthcheck.start_period`. This
  covers the Pi rebooting, when autoheal and everything it watches start together. It also
  blinds autoheal for that long after any deploy that recreates it, which is why it is 120s.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "autoheal"`
