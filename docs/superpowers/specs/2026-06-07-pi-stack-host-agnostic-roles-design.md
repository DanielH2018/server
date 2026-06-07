# Pi Stack — Host-Agnostic Roles Design

**Date:** 2026-06-07
**Status:** Approved (design); pending implementation plan
**Scope:** Make the homelab's web-UI container roles host-agnostic so a minimal,
LAN-only utility stack can run on `daniel-pi`, and collapse the two `wg-easy` roles
into one role that serves both hosts.

## Goal

Stand up a **minimal, LAN-only** utility stack on `daniel-pi` for debugging, logging,
and security, and remove the duplication that currently forces per-host role copies.
Except for the WireGuard UDP port, nothing on the Pi is reachable beyond the local
network.

Target Pi stack:

| Service | Category | Notes |
|---------|----------|-------|
| `wg-easy` | VPN (the point of the Pi) | unified role, LAN-bound UI |
| `glances` | debugging | host vitals incl. CPU temp/throttling; read-only |
| `dozzle` | logging | live multi-container log viewer; read-only (NEW role) |
| `docker-proxy` | security | read-only socket proxy for glances+dozzle; lifecycle sub-proxy for autoheal |
| `autoheal` | resilience | restarts unhealthy containers via the restart-only proxy |

**Removed from the Pi:** `portainer` (and its EXEC-capable socket proxy) — replaced by
Dozzle for logs. This gives the Pi **zero root-equivalent Docker exposure**: everything
is read-only except autoheal's restart capability through the isolated `lifecycle` proxy.

## Decisions (from brainstorming)

1. **No Authelia / Traefik on the Pi.** The Pi is LAN-only; its sole externally reachable
   port is WireGuard's UDP listener, which WireGuard authenticates cryptographically on
   its own. Authelia is inert without Traefik forward-auth, and standing up Traefik + certs
   + DNS on the Pi contradicts "minimal." Admin UIs are LAN-bound instead.
2. **Ad-hoc local UIs only.** The Pi does **not** feed or duplicate the main server's
   Prometheus/Loki/Grafana/uptime-kuma stack. No metrics/log shipping, no central pipeline.
3. **Drop Portainer on the Pi, keep Dozzle.** Dozzle (read-only log UI) + Glances (host
   vitals) + SSH/Ansible for management replace Portainer's heavy, EXEC-capable UI.
   Trade-off accepted: no web UI to start/stop/exec/redeploy containers on the Pi.
4. **Exposure abstraction = Approach B** (host `expose_mode` var + shared macro). Chosen
   over "always emit both" (Portainer pattern) because generalizing Portainer's
   *documented break-glass* LAN port to every UI would open un-authenticated LAN access to
   glances/dozzle **on the server**, regressing its "Traefik + Authelia only" posture.
   Chosen over a per-container flag because the whole Pi is uniformly LAN-only (YAGNI).

## Architecture

### The exposure problem

Every web UI must render two ways:

- **Server (`traefik` mode):** no published port; Traefik routes via labels; Authelia
  middleware gates it. This is the existing behavior — preserve byte-for-byte.
- **Pi (`lan` mode):** no Traefik labels; the UI port is published bound to the host's
  LAN IP (`{{ server_ip }}:port`, not `0.0.0.0`), exactly like the current `wg-easy-pi`.

A host-level variable `expose_mode` selects which. Default `traefik` (so the server and
the template validator render unchanged); `lan` on `daniel-pi`.

### New shared macro: `ansible/templates/expose.yml.j2`

Follows the existing shared-macro convention (`traefik.yml.j2`, `networks.yml.j2`,
`autokuma.yml.j2`, `resources.yml.j2`). Imports `labels` from `traefik.yml.j2`.

Two macros, **both invoked at column 0 on their own line** (each bakes in the service-block
indentation; the "off" mode renders to a single harmless blank line, avoiding a
trailing-whitespace violation). Mode is
`container_item.expose_mode | default(expose_mode | default('traefik'))`:

