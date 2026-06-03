# terraria — Terraria game server (ARCHIVED)

**Not deployed.** Parked in `archive/`; see `../CLAUDE.md` for how to reactivate.

- **Image:** `beardedio/terraria:vanilla-latest`
- **Intended:** apps net · Authelia: no (gaming service)
- **Notable:** Ships `templates/serverconfig.txt.j2` (world/port/password config). Exposes
  the Terraria TCP port directly; no Traefik HTTP route.
