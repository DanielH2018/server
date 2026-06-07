# Idempotent deploys via conditional container recreate

- **Date:** 2026-06-07
- **Status:** Approved (design) — pending spec review
- **Author:** Daniel (with Claude)
- **Scope:** `ansible/roles/containers/common/tasks/docker_deploy.yml` + 10 config-mounting roles

## Problem

`common/tasks/docker_deploy.yml` runs `docker_compose_v2` with `recreate: always`.
This is intentional today: it is the de-facto "restart on config change" mechanism for
the roles that bind-mount an Ansible-templated config file. Editing such a file does **not**
change the Compose service definition (its config-hash), so `recreate: auto` (the module
default) would leave the container running with stale config — the edit silently wouldn't
apply.

The cost: **every** `deploy.yml` run recreates **every** targeted container even when
nothing changed. A tagless full deploy bounces the whole fleet (~42 containers) for no
reason. The deploy is not idempotent.

## Goal

Make deploys idempotent: a no-op `deploy.yml` recreates **nothing**; editing one config
recreates **only** that service. Preserve the "config edit takes effect" guarantee.

## Approach (chosen: A — conditional recreate via Ansible change-detection)

Ansible's `template`/`copy` modules already report `changed` when a destination file's
content changes. Drive the recreate decision off that signal instead of forcing `always`.

Alternatives considered and rejected:
- **B — per-service config-hash label.** Bake a sha256 of the rendered config into a
  Compose label so Compose's own `recreate: auto` notices it. Equally correct but touches
  ~11 compose templates + a hashing task; the hash is visible in `docker inspect` and works
  on a manual `docker compose up`. More machinery than needed for an always-deploy-via-Ansible
  homelab.
- **C — notify/handlers.** The repo's `include_role`-in-a-loop deploy architecture
  (`deploy.yml` loops roles via the toposort) conflicts with handler flushing. Rejected.

## Mechanism

### 1. `docker_deploy.yml` becomes conditional

```yaml
recreate: "{{ 'always' if container_config_changed | default(false) | bool else 'auto' }}"
```

- Config-mounting roles pass `container_config_changed: true|false` reflecting whether any
  of their bind-mounted config files changed this run.
- Every other role passes nothing → default `false` → `auto`. These services stop bouncing
  on unchanged deploys (the idempotency win) and still recreate correctly on image or
  compose-file changes (see "Stays correct automatically").

### 2. The flag is an `include_role` var, NOT a `set_fact`

This is the load-bearing correctness detail.

`set_fact` values **persist across loop iterations** on the same host. `deploy.yml`
deploys all roles in one play via a single `include_role` loop, so a fact set in role X
would leak into role Y and wrongly force Y's recreate. Passing the flag as a `vars:`
parameter on the `include_role: docker_deploy` call scopes it to that single invocation —
no cross-role bleed. Registered task results (`_cfg_*`) may persist, but they are only ever
*referenced* by the role that just set them, at the moment it builds the `vars:` expression.

```yaml
- name: Template prometheus config
  ansible.builtin.template:
    src: prometheus.yml.j2
    dest: "/home/{{ sys_user }}/server/containers/prometheus/prometheus.yml"
  register: _cfg_prometheus

- name: Deploy
  ansible.builtin.include_role:
    name: common
    tasks_from: docker_deploy.yml
  vars:
    container_config_changed: "{{ _cfg_prometheus is changed }}"
```

For roles with several config tasks, OR the registers:
`container_config_changed: "{{ (_cfg_a is changed) or (_cfg_b is changed) }}"`.
For a single looped config task, its top-level `.changed` is already true if any loop item
changed — use it directly.

## Per-role wiring

Only roles that (a) use `common/docker_deploy` AND (b) template/copy a **bind-mounted**
config file need wiring. Host-level files (systemd units, udev rules, `/etc/resolv.conf`)
are excluded — they don't belong to the container and are handled by their own tasks.

