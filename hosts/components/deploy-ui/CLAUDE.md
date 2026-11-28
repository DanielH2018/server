# `hosts/components/deploy-ui` — the host half of deploy.local (daniel-box)

`deploy-ui.service`, `User={{ sys_user }}`, cwd the primary checkout. `homelab-daniel-box` ships
it, with its in-flight panel. It serves the page
and API `k8s/roles/deploy-ui/` routes to, and runs `land.sh` and
`gh` exactly as the operator would from a terminal. It starts no deploy, because Flux applies
every service. The page stays as the one browser view of landings in progress (operator
ruling, 2026-11-06). It also starts and stops the game servers, through the scale-only
`homelab-games` identity (ADR-0033).

## Trust

The daemon does no authentication. Two gates stand in front of it; removing either is a
visible decision:

- ufw admits `deploy_ui_port` from the pod CIDR and both node IPs only, because flannel
  masquerades pod→LAN traffic to the sending node. The `ufw:` rules in `component.yml` declare
  them. ENFORCED: `hosts/lib/tests/host_baselines/test_deploy_ui_component.py`.
- Authelia `two_factor` gates `deploy.local`: `auth_tier: two_factor` on the `deploy-ui`
  containers_list entry, rendered into the authelia role's access_control rules.
  ENFORCED: `tests/services/authelia/test_authelia_access_tiers.py`.

DECIDED: a pod inside the cluster reaches the daemon without Authelia. Accepted 2026-09-10 —
a hostile pod already has kubectl-adjacent reach. Nothing stops it at the network layer:
netpol-baseline is INGRESS ONLY (`k8s/roles/netpol-baseline/templates/networkpolicy.yaml.j2`),
so pod egress is unfenced on this cluster.
Every POST also needs `X-Deploy-UI: 1` so a cross-site form cannot ride the Authelia cookie.

DECIDED: such a pod can also start and stop the game servers. Accepted 2026-11-14 with ADR-0033,
because the daemon's `homelab-games` token can change the replica count of the Deployments
`game_servers` lists and nothing else.

## Turning it off

- To stop it on daniel-box for a while, run `sudo systemctl mask --now deploy-ui.service`
  (**operator-only**).
  To remove it, drop `deploy-ui` from `components:` in `hosts/daniel-box/package.yml`; the next
  package version stops it, and reconcile deletes its three ufw rules over two runs, because
  the arm deletes at most two in one run.
- Remove the `deploy-ui` entry from `containers_list` in `k8s/services.yml` and
  deploy, which drops the route (`k8s/roles/deploy-ui/`) and `deploy.local`.

## In flight

The lock panel reads the git-tree lock only; the turn's locks are the *Landing queue*'s. The
unit passes its path in as
`DEPLOY_UI_TREE_LOCK`, and the panel lists no other lock file
(`hosts/components/deploy-ui/tests/test_deploy_ui_app.py::test_inflight_ignores_other_lock_files_in_the_tree_locks_directory_is_flagged`).
A missing file leaves `locks_watched` empty, which the page renders as its own state rather
than `free` (`hosts/components/deploy-ui/tests/test_deploy_ui_app.py::test_inflight_with_no_lock_files_says_so_is_flagged`).

Two sources, because neither alone says who holds what. One merged-stream `fuser` call over
the tree lock names the processes with the file OPEN. A process blocked on the lock has the
file open too, so fuser lists the waiter exactly like the holder (measured: a `flock`
holder and a `flock -w` waiter both appear). `/proc/locks` is the kernel's own table: a
granted flock per file, and a `->` line with a live pid for each process blocked on it.
The pid on a granted line can be dead when a `flock` child took the lock on an inherited
descriptor, so the holder comes from fuser and only the waiter from `/proc/locks`
(`hosts/components/deploy-ui/tests/test_deploy_ui_app.py::test_inflight_lists_the_tree_lock_holder_and_waiter_is_clean`).

`ps` with `ppid` folds each process family to one row carrying `locks` (held) and
`waiting_on`. A landing (`land.py`, its `uv run` wrapper and its children) is one `land`
row (`hosts/components/deploy-ui/tests/test_deploy_ui_reads.py::test_runs_fold_each_family_to_its_root_is_clean`).
A holder that is not a landing, such as `homelab-checkout-sync` on the tree lock, is a `lock`
row rather than nothing (`hosts/components/deploy-ui/tests/test_deploy_ui_reads.py::test_runs_bare_lock_holder_is_a_row_is_clean`).
A `grep` for a run is not a run (`hosts/components/deploy-ui/tests/test_deploy_ui_reads.py::test_runs_ignore_grep_shells_is_flagged`). fuser lists only processes whose `/proc/<pid>/fd` this
user can read; every landing here runs as `{{ sys_user }}`, the daemon's own user.

