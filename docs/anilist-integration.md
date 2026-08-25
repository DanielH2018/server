# AniList integration for the media stack

**Date:** 2026-08-25
**Status:** design approved, not yet implemented

Two independent pieces connect the homelab's media stack to AniList. Phase 1 pushes watch
status from Jellyfin to AniList. Phase 2 mirrors AniList scores back into Sonarr and Radarr as
tags. Phase 1 is the deliverable; Phase 2 is best-effort and may be dropped without affecting
Phase 1.

Ratings are set on AniList directly. The section below records why no other surface in the
stack can host them.

## The constraint that shaped this design

The original request was to rate a season in Sonarr and have that reach AniList. No component
in the stack holds a numeric user rating, so that flow cannot be built as asked. The evidence,
all read from the live services on 2026-08-25:

- **Sonarr's season object carries three keys** — `monitored`, `seasonNumber`, `statistics`.
  Nothing on a season is writable, so a per-season rating has nowhere to live.
- **Sonarr's series `ratings` is `{votes, value}` from TVDB, and Radarr's is an aggregate of
  `imdb`, `tmdb`, `metacritic`, `rottenTomatoes` and `trakt`.** Both are read-only metadata, not a user
  field.
- **Series-level `tags` is the only user-writable string surface** in either application.
- **Jellyfin declares `Rating` and `Likes` on `UserItemDataDto`, but exposes neither.** The
  only write endpoint is `POST /UserItems/{itemId}/Rating`, whose sole parameter is
  `likes: boolean`. The served 10.11.10 web bundle contains no like control at all: `ThumbUp`
  and `ThumbDown` appear twice each and every occurrence is a gamepad key name
  (`GamepadLeftThumbUp`). `Favorite` is the only per-season signal a person can set by hand.

Neither *arr application fires an edit event. Sonarr's `notification/schema` offers
`SeriesAdd` and `SeriesDelete` with no `SeriesEdit`; Radarr offers `MovieAdded` and
`MovieDelete` with no `MovieEdit`. Any tag-driven flow polls rather than subscribes.

A bridge reaches the *arr APIs over ClusterIP. Authelia returns 302 on the ingress path even
when the request carries a valid `X-Api-Key`.

## Phase 1 — Jellyfin watch status to AniList

`jellyfin-ani-sync` syncs watch status and episode progress to AniList. Its AniList client
calls `UpdateAnime(id, status, progress, numberOfTimesRewatched, startDate, endDate)`, which
issues `SaveMediaListEntry(mediaId, status, progress, ...)`. The word `score` appears nowhere
in any of its four AniList source files, so the plugin cannot overwrite a rating set by hand.
That is the property that makes rating on AniList and syncing from Jellyfin safe to combine.

### Version pin

The Jellyfin server runs 10.11.10, pinned as
`lscr.io/linuxserver/jellyfin:10.11.10ubu2404-ls35`. The plugin's current release, 4.4.0.0,
declares `targetAbi` 10.11.11.0, which the loader rejects on an older server. Version 4.1.0.0
declares 10.11.6.0 and installs. The pin is therefore 4.1.0.0 until Jellyfin moves to 10.11.11
or later.

`ansible/tests/test_anisync_pin_matches_server.py` enforces this rather than leaving it to
memory. The release asset's filename leads with the `targetAbi` it was built for and the image
tag leads with the server version, so the comparison needs no network call. The test also
refuses a version bump that misses the URL, which would otherwise install the old build under
the new marker and never reconcile.

### Where the change lands

The change extends `ansible/roles/k8s/jellyfin/`. The plugin is Jellyfin configuration and the
role already owns `/config`, so a separate role would split one concern across two places.

`defaults/main.yml` gains four variables:

```yaml
jellyfin_k8s_anisync_version: "4.1.0.0"
jellyfin_k8s_anisync_url: "https://github.com/vosmiic/jellyfin-ani-sync/releases/download/v4.1/10.11.6.-.ani-sync_4.1.0.0.zip"
jellyfin_k8s_anisync_md5: "4d95c608192395b72efe57551a6f2ae0"
jellyfin_k8s_anisync_init_image: python:3.14-alpine
```

The URL and the checksum both come from the plugin's published `manifest.json`. The asset
returns HTTP 200.

### The installer

A second init container named `install-ani-sync` runs before Jellyfin starts. It copies the
shape of the existing `convert-encoding-settings` container:

- Image `python:3.14-alpine`, not the `alpine:3.24` its sibling uses. The release is a `.zip`
  and `/config` is owned by uid `{{ puid }}`, so an Alpine container would have to
  `apk add unzip` as root — and root here holds no `DAC_OVERRIDE`, so it could not then write
  the files it had just unpacked. `urllib`, `hashlib` and `zipfile` are standard library, so
  the install runs as the owning uid and installs nothing at run time.
