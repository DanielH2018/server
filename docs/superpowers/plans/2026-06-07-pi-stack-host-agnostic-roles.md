# Pi Stack — Host-Agnostic Roles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a minimal, LAN-only utility stack on `daniel-pi` (wg-easy + glances + dozzle + docker-proxy + autoheal, no Portainer) by making the web-UI roles host-agnostic and collapsing the two `wg-easy` roles into one.

**Architecture:** A new host variable `expose_mode` (`traefik` default / `lan` on the Pi) drives a shared Jinja macro (`ansible/templates/expose.yml.j2`) that renders each web UI either with Traefik+Authelia labels (server) or a LAN-bound published port (Pi). `wg-easy`, `glances`, and a new `dozzle` role consume it; `docker-proxy` and `autoheal` are already host-agnostic and just get added to the Pi's `containers_list`.

**Tech Stack:** Ansible (community.docker), Jinja2 templates, Docker Compose, `uv`-managed pytest + `scripts/validate_compose_templates.py`, `prek` pre-commit.

**Reference spec:** `docs/superpowers/specs/2026-06-07-pi-stack-host-agnostic-roles-design.md`

**Testing model (read first):** This repo does **not** unit-test individual compose templates with pytest. The oracle for template work is `scripts/validate_compose_templates.py` — it renders every container in every host's `containers_list` with Ansible's exact whitespace settings and asserts valid YAML. Treat "run the validator, expect 0 failures" as the test step. `uv run pytest` covers the Python dep/toposort logic (unchanged here but must stay green). All commands run from the repo root `/home/ubuntu/server` unless noted. Deploy/on-host steps (Task 7) run **on daniel-pi**.

---

### Task 1: Add `expose_mode` + the shared `expose.yml.j2` macro, convert `glances`

This task introduces the abstraction and proves it in `traefik` mode against the server's existing `glances` (byte-shape preserved, still valid YAML, still Traefik-routed).

**Files:**
- Modify: `ansible/inventory/group_vars/all.yml`
- Create: `ansible/templates/expose.yml.j2`
- Modify: `prek.toml:58` (validator `files` regex)
- Modify: `ansible/roles/containers/glances/templates/docker-compose.yml.j2`
- Modify: `ansible/roles/containers/glances/CLAUDE.md`

- [ ] **Step 1: Add the `expose_mode` default to `all.yml`**

In `ansible/inventory/group_vars/all.yml`, find the `# Docker` block:

```yaml
# Docker
docker_network: proxy
```

Change it to:

```yaml
# Docker
docker_network: proxy
# Default web-UI exposure mode for hosts. `traefik` = route UIs through Traefik
# (Authelia-gated when use_authelia). Override to `lan` on LAN-only hosts (daniel-pi)
# to publish UI ports bound to the host's LAN IP instead. Consumed by expose.yml.j2.
expose_mode: traefik
```

- [ ] **Step 2: Create the shared macro `ansible/templates/expose.yml.j2`**

```jinja2
{# Host-aware web-UI exposure. Driven by `expose_mode` (host var; default 'traefik',
   'lan' on LAN-only hosts like daniel-pi). A per-container `expose_mode` key overrides
   the host default. Two modes:

     traefik — Traefik routes the UI via labels (Authelia-gated when use_authelia);
               no host port is published. (main server)
     lan     — no Traefik labels; the UI port is published bound to the host's LAN IP
               ({{ server_ip }}), reachable only on the local network. (the Pi)

   Invoke BOTH macros at COLUMN 0 on their own line; each bakes in the service-block
   indentation and renders empty (one harmless blank line) in the other mode:

       labels:
   {{ web_ui_labels('8096') }}
         {{ kuma(container_item.name) }}

   {{ web_ui_ports_block('8096') }}

   `web_ui_ports_block` emits a complete `ports:` block, so it is for services whose only
   published port is the UI (glances, dozzle). wg-easy, which always publishes its
   WireGuard UDP port, appends its LAN UI port with an inline conditional instead.
#}
{% from 'traefik.yml.j2' import labels with context %}
{% macro web_ui_labels(internal_port=none) -%}
{% if (container_item.expose_mode | default(expose_mode | default('traefik'))) != 'lan' %}
      {{ labels(
          container_item.hostname | default(container_item.name),
          (internal_port if internal_port is not none else container_item.port) | string,
          (container_item.networks | default([docker_network]))[0],
          container_item.use_authelia
        ) }}
{%- endif %}
{%- endmacro %}
{% macro web_ui_ports_block(internal_port) -%}
{% if (container_item.expose_mode | default(expose_mode | default('traefik'))) == 'lan' %}
    ports:
      - "{{ server_ip }}:{{ container_item.port }}:{{ internal_port }}/tcp"
{%- endif %}
{%- endmacro %}
```