A landing row carries its PR's title. A landing merges its PR mid-run, which drops it from
`gh pr list`, so `_pr_title` reads the title with `gh pr view <n>` and caches it for the
daemon's life (`hosts/components/deploy-ui/tests/test_deploy_ui_app.py::test_inflight_landing_carries_its_pr_title_once_fetched_is_clean`).
A failed lookup leaves the title blank and is retried on the next poll; it
never makes the lock panel unavailable (`hosts/components/deploy-ui/tests/test_deploy_ui_app.py::test_inflight_title_lookup_failure_leaves_panel_up_is_flagged`).
The page links each PR number to its GitHub page (`hosts/components/deploy-ui/tests/test_deploy_ui_app.py::test_page_links_pr_numbers_to_github_is_clean`).

The *process* column names the script a row runs, not its interpreter, so
`uv run python scripts/deploy_tools/land.py --pr 1` reads `land.py`. The full command line is
the label's hover text, and the log shows as its file name
(`hosts/components/deploy-ui/tests/test_deploy_ui_page_js.py::test_process_names_the_script_not_its_interpreter_is_clean`).

Only a `land` row is cancellable (`hosts/components/deploy-ui/tests/test_deploy_ui_app.py::test_cancel_lock_row_is_flagged`).

The daemon runs outside the venv. The unit renders `DEPLOY_UI_TREE_LOCK` from
`hosts/vars.yml:server_git_tree_lock`.

## Landing queue

The *Landing queue* table opens the page. It reads each landing's state record, the file
`scripts/deploy_tools/land_lib/transitions.py` rewrites at each change of state, and nothing on
it comes from a log sentence. The records sit in `/run/lock/homelab-land-line/` on daniel-box,
the turn host, and a landing on daniel-server writes its record there through ssh, so the
table shows the landings of both hosts. `files/deploy_ui_lib/deploy_ui_states.py` reads them,
and `/api/inflight` carries them as `landings`. The page renders the table from the in-flight
poll, so it adds no request, and it makes no GitHub call beyond the cached PR title.

- **Each row** is one PR's newest record: its state, by the label `STATE_LABELS` gives it, the
  time in that state, the states it passed through, and its progress (`done`/`total` `what`).
  `STATE_LABELS` holds a label for every member of `scripts/deploy_tools/land_lib/core/landing_state.py:State`
  (`hosts/components/deploy-ui/tests/test_deploy_ui_states.py::test_every_landing_state_has_a_label_is_clean`),
  and a state it does not know renders by its raw name
  (`hosts/components/deploy-ui/tests/test_deploy_ui_states.py::test_a_state_the_page_does_not_know_renders_by_its_raw_name_is_flagged`).
  A record that names its verdict, or that is older than a day, is not shown, and a PR landed
  by two users shows the newer record
  (`hosts/components/deploy-ui/tests/test_deploy_ui_states.py::test_the_newest_record_of_a_pr_wins_and_a_verdict_leaves_the_queue_is_flagged`).
- **The order** is the landing that holds the turn, then each waiting landing with its
  position, then the landings that stepped out, then the rest
  (`hosts/components/deploy-ui/tests/test_deploy_ui_states.py::test_inflight_lists_both_hosts_landings_with_state_progress_and_queue_is_clean`).
  The newest of `queued`, `holding`, `yielded`, `no-turn`, `rejoining` and `merged` in a
  record's history places it: `holding` holds the turn, `queued` waits and `yielded` stepped
  out. The waiting rank by the time each entered `queued`. The turn goes to the landing with
  the oldest ticket, the time it first asked
  (`scripts/deploy_tools/land_lib/core/queue_machine.py:may_take`). A landing back from a
  step-out keeps its ticket, so the turn can serve it before landings the page ranks above it
  (`hosts/components/deploy-ui/tests/test_deploy_ui_states.py::test_waiting_landings_rank_by_when_they_queued_then_the_stepped_out_is_clean`).
