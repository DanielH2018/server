# Docs guard — handoff of the open work

This page hands the rest of the facts-and-memory plan to another session. It states what
is done as of 2026-11-15, what is open, the order to take it in, and what the sweep
taught. Delete this page in the PR that closes the last open item.

[ADR-0031](../../adr/0031-each-kind-of-fact-has-one-stateless-detector.md) holds the
decision, and each slice plan beside this page holds its own record. Read the ADR and
`.claude/rules/facts.md` before any of the work below.

## Where the plan stands

- [G6](g6-repair-audit-defects.md), [G8](g8-memory-resolver-and-replica.md) and
  [G10](g10-value-rows.md) are done. The operator declined G10's step 3 on 2026-11-14.
- [G9](g9-blocking-gate.md) stopped at its measurement on 2026-11-13. The diff notice
  stays advisory.
- [G7](g7-removed-names.md) has steps 1 to 4 done. Step 5 is open.
- [G11](g11-binding-sweep.md) has steps 1 and 2 done. Step 3 is open.

## Open work, in order

Take the items in this order. Each names the effort it needs from an Opus session.

1. **The value rows the sweep owes.** Medium effort. Every row is listed under
   *Value rows owed* below. `scripts/docs/doc_values.py` holds the `VALUES` table, and
   `.claude/rules/generated-docs.md` says how to add a row and which values get none.
   The sweep's batches were barred from adding rows, because eighteen batches appending
   to one list would have conflicted at merge.
