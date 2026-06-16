# SLZB-06M → Zigbee2MQTT + Mosquitto → Home Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a Mosquitto MQTT broker and a Zigbee2MQTT instance (two new Ansible container roles) that bridge the network-attached SLZB-06M coordinator (`tcp://10.0.0.127:6638`) into Home Assistant via MQTT discovery.

**Architecture:** Two new roles on daniel-server. `mosquitto` is an internal-only broker on a new `mqtt` isolation network (no Traefik, authenticated). `zigbee2mqtt` is on `apps` (Traefik + Authelia UI at `zigbee.<domain>`) + `mqtt`, dials the coordinator over serial-over-TCP, and publishes with HA discovery on. HA joins the `mqtt` net and consumes devices via its MQTT integration. Broker auth and the Zigbee network key are declarative from SOPS; configuration files are Ansible-owned, device/pairing state lives in Kopia-backed bind mounts that Z2M owns.

**Tech Stack:** Ansible, Docker Compose, `eclipse-mosquitto:2`, `ghcr.io/koenkk/zigbee2mqtt`, SOPS/age, Traefik, Authelia, Uptime-Kuma (autokuma), Kopia.

**Spec:** `docs/superpowers/specs/2026-06-16-zigbee2mqtt-slzb06m-design.md`

---

## File structure (what gets created/modified)

**New — `mosquitto` role:**
- `ansible/roles/containers/mosquitto/tasks/main.yml`
- `ansible/roles/containers/mosquitto/meta/deps.yml`
- `ansible/roles/containers/mosquitto/templates/docker-compose.yml.j2`
- `ansible/roles/containers/mosquitto/templates/mosquitto.conf.j2`
- `ansible/roles/containers/mosquitto/templates/passwordfile.j2`
- `ansible/roles/containers/mosquitto/CLAUDE.md`

**New — `zigbee2mqtt` role:**
- `ansible/roles/containers/zigbee2mqtt/tasks/main.yml`
- `ansible/roles/containers/zigbee2mqtt/meta/deps.yml`
- `ansible/roles/containers/zigbee2mqtt/templates/docker-compose.yml.j2`
- `ansible/roles/containers/zigbee2mqtt/templates/configuration.yaml.j2`
- `ansible/roles/containers/zigbee2mqtt/CLAUDE.md`

**Modified:**
- `ansible/roles/setup/docker_install/tasks/main.yml` — add `mqtt` to the network loop
- `ansible/inventory/host_vars/daniel-server.yml` — register both services; add `mqtt` to HA; add `slzb_ip`/`zigbee_pan_id`/`zigbee_ext_pan_id`
- `ansible/vars/secrets.yml` (via `sops`) — `mqtt_username`, `mqtt_password`, `mqtt_password_hash`, `zigbee_network_key`
- `ansible/secret_rotation.yml` (via `secret_rotation.py sync`)

**Operator/manual (documented, not Ansible):** HA UI MQTT integration; Z2M UI device pairing.

---

## Task 0: Secrets — generate and store the four MQTT/Zigbee values

**Files:** `ansible/vars/secrets.yml` (via `sops`), `ansible/secret_rotation.yml` (via script)

- [ ] **Step 1: Choose a broker username/password and generate the Mosquitto password-file line**

The broker needs a *hashed* password line (Mosquitto 2.x will not accept plaintext). Generate it with the same image we deploy:

```bash
# Pick values first (example shown — choose your own password):
MQTT_USER=homelab
MQTT_PASS='<pick-a-strong-password>'
docker run --rm eclipse-mosquitto:2 sh -c \
  "mosquitto_passwd -b -c /tmp/pw \"$MQTT_USER\" \"$MQTT_PASS\" >/dev/null 2>&1; cat /tmp/pw"
```

Expected output: one line like `homelab:$7$101$<salt>$<hash>`. Copy that whole line — it becomes `mqtt_password_hash`.

- [ ] **Step 2: Generate the Zigbee network key**

```bash
python3 -c "import random; print('[' + ', '.join(str(random.randint(0,255)) for _ in range(16)) + ']')"
```

Expected: a 16-element array like `[12, 200, 5, ...]`. This becomes `zigbee_network_key` (stored as a string).

- [ ] **Step 3: Add all four secrets via SOPS**

```bash
sops ansible/vars/secrets.yml
```

Add (use the values from Steps 1–2; quote the hash and key — they contain `$`/`[`):