- [ ] **Step 3: Add `expose` to the validator's `prek.toml` trigger**

In `prek.toml`, line 58, change the shared-template alternation `(traefik|autokuma)` to `(traefik|autokuma|expose)`:

```toml
files = "^(ansible/roles/containers/[^/]+/templates/docker-compose\\.yml\\.j2|ansible/templates/(traefik|autokuma|expose)\\.yml\\.j2|ansible/inventory/(host_vars/[^/]+|group_vars/all)\\.yml|scripts/validate_compose_templates\\.py)$"
```

- [ ] **Step 4: Convert the `glances` template to use the macros**

Replace the **entire** contents of `ansible/roles/containers/glances/templates/docker-compose.yml.j2` with:

```jinja2
{% from 'autokuma.yml.j2' import labels as kuma with context %}
{% from 'networks.yml.j2' import service_networks, external_networks with context %}
{% from 'resources.yml.j2' import resources %}
{% from 'expose.yml.j2' import web_ui_labels, web_ui_ports_block with context %}
---

services:
  glances:
    container_name: glances
    image: nicolargo/glances:latest
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:61208/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    {{ service_networks() }}
    pid: host
    volumes:
      - /etc/os-release:/etc/os-release:ro
    environment:
      - "GLANCES_OPT=-w"
      - DOCKER_HOST=tcp://docker-proxy:2375
    labels:
{{ web_ui_labels('61208') }}
      {{ kuma(container_item.name) }}

      # Watchtower
      - "com.centurylinklabs.watchtower.depends-on:docker-proxy"
{{ web_ui_ports_block('61208') }}
    # Resource caps for blast-radius containment (M1); tune from cAdvisor/Grafana.
    {{ resources('0.50', '256M', '0.05', '32M') }}

{{ external_networks() }}
```

Note the two macro calls (`web_ui_labels`, `web_ui_ports_block`) are at **column 0**, and the direct `traefik.yml.j2` import is gone (the macro wraps it).

- [ ] **Step 5: Run the validator — this is the test**