| Role | Config tasks to register (bind-mounted) | Notes |
|------|------------------------------------------|-------|
| authelia | `configuration.yml.j2`, `users_database.yml.j2` | Cert/key material is generated separately; not a recreate trigger. |
| traefik | `config.yml.j2`, `traefik.yml.j2`, `crowdsec-acquis.yaml.j2`, `crowdsec-discord.yaml.j2`, copies `crowdsec-whitelist.yaml`, `crowdsec-profiles.yaml` | Exclude `docker-user-rules.sh/.service` (host-level systemd/iptables). |
| homepage | the looped config `template` task (`settings/services/widgets/bookmarks/docker/custom.css`) | Exclude the icons `copy` — static assets served at request time from the mounted dir; no restart needed. |
| grafana | `datasources.yml.j2`, `dashboards.yml.j2`, `dashboards/` copy, `loki-config.yml.j2`, `promtail-config.yml.j2` | Grafana reloads provisioning on restart. |
| prometheus | `prometheus.yml.j2` | The scrape config. |
| janitorr | `application.yml.j2` | JVM app. |
| livesync | `local.ini.j2` | CouchDB config. |
| peanut | the looped NUT config `template` task + `peanut-settings.yml.j2` | Dockerfile/entrypoint are build context → `build: always` + image-change auto-recreate handles them. Exclude udev rules (host-level). |
| recyclarr | `recyclarr.yml.j2` | — |
| kopia | `entrypoint.sh.j2` (the bind-mounted container entrypoint) | `kopiaignore.j2` excluded — read at snapshot time (data, not container config). Not in the original list; found during a completeness sweep. |

## Out of scope / special cases

- **pihole** — does not use `common/docker_deploy`. It renders its own `docker-compose.yml`
  and runs a deliberate `state: absent` → `state: present` DNS-bootstrap sequence (stop
  systemd-resolved, fallback `/etc/resolv.conf`, deploy, wait healthy, repoint resolv.conf)
  every run. Making *that* idempotent is a separate, careful task (highest-risk LAN-DNS
  service). **Untouched by this change.**
- **qbittorrent** — uses `docker_deploy` but templates no bind-mounted config (`wg0.conf`
  template is commented out; `qBittorrent.conf` is stateful/self-managed). It passes nothing
  → inherits `auto`. This is *safer*: it avoids needlessly recreating qBittorrent (whose
  `network_mode: service:wireguard` netns is resolved only at start), and its post-deploy
  wg0-bind task is already idempotent (`changed_when: false`).

## Stays correct automatically (no extra work)

- **Image rebuilds** — `build: always` still rebuilds each run; a *changed* image triggers
  `recreate: auto` natively. An unchanged rebuild yields an identical image ID → no recreate.
- **Compose-file edits** — changing a `docker-compose.yml.j2` (e.g. the healthcheck-macro
  migration) changes Compose's own config-hash → `auto` recreates that service.
- Only the **bind-mounted external config** case is invisible to `auto` — exactly what the
  conditional fills.

## Edge cases & risks

- **Missed trigger:** if a role bind-mounts a config file that isn't registered into the
  flag, an edit won't recreate the container. Mitigation: the per-role table above is
  exhaustive for current roles; new config-mounting roles must wire the flag (add to the
  "Adding a New Container Service" checklist / common CLAUDE.md).
- **Check mode:** `template` reports `changed` in `--check` without writing; the recreate
  decision still evaluates correctly for a dry-run preview.
- **First deploy of a service:** config file doesn't exist → `template` reports `changed`
  → `recreate: always` → container created fresh. Correct.
- **`build: always` cost** is unchanged (separate from recreate); not addressed here.

## Verification plan (requires a live run)

1. **Idempotency:** `ansible-playbook ansible/deploy.yml` twice. Second run: the 10 wired
   roles report `container_config_changed = false` and `docker_compose_v2` reports no
   recreated containers; non-config roles likewise show no recreate.
2. **Targeted recreate:** edit one config (e.g. `prometheus.yml.j2`), deploy with
   `--tags prometheus`. Only prometheus recreates; node-exporter/cadvisor do not (their
   defs unchanged).
3. **Image change still recreates:** confirm a service with a new image tag recreates under
   `auto`.
4. **Preview:** `ansible-playbook ... --tags <svc> --check --diff` before applying.
5. `ansible-lint` clean.

## Rollback

Single-line revert of the `recreate:` expression in `docker_deploy.yml` back to
`always` restores prior behavior; the per-role `register:`/`vars:` additions become inert
(harmless) if the conditional is reverted.

## Implementation outline (writing-plans will expand)

1. Edit `common/tasks/docker_deploy.yml` — conditional `recreate`, update the long comment
   to document the new mechanism.
2. Wire the 10 roles (register config tasks + pass `common_config_changed`).
3. Update docs: `common/CLAUDE.md` and the root "Adding a New Container Service" note so new
   config-mounting roles wire the flag.
4. Verify per the plan above.