- **A landing that died** shows `died`, leaves the queue and sorts last. A record's pid
  belongs to the host that wrote it. A daniel-box landing is alive while its pid is in
  `/proc`
  (`hosts/components/deploy-ui/tests/test_deploy_ui_states.py::test_a_landing_whose_process_is_gone_here_leaves_the_queue_is_flagged`).
  A daniel-server landing is alive while its `homelab-land-pr-<n>.lock` on daniel-box is
  held, because it holds that lock over ssh and daniel-box frees it once the heartbeat stops
  (`scripts/deploy_tools/land_lib/pr_lock.py`). A free PR lock does not prove it died: a
  landing goes on without the lock when it cannot take it or its ssh session ends. So that
  row shows `liveness unknown` (`alive: null`), leaves the numbered queue so it cannot push
  a live waiter down, and sorts between the live and the died
  (`hosts/components/deploy-ui/tests/test_deploy_ui_states.py::test_a_landing_on_another_host_without_its_pr_lock_is_unknown_not_dead_is_flagged`).
- **Any user can create a file in the line's directory**, so the reader opens a record
  without following a link or blocking, reads only a regular file of at most
  `MAX_RECORD_BYTES`, and skips one that does not decode
  (`hosts/components/deploy-ui/tests/test_deploy_ui_states.py::test_a_link_a_fifo_or_undecodable_bytes_at_a_records_name_is_skipped_is_flagged`).
- **The turn's locks** show above the table: the turn lock, the serial lock and the PR in
  `homelab-land-serial.holder` while that PR's own lock is held, and the slots in use, all from
  `/proc/locks` (`turn`;
  `hosts/components/deploy-ui/tests/test_deploy_ui_states.py::test_turn_locks_name_the_serial_holder_and_count_the_slots_is_clean`).
  The daemon cannot import land_lib, so the file names are copies, held to land_lib's by
  `hosts/components/deploy-ui/tests/test_deploy_ui_states.py::test_the_lock_names_are_land_libs_is_clean`.
- A records directory or a record that cannot be read sets `landings_unavailable`, which the
  page shows in red, and leaves the rest of the panel up, because `cancel` reads the panel for
  its pids
  (`hosts/components/deploy-ui/tests/test_deploy_ui_states.py::test_an_unreadable_record_is_named_and_the_others_still_show_is_flagged`).
  A landing writes its record with the writer's umask, so a record another user leaves at
  `0660` is one the daemon's user cannot read.
- A daniel-box landing's row links its log, joined to its `land` row by PR number: the record's
  pid is the landing's python, and the `land` row's is its family root.

`test_deploy_ui_page_js.py` runs the page's own script under node against a stubbed DOM, so
it tests the code the browser loads; it skips where node is absent
(`hosts/components/deploy-ui/tests/test_deploy_ui_page_js.py::test_queue_renders_the_daemons_order_with_each_landings_place_is_clean`).
Both tables show elapsed time as minutes and seconds, `54m 03s`, and keep counting minutes past
the hour
(`hosts/components/deploy-ui/tests/test_deploy_ui_page_js.py::test_elapsed_reads_as_minutes_and_seconds_is_clean`).
`probe.py landing` carries `landings` and `turn` from the same read, and prints one line per
landing (`scripts/diagnostics/probe_lib/landing_blockers.py:queue_line`).

## Changes

The *Changes* table shows one row for each open change, keyed by its PR, or by its fan-out
batch before the batch has a PR. Each row names its stage, its state within the stage, the
time in that state and what it waits on. `/api/changes` carries the rows, and
`hosts/components/deploy-ui/files/deploy_ui_lib/deploy_ui_changes.py:change_rows` is the one mapping from the sources to
a stage (`hosts/components/deploy-ui/tests/test_deploy_ui_changes.py::test_a_batch_its_pr_and_its_landing_are_one_row_in_the_landing_stage_is_clean`).
Each stage's state comes from the tool that owns it:

- **Implementation and review:** the batch's running phase, which the review pipeline writes to
  `.fanout/phase`. `fanout.py board` reads it with the unit's state, one read per host and
  no GitHub call (`scripts/dev/fanout_lib/batch_board.py:board`).
- **Waiting to land:** the PR's draft flag and CI, from the PR list `/api/prs` caches for 60
  seconds. The view adds no GitHub call.
- **Landing and deploying:** the landing's state record, split at the merge.
- **Ended:** the record's verdict, and what the change waits on from the `next` field of the
  newest `LAND-RESULT:` line this host can read: the batch's own landing log, then the logs of
  *Landing logs*
  (`hosts/components/deploy-ui/tests/test_deploy_ui_changes.py::test_an_ended_landing_names_the_next_action_its_land_result_gives_is_clean`).
  A merged change whose `next` is `done` or unreadable shows its verdict for
  `hosts/components/deploy-ui/files/deploy_ui_lib/deploy_ui_changes.py:FINISHED_S`, and any
  other verdict for the records' window.

