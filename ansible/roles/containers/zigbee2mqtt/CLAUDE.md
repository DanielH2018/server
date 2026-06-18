# zigbee2mqtt — Zigbee coordinator bridge (SLZB-06M → MQTT)

Zigbee2MQTT 2.x bridging the network-attached SLZB-06M coordinator into MQTT/Home Assistant.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/koenkk/zigbee2mqtt:2.12.0` (pinned → Renovate-managed, not Watchtower)
- **Host:** daniel-server · **Port:** 8080 · **URL:** `zigbee2mqtt.<domain>` (Authelia: yes)
- **Networks:** `apps` (Traefik) + `mqtt` (broker). Reaches the coordinator at
  `tcp://{{ slzb_ip }}:6638` over the LAN via Docker's outbound NAT — no host networking.
- **Depends on:** traefik, mosquitto
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Network coordinator, not USB.** The SLZB-06M is reached as `serial.port: tcp://<ip>:6638`,
  `adapter: ember` (Silabs EFR32 EmberZNet). No `network_mode: host`, no `devices:`.
- **`configuration.yaml` is templated** (`data/configuration.yaml`) and is the Ansible source
  of truth — overwritten on deploy. The **Zigbee network identity is pinned**
  (`network_key` from SOPS `zigbee_network_key`, `pan_id`/`ext_pan_id` from host_vars) so a
  redeploy can NEVER regenerate it and un-pair every device. Do not switch these to GENERATE.
- **Device/pairing state is Z2M-owned, NOT templated:** `data/database.db`,
  `coordinator_backup.json`, `devices.yaml`, `groups.yaml`. All under the `./data` bind mount
  → Kopia-backed. Losing `./data` = re-pair everything.
- **HA discovery on** (`homeassistant.enabled: true`) — paired devices auto-appear in HA via
  the MQTT integration; no per-device HA config.
- **Pairing is closed by default** (no `permit_join` in 2.x). Enable join from the Z2M UI
  (`zigbee2mqtt.<domain>`) when adding devices, then disable.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Z2M cfg: `templates/configuration.yaml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "zigbee2mqtt"`