```yaml
mqtt_username: homelab
mqtt_password: "<the strong password from step 1>"
mqtt_password_hash: "homelab:$7$101$<salt>$<hash>"
zigbee_network_key: "[12, 200, 5, ...]"
```

`mqtt_username`/`mqtt_password` are what Z2M and HA authenticate with; `mqtt_password_hash` is the broker's password file; `zigbee_network_key` pins the Zigbee mesh encryption key so a redeploy never regenerates it.

- [ ] **Step 4: Register rotation tracking**

```bash
uv run python scripts/secret_rotation.py sync
```

Expected: the four new names appear in `ansible/secret_rotation.yml` with a `tier` (default `assisted`) and `last_rotated: null`. `zigbee_network_key` rarely rotates — set its tier to `pinned` by hand if you prefer (re-keying the mesh re-pairs every device):

```bash
sops --version >/dev/null  # ensure sops present; then edit ansible/secret_rotation.yml tier if desired
```

- [ ] **Step 5: Commit (registry only — secrets.yml stays encrypted, committed with the role tasks later)**

```bash
git add ansible/secret_rotation.yml ansible/vars/secrets.yml
git commit -m "secrets: add MQTT broker creds + Zigbee network key for zigbee2mqtt stack"
```

---

## Task 1: Create the `mqtt` Docker network

**Files:**
- Modify: `ansible/roles/setup/docker_install/tasks/main.yml` (the `Create Docker networks` loop, ~line 146)

- [ ] **Step 1: Add `mqtt` to the network loop**

In `ansible/roles/setup/docker_install/tasks/main.yml`, add to the `loop:` under `Create Docker networks`, after the `ups` line:

```yaml
    - ups        # isolation net: NUT <-> Home Assistant only (UPS state, kept off apps)
    - mqtt       # isolation net: Mosquitto <-> Zigbee2MQTT <-> Home Assistant only (MQTT off apps)
```

- [ ] **Step 2: Create the network on the host**

Networks are created here, not by container deploys — this MUST run before Task 2/3 deploy, or the containers fail with "network mqtt not found".

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags docker-networks
```

Expected: a changed task creating the `mqtt` network. Verify:

```bash
docker network ls --filter name=mqtt
```

Expected: a `mqtt` bridge network listed.

- [ ] **Step 3: Update the role's CLAUDE.md note (network list)**

In `ansible/roles/setup/docker_install/CLAUDE.md`, the "Networks" line (item 5) lists the created nets. Append `, mqtt (Mosquitto ↔ Zigbee2MQTT ↔ HA)` to that sentence so the doc stays accurate.

- [ ] **Step 4: Commit**

```bash
git add ansible/roles/setup/docker_install/tasks/main.yml ansible/roles/setup/docker_install/CLAUDE.md
git commit -m "docker_install: add mqtt isolation network for the zigbee2mqtt stack"
```

---

## Task 2: `mosquitto` role — internal MQTT broker

**Files:**
- Create: `ansible/roles/containers/mosquitto/meta/deps.yml`
- Create: `ansible/roles/containers/mosquitto/templates/mosquitto.conf.j2`
- Create: `ansible/roles/containers/mosquitto/templates/passwordfile.j2`
- Create: `ansible/roles/containers/mosquitto/templates/docker-compose.yml.j2`
- Create: `ansible/roles/containers/mosquitto/tasks/main.yml`
- Create: `ansible/roles/containers/mosquitto/CLAUDE.md`
- Modify: `ansible/inventory/host_vars/daniel-server.yml` (register `mosquitto`)

- [ ] **Step 1: `meta/deps.yml` (no upstream role deps — non-web internal broker)**

```yaml
---
# Internal MQTT broker — no Traefik route, no Authelia. Nothing must deploy before it;
# zigbee2mqtt and (re)deploys of home-assistant depend on IT (see their deps.yml).
role_deps: []
```

- [ ] **Step 2: `templates/mosquitto.conf.j2`**

```jinja
# Managed by Ansible (roles/containers/mosquitto). Source of truth — overwritten on deploy.
listener 1883
allow_anonymous false
password_file /mosquitto/config/passwordfile
persistence true
persistence_location /mosquitto/data/
# Log to stdout so `docker logs` / Loki capture broker logs.
log_dest stdout
```

- [ ] **Step 3: `templates/passwordfile.j2`**

The stored secret is already the full `user:hash` line from Task 0.

```jinja
{{ mqtt_password_hash }}
```

- [ ] **Step 4: `templates/docker-compose.yml.j2`**

Non-web service — mirrors the `nut` sidecar pattern (kuma + healthcheck + resources, **no Traefik `labels()`**). Runs as `{{ puid }}:{{ pgid }}` so the bind-mounted `./data`/`./config` (owned by the deploy user) are writable. Note the `$$SYS` escaping in the healthcheck — a lone `$SYS` is interpolated by Compose at parse time (ref: compose-healthcheck-dollar-escaping).

```jinja
{% from 'autokuma.yml.j2' import labels as kuma with context %}
{% from 'networks.yml.j2' import service_networks, external_networks with context %}
{% from 'resources.yml.j2' import resources %}
---