The newest source decides a change's stage. A live landing beats a running batch, because a
review batch's unit stays active through its own landing. A running batch beats a verdict
older than its phase, because the pipeline resumes the implementer after some verdicts.

The daemon cannot import land_lib or fanout_lib, so the landing states and the `next` actions
are copies. A test holds each landing state in exactly one of the landing and deploying sets,
and the `next` labels to `scripts/deploy_tools/land_lib/outcome.py:Next`
(`hosts/components/deploy-ui/tests/test_deploy_ui_changes.py::test_every_stage_and_state_name_the_page_shows_is_its_owners_is_clean`).
The review phase is the literal the review pipeline writes, which the fan-out's own tests
assert
(`scripts/dev/tests/fanout_review/test_fanout_review_second_round.py::test_a_medium_leftover_gets_a_second_fix_and_delta_review_that_can_clear_it`).
The batch states are shown as `fanout.py board` names them, and the daemon branches only on its
`running` flag. The page renders the names the daemon sends and holds no list of its own.

`hosts/components/deploy-ui/files/deploy_ui_lib/deploy_ui_change_view.py:ChangeView` does the
reads. It runs `fanout.py board` in the primary checkout here, and over ssh on each host in
`DEPLOY_UI_FANOUT_REMOTES`, because an orchestrator keeps its run manifests in its own home. It
caches the batches for `board_cache_s`, so every open tab shares one read. A read that fails
is named in `batches_unavailable` and leaves the rows the other sources give, with their batch
column blank
(`hosts/components/deploy-ui/tests/test_deploy_ui_changes.py::test_a_failed_batch_read_is_named_and_leaves_the_pr_row_is_flagged`).
A host that does not answer for its batches is named the same way, and blanks the batch state
of its rows.
A batch that stopped more than a day ago with no open PR and no landing is not shown. A merge
drops a PR from the PR list, so the daemon remembers each branch's PR for its life.

## Writes

Land spawns `land.sh` detached and logs to `~/.local/state/deploy-ui/`
(`hosts/components/deploy-ui/tests/test_deploy_ui_app.py::test_land_spawns_land_sh_is_clean`).
Land and cancel each emit one logfmt line via `logger -t deploy-ui`
(`hosts/components/deploy-ui/files/deploy_ui_lib/deploy_ui_writes.py:audit`). The page reads
no hold itself: `land.sh` meets `DEPLOY_HOLD` on its own. A PR that bumps a forward-only pin
is refused before it merges
(`scripts/deploy_tools/tests/land_deploy/test_land_pin_gate.py::test_a_bump_is_refused_unless_deploy_hold_reads_none`).
Any other landing under the hold merges and ends `deploy-failed` with cause `promote-held`
(`scripts/deploy_tools/tests/promote/test_promote_land_wait.py::test_a_hold_set_before_the_merge_ends_the_wait_at_the_first_poll`).
The hold is cleared with `gh variable set DEPLOY_HOLD --body none`, never from this page.

Log names carry a `mkstemp` suffix as well as the second-granular timestamp
(`hosts/components/deploy-ui/tests/test_deploy_ui_writes.py::test_spawn_logged_never_takes_a_name_already_on_disk`). Two landings in
one second used to name the same file, and the second open truncated a log the first process
still held.

## Landing logs

