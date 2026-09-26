# mosquitto — the MQTT broker for zigbee2mqtt and Home Assistant

Eclipse Mosquitto, the broker every Zigbee device event and HA automation trigger crosses. No
web UI, no route — infra role.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "mosquitto"`
- **Image:** `eclipse-mosquitto` (`mosquitto_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `mosquitto-data` (no backup (StorageClass longhorn-nobackup))
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — dependency edges — MQTT broker for
  zigbee2mqtt/home-assistant; no intra-tick ordering. ALSO Recreate + its own PVC — two
  independent reasons
<!-- /generated_from -->

- **Must deploy before `zigbee2mqtt`** — z2m resolves the broker by bare Service name. The edge is
  `depends_on: [mosquitto]` on zigbee2mqtt's `containers_list` entry, pinned by
  `ansible/tests/deploy/test_k8s_toposort.py::test_documented_pairwise_ordering_survives_an_adversarial_list`.
- **LAN address:** a MetalLB LoadBalancer Service pinned to `mqtt_k8s_vip` (`group_vars`, so
  daniel-server's Docker-side clients can render the same value), asserted after apply.
- **Persists:** `mosquitto-data` (`longhorn-nobackup`, 1Gi) — retained messages and QoS session
  state only, which the three known clients republish on reconnect; nothing worth a B2
  transaction.
- **Secrets:** `mqtt_username`/`mqtt_password_hash` (SOPS keys), rendered into a password file
  alongside the broker config in one Secret.
- **Why `Recreate` + its own PVC matters for the denylist:** a bad image swap can migrate
  broker state before the gate observes a fault.

## Editing
- Broker/password config: `templates/config-secret.yaml.j2`.
- Deploy: `./scripts/deploy.sh --tags "mosquitto"`.
