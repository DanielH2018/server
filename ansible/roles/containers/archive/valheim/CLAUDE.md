# valheim — Valheim game server (ARCHIVED, superseded)

**Not deployed, and not the rollback path.** Valheim was reactivated on 2026-08-13 as a
native k8s role — **`ansible/roles/k8s/valheim`** — not by un-archiving this one. Do not
follow `../CLAUDE.md`'s reactivation recipe for it: that recipe restores a service onto the
Docker edge, which retired at E7.

This compose is kept only as the historical record of how the service was configured under
Docker, and because the world data it points at
(`daniel-server:/home/ubuntu/server/containers/valheim/`) is still on disk as the seed
source for the k8s copy.

- **Image (then):** `ghcr.io/lloesche/valheim-server` — since renamed upstream to
  `ghcr.io/community-valheim-tools/valheim-server`, which is what the k8s role pins.
- **Was:** apps net · Authelia: no (gaming service) · UDP game ports published directly.
- **Warning:** the `SERVER_PASS` this template once hardcoded is in this **public** repo's
  git history and is disclosed. The k8s role uses a different password from SOPS.
