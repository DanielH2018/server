---
name: new-container
description: Add a new DOCKER service for daniel-pi — a `pi-compose` service in the Pi's host package, which is a `compose_services` entry plus a service tree under hosts/components/pi-compose/services/. New cluster services go to ansible/roles/k8s/ instead; this skill does not scaffold those.
allowed-tools: Read, Write, Edit, Glob, Bash, WebFetch, WebSearch
---

> **STOP — check the target host first.** Since the k3s migration completed (2026-08-14),
> `daniel-server` and `daniel-box` have **no Docker at all**. This skill applies **only to
> `daniel-pi`** (LAN-only utilities, WireGuard).
>
> For anything on the cluster — which is where nearly every new service belongs — do NOT use
> this skill. Use the `new-k8s-service` skill. See repo-root `CLAUDE.md` → "Adding a New
> Service".

The Pi's Docker services are one Compose project, `homelab`, which the `homelab-daniel-pi`
host package installs and `homelab-compose.service` runs. There is no Ansible role and no
`deploy.sh` path for them: H5.12 deleted `ansible/roles/containers/`. The design is
`docs/target-architecture/host/h5-pi-compose.md`, and the package mechanics are
`docs/host-packages.md`.

**First, ask:** is this an existing image pulled from a registry, or a custom build? Every Pi
service pulls a digest-pinned image, and no service has built one through the package. For a
custom build, propose a k3s workload instead, whose image the cluster's image-builder builds.

**If the service is an existing well-known project**, start from its official
`docker-compose.yml` (fetch the raw file, e.g. from `raw.githubusercontent.com`) and adapt it
to the conventions below.

Gather from the user (or infer from context):
1. Service name (lowercase, hyphenated)
2. Docker image, tag and digest
3. Ports to expose (if any)
4. Directories the service must persist
5. Any secret it reads
6. Confirm the target host is `daniel-pi`. If the answer is `daniel-server` or `daniel-box`,
   stop and use `new-k8s-service`.

Then make these changes. `hosts/components/pi-compose/services/docker-proxy/` is the worked
example of a plain service, and `alloy/` of one with a config file.

**`ansible/inventory/host_vars/daniel-pi.yml` → `compose_services`**
- Add `name`, `use_authelia: false`, `networks`, and `port` or `udp_port` if it publishes one.
  The template reads them as `container_item`.
- Never add the service to `containers_list`. That key stays empty, and
  `scripts/validate/compose_templates.py` fails on a Docker entry in it.

**`hosts/components/pi-compose/services/<name>/compose.yaml.j2`**
- Open with a `{#- … -#}` header that holds what a role's `CLAUDE.md` would have: what the
  service is for, and every non-obvious choice in the file.
- A service tree has no `ansible/templates/` on its search path, so the shared macros are not
  available. Write the `networks:`, `deploy.resources` and label blocks out, as `autoheal/`
  does.
- Pin the image as `tag@sha256:<digest>`; `compose_templates.py` refuses a bare tag.
- Set `restart: unless-stopped`, `cap_drop: [ALL]` and
  `security_opt: [no-new-privileges:true]`; `compose_templates.py` refuses a service without
  the last two unless it is allowlisted with a reason.
- Publish a UI port bound to `{{ server_ip }}`, the Pi's LAN IP, never `0.0.0.0`. The Pi has
  no Traefik in front of it, so a service carries no routing labels.
- Include a healthcheck if the image supports one. autoheal restarts any container whose
  healthcheck reports `unhealthy`, so the check must fail only when the service is broken.
- **A bind-mounted config file needs a hash label.** `up -d` recreates a container only when
  its compose definition changes, and Compose cannot see a mounted file's content. Ship the
  config as a package file beside the compose file and carry its sha256 in a label, as alloy's
  `homelab.config-sha256` does; without it an edit sits inert while the install reports green.

**`hosts/components/pi-compose/tmpfiles.d/homelab-compose.conf`**
- Add one `d` line for the service's project directory, `/home/ubuntu/server/containers/<name>`,
  and one for every relative bind source. The bind-source census fails a relative bind source
  with no line.

**A secret**
- Reference it in the compose file as `${<key>}`, add the key to `keys:` on the `compose.env`
  output in `hosts/components/pi-compose/component.yml`, and give the include `env_file:`.
- **Operator-only:** run `host_secrets.py sync daniel-pi` and commit the Pi's SOPS file. An
  agent never decrypts a secret.

**Check before committing**
- `uv run python scripts/validate/compose_templates.py -v` renders every `compose_services`
  entry and runs the compose checks.
- `uv run pytest hosts/lib/tests ansible/tests/setup/test_pi_service_lists_census.py` runs
  the package and service-list censuses.

**Landing.** Merge the PR. The builder publishes the next `homelab-daniel-pi` version, the Pi's
updater installs it, and `homelab-compose.service` brings the service up. `land.sh` waits for
the Pi's applied SHA. Afterwards, check the container with
`uv run python scripts/diagnostics/probe.py health <name> --docker`.
