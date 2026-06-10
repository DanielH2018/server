# docker_install — Docker Engine + Compose v2 + the Docker networks

Installs Docker CE, the Compose/buildx plugins, the daemon config, and creates the shared
Docker networks every container role attaches to. **Not a container role** — a host-setup
role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`. See
repo-root `CLAUDE.md` and `.claude/rules/docker.md` for conventions.

## Where it runs
- In `ansible/initial_setup.yml`, after [[sops_setup]] — every host.
- `uv run ansible-playbook ansible/initial_setup.yml --tags "docker_install"`.

## What it does (`tasks/main.yml`)
1. **APT repo (deb822):** installs prereqs (incl. `python3-debian`, required by
   `deb822_repository`), the Docker GPG key, and the Docker repo as a `.sources` file;
   removes any legacy one-line `docker.list` (the old `apt_repository` form is deprecated).
2. **Install:** `docker-ce`, `-cli`, `containerd.io`, **and explicitly**
   `docker-compose-plugin` + `docker-buildx-plugin` (the engine behind
   `community.docker.docker_compose_v2` and its `build: always` — declared so they can't be
   dropped as auto-installed Recommends). Removes the deprecated linuxserver compose-v1 wrapper.
3. **docker group:** resolves the *connecting* user (not `root` under `become`) via `id -un`
   and appends them to the `docker` group.
4. **Daemon config** (`/etc/docker/daemon.json`): json-file log limits (10m × 3) +
   `live-restore: true` so a daemon restart (e.g. a `docker-ce` upgrade) doesn't bounce all
   ~58 containers. Restarts Docker only when the file changes.
5. **Networks:** creates `proxy` (`{{ docker_network }}`), `monitoring`, `media`, `apps`,
   `homepage_private`, `lifecycle` (Watchtower/Autoheal ↔ docker-proxy only), `kopia`
   (Kopia ↔ Traefik only — keeps the unauthenticated repo off other apps).

## Notable
- **`become: false` user resolution (task 3) is deliberate** — under the play's `become: true`,
  `ansible_facts.env.USER` is `root`; the user who actually runs `docker` is the unprivileged
  connecting user, so membership is resolved with `become: false`.
- **deb822 migration** (commit `fee21f9`) is shared with [[optimize_pi]]'s Log2Ram repo —
  both need `python3-debian` and both clean up the legacy `.list`.
- Networks are created here once; container roles only *attach* (see the `networks.yml.j2`
  macro). Adding a new shared network means editing the `loop:` here.
