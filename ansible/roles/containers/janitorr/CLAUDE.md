# janitorr — Automated media library cleanup

Deletes watched/old media and cleans up Sonarr/Radarr based on disk-usage rules.
See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/schaka/janitorr@sha256:…` — **digest-pinned + `watchtower.enable=false`**
  (currently the `jvm-stable` build pulled 2026-06-28). `jvm-stable` is a floating non-semver
  alias Renovate can't version-track, and janitorr deletes real media, so updates are deliberate.
  **Manual update:** `docker pull ghcr.io/schaka/janitorr:jvm-stable`, take the new digest, redeploy.
- **Host:** daniel-server · **No web UI**, no Authelia (background service)
- **Networks:** media
- **Depends on:** traefik, **sonarr, radarr** (no Authelia — background service)
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Behaviour (retention rules, leaving-soon thresholds, dry-run flag) lives in
  `templates/application.yml.j2`. **It deletes files** — `dry-run` was flipped off
  2026-06-10 (operator decision after the initial trial period), so it now cleans for
  real. Tag media `janitorr-keep` in the *arrs to exempt it (see `exclusion-tags` in
  `application.yml.j2`). **NB:** Radarr/Sonarr reject underscores in tag labels
  (`^[a-z0-9-]+$`) and Janitorr matches by exact label — the upstream-doc `janitorr_keep`
  is uncreatable here, so the tags are hyphenated. Add a tag in Radarr via
  Settings→Tags or the API; Janitorr picks it up on its next run and drops the item
  from the Leaving Soon collection.
- Mounts the whole `containers/data` tree at `/data` (same as Sonarr/Radarr since the
  2026-07-02 hardlink-mount unification). Janitorr acts on media via the Sonarr/Radarr
  APIs; its direct filesystem use is `leaving-soon-dir` (where it writes the symlinks) and
  `free-space-check-dir`, both `/data`-relative. **Path-namespace trap:**
  `media-server-leaving-soon-dir` and the symlink targets are `/data/media/...` strings
  that JELLYFIN must resolve — jellyfin's primary mount puts the media tree at `/data`,
  so it carries a second `data/media:/data/media` mount specifically to make janitorr's
  namespace resolve there (2026-07-02 review M4; see the jellyfin role CLAUDE.md). If
  either side's mounts change, re-check both configs together.
- **A RestartCount of ~4 right after a host reboot is EXPECTED, not a fault** (diagnosed
  2026-07-02): Spring fails fast when sonarr/radarr aren't up yet, and `restart:
  unless-stopped` retries every ~10s until they are (~40s on the 06-28 boot). It crashes
  during context init, *before* any cleanup job — zero deletion risk. This can't be fixed
  with `depends_on`: sonarr/radarr are separate compose projects, and `depends_on` only
  orders services within one project. Don't re-flag; only investigate restarts that are
  NOT clustered in a post-boot window (check `last reboot` + log timestamps first).

## Editing
- Compose: `templates/docker-compose.yml.j2` · Rules: `templates/application.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "janitorr"`
