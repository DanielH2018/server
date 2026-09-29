# docker_install — Docker Engine + Compose v2 + the Docker networks

Installs Docker CE, the Compose/buildx plugins, the daemon config, and creates the shared
Docker networks every container role attaches to. **Not a container role** — a host-setup
role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`. See
repo-root `CLAUDE.md` and `.claude/rules/docker.md` for conventions.

`docs/docker-engine-pins-and-runtime.md` carries the working-out: the 2026-09-18 upgrade
incident, the pin derivations, the Go runtime measurements and the uninstall the teardown arm
answers. This file keeps the rules a session must not break.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "docker_install"`
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## Where it runs
- In `ansible/initial_setup.yml`, after [[sops_setup]] — every host, **unconditionally**.
  `tasks/main.yml` is a dispatcher: `has_docker: true` runs `install.yml` (everything below),
  `has_docker: false` runs `teardown.yml`.
- `uv run ansible-playbook ansible/initial_setup.yml --tags "docker_install"`.
- **Granular tags:** `docker-repo` (APT repo + GPG + the cache refresh), `docker-engine`
  (install + hold + v1-wrapper removal), `docker-group`, `docker-daemon` (daemon.json +
  conditional restart), `docker-networks`, `docker-go-runtime` (both Go runtime drop-ins;
  `-dockerd` / `-containerd` select one). `docker-engine-upgrade` is `never`-tagged AND gated on `docker_install_engine_upgrade`: it runs only when named and opened with `-e` (below). `never` alone is not enough — the role tag inherits onto the include and overrides it (#1998).

## The engine is held; `--tags docker-engine-upgrade` is how it moves
`docker_engine_packages` (`group_vars/all.yml`) are `apt-mark hold`-equivalent on every
`has_docker` host, set by `ansible.builtin.dpkg_selections` in two places: [[initial_setup]]'s
`host-basics.yml` immediately BEFORE its dist-upgrade (this role runs after that one, so a
hold set only here would land one upgrade too late), and `install.yml` right after the
install, for the fresh host that had nothing to hold yet. Held, `apt-get upgrade`,
`dist-upgrade` and unattended-upgrades leave them alone. What the hold prevents is an engine
swapped under running containers.

**The deliberate bump** (`tasks/engine-upgrade.yml`):
```
uv run ansible-playbook ansible/initial_setup.yml --tags docker-engine-upgrade -e docker_install_engine_upgrade=true -e target=daniel-pi
```
It refuses a host with no `~/server` checkout, unholds, and asks the apt module in check mode
whether `docker_install_package_specs` would change anything. Nothing pending: re-hold and
report. Otherwise it stops every Compose project in `containers_list` (reverse order),
upgrades, re-holds in `always:` so a failed apt run leaves the hold in place, and brings every
project back with `recreate: always` — the recreate docker-proxy needs to pick up the new
socket inode. The Pi's containers are down for the apt run, so run it from a LAN session.

**The four versions are pinned in `defaults/main.yml`, and Renovate carries the signal**
(#2153), rendered into apt's `name=5:29.8.1-*~ubuntu.24.04~noble` form as
`docker_install_package_specs`. The manager reads Docker's own noble/arm64 index rather than
GitHub releases (#2341), and the spec globs the Debian revision rather than naming one
(#2357); the docs page derives both. ENFORCED by
`scripts/tests/test_renovate_docker_engine_pins.py::test_managers_name_the_apt_packages_they_pin`.

**Merging a pin PR moves nothing on the Pi.** `install.yml` installs only where there is no
docker-ce; on an installed host it reports the gap and leaves it, so a Docker security fix
waits for an operator to run the command above.

**Teardown unholds first**, because apt with `-y` refuses to change a held package.
ENFORCED by `ansible/tests/setup/test_docker_engine_is_held_before_apt_upgrade.py`: the
hold set is the install set, the hold precedes the dist-upgrade, and the teardown unholds.

## Teardown (`tasks/teardown.yml`, `has_docker: false`)
Reaps what an imperative Docker uninstall leaves behind: any `docker-compose-*.service` unit
and the crons in `docker_install_stale_crons`, all `state: absent`, so it is a no-op on a host
that never had Docker. **Not covered, deliberately:** package purge, `/var/lib/docker` and the
rendered `containers/` tree, because those hold data.

## What it does (`tasks/install.yml`)
`tasks/install.yml` is the list of steps, and two of them carry a rule it does not state.

- **The `loop:` that creates the networks is the list that decides** which shared networks
  exist, and its comments record what each is for and when a retired one went. Container roles
  only *attach*, through the `networks.yml.j2` macro, so adding one means editing that loop.
- **The docker-group task resolves its user under `become: false` on purpose.** Under the
  play's `become: true`, `ansible_facts.env.USER` is `root`; the user who runs `docker` is the
  unprivileged connecting user.

`daemon.json` pins `default-address-pools` to `10.200.0.0/16` in /24s and only NEW networks
draw from it; it also sets json-file log limits and `live-restore: true`, so a change to it
restarts Docker — see the first bullet under *Notable*.

## Notable
- **`live-restore` covers every container EXCEPT the `network_mode: service:wireguard` pair.** A
  daemon restart — a `docker-ce` upgrade OR **any** `daemon.json` edit — re-triggers
  `docker-compose-qbittorrent.service` (`Requires=docker.service`, `Type=oneshot`), which
  RECREATES `wireguard` and `qbittorrent` (the boot-race unit [[qbittorrent]] documents). It
  self-heals, but confirm the tunnel came back: `docker exec qbittorrent curl -s
  localhost:8080/api/v2/transfer/info` should show `dht_nodes` > 0. A silent rebind to `eth0`
  stalls every torrent at 0% while the TCP-only healthcheck stays green.
- **The GPG key stays ASCII-armored at `/etc/apt/keyrings/docker.asc`.** Do **not** reintroduce
  `gpg --dearmor` via `command`: it writes the file under the `027` umask [[initial_setup]] set
  earlier in the same play, and apt then reports the repo as unsigned. Guarded by
  `ansible/tests/setup/test_apt_keyring_permissions.py`.
- **Each daemon runs `GOGC=off` under a `GOMEMLIMIT`** (`tasks/go-runtime.yml`, sized at
  `docker_install_gomemlimit_<daemon>`). Apply a change by hand, one daemon per day, so the
  day-after reading is attributable; a child inherits the environment, which is why
  containerd's `config.toml` carries a shim override under the `docker-go-runtime-containerd`
  tag. `ansible/tests/setup/test_docker_daemons_gomemlimit_headroom.py` refuses a limit with
  under two minutes of slack.
