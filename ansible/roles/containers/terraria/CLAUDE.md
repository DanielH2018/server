# terraria — Terraria game server

Vanilla Terraria dedicated server. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `beardedio/terraria:vanilla-latest`
- **Host:** daniel-server · **Networks:** apps · **Authelia:** no (raw-TCP game server)
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **No HTTP route.** Exposed two ways: a dedicated Traefik raw-TCP entrypoint
  `terraria` (`:7777`, `HostSNI(*)`) and a direct `7778->7777` host publish. The TCP
  entrypoint + its `7777:7777` publish live in the **traefik** role
  (`templates/traefik.yml.j2` + `templates/docker-compose.yml.j2`) — re-enabled together
  with this role. Raw-TCP routers bypass the CrowdSec + rate-limit chain (no HTTP
  middleware), which is inherent to a game-server port.
- World/port/password config is templated to `config/serverconfig.txt` from
  `serverconfig.txt.j2`; `password` is `terraria_password` (secrets, rotation-tracked).
  The template task is wired to `common_config_changed`, so editing it recreates the
  container on the next deploy.
- `stdin_open` + `tty` are set for this image — `docker attach terraria` to reach the
  server console after deployment.
- **Healthcheck reads `/proc/net/tcp`, it does NOT connect.** The probe greps the kernel
  socket table for a LISTEN on local port `1E61` (hex 7777, state `0A`) — confirming the
  server is actually serving the port (stronger than `pidof`) — instead of opening a
  socket. **Do not "simplify" it to a `/dev/tcp` connect probe** (the grafana-style one):
  Terraria's binary protocol logs every accepted socket as `<ip> is connecting...`, so a
  connect probe firing every ~30s would spam the game console ~2400×/day (verified — both
  `echo >` and bare `exec 3<>` forms trigger it; the connection itself is what logs, not
  the byte). Half-open probes don't linger or eat player slots. `start_period=120s` covers
  the world-load before the port binds (a cold load measured ~74s; warm ~10s) so autoheal
  (`AUTOHEAL_CONTAINER_LABEL=all`) doesn't restart it mid-load — a first-boot world
  *generation* can exceed 120s but is a rare one-time event.
- **All persistent state is `./config` → `/config`** (Kopia-backed): config, banlist,
  and worlds. Worlds only land here because `serverconfig.txt` sets an **absolute**
  `world=/config/DBoys_Terraria_Server.wld` — **keep it absolute.** A bare `world=` name
  (even with `worldpath=/config`) resolves against the WORKDIR (`/vanilla`) and saves the
  world into the ephemeral container layer, lost on recreate. Tested both forms; only the
  absolute path lands it in `/config` (the `Worlds → /config` symlink `run.sh` makes is
  not enough on its own).
- The app dir (`/vanilla`, the image WORKDIR with `run.sh` + binaries) is intentionally
  **not** mounted — it holds no user state once `worldpath` is set. The original `worlds`
  *named volume* mounted there was load-bearing only because autocreate writes to the
  WORKDIR; it's also un-backed-up and pins stale binaries across image updates. A *bind*
  mount there would shadow `run.sh` and break startup. `worldpath=/config` sidesteps all
  of that.
- World file is `DBoys_Terraria_Server.wld` with `autocreate=3` — first boot generates
  a fresh large world in `/config` if none exists.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Server cfg: `templates/serverconfig.txt.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "terraria"`
  (redeploy `traefik` too if you change the entrypoint/published port)
