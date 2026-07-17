# configarr — sole Sonarr/Radarr guide-syncer

The homelab's only quality-profile/custom-format syncer, using
[Configarr](https://configarr.de) (a recyclarr-compatible syncer that ALSO supports local
custom-format definitions — recyclarr is TRaSH-guide-only). It absorbed the retired
`recyclarr` role's two guide-backed profiles on 2026-07-17, on top of its original job
guarding Sonarr's bespoke "Anime" profile. See repo-root `CLAUDE.md` for shared conventions.

## Why the Anime local CFs exist
Mushoku Tensei S2 was grabbed from `[NTRX] … (BD Remux 1080p AVC …)` — a release whose title
advertised an **AVC Blu-ray remux** but which actually ships a **long-GOP HEVC 10-bit x265
re-encode** (250-frame GOP). That caused Jellyfin buffering + very slow seeks (2026-07-16).
Sonarr parses quality/codec from the release **title** at grab time, so no codec custom format
can catch a title that lies — the only pre-grab lever is **release-group reputation**.

## At a glance
- **Image:** `ghcr.io/raydak-labs/configarr` (version-pinned, Renovate-managed)
- **Host:** daniel-server · **No web UI**, no Authelia · **Networks:** media
- **Depends on:** sonarr, radarr (`meta/deps.yml`)
- **One-shot (ephemeral):** run via `compose run --rm` (a fresh container that auto-removes each
  sync, so nothing lingers in `docker ps -a`), on deploy + a daily 04:30 cron. No container_name /
  restart / healthcheck / AutoKuma — a batch job, not a service.
- **Host-plane cron:** `configarr_sync.py` wraps the `compose run` and writes a
  `{ts,ok,msg}` state file monitor-bridge reads for its **"Configarr Sync"** monitor (see
  monitor-bridge's CLAUDE.md) — the healthcheck a one-shot batch job can't have.
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Scope — what configarr manages
`delete_unmanaged_custom_formats` is left **OFF** everywhere, so Configarr never deletes CFs it
didn't create.

- **Sonarr `WEB-1080p` profile** — guide-backed, via recyclarr `include:` templates
  (`sonarr-v4-quality-profile-web-1080p` / `sonarr-v4-custom-formats-web-1080p`).
- **Radarr `HD Bluray + WEB` profile** — guide-backed, via recyclarr `include:` templates
  (`radarr-quality-profile-hd-bluray-web` / `radarr-custom-formats-hd-bluray-web`).
- **Sonarr `Anime` profile** — the operator's own scheme: **52 scored bespoke
  `Anime Profile N_N_N` custom formats**, plus TRaSH-style CFs (WEB tiers, streaming tags,
  `Bad Dual Groups`, …). Configarr manages **only** two local CFs and their scores here:

  | Local CF | Match | Score in Anime | Effect |
  |---|---|---|---|
  | `Fake/Mislabeled Remux Groups` | release group `^(NTRX)$` | **-10000** | rejected (profile `minFormatScore=0`) |
  | `Trusted Anime Groups` | `^(TTGA)$`, `^(LostYears)$` | **+200** | preferred on upgrade |

  `delete_unmanaged_custom_formats` OFF means Configarr never deletes/alters the 52 bespoke CFs
  or their scores — it only reconciles the two local CFs above. A full read-only snapshot of the
  current Anime profile + CF scores lives in `files/baseline/` (documentation; not applied). The
  live CF definitions stay in Sonarr's DB (Kopia-backed).

**Accepted trade-off from the recyclarr port:** `include:`'s `reset_unmatched_scores` behavior
made Configarr authoritative for scores *inside the guide profiles it syncs* — on cutover it
reset 3 Radarr CFs from a stray `-10000` to `0` that were unmanaged leftovers from recyclarr's
old config. That's intentional: the guide profiles are the source of truth now, not whatever
scores happened to accumulate in Sonarr/Radarr's DB. This only applies within the `WEB-1080p` /
`HD Bluray + WEB` profiles — it does not touch the bespoke Anime scheme (`delete_unmanaged` stays
OFF there too).

**To extend the Anime defense:** add release groups to the `^(NTRX)$` alternation (or a new local
CF) in `templates/config.yml.j2`. To have Configarr own MORE of the Anime profile, add a
`quality_profiles` block for it — but that makes Configarr authoritative (UI edits get reverted),
so weigh it against the bespoke scheme first.

## Deploy ordering
`/opt/configarr` (scripts) and `/var/lib/configarr` (state.json) are created `sys_user`-owned by
this role. **Deploy `configarr` before `monitor-bridge`** on a fresh host — otherwise Docker
auto-creates the `/var/lib/configarr:/configarr:ro` bind-mount source root-owned and the
non-root monitor-bridge container can't read it. The state file is written on every deploy (the
role runs the sync wrapper as a `deploy`-tagged task), which doubles as the first-deploy seed so
the Configarr Sync monitor doesn't false-DOWN before the first daily cron tick.

## Refreshing the Anime baseline snapshot
```bash
uv run python scripts/probe.py arr sonarr "/api/v3/qualityprofile" --json \
  | jq '.[]|select(.name=="Anime")' > ansible/roles/containers/configarr/files/baseline/anime-profile.json
```

## Editing
- Compose: `templates/docker-compose.yml.j2` · Sync config: `templates/config.yml.j2`
- Host cron: `files/configarr_sync.py` (I/O shell) + `files/configarr_status.py` (pure
  exit-code/output evaluator, unit-tested in `files/test_configarr_status.py`). Both are copied
  from `roles/setup/common/files/host_lib.py` beside them and run under the host's system
  Python — kept 3.12-clean and registered in `ansible/tests/test_host_scripts_py312.py`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "configarr"`
- Verify/run a sync manually (no persistent container to `docker logs` — it's `--rm`):
  `docker compose -f containers/configarr/docker-compose.yml run --rm -T configarr` — a healthy
  run lists the managed CFs and reports no errors. Or run the state-writing wrapper directly:
  `CONFIGARR_COMPOSE=containers/configarr/docker-compose.yml python3 /opt/configarr/configarr_sync.py`
  and check `/var/lib/configarr/state.json`.
- Unit tests: `uv run pytest ansible/roles/containers/configarr/files`.
