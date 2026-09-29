# configarr — sole Sonarr/Radarr guide-syncer

The homelab's only quality-profile/custom-format syncer, using
[Configarr](https://configarr.de) — a recyclarr-compatible syncer that ALSO supports local
custom-format definitions, where recyclarr is TRaSH-guide-only. It holds the two guide-backed
profiles the retired `recyclarr` role synced, plus Sonarr's "Anime" profile.

## Why the Anime local CFs exist
Sonarr parses quality and codec from the release **title** at grab time, so no codec custom
format can catch a title that lies, and the only pre-grab lever is **release-group
reputation**. A `[NTRX] … (BD Remux 1080p AVC …)` grab in 2026-07 shipped a long-GOP HEVC
x265 re-encode instead, which made Jellyfin buffer and seek slowly.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Deploy tag:** `--tags "configarr"`
- **Image:** `ghcr.io/raydak-labs/configarr` (`configarr_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **Host: daniel-box (k8s CronJob).** This role renders `templates/config/config.yml.j2` and
  copies `files/configarr_status.py`. Edit the config HERE; deploy with `--tags configarr`
  from daniel-box.
- **No web UI** or Authelia; it targets the cluster sonarr/radarr
- **One-shot (ephemeral):** a nightly k8s CronJob, plus a `configarr-deploy-gate` Job the
  deploy creates from it (via `k8s/cronjob-gate`) so a config change syncs immediately. That
  gate proves the new image RUNS — a container that never starts fails the deploy; a sync that
  runs and fails is reported and the deploy continues, deliberately: a transient *arr outage
  must not fail the deploy. No healthcheck / AutoKuma — a batch
  job, not a service; its health signal is a daniel-box host cron (`/opt/configarr-health`)
  that reads the last Job's outcome via `configarr_status.py` and pushes the "Configarr Sync"
  Kuma monitor. The health reader counts the gate's own Job like any other finished run,
  because that run genuinely performs the reconcile — `pi-peer-backup`'s gate run does not
  count toward its dead-man monitor, and that role's CLAUDE.md has the other side of the choice.

## Scope — what configarr manages
`delete_unmanaged_custom_formats` is **OFF** everywhere: Configarr never deletes a CF it did
not create.

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
  only option, then let Bazarr/Whisper add subs" fallback. Configarr reconciles the four local
  CFs above and nothing else — the 52 bespoke CFs and their scores are untouched.

  **`cutoffFormatScore` is 400, set in Sonarr's DB — NOT Configarr (2026-07-17).** A raw grab
  loses the +300/+100 English-sub bonus, so it scores in the low hundreds, stays under the
  cutoff and keeps upgrading until Sonarr finds a subbed release. **Only the +300
  `Anime English-Sub Groups` path is guaranteed to clear 400** — a listed-group sub lands ≥400,
  and a release that trips only the milder +100 title CF scores ~105 and stays *permanently*
  upgrade-eligible. That is intended: the cutoff stays 400 so such an episode keeps searching.
  The accepted cost (2026-07-18 review) is ongoing RSS/search churn for it plus a hard delete
  on each upgrade — Sonarr's recycle bin is off, so an upgrade has no undo. The live CF
  definitions stay in Sonarr's DB (on its Longhorn PVC, backed up to B2), and
  `files/baseline/anime-profile.json` is a read-only snapshot of the Anime profile and its CF
  scores — this repo's only git record of them, documentation rather than something applied.

**Accepted trade-off from the recyclarr port:** `include:`'s `reset_unmatched_scores` makes
Configarr authoritative for scores *inside the guide profiles it syncs*, which is why the
cutover reset 3 Radarr CFs from a stray `-10000` to `0`. The guide profiles are the source of
truth now, and this reaches only `WEB-1080p` and `HD Bluray + WEB` — never the bespoke Anime
scheme.

**To extend the Anime defense:** add release groups to the `^(NTRX)$` alternation, or a new
local CF, in `templates/config/config.yml.j2`. A `quality_profiles` block for the Anime
profile would make Configarr authoritative over it and revert UI edits, so weigh that against
the bespoke scheme first.

## Editing
- Sync config: `templates/config/config.yml.j2`
- Refresh the Anime baseline snapshot:
  ```bash
  uv run python scripts/diagnostics/probe.py arr sonarr "/api/v3/qualityprofile" --json \
    | jq '.[]|select(.name=="Anime")' > ansible/roles/k8s/configarr/files/baseline/anime-profile.json
  ```
- Health evaluator: `files/configarr_status.py` (exit-code/output verdict logic, tested in
  `tests/test_configarr_status.py`) — copied into `/opt/configarr-health` on
  daniel-box, where a cron reads the last Job and pushes Kuma.
- Deploy (from daniel-box): `./scripts/deploy.sh --tags "configarr"` —
  the k8s role also runs a one-off `configarr-deploy-gate` Job so the edit syncs immediately.
- Verify a sync: `kubectl -n homelab logs job/configarr-deploy-gate` — a healthy run lists
  the managed CFs and reports no errors.
- Unit tests: `uv run pytest ansible/roles/k8s/configarr/tests`.
