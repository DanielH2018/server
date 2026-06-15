# Connect APC UPS to Home Assistant via NUT — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing `peanut`/NUT `upsd` reachable by Home Assistant and give it a read-only credential, so HA's native Network UPS Tools integration can monitor the APC BR1500MS2.

**Architecture:** A new shared external Docker network `ups` (isolation net, mirroring `kopia`/`lifecycle`) is joined by only the `nut` sidecar and `home-assistant`, letting HA reach `nut:3493` without exposing `upsd` on the busy `apps` network. A dedicated read-only `homeassistant` NUT user (no `upsmon` role, no `instcmds`/`actions`) lets HA read UPS variables but never trigger shutdown. The HA integration itself is UI-config-flow only, so the final step is a one-time manual UI action.

**Tech Stack:** Ansible (community.docker), Docker Compose (Jinja2 templates + shared macros), NUT (`upsd`/`upsd.users`), SOPS/age secrets.

**Spec:** `docs/superpowers/specs/2026-06-15-home-assistant-ups-nut-design.md`

---

## File Structure

- `ansible/roles/setup/docker_install/tasks/main.yml` — add `ups` to the network-creation loop.
- `ansible/roles/containers/peanut/templates/docker-compose.yml.j2` — attach `nut` to `ups`; declare `ups` external.
- `ansible/inventory/host_vars/daniel-server.yml` — add `ups` to `home-assistant`'s networks (after `apps`).
- `ansible/roles/containers/peanut/templates/upsd.users.j2` — add read-only `homeassistant` user.
- `ansible/vars/secrets.yml` — add `nut_ha_password` (SOPS-encrypted).
- `ansible/secret_rotation.yml` — registry entry (auto-written by `secret_rotation.py sync`).

---

## Task 1: Network reachability (Home Assistant ↔ nut)

Three coupled edits that establish the `ups` isolation network and attach both endpoints. Committed together — individually they are inert, and CI only renders templates (no Docker), so a partial state is safe but meaningless.

**Files:**
- Modify: `ansible/roles/setup/docker_install/tasks/main.yml`
- Modify: `ansible/roles/containers/peanut/templates/docker-compose.yml.j2`
- Modify: `ansible/inventory/host_vars/daniel-server.yml`

- [ ] **Step 1: Add the `ups` network to the creation loop**

In `ansible/roles/setup/docker_install/tasks/main.yml`, the `Create Docker networks` loop currently ends:

```yaml
    - lifecycle  # private net: Watchtower + Autoheal <-> docker-proxy-lifecycle only
    - kopia      # isolation net: Kopia <-> Traefik only (unauthenticated repo not reachable by other apps)
  become: true
```

Add `ups` after `kopia`:

```yaml
    - lifecycle  # private net: Watchtower + Autoheal <-> docker-proxy-lifecycle only
    - kopia      # isolation net: Kopia <-> Traefik only (unauthenticated repo not reachable by other apps)
    - ups        # isolation net: NUT <-> Home Assistant only (UPS state, kept off apps)
  become: true
```

- [ ] **Step 2: Attach the `nut` service to `ups`**

In `ansible/roles/containers/peanut/templates/docker-compose.yml.j2`, the `nut` service's networks block currently reads:

```yaml
    networks:
      - internal
      # Plain bridge ONLY for the loopback publish below — `internal: true` networks
      # cannot publish ports. No other container joins it.
      - nut_host
```

Add `ups`:

```yaml
    networks:
      - internal
      # Plain bridge ONLY for the loopback publish below — `internal: true` networks
      # cannot publish ports. No other container joins it.
      - nut_host
      # Shared isolation net so Home Assistant's NUT integration can reach upsd:3493.
      - ups
```

- [ ] **Step 3: Declare `ups` as external at the top level**

In the same file, the top-level networks stanza currently reads:

```yaml
{{ external_networks() }}
  internal:
    internal: true
  nut_host:
    driver: bridge
```

Add the `ups` external declaration:

```yaml
{{ external_networks() }}
  internal:
    internal: true
  nut_host:
    driver: bridge
  ups:
    external: true
```

- [ ] **Step 4: Join Home Assistant to `ups` (apps stays first)**

In `ansible/inventory/host_vars/daniel-server.yml`, the `home-assistant` entry currently reads:

```yaml
    use_authelia: false
    networks:
      - apps
  # Organization: Development
```

Add `ups` *after* `apps` (order matters — the Traefik label binds to `networks[0]`):

```yaml
    use_authelia: false
    networks:
      - apps
      - ups  # reach the nut sidecar's upsd:3493 for the Home Assistant NUT integration
  # Organization: Development
```

- [ ] **Step 5: Validate the rendered templates**

Run:
```bash
uv run python scripts/validate_compose_templates.py
```
Expected: exits 0 with no `render error` / YAML errors. (Unresolved `nut_ha_password` is fine — the validator stubs SOPS secrets.) This catches the Ansible-specific whitespace/indent bugs a vanilla Jinja2 harness misses.

- [ ] **Step 6: Confirm ordering and presence by eye**

Run:
```bash
grep -n -A4 "name: home-assistant" ansible/inventory/host_vars/daniel-server.yml
grep -n "ups" ansible/roles/containers/peanut/templates/docker-compose.yml.j2 ansible/roles/setup/docker_install/tasks/main.yml
```
Expected: HA lists `- apps` immediately before `- ups`; `peanut` template shows both the `- ups` attachment and the `ups:`/`external: true` declaration; the loop shows the `ups` entry.

- [ ] **Step 7: Commit**

```bash
git add ansible/roles/setup/docker_install/tasks/main.yml \
        ansible/roles/containers/peanut/templates/docker-compose.yml.j2 \
        ansible/inventory/host_vars/daniel-server.yml
git commit -m "$(cat <<'EOF'
home-assistant: add ups isolation net so HA can reach the NUT upsd

New shared external network `ups` (NUT <-> Home Assistant only, mirroring the
kopia/lifecycle isolation nets). The nut sidecar joins it alongside its existing
internal/nut_host nets; home-assistant joins it after apps (apps stays networks[0]
so its Traefik label is unchanged). Reachability only — credential follows.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Read-only NUT credential for Home Assistant

Adds a dedicated `homeassistant` NUT user (read-only) plus its SOPS secret and rotation-registry entry. Editing `upsd.users.j2` flows through the role's existing `peanut_cfg_nut is changed` → `common_config_changed` wiring, so `nut` recreates on the next deploy.

**Files:**
- Modify: `ansible/roles/containers/peanut/templates/upsd.users.j2`
- Modify: `ansible/vars/secrets.yml` (via `sops set`)
- Modify: `ansible/secret_rotation.yml` (via `secret_rotation.py sync`)

- [ ] **Step 1: Add the read-only user to the template**

`ansible/roles/containers/peanut/templates/upsd.users.j2` currently is:

```jinja
[upsmon]
  password = {{ nut_monitor_password }}
  upsmon primary
```

Append the `homeassistant` user (no `upsmon` role line, no `instcmds`/`actions` ⇒ read-only):

```jinja
[upsmon]
  password = {{ nut_monitor_password }}
  upsmon primary

# Read-only login for Home Assistant's NUT integration: it can read every UPS
# variable but holds no upsmon role and no instcmds/actions, so it cannot raise
# FSD or run instant commands. Reaches upsd over the `ups` isolation network.
[homeassistant]
  password = {{ nut_ha_password }}
```

- [ ] **Step 2: Create the SOPS secret (non-interactive, value never printed)**

Run (requires the age key, present on daniel-server; generates a 48-char hex password — alphanumeric, safe for the `upsd.users` parser):
```bash
sops set ansible/vars/secrets.yml '["nut_ha_password"]' "\"$(openssl rand -hex 24)\""
```
Expected: exit 0, no output. (Command substitution will trigger a permission prompt — that's expected for a write.)

- [ ] **Step 3: Reconcile the rotation registry**

Run:
```bash
uv run python scripts/secret_rotation.py sync
```
Expected: output reports `nut_ha_password` added to the registry (auto-classified, staggered due-date); no `stale` warnings about it.

- [ ] **Step 4: Verify both files reference the secret (names only, no values)**

Run:
```bash
grep -n "nut_ha_password" ansible/vars/secrets.yml ansible/secret_rotation.yml
```
Expected: `secrets.yml` shows `nut_ha_password: ENC[AES256_GCM,...]` (encrypted); `secret_rotation.yml` shows a `nut_ha_password:` entry with `tier:` and `last_rotated:`.

- [ ] **Step 5: Re-validate templates**

Run:
```bash
uv run python scripts/validate_compose_templates.py
```
Expected: exits 0 (the new template line renders; `nut_ha_password` stubs cleanly).

- [ ] **Step 6: Commit**

```bash
git add ansible/roles/containers/peanut/templates/upsd.users.j2 \
        ansible/vars/secrets.yml \
        ansible/secret_rotation.yml
