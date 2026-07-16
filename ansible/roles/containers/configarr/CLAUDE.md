# configarr — IaC guard for the Sonarr "Anime" quality profile

Keeps a **release-group defense** for the operator's bespoke Sonarr "Anime" profile under
version control, using [Configarr](https://configarr.de) (a recyclarr-compatible syncer that
ALSO supports local custom-format definitions — recyclarr is TRaSH-guide-only). See repo-root
`CLAUDE.md` for shared conventions.

## Why this exists
Mushoku Tensei S2 was grabbed from `[NTRX] … (BD Remux 1080p AVC …)` — a release whose title
advertised an **AVC Blu-ray remux** but which actually ships a **long-GOP HEVC 10-bit x265
re-encode** (250-frame GOP). That caused Jellyfin buffering + very slow seeks (2026-07-16).
Sonarr parses quality/codec from the release **title** at grab time, so no codec custom format
can catch a title that lies — the only pre-grab lever is **release-group reputation**.

## At a glance
- **Image:** `ghcr.io/raydak-labs/configarr` (version-pinned, Renovate-managed)
- **Host:** daniel-server · **No web UI**, no Authelia · **Networks:** media · **Depends on:** sonarr
- **One-shot:** runs, reconciles Sonarr, exits (`restart: "no"`, no healthcheck/AutoKuma).
  Re-runs on deploy (config change) + a daily `docker start -a configarr` cron.
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Scope — deliberately minimal (READ THIS before changing the config)
The "Anime" profile is the operator's own scheme: **52 scored bespoke `Anime Profile N_N_N`
custom formats**, plus TRaSH-style CFs (WEB tiers, streaming tags, `Bad Dual Groups`, …).
Configarr here manages **only** two local CFs and their scores in the Anime profile:

| Local CF | Match | Score in Anime | Effect |
|---|---|---|---|
| `Fake/Mislabeled Remux Groups` | release group `^(NTRX)$` | **-10000** | rejected (profile `minFormatScore=0`) |
| `Trusted Anime Groups` | `^(TTGA)$`, `^(LostYears)$` | **+200** | preferred on upgrade |

`delete_unmanaged_custom_formats` is left **OFF**, so Configarr never deletes/alters the 52
bespoke CFs or their scores — it only reconciles the two local CFs above. A full read-only
snapshot of the current profile + CF scores lives in `files/baseline/` (documentation; not
applied). The live CF definitions stay in Sonarr's DB (Kopia-backed).

**To extend the defense:** add release groups to the `^(NTRX)$` alternation (or a new local CF)
in `templates/config.yml.j2`. To have Configarr own MORE of the profile, add a `quality_profiles`
block — but that makes Configarr authoritative (UI edits get reverted), so weigh it against the
bespoke scheme first.

## Refreshing the baseline snapshot
```bash
uv run python scripts/probe.py arr sonarr "/api/v3/qualityprofile" --json \
  | jq '.[]|select(.name=="Anime")' > ansible/roles/containers/configarr/files/baseline/anime-profile.json
```

## Editing
- Compose: `templates/docker-compose.yml.j2` · Sync config: `templates/config.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "configarr"`
- Verify a sync: `docker logs configarr` (it exits after reconciling; a healthy run lists the
  managed CFs and reports no errors).
