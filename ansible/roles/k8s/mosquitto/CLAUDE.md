# mosquitto — the MQTT broker for zigbee2mqtt and Home Assistant

Eclipse Mosquitto, the broker every Zigbee device event and HA automation trigger crosses. No
web UI, no route — infra role.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=mosquitto` with exit 5
- **Image:** `eclipse-mosquitto` (`mosquitto_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claim:** `mosquitto-data` (no backup (StorageClass longhorn-nobackup))
<!-- /generated_from -->

- **Flux applies it, not Ansible** (D5.20, a Wave D PR with sonarr and radarr). Its
  `containers_list` entry carries `owner: flux`, so `deploy.sh`, the play and the GitOps
  deployer all skip it, and a merged change reaches the cluster when `promote` publishes and
  verifies the next bundle. To give it back to Ansible, follow the two-PR handback in
  `docs/flux-deploy-plane.md`; a plain revert of the flag leaves two writers on the objects.
  zigbee2mqtt is Flux-owned too and depends on it. The Deployment is `Recreate`, so a change
  that rolls it drops every MQTT client briefly. `mosquitto-data` is on `longhorn-nobackup`,
  and the role declares no snapshot claim, so the tag renders no `-snapshot` Job.
- **Where the tasks went.** `tasks/main.yml` holds one task, the include of `k8s/manifests`,
  and no Flux path runs it. It stays for the handback. Its two asserts each have a successor.
  The colon refusal moved into `cluster_vars.py write` (below). The LAN-address assert became
  observability's `MosquittoLanAddressNotPinned` rule, which the soak reads. The
  Kustomization's `wait` replaces the rollout wait.
- **Must deploy before `zigbee2mqtt`** — z2m resolves the broker by bare Service name. The edge is
  `depends_on: [mosquitto]` on zigbee2mqtt's `containers_list` entry, pinned by
  `ansible/tests/deploy/test_k8s_toposort.py::test_documented_pairwise_ordering_survives_an_adversarial_list`.
- **LAN address:** a MetalLB LoadBalancer Service pinned to `mqtt_k8s_vip` (`group_vars`, so
  daniel-server's Docker-side clients can render the same value). observability's
  `MosquittoLanAddressNotPinned` rule watches the address from kube-state-metrics, at
  `warning` with `verify="true"`, so the soak checks it on every revision. MetalLB ignores an
  annotation key it does not recognise, so a wrong prefix leaves the Service on an
  auto-assigned address with every pod healthy; the rule is what catches that. Its address is a literal, so moving the VIP is an edit to that rule too.
- **Persists:** `mosquitto-data` (`longhorn-nobackup`, 1Gi) — retained messages and QoS session
  state only, which the three known clients republish on reconnect; nothing worth a B2
  transaction.
- **Secrets:** `mqtt_username`/`mqtt_password_hash` (SOPS keys), rendered into a password file
  alongside the broker config in one Secret. **The stored hash carries no `user:` prefix.** The
  template renders `mqtt_username:mqtt_password_hash` with no transform on the hash, so a stored
  `mosquitto_passwd` line would render `user:user:$7$...` and fail every client's auth.
  `scripts/secrets_mgmt/cluster_vars.py:successor_refusals` refuses a hash that holds any colon, so `write` stops before the
  value reaches the cluster file, and names the key without the value.
- **Why `Recreate` + its own PVC matters for the denylist:** a bad image swap can migrate
  broker state before the gate observes a fault.

## Editing
- Broker/password config: `templates/config-secret.yaml.j2`.