Run:
```bash
uv run python scripts/validate_compose_templates.py
```
Expected: ends with `N template(s) checked, 0 failure(s).` and `[ok]   glances` for the `daniel-server.yml` host. (The Pi has no glances yet, so only the server's glances is exercised — in `traefik` mode.)

If glances FAILS with invalid YAML or a render error, the rendered template is dumped with line numbers. The usual culprit is whitespace: confirm the macro `{% if %}`/`{%- endif %}` trims match Step 2 exactly and that the macro calls sit at column 0.

- [ ] **Step 6: Eyeball the rendered server output (optional sanity)**

Run:
```bash
uv run python -c "
from pathlib import Path
import sys; sys.argv=['x']
import scripts.validate_compose_templates as v
host=v.load_yaml(v.HOST_VARS/'daniel-server.yml')
ctx={**v.BASE_CONTEXT, **v.load_yaml(v.ALL_VARS), **host}; ctx.pop('containers_list',None)
ci=next(c for c in host['containers_list'] if c['name']=='glances')
env=v.build_env('glances'); ctx['container_item']=ci; env.globals.update(ctx)
print(env.get_template('docker-compose.yml.j2').render(**ctx))
"
```
Expected: a `labels:` block containing `- "traefik.enable=true"` … `- "traefik.http.routers.glances-secure.middlewares=authelia@docker"`, **no** `ports:` block, and a single blank line where `web_ui_ports_block` rendered empty. This confirms server behavior is preserved.

- [ ] **Step 7: Update the glances CLAUDE.md**

In `ansible/roles/containers/glances/CLAUDE.md`, under `## Notable`, add a bullet:

```markdown
- **Host-agnostic exposure:** the template uses `expose.yml.j2` (`web_ui_labels` /
  `web_ui_ports_block`) so it renders Traefik+Authelia labels on the server (`expose_mode:
  traefik`) and a LAN-bound port on hosts with `expose_mode: lan` (daniel-pi). It runs on
  both hosts where listed in `containers_list`.
```

- [ ] **Step 8: Commit**

```bash
git add ansible/inventory/group_vars/all.yml ansible/templates/expose.yml.j2 prek.toml ansible/roles/containers/glances/templates/docker-compose.yml.j2 ansible/roles/containers/glances/CLAUDE.md
git commit -m "containers: add expose_mode + expose.yml.j2 macro; convert glances

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Unify the two `wg-easy` roles into one host-agnostic role

**Files:**
- Modify: `ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2`
- Modify: `ansible/inventory/host_vars/daniel-server.yml` (add `udp_port: 51820` to the wg-easy entry)
- Modify: `ansible/roles/containers/wg-easy/CLAUDE.md`
- Delete: `ansible/roles/containers/wg-easy-pi/` (entire directory)

- [ ] **Step 1: Rewrite the `wg-easy` template (host-agnostic)**

Replace the **entire** contents of `ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2` with:

```jinja2
{% from 'autokuma.yml.j2' import labels as kuma with context %}
{% from 'networks.yml.j2' import service_networks, external_networks with context %}
{% from 'resources.yml.j2' import resources %}
{% from 'expose.yml.j2' import web_ui_labels with context %}
---

services:
  wg-easy:
    image: ghcr.io/wg-easy/wg-easy:latest
    container_name: wg-easy
    # NET_ADMIN to create the wg interface + apply rules; NET_RAW because wg-quick runs
    # iptables (the iptables CLI opens a raw socket to init the 'nat' table — fails with
    # "can't initialize iptables table `nat`: Permission denied" without it). SYS_MODULE
    # not needed (wireguard module loaded on host). Verify on deploy (docker logs wg-easy).
    cap_drop:
      - ALL
    cap_add:
      - NET_ADMIN
      - NET_RAW
    security_opt:
      - no-new-privileges:true
    sysctls:
      - net.ipv4.ip_forward=1
      - net.ipv4.conf.all.src_valid_mark=1
    ports:
      - "{{ container_item.udp_port }}:{{ container_item.udp_port }}/udp"
{% if (container_item.expose_mode | default(expose_mode | default('traefik'))) == 'lan' %}
      # LAN-only hosts (daniel-pi): bind the admin UI to the host's LAN IP, not 0.0.0.0,
      # so it isn't reachable on the WireGuard tunnel or other interfaces.
      - "{{ server_ip }}:{{ container_item.port }}:51821/tcp"
{% endif %}
    environment:
      - LANG=en
      - WG_HOST=wireguard.{{ domain }}
      - WG_PORT={{ container_item.udp_port }}
      - WG_PERSISTENT_KEEPALIVE=25
    volumes:
      - ./config:/etc/wireguard
    restart: unless-stopped
    {{ service_networks() }}
    labels:
{{ web_ui_labels('51821') }}
      {{ kuma(container_item.name) }}
    # Resource caps for blast-radius containment (M1); tune from cAdvisor/Grafana.
    {{ resources('0.50', '256M', '0.05', '32M') }}

{{ external_networks() }}
```

Key changes vs. the old server template: UDP port is now `container_item.udp_port` (was hardcoded `51820`), `WG_PORT` is set explicitly, the LAN UI port is appended via the inline `{% if %}`, and the `labels:` block uses `web_ui_labels` (so on the Pi it emits no Traefik labels). The native image healthcheck is unchanged (no compose `healthcheck:`).

- [ ] **Step 2: Add `udp_port` to the server's wg-easy entry**

In `ansible/inventory/host_vars/daniel-server.yml`, find the wg-easy entry:

```yaml
  - name: wg-easy
    port: 51821
    use_authelia: true
    networks:
      - apps
    tags:
      - wg-easy
```

Change it to:

```yaml
  - name: wg-easy
    port: 51821
    udp_port: 51820
    use_authelia: true
    networks:
      - apps
    tags:
      - wg-easy
```

- [ ] **Step 3: Delete the `wg-easy-pi` role**

```bash
git rm -r ansible/roles/containers/wg-easy-pi
```
Expected: removes `tasks/`, `templates/`, `meta/`, `CLAUDE.md` under that role.

- [ ] **Step 4: Run the validator**

Run:
```bash
uv run python scripts/validate_compose_templates.py
```
Expected: `0 failure(s).`, `[ok]   wg-easy` under `daniel-server.yml`. (The Pi's `containers_list` still references `wg-easy-pi` at this point — that's fine; the validator skips containers whose role has no compose template, and `wg-easy-pi`'s template still exists until... actually it was just deleted. See note.)

**Note:** Step 3 deleted `wg-easy-pi`'s template, but `daniel-pi.yml` still lists `name: wg-easy-pi`. The validator's `check_container` returns `None` (skips) when `roles/containers/wg-easy-pi/templates/docker-compose.yml.j2` does not exist, so it will not error — it simply won't validate that entry. Task 4 removes the stale `wg-easy-pi` entry. If you prefer zero stale references between commits, do Task 4 before committing Task 2; otherwise this intermediate state is safe (validator green).

- [ ] **Step 5: Update the wg-easy CLAUDE.md to cover both hosts**

Replace the contents of `ansible/roles/containers/wg-easy/CLAUDE.md` with:

```markdown
# wg-easy — WireGuard VPN server + UI

WireGuard VPN with the wg-easy web admin, for remote access into the homelab.
**One host-agnostic role serves both daniel-server and daniel-pi** (the old separate
`wg-easy-pi` role was merged into this one). See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/wg-easy/wg-easy:latest`
- **Hosts:** daniel-server AND daniel-pi
- **UI port:** 51821 · **URL:** `wg-easy.<domain>` (server, behind Authelia) / `http://<pi-lan-ip>:51821` (Pi)
- **WireGuard UDP port:** `udp_port` per host — **51820 on daniel-server, 51822 on daniel-pi**
  (both sit behind one public IP/router, so the listen ports must differ).
- **Networks:** apps (server) / proxy (Pi)
- **Config in:** each `ansible/inventory/host_vars/<host>.yml` → `containers_list`

## Notable
- **Exposure is host-driven** via `expose.yml.j2` + `expose_mode`: on the server
  (`expose_mode: traefik`) the UI is routed through Traefik behind Authelia and no host
  port is published; on the Pi (`expose_mode: lan`) the UI is published bound to the Pi's
  LAN IP and emits no Traefik labels. The WireGuard UDP port is always published on the host.
- **Built-in healthcheck:** the `wg-easy/wg-easy` image ships its own Docker `HEALTHCHECK`
  (`wg show | grep -q interface` — verifies the WireGuard *interface* is up, not just the
  UI), so there is no compose `healthcheck:` block. `autoheal` and uptime-kuma rely on this
  native status — don't add a redundant probe.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy (server): `uv run ansible-playbook ansible/deploy.yml --tags "wg-easy"`
- Deploy (Pi): run on daniel-pi — `uv run ansible-playbook ansible/deploy.yml --tags "wg-easy" --limit daniel-pi`
```

- [ ] **Step 6: Commit**

```bash
git add ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2 ansible/inventory/host_vars/daniel-server.yml ansible/roles/containers/wg-easy/CLAUDE.md
git commit -m "containers: unify wg-easy into one host-agnostic role; delete wg-easy-pi

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
(The `git rm` from Step 3 is already staged and included in this commit.)

---

### Task 3: Create the new `dozzle` role

**Files:**
- Create: `ansible/roles/containers/dozzle/tasks/main.yml`
- Create: `ansible/roles/containers/dozzle/templates/docker-compose.yml.j2`
- Create: `ansible/roles/containers/dozzle/meta/main.yml`
- Create: `ansible/roles/containers/dozzle/meta/deps.yml`
- Create: `ansible/roles/containers/dozzle/CLAUDE.md`

This role is inert until wired into a host's `containers_list` (Task 4), so there is no render to validate yet — the validator exercises it in Task 4.

- [ ] **Step 1: `tasks/main.yml`** (mirrors every other container role)

```yaml
---
- name: Create required directories
  ansible.builtin.include_role:
    name: common
    tasks_from: setup_dirs.yml
  vars:
    common_dirs_to_create:
      - "{{ container_item.name }}"

- name: Deploy Container
  ansible.builtin.include_role:
    name: common
    tasks_from: docker_deploy.yml
```

- [ ] **Step 2: `templates/docker-compose.yml.j2`**

```jinja2
{% from 'autokuma.yml.j2' import labels as kuma with context %}
{% from 'networks.yml.j2' import service_networks, external_networks with context %}
{% from 'resources.yml.j2' import resources %}
{% from 'expose.yml.j2' import web_ui_labels, web_ui_ports_block with context %}
---

services:
  dozzle:
    image: amir20/dozzle:latest
    container_name: dozzle
    restart: unless-stopped
    # Read-only log viewer: reaches Docker through the read-only docker-proxy (no raw
    # socket mount). It only needs GET /containers, /logs, /events — all allowed by the
    # shared read-only proxy (CONTAINERS=1, EVENTS=1, POST=0). Verify on deploy.
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    environment:
      - DOCKER_HOST=tcp://docker-proxy:2375
      - DOZZLE_NO_ANALYTICS=true
      - TZ={{ tz }}
    {{ service_networks() }}
    healthcheck:
      # Dozzle ships a self-check subcommand in its own binary (no curl/wget needed,
      # so it works under cap_drop ALL). Verify the path on deploy (docker inspect).
      test: ["CMD", "/dozzle", "healthcheck"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
    labels:
{{ web_ui_labels('8080') }}
      {{ kuma(container_item.name) }}

      # Watchtower
      - "com.centurylinklabs.watchtower.depends-on:docker-proxy"
{{ web_ui_ports_block('8080') }}
    # Resource caps for blast-radius containment (M1); tune from cAdvisor/Grafana.
    {{ resources('0.25', '128M', '0.05', '16M') }}

{{ external_networks() }}
```

- [ ] **Step 3: `meta/main.yml`** (mirror `glances/meta/main.yml`)

```yaml
---
galaxy_info:
  author: DanielH2018
  description: "Real-time Docker log viewer"
  license: MIT
  min_ansible_version: "2.15"

# Logical ordering: requires docker-proxy (reads the Docker API through it).
# Actual sequencing is computed by the toposort filter in deploy.yml reading meta/deps.yml,
# not by Ansible meta dependencies (which conflict with the include_role loop
# variable architecture used in deploy.yml).
dependencies: []
```

- [ ] **Step 4: `meta/deps.yml`**

```yaml
---
role_deps:
  - docker-proxy
```

- [ ] **Step 5: `CLAUDE.md`**

```markdown
# dozzle — Real-time Docker log viewer

Read-only web UI that live-tails `docker logs` across containers. Added to the Pi as the
ad-hoc logging tool in place of Portainer. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `amir20/dozzle:latest`
- **Host:** daniel-pi (role is host-agnostic; only listed in the Pi's `containers_list`)
- **Port:** 8080 · **URL:** `http://<pi-lan-ip>:8080` (LAN-bound, no Authelia)
- **Networks:** proxy
- **Depends on:** docker-proxy
- **Config in:** `ansible/inventory/host_vars/daniel-pi.yml` → `containers_list`

## Notable
- **No raw socket:** reads the Docker API through the read-only `docker-proxy`
  (`DOCKER_HOST=tcp://docker-proxy:2375`), so it never mounts `/var/run/docker.sock`.
- **Stateless:** no bind mounts / DB. `DOZZLE_NO_ANALYTICS=true` disables phone-home.
- **Exposure is host-driven** via `expose.yml.j2` + `expose_mode` — Traefik+Authelia where
  `expose_mode: traefik`, LAN-bound where `expose_mode: lan`.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: run on daniel-pi — `uv run ansible-playbook ansible/deploy.yml --tags "dozzle" --limit daniel-pi`
```

- [ ] **Step 6: Commit**

```bash
git add ansible/roles/containers/dozzle
git commit -m "containers: add dozzle role (read-only log viewer, host-agnostic)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Wire the Pi's `containers_list`

Swap the Pi's stack to the new set and turn on `lan` mode. This is where the validator first renders glances/dozzle/wg-easy in `lan` mode and docker-proxy/autoheal on the Pi.

**Files:**
- Modify: `ansible/inventory/host_vars/daniel-pi.yml`

- [ ] **Step 1: Replace `daniel-pi.yml`**

Replace the **entire** contents of `ansible/inventory/host_vars/daniel-pi.yml` with:

```yaml
---
kuma_docker_host: 1
server_ip: 10.0.0.139
ssh_config_path: /etc/ssh/sshd_config.d/50-cloud-init.conf
domain: daniel-pi.com
# LAN-only host: publish web UIs bound to the LAN IP instead of routing through Traefik.
expose_mode: lan
containers_list:
  # Read-only Docker socket proxy — serves glances + dozzle; the hardcoded
  # docker-proxy-lifecycle sub-proxy (restart-only) serves autoheal.
  - name: docker-proxy
    use_authelia: false
    networks:
      - proxy
    tags:
      - docker-proxy
  - name: wg-easy
    port: 51821
    udp_port: 51822
    use_authelia: false
    networks:
      - proxy
    tags:
      - wg-easy
  - name: glances
    port: 61208
    use_authelia: false
    networks:
      - proxy
    tags:
      - glances
  - name: dozzle
    port: 8080
    use_authelia: false
    networks:
      - proxy
    tags:
      - dozzle
  - name: autoheal
    use_authelia: false
    networks:
      - lifecycle
    tags:
      - autoheal
```

This removes the old `portainer` and `wg-easy-pi` entries and the commented-out
autoheal/watchtower stubs.

- [ ] **Step 2: Run the validator — exercises the full Pi stack in `lan` mode**

Run:
```bash
uv run python scripts/validate_compose_templates.py
```
Expected: `0 failure(s).` and under `daniel-pi.yml`: `[ok]` for `docker-proxy`, `wg-easy`, `glances`, `dozzle`, `autoheal`.

If a `lan`-mode template fails, dump and inspect — the common issue is the `web_ui_ports_block` / inline `{% if %}` whitespace. Cross-check against Task 1 Step 2 and Task 2 Step 1.

- [ ] **Step 3: Eyeball the Pi-rendered wg-easy + dozzle (sanity)**

Run:
```bash
uv run python -c "
import scripts.validate_compose_templates as v
host=v.load_yaml(v.HOST_VARS/'daniel-pi.yml')
base={**v.BASE_CONTEXT, **v.load_yaml(v.ALL_VARS), **host}; base.pop('containers_list',None)
for nm in ('wg-easy','dozzle'):
    ci=next(c for c in host['containers_list'] if c['name']==nm)
    ctx=dict(base, container_item=ci); env=v.build_env(nm); env.globals.update(ctx)
    print('================',nm,'================')
    print(env.get_template('docker-compose.yml.j2').render(**ctx))
"
```
Expected for `wg-easy`: a `ports:` block with both `- "51822:51822/udp"` and `- "10.0.0.139:51821:51821/tcp"`, `WG_PORT=51822`, `WG_HOST=wireguard.daniel-pi.com`, and a `labels:` block with **no** `traefik.*` labels (only `kuma.*`). Expected for `dozzle`: a `ports:` block `- "10.0.0.139:8080:8080/tcp"` and no `traefik.*` labels.

- [ ] **Step 4: Commit**

```bash
git add ansible/inventory/host_vars/daniel-pi.yml
git commit -m "inventory: switch daniel-pi to LAN-only utility stack (drop portainer)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Full local verification gate

Run the complete CI-equivalent suite before deploying. All from repo root.

- [ ] **Step 1: Python unit tests stay green** (toposort/dep filters unaffected but must pass)

Run:
```bash
uv run pytest
```
Expected: all pass (no failures). Of particular interest: `ansible/tests/test_toposort.py` (incl. `test_dep_absent_from_list_is_ignored`, which is what lets the unified wg-easy carry `traefik`/`authelia` deps absent on the Pi).

- [ ] **Step 2: Full pre-commit suite**

Run:
```bash
prek run --all-files
```
Expected: all hooks Passed — notably `ansible-lint`, `check yaml`, `Validate rendered docker-compose templates`, `trim trailing whitespace`, `Detect hardcoded secrets`. If `trim trailing whitespace` flags a macro-rendered file, re-check the `{%- endif %}` trims.

- [ ] **Step 3: Ansible dry run for the Pi stack**

> Run this where the Pi inventory resolves the way it deploys. Deploys use
> `ansible_connection=local` per host, so run on **daniel-pi**. `--check` validates that the
> playbook + roles render and sequence without applying changes (community.docker tasks may
> report `changed` under check mode — that's expected; you're looking for *errors*, not diffs).

On daniel-pi:
```bash
uv run ansible-playbook ansible/deploy.yml --tags "docker-proxy,wg-easy,glances,dozzle,autoheal" --limit daniel-pi --check
```
Expected: play runs through the pre_tasks (toposort) and each role's tasks with no fatal errors; the toposort orders `docker-proxy` before `autoheal`/`dozzle`. If `--check` errors on a docker module that can't simulate, note it and rely on Step 1/2 + the real deploy in Task 7.

- [ ] **Step 4: No commit** (verification only). If anything failed, fix in the relevant earlier task's files and re-run.

---

### Task 6: Push

- [ ] **Step 1: Push the branch/commits**

```bash
git push
```
Expected: CI (`.github/workflows/ci.yml`) runs `prek run --all-files` on the push and goes green. (Commits go to `master` per project convention — no feature branch.)

> **Heads-up — GitOps auto-deploy:** pushing to `master` triggers the server's GitOps
> deployer (timer-driven, ~30 min). It will redeploy the server-side roles whose templates
> changed here — `glances` and `wg-easy`. Both render in `traefik` mode with the same
> effective config (wg-easy gains an explicit `WG_PORT=51820` that already matched the
> default), so the recreate is safe and idempotent. If you want to control timing, deploy
> the Pi (Task 7) right after pushing rather than waiting for the next GitOps tick.

---

### Task 7: Deploy to the Pi + decommission Portainer + verify on host

> Runs **on daniel-pi**. This is the only step that changes running infrastructure.

- [ ] **Step 1: Deploy the new Pi stack**

On daniel-pi, first pull the pushed changes into the Pi's own checkout, then deploy:
```bash
cd ~/server && git pull
uv run ansible-playbook ansible/deploy.yml --tags "docker-proxy,wg-easy,glances,dozzle,autoheal" --limit daniel-pi
```
Expected: `docker-proxy`, `wg-easy` (recreated in place — same container name as the old `wg-easy-pi` produced), `glances`, `dozzle`, `autoheal` all deploy. The pre_task toposort runs docker-proxy first.

- [ ] **Step 2: Verify containers are healthy**

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}'
```
Expected: `docker-proxy`, `docker-proxy-lifecycle`, `wg-easy`, `glances`, `dozzle`, `autoheal` present and `healthy` (wg-easy/glances/dozzle have healthchecks; the proxies have theirs). If `dozzle` is `unhealthy`, inspect the healthcheck:
```bash
docker inspect --format '{{json .State.Health}}' dozzle
docker logs dozzle | tail -30
```
If `/dozzle healthcheck` isn't the right path for this image, swap the healthcheck `test` for `["CMD", "wget", "-qO", "/dev/null", "http://127.0.0.1:8080/healthcheck"]` in the dozzle template, redeploy `--tags dozzle`, and update the role's commit.

- [ ] **Step 3: Verify exposure is LAN-bound and read-only**

```bash
# UIs answer on the Pi's LAN IP only:
curl -sS -o /dev/null -w '%{http_code}\n' http://10.0.0.139:61208/   # glances
curl -sS -o /dev/null -w '%{http_code}\n' http://10.0.0.139:8080/    # dozzle
curl -sS -o /dev/null -w '%{http_code}\n' http://10.0.0.139:51821/   # wg-easy UI
# WireGuard UDP listener published:
docker port wg-easy
# wg interface up:
docker exec wg-easy wg show
# dozzle is actually reading via the proxy (no raw socket mount):
docker inspect dozzle --format '{{json .HostConfig.Binds}}'
```
Expected: HTTP codes are non-000 (a `200`/`302`/`401` means the UI is up); `docker port wg-easy` shows `51822/udp`; `wg show` lists an interface; dozzle `Binds` is `null` (no `/var/run/docker.sock`). Confirm the UIs do **not** answer on a non-LAN interface (they're bound to `10.0.0.139`).

- [ ] **Step 4: Decommission Portainer (manual — Ansible no longer manages it)**

```bash
docker rm -f portainer docker-proxy-portainer
docker network rm portainer_internal 2>/dev/null || true
```
Expected: the two containers are removed; the `portainer_internal` network is removed if nothing else references it (the `|| true` tolerates "has active endpoints" if removal must wait — re-run after the containers are gone). Confirm:
```bash
docker ps -a --filter 'name=portainer' --format '{{.Names}}'
```
Expected: empty output.

- [ ] **Step 5: Verify autoheal sees the lifecycle proxy**

```bash
docker logs autoheal | tail -20
```
Expected: autoheal is monitoring (no connection errors to `docker-proxy-lifecycle:2375`).

- [ ] **Step 6: Final report**

Confirm and report: 5 managed containers healthy, UIs LAN-bound only, WireGuard UDP `51822` published and interface up, Portainer + its socket proxy removed. Do **not** report the Pi stack as final until Portainer is actually removed (Step 4) — until then it's only unmanaged.

---

## Notes for the implementer

- **Commit directly to `master`** (project convention — no feature branches unless asked).
- **`containers/` is read-only** — never edit the deployed compose files; only the role
  templates under `ansible/roles/containers/*/templates/`.
- **The validator is the template test.** Any time you touch a `.j2` or a host_vars file,
  re-run `uv run python scripts/validate_compose_templates.py` and expect `0 failure(s).`
- **Whitespace failures** almost always trace to the `{% if %}` / `{%- endif %}` trims in
  `expose.yml.j2` or the wg-easy inline conditional. The validator dumps the rendered text
  with line numbers — read it.
- **Pi deploys run on the Pi** (`ansible_connection=local`); local validation (validator,
  pytest, prek) runs on your dev host.