- **`web_ui_labels(internal_port=None)`** — in `traefik` mode emits the existing Traefik
  `labels()` at 6-space indent (container = `hostname|name`, port =
  `internal_port | container_item.port`, network = first of `networks`, authelia =
  `use_authelia`). In `lan` mode emits nothing. Also DRYs up the 4-arg `labels()` unpacking
  that every template currently repeats. Slots into a template's `labels:` block before
  `kuma()`.
- **`web_ui_ports_block(internal_port)`** — in `lan` mode emits a full `ports:` block with
  one item `"{{ server_ip }}:{{ container_item.port }}:{{ internal_port }}/tcp"`. In
  `traefik` mode emits nothing. For services whose only published port is the UI
  (glances, dozzle).

`wg-easy` does **not** use a third macro: it always publishes its WireGuard UDP port, so it
appends the LAN UI port with a small inline `{% if … == 'lan' %}` conditional under its
existing `ports:` block (with `trim_blocks`, the "off" mode renders nothing — no blank line).

**Whitespace:** the macros use `{% if %}` (relying on `trim_blocks` to eat the tag's
newline) and `{%- endif %}` to strip the content's trailing newline, with `if`/`endif` at
column 0 so no stray indent is injected. Validate with
`scripts/validate_compose_templates.py` — the vanilla-Jinja2 harness misses Ansible's
whitespace behavior (see `compose-healthcheck-dollar-escaping` and the validator memo).
Also add `expose` to the validator's `files` regex in `prek.toml` so edits to the macro
re-trigger validation.

### Component changes

#### 1. `wg-easy` — unify the two roles

- Rewrite `ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2` to be
  host-agnostic; **delete the entire `wg-easy-pi` role** (`tasks`, `templates`, `meta`,
  `CLAUDE.md`).
- Import `web_ui_labels` from `expose.yml.j2`.
- `ports:` block always publishes the UDP listener
  `"{{ container_item.udp_port }}:{{ container_item.udp_port }}/udp"`, then an inline
  `{% if … == 'lan' %}` appends the LAN UI item
  `"{{ server_ip }}:{{ container_item.port }}:51821/tcp"` (Pi only).
- `environment:` adds `WG_PORT={{ container_item.udp_port }}` (explicit on both hosts).
  `WG_HOST=wireguard.{{ domain }}` unchanged (resolves per host domain).
- `labels:` block: `{{ web_ui_labels('51821') }}` (column 0) then
  `{{ kuma(container_item.name) }}`.
- Unchanged: `cap_drop: ALL` + `cap_add: [NET_ADMIN, NET_RAW]`, sysctls
  (`ip_forward`, `src_valid_mark`), `security_opt`, native image healthcheck (no compose
  `healthcheck:`), `service_networks()`/`external_networks()`, `resources('0.50','256M','0.05','32M')`.
- `meta/deps.yml`: **keep `role_deps: [traefik, authelia]` unchanged** (no per-host deps
  file). **Verified safe:** `meta/deps.yml` declares the full dependency contract, and each
  host's `containers_list` decides which deps are active. The toposort resolver
  (`ansible/filter_plugins/toposort.py`) silently ignores any dep absent from the host's
  list — `toposort_containers` line 65 (`if dep in name_to_idx`), and `dep_closure` /
  `expand_with_deps` lines 101/127 (`if dep in name_to_obj`). On the server traefik+authelia
  are present → wg-easy sorts after them; on the Pi they're absent → dropped, no error.
  Regression-pinned by `test_dep_absent_from_list_is_ignored` in
  `ansible/tests/test_toposort.py`. The deleted `wg-easy-pi` role's `role_deps: []` is
  therefore unnecessary — the unified role's deps "just work" on both hosts.

#### 2. `dozzle` — new role

