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
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=configarr` with exit 5
- **Images:** `ghcr.io/raydak-labs/configarr` (`configarr_k8s_image`), `python`
  (`configarr_k8s_health_image`), `alpine/k8s` (`configarr_k8s_health_kubectl_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (D5, Wave D). Its `containers_list` entry carries
  `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it. A merged change
  reaches the cluster when `promote` publishes and verifies the next bundle. To give it back
  to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain revert of the
  flag leaves two writers on the objects.
- **Host: daniel-box (k8s CronJob).** This role renders `templates/config/config.yml.j2` and
  stages `files/configarr_lib/configarr_status.py` into a ConfigMap. Edit the config HERE.
- **No web UI** or Authelia; it targets the cluster sonarr/radarr
- **One-shot (ephemeral):** a nightly k8s CronJob, plus a gate Job that Flux runs from it in
  `configarr-verify` on every role revision, so a config change syncs immediately (below).
  No healthcheck / AutoKuma — a batch
  job, not a service; its health signal is a second CronJob, `configarr-health`
  (`templates/health-cronjob.yaml.j2`), every 10 minutes. It reads the last Job's outcome via
  `configarr_lib/configarr_status.py` through its own ServiceAccount and pushes the
  "Configarr Sync" Kuma monitor. The health reader counts the gate's own Job like any other
  finished run, because that run genuinely performs the reconcile — `pi-peer-backup`'s gate
  run does not count toward its dead-man monitor, and that role's CLAUDE.md has the other side
  of the choice.
- **The health reader was a daniel-box host cron until H2.5b.** The absent arms for the cron,
  `/opt/configarr-health`, `/usr/local/bin/configarr-health.sh` and the `configarr` drift
  fragment live in `ansible/roles/setup/k8s_host_files/tasks/configarr.yml`, which the setup
  plane applies. D5's conversion PR moved them out of `tasks/main.yml` ahead of the Flux flip.
- **`flux_cronjob_gates` in `defaults/main.yml` names the two gate Jobs.** The renderer copies
  each CronJob's `jobTemplate` into `configarr-verify` as `<cronjob>-deploy-gate-<spec
  digest8>`, so the reconcile's Job is `configarr-deploy-gate-<spec digest8>` and the reader's
  is `configarr-health-deploy-gate-<spec digest8>`. They replaced the `k8s/cronjob-gate` runs
  at configarr's flip. configarr's gate is a post-apply mutation whose `homelab/rerun-safe`
  names
  `ansible/roles/k8s/configarr/tests/test_configarr_gate.py::test_a_second_sync_reports_no_change`.
  configarr-health's gate is a verify. The renamed Job keeps `app: configarr`, and the health
  reader selects by that label and by finish time, never by name, so it still counts a gate
  run. `ansible/roles/k8s/configarr/tests/test_configarr_gate.py::test_the_reconcile_gate_is_a_rerun_safe_mutation_the_health_reader_counts`
  runs the reader's own predicate over the rendered Job.
- **The Flux gate is stricter than the Ansible one was.** A sync that exits non-zero fails
  `configarr-verify` and holds the revision, where `k8s/cronjob-gate` reported it and let the
  deploy continue. The copy carries no ownerReference to the CronJob, so `concurrencyPolicy:
  Forbid` no longer keeps a scheduled 04:30 firing off a gate run; the sync converges, so an
  overlap writes nothing twice.
- **Both gates start together, and that is accepted, not ordered.** Flux applies the two gate
  Jobs in one `configarr-verify` Kustomization, so the health gate can finish first and push
  the previous run's verdict. That push repeats what the last `*/10` run already pushed, so
  Kuma makes no transition. The next scheduled run reports the fresh sync within 10 minutes,
  the same wait the nightly 04:30 sync has. A failed sync also fails `configarr-verify`, so
  the tile is not the only signal. The health gate stays because it is the only deploy-time
  proof of the reader's images and ServiceAccount, which Renovate bumps. The reasoning is at
  `ansible/roles/k8s/configarr/defaults/main.yml:DECIDED: the two gates run unordered`
  (#5617).
- **The push URL, token included, lives in its own Secret, `configarr-health`**
  (`templates/health-secret.yaml.j2`), and reaches the pod as `KUMA_PUSH_URL` through
  `secretKeyRef`. It is a whole URL rather than a bare token so that it stays out of
  `ansible/roles/k8s/manifests/files/standalone_secret_keys.json`: a change to that file fans a
  narrowed apply out to every `k8s/manifests` caller. The reader's code ships in the
  `configarr-health` ConfigMap, beside `host_lib.py` and `kuma_push.py` from `setup/common`, and
  no secret goes in it (`ansible/tests/k8s/test_no_secret_in_a_moved_jobs_configmap.py`).
- **The reader also exports its verdict** (M6 B6b). It patches `homelab/check-verdict`,
  `homelab/check-last-run` and `homelab/check-message` onto the `configarr-health-verdict`
  ConfigMap, which the role renders with no annotations so the reader is their only writer.
  kube-state-metrics exports the verdict, and the `pod-producers` shadow rules read it. A
  failed export makes the reader exit 1, which also fails its gate Job.
- **The health pod carries `app: configarr-health`, never `app: configarr`.** The reader judges
  the Jobs labelled `app=configarr`, so that label would make it judge its own runs. uptime-kuma's
  `netpol_from` admits `configarr-health`.

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
- Health evaluator: `files/configarr_lib/configarr_status.py` (exit-code/output verdict logic, tested in
  `tests/test_configarr_status.py`) — staged into the `configarr-health` ConfigMap and mounted
  at `configarr_lib/`, beside `configarr_lib/configarr_health_logic.py`, under the
  `configarr_health.py` reader.
- Verify the health reader: `kubectl -n homelab logs -l app=configarr-health --tail=5` — its
  `configarr-health: status=up` line is the verdict, and `configarr-health: pushed` is the push.
- Deploy: merge the change. `promote` publishes the revision, and Flux runs the gate Job
  `configarr-deploy-gate-<spec digest8>` in `configarr-verify`, so the edit syncs immediately.
- Verify a sync: `kubectl -n homelab get jobs -l app=configarr`, then `kubectl -n homelab logs
  job/<newest>` — a healthy run lists the managed CFs and reports no errors.
- Unit tests: `uv run pytest ansible/roles/k8s/configarr/tests`.