git commit -m "$(cat <<'EOF'
peanut: add read-only homeassistant NUT user for HA integration

Dedicated upsd.users login (no upsmon role, no instcmds/actions) so Home
Assistant can read UPS variables but never trigger FSD/shutdown. New SOPS
secret nut_ha_password + rotation-registry entry. The upsd.users change flows
through peanut_cfg_nut -> common_config_changed, recreating nut on deploy.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Create the network, deploy, and verify (production-affecting)

Creates the `ups` network on the host, then redeploys the two services. The network must exist before either container is recreated.

**Files:** none (runtime only).

- [ ] **Step 1: Create the `ups` network (idempotent)**

Run:
```bash
uv run ansible-playbook ansible/initial_setup.yml --tags docker-networks
```
Expected: the `Create Docker networks` task reports `ok`/`changed` and `ups` now exists. Confirm:
```bash
docker network ls --filter name=ups
```
Expected: a row for `ups`.
(Fallback if you only want the one network without the setup play: `docker network create ups`.)

- [ ] **Step 2: Deploy peanut (adds user + ups attachment, recreates nut)**

Run:
```bash
uv run ansible-playbook ansible/deploy.yml --tags peanut
```
Expected: completes without errors; the `nut` (and `peanut`) container is recreated (config change detected).

- [ ] **Step 3: Confirm existing NUT clients still work (regression)**

Run:
```bash
docker exec nut upsc apc-ups@localhost ups.status
```
Expected: prints a status such as `OL` (online). Confirms the USB link, `upsd`, and the host-side/PeaNUT consumers are unaffected.

- [ ] **Step 4: Deploy Home Assistant (joins ups)**

Run:
```bash
uv run ansible-playbook ansible/deploy.yml --tags home-assistant
```
Expected: completes without errors; `home-assistant` recreated and now attached to `apps` + `ups`.

- [ ] **Step 5: Verify HA can reach upsd**

Run:
```bash
docker exec home-assistant getent hosts nut
docker exec home-assistant python3 -c "import socket; socket.create_connection(('nut',3493),5); print('nut:3493 reachable')"
```
Expected: `nut` resolves to a `172.x` address on the `ups` subnet, and the second command prints `nut:3493 reachable`. (HA's LSIO image ships Python 3; if absent, substitute any TCP check.)

---

## Task 4: Configure the integration in Home Assistant (manual, one-time)

The NUT integration is UI-config-flow only and cannot be templated. This step is performed by the user in the HA web UI.

- [ ] **Step 1: Retrieve the password to enter**

Run (prints the value so it can be typed into the UI — do this in a private terminal):
```bash
sops -d --extract '["nut_ha_password"]' ansible/vars/secrets.yml
```

- [ ] **Step 2: Add the integration**

In Home Assistant: **Settings → Devices & Services → Add Integration → Network UPS Tools (NUT)**. Enter:
- Host: `nut`
- Port: `3493`
- Username: `homeassistant`
- Password: *(value from Step 1)*

Then select the `apc-ups` device when prompted.

- [ ] **Step 3: Confirm sensors**

Expected: HA creates entities for the UPS (battery charge %, load %, runtime, input voltage, status). Spot-check that battery charge matches what PeaNUT shows.

---

## Self-Review Notes

- **Spec coverage:** every spec change (1 network, 2 nut-attach, 3 HA-attach, 4 read-only user, 5 secret, deploy sequence, manual UI step, verification) maps to a task above.
- **Ordering risk** from the spec (apps must stay `networks[0]`) is enforced in Task 1 Step 4 + checked in Step 6.
- **Deploy-order risk** (network before recreate) is enforced by Task 3 Step 1 preceding Steps 2/4.
- **Rollback:** revert the Task 1/2 commits and `docker network rm ups` (after containers detach) + remove the `nut_ha_password` secret; no existing UPS behaviour (host shutdown chain, PeaNUT) is touched.