`ansible/roles/containers/dozzle/` with `tasks/main.yml`, `templates/docker-compose.yml.j2`,
`meta/main.yml`, `meta/deps.yml`, `CLAUDE.md`. Use the `/new-container` skeleton + shared
macros.

- Image `amir20/dozzle:latest` (non-critical tier; `:latest` + watchtower acceptable per
  homelab norm — not in the pinned critical/stateful tier).
- `environment: DOCKER_HOST=tcp://docker-proxy:2375` (read-only proxy; Dozzle issues GET
  `/containers`, `/containers/{id}/logs`, `/events` — all permitted under `CONTAINERS=1` +
  `EVENTS=1` with `POST=0`). **Implementation must verify Dozzle works against the
  read-only proxy on deploy** (`docker logs dozzle`); if it needs more, do NOT raise the
  shared read-only proxy's permissions — give Dozzle a dedicated read-only proxy instead.
- Internal port `8080`. `web_ui_labels('8080')` + `web_ui_ports_block('8080')`.
- `cap_drop: ALL`, `security_opt: no-new-privileges:true` (Go binary, read-only — verify
  on deploy). Healthcheck via Dozzle's built-in (`/healthcheck` endpoint or
  `dozzle healthcheck` — verify the image's mechanism), else a `wget` spider probe.
- `resources('0.25','128M','0.05','16M')` (Pi-sized; tune after observing).
- `kuma(container_item.name)`. `meta/deps.yml`: depends on `docker-proxy`.

#### 3. `docker-proxy` — add to Pi, no template change

Already host-agnostic. Add to `daniel-pi.yml` `containers_list` on `[proxy]` (the
read-only `docker-proxy` service joins `proxy`, reachable by glances+dozzle; the
`docker-proxy-lifecycle` sub-proxy hardcodes its own `lifecycle` net in the template).

#### 4. `autoheal` — add to Pi, no template change

Already host-agnostic. Add on `[lifecycle]`; it talks to `docker-proxy-lifecycle`.
`meta/deps.yml` already declares the docker-proxy dependency → toposort orders it after
docker-proxy.

#### 5. `portainer` — remove from Pi

Remove the `portainer` entry from `daniel-pi.yml`. The role itself is unchanged (still
serves the server). **Manual decommission required** (Ansible only manages *listed*
containers — removing the entry stops management, not the running container):

```bash
# on daniel-pi
docker rm -f portainer docker-proxy-portainer
docker network rm portainer_internal   # if no longer referenced
```

#### 6. `glances` — make UI exposure host-agnostic

- Replace the direct `labels(...)` call with `{{ web_ui_labels('61208') }}` and add
  `{{ web_ui_ports_block('61208') }}` (no-op on the server, LAN-binds on the Pi).
- Server behavior must stay byte-identical (Traefik + Authelia, no published port).
- `DOCKER_HOST=tcp://docker-proxy:2375` already present; on the Pi glances joins `proxy`
  alongside docker-proxy, so the name resolves.

### Data / config changes

- `ansible/inventory/group_vars/all.yml`: add `expose_mode: traefik`.
- `ansible/inventory/host_vars/daniel-server.yml`: add `udp_port: 51820` to the existing
  `wg-easy` entry (server keeps `port: 51821`, `networks: [apps]`, `use_authelia: true`).
- `ansible/inventory/host_vars/daniel-pi.yml`: set `expose_mode: lan`; replace the
  `portainer` + `wg-easy-pi` entries with:

  ```yaml
  containers_list:
    - name: docker-proxy
      use_authelia: false
      networks: [proxy]
      tags: [docker-proxy]
    - name: wg-easy
      port: 51821
      udp_port: 51822
      use_authelia: false
      networks: [proxy]
      tags: [wg-easy]
    - name: glances
      port: 61208
      use_authelia: false
      networks: [proxy]
      tags: [glances]
    - name: dozzle
      port: 8080
      use_authelia: false
      networks: [proxy]
      tags: [dozzle]
    - name: autoheal
      use_authelia: false
      networks: [lifecycle]
      tags: [autoheal]
  ```

