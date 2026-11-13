# `hosts/components/deploy-ui` — the host half of deploy.local (daniel-box)

`deploy-ui.service`, `User={{ sys_user }}`, cwd the primary checkout. `homelab-daniel-box` ships
it. H7.50 moved it from the `deploy_ui` setup role, with its in-flight panel. It serves the page
and API `k8s/roles/deploy-ui/` routes to, and runs `land.sh` and
`gh` exactly as the operator would from a terminal. It starts no deploy, because Flux applies
every service. The page stays as the one browser view of landings in progress (operator
ruling, 2026-11-06).

## Trust

The daemon does no authentication. Two gates stand in front of it; removing either is a
visible decision:

- ufw admits `deploy_ui_port` from the pod CIDR and both node IPs only, because flannel
  masquerades pod→LAN traffic to the sending node. The `ufw:` rules in `component.yml` declare
  them. ENFORCED: `hosts/lib/tests/test_deploy_ui_component.py`.
- Authelia `two_factor` gates `deploy.local`: `auth_tier: two_factor` on the `deploy-ui`
  containers_list entry, rendered into the authelia role's access_control rules.
  ENFORCED: `tests/services/test_authelia_access_tiers.py`.

DECIDED: a pod inside the cluster reaches the daemon without Authelia. Accepted 2026-09-10 —
a hostile pod already has kubectl-adjacent reach. Nothing stops it at the network layer:
netpol-baseline is INGRESS ONLY (`k8s/roles/netpol-baseline/templates/networkpolicy.yaml.j2`),
so pod egress is unfenced on this cluster.
Every POST also needs `X-Deploy-UI: 1` so a cross-site form cannot ride the Authelia cookie.

## Turning it off

- To stop it on daniel-box for a while, run `sudo systemctl mask --now deploy-ui.service`.
  To remove it, drop `deploy-ui` from `components:` in `hosts/daniel-box/package.yml`; the next
  package version stops it, and reconcile deletes its three ufw rules over two runs, because
  the arm deletes at most two in one run.
- Remove the `deploy-ui` entry from `containers_list` in `cluster/services.yml` and
  deploy, which drops the route (`k8s/roles/deploy-ui/`) and `deploy.local`.

## In flight

The panel reads the git-tree lock only. The unit passes its path in as
`DEPLOY_UI_TREE_LOCK`, and the panel lists no other lock file. A missing file leaves
`locks_watched` empty, which the page renders as its own state rather than `free`.

Two sources, because neither alone says who holds what. One merged-stream `fuser` call over
the tree lock names the processes with the file OPEN. A process blocked on the lock has the
file open too, so fuser lists the waiter exactly like the holder (measured: a `flock`
holder and a `flock -w` waiter both appear). `/proc/locks` is the kernel's own table: a
granted flock per file, and a `->` line with a live pid for each process blocked on it.
The pid on a granted line can be dead when a `flock` child took the lock on an inherited
descriptor, so the holder comes from fuser and only the waiter from `/proc/locks`.

`ps` with `ppid` folds each process family to one row carrying `locks` (held) and
`waiting_on`. A landing (`land.py`, its `uv run` wrapper and its children) is one `land`
row. A holder that is not a landing, such as `homelab-checkout-sync` on the tree lock, is a `lock`
row rather than nothing. A `grep` for a run is not a run. fuser lists only processes whose `/proc/<pid>/fd` this
user can read; every landing here runs as `{{ sys_user }}`, the daemon's own user.

A landing row carries its PR's title. A landing merges its PR mid-run, which drops it from
`gh pr list`, so `_pr_title` reads the title with `gh pr view <n>` and caches it for the
daemon's life. A failed lookup leaves the title blank and is retried on the next poll; it
never makes the lock panel unavailable. The page links each PR number to its GitHub page.

Only a `land` row is cancellable.

The daemon runs outside the venv. The unit renders `DEPLOY_UI_TREE_LOCK` from
`server_git_tree_lock`.

## Writes

Land spawns `land.sh` detached and logs to `~/.local/state/deploy-ui/`. Land and cancel each
emit one logfmt line via `logger -t deploy-ui`. The page reads no deployer state: `land.sh`
refuses a landing under `DEPLOY_HOLD` itself, and the hold is cleared with `gh variable set
DEPLOY_HOLD --body none`, never from this page.

Log names carry a `mkstemp` suffix as well as the second-granular timestamp. Two landings in
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

## Usage count

The daemon writes one logfmt line per request to its unit journal (#2864):
`request method=GET path=/ status=200 user=<Remote-User>`. `user` is the Authelia login
Traefik forwards, and `-` for a caller that reached the port around Authelia. The query
string is dropped, so a log path never reaches the journal.

A client that closes the connection mid-response adds a second line,
`client-disconnected method=GET path=/ status=200 user=<Remote-User>`, in place of a
`BrokenPipeError` traceback that HostLogErrors would count (#7921). It opens with a word
other than `request`, so the count below does not see the request twice.

The page polls four panels every 10 seconds, so an open tab writes about 24 lines a minute,
and 18 while the finished list is hidden, because the page skips its fetch.
An open landing viewer polls `/api/log` every 2 seconds and adds 30 more. Those polls show a tab was open, not that anyone used it. Count page
loads and writes instead:

```bash
journalctl -u deploy-ui --since -30d | grep -cE 'request method=GET path=/ |request method=POST'
```

journald on daniel-box is capped at 1G (`SystemMaxUse`), which held entries back to
2026-07-31 on 2026-09-30, so a 30-day window fits. `request_line` in `files/deploy_ui.py`
formats the line with host_lib's `logfmt`, so a path holding a space or a quote is quoted
rather than split (#6202). The component installs `host_lib.py` beside the daemon for it. The `user`
value is narrowed to `[\w.@-]` first, because `logfmt` quotes a newline without escaping it.
The line is unit-tested.

## Deploy

The host package applies it: a merge that changes this component reaches daniel-box when the
builder publishes the next `homelab-daniel-box`. dpkg only installs the files, so every Python
file entry in `component.yml` carries `activate: "try-restart:deploy-ui.service"`, and reconcile
restarts the daemon when one of them changed. A new Python file needs the same action, or the
daemon keeps running the old code (ENFORCED:
`hosts/lib/tests/test_deploy_ui_component.py::test_a_changed_python_file_restarts_the_daemon_is_clean`).
The HTML needs no action, because the daemon reads it on each request. To verify, run
`curl -s http://<daniel-box server_ip>:8790/api/inflight` from the box.

Tests: `uv run pytest hosts/components/deploy-ui/tests hosts/lib/tests/test_deploy_ui_component.py`.