services:
  mosquitto:
    image: eclipse-mosquitto:2
    container_name: mosquitto
    user: "{{ puid }}:{{ pgid }}"
    environment:
      - TZ={{ tz }}
    volumes:
      - ./config:/mosquitto/config
      - ./data:/mosquitto/data
    restart: unless-stopped
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    {{ service_networks() }}
    # $$SYS so Compose passes a literal $SYS to the broker (escaping; validate-hook won't catch it).
    healthcheck:
      test: ["CMD-SHELL", "mosquitto_sub -h localhost -p 1883 -u {{ mqtt_username }} -P {{ mqtt_password }} -t '$$SYS/broker/uptime' -C 1 -W 3 >/dev/null 2>&1 || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s
    labels:
      {{ kuma(container_item.name, name='Mosquitto') }}
    # Resource caps for blast-radius containment (M1); broker is tiny — tune from Grafana.
    {{ resources('0.25', '64M', '0.02', '16M') }}

{{ external_networks() }}
```

- [ ] **Step 5: `tasks/main.yml`**

Templates are `no_log` (creds) and `register`-ed so `common_config_changed` recreates the container on edit.

```yaml
---
- name: Create required directories
  tags: [config]
  ansible.builtin.include_role:
    name: common
    tasks_from: setup_dirs.yml
  vars:
    common_dirs_to_create:
      - "{{ container_item.name }}/config"
      - "{{ container_item.name }}/data"

- name: Deploy Mosquitto config
  tags: [config]
  ansible.builtin.template:
    src: mosquitto.conf.j2
    dest: "/home/{{ sys_user }}/server/containers/{{ container_item.name }}/config/mosquitto.conf"
    mode: "0640"
  register: mosquitto_conf

- name: Deploy Mosquitto password file
  tags: [config]
  ansible.builtin.template:
    src: passwordfile.j2
    dest: "/home/{{ sys_user }}/server/containers/{{ container_item.name }}/config/passwordfile"
    mode: "0640"
  no_log: true
  register: mosquitto_passwordfile

- name: Deploy Container
  tags: [deploy]
  ansible.builtin.include_role:
    name: common
    tasks_from: docker_deploy.yml
  vars:
    common_config_changed: "{{ (mosquitto_conf is changed) or (mosquitto_passwordfile is changed) }}"
```

- [ ] **Step 6: `CLAUDE.md`**

```markdown
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
  from SOPS: `mqtt_username` / `mqtt_password` (clients) and `mqtt_password_hash` (the
  templated `passwordfile`, a `mosquitto_passwd` line). Regenerate the hash with
  `docker run --rm eclipse-mosquitto:2 sh -c 'mosquitto_passwd -b -c /tmp/pw USER PASS; cat /tmp/pw'`.
- **Port 1883 is NOT host-published** — only reachable on the `mqtt` net. No external MQTT clients.
- **Runs as `1000:1000`** (`user:`) so the bind-mounted `./config`/`./data` are writable
  (Mosquitto's default uid is 1883, which can't write deploy-user-owned dirs).
- **Healthcheck** subscribes to `$$SYS/broker/uptime` with the broker creds — the `$$`
  escaping is required (Compose interpolates a lone `$SYS`).
- **Persistence** (`./data`) is regenerable retained-message state; bind-mounted so Kopia
  backs it up, but losing it is harmless.

## Editing
- Compose: `templates/docker-compose.yml.j2` · cfg: `templates/mosquitto.conf.j2`, `templates/passwordfile.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "mosquitto"`
```

- [ ] **Step 7: Register in host_vars**

In `ansible/inventory/host_vars/daniel-server.yml`, add to `containers_list` (place it near the other infra services, e.g. just before `home-assistant`):

```yaml
  - name: mosquitto
    # Internal MQTT broker for the Zigbee2MQTT stack. No port/Traefik (not web-facing);
    # reachable only by zigbee2mqtt + home-assistant on the mqtt isolation net.
    use_authelia: false
    networks:
      - mqtt
```

- [ ] **Step 8: Validate template render + lint**

```bash
uv run python scripts/validate_compose_templates.py
uv run ansible-lint ansible/roles/containers/mosquitto/
```

Expected: validator renders `mosquitto` with no YAML errors; ansible-lint clean.

- [ ] **Step 9: Dry run**

```bash
uv run ansible-playbook ansible/deploy.yml --tags "mosquitto" --check
```

Expected: no failures (note: `--check` may report planned changes for the dir/template tasks).

- [ ] **Step 10: Deploy + health gate**

```bash
uv run ansible-playbook ansible/deploy.yml --tags "mosquitto"
uv run python scripts/probe.py health mosquitto
```

Expected: `probe.py health mosquitto` exits 0 (running + healthy). If unhealthy, `docker logs mosquitto` — a password-file permission or `$SYS` escaping error shows here.

- [ ] **Step 11: Commit**

```bash
git add ansible/roles/containers/mosquitto ansible/inventory/host_vars/daniel-server.yml
git commit -m "mosquitto: internal MQTT broker on the mqtt isolation net"
```

---

## Task 3: `zigbee2mqtt` role — Zigbee stack + admin UI

**Files:**
- Create: `ansible/roles/containers/zigbee2mqtt/meta/deps.yml`
- Create: `ansible/roles/containers/zigbee2mqtt/templates/configuration.yaml.j2`
- Create: `ansible/roles/containers/zigbee2mqtt/templates/docker-compose.yml.j2`
- Create: `ansible/roles/containers/zigbee2mqtt/tasks/main.yml`
- Create: `ansible/roles/containers/zigbee2mqtt/CLAUDE.md`
- Modify: `ansible/inventory/host_vars/daniel-server.yml` (register `zigbee2mqtt`; add `slzb_ip`, `zigbee_pan_id`, `zigbee_ext_pan_id`)

- [ ] **Step 1: Confirm the Z2M image tag and config schema**

Pin a specific stable Z2M 2.x tag (a fixed tag also opts the stateful container out of Watchtower auto-updates; Renovate PRs bumps). Check the latest stable release:

```bash
# Browse https://github.com/Koenkk/zigbee2mqtt/releases and note the latest 2.x tag.
```

Use that tag in Step 4 (the plan shows `2.6.0` as a concrete placeholder — replace with the confirmed latest 2.x). Also skim the config schema for that version (`https://www.zigbee2mqtt.io/guide/configuration/`) and confirm the keys in Step 3 (`homeassistant.enabled`, `frontend.enabled`, `serial.adapter: ember`, `advanced.network_key/pan_id/ext_pan_id`) match — Z2M validates config on boot, so a mismatch surfaces in `docker logs zigbee2mqtt` and fails the health gate in Step 11.

- [ ] **Step 2: `meta/deps.yml`**

```yaml
---
# HTTP UI fronted by Traefik + Authelia; bridges to MQTT, so it needs both traefik and
# the broker up first.
role_deps:
  - traefik
  - mosquitto
```

- [ ] **Step 3: `templates/configuration.yaml.j2`**

Ansible-owned source of truth (overwritten on deploy). Network identity is pinned from SOPS/host_vars so a redeploy never regenerates it. Device/pairing state lives in `./data` (`database.db`, `coordinator_backup.json`, `devices.yaml`, `groups.yaml`) which Z2M owns and we do NOT template. No `permit_join` key (removed in Z2M 2.x; joining defaults closed and is enabled from the UI when pairing).

```jinja
# Managed by Ansible (roles/containers/zigbee2mqtt). Source of truth — overwritten on deploy.
# Device pairings + network state live in ./data (database.db, coordinator_backup.json,
# devices.yaml, groups.yaml), owned by Z2M and backed up by Kopia — NOT templated here.
homeassistant:
  enabled: true
mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://mosquitto:1883
  user: "{{ mqtt_username }}"
  password: "{{ mqtt_password }}"
serial:
  port: tcp://{{ slzb_ip }}:6638
  adapter: ember
frontend:
  enabled: true
  host: 0.0.0.0
  port: 8080
advanced:
  log_level: info
  # Pinned network identity — fixed so a templated-config redeploy can't regenerate it
  # and silently un-pair every device.
  network_key: {{ zigbee_network_key }}
  pan_id: {{ zigbee_pan_id }}
  ext_pan_id: {{ zigbee_ext_pan_id }}
devices: devices.yaml
groups: groups.yaml
```

- [ ] **Step 4: `templates/docker-compose.yml.j2`**

Standard web service: Traefik `labels()` (binds to networks[0] = `apps`) + kuma + healthcheck + resources. Replace `2.6.0` with the tag confirmed in Step 1.

```jinja
{% from 'traefik.yml.j2' import labels with context %}
{% from 'autokuma.yml.j2' import labels as kuma with context %}
{% from 'networks.yml.j2' import service_networks, external_networks with context %}
{% from 'resources.yml.j2' import resources %}
---

services:
  zigbee2mqtt:
    image: ghcr.io/koenkk/zigbee2mqtt:2.6.0
    container_name: zigbee2mqtt
    environment:
      - TZ={{ tz }}
    volumes:
      - ./data:/app/data
    restart: unless-stopped
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    depends_on:
      - mosquitto
    {{ service_networks() }}
    # Z2M takes a moment to connect to the coordinator before the frontend serves.
    {{ healthcheck('wget -q -O /dev/null http://localhost:8080/ || exit 1', start_period='60s') }}
    labels:
      {# Generate labels(container, port, docker_network, use_authelia) -#}
      {{ labels(
          container_item.hostname | default(container_item.name),
          container_item.port | string,
          (container_item.networks | default([docker_network]))[0],
          container_item.use_authelia
        )
      }}
      {{ kuma(container_item.name) }}
    # Resource caps for blast-radius containment (M1); tune from cAdvisor/Grafana.
    {{ resources('0.50', '256M', '0.05', '64M') }}

{{ external_networks() }}
```

Note: `depends_on: mosquitto` is intra-compose only at runtime if both were in one file — here they are separate roles/compose projects, so the real ordering guarantee is `meta/deps.yml` (Step 2). The `depends_on` is harmless/ignored across projects; keep it documentary or drop it. **Drop the `depends_on:` block** to avoid a Compose warning about an undefined service — cross-project ordering is handled by `deps.yml`.

Corrected service block (no `depends_on`):

```jinja
  zigbee2mqtt:
    image: ghcr.io/koenkk/zigbee2mqtt:2.6.0
    container_name: zigbee2mqtt
    environment:
      - TZ={{ tz }}
    volumes:
      - ./data:/app/data
    restart: unless-stopped
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    {{ service_networks() }}
    {{ healthcheck('wget -q -O /dev/null http://localhost:8080/ || exit 1', start_period='60s') }}
    labels:
      {{ labels(
          container_item.hostname | default(container_item.name),
          container_item.port | string,
          (container_item.networks | default([docker_network]))[0],
          container_item.use_authelia
        )
      }}
      {{ kuma(container_item.name) }}
    {{ resources('0.50', '256M', '0.05', '64M') }}
```

- [ ] **Step 5: `tasks/main.yml`**

`no_log` on the config template (MQTT creds + network key); `register` → `common_config_changed`.

```yaml
---
- name: Create required directories
  tags: [config]
  ansible.builtin.include_role:
    name: common
    tasks_from: setup_dirs.yml
  vars:
    common_dirs_to_create:
      - "{{ container_item.name }}/data"

- name: Create Zigbee2MQTT configuration.yaml from template
  tags: [config]
  ansible.builtin.template:
    src: configuration.yaml.j2
    dest: "/home/{{ sys_user }}/server/containers/{{ container_item.name }}/data/configuration.yaml"
    mode: "0640"
  no_log: true
  register: zigbee2mqtt_config

- name: Deploy Container
  tags: [deploy]
  ansible.builtin.include_role:
    name: common
    tasks_from: docker_deploy.yml
  vars:
    # configuration.yaml is the Ansible source of truth — recreate on edit.
    common_config_changed: "{{ zigbee2mqtt_config is changed }}"
```

- [ ] **Step 6: `CLAUDE.md`**

```markdown
# zigbee2mqtt — Zigbee coordinator bridge (SLZB-06M → MQTT)

Zigbee2MQTT 2.x bridging the network-attached SLZB-06M coordinator into MQTT/Home Assistant.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/koenkk/zigbee2mqtt:<pinned 2.x>` (pinned → Renovate-managed, not Watchtower)
- **Host:** daniel-server · **Port:** 8080 · **URL:** `zigbee.<domain>` (Authelia: yes)
- **Networks:** `apps` (Traefik) + `mqtt` (broker). Reaches the coordinator at `tcp://{{ slzb_ip }}:6638`
  over the LAN via Docker's outbound NAT — no host networking.
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
  (`zigbee.<domain>`) when adding devices, then disable.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Z2M cfg: `templates/configuration.yaml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "zigbee2mqtt"`
```

- [ ] **Step 7: Register in host_vars + add the coordinator/PAN vars**

In `ansible/inventory/host_vars/daniel-server.yml`, add the service to `containers_list` (in the apps section, near `code-server`):

```yaml
  - name: zigbee2mqtt
    port: 8080
    use_authelia: true
    networks:
      - apps   # networks[0]: Traefik binds the route here
      - mqtt   # reach the mosquitto broker
```

And add the (non-secret) coordinator + PAN identity vars near the top of the host_vars file (the network_key is the only sensitive one and lives in SOPS). Generate fresh ext_pan_id bytes; pan_id is any 1..65534 int:

```yaml
# SLZB-06M Zigbee coordinator (network-attached) + pinned Zigbee PAN identity.
slzb_ip: 10.0.0.127
zigbee_pan_id: 6754
zigbee_ext_pan_id: [221, 187, 17, 34, 51, 68, 85, 102]
```

- [ ] **Step 8: Validate render + lint**

```bash
uv run python scripts/validate_compose_templates.py
uv run ansible-lint ansible/roles/containers/zigbee2mqtt/
```

Expected: `zigbee2mqtt` renders to valid YAML; lint clean. (The validator stubs secrets; the real `mqtt_*`/`zigbee_network_key` values come from SOPS at deploy.)

- [ ] **Step 9: Dry run**

```bash
uv run ansible-playbook ansible/deploy.yml --tags "zigbee2mqtt" --check
```

Expected: no failures.

- [ ] **Step 10: Deploy**

```bash
uv run ansible-playbook ansible/deploy.yml --tags "zigbee2mqtt"
```

- [ ] **Step 11: Health gate + coordinator connection check**

```bash
uv run python scripts/probe.py health zigbee2mqtt
docker logs zigbee2mqtt 2>&1 | grep -iE "coordinator|connected|started|error" | tail -20
```

Expected: `probe.py health zigbee2mqtt` exits 0; logs show Z2M connected to the coordinator and the frontend started, with no MQTT-auth or adapter errors. A `tcp://10.0.0.127:6638` connection failure points at the coordinator's network/firmware; an MQTT auth failure points at the Task 0 creds.

- [ ] **Step 12: Verify the UI route**

```bash
uv run python scripts/probe.py targets zigbee
```

Expected: `zigbee.<domain>` resolves through Traefik (Authelia gate in front). Confirm in a browser that the Z2M dashboard loads behind Authelia.

- [ ] **Step 13: Commit**

```bash
git add ansible/roles/containers/zigbee2mqtt ansible/inventory/host_vars/daniel-server.yml
git commit -m "zigbee2mqtt: SLZB-06M bridge to MQTT, Traefik+Authelia UI, pinned network key"
```

---

## Task 4: Join Home Assistant to the `mqtt` network

**Files:**
- Modify: `ansible/inventory/host_vars/daniel-server.yml` (`home-assistant` networks)

- [ ] **Step 1: Add `mqtt` to HA's networks**

In `ansible/inventory/host_vars/daniel-server.yml`, the `home-assistant` entry's `networks:`:

```yaml
    networks:
      - apps
      - ups  # reach the nut sidecar's upsd:3493 for the Home Assistant NUT integration
      - mqtt  # reach the mosquitto broker for the MQTT (Zigbee2MQTT) integration
```

- [ ] **Step 2: Validate render**

```bash
uv run python scripts/validate_compose_templates.py
```

Expected: `home-assistant` renders with the `mqtt` network attached (top-level + service list).

- [ ] **Step 3: Redeploy HA**

```bash
uv run ansible-playbook ansible/deploy.yml --tags "home-assistant"
uv run python scripts/probe.py health home-assistant
```

Expected: HA recreates onto the `mqtt` net and returns healthy. Verify reachability to the broker:

```bash
docker exec home-assistant getent hosts mosquitto
```

Expected: resolves to the mosquitto container IP (confirms HA is on the `mqtt` net with the broker).

- [ ] **Step 4: Commit**

```bash
git add ansible/inventory/host_vars/daniel-server.yml
git commit -m "home-assistant: join mqtt net to reach the mosquitto broker"
```

---

## Task 5: Confirm Kopia backs up the new state (no exclusion)

**Files:** read-only check of `ansible/roles/containers/kopia/templates/kopiaignore.j2`

- [ ] **Step 1: Verify no kopiaignore pattern excludes the new data**

```bash
grep -nE "mosquitto|zigbee|database\.db|coordinator_backup|\*\.db" ansible/roles/containers/kopia/templates/kopiaignore.j2
```

Expected: no pattern that would exclude `containers/zigbee2mqtt/data/database.db`, `coordinator_backup.json`, or `containers/mosquitto/data`. The existing `*.db-wal`/`*.db-shm` rules do NOT match `database.db`/`mosquitto.db`, so nothing to change. If a broad `*.db` rule exists (it should not), add a negation for the Z2M database — losing it means re-pairing every device.

- [ ] **Step 2: (Only if a change was needed) redeploy kopia + commit**

If Step 1 found an exclusion, fix `kopiaignore.j2`, then:

```bash
uv run ansible-playbook ansible/deploy.yml --tags "kopia"
git add ansible/roles/containers/kopia/templates/kopiaignore.j2
git commit -m "kopia: ensure zigbee2mqtt device DB is in backup scope"
```

Otherwise record "no change needed — Z2M database.db and mosquitto data are in scope" and move on.

---

## Task 6: Operator steps — HA MQTT integration + first pairing (manual)

These are HA/Z2M **UI** actions (HA stores integration config in `.storage/`, intentionally not templated). Document completion; nothing to commit.

- [ ] **Step 1: Add the MQTT integration in HA**

In HA → Settings → Devices & Services → Add Integration → **MQTT**:
- Broker: `mosquitto`
- Port: `1883`
- Username / Password: the `mqtt_username` / `mqtt_password` from Task 0

Expected: integration connects (HA is on the `mqtt` net from Task 4).

- [ ] **Step 2: Pair a device via the Z2M UI**

At `zigbee.<domain>` (Z2M dashboard) → enable "Permit join" → put a Zigbee device into pairing mode → confirm it appears in Z2M → disable "Permit join".

- [ ] **Step 3: Confirm discovery into HA**

Expected: the paired device appears automatically in HA (MQTT discovery) under the MQTT integration. If it doesn't, check `docker logs zigbee2mqtt` for the discovery publish and HA's MQTT integration for received topics.

---

## Task 7: Final verification + docs/memory

- [ ] **Step 1: Full repo gate**

```bash
prek run --all-files
```

Expected: ansible-lint, validate-compose-templates, gitleaks, and pytest all pass.

- [ ] **Step 2: Confirm all three containers healthy**

```bash
uv run python scripts/probe.py health mosquitto
uv run python scripts/probe.py health zigbee2mqtt
uv run python scripts/probe.py health home-assistant
```

Expected: all exit 0.

- [ ] **Step 3: Record the build in memory (optional, recommended)**

Add a memory note capturing: the `mqtt` isolation net; the pinned-network-key invariant (never set to GENERATE — re-pairs all devices); the `$$SYS` healthcheck escaping; coordinator at `tcp://10.0.0.127:6638` adapter `ember`.

---

## Self-review notes (author)

- **Spec coverage:** mosquitto role (Task 2), zigbee2mqtt role (Task 3), `mqtt` net (Task 1), HA net join (Task 4), secrets incl. the planning-surfaced `zigbee_network_key` (Task 0), Kopia scope (Task 5), HA MQTT integration + pairing (Task 6) — all spec sections mapped.
- **Beyond spec (flagged):** `zigbee_network_key` secret + `zigbee_pan_id`/`zigbee_ext_pan_id` host_vars pin the Zigbee identity so the templated-config-overwrite model is safe. This is an addition to the approved spec — confirm with the operator.
- **Verify-at-impl items (not placeholders — explicit checks):** exact Z2M image tag + config-schema keys (Task 3 Step 1); `ember` adapter vs the flashed firmware (surfaces in logs, Task 3 Step 11).