- `ansible/deploy.yml` already loops `containers_list` and includes roles by name; the new
  `dozzle` role needs no deploy.yml change beyond existing per-host listing. Confirm the
  role is discoverable (it lives under `roles/containers/dozzle`, matching the loop's
  `include_role: name: "{{ container_item.name }}"`).

## Error handling / edge cases

- **`expose_mode` default** in `all.yml` guarantees the server *and* the validator render
  in `traefik` mode unchanged.
- **wg-easy deps on the Pi** (traefik/authelia absent): **resolved.** The toposort/dep
  filters silently ignore deps not in the host's `containers_list` (pinned by
  `test_dep_absent_from_list_is_ignored`). Keep `role_deps: [traefik, authelia]`; no
  conditional logic needed. See component change #1.
- **Dozzle vs read-only proxy:** verify on deploy; isolate with a dedicated read-only proxy
  rather than loosening the shared one if needed.
- **Portainer is only *unmanaged*, not removed** — manual `docker rm -f` step documented;
  do not report the Pi stack as final until that's done.
- **Same public IP, two WireGuard servers:** server `51820`, Pi `51822` must stay distinct;
  `udp_port` per host enforces this.

## Testing / verification

1. `scripts/validate_compose_templates.py` — renders every host × `containers_list` entry;
   must succeed for both `traefik` and `lan` modes (catches Ansible whitespace bugs).
2. `uv run pytest` — toposort/deploy-ordering + dep filters still green with the new role
   and the wg-easy dep change.
3. `prek run --all-files` — YAML lint, ansible-lint, gitleaks.
4. Dry run: `uv run ansible-playbook ansible/deploy.yml --tags "docker-proxy,wg-easy,glances,dozzle,autoheal" --limit daniel-pi --check`.
5. Deploy to the Pi; verify:
   - `docker ps` — all five containers healthy.
   - UIs reachable on the LAN only: `http://10.0.0.139:51821` (wg-easy),
     `:61208` (glances), `:8080` (dozzle); **not** on `0.0.0.0`/other interfaces.
   - `udp/51822` published; WireGuard interface up (`docker exec wg-easy wg show`).
   - glances + dozzle reading through `docker-proxy` (no raw `docker.sock` mount).
   - autoheal connected to `docker-proxy-lifecycle`.
6. Decommission Portainer on the Pi (manual step above) and confirm it's gone.

## Out of scope

- Deploying Dozzle/Glances on the *server* (role is made host-agnostic but only the Pi
  consumes the new exposure path now).
- Any central metrics/log pipeline for the Pi.
- Pruning the host-agnostic network-creation list (Pi still creates unused `media`/`kopia`
  nets via `docker_install` — harmless, left alone).

## Files touched (summary)

**New:**
- `ansible/templates/expose.yml.j2`
- `ansible/roles/containers/dozzle/{tasks/main.yml,templates/docker-compose.yml.j2,meta/main.yml,meta/deps.yml,CLAUDE.md}`

**Modified:**
- `ansible/inventory/group_vars/all.yml` (+`expose_mode: traefik`)
- `ansible/inventory/host_vars/daniel-server.yml` (+`udp_port: 51820` on wg-easy)
- `ansible/inventory/host_vars/daniel-pi.yml` (new `containers_list`, `expose_mode: lan`)
- `ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2` (unified)
- `ansible/roles/containers/wg-easy/CLAUDE.md` (document both hosts)
- `ansible/roles/containers/glances/templates/docker-compose.yml.j2` (macro-based exposure)
- `ansible/roles/containers/glances/CLAUDE.md` (note host-agnostic)

**Deleted:**
- `ansible/roles/containers/wg-easy-pi/` (entire role)

**Manual (on daniel-pi):**
- `docker rm -f portainer docker-proxy-portainer` (+ `portainer_internal` net)
