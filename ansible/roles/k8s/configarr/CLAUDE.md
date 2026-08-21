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
- **Host: daniel-box (k8s CronJob), since 2026-08-08 — slice 4, B7a.** This role renders
  `templates/config/config.yml.j2` and copies `files/configarr_status.py`. Edit configarr
  config HERE; deploy with `--tags configarr` from daniel-box.
- **No web UI**, no Authelia · targets the cluster sonarr/radarr
- **One-shot (ephemeral):** a nightly k8s CronJob, plus a `configarr-deploy-gate` Job the
  deploy creates from it (via `k8s/cronjob-gate`) so a config change syncs immediately. That
  gate proves the new image RUNS — a container that never starts fails the deploy; a sync that
  runs and fails is reported and the deploy continues, deliberately, because failing over a
  transient *arr outage is what the retired wrapper avoided. No healthcheck / AutoKuma — a batch
  job, not a service; its health signal is a daniel-box host cron (`/opt/configarr-health`)
  that reads the last Job's outcome via `configarr_status.py` (still owned + tested here) and
  pushes the "Configarr Sync" Kuma monitor. The health reader counts the gate's own Job like any
  other finished run: unlike `pi-peer-backup` (whose gate run explicitly does NOT count toward
  its dead-man monitor), a `configarr-deploy-gate` run genuinely performs the reconcile, so
  counting it is correct rather than a shared-role inconsistency — see
  `ansible/roles/k8s/pi-peer-backup/CLAUDE.md` for the other side of this choice.
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`

## Scope — what configarr manages
`delete_unmanaged_custom_formats` is left **OFF** everywhere, so Configarr never deletes CFs it
didn't create.

- **Sonarr `WEB-1080p` profile** — guide-backed, via recyclarr `include:` templates
  (`sonarr-v4-quality-profile-web-1080p` / `sonarr-v4-custom-formats-web-1080p`).
- **Radarr `HD Bluray + WEB` profile** — guide-backed, via recyclarr `include:` templates
  (`radarr-quality-profile-hd-bluray-web` / `radarr-custom-formats-hd-bluray-web`).
- **Sonarr `Anime` profile** — the operator's own scheme: **52 scored bespoke
  `Anime Profile N_N_N` custom formats**, plus TRaSH-style CFs (WEB tiers, streaming tags,
  `Bad Dual Groups`, …). Configarr manages **only** these four local CFs and their scores here:

  | Local CF | Match | Score in Anime | Effect |
  |---|---|---|---|
  | `Fake/Mislabeled Remux Groups` | release group `^(NTRX)$` | **-10000** | rejected (profile `minFormatScore=0`) |
  | `Trusted Anime Groups` | `^(TTGA)$`, `^(LostYears)$` | **+200** | preferred on upgrade |
  | `Anime English-Sub Groups` | `^(SubsPlease\|Erai-raws\|ASW\|EMBER\|ToonsHub)$` | **+300** | prefer releases that ship English softsubs |
  | `Anime Multi-Sub / Dual-Audio (title)` | release title `Multi-Sub`/`Dual-Audio` | **+100** | milder English-sub preference by title tag |

  The two English-sub CFs are **positive-only** by design: with `minFormatScore=0` a negative
  score would reject a subs-less release outright, breaking the intended "grab raw when it's the
  only option, then let Bazarr/Whisper add subs" fallback. `delete_unmanaged_custom_formats` OFF
  means Configarr never deletes/alters the 52 bespoke CFs or their scores — it only reconciles the
  four local CFs above.

  **`cutoffFormatScore` raised 0 → 400 (2026-07-17, set in Sonarr's DB — NOT Configarr).** Makes a
  raw that slips in auto-upgrade to a subbed release: a raw loses the +300/+100 English-sub bonus so
  it lands in the low hundreds (~305), staying below the 400 cutoff and thus upgrade-eligible until
  Sonarr grabs a higher-scoring release. **The "clears 400" guarantee holds only for the +300
  `Anime English-Sub Groups` path** — a listed-group sub lands ≥400 (e.g. an Erai-raws grab scores
  705). A subbed release that trips ONLY the milder +100 `Anime Multi-Sub / Dual-Audio (title)` CF
  (from a group NOT in the +300 list, e.g. `[Breeze] …[multisub]`) scores ~105 and stays
  *permanently* upgrade-eligible. That's intended, not a bug: cutoff stays 400 precisely so it keeps
  searching and upgrades to a listed-group sub when one appears. Accepted trade-off (2026-07-18
  review): ongoing RSS/search churn for such an episode, plus a hard-delete on each upgrade (Sonarr's
  recycle bin is off — no undo). No existing file re-grabs (all on-disk Anime files currently score
  506/705, none in the 100-399 gap). Blunt total-score lever, reversible via the API; the refreshed
  `files/baseline/anime-profile.json` snapshot is its only git record. A full read-only snapshot of the
  current Anime profile + CF scores lives in `files/baseline/` (documentation; not applied). The
  live CF definitions stay in Sonarr's DB (on its Longhorn PVC, backed up to B2).

**Accepted trade-off from the recyclarr port:** `include:`'s `reset_unmatched_scores` behavior
made Configarr authoritative for scores *inside the guide profiles it syncs* — on cutover it
reset 3 Radarr CFs from a stray `-10000` to `0` that were unmanaged leftovers from recyclarr's
old config. That's intentional: the guide profiles are the source of truth now, not whatever
scores happened to accumulate in Sonarr/Radarr's DB. This only applies within the `WEB-1080p` /
`HD Bluray + WEB` profiles — it does not touch the bespoke Anime scheme (`delete_unmanaged` stays
OFF there too).

**To extend the Anime defense:** add release groups to the `^(NTRX)$` alternation (or a new local
CF) in `templates/config/config.yml.j2`. To have Configarr own MORE of the Anime profile, add a
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
  | jq '.[]|select(.name=="Anime")' > ansible/roles/k8s/configarr/files/baseline/anime-profile.json
```

## Editing
- Sync config: `templates/config/config.yml.j2`
- Health evaluator: `files/configarr_status.py` (pure exit-code/output verdict logic,
  unit-tested in `files/test_configarr_status.py`) — copied by `roles/k8s/configarr` into
  `/opt/configarr-health` on daniel-box, where a cron reads the last Job and pushes Kuma.
  (Its Docker-era compose wrapper `files/configarr_sync.py` was deleted 2026-08-14, with the
  host residue it wrote to — `/opt/configarr` and `/var/lib/configarr` — removed 2026-08-09.)
- Deploy (from daniel-box): `uv run ansible-playbook ansible/deploy.yml --tags "configarr"` —
  the k8s role also runs a one-off `configarr-deploy-gate` Job so the edit syncs immediately.
- Verify a sync: `kubectl -n homelab logs job/configarr-deploy-gate` (or the latest
  `configarr-…` CronJob pod) — a healthy run lists the managed CFs and reports no errors.
- Unit tests: `uv run pytest ansible/roles/k8s/configarr/files`.
