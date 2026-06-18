# mosquitto — MQTT broker (Zigbee2MQTT ↔ Home Assistant)

Eclipse Mosquitto 2.x. Internal-only broker for the Zigbee2MQTT stack. See repo-root
`CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `eclipse-mosquitto:2`
- **Host:** daniel-server · **Networks:** `mqtt` only · **Web/Authelia:** none (no Traefik route)
- **Reached by:** zigbee2mqtt + home-assistant on the `mqtt` isolation net, at `mosquitto:1883`
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Authenticated, not anonymous.** `allow_anonymous false` + `password_file`. Creds come
  from SOPS: `mqtt_username` / `mqtt_password` (clients) and `mqtt_password_hash` (the hash).
  Regenerate the hash with
  `docker run --rm eclipse-mosquitto:2 sh -c 'mosquitto_passwd -b -c /tmp/pw x PASS; cat /tmp/pw'`
  — the `passwordfile` template prepends `{{ mqtt_username }}:` and strips any `user:` prefix
  from the stored hash, so the username in the hash command is irrelevant (mosquitto's PBKDF2
  hash is salt+password only, not username). This is the single-source-of-truth fix for the
  2026-06-17 `not authorised` bug (hash baked `homelab:` while `mqtt_username` was `ubuntu`).
- **Port 1883 is NOT host-published** — only reachable on the `mqtt` net. No external MQTT clients.
- **Runs as `1000:1000`** (`user:`) so the bind-mounted `./config`/`./data` are writable
  (Mosquitto's default uid is 1883, which can't write deploy-user-owned dirs).
- **Healthcheck** subscribes to `$$SYS/broker/uptime` with the broker creds — the `$$`
  escaping is required (Compose interpolates a lone `$SYS`).
- **Persistence** (`./data`) is regenerable retained-message state; bind-mounted so Kopia
  backs it up, but losing it is harmless.
- **Ad-hoc publish/subscribe (admin/debug)** — from the host, `docker exec mosquitto mosquitto_pub
  -h localhost -u <user> -P <pass> -t <topic> -m '<payload>'` (and `mosquitto_sub`). The creds are
  in SOPS (`mqtt_username`/`mqtt_password`); the rendered values also sit in the Z2M container's
  `data/configuration.yaml`. Used for Z2M bridge requests (`zigbee2mqtt/bridge/request/device/rename`)
  and device settings (`zigbee2mqtt/<name>/set`) — e.g. renames and the FP300 presence tuning.

## Editing
- Compose: `templates/docker-compose.yml.j2` · cfg: `templates/mosquitto.conf.j2`, `templates/passwordfile.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "mosquitto"`