2. **Mask the generated block in `has_prose`.**
   [#8787](https://github.com/DanielH2018/server/issues/8787), medium effort. Three
   sections hold only a `generated_from` block and can never leave the backlog: `At a
   glance` in the flux, blackbox-exporter and karma role docs.
3. **The sections the sweep kept back.** Medium effort, in a solo session and not a
   fan-out batch. `.claude/rules/docker.md` has two uncited sections, and
   `.claude/skills/issue-fanout/SKILL.md` has four. The skill describes the batch
   pipeline, so an issue that cites it reads as solo-only.
4. **G11 step 3.** High effort, solo. Rewrite `.claude/rules/facts.md` to the end-state
   model in the G11 plan's design, and replace tiers 2 and 3 of
   `.claude/agents/homelab-docs-freshness-reviewer.md` with a read of the sections that
   `fact_status.py report` ranks as loaded most and cited least. That agent's four
   uncited sections are bound in the same change. `facts.md` loads with every
   instruction file, so a wrong sentence there reaches every session.
5. **The pilot's comparison, due 2026-12-14.** High effort. Count the notice rows that
   the root `CLAUDE.md`'s eight content citations drew in the 30 days after
   [#8707](https://github.com/DanielH2018/server/pull/8707), and grade each unedited
   row as a false sentence or not. G9 measured 0.29 unedited rows per content citation,
   and 6% of them left a false sentence. The sweep's citations can be measured the same
   way from 2026-12-15.
6. **G7 step 5.** High effort, and it needs the operator's ruling first. The step moves
   the replay into `fact_status.py report`, measures a second window, and promotes a
   removed-name class to an error only when fewer than one graded row in five is a
   false alarm. No class was under that bar on 2026-11-14.

Two things are deliberately not on this list. ADR-0031 defers an outbox for memories
written on a replica host until refused writes are counted. Two directive sections cite
nothing by design: "A check that reaches over a network measures its transport first"
in `.claude/rules/python-layout.md`, and "Never print a secret value" in
`.claude/rules/k8s-templates.md`.

## Decisions that stay with the operator

- Whether any removed-name class blocks a commit (G7 step 5).
- What the 2026-12-14 number means for the citations the sweep added.
- Any edit to the memory store. daniel-box holds the store, and daniel-server's copy is
  read-only by design, so a session on daniel-server cannot write a memory.

## What the sweep taught

- **A batch's own correction can be wrong.** In at least five of the eighteen batches
  the separate reviewer found a defect that the fix round repaired. One batch wrote
  that every landing under `DEPLOY_HOLD` merges, which is false for a pin bump. Keep
  the review phase on any batch that rewrites a sentence.
- **Two batches that cite one test conflict at merge.** Each writes its `# fact:`
  comment at the top of the same test function. The home-automation batch and the Home
  Assistant skills batch collided this way in
  `scripts/home_assistant/tests/test_ha_state_checks.py`. Group batches so that docs
  held by one test directory share a batch, or rebase the second PR and keep every
  comment.
- **A PR that touches the lander's own surface needs a hand landing.** The server
  lander refuses a PR that edits a file under `.claude/agents/`, a test under
  `scripts/deploy_tools/tests/`, or the claude-code host component. Run
  `probe.py lander` first, then land with `land.sh`.
- **A full set of landing slots reads as `needs-input` on daniel-box.**
  [#8797](https://github.com/DanielH2018/server/issues/8797) holds the defect. The PR
  is ready, and a hand landing finishes it.
- **A landing that merges and then ends `refused` graded nothing.** It happens when a
  landing module changes on master mid-landing. Re-run `land.sh --pr <n> --fresh
  --detach` for the verdict.
- **An eval case that quotes an edited section must be re-pasted.**
  `evals/tests/test_eval_cases.py` names the case.
- **A stated value may already have a detector.** Grep the tests for the doc's name
  before rewording a number.

## What the citation grammar cannot reach

The batches named three shapes they could not cite. Each is a candidate change to
`scripts/lib/facts/citations.py`, and none is decided.

- **A list entry in YAML.** A YAML atom is a dotted key, so it cannot select the
  `port` of one entry in `cluster/services.yml`. Eight of the owed values have such a
  source.
- **A root-level file.** A path atom needs a slash, so the root `CLAUDE.md` and
  `renovate.json` cannot be cited.
- **An absence.** "Neither Deployment pushes a heartbeat" is true and no atom holds it.
  Only a test can.

## Small leftovers the batches named

- The heading of `k8s/roles/cloudflare-ddns/CLAUDE.md` says the role "keeps two
  Cloudflare A records". It keeps three. The batch left the heading alone, because a
  section's key is its heading text. Change it together with every `# fact:` comment
  and doc that names the key.
- A `DECIDED:` marker in `scripts/dev/prune_worktrees.py` says "the weekly cron
  PRUNES". The sweep is a systemd timer on both k3s hosts. The worktree-cleanup skill
  cites the marker by its text, so the two change together.
- `k8s/roles/game-stats/CLAUDE.md` says `stats_lib.py` is "~250 non-comment lines". The
  batch counted 212.

## Value rows owed

Each entry gives the doc, the value as the doc states it, and the source a row would
read. A measured value with no source in the tree is left out: it stays prose.

`hosts/components/claude-code/CLAUDE.md`, with sources relative to that component:

- "two minutes after each audit run": the `:02/` offset in `OnCalendar` of
  `units/homelab-claude-guardrail-ack.timer.j2`.
- "a 10-second connect timeout": `ConnectTimeout=10` in `templates/sync-artifacts.sh.j2`.
- "`$10`, 60 turns and a 30-minute wall clock per run, and six runs a UTC day":
  `budget_usd`, `max_turns`, `run_timeout_s` and `daily_cap` in
  `templates/claude-triage-pins.j2`.
- "holds for two polls" and "more than three falls":
  `defaults/main.yml:claude_code_triage_poll_streak` and
  `:claude_code_triage_poll_storm_size`.
- "at most once an hour": `-mmin -60` in `ExecCondition` of
  `units/homelab-claude-triage-poll-alert.service`.

`hosts/components/deploy-ui/CLAUDE.md`:

- "polls four panels every 10 seconds" and "polls `/api/log` every 2 seconds": the two
  `setInterval` calls in `hosts/components/deploy-ui/files/deploy_ui.html`.

The arr roles:

- qbittorrent, "five-minute startupProbe window": `periodSeconds` times
  `failureThreshold` of the sidecar's `startupProbe` in
  `k8s/templates/wireguard-sidecar.yml.j2`.
- qbittorrent, "within 120 seconds":
  `k8s/roles/qbittorrent/files/qbittorrent_job.py:EXTERNAL_WAIT_SECONDS`.
- arr-notification, "retries for 60 seconds":
  `scripts/renderer/job_files/flux_job_connect.py:RETRY_SECONDS`.
- janitorr, the rungs "5, 10, 15, 20": `media-deletion` in
  `k8s/roles/janitorr/templates/config/application.yml.j2`.
- janitorr, "75% used":
  `hosts/components/k3s-host-checks/defaults/main.yml:k3s_disk_max_pct`.
- configarr, the scores -10000, +200, +300 and +100: `custom_formats` in
  `k8s/roles/configarr/templates/config/config.yml.j2`.
- configarr, "52" bespoke custom formats:
  `k8s/roles/configarr/files/baseline/anime-cf-scores.json`.
- configarr, `cutoffFormatScore` 400:
  `k8s/roles/configarr/files/baseline/anime-profile.json`.

The DNS roles:

- pihole-exporter, `:9617`: the entry's `metrics` port in `cluster/services.yml`.
- pihole, `300s`, stated twice: `GUARD_WAIT_SECONDS` in
  `k8s/roles/pihole/templates/precheck/guard.yaml.j2`.
- pihole, "port 80" for the web UI: the pihole entry's `port` in `cluster/services.yml`.
- pihole-exporter, "on port 80": the admin-UI rule in
  `k8s/roles/netpol-baseline/templates/networkpolicy-pihole.yaml.j2`.
- cloudflare-ddns, "15 minutes": the `[15m]` window of both `*UpdateSilent` rules in
  `k8s/roles/loki-homelab/files/rules/homelab-logs.yaml`.

The cluster roles:

- flux, metrics port 8080: the literal in
  `k8s/roles/flux/templates/networkpolicy.yaml.j2`.
- flux, "90-day expiry": the `external-90d` tier of `promote_token` in
  `secrets/secret_rotation.yml`.
- cluster-base, "four" PriorityClasses: the count of `kind: PriorityClass` in
  `k8s/roles/cluster-base/templates/priorityclass.yaml.j2`.
- metallb, "two address pools": the count of `kind: IPAddressPool` in
  `k8s/roles/metallb/templates/metallb-pool.yaml.j2`.
- crowdsec, "every 5 minutes":
  `k8s/roles/crowdsec/defaults/main.yml:crowdsec_k8s_home_ips_period_minutes`.
- image-builder, "uid 1000": `runAsUser` in
  `k8s/roles/image-builder/templates/build-job.yaml.j2`.

The monitoring and exporter roles:

- loki-homelab, `5Gi` and `744h`:
  `k8s/roles/loki-homelab/defaults/main.yml:loki_homelab_size` and
  `:loki_homelab_retention`.
- loki-homelab, port `9093` and `5m`: the ruler's `alertmanager_url` in
  `k8s/roles/loki-homelab/templates/configmap.yaml.j2`, and `interval:` in
  `k8s/roles/loki-homelab/files/rules/homelab-logs.yaml`.
- node-exporter, CPU limit `1`, liveness `300s` and readiness `10s`:
  `k8s/roles/node-exporter/templates/daemonset.yaml.j2`.
- nut-exporter, `:9199`, and gpu-exporter, `:9101`: each entry's port in
  `cluster/services.yml`.
- peanut, `fsGroup: 1000`: `fs_group=1000` in
  `k8s/roles/peanut/templates/deployment.yaml.j2`.

The storage roles:

- media-volume, "ten media-stack roles": the entries whose `depends_on` names
  `media-volume` in `cluster/services.yml`.
- media-volume, "its 300s deadline": `activeDeadlineSeconds` in
  `k8s/roles/media-volume/templates/verify/job.yaml.j2`.
- pi-peer-backup, "weekly-backup-d6": the entry's `weekly_backup_claims` shard in
  `cluster/services.yml`.
- longhorn-backup, "`weekly-backup-d0` to `weekly-backup-d6`":
  `scripts/lib/longhorn_lib/longhorn_backups.py:WEEKLY_SHARDS`.

The app roles:

- home automation, ical-proxy, "every 15 min": the `REFRESH_INTERVAL` env value in
  `k8s/roles/ical-proxy/templates/deployment.yaml.j2`.
- docs, the `8080` in the `curl` line: the docs entry's `port` in `cluster/services.yml`.
- game-stats, `:9420`: the entry's `metrics` port in `cluster/services.yml`.
- autofix-bridge, "sidecar `DRY_RUN` (live)": the `DRY_RUN` env value in
  `k8s/roles/autofix-bridge/templates/deployment.yaml.j2`.
- autofix-bridge, "run `live` by host_vars":
  `inventory/host_vars/daniel-box.yml:autofix_fake_remux_replace_mode`.
- slskd, "every 5 minutes": `slskd_k8s_soularr_interval`.
- tdarr, "3Gi" and "1Gi": `tdarr_k8s_server_size` and `tdarr_k8s_configs_size`.
- tdarr, "`devic.es/dri`": `tdarr_k8s_dri_resource`.
- jellyfin, "16 GiB": `jellyfin_k8s_snapshot_max_size`.
- bento-pdf, "101": `bento_pdf_k8s_uid`.

The skills:

- worktree-cleanup, "Exit 64": `scripts/lib/exit_codes.py:USAGE_ERROR`.
- worktree-cleanup, "the same four-layer ladder": the layers in
  `scripts/lib/worktrees.py:merged_layer`.
- worktree-cleanup, "older than a day": `--expire=1.day.ago` in
  `scripts/lib/git.py:repair_object_store`.
- worktree-cleanup, "A weekly timer": `OnCalendar` in
  `hosts/components/worktree-sweep/units/homelab-worktree-sweep.timer`.
- renovate-prs, "3 days", "7", "1 day", "8 days, not 10" and "a 7-day grace": the
  `DIGEST_SOAK_DAYS`, `VERSION_SOAK_DAYS`, `FAST_DIGEST_SOAK_DAYS` and
  `PENDING_GRACE_DAYS` constants in
  `hosts/components/renovate-notify/files/pending_logic.py`.