The page shows any landing's log, not only one it started: a "log" button on each `land` row in
flight, and on each row of *Recently finished landings*. The viewer parses the log into its
`== arm` and `== <n>/<of>` stages and its `VERDICT:` line. `parse_landing` in
`files/deploy_ui_lib/deploy_ui_reads.py` does the parsing, so the page only renders it, and a
run of identical lines (a wait's poll line) folds into one with a count.

The viewer carries a tab for each landing in flight, and `[` and `]` cycle through them, so
switching logs never means scrolling back to the in-flight table. Clicking a step box that has
started opens its stage and scrolls the log to it, and stops follow mode so a live landing's
next poll does not scroll it away. *Recently finished landings*
shows the newest five, with a toggle to show all of them and one to hide the section. The page
keeps both choices in the browser's `localStorage`, per viewer, and defaults to five shown when
storage is blocked.

The page names a log by file name, never by path, and the daemon reads only a name in its own
index (`App._landing_index`). The index holds two kinds of file, written in the last 24 hours,
named like a landing log, and regular files rather than symlinks:

- every log in the daemon's own log dir and in `/tmp/homelab-landings-<user>`, where
  `land.sh --detach` writes when no session dir is set (`land_lib/detach.py`);
- the stdout of each `land` row the in-flight panel listed. A session's landing logs under its
  own `$CLAUDE_JOB_DIR/tmp` or `--log-dir`, which no fixed list of directories covers. A
  foreground landing's stdout is a terminal, which fails the regular-file check.

A landing run as the `claude` user logs under that user's directories, which the daemon
cannot read, so it does not appear. `VERDICT_HINTS` gives each verdict a tone and a next step,
and a test pins its keys to `scripts/deploy_tools/land_lib/outcome.py:VERDICTS`, because the
daemon cannot import land_lib
(`hosts/components/deploy-ui/tests/test_deploy_ui_landings.py::test_every_land_verdict_has_a_hint_is_clean`).

## Game servers

The *Game servers* panel shows each game in `game_servers` (`estate/vars.yml`), which
the component renders into `games.json` beside the daemon (`templates/games.json.j2`) and the unit
names in `DEPLOY_UI_GAMES_FILE`. A game is its server's Deployment plus every Deployment
that runs only while it does, such as its game-stats exporter. `files/deploy_ui_lib/deploy_ui_games.py`
folds them into one state: `running`, `starting`, `stopping`, `stopped`, `partial` (some scaled up,
some down) or `missing`.

- **The state comes from the read-only kubeconfig.** The unit sets `KUBECONFIG` to the operator's
  `~/.kube/config`, the `homelab-readonly` identity, and the panel runs two `kubectl get` calls
  per poll: the Deployments by name, and their pods by `app in (...)`.
- **The console comes from Loki, not from `pods/log`.** `curl` reads the newest 200 lines of
  `{namespace=<ns>,container=<server>}` from the last 7 days through the LAN route in
  `DEPLOY_UI_LOKI`, so a parked server still shows its last run. Loki splits one container
  into a stream per level, and `parse_console` merges them by timestamp.

### Starting and stopping a game

`POST /api/game {game, action}` with `action` one of `start`, `stop` or `restart` scales every
Deployment the game lists, together
(`hosts/components/deploy-ui/tests/test_deploy_ui_game_control.py::test_start_and_stop_scale_every_deployment_of_the_game_is_clean`).
`hosts/components/deploy-ui/files/deploy_ui_lib/deploy_ui_game_control.py:GameControl`
owns the games' reads and writes, and `App` routes to it as `App.games`. No template renders those counts (ADR-0033), so Flux leaves
them where the panel sets them.

- **Each write is a PATCH to the `scale` subresource, through `curl`.** The token is the
  `games_panel_token` credential, which the unit imports with `ImportCredential=` and
  `/run/credstore` supplies; it travels on curl's stdin as a header file, never in argv, which
  `ps` shows to every user. The API server's URL and CA come from the read-only kubeconfig. Each
  write carries `fieldManager=deploy-ui-games`
  (`hosts/components/deploy-ui/files/deploy_ui_lib/deploy_ui_game_control.py:GAMES_FIELD_MANAGER`),
  which Flux's takeover leaves alone.
- **A missing credential refuses writes, not the page.** `ImportCredential=` starts the unit
  without it, and each write answers 503 naming the operator's `host_secrets.py sync daniel-box`
  (`hosts/components/deploy-ui/tests/test_deploy_ui_game_control.py::test_a_write_without_the_token_is_unavailable_is_flagged`).
- **A restart is a stop, then a start.** The Deployments are `Recreate` over RWO claims, so
  `finish_restart` waits on its own thread for the game's pods to go, up to `restart_wait_s`,
  then scales back to 1
  (`hosts/components/deploy-ui/tests/test_deploy_ui_game_control.py::test_a_restart_stops_the_game_then_starts_it_again_is_clean`).
  A wait that runs out leaves the game stopped and says so in an audit
  line (`hosts/components/deploy-ui/tests/test_deploy_ui_game_control.py::test_a_restart_whose_pods_never_go_leaves_the_game_stopped_is_flagged`).
- **`start_refused` on a game's entry refuses start and restart**, with its text as the 409
  reason (`hosts/components/deploy-ui/tests/test_deploy_ui_game_control.py::test_a_refused_start_scales_nothing_is_flagged`);
  stop stays allowed (`hosts/components/deploy-ui/tests/test_deploy_ui_game_control.py::test_a_refused_game_can_still_be_stopped_is_clean`).
  terraria carries one until issue #822's restore checklist is done
  (`estate/vars.yml:game_servers`).
- Every action writes one audit line through `logger -t deploy-ui`:
  `action=game-<action> game=<name>`, and the restart's outcome as
  `action=game-restart game=<name> result=started|left-stopped`
  (`hosts/components/deploy-ui/tests/test_deploy_ui_game_control.py::test_a_restart_stops_the_game_then_starts_it_again_is_clean`).

## Usage count

The daemon writes one logfmt line per request to its unit journal (#2864), under the syslog
tag `deploy-ui` (`SyslogIdentifier=` in
`hosts/components/deploy-ui/templates/deploy-ui.service.j2`):
`request method=GET path=/ status=200 user=<Remote-User>`. The exception is a successful `GET`
on an `/api/` route, which writes no line, because the panels poll those
(`hosts/components/deploy-ui/files/deploy_ui.py:is_poll`,
`hosts/components/deploy-ui/tests/test_deploy_ui_app.py::test_a_panel_poll_writes_no_request_line_is_clean`).
An `/api/` read answered with anything but 200 still writes its line. A panel that cannot read
answers 200 with `unavailable`, so its poll writes none. `user` is the Authelia login
Traefik forwards, and `-` for a caller that reached the port around Authelia. The query
string is dropped, so a log path never reaches the journal
(`hosts/components/deploy-ui/tests/test_deploy_ui_app.py::test_request_line_drops_the_query_and_neuters_a_hostile_user_is_flagged`).

A client that closes the connection mid-response adds a line,
`client-disconnected method=GET path=/ status=200 user=<Remote-User>`, in place of a
`BrokenPipeError` traceback that HostLogErrors would count (#7921). It opens with a word
other than `request`, so the count below does not see the request twice
(`hosts/components/deploy-ui/tests/test_deploy_ui_app.py::test_client_closing_mid_response_logs_one_line_no_traceback_is_flagged`).

The page polls each panel every 10 seconds, an open game console polls too, and an open
landing viewer polls `/api/log` every 2 seconds (`hosts/components/deploy-ui/files/deploy_ui.html`).
Those polls show a tab was open, not that anyone used it, so they write no line. Count page
loads and writes:

```bash
journalctl -u deploy-ui --since -30d | grep -cE 'request method=GET path=/ |request method=POST'
```

journald on daniel-box is capped at 1G (`SystemMaxUse`,
`hosts/lib/tests/host_baselines/test_pi_sshd_apt_and_tuning.py::test_journald_and_limits_ship_as_drop_ins`), which held entries back to
2026-07-31 on 2026-09-30, so a 30-day window fits. `hosts/components/deploy-ui/files/deploy_ui.py:request_line`
formats the line with host_lib's `logfmt`, so a path holding a space or a quote is quoted
rather than split (#6202,
`hosts/components/deploy-ui/tests/test_deploy_ui_app.py::test_request_line_quotes_a_path_logfmt_would_split_is_flagged`). The component installs `host_lib.py` beside the daemon for it. The `user`
value is narrowed to `[\w.@-]` first, because `logfmt` quotes a newline without escaping it.
The line is unit-tested.

## Deploy

The host package applies it: a merge that changes this component reaches daniel-box when the
builder publishes the next `homelab-daniel-box`. dpkg only installs the files, so every Python
file entry in `component.yml` carries `activate: "try-restart:deploy-ui.service"`, and reconcile
restarts the daemon when one of them changed. A new Python file needs the same action, or the
daemon keeps running the old code (ENFORCED:
`hosts/lib/tests/host_baselines/test_deploy_ui_component.py::test_a_changed_python_file_restarts_the_daemon_is_clean`).
The HTML needs no action, because the daemon reads it on each request.
`host_lib.py` and `ci_verdict.py` are `lib:` entries from `hosts/lib/`. The open-PR table's CI
column is `hosts/lib/ci_verdict.py:checks_verdict` over each PR's status rollup, or `none` for
a PR with no check. To verify, run
`curl -s http://<daniel-box server_ip>:8790/api/inflight` from the box.

Tests: `uv run pytest hosts/components/deploy-ui/tests hosts/lib/tests/host_baselines/test_deploy_ui_component.py`.
