# minecraft — Minecraft server (ARCHIVED)

**Not deployed.** Parked in `archive/`; see `../CLAUDE.md` for how to reactivate.

- **Image:** `itzg/minecraft-server:latest`
- **Intended:** no web port · apps net · Authelia: no
- **Notable:** Was a top-level role commented out in `host_vars`, not a `containers_list`
  entry with port/Authelia (it's a TCP game server, fronted outside Traefik via a
  dedicated `minecraft` entrypoint). Reactivating needs the TCP entrypoint in Traefik,
  a whitelist of player usernames in the compose env, and `MEMORY` tuned to the host.
  Uses Fabric modpack (TYPE=FABRIC, VERSION=1.21.10). Resource caps set to 6 CPUs / 20 GB.
