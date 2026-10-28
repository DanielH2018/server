# H5 — Pi Compose

Status: PLANNED
Depends on: H3, H4
Unblocks: H7

This slice moves the Pi's four Compose services from Ansible's per-service deploy into one
Compose project. `homelab-daniel-pi` carries that project, and `homelab-compose.service`
starts it. docker-proxy moves first, because autoheal and alloy reach the Docker API through
it. autoheal and alloy follow. wg-easy moves last, after a second access path is proven.
Names and paths follow H3's *Shared names for the host slices*, secret handling follows H4,
and every move follows H3's *host handoff rule*.

## Goal and exit criterion

Afterwards, `docker compose ls` on the Pi lists one project, `homelab`. It runs every
container the rendered project declares: alloy, autoheal, docker-proxy,
docker-proxy-lifecycle and wg-easy. The Pi's `containers_list` in
`ansible/inventory/host_vars/daniel-pi.yml` is empty, and the `ansible/roles/containers/` tree
is gone. Two things leave later (*What stays with Ansible until H7*). The inert Docker play in
`ansible/deploy.yml` goes with that file's last play, in R4 or H7. The empty key goes in H7.
`compose_services` stays in the inventory, which H7 keeps as renderer data.

If D5's crowdsec conversion has landed, H5.14 also moves D5's probe timer on the Pi out of
the setup role `k8s_host_files` and into the Pi package (*Rows H5 takes from D5's
`k8s_host_files`*).

The exit criterion is three observations:

1. The container set on the Pi equals the set the project declares. The names that
   `sudo docker compose -f /usr/lib/homelab/compose/compose.yaml config --services` prints
   equal the names `docker ps` prints with label `com.docker.compose.project=homelab`, and
   each one is running. Every service sets a `container_name` equal to its service name, and
   `--services` prints names only, never an interpolated value. `sudo` is needed because the
   project reads the root-only `compose.env`, so the operator runs this check (*Verification*).
   At `70591c307` that set has 5 names. `docker compose ls` shows `homelab` as the only
   project.
2. A Renovate PR that bumps one Pi image digest merges and reaches the Pi with no step on the
   Pi. `docker inspect` then shows the bumped digest, and `land.sh` reports `settled`. From
   H5.3 on, `settled` includes a passing run of `homelab-compose.service`.
3. A PR that removes a service from the project removes its container. `docker ps -a
   --filter name=<service>` prints nothing after the updater runs.

## Baseline at `70591c307`

- **Four roles, four projects.** `ansible/roles/containers/` holds `alloy`, `autoheal`,
  `docker-proxy` and `wg-easy`, plus the helper role `common`.
    - `common/tasks/docker_deploy.yml` writes
      `/home/ubuntu/server/containers/<name>/docker-compose.yml` at mode 0600.
    - It then runs `community.docker.docker_compose_v2` with `remove_orphans: true`, so each
      role is its own project, named after its directory.
- **The service list is inventory, and its order is the deploy order.**
  `ansible/inventory/host_vars/daniel-pi.yml` lists four `containers_list` entries, in this
  order: `docker-proxy`, `wg-easy` (port 51821, UDP 51822), `autoheal` and `alloy` (port
  12345). The `Deploy Containers` play in `ansible/deploy.yml` deploys them in list order.
  `ansible/tests/deploy/test_platform_filter_real_inventory.py:PI_ORDERING` pins docker-proxy
  ahead of autoheal and alloy, and its `_order_violations` reports an absent name as a
  violation.
- **Five containers run on the Pi.** The four `docker-compose.yml.j2` templates set
  `container_name` on every service: `autoheal`, `wg-easy`, `alloy`, `docker-proxy`,
  `docker-proxy-lifecycle` and `docker-proxy-codeserver`. The last one renders only inside
  `{% if has_code_server %}`. `ansible/inventory/group_vars/all.yml` sets `has_code_server:
  false`, and the Pi does not override it. A service that moves to another project collides
  on its name until the old container goes.
- **Bind mounts are relative, and Ansible creates their sources.** alloy mounts
  `./config/config.alloy` and `./data`, and wg-easy mounts `./config`.
  `common/tasks/setup_dirs.yml` creates each service directory, and alloy's `config` and
  `data`, at mode 0755, owned by the connecting user. alloy runs as `user: "1000:1000"` and
  writes its positions under `./data`.
- **Every service name equals its `container_name`.** The four templates declare the services
  `autoheal`, `wg-easy`, `alloy`, `docker-proxy`, `docker-proxy-lifecycle` and
  `docker-proxy-codeserver`, and each sets `container_name` to its own key. Every `image:`
  line in them carries an `@sha256:` digest.
- **alloy's password file is 0640 on purpose.** `ansible/roles/containers/alloy/tasks/main.yml`
  writes `config.alloy` at `mode: "0640"`, and its comment says the file carries the HTTP
  server's basic-auth password. `ansible/roles/containers/docker-proxy/CLAUDE.md` (*Notable*)
  records that the read-only proxy returns every container's `Env`, which is why that proxy
  left the `apps` network.
- **pi-peer-backup reads wg-easy's data path.**
  `ansible/roles/k8s/pi-peer-backup/defaults/main.yml:pi_peer_backup_src_dir` is
  `/home/{{ sys_user }}/server/containers/wg-easy/config/`.
- **Networks are external.** The `networks.yml.j2` macro declares each network `external:
  true`. The `docker_install` role's install task loop creates `proxy` and `lifecycle`.
- **Three Pi services read three secret keys.**
    - autoheal's template reads `monitor_discord_webhook_url`.
    - wg-easy's template reads `domain` in `WG_HOST`.
    - alloy's `config.alloy.j2` reads `alloy_pi_http_password` and `domain`.
    - `grep -c '^<key>:' ansible/vars/secrets.yml` prints 1 for each of the three keys and 0
      for every other variable the four templates, `config.alloy.j2` and the three macros
      they import read.
    - `ansible/secret_rotation.yml` gives them the tiers `external`, `ignore` and `assisted`.
      None is `auto`.
- **alloy recreates on config change through a flag.** alloy's role passes
  `common_config_changed`, because a config-file edit leaves the compose hash unchanged.
  alloy reads its config once, at startup.
- **Five readers outside the Docker play read the Pi's list, and one module passes it into
  renders.**
    - `ansible/roles/k8s/observability/templates/prometheus.yaml.j2` reads
      `hostvars['daniel-pi'].containers_list | entry_port('alloy')` for the scrape target.
    - `ansible/roles/k8s/monitor-bridge/templates/env-secret.yaml.j2` builds
      `PI_PUBLISHED_PORTS` from the entries that define `port`.
      `ansible/tests/services/test_pi_publishing_containers.py:EXPECTED_PUBLISHERS` pins
      `alloy` on 12345 and `wg-easy` on 51821.
    - `ansible/roles/k8s/artifacts/templates/configmap.yaml.j2` adds the Pi's names to
      `known_services.json` through `default([])`.
    - `scripts/diagnostics/probe_lib/health_docker.py:declared_on_pi` reads only
      `containers_list`.
    - `scripts/infra_map/inventory.py:declared_services` builds each host's declared
      services through `lib.ansible_inventory.containers_entries_in`, which reads only
      `containers_list`. The docstring of `scripts/infra_map/gen_infra_map.py` overlays the
      Pi's `docker ps` onto that declared skeleton so drift is visible, and
      `scripts/infra_map/constants.py:HOST_PLANE` marks daniel-pi as `docker`. A Pi service
      missing from the declared set therefore shows as a running container with no
      declaration. `load_roles` in the same module maps each `container_name` to its role
      by globbing `ansible/roles/containers/*/templates/docker-compose.yml.j2`. It builds
      that path from parts, so the `roles/containers` grep below does not find it.
    - `scripts/lib/render_context.py:_HOSTVARS_KEYS` passes only `server_ip` and
      `containers_list` into a render's `hostvars`.
- **Five more modules call `containers_entries_in`.** `git grep -n containers_entries_in
  -- scripts ':!*/tests/*'` lists eight modules: its definition in
  `scripts/lib/ansible_inventory.py`, the two readers above, and these five, which *The
  service list* sorts into deliberate `containers_list` reads and Pi-role reads the moves
  retire:
    - `scripts/lib/render_guard.py:containers_entries` and `containers_entries_at`, which
      derive the Ansible deploy tags that exist. `scripts/deploy_tools/land_lib/land_tags.py`
      re-exports `containers_entries_in` from `render_guard`.
    - `scripts/deploy_tools/narrow_lib/containers.py:changed_fields`, which diffs the
      `containers_list` key of an inventory edit.
    - `scripts/validate/compose_templates.py:main`, which checks each Docker entry's role
      template.
    - `scripts/lib/estate.py:Estate.pi_entries`, whose only non-test caller is
      `scripts/docs/gen_role_glance.py`.
- **The recovery heartbeat watches the list.**
  `ansible/roles/setup/optimize_pi/templates/pi-recovery-health.sh.j2` derives its watch set
  as `containers_list | map(attribute='name')`. It adds `docker-proxy-lifecycle` when
  `docker-proxy` is in that list. Its comment records #1910: with a hardcoded set, wg-easy
  and docker-proxy-lifecycle sat Exited after a failed start until a person noticed. H3.8b
  moves the template into the `pi-heartbeats` component and the autonomous contract into that
  component's header, with the derivation unchanged.
  `ansible/tests/setup/test_pi_recovery_restarts_and_reports.py:MUST_WATCH` names autoheal,
  docker-proxy, docker-proxy-lifecycle and wg-easy.
