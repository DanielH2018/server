# mosquitto — the MQTT broker for zigbee2mqtt and Home Assistant

Eclipse Mosquitto, the broker every Zigbee device event and HA automation trigger crosses. No
web UI, no route — infra role.

## At a glance
- **Deploy tag:** `--tags "mosquitto"`. Must deploy **before** `zigbee2mqtt`
  (`containers_list` orders it right after, with a comment saying so — z2m resolves the broker
  by bare Service name).
- **LAN address:** a MetalLB LoadBalancer Service pinned to `mqtt_k8s_vip` (`group_vars`, so
  daniel-server's Docker-side clients can render the same value), asserted after apply.
- **Persists:** `mosquitto-data` (`longhorn-nobackup`, 1Gi) — retained messages and QoS session
  state only, which the three known clients republish on reconnect; nothing worth a B2
  transaction.
- **Secrets:** `mqtt_username`/`mqtt_password_hash` (SOPS keys), rendered into a password file
  alongside the broker config in one Secret.
- **`k8s_autodeploy: false`** for two independent reasons: it's a dependency edge the deployer
  models no intra-tick ordering for (zigbee2mqtt and Home Assistant both need it up), and it's
  `Recreate` + its own PVC, so a bad image swap can migrate broker state before the gate
  observes a fault.

## Editing
- Broker/password config: `templates/config-secret.yaml.j2`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "mosquitto"`.