- Mounts `jellyfin-config` at `/config`.
- Runs as `{{ puid }}`/`{{ pgid }}`, not root. A root process with every capability dropped
  holds no `DAC_OVERRIDE` and cannot write into a directory owned by another uid. The existing
  container documents this failure at length; the installer inherits the same fix.
- Checks that `jellyfin-ani-sync.dll` exists after extraction rather than trusting that the
  archive held what its name implies.

The script guards on a version marker at `/config/plugins/ani-sync/.installed-4.1.0.0`. When
the marker exists the container exits 0 immediately and opens no network connection, so an
ordinary pod restart pays nothing. When the marker is absent the container downloads the
release, verifies the MD5, extracts to a staging directory inside `/config/plugins`, checks the
DLL is present, renames that directory into place, and writes the marker last. Writing the marker last is what makes a failed
install retry on the next start instead of latching a broken state.

Raising `jellyfin_k8s_anisync_version` changes the rendered deployment, which the central
rollout-restart at `ansible/roles/k8s/manifests/tasks/main.yml:112` turns into a pod roll.

### The one manual step

The AniList OAuth handshake happens once, in the plugin's settings page. The resulting token is
specific to the AniList account and cannot be templated. It is written to
`/config/plugins/configurations/`, on the config PVC that Longhorn already backs up.

### Verifying Phase 1

A green rollout does not prove the plugin loaded. Verification has three steps:

1. `uv run python scripts/probe.py health jellyfin` gates the rollout and the restart window.
2. `GET /Plugins` on the Jellyfin API lists `Ani-Sync` with version 4.1.0.0.
3. Playing an episode to completion produces a matching progress change on the AniList entry.

Step 3 is the only one that exercises the actual integration. Steps 1 and 2 both pass on a
plugin that has loaded and cannot reach AniList.

## Phase 2 — AniList scores into *arr tags

A new role, `ansible/roles/k8s/anilist-tags`, runs a CronJob every 30 minutes on
`python:3.14-alpine`. It needs no ingress and no PVC beyond a cache for the mapping table.

Each run:

1. Reads the AniList list entries that carry a score, along with
   `mediaListOptions.scoreFormat`. Reading the format is what lets a POINT_100 list and a
   POINT_10 list normalise to the same displayed value.
2. Maps each `anilist_id` to `(tvdb_id, season)` for series and to `tmdb_id` for movies.
3. Reconciles tags in Sonarr and Radarr: it creates a missing tag, attaches the correct ones,
   and strips the ones that no longer apply.

The reconcile considers only tags carrying the rating prefix. The `anime` and `janitorr-keep`
tags are never candidates for removal. This is the single most important safety property of
the script, because a tag drives janitorr's retention decisions.

Tests live in the role's `files/` directory beside the script, matching how `monitor-bridge`
and `autofix-bridge` are organised.

### The mapping table

`Fribb/anime-lists` provides `anime-list-full.json`. Coverage measured on 2026-08-25: 7,115
rows carry `anilist_id` together with `tvdb_id` and `season.tvdb`, and 8,115 rows carry
`anilist_id` with `themoviedb_id`. The movie identifier is an array under
`themoviedb_id.movie`, so a reverse index handles the one-to-many case.

The table is 7.5 MB. Fetching it every 30 minutes costs 360 MB of egress per day for a file
that changes rarely, so it caches on a PVC and is checked for changes once a week.

## Risks

- **Split cours break the season assumption.** 420 of 6,049 distinct `(tvdb_id, season)` pairs
  map to more than one AniList entry. The `episode_offset` field distinguishes them. The script
  emits a tag per AniList entry rather than picking one, because picking one silently discards
  a rating the user set.
- **Tag labels are unverified for the rating prefix.** Sonarr converts tag labels to lower case and may
  restrict the character set. Whether a non-ASCII prefix survives is checked before the format
  is fixed.
- **AniList rate-limits at 30 requests per minute**, read from a live response header rather
  than from the documentation, which states 90. The limit is far above what a 15-series library
  needs.
- **Radarr holds zero movies.** The movie path ships without live exercise until a movie is
  added.
- **An unauthenticated read requires a public AniList profile.** A test against a non-public
  account returned `Private User`. The design therefore carries a token in SOPS and treats
  public-profile access as a fallback rather than the plan.

## Open inputs

Phase 2 needs two decisions that Phase 1 does not:

- The AniList username or user id.
- The tag format. The proposal is a rating prefix plus a season, resolving to one tag per rated
  AniList entry.

## Findings outside this design

Two observations surfaced while measuring the stack. Neither belongs to this work.

- **Sonarr cold-starts slowly.** A docker-mod installs 86 Alpine packages on every boot,
  leaving the pod unready for roughly three minutes. Traefik drops a route whose endpoints are
  unready and answers 404, so a Sonarr restart presents as a routing fault rather than a boot
  delay.
- **Radarr's library is empty.**