- **The engine upgrade reads the list too.**
  `ansible/roles/setup/docker_install/tasks/engine-upgrade.yml` stops every Compose project in
  `containers_list | filter_by_platform('docker')` before apt runs. It then recreates each
  with `recreate: always`. Its header gives the reason: a containerd.io upgrade replaces the
  shim under running containers, and a socket restart leaves docker-proxy on the old inode
  (#1961). Both compose tasks run with `become: false`. The role's `CLAUDE.md` says "The Pi's
  containers are down for the apt run". renovate.json's group `docker engine on the Pi
  (manual — merge, then run docker-engine-upgrade; it stops every Compose project)` carries
  the pin bumps that lead to this play.
- **The Docker play owns a fact the k8s play reads.** In `ansible/deploy.yml`, the
  `Deploy Containers` play's pre_task `Preserve the unfiltered container list for the k8s
  play` sets `containers_list_unfiltered`. The k8s play's pre_task `Restrict this play to
  k8s-platform containers` reads that fact.
- **The deployer routes each part of a move PR differently.**
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_changes.py:services_from_changed_paths`
  does the routing.
    - An `ansible/inventory/` edit matches `_BROAD_DEPLOY_PREFIXES` and sets
      `broad_deploy`. `scripts/deploy_tools/narrow_lib/broad.py:narrow` maps the changed keys
      to the roles that read them, or refuses. A failed broad arm writes `hold_sha`.
    - A path under `roles/containers/<svc>/templates/` joins `cs.services`, which
      `deploy_handlers.py:log_pi_changes` names in the journal and never applies.
    - A path under `tasks/` or `meta/` joins `cs.tasks`, which defers and alerts once.
    - A path under `roles/containers/common/` matches `_PI_SHARED_PREFIX`.
- **Tooling keys on `roles/containers/`.**
  `git grep -l -E 'roles/containers|containers/(alloy|autoheal|wg-easy|docker-proxy|common)'
  -- ansible/tests scripts '*.toml' .github .claude | wc -l` prints 49. Among them:
    - `scripts/validate/compose_templates.py:ROLES`, behind the `validate-compose-templates`
      hook;
    - `scripts/docs/reference/decisions.py:_PLANES`, which feeds `docs/reference/decisions.md`;
    - `scripts/lib/facts/pins.py:_COMPOSE_FILES`, which the `version-as-fact` lint reads;
    - `scripts/deploy_tools/narrow_lib/broad.py:ROLE_TREES`;
    - `ansible.cfg`, whose `roles_path` starts with `ansible/roles/containers`.
- **The rotation consumer census reads only `ansible/`.**
  `scripts/secrets_mgmt/consumers.py:_census_corpus` walks the `ansible/` directory and keeps
  only files under `roles/<plane>/<role>/` for the planes in `_ROLE_PLANES` (`k8s`,
  `containers` and `setup`). `tree_consumers` and `consumer_commands` read that corpus to tell
  the operator who holds a stale copy after a rotation. The grep above cannot find this
  module, because it builds the path from `_ROLE_PLANES` and never spells `roles/containers`.
  `git grep -l alloy_pi_http_password -- ansible scripts` lists alloy's `config.alloy.j2`
  and, outside it, only the rotation registry and the vault.
- **`docs/facts.lock` records rows that the move PRs break.** The section rows
  `ansible/roles/containers/alloy/CLAUDE.md#At a glance`, `#Notable`,
  `ansible/roles/containers/autoheal/CLAUDE.md#At a glance`, `#Notable`,
  `ansible/roles/containers/wg-easy/CLAUDE.md#Notable` and
  `#wg-easy — WireGuard VPN server + UI` belong to docs that the move PRs delete. Two rows
  outside the moved roles cite a deleted doc as an atom.
  `ansible/roles/setup/docker_install/CLAUDE.md#Notable` cites
  `ansible/roles/containers/docker-proxy/CLAUDE.md`, and root `CLAUDE.md#Where to Look (task →
  start here)` cites `ansible/roles/containers/common/CLAUDE.md`. Root `CLAUDE.md` names
  `test_every_recorded_atom_hashes_as_recorded` as the CI test that fails on both kinds, and
  prescribes `fact_status.py forget` for a `section-gone` row.
- **Pi images are untracked by decision, and the catch-all group automerges.**
  `scripts/tests/test_renovate_managers.py:test_no_custom_manager_tracks_the_retired_compose_plane`
  forbids a manager over `roles/containers`. Its docstring gives the reason: the Pi has
  `has_gitops: false` and no CI deploy path. renovate.json's first packageRule matches every
  `custom.regex` minor and patch update, with no `matchFileNames`, into the group `container
  images (non-major)` with `automerge: true`.
- **The pinDigests rule must stay last, and it covers three file patterns.**
  `scripts/tests/test_renovate_managers.py:test_every_k8s_image_file_gets_pin_digests_from_a_package_rule`
  asserts that the rule is `_PACKAGE_RULES[-1]`. The rule's description in `renovate.json`
  records that the regex custom manager defaults to `pinDigests: false`, so "a version bump
  carried no `newDigest` (grafana/alloy v1.19.2 -> v1.20.0)". Its `matchFileNames` lists only
  `ansible/roles/k8s/*/defaults/main.yml`, `ansible/roles/setup/*/defaults/main.yml` and
  `ansible/inventory/group_vars/all.yml`. Its description also justifies that scope: only the
  k8s-images manager carries a template that can write a digest onto a pin.
  `scripts/tests/test_renovate_auto_replace.py:test_the_k8s_image_template_round_trips_every_live_pin`
  holds that manager's `autoReplaceStringTemplate` to every live pin. alloy and wg-easy pin a
  tag and a digest.
- **A Pi deploy is heavy.** The `container_healthcheck_timeout` comment in
  `ansible/inventory/host_vars/daniel-pi.yml` records that a Pi deploy drives load1 8-17 and
  memory PSI `full` 20-25%, with OCI `Timeout waiting for systemd to create scope` failures.
  Every Pi deploy at `70591c307` runs with an operator attending.
- **The Pi's healthchecks are slow by design.** The same file sets
  `container_healthcheck_interval: 60s`, `container_healthcheck_timeout: 60s` and
  `container_healthcheck_retries: 5`. Its comment gives the worst-case detection latency as
  "5 x (60s interval + 60s timeout) = 10 min", and records exec stalls past a 30-second
  timeout under deploy load. docker-proxy's three services also set `start_period: 10s`.
- **Prometheus scrapes the Pi once a minute.**
  `ansible/roles/k8s/observability/templates/prometheus.yaml.j2` gives the `node-pi` job
  `scrape_interval: 1m`, and `ansible/roles/k8s/observability/defaults/main.yml` pins
  `prom/prometheus` 3.x in `observability_k8s_prometheus_image`. Prometheus 3's range
  selectors leave out the sample at the range's left edge, so a `[1m]` window over a 1-minute
  scrape usually holds one sample, and `rate()` over it returns nothing. alloy's compose
  template records the Pi's swap-in rate as `rate(node_vmstat_pswpin{job="node-pi"}[1d])` at
  136-181/s for 2026-09-03 to 09-17.
- **The Pi is the backup WireGuard endpoint.** The header of
  `hosts/components/pi-compose/services/wg-easy/compose.yaml.j2` (the deleted role's
  `CLAUDE.md` before H5.9) records the Pi's UDP 51822 as the fallback entry when the cluster's wg-easy, in
  `ansible/roles/k8s/wg-easy/` on 51820, is unreachable.
- **ADR-0013 names the deploy path H5 replaces.** `docs/adr/0013-daniel-pi-stays-on-docker.md`
  decides that the Pi "is driven remotely over SSH with `-e target=daniel-pi`". Its *Governs*
  section says the decision is expressed by the Pi's inventory entry and by the continued
  existence of `ansible/roles/containers/`.

## Design

### The service list: a second key in the Pi's inventory

`ansible/inventory/host_vars/daniel-pi.yml` gains `compose_services:` beside
`containers_list`. Entries keep the `containers_list` field names. A service lives in exactly
one of the two keys: the package deploys `compose_services`, and Ansible's Docker play
deploys `containers_list`.

The key lives in inventory, not in `hosts/daniel-pi/package.yml`, for two reasons. Ansible
still renders the cluster readers until D5 or H1, and H3.1 builds the renderer's host context
from the inventory layers. Both writers therefore read one source, and R1's zero-diff
comparison sees the same value. The key stays there after Ansible goes, because H7's exit
observation 1 keeps `ansible/inventory/` as renderer data.

A third `platform:` value on `containers_list` entries was rejected. `deploy_tags`,
`render_guard`, `render_targets`, `detach_notify` and `land_platform` each switch on
`docker` and `k8s` only. A third value would need a branch in each one, plus a refusal so that
`deploy.sh --tags alloy -e target=daniel-pi` cannot pass validation and deploy nothing. With a
second key, a moved tag leaves `containers_list`. `deploy_tags.py validate` checks a tag
against every host's list, so it then refuses `alloy`, `autoheal` and `docker-proxy`. It still
accepts `wg-easy`, which daniel-box declares, so after H5.9 `deploy.sh --tags wg-easy -e
target=daniel-pi` deploys nothing on the Pi. H5.12 removes that command from the docs and
skills that name it.

**Every reader reads both keys, from H5.1.**

- The three Jinja readers read `hostvars['daniel-pi'].containers_list +
  hostvars['daniel-pi'].compose_services`, with no `default`, so a missing key fails the
  render. monitor-bridge sorts the publishers by name, which makes `PI_PUBLISHED_PORTS`
  independent of which key holds an entry. artifacts drops its `default([])`.
- `scripts/lib/render_context.py:_HOSTVARS_KEYS` gains `compose_services`.
- `scripts/lib/ansible_inventory.py` gains one helper that returns the Pi's entries from both
  keys and raises when either key is missing. `declared_on_pi` and the Pi test helpers call
  it.
- `scripts/infra_map/inventory.py:declared_services` calls the same helper when the host is
  daniel-pi, and keeps `containers_entries_in` for the other hosts. The infra map's
  declared skeleton then keeps every moved service, so the Pi's live containers still
  match a declaration. From H5.2b, `load_roles` in that module also reads the service
  files under `hosts/components/pi-compose/services/`, so a moved `container_name`, such as
  `docker-proxy-lifecycle`, keeps its owner.
- The recovery heartbeat derives its watch set from the same union, sorted by name. The
  docker-proxy sub-proxies key off `docker-proxy` in the union.
- Each template names both keys literally, so the grep in `narrow_lib/broad.py` for a changed
  key still finds every reader.

**The other `containers_entries_in` callers stay on `containers_list`.** H5.1 reruns the
grep from the *Baseline* and records in its PR body why each caller is repointed or keeps
reading `containers_list` alone. At `70591c307` the answer is:

- `render_guard.py`'s `containers_entries` and `containers_entries_at`, and `land_tags.py`'s
  re-export, derive the Ansible deploy tags that exist. A moved service has no Ansible tag,
  so these read `containers_list` alone on purpose. That is how `deploy_tags.py validate`
  comes to refuse a moved tag.
- `narrow_lib/containers.py:changed_fields` diffs the `containers_list` key of an inventory
  edit. A move removes an entry, so it returns `None`, which its docstring defines as a
  change that moves every reader. The `compose_services` half of the edit reaches the
  readers through `narrow_lib/broad.py`'s key grep.
- `compose_templates.py:main` checks the role template of each Docker entry in
  `containers_list`. A moved entry leaves the list in the PR that deletes its role, and
  H5.2b points the tool at the component for the moved files.
- `estate.py:Estate.pi_entries` serves only `gen_role_glance.py`'s Pi compose-role shape.
  Each move PR deletes the role whose block that shape writes, and H5.12 drops the shape.

**Census.** A test in `ansible/tests/setup/` fails on three conditions:

- a name appears in both keys;
- a `compose_services` entry other than `pi-canary` has no legacy entry for its Ansible-era
  compose file in the `pi-compose` component;
- one of the three cluster readers, or the recovery script, renders differently after any one
  entry moves from `containers_list` to `compose_services`. The cluster readers render
  twice. One render goes through `ansible/tests/_k8s_render.py`, which builds its `hostvars`
  with `scripts/lib/render_context.py`. The other goes through C1's renderer, `homelab-render
  cluster --role <tag>`, which builds its own `hostvars` (R1's `renderer_lib/context.py`)
  and is what publishes a flux-owned reader. H5 depends on H3, and H3 on R1, so that
  renderer exists before H5.1. Before R2 lands, `cluster` runs only with `--secret-stubs`
  (C1), so the census passes R1's stub file. That is a test-only render that publishes
  nothing, and C1 says stub mode exists only for R1's shadow comparison, so *Open questions*
  5 asks C1 to allow it. After R2 the census renders in placeholder mode. None
  of the three readers is a `render_sops_manifests` entry in R2, so placeholder mode still
  renders monitor-bridge's env Secret.

That is H3's two-writer rule for this list. The census asserts that it read both keys and
found `docker-proxy` and `alloy` by name.

### One project, included per service

The package installs `/usr/lib/homelab/compose/compose.yaml`, which looks like this:

```yaml
name: homelab
include:
  - path: /usr/lib/homelab/compose/docker-proxy/compose.yaml
    project_directory: /home/ubuntu/server/containers/docker-proxy
    env_file: /usr/lib/homelab/compose/compose.env
  - path: /usr/lib/homelab/compose/autoheal/compose.yaml
    project_directory: /home/ubuntu/server/containers/autoheal
    env_file: /usr/lib/homelab/compose/compose.env
```

- Each service's file renders from its template, with `container_item` set from its
  `compose_services` entry.
- `project_directory` points at the data directory the Ansible-era project used. `./config`
  and `./data` therefore resolve to the paths the data already occupies, including the one
  pi-peer-backup reads.
- **`env_file:` is named on every include.** The Compose `include` reference says an include's
  `env_file` "defaults to `.env` file in the `project_directory`". It does not say whether the
  CLI's `--env-file` reaches an included file. Naming the file on every include removes that
  doubt. H5.7, the first move that carries a secret, proves that a value arrives (*Open
  questions* 7, the 2026-10-24 ruling).
- **Order inside the project is `depends_on`.** In its own move PR, autoheal gains `depends_on:
  docker-proxy-lifecycle`, and alloy gains `depends_on: docker-proxy`. That replaces
  `PI_ORDERING`'s list-order rule. Compose rejects a project whose `depends_on` names a
  service it does not declare, so a project census in CI fails the same render first.
- **Every `container_name` equals its service key**, as at `70591c307`. compose-adopt relies on
  that rule, and the same project census asserts it.
- **Every image is pinned by digest.** compose-run pulls only an image the host lacks, which
  is correct only for a digest pin. The project census asserts it.
- **Bind sources use long syntax with `bind: create_host_path: false`.** A missing source
  then fails the start. It does not become an empty directory owned by root.

### Data directories

The component ships `/usr/lib/tmpfiles.d/homelab-compose.conf`. It holds one `d` line for each
directory a moved service binds or runs in:

- `<svc>` for each service, and `alloy/data`, which `setup_dirs.yml` creates;
- `wg-easy/config`, which Docker created;
- `/var/log/pi-health`, which alloy reads. The H3.8b heartbeat units create it through
  `LogsDirectory=pi-health`, so its line names the owner those units use.

Each line copies the mode and owner the directory has on the live Pi. The move PR's body
records them with `stat -c '%a %U %n'`. On the live Pi the lines therefore change nothing. On
a rebuilt Pi, which H7's exit criterion needs, they create the directories before any
container starts.

The only bind source that is not a package path or a listed directory is
`/var/run/docker.sock`, which docker-proxy's three services bind and dockerd creates.

The package's postinst calls only `systemctl`, so it cannot run tmpfiles itself.
`homelab-compose.service` runs it instead, in `ExecStartPre=`.

### `homelab-compose.service`

The unit has these settings:

- `Type=oneshot` and `RemainAfterExit=yes`;
- `Wants=` and `After=` on `docker.service`, and `Wants=` and `After=` on
  `homelab-secrets.service`, per H3's *Ordering on the secrets unit*. The unit does not use
  `Requires=docker.service`. systemd propagates an explicit stop or restart of a required unit
  to the units that require it. A restart of `docker.service`, by a package's maintainer
  script or by hand, would then start a compose run outside reconcile, during an engine
  upgrade among other times. That is H3's argument against `Requires=homelab-secrets.service`.
  Neither form recreates a container after a daemon restart, so the change loses nothing;
- `Environment=COMPOSE_PARALLEL_LIMIT=1` and `MemoryAccounting=yes`;
- `Environment=HOMELAB_COMPOSE_WAIT_TIMEOUT=<seconds>` and `TimeoutStartSec=`, both derived in
  the unit template (*The wait timeout*);
- `Environment=HOMELAB_COMPOSE_INPUTS_SHA256=<64-hex>`, described below;
- an enable link in `/usr/lib/systemd/system/multi-user.target.wants/`, shipped by the
  package. The boot path, reconcile's step 2 and H4's restart set all need it.

It runs two commands:

- `ExecStartPre=/usr/bin/systemd-tmpfiles --create /usr/lib/tmpfiles.d/homelab-compose.conf`;
- `ExecStart=/usr/lib/homelab/compose-run`.

It has no `ExecStop=`, so stopping or restarting the unit never stops a container.

**The inputs line.** Reconcile's restart set holds a unit only when its unit file or drop-in
hash changes (H3, *The reconcile unit*, step 3), or when `/run/homelab-secrets/pending.json`
lists it (H4, *The restart contract*). A compose file is neither, so a Renovate digest bump
alone would restart nothing. The renderer therefore renders the component's other files
first. It then passes the sha256 over every
regular file the component installs under `/usr/lib/homelab/compose/`, plus `compose-run`,
`compose-adopt` and the tmpfiles file, into the unit template. Any change to the project
then changes the unit file. The alloy label below uses the same two-pass render.

**What starts it.** H4 owns every restart, and `homelab-reconcile` is the only process that
restarts this unit (*Restart ordering* under *Secrets*). Three paths start a run:

- the boot path, through its enable link;
- reconcile's step 2, which starts an enabled unit that is not active. That is the first run
  after H5.2b ships the enable link;
- H4's restart set in reconcile's step 3, when the inputs line changes or `pending.json` lists
  the unit because `compose.env` changed. Reconcile runs `systemctl reset-failed`, then
  `systemctl restart`, once per reconcile run, and drops the unit from `pending.json` only
  after that restart succeeds.

On each run, `up -d` recreates only the services whose config changed.

**`compose-run`** is a POSIX shell script that does six things in order. Every check that can
refuse runs before compose-adopt, so adopt removes an Ansible-era container only in a run that
can create its replacement at once.

1. **The key check.** The script takes the key set from the `${<key>}` spans in the installed
   compose files, never from Compose's interpolated output. It refuses to continue when
   `compose.env` lacks one of those keys or holds an empty value for one. Compose itself
   would warn and substitute an empty string, which is failure class 3. A refusal names the
   key, never a value.
2. **The model check.** It runs `docker compose -f /usr/lib/homelab/compose/compose.yaml
   config -q`, which validates the merged project and prints nothing on success.
3. **The pull of missing images.** It lists the project's images with `docker compose -f
   /usr/lib/homelab/compose/compose.yaml config --images`, and runs `docker pull --quiet` only
   for an image that `docker image inspect` does not find. Every image is digest-pinned, which
   the project census asserts, so an absent image means a new digest. A run whose digests are
   all present makes no registry request. The boot run, a step-2 start and a rotation-only
   restart therefore do not depend on a registry, as `up -d` did not at `70591c307`. The
   script does not rely on Compose's own `pull --policy missing`, whose support in the pinned
   plugin the tree does not show.
4. **Adoption.** It runs `compose-adopt`.
5. **The start.** It runs `docker compose -f /usr/lib/homelab/compose/compose.yaml up -d
   --remove-orphans --wait --wait-timeout "$HOMELAB_COMPOSE_WAIT_TIMEOUT"`. The images are
   already present, so `up` pulls nothing.
6. **The metric.** It writes `/var/lib/node-exporter-textfile/homelab_compose.prom`
   whatever the outcome, and then exits with the outcome's code. It follows M3's textfile
   convention: `mktemp` in the same directory, `chmod 0644`, then `mv`. A bare `mktemp` file
   is 0600, which node_exporter and the recovery heartbeat, running as `ubuntu`, could not
   read. The file holds
   `homelab_compose_up_result{host="daniel-pi",sha="<12-hex>"}`, set to 1 or 0,
   `homelab_compose_up_timestamp_seconds{host="daniel-pi"}` at the run's end and
   `homelab_compose_up_duration_seconds{host="daniel-pi"}`.
    - The `sha` label comes from the installed package version. compose-run runs
      `dpkg-query -W -f '${Version}' homelab-daniel-pi` and keeps the 12 hex digits after
      `+g`, because C5 fixes the version as `0.<count>+g<12-hex sha>`. That SHA is the last
      merge that changed the Pi package, which H3's ancestry comparison already expects.
    - A version without that suffix writes `sha="unknown"`. No ancestry check accepts it, so
      a landing waits rather than settling on it.

A failure in steps 1 to 3 writes 0 and exits before step 4, so every container keeps
running as it was. compose-run prints nothing that Compose interpolated. The unit's output
goes to the journal, which Alloy ships to loki-homelab.

**`manifest.json` carries no SHA.** H3 keeps the version and the SHA out of it, so that a
render changes only when the package's content does. No executor adds a SHA to it: a field
that changes on every merge would make every host package publish and reconcile on every
merge. compose-run therefore reads the SHA from `dpkg-query`, never from the manifest.

**The wait timeout.** The unit template derives `HOMELAB_COMPOSE_WAIT_TIMEOUT` from the Pi's
healthcheck variables rather than writing a number. Compose's `--wait` returns once a
container reports healthy, and declares it failed only after `retries` consecutive failed
checks. The worst case before a verdict is therefore the largest `start_period` plus
`container_healthcheck_retries` × (`container_healthcheck_interval` +
`container_healthcheck_timeout`). The template adds 120 s of slack to that. At `70591c307`
the result is 10 + 5 × (60 + 60) + 120 = 730 s. `TimeoutStartSec` is that timeout plus 600 s
for the pull and 60 s for the checks and compose-adopt, 1390 s or about 23 minutes. That is
inside reconcile's 30-minute `TimeoutStartSec`.

**At boot**, dockerd restarts the containers by their restart policy, whatever order systemd
starts its units in. No container binds a path under `/run/homelab-secrets/` (*Secrets*), so
that restart uses the config the container was created with. `homelab-compose.service` then
runs after `homelab-secrets`, and its `up -d` changes nothing unless the package changed.

### `compose-adopt`

`compose-adopt` resolves the case where a service moves projects. It lists the service names
with `docker compose config --services`, which prints names only. Every `container_name`
equals its service key (*One project, included per service*), so each name is also a
container name. compose-adopt never runs `config` in a form that prints the interpolated
model. A container with that name whose `com.docker.compose.project` label is not `homelab`
was started by the Ansible-era project. compose-adopt removes it with `docker rm -f`, and
`up` then recreates it under `homelab`.

- **Scope.** It touches only names the merged file declares, and only containers from
  another project. Each removal is one `logger -t compose-adopt` line naming the container
  and its old project.
- **When it runs.** compose-run calls it only after the key check, the model check and the
  pull have passed.
- **Contract.** The component's `component.yml` header carries the same five-part
  autonomous contract the setup roles use, because compose-adopt deletes containers
  unattended.

### Secrets

**One env file, single-quoted.** `homelab-secrets` renders `compose.env`, a secret-bearing
template that holds one `<key>='${<key>}'` line per key the rendered compose files reference.
Per H4, the template installs under `/usr/lib/homelab/secret-templates/`. The package ships a
symlink at `/usr/lib/homelab/compose/compose.env` pointing to
`/run/homelab-secrets/usr/lib/homelab/compose/compose.env`, rendered root 0600. `secrets.json`
names `homelab-compose.service` as its dependent.

- Compose's dotenv parser reads a single-quoted value literally. An unquoted or double-quoted
  value is interpolated, and an unquoted ` #` starts a comment.
- The manifest marks `compose.env` with `format: dotenv`. For such an output, `homelab-secrets`
  refuses a value that holds `'` or a newline. H5.2a2 adds that check to the script.

**Compose files keep their placeholders.** A rendered compose file carries `${<key>}` and
passes through exactly one substitution, Compose's own, as H4's rule requires. It therefore
installs at its real path under `/usr/lib/homelab/compose/`. H4's renderer rule, which routes
a placeholder-bearing file to `secret-templates/`, exempts that directory.

- Compose reads `$$` as a literal `$`, so C1's `$${` comes out as a literal `${`.
- Compose also interpolates a bare `$name`, which C1 does not escape. A census therefore
  asserts that every `$` in a rendered compose file opens either `$$` or a `${<key>}` that
  `compose.env` carries.

**alloy's config carries no secret.** `config.alloy.j2` reads the password as
`sys.env("ALLOY_PI_HTTP_PASSWORD")` and the domain as `sys.env("HOMELAB_DOMAIN")`. Alloy's
`http` block reference uses that form for `password` in its own example. alloy's compose file
sets both variables from `${alloy_pi_http_password}` and `${domain}`. Four things follow:

- `config.alloy` installs as a plain package file,
  `/usr/lib/homelab/compose/alloy/config.alloy`, at mode 0644, mounted read-only.
- A secret rotation changes alloy's environment, so `up -d` recreates alloy.
- A template change alters a plain file that Compose cannot see. A `homelab.config-sha256`
  label carries the sha256 of the rendered `config.alloy`, which holds no secret, so the label
  changes and Compose recreates alloy. The renderer renders `config.alloy` first and passes
  its sha256 into the compose template's context as `alloy_config_sha256`. The label replaces
  `common_config_changed`.
- No compose file binds a path under `/run/homelab-secrets/`. The password sits in alloy's
  container environment, as autoheal's webhook sits in autoheal's at `70591c307`.

**The password's new exposure is an accepted risk, decided by the operator before H5.8
merges (**operator-only**).** At `70591c307` the password lives in a 0640 file, and the
role's comment gives the password as the reason for that mode. In the container environment
it joins the `Env` that `ansible/roles/containers/docker-proxy/CLAUDE.md` records the
read-only proxy returning. That is the exposure autoheal's webhook already has. The H5.8 PR
body states this change, and the operator approves it or declines it there. *Open questions*
6 records the decision.

If the operator declines, H5.8 does not merge, and the password moves to a file instead: an
H4 output with `persist: false`, owned by uid 1000, bound read-only into alloy, and read
through Alloy's `local.file` with `is_secret = true`. The file lives under
`/run/homelab-secrets/`, never at rest. C5 allows a persisted output only for a consumer
that must start before the secrets unit runs, and alloy is not one, so H4 sets the output
to `persist: false`. The bind source then exists only once `homelab-secrets` has run.
That route is not cheap, for two reasons:

- A rotation rewrites the file and changes nothing in the Compose model, so `up -d` does
  not recreate alloy. That is the secret-only recreate problem H5-R1-11 fixed, and the
  route needs its own fix for it.
- Nothing in the tree shows whether Alloy's top-level `http` block accepts a `local.file`
  export for `password`. A CI run of the pinned Alloy image against the rendered config
  would have to prove it first.

**Restart ordering.** H5 adopts H4's restart contract (*The restart contract*) as written.
`homelab-secrets` restarts nothing, and no H5 script calls `systemctl` on another unit.

- **The order comes from reconcile's sequence.** When the secrets inputs hash changes,
  reconcile's secrets step runs `systemctl restart homelab-secrets.service`. That unit is a
  oneshot, so the call returns only after the run has written `compose.env`. The secrets step
  runs before steps 2 and 3, so any start or restart of `homelab-compose` reads the new file.
  The order does not depend on one `systemctl` call or on `After=` inside a transaction.
  `After=homelab-secrets.service` orders the two units only at boot.
- **compose-run's own checks are the gate on `compose.env`.** H4's existence gate covers
  every start or restart that reconcile makes. Reconcile starts or restarts
  `homelab-compose` only when `compose.env` exists, whether a hash change, `pending.json`
  or step 2 put the unit in line. Two cases still reach compose-run with a file that does
  not fit the project:
    - The boot path is not reconcile, so no gate sees it.
    - An existence gate passes on a stale file. A failed re-render leaves the previous
      `compose.env` in place, because `homelab-secrets` writes through a temporary file and
      a rename.

  compose-run therefore checks the file itself. Its key check, model check and pull all run
  before compose-adopt. A `compose.env` that lacks a key the installed project references,
  whether stale or incomplete, fails the run while every container keeps running.
- **A failed run is retried.** `reset-failed` clears the start limit, and `restart` runs a
  failed unit, which `try-restart` would skip. The next package that changes the project
  therefore reruns it.
- **The restart waits for the run.** A Pi reconcile lasts as long as the run does, at most
  the unit's derived `TimeoutStartSec`, about 23 minutes (*The wait timeout*). A failed run
  fails reconcile. The updater then holds the Pi and writes `homelab_host_reconcile_ok 0`
  (H3, *Host-plane holds and stop levers*), and `land.sh` ends the landing `deploy-failed`.
- **The hand path is reconcile.** A person who restarts `homelab-secrets` by hand restarts no
  dependent. That run queues `homelab-compose.service` in `pending.json` when `compose.env`
  changed, and `sudo systemctl start homelab-reconcile.service` restarts it (H4). A hand
  `sudo systemctl restart homelab-compose.service` also applies the file, but it leaves the
  unit's `pending.json` entry in place, so the next reconcile run restarts the unit once
  more. That extra run changes nothing, because `up -d` finds nothing to recreate.

**The Pi's SOPS file.** Each PR that adds a placeholder has the operator run
`uv run python scripts/secrets_mgmt/host_secrets.py sync` and commit
`ansible/vars/hosts/daniel-pi.sops.yaml` (**operator-only**, because `sync` decrypts). H4's
`host_secrets.py check` fails the PR until the file carries the key.

### The engine upgrade

From H5.3, `engine-upgrade.yml` stops and recreates the `homelab` project itself, before any
service moves.

- **The tool.** It uses `community.docker.docker_compose_v2` (5.3.0 in
  `ansible/requirements.yml`), with `project_src: /usr/lib/homelab/compose` and `files:
  [compose.yaml]`.
- **Privilege.** These tasks run with `become: true`, because `compose.env` is root 0600.
- **The play holds the host plane first.** From H5.3 the play and `homelab-reconcile` both
  write the `homelab` project. Without a hold, an updater run that waits on the play's dpkg
  lock installs a package once apt releases it, and reconcile then restarts
  `homelab-compose` during the play's recreate. The play's first tasks, before the apt
  probe, therefore take H3's per-host lever:
    - It records whether `homelab-updater.timer` is already masked.
    - It runs `systemctl mask --now homelab-updater.timer`. Reconcile never starts a masked
      unit, so no later package can re-arm it mid-play.
    - It waits until `systemctl is-active homelab-updater.service
      homelab-reconcile.service` reports neither unit `active` or `activating`. A run already
      in progress therefore finishes before the play stops anything. The wait's limit is
      the `TimeoutStartSec` of the rendered `homelab-updater.service` plus 60 s, 51 minutes
      against H3's 50. An updater run holds its install and a synchronous reconcile, so it
      is the longest run the wait can meet, and reconcile's own 30 minutes would fail the
      play while a normal updater run is still active. The play carries the limit as a
      `docker_install` role default, and the engine-upgrade test reads H3's rendered unit
      and fails when the default does not exceed that unit's limit. Past the limit the
      play fails before apt, with the timer masked and nothing stopped.
    - It never writes `/var/lib/homelab-updater/hold`. That file is the failed-reconcile
      signal, and `--clear-hold` removes it only after a passing reconcile.
- **The play releases the hold last.** Its final task runs `systemctl unmask` and then
  `systemctl start` on `homelab-updater.timer`, after the recreate has succeeded or after the
  probe has found nothing to upgrade. A timer that was already masked when the play began
  stays masked, so the play never undoes the operator's own lever.
- **A failed play leaves the timer masked.** The Pi then installs no package, so no
  unattended compose run meets an engine in an unknown state. If the play failed after the
  stop and before the recreate, the containers it stopped stay stopped. The operator fixes
  the cause, reruns the play, or runs `sudo systemctl unmask homelab-updater.timer` and `sudo
  systemctl start homelab-updater.timer` from a LAN session. The next updater run then
  installs as usual. A reboot does not clear the mask.
- **Before apt.** The per-project loop stops the Ansible-era projects, and then `state:
  stopped` stops `homelab`.
- **After the daemon starts.** `state: present` with `recreate: always` recreates `homelab`
  first, and then the loop recreates the remaining projects. That is
  `up -d --force-recreate`, which gives every container the new shim and the live socket
  inode.
- **The order holds in every intermediate state.** docker-proxy moves first, so `homelab`
  holds what the other projects depend on from H5.6 on.
- **A guard task fails before apt** when `compose_services` is non-empty and
  `/usr/lib/homelab/compose/compose.yaml` is missing.
- **Later changes.** H5.12 deletes the per-project loop. H7.5 replaces the play, and its unit
  needs the same hold and its own forced recreate (*Open questions* 5, note to H7).

### What stays with Ansible until H7

- The Docker engine, its pins and hold, `daemon.json`, and the `proxy` and `lifecycle`
  networks stay with `docker_install`.
- The Pi's `containers_list: []` stays in inventory until H7. An empty Pi list is the shape
  daniel-server's list already has. H5.1 makes every reader of the union strict, so the key
  and the readers must change in one PR. H7's step H7.20d is that PR: after H7.20a has
  deleted the last Ansible reader, it deletes the key and repoints every reader at
  `compose_services` alone. *Open questions* 5 asks C4 to name H7.20d and its reader set in
  place of its present trigger.
- `compose_services` never leaves the inventory. H7 keeps `ansible/inventory/` as renderer
  data (H7's exit observation 1), so the key stays the renderer's source for the Pi's
  project.
- **`ansible/deploy.yml` is not edited by H5.** Once the Pi's list is empty, the Docker play
  loops over an empty list on every host. Its snapshot pre_task keeps feeding the k8s play.
  The file's last play goes with R4 or H7, and the Docker play goes with it.

### Rows H5 takes from D5's `k8s_host_files`

D5 moves the host-side task rows of the roles it flips into one setup role,
`ansible/roles/setup/k8s_host_files/` (D5, *Host-side rows*). Two of those rows write the Pi
through `delegate_to`, and D5 says each stays there "until H5". D5's crowdsec conversion also
edits the Pi's Alloy template. H5 takes two of these three changes and leaves one:

- **The crowdsec probe timer.** D5's crowdsec conversion installs a timer on daniel-pi through
  `delegate_to: daniel-pi`. It probes the VIP every minute and appends the status to a file
  under `/var/log/pi-health/`. H5.14 moves it into the Pi package, in the same PR that deletes
  its `delegate_to` tasks.
- **The Alloy metrics stage.** The same D5 PR adds a stage to
  `ansible/roles/containers/alloy/templates/config.alloy.j2`, which counts the probe results,
  and applies it by hand with `-e target=daniel-pi`. H5.8 `git mv`s that template, so the
  stage moves with it.
- **pi-peer-backup's two writes stay.** The forced-command wrapper and the authorized key are
  not Compose services. `k8s_host_files` keeps writing them until H7 (*Open questions* 3).

**Ordering against D5's crowdsec conversion.** Neither slice depends on the other, so either
can land first. Whichever lands second carries the other's change.

- **D5 first.** H5.8 rebases onto the stage and moves it with the template. H5.8's tooling
  test asserts the stage by the component label D5 gives it, so a rebase that drops it goes
  red. H5.14 then moves the timer.
- **H5.8 first.** D5's PR adds the stage to
  `hosts/components/pi-compose/services/alloy/config.alloy.j2` instead. The config-sha256
  label recreates alloy, and no hand apply runs on the Pi. D5 adds the timer to the Pi
  package directly, and H5.14 is not needed. *Open questions* 5 records this as a note to D5.

### File tree

```diff
+ hosts/components/pi-compose/component.yml
+ hosts/components/pi-compose/compose-run
+ hosts/components/pi-compose/compose-adopt
+ hosts/components/pi-compose/units/
+ hosts/components/pi-compose/tmpfiles.d/homelab-compose.conf
+ hosts/components/pi-compose/compose.env.j2
+ hosts/components/pi-compose/services/docker-proxy/  (git mv of roles/containers/docker-proxy/templates)
+ hosts/components/pi-compose/services/autoheal/      (git mv)
+ hosts/components/pi-compose/services/alloy/         (git mv)
+ hosts/components/pi-compose/services/wg-easy/       (git mv)
+ hosts/components/pi-compose/services/pi-canary/     (added in H5.2b, removed in H5.10)
+ hosts/components/crowdsec-probe/                    (D5's Pi probe timer, H5.14)
  hosts/daniel-pi/package.yml                         (+ pi-compose, + crowdsec-probe)
  ansible/roles/setup/k8s_host_files/                 (- the probe timer's delegate_to tasks, H5.14)
  hosts/lib/homelab-secrets                           (format: dotenv check)
  hosts/components/pi-heartbeats/templates/pi-recovery-health.sh.j2   (watch set over both keys;
                                                      compose arm, H5.2b)
  scripts/secrets_mgmt/consumers.py                   (+ hosts/components/ walk root, H5.7)
- ansible/roles/containers/                           (all five roles, by H5.12)
  ansible/inventory/host_vars/daniel-pi.yml           (+ compose_services; containers_list emptied)
  ansible/roles/k8s/observability/templates/prometheus.yaml.j2   (reads both keys)
  ansible/roles/k8s/monitor-bridge/templates/env-secret.yaml.j2  (reads both keys, sorted)
  ansible/roles/k8s/artifacts/templates/configmap.yaml.j2        (reads both keys, no default)
  scripts/lib/render_context.py                       (+ compose_services in hostvars)
  scripts/lib/ansible_inventory.py                    (+ one helper over both keys)
  scripts/diagnostics/probe_lib/health_docker.py      (declared_on_pi over both keys)
  ansible/roles/setup/docker_install/tasks/engine-upgrade.yml    (updater mask; homelab stop
                                                                  and recreate; unmask)
  ansible/roles/setup/docker_install/defaults/main.yml           (+ the wait's limit)
  scripts/infra_map/inventory.py                      (declared_services over both keys for
                                                      the Pi; load_roles scans the component)
  scripts/deploy_tools/land_lib/                      (host wait reads the compose result)
  scripts/validate/compose_templates.py, scripts/docs/reference/decisions.py,
  scripts/lib/facts/pins.py                           (scan the component)
  renovate.json                                       (+ manager and rules over pi-compose,
                                                      + Pi glob in the pinDigests rule, H5.11)
  ansible.cfg                                         (- roles/containers in roles_path, H5.12)
  ansible/roles/setup/gitops_deploy/files/deployer_lib/   (- the Pi Docker arms, H5.13)
```

## Steps, as PRs

**Landing a move PR (H5.6 to H5.9).** Each PR edits `ansible/inventory/`, deletes one role and
adds one service to the package. The PR body records the output of
`uv run python scripts/deploy_tools/deploy_tags.py narrow` and
`uv run python scripts/deploy_tools/deploy_tags.py blockers` before merge.

- **Tick arm.** The inventory edit takes the deploy-plane arm. `narrow` maps
  `containers_list` and `compose_services` to the list readers (monitor-bridge, observability,
  artifacts) or refuses. H5.1's census proves that their renders are byte-identical across a
  move, so an apply of them changes nothing. Such an apply cannot fail because of the move.
  It can still fail for an unrelated reason and write `hold_sha`, like any broad apply. If
  `blockers` exits 3, the PR follows root `CLAUDE.md`'s *When to wait*: name the blocker and
  stop.
- **Alerts.** The deleted `templates/` path produces one `log_pi_changes` journal line. The
  deleted `tasks/` and `meta/` paths produce one tasks-channel deferral. The operator does not
  act on either, because the role is deleted on purpose.
- **Pi.** No `deploy.sh -e target=daniel-pi` runs. The builder publishes the package, and the
  Pi's updater installs it.
- **Verdict.** `land.sh` waits for the Pi's applied SHA and, from H5.3, a compose result at or
  after the merge SHA. The expected verdict is `settled`.
- **When a reader role is flux-owned.** H5 depends only on H3 and H4, so a PR may land after
  D5 or H1 has made monitor-bridge, observability or artifacts `owner: flux`. Each reader
  then takes one of two branches:
    - An Ansible-owned reader takes the tick arm above.
    - A flux-owned reader is skipped by `narrow` and by the deployer's k8s arms (C4). Its
      render reaches the cluster only through `promote`. The census below proves its render
      unchanged across a move, through C1's renderer as well, so the publish changes no
      file under that tag. The verdict then rests on the host wait, and the expected
      verdict stays `settled`. If a render did change, C6's cluster wait would apply to that
      tag, and the PR would be wrong by the census's own rule.

1. **H5.1 One list, read whole.**
    - Add `compose_services: []` to `ansible/inventory/host_vars/daniel-pi.yml`.
    - Repoint the five readers and the recovery watch set at both keys, as *The service list*
      describes. The infra map's `declared_services` is one of the five.
    - Run `git grep -n containers_entries_in -- scripts ':!*/tests/*'` and record in the PR
      body why each remaining caller keeps reading `containers_list` alone (*The service
      list*). A caller the list does not name is repointed in this PR or the PR stops.
    - Add the census.
    - Change the `has_docker: true` comment ("Every service in containers_list below runs on
      it") to name both keys.
    - Two-writer check: no entry moves, so each service still has one writer.
    - Landing, for an Ansible-owned reader: the three k8s template edits defer and alert,
      and `land.sh` deploys monitor-bridge, observability and artifacts. Only
      monitor-bridge's rendered Secret changes, by the order of `PI_PUBLISHED_PORTS`.
    - Landing, for a flux-owned reader: the edit lands through `promote`, and `land.sh`
      waits on that tag's Kustomization as C6 states. The same one changed value is the
      only rendered difference.
    - The Pi package picks up the re-rendered recovery script.
2. **H5.2 The unit, adopt and a canary.** H5.2a is three PRs and H5.2b is one, in this order.
   Each part of H5.2a stays inert until H5.2b ships the enable link. H5.2a keeps one name
   for the three, because H4 cites "H5.2a's quiet window".
    - **H5.2a1, the renderer pieces.**
        - Add the include renderer, the two-pass render that computes the inputs hash,
          `compose.env.j2` and the tmpfiles file.
        - Tests: the single-substitution census, the bind-source census, the secret-dependent
          check and the project census (`depends_on` targets and container names).
        - Two-writer check: nothing deploys yet.
    - **H5.2a2, the dotenv check.**
        - Add the `format: dotenv` refusal of `'` and newline to `homelab-secrets`, with its
          tests beside H4's.
    - **H5.2a3, the unit and its scripts.**
        - Add `homelab-compose.service`, whose template takes the inputs line from H5.2a1's
          hash, and `compose-run`, `compose-adopt` and the textfile metric. The tests are the
          stub-`docker` tests, the unit-timeout test and the restart-contract tests in
          `hosts/lib/tests/`, beside H4's reconcile tests (*Verification*).
        - The unit ships without its enable link. Reconcile's step 2 starts only enabled
          units, and H4's restart set restarts a unit only when it is enabled or active. No
          run therefore happens while the project declares no service.
        - Two-writer check: nothing deploys yet.
    - **H5.2b, the canary.**
        - **Operator-only, before merge:** the operator decides on the ADR-0013 amendment
          (*Open questions* 7). This PR ships the first enable link, so it is the first PR
          that runs anything through the package. If the operator declines, H5.2b does not
          merge, H5.2a3 to H5.2a1 revert newest first, and no service has moved. H5.1 stays,
          because it moves no entry.
        - Ship the unit's enable link.
        - **The interim failure signal.** The recovery heartbeat in the `pi-heartbeats`
          component gains one read. When `homelab_compose.prom` holds
          `homelab_compose_up_result` 0, the heartbeat pushes `down` to its existing Kuma
          tile, naming `homelab-compose` and the result's SHA. It acts on nothing, so its
          autonomous contract gains a read and no action. An absent file adds nothing,
          because the file is absent by design until the first run. A file that exists but
          cannot be read is reported as `down`, never treated as absent. The signal lasts
          until M1's rule replaces it (*Open questions* 5, note to M1).
        - The project's first service is `pi-canary`, a digest-pinned `busybox` running
          `sleep infinity`, which costs about 1 MB. It sets `HOMELAB_DOMAIN={{ domain }}`, a
          plain variable that renders into the compose file at build time. It reads no secret,
          has no `env_file:`, and `pi-compose` declares no `outputs:` entry, so no `compose.env`
          exists yet (*Open questions* 7, the 2026-10-24 ruling). The proof that an included
          file receives a value from `compose.env` waits for H5.7.
        - Point `compose_templates.py`, `decisions.py:_PLANES`, `pins.py:_COMPOSE_FILES`
          and `scripts/infra_map/inventory.py:load_roles` at
          `hosts/components/pi-compose/services/` as well as `roles/containers`. Each tool
          asserts a named member: `pi-canary`'s template, the autoheal marker `DECIDED
          autoheal runs with NO healthcheck`, alloy's `image:` line, and `pi-canary` as an
          owner in the role index.
        - No `host_secrets.py sync` runs in this PR, and the Pi's SOPS file is untouched
          (*Open questions* 7, the 2026-10-24 ruling).
        - Two-writer check: `pi-canary` exists in no Ansible list.
3. **H5.3 The engine upgrade and the land wait.**
    - Change `engine-upgrade.yml` as *The engine upgrade* describes, with its test and red
      fixtures. The change includes the hold on `homelab-updater.timer` before anything
      stops, and its release after the recreate.
    - Add the compose condition to H3.9's host-wait module in `scripts/deploy_tools/land_lib/`.
      When the PR's rendered diff for daniel-pi touches `/usr/lib/homelab/compose/`, the wait
      also needs `homelab_compose_up_result{host="daniel-pi"} == 1` at a SHA equal to or
      descending from the merge SHA. A 0 there means not settled.
    - A run that reconcile started and that failed also fails reconcile, so H3's wait ends
      that landing `deploy-failed` on `homelab_host_reconcile_ok` 0. The compose condition
      adds one case H3's cannot see: a reconcile that passed without running the project,
      such as with the unit masked.
    - Landing: the `docker_install` path is a setup-plane change. The tick applies
      `initial_setup.yml --tags docker_install` on daniel-box, where `has_docker: false`
      runs the no-op teardown.
4. **H5.4 Measure an unattended pull.**
    - Bump `pi-canary`'s digest to another `busybox` digest, a one-line change.
    - The run's window comes from its own metric. `homelab_compose_up_timestamp_seconds` is
      the end, and `homelab_compose_up_duration_seconds` gives the start. The figures are
      host-level, because an image pull and its extraction run in dockerd and containerd,
      outside the unit's cgroup.
    - An agent reads four figures through Prometheus, with the transport in *Verification*:
        - the highest 5-minute memory PSI `full` over the run,
          `max_over_time(rate(node_pressure_memory_stalled_seconds_total{job="node-pi"}[5m])[<window>:1m] @ <end>)`;
        - the lowest `node_memory_MemAvailable_bytes{job="node-pi"}` over the run, and the
          lowest over the 24 hours before its start;
        - the highest 5-minute swap-in rate over the run,
          `max_over_time(rate(node_vmstat_pswpin{job="node-pi"}[5m])[<window>:1m] @ <end>)`;
        - the run's duration.
    - The `[5m]` window holds four or five samples at the `node-pi` job's 1-minute scrape.
      A `[1m]` window would usually hold one sample under Prometheus 3, and `rate()` would
      return nothing.
    - **A query that returns no sample fails the step.** An empty result is never read as
      "below the threshold" (failure class 3).
    - The figures go in `docs/pi-host-tuning-record.md`, with the swap-in rate beside the
      136-181/s daily rate that alloy's compose template records.
    - Threshold: a 5-minute memory PSI `full` at or above 20%, or a run minimum of
      `MemAvailable` below the lowest value of the 24 hours before the run. 20% is a starting
      threshold, not a like-for-like comparison: the inventory comment's 20-25% for an
      Ansible-era deploy does not name its window. At or above the threshold, the Pi's
      updater timer moves to once a day, as H3's apt-memory risk already provides, and exit
      observation 2 can take a day.
5. **H5.5 Prove the second access path.** This step has no code, and it can run at any time
   before H5.9. The operator does three things:
    - connects a client from outside the LAN through the cluster's wg-easy on 51820;
    - confirms console or keyboard access to the Pi;
    - records both, with the command output, in H5.9's PR body.
   If the cluster's wg-easy is down, H5.9 waits.
6. **H5.6 docker-proxy.**
    - `git mv` the template, move the entry from `containers_list` to `compose_services` and
      delete `ansible/roles/containers/docker-proxy/`.
    - Legacy entry: `/home/ubuntu/server/containers/docker-proxy/docker-compose.yml`.
    - compose-adopt removes `docker-proxy` and `docker-proxy-lifecycle`, and `up` recreates
      both. The recreate gives each proxy the live socket inode, which is the fix
      `ansible/roles/containers/docker-proxy/CLAUDE.md` prescribes. For those seconds,
      autoheal and alloy retry against a missing proxy.
    - Replace the `PI_ORDERING` test. The replacement asserts that an upstream moves no later
      than its downstream, and that a downstream inside the project carries `depends_on` on
      its upstream.
    - Change the `has_gitops: false` comment ("No GitOps auto-deploy path to the Pi") to name
      the package path. Keep its sentence on why the key stays explicit.
    - **The facts lock**, until G2 retires `docs/facts.lock`. Edit
      `ansible/roles/setup/docker_install/CLAUDE.md#Notable`, which cites the deleted
      docker-proxy doc, and run
      `uv run python scripts/dev/fact_status.py verify 'ansible/roles/setup/docker_install/CLAUDE.md#Notable'`.
      docker-proxy's own `CLAUDE.md` has no lock row.
7. **H5.7 autoheal.**
    - Same shape. Add `depends_on: docker-proxy-lifecycle`.
    - Legacy entry: `/home/ubuntu/server/containers/autoheal/docker-compose.yml`.
    - **Operator-only:** run `host_secrets.py sync` and commit the Pi file with
      `monitor_discord_webhook_url`.
    - `docs/secret-rotation.md`'s external-tier procedure gains the operator-only `sync` step
      for a key the Pi file carries.
    - **The rotation consumer census.** This PR moves the first secret consumer out of
      `ansible/`. `consumers.py:_census_corpus` gains a second walk root, `hosts/components/`,
      and maps a hit there to its host package rather than to a role. For such a consumer,
      `consumer_commands` emits the operator-only `host_secrets.py sync` command and the
      package's path, wrapped in backticks as its other commands are. A named-member test
      asserts
      that `tree_consumers('monitor_discord_webhook_url')` still names the Pi package after
      the move. H5.8 adds `alloy_pi_http_password` to that test.
    - **The facts lock**, until G2 retires it: run `fact_status.py forget` for
      `ansible/roles/containers/autoheal/CLAUDE.md#At a glance` and
      `ansible/roles/containers/autoheal/CLAUDE.md#Notable`.
    - For the seconds between remove and create, no container restarts on an unhealthy check.
8. **H5.8 alloy.**
    - **Operator-only, before merge:** the operator approves or declines the password's move
      into alloy's environment (*Open questions* 6). If the operator declines, this PR does
      not merge, and the file route in *Secrets* replaces it.
    - Same shape. Add `depends_on: docker-proxy`, the `sys.env` config, the plain
      `config.alloy` file and its label.
    - `HOMELAB_DOMAIN` renders from the plain variable `domain` at build time, not from
      `compose.env`, which carries only SOPS keys (*Open questions* 7, the 2026-10-24 ruling).
      Only `alloy_pi_http_password` goes through `compose.env`.
    - Legacy entries: `/home/ubuntu/server/containers/alloy/docker-compose.yml` and
      `/home/ubuntu/server/containers/alloy/config/config.alloy`.
    - **Operator-only:** run `host_secrets.py sync` and commit the Pi file with
      `alloy_pi_http_password`.
    - `docs/secret-rotation.md`'s assisted-tier procedure gains the same `sync` step.
    - The consumer census test gains `tree_consumers('alloy_pi_http_password')`, which must
      name the Pi package.
    - **The facts lock**, until G2 retires it: run `fact_status.py forget` for
      `ansible/roles/containers/alloy/CLAUDE.md#At a glance` and
      `ansible/roles/containers/alloy/CLAUDE.md#Notable`.
    - **Ordering against D5's crowdsec conversion.** If D5's PR has added the Alloy metrics
      stage, this PR moves it with the template, and the tooling test asserts it by D5's
      label. If this PR lands first, D5 edits the component's template instead (*Rows H5
      takes from D5's `k8s_host_files`*).
    - **When D5 landed first, this PR is the one that lands second,** so it carries D5's
      check. After the Pi installs the package, an agent reads the stage's counter on
      `job="alloy-pi"`, by the series name D5's PR gives it, through the Prometheus transport
      in *Verification*. The PR body quotes the result. A read with no sample fails the step.
      D5 names `probe.py metric` for this read, which decrypts `domain`
      (`scripts/diagnostics/probe_lib/core.py:k8s_endpoint`), so that form is the
      operator's. *Open questions* 5 asks D5 to accept the agent transport.
9. **H5.9 wg-easy.**
    - Same shape as autoheal. wg-easy reads the plain variable `domain`, which renders into its compose file at build time and not through `compose.env` (*Open questions* 7, the 2026-10-24 ruling).
    - Legacy entry: `/home/ubuntu/server/containers/wg-easy/docker-compose.yml`.
    - **The facts lock**, until G2 retires it: run `fact_status.py forget` for
      `ansible/roles/containers/wg-easy/CLAUDE.md#Notable` and
      `ansible/roles/containers/wg-easy/CLAUDE.md#wg-easy — WireGuard VPN server + UI`.
    - **Timer hold.** H3.9 arms the Pi's updater timer every 15 minutes, so a merge alone moves
      wg-easy within about 17 minutes. The operator follows four steps from a LAN session:
        1. Run `sudo systemctl stop homelab-updater.timer` on the Pi before merging.
        2. After the builder publishes, run `sudo systemctl start homelab-updater.service`.
        3. Verify the tunnel and the container.
        4. Leave the timer to H3's reconcile.
    - Reconcile's step 2 starts every enabled unit that is not active. The hand-started
      install therefore re-arms the timer itself. The hold ends exactly at the attended run,
      which is when it stops being needed. A reboot also re-arms it, so the operator does not
      reboot between steps 1 and 2.
    - The recreate drops tunnels for the seconds `up --wait` takes. The peer configs in
      `./config` stay where pi-peer-backup reads them.
10. **H5.10 Remove `pi-canary`.** This is exit observation 3.
11. **H5.11 Track the Pi's images.**
    - **Operator-only:** before this PR merges, the operator confirms *Open questions* 1.
    - Add a custom manager over the `image:` lines in
      `hosts/components/pi-compose/services/*/compose.yaml.j2`.
    - Give the manager an `autoReplaceStringTemplate` that keeps the matched `image: ` prefix
      and writes `{{#if newDigest}}@{{{newDigest}}}{{/if}}` after the version.
    - Add packageRules between the catch-all and the pinDigests rule, so the catch-all cannot
      group or automerge these updates and the pinDigests rule stays last:
        - one group per Pi service;
        - `automerge: false` for wg-easy, in a group named `manual`, as the Docker engine's is;
        - one lockstep group for `grafana/alloy` that joins the Pi's pin with
          `ansible/roles/k8s/loki-homelab/defaults/main.yml`, with `automerge: false`;
        - autoheal and docker-proxy digests in their own groups, automerged only if the
          operator chose that in *Open questions* 1.
    - **Add the Pi's template glob to the pinDigests rule's `matchFileNames`,** and change
      the sentence in its description that limits the template to the k8s-images manager.
      Without that rule, Renovate's regex manager offers a version bump with no
      `newDigest`. alloy and wg-easy pin a tag and a digest, and Docker pulls by the digest,
      so a bump that moves only the tag changes nothing on the Pi.
    - Rewrite `test_no_custom_manager_tracks_the_retired_compose_plane` to forbid a manager
      over `roles/containers` only, and record the operator's decision in its docstring.
12. **H5.12 Retire the Ansible Compose path, Ansible half.**
    - This PR carries the ADR-0013 amendment's text, which the operator decided at H5.2b
      (*Open questions* 7). It covers the package path that replaces `-e target=daniel-pi`
      and the retirement of `ansible/roles/containers/`. Approving the ADR text is
      **operator-only**, before merge.
    - Delete `ansible/roles/containers/common/` and `ansible/roles/containers` from
      `ansible.cfg`'s `roles_path`. Delete `engine-upgrade.yml`'s per-project loop and its
      `has_repo_checkout` guard.
    - Drop the `roles/containers` halves that H5.2b kept in `compose_templates.py`,
      `decisions.py`, `pins.py` and the infra map's `load_roles`. Drop the Pi-compose role
      shape from
      `scripts/docs/gen_role_glance.py`.
    - Tests: `ansible/tests/deploy/test_containers_list_roles_exist.py` loses the Pi half, and
      `test_platform_filter_real_inventory.py` pins the Pi's list empty, as
      `test_daniel_server_is_fully_drained` does for daniel-server.
    - Docs in the same PR (listed under *Tests and docs*).
13. **H5.13 Retire the deployer's Pi Docker arms.**
    - Delete `_PI_SHARED_PREFIX`, `cs.pi_shared`, the `containers` arm of
      `services_from_changed_paths` and `log_pi_changes`.
    - Delete the `roles/containers` entry in `narrow_lib/broad.py:ROLE_TREES` and the
      `roles/containers` arms in `land_lib/land_platform.py`, `land_tags.py` and
      `shared_role_reach.py`, with their tests.
    - Landing: a setup-plane change to `gitops_deploy`. The tick applies it on daniel-box.
14. **H5.14 Take D5's probe timer on the Pi.** This PR runs only when D5's crowdsec
    conversion has installed the timer through `k8s_host_files`. It needs H4's package
    machinery but none of H5.2 to H5.13, so it can land at any point after that D5 PR.
    - Add the `crowdsec-probe` component to the Pi package: the probe script, and
      `homelab-crowdsec-probe.service` and `.timer` with the timer's enable link. The service
      writes under `/var/log/pi-health/` through `LogsDirectory=pi-health`, as the
      `pi-heartbeats` units do. It reads no secret.
    - The service sets `User={{ sys_user }}`, the user H3.8b gives the heartbeat services.
      systemd re-owns a `LogsDirectory=` tree to the starting unit's user when the owner
      differs, so a root probe that starts every minute would take `/var/log/pi-health/`
      from the heartbeat writers. A census over the Pi package asserts that every unit
      declaring `LogsDirectory=pi-health` declares the same `User=`. Once H5.2a1 ships the
      tmpfiles line for that directory, the census also holds the line's owner to that
      user.
    - Extend H3's two-writer census to attribute a task with `delegate_to: <host>` to that
      host, not to the host its playbook entry runs on. Without that, the census reads the
      probe timer's tasks as daniel-box's and never compares them with the Pi package. The
      census's named member on the Ansible side is the probe timer's task. *Open questions*
      5 records this as a note to H3.
    - The component's header carries the autonomous contract that `k8s_host_files`'
      `CLAUDE.md` carries for the timer.
    - Legacy entries: the Ansible-era timer and service units and the script path, as D5's
      conversion PR names them.
    - Delete the timer's `delegate_to: daniel-pi` tasks from `k8s_host_files`. Two-writer
      check: the same PR deletes the Ansible writer and adds the package writer, as H3's
      *host handoff rule* requires.
    - Landing: the `k8s_host_files` edit is a setup-plane change, which the tick applies on
      daniel-box. The package installs the timer, and reconcile retires the legacy units
      once it has started.

## Verification

The checks split as H3's *Verification* splits them. An agent runs the Prometheus block,
through the loopback-then-ClusterIP transport of the land host wait. The fallback address is
`ansible/roles/k8s/observability/defaults/main.yml:observability_prometheus_cluster_ip`.
`probe.py metric` decrypts `domain`, so it is the operator's. The operator runs every `ssh
daniel-pi` block and every command on the Pi, because the `claude` user has no login there
and `sudo` there asks for a password. Each ssh call is one batched, read-only command, which
keeps it under the ssh rate limiter.

```bash
# Agent: the compose result, the alloy target and (H5.2b on) the canary's run.
Q='{__name__=~"homelab_compose_up_(result|timestamp_seconds|duration_seconds)",host="daniel-pi"} or up{job="alloy-pi"}'
curl -s --max-time 5 http://127.0.0.1:9090/api/v1/query --data-urlencode "query=$Q" \
  || curl -s --max-time 5 http://10.43.39.218:9090/api/v1/query --data-urlencode "query=$Q"

# Agent, H5.4: the run's highest 5-minute memory PSI `full`. <window> is the run's duration
# plus 5m, rounded up to whole minutes, and <end> is its end timestamp. The same form
# reads the swap-in rate, and min_over_time reads MemAvailable.
Q='max_over_time(rate(node_pressure_memory_stalled_seconds_total{job="node-pi"}[5m])[<window>:1m] @ <end>)'
curl -s --max-time 5 http://127.0.0.1:9090/api/v1/query --data-urlencode "query=$Q" \
  || curl -s --max-time 5 http://10.43.39.218:9090/api/v1/query --data-urlencode "query=$Q"

# Agent: the readers did not move.
uv run pytest ansible/tests/services/test_pi_publishing_containers.py

# Agent, exit observation 2: a Renovate digest bump merged.
gh pr list --author app/renovate --search 'hosts/components/pi-compose' --state merged --limit 1
```

```bash
# Operator, after each move PR: one batched read-only call. `sudo` is needed because the
# project reads the root-only compose.env. `config --services` prints service names only,
# never an interpolated value.
ssh daniel-pi 'dpkg-query -W homelab-daniel-pi; docker compose ls --format json; sudo docker compose -f /usr/lib/homelab/compose/compose.yaml config --services | sort; docker ps --format "{{.Names}} {{.Label \"com.docker.compose.project\"}} {{.Status}}" | sort; cat /var/lib/node-exporter-textfile/homelab_compose.prom; journalctl -t compose-adopt --since -1h --no-pager | tail -n 5'

# Operator, H5.2b: the canary received the build-time `domain` (prints 1, never the value).
# This does not prove compose.env; H5.7's autoheal check does (*Open questions* 4).
ssh daniel-pi 'docker inspect pi-canary --format "{{range .Config.Env}}{{println .}}{{end}}" | grep -c "^HOMELAB_DOMAIN=."'

# Operator, exit observation 2: the bumped image reached the Pi.
ssh daniel-pi 'docker inspect autoheal --format "{{.Config.Image}}"'

# Operator, exit observation 3: removal removes the container.
ssh daniel-pi 'docker ps -a --filter name=pi-canary --format "{{.Names}}"'

# Operator, H5.14: the package owns the probe timer, and its last run succeeded as ubuntu.
ssh daniel-pi 'dpkg -S /usr/lib/systemd/system/homelab-crowdsec-probe.timer; systemctl show homelab-crowdsec-probe.service -p ExecMainExitTimestamp -p Result -p User; stat -c "%U %n" /var/log/pi-health'
```

The expected results are:

- `homelab_compose_up_result{host="daniel-pi"}` is 1 at the package's SHA, and
  `up{job="alloy-pi"}` is 1;
- each H5.4 query returns a sample, and the figures sit below the H5.4 threshold or the
  updater moves to once a day;
- the publishing test passes unchanged;
- one project, `homelab`;
- the declared container names equal the running names, every one labelled `homelab`, and
  `Up (healthy)` where it has a healthcheck;
- the canary check prints 1;
- the inspected image carries the merged digest;
- the canary query prints nothing;
- `dpkg -S` names `homelab-daniel-pi`, the probe's last run reads `Result=success` with an
  exit time inside the last two minutes and `User=ubuntu`, and `/var/log/pi-health` stays
  owned by `ubuntu`.

**Red proofs for the checks this slice adds.**

- *List census.*
    - A fixture with `autoheal` in both keys is flagged.
    - A `compose_services` entry with no legacy entry is flagged.
    - A fixture reader that reads `containers_list` alone renders differently after a move,
      and is flagged, through each of the two renders.
    - The census asserts it read both keys and found `docker-proxy` and `alloy`, and that
      each render produced monitor-bridge's `PI_PUBLISHED_PORTS`.
- *Infra map declared set (H5.1).* With `alloy` moved from `containers_list` to
  `compose_services` in a fixture Pi host_vars, `declared_services` for daniel-pi still
  returns a record named `alloy`. A fixture `declared_services` that reads
  `containers_list` alone is flagged by that case. A fixture Pi host_vars without
  `compose_services` raises, as the helper does for every other caller.
- *Recovery watch set.* With `autoheal` moved to `compose_services`, the rendered set still
  names autoheal. A derivation over `containers_list` alone is flagged. `MUST_WATCH` stays as
  it is at `70591c307`.
- *Recovery heartbeat, compose arm (H5.2b).* Run against a stub `docker` with a fixture
  textfile directory: a `homelab_compose_up_result` of 0 makes the script push `down`
  naming `homelab-compose`. A result of 1, or no file, adds nothing to the message. A file
  at mode 0600 owned by root makes it push `down` naming the unreadable file. A fixture
  script without the arm is flagged by the first case, and one that treats an unreadable
  file as absent by the third.
- *Project census (H5.2a1).* Over the rendered project:
    - every `depends_on` target is a declared service. A fixture where autoheal depends on
      `docker-proxy-lifecycle` and the project does not declare it is flagged;
    - every service's `container_name` equals its key. A fixture with `container_name:
      autoheal-1` under `autoheal` is flagged;
    - every `image:` carries an `@sha256:` digest, which compose-run's missing-image pull
      relies on. A fixture with a tag-only image is flagged;
    - the census asserts it found `docker-proxy-lifecycle` and `alloy` by name once they
      have moved.
- *compose-adopt.* A unit test drives it against a stub `docker`:
    - a same-name container from project `autoheal` is removed;
    - a same-name container already in `homelab` is left alone;
    - a container whose name the file does not declare is never touched;
    - the stub's argv log shows `config` only with `--services` or `-q`. A fixture
      compose-adopt that runs a bare `config` is flagged.
- *compose-run.* Against a stub `docker` that logs its argv:
    - an env file missing a referenced key, or holding an empty value, writes 0 and exits
      non-zero, and the log holds no `rm -f` and no `up`;
    - a failed `config -q`, and separately a failed `pull` of a missing image, writes 0, and
      the log holds no `rm -f` and no `up`;
    - with every image present, the log holds no `pull`. With one image absent, it holds one
      `pull` naming that image only;
    - a fixture compose-run that runs compose-adopt before the key check is flagged by the
      first case;
    - a compose file carrying C1's `$${literal}` escape adds no key named `literal` to the
      checked set;
    - a failed `up` writes 0 and exits non-zero;
    - a good `up` writes 1, with a duration, in a file at mode 0644. Its `sha` label is the
      hex after `+g` in a stub `dpkg-query` version, and the fixture `manifest.json` holds
      no SHA. A fixture compose-run that reads the manifest writes no `sha` label, or the
      wrong one, and is flagged. A stub version without `+g` writes `sha="unknown"`;
    - the script's output never contains a fixture secret value.
- *Unit timeout.* A test renders the unit with the Pi's inventory and asserts that
  `HOMELAB_COMPOSE_WAIT_TIMEOUT` is at least the largest `start_period` plus
  `container_healthcheck_retries` × (`container_healthcheck_interval` +
  `container_healthcheck_timeout`), and that `TimeoutStartSec` is at least that timeout plus
  600 s and below reconcile's 30 minutes. A fixture unit with `--wait-timeout 600` is
  flagged.
- *Single substitution.*
    - A test renders autoheal's and wg-easy's compose files and asserts that
      `${monitor_discord_webhook_url}` and `${domain}` survive rendering.
    - The env template carries each key, single-quoted.
    - A fixture value holding `'` is refused. A fixture value holding `$HOME` and ` #` reaches
      the stub's environment unchanged.
    - A compose file routed through `homelab-secrets` is flagged, and so is a compose file
      with a bare `$name`.
- *Secret dependents.* The rendered `secrets.json` names `homelab-compose.service` as the
  dependent of `compose.env`. A fixture without it is flagged.
- *Restart contract,* in `hosts/lib/tests/` beside H4's reconcile tests.
    - Changing one line of any one included compose file, or of `config.alloy.j2`, changes
      the rendered unit file's inputs line. A render with no change leaves the unit
      byte-identical. A fixture renderer that omits the line is flagged.
    - A stub reconcile run, with `pending.json` listing `homelab-compose.service` and the
      unit failed, logs `restart homelab-secrets.service`, then `reset-failed
      homelab-compose.service`, then `restart homelab-compose.service`, once each. The log
      holds no `try-restart`, and `pending.json` no longer lists the unit afterwards. A
      fixture log that restarts `homelab-compose` before `homelab-secrets` returns is
      flagged.
    - With the unit neither enabled nor active, as in H5.2a, the log holds no call on it.
    - A stub `restart homelab-compose.service` that exits non-zero makes reconcile exit
      non-zero, and `pending.json` still lists the unit.
    - `compose-run` and `compose-adopt` contain no `systemctl` call.
- *alloy.*
    - The rendered `config.alloy` holds no C1 placeholder. A fixture carrying
      `${alloy_pi_http_password}` in it is flagged.
    - Changing one line of `config.alloy.j2` changes the rendered label.
    - Changing only the password's value changes alloy's rendered `environment:` in the
      Compose model, which is what `up -d` compares.
- *Bind sources.* Every bind source in the rendered project is a package path, a directory
  the tmpfiles file lists, or `/var/run/docker.sock`. Each one carries `create_host_path:
  false`. A fixture source under `/run/homelab-secrets/` is flagged, and so is a relative
  source with no tmpfiles line. The test asserts it found `alloy/data` and `wg-easy/config`.
- *Memory floor (H5.2b follow-up).* The Docker daemon refuses a memory limit or reservation
  under 6 MiB at `up`, though the Compose model and `config -q` accept it. The canary's first
  run failed on a 2M reservation (2026-10-24). Every compose file the package installs is read
  for `deploy.resources.{limits,reservations}.memory`, `mem_limit` and `mem_reservation`. A
  fixture reservation of 2M, a limit of 5M or 512k, and an unreadable size are flagged, and 6M
  with a 16M limit passes. The test asserts it found the canary's own 16M and 6M.
- *Engine upgrade.* A test parses `engine-upgrade.yml`. It asserts five things:
    - a task before the apt probe masks `homelab-updater.timer`, and a later task, still
      before any `state: stopped`, waits on `homelab-updater.service` and
      `homelab-reconcile.service`;
    - that wait's limit, the `docker_install` role default, exceeds the `TimeoutStartSec`
      of `homelab-updater.service` as H3's renderer renders it. A fixture default of 30
      minutes, reconcile's limit, is flagged;
    - a task before the apt upgrade stops the `homelab` project;
    - a task after the daemon start recreates it with `recreate: always` and `become: true`;
    - the last task unmasks and starts the timer, guarded by the recorded mask state.
  A fixture play without the mask task is flagged, and so is a fixture play that only runs
  `systemctl stop` and `systemctl start homelab-compose.service`.
- *Land wait.* A stub Prometheus returning `homelab_compose_up_result` 0 at the merge SHA is
  not settled. A 1 at an older SHA keeps waiting. A 1 at the merge SHA settles.
- *Renovate rules.*
    - A fixture Pi dependency that the catch-all would automerge is flagged. The real config
      puts wg-easy in a manual group and joins both alloy pins in one group.
    - `test_every_k8s_image_file_gets_pin_digests_from_a_package_rule`, or a sibling test,
      asserts that each Pi compose template is covered by the pinDigests rule, which stays
      `_PACKAGE_RULES[-1]`. A fixture Pi template outside the rule's `matchFileNames` is
      flagged.
    - A round-trip test like `test_the_k8s_image_template_round_trips_every_live_pin`
      asserts that the Pi manager's template reproduces every live Pi `image:` line, tag and
      digest, byte for byte. It names alloy's and wg-easy's lines as members.
- *Rotation consumer census (H5.7).* `tree_consumers('monitor_discord_webhook_url')` names the
  Pi package after H5.7, and `tree_consumers('alloy_pi_http_password')` names it after
  H5.8. A corpus walk that skips `hosts/components/` fails both by name.
- *Tooling scan.* `compose_templates.py`, `decisions.py`, `pins.py` and the infra map's
  `load_roles` each fail when their named member is missing from the component. From H5.8,
  when D5's crowdsec conversion has landed, the alloy test also fails when the rendered
  `config.alloy` lacks D5's metrics stage label.
- *Probe timer (H5.14).*
    - H3's two-writer census, extended to attribute `delegate_to: daniel-pi` tasks to the
      Pi, fails a fixture that keeps the timer's `delegate_to` task while the package ships
      the timer. A fixture census without the `delegate_to` rule passes that fixture, which
      is the proof the rule is needed. The census asserts that it found
      `homelab-crowdsec-probe.timer` in the Pi package and the probe timer's task in
      `k8s_host_files` by name.
    - The `LogsDirectory` census fails a fixture probe unit with no `User=`, and asserts that
      it found the three heartbeat services and `homelab-crowdsec-probe.service` by name.

## Rollback

**Order.** Revert H5.13, then H5.12, before reverting any move PR. A move PR's revert restores
a role that includes `common`, which H5.12 deletes. The move PRs then revert newest first:
H5.9, then H5.8, then H5.7, then H5.6. A downstream carries `depends_on` on its upstream, so
reverting H5.6 while H5.7 or H5.8 stays would leave autoheal or alloy depending on a service
the project no longer declares. Compose rejects that project, compose-run fails at its model
check, and the Pi holds. The project census fails the same revert in CI first. To revert an
upstream, revert its downstreams before it.

- **H5.1:** revert. No entry has moved, so only the readers change back.
- **H5.2a1 to H5.4:** revert, newest first. The canary goes, and dpkg deletes the unit and the
  scripts. A revert of H5.3 restores the per-project engine upgrade, which is correct while
  no service has moved.
- **H5.6 to H5.9:** stop the Pi's updater timer first (as in H5.9), then revert the move PR.
    - The builder publishes a version without the service.
    - If the move's own run failed, the Pi holds (*Restart ordering*). `--clear-hold` cannot
      pass while the installed package still declares the failing service, because it
      reruns the project. The operator therefore runs `sudo /usr/lib/homelab/homelab-updater
      --clear-hold --install-newest` from the LAN session (H3, *Host-plane holds and stop
      levers*). It installs the version without the service before reconcile runs.
    - When the Pi does not hold, the operator starts the updater by hand instead.
    - Either way, the inputs line changed, so reconcile restarts `homelab-compose`, and
      `up --remove-orphans` removes the `homelab` container. A failure there writes the
      hold again, and `HostReconcileFailed` reports it.
    - The operator then restores the old project from a LAN session with
      `./scripts/deploy.sh --tags "<svc>" -e target=daniel-pi`, using the role the revert
      restored.
    - Between those two steps the service is down.
    - For wg-easy that gap is the access path itself. The rollback runs from the LAN, with
      the cluster path proven in H5.5.
- **H5.10 and H5.11:** revert. The canary returns, or the Pi's images go untracked again.
- **H5.12 and H5.13:** revert, newest first. The roles and arms come back. No container moves,
  because every service stays in `compose_services`.
- **H5.14:** revert. The package drops the timer, and reconcile's step 1 stops it. The revert
  restores the `delegate_to` tasks, and the tick's apply of `k8s_host_files` reinstalls the
  Ansible-era timer. Between the two, the probe runs twice or not at all. A crowdsec verify
  that runs in a gap fails and names the evidence channel, as D5's red fixture requires.

**What a partial failure leaves live:**

- A failed `up --wait` leaves the unit failed and `homelab_compose_up_result` at 0, so
  `land.sh` does not report `settled`. Healthy services keep running. The unhealthy one stays
  in whatever state Compose left it.
    - When reconcile started the run, reconcile fails too. The updater holds the Pi, and
      `land.sh` reports `deploy-failed`. No later package reaches the Pi until the hold
      clears.
    - If compose-adopt had already removed its Ansible-era container, no container of that
      name exists.
    - The recovery heartbeat watches the union of both keys, so it still names the service.
      Its `docker start` fails on a missing container, and it pushes `down` naming the
      service, as `test_pi_recovery_restarts_and_reports.py` already asserts for a container
      that is gone. From H5.2b its compose arm also pushes `down` while the result reads 0.
    - After the cause is fixed, the operator runs `sudo /usr/lib/homelab/homelab-updater
      --clear-hold` from a LAN session. It reruns reconcile, whose step 2 starts the failed
      unit, and removes the hold when that run passes. With no hold, `sudo systemctl restart
      homelab-compose.service` reruns the project directly.
    - A cause inside the installed package cannot pass that rerun. The way out is a revert
      and H3's `--clear-hold --install-newest`, as the move PRs' rollback above describes.
- A failed key check, model check or pull stops before compose-adopt. Every container,
  Ansible-era ones included, keeps running as it was, and the result reads 0. This covers a
  stale `compose.env` left by a failed re-render.
- A compose-adopt that fails partway stops before `up`. A container it already removed is
  absent until a later run passes, and the recovery heartbeat names it as above.
- A failed engine-upgrade play leaves `homelab-updater.timer` masked, so the Pi installs no
  package until the operator clears it (*The engine upgrade*). If the play failed between
  the stop and the recreate, the stopped containers stay stopped until the play is rerun.

## Risks

- **Losing remote access.** wg-easy moves last, after H5.5 proves the cluster path. Its
  updater run is started by hand from the LAN, behind the timer hold. Peer data stays in
  place.
- **Failure class 3, empty read as clean.**
    - `compose-run` refuses a missing or empty env value instead of letting Compose substitute
      an empty string.
    - The readers drop `default([])`, so a missing key fails the render.
    - The land wait treats an absent compose result as not settled.
    - An H5.4 query that returns no sample fails the step rather than reading as below the
      threshold. The `[5m]` window keeps four or five samples in each rate.
- **Failure class 4, selector drift.**
    - Each tool keyed on `roles/containers` scans the component from H5.2b, with a named member.
    - The readers name both keys literally, so the narrowing grep still finds them.
    - The recovery watch set derives from both keys, and `MUST_WATCH` holds the members.
    - The rotation consumer census walks `hosts/components/` from H5.7, and names the Pi
      package for both moved secrets.
    - H3's two-writer census attributes a `delegate_to` task to its target host from H5.14.
- **Failure class 5, sequences treated as transactions.**
    - compose-adopt removes the old container only from inside the unit run that starts its
      replacement, and only after the key check, the model check and the pull have passed.
    - Ansible deletes nothing, so no ordering exists between two writers.
    - The engine upgrade handles both kinds of project from H5.3, before any service moves,
      so no PR leaves a moved service outside the stop and the recreate. It masks the
      updater timer first, so reconcile cannot restart `homelab-compose` during the play.
    - `compose.env` is re-rendered before `up` reads it because reconcile's secrets step
      waits for `homelab-secrets` before step 3 runs. The restart-contract test pins that
      order with a fixture log that breaks it. When that re-render fails, the stale file
      fails compose-run's key check before anything is removed.
    - Move PRs revert newest first, and the project census fails a `depends_on` on an
      undeclared service.
- **A failed compose run holds the Pi.** H4's restart waits for the run, so its failure
  fails reconcile, and the updater holds the host. That stops every later package, Renovate
  bumps included, until the operator clears the hold. This is the host-plane form of the
  trade `hold_sha` makes for the cluster, and `land.sh` reports it as `deploy-failed`.
  `--clear-hold` cannot pass while the installed package itself is the cause. H3's
  `--clear-hold --install-newest` clears it once the builder publishes the revert
  (*Rollback*). H3's `HostReconcileFailed` pages after 15 minutes at 0.
- **A failed run outside a landing raises little signal.** Three runs happen outside any
  landing: the boot run, a step-2 start after `--clear-hold`, and a restart from
  `pending.json` alone. `land.sh` reads `homelab_compose_up_result` only during a landing.
  The recovery heartbeat already pushes `down` for a declared container that is missing or
  exited. From H5.2b its compose arm also pushes `down` while the result reads 0, which
  covers a failed run that leaves every container running. H3's `HostReconcileFailed`
  (PR H3.5d, C7) pages for a Pi held with `homelab_host_reconcile_ok` at 0 for 15 minutes,
  whatever caused the hold. `ComposeUpStale` (#6187) pages a compose result older than the
  Pi's last boot, once the boot is older than the unit's `TimeoutStartSec` plus 300 s.
  `ComposeRestartUnreported` (#6342) pages a start or restart by reconcile that wrote no
  result. Reconcile records when it called the unit as `homelab_host_unit_started_seconds`,
  and the rule fires for 5 minutes while the compose result's timestamp predates that call.
  That catches a write that failed after a successful `up`, which exits 0 and leaves
  `HostReconcileFailed` quiet.
- **A compose run lengthens the Pi's reconcile.** One run takes up to the unit's derived
  `TimeoutStartSec`, 1390 s or about 23 minutes at `70591c307`'s healthcheck values. That is
  inside reconcile's `TimeoutStartSec=30min`, and the unit-timeout test holds the relation.
  H4's step 2 starts only a unit that is not in step 3's restart set, so one reconcile run
  makes at most one start or restart of `homelab-compose`. H3's `homelab-updater.service`
  allows 50 minutes, derived from reconcile's 30 plus its 10-minute lock wait and 10 minutes
  for the fetch and the unpack, so an updater run that includes a worst-case compose run
  still fits. H5.4 records the measured run duration.
- **A correct recreate could time out on a loaded Pi.** The wait timeout derives from the
  healthcheck variables with 120 s of slack (*The wait timeout*), so it moves when they
  move. A test holds the relation between the two timeouts.
- **Failure class 6, restart is not recreate.**
    - The engine upgrade force-recreates `homelab`, so a new shim or socket inode reaches every
      moved container.
    - alloy's config-hash label makes a config edit change the Compose model.
    - A secret rotation changes alloy's environment, so the restart is a recreate there too.
- **The boot race on `/run/homelab-secrets/`.** The fix that shipped removes the race rather
  than guarding it. alloy reads its secrets from its environment, so no bind source sits
  under `/run/homelab-secrets/`. Every bind also carries `create_host_path: false`, and a
  census asserts both. The one bind under `/run` is `/var/run/docker.sock`, which dockerd
  creates before it starts any container.
- **Memory on the Pi.**
    - compose-run pulls only an image the host lacks, so a run whose digests did not change
      makes no registry request. The project merge downloads nothing, because every image
      digest stays the same.
    - Unattended runs are the change. H5.4 measures one before any real service moves, with
      host-level figures: memory PSI `full`, the minimum of `MemAvailable` and the swap-in
      rate. A pull runs in dockerd and containerd, so the unit's own cgroup would miss it.
    - `COMPOSE_PARALLEL_LIMIT=1` serialises the pulls, and the threshold moves the updater to
      once a day.
- **Rotation turns CI red.** H4's `check` fails while the Pi file is older than a key's
  `last_rotated`. H5's keys are not auto-tier, so H5.7 and H5.8 add the operator-only `sync`
  to their rotation procedures. *Open questions* 5 asks H4 to scope the failure.
- **alloy's password moves into the container environment.** The 0640 file at `70591c307`
  becomes an `Env` entry, with the exposure autoheal's webhook already has. The operator
  approves or declines that before H5.8 (*Open questions* 6), and the file route is the
  alternative.
- **Unattended image bumps on the backup access host.** Exit observation 2 reverses a
  standing decision. See *Open questions* 1.

## Tests and docs that change in the same PR

- **H5.1:**
    - the census;
    - `ansible/tests/setup/test_pi_recovery_restarts_and_reports.py`, whose expected set reads
      both keys while `MUST_WATCH` stays fixed;
    - `ansible/tests/services/test_pi_publishing_containers.py`'s helper, whose expected
      members stay unchanged;
    - `scripts/diagnostics/tests/test_probe_health.py` for `declared_on_pi`;
    - `scripts/infra_map/tests/test_gen_infra_map.py` for `declared_services` on daniel-pi,
      beside `test_declared_services_defaults_to_docker_platform`;
    - the `pi-heartbeats` component header, whose contract scope names both keys (H3.8b moved
      the contract there from `ansible/roles/setup/optimize_pi/CLAUDE.md`).
- **H5.2a1:** the single-substitution, secret-dependent, bind-source and project censuses.
- **H5.2a2:** the `homelab-secrets` tests for `format: dotenv`.
- **H5.2a3:** the compose-adopt, compose-run and unit-timeout tests, and the
  restart-contract tests in `hosts/lib/tests/`.
- **H5.2b:**
    - the tooling scan tests;
    - the recovery heartbeat's compose-arm test, and the `pi-heartbeats` component header,
      whose contract gains the read;
    - `docs/adr/0013-daniel-pi-stays-on-docker.md` is not edited yet. The operator's
      decision is recorded in the PR body, and H5.12 carries the ADR text.
- **H5.3:**
    - the engine-upgrade test, with its mask, wait-limit and release cases, and the
      land-wait tests;
    - `ansible/roles/setup/docker_install/CLAUDE.md`'s engine-upgrade paragraph and
      `docs/docker-engine-pins-and-runtime.md`, which name the `homelab` project and the
      updater-timer mask;
    - `docs/host-packages.md`, which lists the engine play as a user of H3's mask lever.
    - The renovate.json group name stays as it is, because "it stops every Compose project"
      remains true.
- **H5.4:** `docs/pi-host-tuning-record.md` gains the measured run.
- **H5.6 to H5.9:**
    - each deleted role's `CLAUDE.md` content moves into its service directory as comments,
      or into `docs/`;
    - any test that renders the role template retargets the component;
    - H5.6 replaces the `PI_ORDERING` test;
    - until G2 retires `docs/facts.lock`, each PR runs the lock commands its step names:
      `verify` on `ansible/roles/setup/docker_install/CLAUDE.md#Notable` in H5.6, and
      `forget` on the deleted role's rows in H5.7, H5.8 and H5.9;
    - H5.7 extends `scripts/secrets_mgmt/consumers.py` and its tests, and H5.8 adds the alloy
      member;
    - H5.7 and H5.8 change `docs/secret-rotation.md`;
    - H5.9 adds the access-path proof and the project move to
      `docs/wireguard-private-homelab-access.md`.
- **H5.11:** the Renovate rule tests in `scripts/tests/test_renovate_managers.py`, and the
  Pi template's round-trip test beside
  `scripts/tests/test_renovate_auto_replace.py:test_the_k8s_image_template_round_trips_every_live_pin`.
- **H5.12:**
    - root `CLAUDE.md`, in three places: the *roles/containers/ is now only the Pi* block, the
      Docker on daniel-pi line under *Adding a New Service*, and the table row on a config
      edit that does not recreate the container;
    - `.claude/rules/docker.md`;
    - the `new-container` skill, rewritten to add a `pi-compose` service;
    - the `deploy` and `add-secret` skills, and the reviewer agents that name
      `roles/containers`;
    - every page that `git grep -l 'roles/containers' -- docs ':!docs/archive'` lists, edited
      or marked `**HISTORY —`;
    - Until G2 retires `docs/facts.lock`, sweep what the move PRs left. Edit root
      `CLAUDE.md#Where to Look (task → start here)`, which cites the deleted
      `ansible/roles/containers/common/CLAUDE.md`, and run
      `uv run python scripts/dev/fact_status.py verify '<doc>#<heading>'` on it and on any
      other section that cites a path this PR deletes. Commit the lock. Once G2 has retired
      the lock, this step drops.
- **H5.13:** `ansible/roles/setup/gitops_deploy/CLAUDE.md` (the *Pi Docker changes are never
  auto-deployed* line and the contract's scope) and `docs/gitops-pipeline.md`.
- **H5.14:**
    - the two-writer census's `delegate_to` rule, with its named member and fixtures;
    - the `LogsDirectory` census and its fixture probe unit with no `User=`;
    - `k8s_host_files`' `CLAUDE.md`, whose contract scope drops the Pi timer;
    - `docs/host-packages.md`, which lists the Pi package's timers.

## Complexity delta

- **Added:**
    - one unit;
    - two scripts: `compose-run`, about 70 lines, and `compose-adopt`, about 60 lines;
    - an include renderer, an env template, a tmpfiles file, a config-hash label, the
      unit's inputs line and its derived timeouts;
    - one moved component, `crowdsec-probe`, when D5 has landed the timer;
    - one textfile metric, one land-wait condition and one read in the recovery heartbeat;
    - four tasks in the engine-upgrade play: the mask, the wait, and the unmask and start,
      plus one role default for the wait's limit;
    - the list census, the project census and the `LogsDirectory` census, plus one rule in
      H3's two-writer census and one walk root in the rotation consumer census;
    - one inventory key;
    - one Renovate manager with its rules, and one glob in the pinDigests rule.
- **Removed:**
    - five Ansible roles under `ansible/roles/containers/`;
    - the Pi's `containers_list` entries;
    - the engine upgrade's per-project loop;
    - the deployer's Pi Docker arms, `_PI_SHARED_PREFIX` among them;
    - `common_config_changed`;
    - `deploy.sh -e target=daniel-pi` as a deploy path;
    - the secret-bearing `config.alloy` file;
    - the manual-only status of every Pi image bump;
    - the probe timer's `delegate_to: daniel-pi` tasks in `k8s_host_files`.

## Open questions

1. **Should Pi image bumps auto-deploy?** Exit observation 2 needs a Renovate manager over
   the Pi's compose images. The docstring of
   `test_no_custom_manager_tracks_the_retired_compose_plane` records the operator's decision
   to leave them untracked, because the Pi had no deploy path. H5 removes that reason.
    - Resolution: the operator confirms before H5.11 merges (**operator-only**). If the
      operator declines, exit observation 2 becomes a hand-written digest bump through the
      same pipeline.
    - Either way, wg-easy's pin sits in a manual group, and the two alloy pins move together.
2. **When does C5's dependency rule reach Docker?** C5 said third-party sources such as
   Docker's ship in `homelab-repos`; the 2026-10-24 ruling puts each in the component of the host that uses it. H5 keeps the engine under `docker_install`, so H5 adds
   no `Depends: docker-ce`. Resolution: H7 moves the engine, and Docker's source goes to the `apt-docker` component of `homelab-daniel-pi`.
3. **Who owns pi-peer-backup's two writes on the Pi?** R4's open question 4 expects H5's
   package to list them. They are an authorized key and a forced-command wrapper, not Compose
   services, so H5 does not move them. D5 moves the two tasks into `k8s_host_files` with
   `delegate_to: daniel-pi`, "until H5's package lists them". Resolution: H7's Pi PRs move
   them, as H7.4's `pi-peer-backup-target` component already allows. Until then the k8s role
   writes them, or `k8s_host_files` once D5 has converted pi-peer-backup. *Open questions* 5
   asks D5 to name H7 instead of H5.
4. **What if the first secret-bearing move shows that `env_file:` on an include delivers
   nothing?** The canary no longer carries this proof (*Open questions* 7, the 2026-10-24
   ruling). H5.7 is the first move that carries a real secret (autoheal's
   `monitor_discord_webhook_url`), so H5.7 carries it. H5.7 stops before its container is
   removed, and the other services have not moved. Resolution: the project renders as one merged file with no
   `include:`. Its relative paths are then written as absolute paths under each service's
   data directory.
5. **Behaviour this plan needs from other slices.** H5 adopts H4's restart contract as
   written, so it asks for no restart behaviour of its own. The items below are notes to the
   owning slices:
    - **H3.**
        - Resolved in the 2026-10-12 fold: H3 names `--clear-hold --install-newest`, the
          lever that installs over a hold whose cause is the installed package. H5's move
          rollback uses it.
        - The engine-upgrade play's wait reads the `TimeoutStartSec` of H3's rendered
          `homelab-updater.service` (*The engine upgrade*). A change to that limit turns
          H5's engine-upgrade test red until the play's limit follows it. H3 needs no
          change for this.
        - Its two-writer census attributes a task to the host its playbook entry runs on.
          H5.14 extends it to attribute a `delegate_to: <host>` task to that host, and H3's
          census text should say so.
        - The engine-upgrade play uses H3's per-host lever, `systemctl mask --now
          homelab-updater.timer`, from H5.3. *Host-plane holds and stop levers* and the
          runbook in `docs/host-packages.md` should name that use.
    - **H4.**
        - Its routing rule exempts `/usr/lib/homelab/compose/`.
        - `host_secrets.py check` could scope its date failure to PRs that touch the Pi file
          or `ansible/secret_rotation.yml`.
        - *An owed restart survives a failed run* under *The restart contract*, and the
          `homelab-compose` bullet in H4's *Open questions*, cite a 15-minute
          `homelab-compose` restart. H5 derives the unit's `TimeoutStartSec` as 1390 s,
          about 23 minutes at `70591c307` (*The wait timeout*). H4 should cite the unit's
          derived `TimeoutStartSec` instead of a fixed figure. One such call still fits
          reconcile's 30 minutes, so H4's argument holds unchanged.
    - **H7.**
        - *The Docker engine is held* says H5.7 changes `engine-upgrade.yml`. H5.3 does, and
          it recreates `homelab` through `docker_compose_v2` with `recreate: always`, not by
          restarting `homelab-compose.service`.
        - H7.5's engine unit starts `homelab-compose.service` and expects a recreate. A start
          runs `up -d`, which recreates only services whose config changed, so the unit must
          run `docker compose -f /usr/lib/homelab/compose/compose.yaml up -d --force-recreate
          --wait` itself.
        - H7.5's unit needs the same hold as H5.3's play: mask `homelab-updater.timer`, wait
          for the updater and reconcile to be inactive, and release the hold only after the
          recreate succeeds. Its wait's limit should exceed the updater unit's
          `TimeoutStartSec`, as H5.3's play's does, not reconcile's.
    - **C4.** *Removal* says H7 deletes the Pi's `containers_list` key in the PR that deletes
      "`ansible/preflight.yml` or the Ansible Docker play, whichever goes last". H5.1 makes
      every reader of the union strict, so deleting the key alone fails every render that
      reads it. C4 should name H7.20d, after H7.20a, as the one PR that deletes the Pi's key
      and `_example.yml`'s and repoints every reader at `compose_services` alone: the three
      cluster templates, `scripts/lib/ansible_inventory.py`'s helper,
      `health_docker.py:declared_on_pi`, `render_context.py:_HOSTVARS_KEYS` and the
      `pi-heartbeats` watch set. H7's step H7.20d already reads that way.
    - **C1.** C1 says stub mode "exists only for R1's shadow comparison". H5's list census
      renders the three cluster readers with `--secret-stubs` until R2 lands. C1 should
      allow a test-only stub render that publishes nothing, or H5's census waits for R2's
      placeholder mode.
    - **D5.**
        - pi-peer-backup's Pi writes stay in `k8s_host_files` until H7, not H5.
        - When H5.8 has landed first, D5's crowdsec PR edits the component's
          `config.alloy.j2` and adds the probe timer to the Pi package. That unit sets
          `User={{ sys_user }}` and passes H5's `LogsDirectory` census.
        - D5 names `probe.py metric` for the counter quote, which decrypts `domain` and is
          therefore the operator's. An agent can read the same series through the Prometheus
          loopback-then-ClusterIP transport. D5 should accept either.
    - **M1.** Ask for two rules, each labelled `service="daniel-pi"` per C7:
      `homelab_compose_up_result == 0`, and a stale `homelab_compose_up_timestamp_seconds`
      with a bound M1 derives from the updater's schedule. H3's `HostReconcileFailed`
      (H3.5d) covers `homelab_host_reconcile_ok == 0`. `ComposeUpFailed` (#6166) is the
      first, in shadow while the recovery heartbeat's compose arm pages the same 0.
      `ComposeUpStale` (#6187) is the second. Its bound comes from boot, not from the
      updater's schedule: reconcile restarts the unit only when the unit's file changes, so
      a package change that leaves the project alone writes no result. Every boot does run
      the enabled unit, and its `TimeoutStartSec` caps that run.
      `hosts/lib/tests/test_pi_compose_unit.py` pins the rule's literals to the rendered
      unit.
    - **G2.** G2 says H5.7 adds a Renovate manager. H5.11 adds the Pi's.

   Resolution: each owning slice confirms the item or amends its plan, before H5.2a1. The
   D5 item resolves before D5's crowdsec conversion. The M1 item resolves when M1 ships the
   rules, and the recovery heartbeat's compose arm retires with them.
6. **Does alloy's password sit in its container environment?** H5.8 moves it from a 0640
   file into the `Env` that the read-only proxy returns (*Secrets*).
    - Resolution: the operator approves or declines before H5.8 merges (**operator-only**).
      An approval is recorded in the H5.8 PR body, and in `docs/secret-rotation.md`'s entry
      for the key.
    - If the operator declines, H5.8 waits for the file route that *Secrets* describes. That
      route first needs a fix for the rotation recreate and a CI proof that Alloy's `http`
      block reads a `local.file` export.
7. **Does the operator accept the ADR-0013 amendment?**
   `docs/adr/0013-daniel-pi-stays-on-docker.md` decides that the Pi is driven over SSH with
   `-e target=daniel-pi`, and H5.6 to H5.9 replace that path.
    - Resolution: the operator decides before H5.2b merges (**operator-only**), because H5.2b
      is the first PR that runs a service through the package. H5.12 carries the ADR text.
    - **Ruled 2026-10-24: the operator accepted the amendment**, so H5.2b may merge. H5.2b's
      PR body records the ruling, and `docs/adr/0013-daniel-pi-stays-on-docker.md` stays
      unedited until H5.12.
    - **Ruled 2026-10-24, on finding that `domain` is no SOPS key** (#5087 made it a plain
      variable in `group_vars/all.yml`, so `keys: [domain]` is refused as "not keys of
      secrets.yml"). The operator chose that the canary reads no secret:
        1. `pi-canary` has no `env_file:`, and `pi-compose` declares no `outputs:` entry for
           `compose.env` in H5.2b. No `host_secrets.py sync` runs, and the Pi's SOPS file is
           untouched.
        2. A plain variable such as `domain` renders into the compose file at package build
           time. It does not go through `compose.env`, which carries only SOPS keys.
        3. The proof that `env_file:` on an include delivers a value moves to the first move
           that carries a real secret, which is H5.7 (autoheal, `monitor_discord_webhook_url`).
           H5.8 and H5.9 read `domain` as a plain variable.
    - Had the operator declined, H5.2b would not merge, H5.2a3 to H5.2a1 would revert, and no
      service would have moved.

## Review log

### Round 1 (per-slice adversarial review)

All 21 findings were checked against the tree at `70591c307`, and all 21 hold. None is
refuted. Two design changes go beyond the findings:

- docker-proxy now moves first. One stop and recreate order then holds in every state of the
  engine upgrade, and `depends_on` replaces list order.
- alloy reads its secrets through `sys.env`. That removes the `/run` bind mount, the
  secret-only recreate problem and the config file's ownership question together.

- **H5-R1-01 (critical)** The engine upgrade neither stops nor recreates the Pi's
  containers, and moved services fall out of its loop. *Fixed:*
    - H5.3 changes the play before any service moves, with `state: stopped` and `recreate:
      always` under `become: true`.
    - The test parses the play for the stop before apt and the recreate after, and a
      unit-only play is flagged.
    - `docker_install/CLAUDE.md` changes in H5.3. The renovate.json group name stays, because
      it remains true.
- **H5-R1-02 (high)** alloy's config bind from `/run` races `homelab-secrets` at boot.
  *Fixed:* alloy reads its secrets from its environment, so no bind source sits under
  `/run/homelab-secrets/`. Every bind carries `create_host_path: false`, and a census
  asserts both. *Risks*
  states which fix shipped.
- **H5-R1-03 (high)** Each move PR dropped the moved containers from the recovery watch set.
  *Fixed:*
    - H5.1 derives the set from both keys.
    - `MUST_WATCH` stays fixed across the moves.
    - The component's contract header names both keys.
    - The partial-failure text now describes the `docker start` failure on a missing
      container.
- **H5-R1-04 (high)** Prometheus is one of four readers. *Fixed:*
    - The Baseline names all four readers and `_HOSTVARS_KEYS`.
    - H5.1 repoints them at both keys.
    - `EXPECTED_PUBLISHERS` stays unchanged.
    - artifacts drops `default([])`, and *Risks* lists class 3.
- **H5-R1-05 (high)** `pi_compose_services` existed only in the renderer. *Fixed:* the key
  lives in `ansible/inventory/host_vars/daniel-pi.yml`, which Ansible and the renderer both
  read. The census renders the readers through the test render context before and after each
  move.
- **H5-R1-06 (high)** Deleting the `Deploy Containers` play deletes the k8s play's snapshot.
  *Fixed:* H5 does not edit `ansible/deploy.yml`. The Docker play stays, inert, until R4 or
  H7 deletes the file's last play.
- **H5-R1-07 (high)** `domain` was a third secret key. *Fixed:*
    - The Baseline lists three keys with their tiers, and the grep that found no others.
    - H5.2b's canary brings `domain` into the Pi file.
    - The single-substitution test renders wg-easy's `${domain}`.
- **H5-R1-08 (high)** ADR-0013 changes, and its amendment was not operator-only. *Fixed:*
    - H5.12 carries an **operator-only** ADR-0013 decision before merge.
    - *Open questions* 1 is **operator-only**.
    - H5.1 and H5.6 update the `has_docker` and `has_gitops` comments.
- **H5-R1-09 (high)** The Pi runs 5 containers, not 6. *Fixed:* exit observation 1 compares
  the declared set with the running set, and states 5. The Baseline says
  `docker-proxy-codeserver` does not render.
- **H5-R1-10 (high)** The wg-easy move and its rollback assumed hand-started updates.
  *Fixed:*
    - H5.9 and the rollback stop the timer before merging.
    - The plan states that reconcile re-arms the timer at the attended install, and that a
      reboot also re-arms it.
    - The rollback reverts H5.13 and H5.12 first.
- **H5-R1-11 (medium)** A secret-only alloy change never recreated alloy, and nothing ordered
  the re-render. *Fixed:*
    - The password is in alloy's environment, so a rotation recreates alloy through
      `compose.env`'s dependent.
    - `secrets.json` names `homelab-compose.service`.
    - `--no-block` and reconcile's single call order the restarts.
    - A test asserts that a changed value changes alloy's environment.
- **H5-R1-12 (medium)** Legacy entries were incomplete. *Fixed:* each move PR lists its entries
  by path, including alloy's `config/config.alloy`. The census requires a legacy entry for each
  service.
- **H5-R1-13 (medium)** Nothing created bind-source directories after H5.12. *Fixed:* a tmpfiles
  file runs from `ExecStartPre=`, and a census matches it to the relative bind sources.
- **H5-R1-14 (medium)** The plan did not say what the deployer and `land.sh` do. *Fixed:*
    - *Landing a move PR* names the tick arm, the alerts, the Pi path and the verdict.
    - Each other PR names its landing.
    - H5.13 retires the Pi Docker arms with their tests.
- **H5-R1-15 (medium)** The Renovate manager fell into the catch-all automerge group.
  *Fixed:* H5.11 adds per-service rules after the catch-all, a required manual group for
  wg-easy, an alloy lockstep group, and red and green tests.
- **H5-R1-16 (medium)** A rotation of a non-auto key turns `check` red. *Fixed:*
    - H5.7 and H5.8 add the operator-only `sync` to `docs/secret-rotation.md`.
    - `domain` is tier `ignore`.
    - *Open questions* 5 asks H4 to scope the failure.
- **H5-R1-17 (medium)** Tooling keyed on `roles/containers` stopped covering moved templates.
  *Fixed:* H5.2b points `compose_templates.py`, `decisions.py` and `pins.py` at the component,
  each with a named member. *Risks* adds classes 3 and 4.
- **H5-R1-18 (medium)** H5.7 was far larger than one PR. *Fixed:*
    - The retirement is split into H5.10 to H5.13, with the Docker play left to R4 or H7.
    - The Pi's key stays empty until H7.
    - H5.12 names the `CLAUDE.md`, skill, rule and `docs/facts.lock` changes.
    - The setup work is split the same way: H5.1, H5.2a, H5.2b, H5.3 and H5.4. H5.5 keeps
      the number H7 cites.
- **H5-R1-19 (medium)** The cost of unattended compose runs was unmeasured. *Fixed:* H5.4
  measures a canary digest bump and sets a threshold. The unit sets
  `COMPOSE_PARALLEL_LIMIT=1`.
- **H5-R1-20 (medium)** A failed `homelab-compose.service` raised no signal. *Fixed:*
  `compose-run` writes a result metric on every outcome, and H5.3's land wait reads it. The
  alert rule waits for the alert plane (*Open questions* 5, note to M1, added in round 10).
- **H5-R1-21 (medium)** `--env-file` reaching included files was unproven, and the escaping
  claim was loose. *Fixed:*
    - Each include names `env_file:`, and the H5.2b canary asserts that a value arrives.
    - Values are single-quoted, and a `'` is refused.
    - A `$`-bearing fixture passes through unchanged.
    - The escaping text now says what Compose does with `$$` and a bare `$name`.

### Integration round (cross-slice review)

Four findings named H5. All four hold against the other slices' text, and H5 fixes its part
of each. None is refuted. Each change another slice must make is recorded in *Open
questions* 5.

- **X-09 (high)** Unit-restart ownership contradicted H4, so a failed compose unit was
  never restarted. *Fixed:*
    - H5 adopts H4's contract as written. `homelab-secrets` restarts nothing, and
      reconcile runs `reset-failed` and then `restart` on `homelab-compose`, once per run.
      H5 no longer asks H3 or H4 for `--no-block` or for one `systemctl` call.
    - The ordering argument is re-derived. Reconcile's secrets step waits for the
      `homelab-secrets` oneshot before steps 2 and 3 run, so `up` reads the re-rendered
      `compose.env`. `After=` orders the two only at boot.
    - Under H3 and H4, reconcile restarts a service only when its unit file, its drop-in or
      `changed.json` changes, and a compose file is none of these. The unit therefore gains
      an inputs line that carries the sha256 of the project's files, so a Renovate digest
      bump restarts it.
    - The blocking restart has two consequences, now stated. A failed run fails reconcile
      and holds the Pi, which the partial-failure text, the move rollback and two new
      *Risks* entries cover. A run also lengthens the Pi's reconcile.
    - New restart-contract tests in `hosts/lib/tests/` pin the call order, the absence of
      `try-restart`, the inactive-unit case and the failure propagation.
    - The `dpkg-divert` mismatch between H3 and H4 does not touch H5, which ships no
      diversion. It goes to H3 as a note.
- **X-14 (medium)** No slice took D5's Pi rows from `k8s_host_files`, and D5's Alloy stage
  raced H5.8. *Fixed:* a new Design section, *Rows H5 takes from D5's `k8s_host_files`*,
  lists the probe timer (H5.14), the Alloy metrics stage (H5.8) and pi-peer-backup's two
  writes (left to H7). It gives the ordering in both directions. H5.8 carries the ordering
  note, and *Open questions* 3 names `k8s_host_files` as the interim writer.
- **X-19 (medium)** Nobody removed the Pi's `containers_list`, and H5 deferred it to an H7
  that keeps the inventory. *Fixed:* the Goal, the service-list design and *What stays with
  Ansible until H7* no longer say that H7 deletes the inventory. `compose_services` stays in
  the inventory H7 keeps. The empty `containers_list` key goes to H7 as a named removal, and
  C4's *Removal* gets the Pi's owner.
- **X-21 (medium)** H5.12 committed `docs/facts.lock` with no condition. *Fixed:* the step
  now reads "Until G2 retires `docs/facts.lock`", in R4's wording, and drops once the lock
  is gone.

### Round 10 (per-slice adversarial review)

Raised 20 findings: 3 high and 17 medium. All 20 were checked against the tree at `70591c307`
and the other slices' text, and all 20 are fixed. No finding is refuted as a whole. Two carry
a sub-claim that does not hold, and each entry records the correction with its evidence.

- **H5-R10-01 (high)** compose-run removed the Ansible-era container before it checked
  `compose.env`, and the partial-failure text claimed the opposite. *Fixed:*
    - compose-run runs the key check, `config -q` and the pull before compose-adopt. The key
      set comes from the `${<key>}` spans in the installed files, never from interpolated
      output, and a `$${` escape adds no key. A failure in those steps writes 0 and removes
      nothing.
    - The pull fetches only an image the host lacks, so a run with unchanged digests makes no
      registry request, as `up -d` did not at `70591c307`. The project census asserts that
      every image is digest-pinned, which that rule needs.
    - The "missing file blocks the restart" bullet now states H4's gate as written: it
      covers a pending restart only. A hash-driven restart, a step-2 start and the boot path
      pass no gate, and a failed re-render leaves the old file in place. compose-run's own
      checks are the gate.
    - The partial-failure text, the class-5 risk and the compose-run red proofs changed to
      match. With a missing key, a failed `config -q` or a failed pull, the stub `docker`
      log holds no `rm -f` and no `up`.
    - *Correction to the finding.* "Neither H3 nor H4 says that reconcile stops before step
      3 after a failed secrets step" is partly false. H4's *What a partial failure leaves
      live* says that for one missing key "Reconcile restarts none of that output's
      dependents", and H4's reconcile tests assert that "when `homelab-secrets` fails,
      `nut-monitor` is not restarted". H4's gate under *The restart contract* is narrower
      than that text. *Open questions* 5 asks H4 to reconcile the two. The fix holds
      either way.
- **H5-R10-02 (high)** The engine upgrade and the updater were two writers of `homelab`
  with no hold between them. *Fixed:*
    - The play masks `homelab-updater.timer`, H3's per-host lever, before the apt probe. It
      waits until the updater and reconcile are inactive, and unmasks and starts the timer
      only after the recreate succeeds. A timer already masked stays masked. A failed play
      leaves the timer masked, and the text names the commands that clear it.
    - The play never writes the hold file, which is the failed-reconcile signal.
    - The unit uses `Wants=` with `After=docker.service` in place of `Requires=`, for the
      reason H3 gives against `Requires=homelab-secrets.service`.
    - The engine-upgrade test asserts the mask and the release, and flags a fixture play
      without the mask. *Open questions* 5 asks H7.5 for the same hold.
- **H5-R10-03 (high)** H5.4's PSI query read empty under a 1-minute scrape. *Fixed:*
    - The Baseline cites the `node-pi` job's `scrape_interval: 1m` and the Prometheus 3 pin.
    - Every rate uses `[5m]`, and a query with no sample fails the step.
    - `MemoryPeak` is replaced by host figures: the minimum of `MemAvailable` and the
      swap-in rate, beside the 136-181/s that alloy's compose template records.
    - The threshold is restated as PSI `full` at or above 20% over 5 minutes, or a
      `MemAvailable` minimum below the previous 24 hours' lowest.
    - compose-run gains `homelab_compose_up_duration_seconds`, which gives the run's window.
- **H5-R10-04 (medium)** The lock work belonged in the move PRs. *Fixed:* H5.6 edits
  `docker_install/CLAUDE.md#Notable` and runs `verify` on it. H5.7, H5.8 and H5.9 run
  `forget` on the deleted roles' six rows, each named by key. H5.12 keeps the sweep, which
  now names root `CLAUDE.md#Where to Look (task → start here)`, the row that cites
  `common/CLAUDE.md`.
- **H5-R10-05 (medium)** C4, H5 and H7 named three different removals of the Pi's key.
  *Fixed:* the stale "C4 names H1 only" sentence is gone. *What stays with Ansible until H7*
  names H7.20d. *Open questions* 5 asks C4 to name H7.20d and its reader set.
    - *Correction to the finding.* "H7 step 20 defines only H7.20a, H7.20b and H7.20c" is
      false. H7's step 20 has an H7.20d bullet that deletes the Pi's key after H7.20a and
      "points every reader that H5.1 pointed at both keys at `compose_services` alone".
- **H5-R10-06 (medium)** H5 used H4's retired `changed.json`. *Fixed:* every live use reads
  `pending.json`. The restart-contract fixture drains the entry only after a successful
  restart. The stale hand-path note to H4 is deleted. *The hand path is reconcile* names
  `systemctl start homelab-reconcile`, and says that a hand restart of `homelab-compose`
  leaves its entry for one more run.
- **H5-R10-07 (medium)** The probe unit named no `User=`. *Fixed:* H5.14 sets
  `User={{ sys_user }}` and adds a `LogsDirectory` census with a red fixture.
- **H5-R10-08 (medium)** H3's census could not see a `delegate_to` task, and H5.8 omitted
  D5's counter check. *Fixed:*
    - H5.14 extends the census to attribute a `delegate_to` task to its target, with a
      named member and fixtures. *Open questions* 5 tells H3.
    - When D5 landed first, H5.8 quotes the stage's counter through the agent Prometheus
      transport. *Open questions* 5 asks D5 to accept that transport in place of `probe.py
      metric`, which decrypts `domain` (`scripts/diagnostics/probe_lib/core.py:k8s_endpoint`).
- **H5-R10-09 (medium)** alloy's password moved from a 0640 file into the container
  environment without saying so. *Fixed:*
    - The move is an accepted risk that the operator approves or declines before H5.8
      merges (**operator-only**, *Open questions* 6). The Baseline cites the 0640 comment
      and docker-proxy's `CLAUDE.md`.
    - The file route is the declined path. It is not the default, because a persisted file
      changes nothing in the Compose model on rotation, which brings back H5-R1-11. The tree
      also cannot show whether Alloy's `http` block reads a `local.file` export.
- **H5-R10-10 (medium)** The rollback did not order the move PRs. *Fixed:* move PRs revert
  newest first, and an upstream's downstreams revert before it. The project census flags a
  `depends_on` on an undeclared service.
- **H5-R10-11 (medium)** The Pi rules had to sit before the pinDigests rule, inside its
  scope. *Fixed:*
    - H5.11 inserts its rules between the catch-all and the pinDigests rule.
    - It adds the Pi template glob to that rule and rewrites the rule's scope sentence.
    - It gives the manager a template with the `newDigest` arm.
    - Tests assert the Pi templates' coverage, with an uncovered fixture, and a round trip
      of every live Pi `image:` line.
- **H5-R10-12 (medium)** The rotation consumer census lost the Pi's consumers. *Fixed:* H5.7
  adds `hosts/components/` as a second walk root of `_census_corpus` and maps a hit to its
  package. `consumer_commands` emits the operator-only `sync`. Named-member tests cover both
  keys, and the Baseline lists the module.
- **H5-R10-13 (medium)** H7.5 relied on behaviour H5 does not provide. *Fixed:* *Open
  questions* 5 tells H7 that H5.3, not H5.7, changes the play. It also says H7.5's unit must
  run its own `up -d --force-recreate --wait`, and that it needs the H5.3 hold.
- **H5-R10-14 (medium)** The ADR-0013 decision came after four move PRs. *Fixed:* the
  **operator-only** decision gates H5.2b, the first enable link (*Open questions* 7). If the
  operator declines, H5.2a reverts and nothing has moved. H5.12 keeps the ADR text.
- **H5-R10-15 (medium)** *Verification* ran Pi commands as an agent. *Fixed:* the section
  splits as H3's does. Agent reads go through Prometheus by loopback, then ClusterIP. The
  operator runs every Pi shell block and every privileged command, and exit observation 1
  says so.
- **H5-R10-16 (medium)** A failed run outside a landing raised no signal, and the R1 entry
  pointed at a note that did not exist. *Fixed:*
    - *Open questions* 5 asks M1 for rules on the compose result, its staleness and
      `homelab_host_reconcile_ok`.
    - From H5.2b the recovery heartbeat pushes `down` to its existing tile while the result
      reads 0, with a red proof. It reuses the heartbeat's tile and token, so it needs no new
      secret. compose-run writes the file at 0644, as M3's convention does, and the
      heartbeat reports a file it cannot read rather than treating it as absent.
    - *Risks* states the remaining gap, and the R1 entry points at the M1 note.
- **H5-R10-17 (medium)** H5.2a exceeded the PR-size guide. *Fixed:* it splits into H5.2a1
  (renderer pieces), H5.2a2 (the dotenv check) and H5.2a3 (unit, scripts and metric). Each
  part is inert until H5.2b. The family name stays, because H4 cites "H5.2a's quiet window".
- **H5-R10-18 (medium)** `--wait-timeout 600` was not derived. *Fixed:* the unit template
  derives the wait timeout from the healthcheck variables plus 120 s, 730 s at
  `70591c307`. It also derives `TimeoutStartSec` from that, 1390 s. A test holds the
  relation, and the H3 note and *Risks* carry the new figures. Two such runs in one
  reconcile would pass its 30-minute limit, so *Open questions* 5 asks H4 to skip a step-2
  start of a unit that step 3 restarts.
- **H5-R10-19 (medium)** compose-adopt read the full interpolated `config`. *Fixed:* it uses
  `config --services`, relying on the container-name rule, which the project census now
  asserts. Its test checks the stub's argv.
- **H5-R10-20 (medium)** The landing text assumed Ansible-owned readers. *Fixed:* *Landing a
  move PR* and H5.1 give the flux-owned branch. The census also renders the three readers
  through `homelab-render cluster`, in stub mode before R2 and placeholder mode after it.
  None of the three is a `render_sops_manifests` entry in R2. The stub-mode render is a
  test-only use, which *Open questions* 5 asks C1 to allow.

### Round c1 (per-slice adversarial review)

Raised 3 findings: 1 high and 2 medium. All 3 were checked against the tree at `70591c307`
and against the H3 and H4 texts, and all 3 are fixed. None is refuted.

- **H5-C1-01 (high)** compose-run read its SHA from `manifest.json`, which H3 keeps
  SHA-free by design, so the land wait could not settle. *Checked:* H3's *Package rules*
  Version bullet says "The version and SHA stay out of `manifest.json`", and C5 fixes the
  version as `0.<count>+g<12-hex sha>`. *Fixed:*
    - Step 6 of compose-run takes the `sha` label from `dpkg-query -W -f '${Version}'
      homelab-daniel-pi`, the hex after `+g`. That SHA is the last merge that changed the Pi
      package, which H3's ancestry comparison expects. A version without the suffix writes
      `sha="unknown"`, which never settles a landing.
    - *Design* states that `manifest.json` carries no SHA and that no executor adds one,
      because a per-merge field would make every host package publish on every merge.
    - The compose-run red proofs gain a case with a SHA-free fixture manifest and a stub
      `dpkg-query` version. A fixture compose-run that reads the manifest is flagged.
- **H5-C1-02 (medium)** *Risks* and the notes to H3 and H4 contradicted those slices' text.
  *Checked:* H3's updater carries `TimeoutStartSec=50min`, derived from reconcile's, with a
  test. H4's existence gate "covers steps 2 and 3" whatever put the unit there, and H4's
  step 2 starts only what the restart set does not hold. H4's partial-failure text now
  agrees with its gate. H4 still cites a 15-minute `homelab-compose` restart. *Fixed:*
    - The *Risks* entry on reconcile length states H3's 50 minutes and H4's one call per
      unit per run. The 46-minute double-run risk is gone.
    - The *Restart ordering* gate bullet says H4's gate covers every reconcile start or
      restart. compose-run's key check stays, because the boot path is not reconcile and a
      stale `compose.env` passes an existence gate.
    - *Open questions* 5 drops the H3 timeout request and the two stale H4 requests. It
      asks H4 to cite the unit's derived `TimeoutStartSec`, 1390 s, in place of 15 minutes.
    - The engine-upgrade play's wait limit is the rendered `homelab-updater.service`
      `TimeoutStartSec` plus 60 s, carried as a `docker_install` role default. The
      engine-upgrade test gains a fifth assertion that holds the default above the
      updater's limit, and a fixture default of 30 minutes is flagged. *Tests and docs*,
      the file tree and *Complexity delta* changed to match.
- **H5-C1-03 (medium)** The *Baseline* reader list missed `scripts/infra_map`. *Checked:*
  `scripts/infra_map/inventory.py:declared_services` calls
  `lib.ansible_inventory.containers_entries_in`, which reads only `containers_list`. The
  same module's `load_roles` also globs `ansible/roles/containers/*/templates/`, from path
  parts the *Baseline*'s `roles/containers` grep cannot match. The finding did not name that
  second read. *Fixed:*
    - The *Baseline* lists the infra map as a fifth reader and names the five other
      `containers_entries_in` callers.
    - *The service list* routes `declared_services` for daniel-pi through the helper over
      both keys. It records why `render_guard.py` and `land_tags.py`, `narrow_lib/containers.py`,
      `compose_templates.py` and `estate.py` keep reading `containers_list` alone.
    - H5.1 repoints `declared_services` and reruns the grep, recording each caller in the
      PR body. H5.2b points `load_roles` at the component, and H5.12 drops its
      `roles/containers` half.
    - *Verification* gains a named-member case: after a fixture move, `declared_services`
      for daniel-pi still returns `alloy`. The file tree and *Tests and docs* list
      `scripts/infra_map/inventory.py` and its test file.

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review
round covers it.

- **F-14, the held-Pi page.** *Risks* said nothing pages for a Pi held with
  `homelab_host_reconcile_ok` at 0, while H3 said it pages. Both texts name H3's
  `HostReconcileFailed` (PR H3.5d, C7) instead. *Risks* and *Open questions* 5 changed,
  and the M1 item asks for two rules instead of three.
- **F-14, the hold lever.** The move PRs' *Rollback* and the partial-failure text use H3's
  `--clear-hold --install-newest` instead of deleting the hold file by hand. *Risks* names
  the lever, and the H3 item of *Open questions* 5 is resolved. *Complexity delta* is
  unchanged: the lever is H3's, and H5 adds no step.
- **Note from the H4 owner, C5's persisted-output rule.** The declined alloy file route in
  *Secrets* reads `persist: false`, as H4 sets it, because alloy need not start before
  the secrets unit runs.
