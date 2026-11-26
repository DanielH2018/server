# lidarr — music library manager

Lidarr, the music member of the *arr stack. It finds albums through Prowlarr, downloads them
through qBittorrent, and imports them by hardlink into `/data/media/music`, which navidrome
serves read-only (`k8s/roles/navidrome/defaults/main.yml:navidrome_k8s_media_subpath`).

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=lidarr` gates the rollout
- **Images:** `lscr.io/linuxserver/lidarr` (`lidarr_k8s_image`), `ghcr.io/onedr0p/exportarr`
  (`lidarr_k8s_exportarr_image`), `python` (`lidarr_k8s_stall_sweep_image`)
- **Route:** `lidarr.<domain>` · `lidarr.local.<domain>`, Authelia one_factor
- **Claims:** `lidarr-config` (weekly -> B2 (default target)), `media-data` (not Longhorn
  (media-local))
<!-- /generated_from -->

- **Stable branch only.** `lidarr_k8s_image` in `k8s/roles/lidarr/defaults/main.yml` tracks
  LSIO's stable stream. Lidarr cannot move
  from nightly or the plugins branch back to stable without restoring its database, so a
  Soulseek plugin that needs another branch is a decision for #8192, not an image bump.
- **The API key comes from SOPS.** `lidarr_api_key` reaches the pod through the
  `lidarr-exportarr` Secret (`k8s/roles/lidarr/defaults/main.yml:render_secrets`), and the
  shared macro's `api_key_secret` argument (`k8s/templates/arr-deployment.yml.j2`) sets
  `LIDARR__AUTH__APIKEY` from it. Lidarr reads `Lidarr:Auth:ApiKey` before its own
  `config.xml`, so the key in SOPS is the key Lidarr uses, and exportarr and Prowlarr can hold
  it before Lidarr first starts.
- **The `lidarr-snapshot` Job snapshots `lidarr-config` before each revision applies**
  (`k8s/roles/lidarr/defaults/main.yml:k8s_autodeploy_snapshot_pvcs`), as radarr's does, and
  `media-data` is mounted but never reverted
  (`k8s/roles/lidarr/defaults/main.yml:k8s_autodeploy_unreverted_claims`).
- **Mounts all of `media-data` at `/data`**
  (`k8s/roles/lidarr/defaults/main.yml:lidarr_k8s_media_claim`). The root folder is
  `/data/media/music`, and
  downloads land under `/data/torrents/lidarr`, so an import is a hardlink.

## Notable
- **`templates/verify/` holds arr-notification's seed Job.** The renderer emits it into
  `lidarr-verify`, which declares the `Discord (health)` Connect notification over `/api/v1`
  (the script picks the version per app). The Job's pod carries `app: lidarr-verify`, which
  lidarr's `netpol_from` lists. `k8s/roles/arr-notification/CLAUDE.md` has the design.
- **`templates/verify/quality-profiles.yaml.j2` declares the two quality profiles decided on
  #8190**, from `lidarr_quality_profiles` in `defaults/main.yml`, through
  `files/seed_lidarr_quality_profiles.py` at `/api/v1/qualityprofile`. It adopts Lidarr's shipped
  `Standard` (the lossy profile) and `Lossless` by name, creates one that is missing, and never
  deletes: `Any` stays. The script splits Lidarr's quality groups into single qualities, because
  `allowed` belongs to a group and `MP3-320` shares one with `AAC-320`. To read what the
  profiles hold, run `probe.py arr lidarr qualityprofile --json`.
- **Lidarr has no Opus quality, and the lossy profile allows no Vorbis tier in its place.** The
  operator ruled on 2026-11-13 to drop the "Opus 192 and above" tier, and `AAC-VBR` stands for
  the AAC tier above `AAC-256`. A Lidarr that adds Opus is an edit to `lidarr_quality_profiles`.
  The same ruling sets `upgradeAllowed` true on `Standard` and false on `Lossless`.
- **Not wired yet, and why.** Homepage's tiles read each app's key by name, so lidarr has no
  tile.
- **Lidarr's own state lives in `lidarr.db`:** the root folder and the qBittorrent download
  client are configured in the UI and survive through the snapshot, not git. The quality
  profiles are declared by the seed Job above and survive the same way.
- **Import lists live in `lidarr.db` too.** Settings, Import Lists offers Last.fm tags,
  Lidarr's curated lists, MusicBrainz series and Spotify. A Last.fm tag list needs no Last.fm
  account. Soularr fetches whatever the lists leave wanted.
- **A list's Monitor setting decides whether it downloads anything.** The choices are None,
  Specific Album and All Artist Albums. A discovery list (Last.fm, curated, Spotify Followed
  Artists) takes None, with Monitor New Albums set to None or New Albums. A library list
  (Spotify Saved Albums, Spotify Playlists) takes Specific Album, because None adds the
  artists and downloads nothing. No list takes All Artist Albums: it monitors every album of
  every artist the list adds and can fill `/srv/media`, which shares daniel-box's root disk.
  Library lists use the `Standard` quality profile, since a lossless album runs 300-500 MB.
- **Spotify Playlists turns each track into its album.** A 300-track playlist can add up to
  300 wanted albums, compilations included. Liked Songs is not offered as a playlist; copy it
  into an ordinary playlist first.
- **The Spotify lists sign in through Lidarr's own proxy, `spotify.lidarr.audio`.** Lidarr
  has no field for a Spotify client ID of its own, so no Spotify developer app or SOPS secret
  is involved. The refresh token sits in each list's settings in `lidarr.db`, and it renews
  only while that proxy answers. When it stops, every sync fails until the proxy returns and
  someone re-authenticates in the list's settings.
- **A failing list reaches Discord only when every list fails.** Lidarr's import-list health
  check is an Error when all enabled lists fail and a Warning when some do, and
  `k8s/roles/arr-notification/defaults/main.yml:arr_notification_include_health_warnings`
  keeps warnings out of `Discord (health)`. With a Last.fm list beside the Spotify ones, an
  expired Spotify token shows only on Lidarr's System, Status page.

## Autonomous-role contract

The `lidarr-stall-sweep` CronJob removes Lidarr queue records with no human in the loop
(`templates/stall-sweep-cronjob.yaml.j2`, `files/sweep_lidarr_stalls.py`).

- **Scope.** It removes a record only for a `lidarr`-category torrent that qBittorrent reports
  `stalledDL` or `metaDL`, that has run for 2 hours, and that has received no data for 1 hour
  (`k8s/roles/lidarr/files/sweep_lidarr_stalls.py:is_dead`). Each removal also deletes the
  torrent from qBittorrent, blocklists that release and starts a search for another one. It
  never touches another category or a record without a torrent behind it.
- **Why it reads qBittorrent.** Lidarr's `added` is the grab time, and a torrent can wait hours
  in qBittorrent's queue before it starts. Only qBittorrent's `time_active` counts running time,
  so a magnet that has just started is never removed in its first minutes
  (`k8s/roles/lidarr/tests/test_sweep_lidarr_stalls.py::test_a_torrent_that_just_left_the_queue_is_not_dead`).
  The qBittorrent key reaches the pod through the `lidarr-stall-sweep` Secret.
- **Mode and arming.** `k8s/roles/lidarr/defaults/main.yml:lidarr_k8s_stall_sweep_armed`. While
  it is false, each run logs what it would remove and removes nothing. To stop the sweep, set it
  false again. Besides the hourly schedule, a gate copy runs once per revision in
  `lidarr-verify`, after qbittorrent is Ready (`k8s/roles/lidarr/defaults/main.yml:flux_verify_after`).
  A second run finds nothing more to remove
  (`k8s/roles/lidarr/tests/test_sweep_lidarr_stalls.py::test_a_second_run_removes_nothing`).
- **Abort valves.** A run removes nothing when qBittorrent reports fewer than 10 DHT nodes,
  because a dropped VPN tunnel stalls every torrent at once
  (`k8s/roles/lidarr/tests/test_sweep_lidarr_stalls.py::test_a_run_with_too_few_dht_nodes_refuses_before_any_removal`).
  It also refuses a Lidarr queue page shorter than Lidarr's total. It removes at most 10 records
  per run, the longest idle first. `activeDeadlineSeconds` bounds a hung run.
- **Required evidence.** Each run logs one count line in Loki,
  `lidarr stall sweep: N removed, M over the cap, K untracked, T checked`, then one line per
  removal with its state, progress, running time and idle time. Nothing pages on a failed run.
  A failure only leaves dead torrents in the queue, and they no longer block a queue slot.
- **Next-run review.** A run that removes a torrent you expected to finish means the
  thresholds are too tight. Read the removal line's running and idle hours before widening
  anything.

## Editing
- Manifest: `templates/deployment.yaml.j2`, a single call to
  `k8s/templates/arr-deployment.yml.j2`, the macro radarr and sonarr render from.
- Image pin, Renovate stance, resources and log bounds: `defaults/main.yml`.
