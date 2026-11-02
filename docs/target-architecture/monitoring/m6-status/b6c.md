### B6c registry GC

B6c joined M6 on 2026-11-02 (#6926). D5's registry PRs retired the push store's GC phase
only, and the mirror phases of `registry-gc.sh` kept pushing to the `Registry GC` tile, so
C-1a's gate could never clear while the plan left the push to D5.

**The shadow PR** is the PR that adds this file. It changes four things:

- `registry-gc.sh.j2` sources `homelab-check-lib.sh` and calls
  `write_check_textfile registry-gc` in `finish`, before the push and on every run, with a
  `DEADLINE_S` of 612000, the tile's interval. A run with no token or a refused push still
  writes the file.
- `rules/registry.yaml` gains `RegistryGcFailed` and `RegistryGcNotReporting`, both
  `warning` and `shadow: "true"`, with promtool cases in `registry.test.yaml`.
- `KUMA_RETIRING_TAGS` gains `registry-gc`.
- `kuma-cutover.yaml` gains the `[340h10m]` term for `Registry GC`.

The tick applies the script change (`k8s_host_files`), and Flux applies the two k8s halves
through `promote`. NotReporting fires in shadow from its deploy until the first Sunday run
writes the file.

**Still to come, in order:**

1. The soak read, one week after the first Sunday run that writes the textfile.
2. Cutover-1a: delete `registry-gc.json` and the `^registry-gc$` status-page pattern from
   `uptime-kuma`, and move `registry_gc_push_token` to `PENDING_RETIREMENT`. The tag must
   have been in the deployed `KUMA_RETIRING_TAGS` for a week first.
3. Cutover-1b: drop `shadow` from both rules and remove the `Registry GC` term.
4. Cutover-2: drop the push, `PUSH_TOKEN`, `KUMA_HOST` and the `kuma-push-lib.sh` source from
   `registry-gc.sh.j2`. The healthchecks.io dead-man stays.
   `.claude/hooks/tests/test_block_protected_bash.py` asserts the token in the script's
   protected paths, so the same PR updates it. With no token in the render, the script is no
   longer a STUB, and `probe.py applied` compares it (#6917).
5. The token PR, a week after Cutover-1b, retires `registry_gc_push_token`. C-1a waits for it.
   It is the last retirement the plans schedule, so it also deletes
   `scripts/secrets_mgmt/pending_retirement.py`, its readers in `consumers.py`,
   `cluster_vars.py` and `cluster_vars_check.py`, its `HOST_INPUTS` entry in
   `scripts/renderer/renderer_lib/host_inputs.py`, and the tests that hold it. M5's plan
   scheduled that deletion for its PR 5. #6371 closed with the clause moved here.

**Cutover-1a, the tile goes** (branch `m6-b6c-cutover-1a-tiles-go`). It deletes the `registry-gc`
declaration from `static-monitors.yaml.j2` and the `^registry-gc$` status-page pattern.
"Backups & Storage" keeps `match: []`, because four monitor-bridge rows pin themselves into it.
`registry_gc_push_token` enters `PENDING_RETIREMENT`, because no cluster render references it
once the tile goes. `registry-gc.sh.j2` keeps pushing into the deleted token until Cutover-2.
`KUMA_RETIRING_TAGS` lists `registry-gc`, so `swallowed_verdicts` does not page the refused push.
Both rules stay `shadow`, so this PR adds no pager and removes one.

**What the tile's removal moved in the tests.** The floors that counted the tile fell by one:
the monitor census (`>= 28` to `>= 27`), the declaration index (`> 30` to `> 29`), the renderer's
index (`> 25` to `> 24`), and the three push-tile or token-name floors (`>= 20` to `>= 19`). The
tile was a named member of two censuses, so `GATED_MEMBERS` swaps it for `aide-daniel-pi.json`,
`_INCIDENT_TILE` becomes `aide-daniel-pi`, and `KNOWN_IDS` drops it. The worst-run guard read the
tile's interval twice. It now reads the script's `DEADLINE_S`, which the same guard already
equated to the rule literals. `test_the_deleted_b6c_tile_is_not_declared` fails if the tile is
declared again.

**The gates are not read, and cannot pass yet.** Read on 2026-11-02 at 18:29Z:

- `homelab_check_last_run_timestamp_seconds{check="registry-gc",origin="daniel-box"}` reads
  1793643955 (2026-11-02T18:25:55Z) and `homelab_check_success` reads 1. The textfile exists, but
  it came from a run outside the Sunday 04:20 schedule. The first scheduled run that writes it is
  2026-11-08, so the soak read is due on or after 2026-11-15.
- The live `monitor-bridge-script-verdicts-logs` ConfigMap lists `registry-gc` in
  `KUMA_RETIRING_TAGS`. #6931 landed on 2026-11-02, so the tag has been deployed for a week on
  2026-11-09 at the earliest, and Cutover-1a waits for that.
- `monitor_status{monitor_name="Registry GC"}` reads 1, and `ALERTS{alertname=~"RegistryGc.*"}` is
  empty. The tile is live and both rules are quiet.

The operator reads the soak, with M2's 8-minute margin over `[168h:1m]`, before merging. The
two-hour no-pager bound counts from the `uptime-kuma` apply of this PR. It ends at the
`observability` apply of Cutover-1b. If Cutover-1b has not applied by then, revert this PR. Its
rules are still `shadow`, so the revert restores Kuma as the only pager, and the tile goes green
on the next push because the token stays in `secrets.yml` until the token PR. A Cutover-2 that
has merged is reverted first, so the cron pushes again.

**Cutover-1b, the rules page** (branch `m6-b6c-cutover-1b-rules-page`, stacked on Cutover-1a).
`RegistryGcFailed` and `RegistryGcNotReporting` drop `shadow`, and the `kuma-cutover` group
loses B6c's `Registry GC` term. The group stays, because B9's six terms and Phase A's terms
remain. Both rules are `warning`, which posts to Discord alone, the channel mix the tile had.

**Routing.** `test_alertmanager_routing_b6c_registry_gc.py` is a sibling of the routing module,
which is at its line cap. It walks both rules through the rendered routing tree: each reaches
Discord alone, muted for the weekly reboot. A rule that regains `shadow` walks to the blackhole
and fails it. The main module's censuses read no `registry.yaml` rule, so none needs a row.

**Inhibits: none added.** `RegistryGcNotReporting` ends in `unless on (origin) (up{job="node"}
== 0)`, and a dead node exporter leaves `RegistryGcFailed` no series, so `ScrapeTargetDown`
stays the one page. The generic `HostCheckOverdue` deadman is already muted beside the
`NotReporting` pair by the inhibit on `equal: [check, origin]`.

**Soak effect: none.** Both rules are `warning` and carry no `verify` label, so neither the
health gate nor the soak reads them, and `renderer_lib/soak.py:longest_for` counts critical
rules only. A firing `RegistryGcFailed` therefore does not block `registry`'s `promote`.

**Precondition.** `RegistryGcNotReporting` pages when the textfile is older than 612000s, which
is 7 days 2 hours. The file was last written 2026-11-02T18:25:55Z, so it ages out at
2026-11-09T20:25Z unless a run refreshes it. The scheduled run of 2026-11-08 04:20 comes first.
A merge before a file exists at all pages at once, because the rule's absent arm fires on no
series.

**Cutover-2, the push stops** (branch `m6-b6c-cutover-2-setup-plane`, stacked on Cutover-1b).
`registry-gc.sh.j2` loses `PUSH_TOKEN`, `KUMA_HOST`, `KUMA_RESOLVE_IP`, the `kuma-push-lib.sh`
source and the push call. It keeps the textfile write and the healthchecks.io dead-man.

**Which plane applies it.** `k8s_host_files` is a setup role, and `setup_routing.py --host
daniel-box` places it on the tick (`initial_setup.yml --tags k8s_host_files`, `on_tick_host`
true). `deploy_changes.setup_tags_for` maps the script's path to `k8s_host_files`, with
`broad_setup` set and `broad_manual` clear. No hand apply is owed, and the playbook entry is
gated to daniel-box, so no other host owes the role.

**Four things follow from a render that holds no token.**

- `tasks/registry.yml` loses the script task's `no_log` and the three tasks that recorded the
  script's SHA-256, because the render holds no STUB now and `probe.py applied` hashes it. The
  old record file stays on daniel-box, and nothing reads it.
- The script was the last secret-bearing host script, so the census behind the hook's arm 2
  (`secret_bearing_host_paths`) is empty. The hook's tests run over a fixture checkout whose
  Ansible tree installs a script with a fake token. Each flagged and clean pair stays.
- These assertions retired because their subject is gone: `MUST_FIND`'s `registry-gc.sh` entry and
  its test, `test_tiering_the_address_did_not_drop_its_script_from_the_census`, the
  `len(census) >= 1` line of the world-readable test, `KNOWN_SECRET_SCRIPTS` with
  `test_the_census_finds_the_known_members`, and the two hook tests that derived a set from the
  hook's own tree (`..._in_a_subdirectory_of_the_hooks_checkout_...` and `..._in_no_checkout_...`).
  `test_the_k8s_host_files_apply_writes_the_sha256_record` retired with the record task.
- The push corpus of `test_cron_scripts_publish_via_pr.py` falls from 5 to 4 templates. Its named
  members become the aide and rkhunter checks, and `_MIN_CORPUS` stays 3.

**How to verify.** After the tick applies it and the next run: `journalctl -t registry-gc` shows
no `push failed` line, `probe.py applied` reads `/usr/local/bin/registry-gc.sh` as `ok`, and
`homelab_check_last_run_timestamp_seconds{check="registry-gc"}` advances. The token PR retires
`registry_gc_push_token` a week after Cutover-1b.
