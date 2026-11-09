# Host baseline record

The traps, derivations and measured incidents behind the `initial_setup` role — the host
baseline every machine in the homelab runs. `ansible/roles/setup/initial_setup/CLAUDE.md`
carries the rules and the tag inventory; this page is the working-out, read when you edit the
block it covers.

`tasks/` is the role's own inventory: one file per block tag, named for its subject. Read the
task file for what a block does. What follows is what a task file does not say on its own.

## What each block tag covers

The role's `CLAUDE.md` lists the tag names; these are the ones whose scope is not obvious from
the name:

None remains: H7.63 deleted the role's last tasks, the uv tools, the collections install,
the `ansible.log` cron arm and the setup-drift check.

The host packages own every other subject the role once tagged: the swap, apt and base packages,
the sudo cache, the SSH directory, auditd, the kernel modules, the banners, core dumps, the
networkd-dispatcher mask and the i915 option. H7.58 to H7.60 moved them into the `host-hardening`
component and step 4 of `hosts/bootstrap-host.sh`, and deleted the tasks.

## Tooling, pins and the root HOME trap

- **The packages and the dotfiles repo own the dev tooling (H7.61, H7.62).** `/usr/bin/uv` and
  `/usr/bin/vale` are one-line execs of the `homelab-tools` binaries, the `worktree-holders`
  component ships the root-run holder scan, and the dotfiles repo installs the per-user uv, prek and
  Python. H7.63 deleted the role's last two tools, the `ansible-core` and `ansible-lint` uv
  installs; the copies already on the hosts stay until H7.56. Each cluster host's package lists
  `/usr/local/bin/uv` as a legacy entry, so the install removes the copy that `initial_setup`
  wrote.
- **Vale's copies are in `homelab-tools` and CI** (`ansible/tests/repo/test_vale_matches_the_ci_pin.py`).
  The dotfiles repo's `tools.toml` pins uv, prek and Python, and this repo holds no twin of uv or
  prek.
- **`host_python_version` names an interpreter only the dotfiles repo installs.** Units run `uv` with
  `--no-python-downloads`, so a bump merged first leaves every unit naming the pin without a Python.
  To bump it, land the dotfiles' `python` pin, run `chezmoi apply` on daniel-box and daniel-server,
  read `~/.local/bin/uv python list --only-installed` on each, and then merge this repo's bump. No
  test can hold the two equal, because the dotfiles repo is not in this tree.
- **This play becomes root, so `~` is `/root` — SSH paths are absolute only** (#2413). The
  `role-tasks-write-no-home-relative-path` row of `ansible/tests/repo/test_census_rows_roles.py`
  ratchets the class. The home-dir resolver task
  exists for the same reason. The role doc's fact-dependency rule said a `register:` carries
  every consumer's tag. A check held it over every setup role until H7.44 emptied the last
  role with a `register:`.
- **`become` vs HOME:** `ansible_facts.env.HOME` is root's under the play's `become: true`. The
  `Resolve the deploy user's home directory` task registers the deploy user's home, and no task
  reads it since H7.62. H7.63 deletes it.

## SSH, UFW and sudo

- **The `Match sys_user` block sets `AllowTcpForwarding all`, not `local`** (#397), plus
  `AllowStreamLocalForwarding remote` and `StreamLocalBindUnlink yes`, for the clipboard
  bridge's reverse unix-socket forward: sshd builds the channel layer from
  `AllowTcpForwarding` alone, so `local` refuses every remote forward before
  `AllowStreamLocalForwarding` is read. The comment in
  `hosts/components/sshd/templates/90-homelab-match.conf.j2` carries the derivation. The refusal
  from this permission check logs `to remote forward to path ... but the request was denied`.
  The forwarding allowlist's refusal reads `Server has disabled port forwarding` instead.
- **On daniel-pi (H7.27), daniel-server (H7.28) and daniel-box (H7.29), sshd reads the package's two drop-ins instead of the tasks.**
  `10-homelab.conf` holds the 17 settings and `90-homelab-match.conf` holds the `Match` block.
  sshd keeps the first value it reads, and `10-` sorts before cloud-init's `50-cloud-init.conf`,
  where the Ansible tasks wrote the same lines (`ssh_config_path` on the Pi and on daniel-box). On daniel-server the tasks wrote `/etc/ssh/sshd_config`, whose
  Ubuntu `Include` line is read first, so the `10-` drop-in wins there too. Those lines stay
  there as harmless duplicates. The `Match` file sorts last so its block cannot reach another
  drop-in. `reload:ssh.service` runs `sshd -t` first, and
  `hosts/lib/tests/test_pi_initial_setup_part1.py` runs `sshd -T` over both drop-ins for the
  operator account and for another user. rkhunter still reads only the main `sshd_config`, so the Pi's
  `PermitRootLogin` read is unchanged by the move. H7's last host PR deleted the role's sshd tasks.
- **The UFW LAN allow must sort above the SSH limit rule**, which is why the `ssh-firewall`
  component declares `lan-ssh` with `position: first` and `before: ssh-limit`: UFW takes the
  first matching rule, so an allow appended below the limiter is inert (#508).
  `ansible/tests/setup/test_ssh_rate_limit_lan_exempt.py` pins the exemption and its ordering.
  The exemption exists because `limit` REJECTs a source at 6 connections in 30 seconds on a
  rolling window, counting SUCCESSFUL ones, so retries sustain the block — parallel Claude
  sessions over ssh crossed it twice. fail2ban's sshd jail is what answers a credential attack.
- **On daniel-pi (H7.30), the UFW arm of `homelab-reconcile` owns the firewall instead of the tasks.** The
  `ssh-firewall` component declares the two SSH rules as `keep`, `lan-ssh` before
  `ssh-limit`, and `hosts/daniel-pi/package.yml` declares the Pi's three LAN metrics rules.
  The arm adopts each Ansible-era rule in place by relabelling it `homelab:`, deletes the stale
  `allow 51820/udp`, and leaves the hand-added 51822 allow alone. It sets a default policy only
  where it differs and enables only an inactive UFW, as the tasks did.
  The role keeps no firewall task for any host.
- **On daniel-server (H7.31), the same arm owns the two SSH rules and declares nothing else.**
  The `k3s` role's agent join rules carry no `homelab:` label, so the arm leaves them until
  H7.34 declares them. It also leaves the hand-added 45876 allow from the pod network, which
  no task writes. The live defaults already match, so the arm runs no `ufw default`, which on
  a k3s node would reload the ruleset under kube-router (the 2026-10-25 outage).
- **On daniel-box (H7.32), the arm owns the two SSH rules beside the builder's two repository
  allows, and initial_setup keeps no UFW task for any host.** H7.35 declares the k3s join rules
  `keep` and adopts the coredns-host metrics allows. H7.50 declares the deploy-ui
  allows, and the arm relabels the role's commented rules in place. As on daniel-server, the live defaults match and the arm runs no
  `ufw default`.
- **UFW owns a SECOND sysctl file**, `/etc/ufw/sysctl.conf` (`IPT_SYSCTL` in
  `/etc/default/ufw`), re-applied on every `ufw enable` or `reload`. Ubuntu ships it with
  `log_martians=0`, so before #2977 this role's loop wrote 1 and `Enable UFW firewall` wrote 0
  back twenty tasks later.
- **The credential cache is what stops every `!` command re-prompting.**
  `/etc/sudoers.d/10-timestamp` sets `timestamp_type=global` with a 60-minute timeout, so one
  authentication covers every pane and every tty-less shell. Ansible writes it with
  `validate: visudo -cf %s` on the cluster nodes, and a test runs the same check on the
  `host-hardening` component's copy, because a malformed drop-in locks the escalation path out.

## Unattended upgrades: two traps

Use `Origins-Pattern`, not `Allowed-Origins` — the legacy form is rewritten to `o=X,a=Y` and
matches only a repo publishing a `Suite:` field, which the gh repo does not
(`ansible/tests/setup/test_unattended_origins_pattern.py`). And use the `::` append syntax: a
`{ ... }` block reads as a replacement and drops the `-security` and ESM pockets. Verify both
`apt-config dump` keys — `Allowed-Origins` must still list `-security`, `Origins-Pattern` the
extras from `unattended_upgrades_origins_patterns`, which is `[]` for security-only.

## Integrity scans, Postfix and the Pi's swap file

- **On every host since H7.32 (daniel-pi in H7.30, daniel-server in H7.31, daniel-box in
  H7.32), the package runs the scans and the maintenance jobs.** The
  `integrity-scans`, `fail2ban`, `postfix`, `accounting` and `host-maintenance` components
  render this role's templates in place (`role: initial_setup`), and each cron becomes a timer
  at the cron's slot. The weekly reboot refuses while the validation flag of `homelab-reconcile` exists. The
  push tokens and the Healthchecks.io key arrive as unit credentials, not as `/etc` env files.
  The first AIDE and rkhunter baselines and the mask on `dailyaidecheck.timer` stay host state.
  The role keeps no task of either subject for any host. The weekly
  firmware update ships only where `has_fwupd` is true, which is daniel-box alone.
- **AIDE and rkhunter are scheduled against each other, and the package's own timer is masked.**
  `dailyaidecheck.timer` duplicated the weekly cron nightly with broken mail alerting, at about
  1h20m of CPU per night on the Pi. Both weekly scans run `nice -n19 ionice -c3`, staggered to
  AIDE Monday 03:00 and rkhunter Wednesday 02:00, because they used to overlap at full priority
  for over an hour each on the Pi. The AIDE database init is the slow part of a first run on any
  host — about 8 minutes, with progress monitoring, so expect a long pause rather than a hang.
- **The AIDE and rkhunter scans report through the `host-checks` rules.** `aide-check.sh` keeps
  AIDE's exit code and writes one textfile verdict per host. It pushed one "AIDE Integrity" tile
  per host until M6 batch B9's Cutover-2. An error exit reads DOWN (#4594). A difference
  reads DOWN only when it is in a paged tree and no package's digest explains it, because
  package upgrades change files every week (#4599). The script advances the baseline after
  every UP run and keeps it on a DOWN until `aide-check --accept`. `docs/security-tools.md`
  has the table and the review steps. The script runs from the cron rather than
  from a `kuma-check-*` timer, because a timer reruns a red check, and each rerun here is
  another full-filesystem scan.
- **Both scans write a node_exporter textfile** (#6168). Each calls `write_check_textfile` from
  `homelab-check-lib.sh`, as check `aide-check` or `rkhunter-check`. Its `DEADLINE_S` is
  626400s for AIDE and 612000s for rkhunter. The `host-checks` rule group reads the files with a
  `Failed` and a `NotReporting` rule per scan per host. A cron moved without its `DEADLINE_S` and
  its rules fails `ansible/tests/setup/test_integrity_scan_rule_literals.py`.
- **`aide-check.sh` runs one scan at a time.** It takes `/run/lock/aide-check.lock` with
  `flock -n`, and a run that finds the lock held reads DOWN without starting a second scan.
  The deadline counts from the last verdict, so a scan that never finishes stays silent
  until then; the lock makes the next Monday's run report it instead. On 2026-10-19 daniel-box
  started a scan that was still running 35 hours later, killed by hand (#5785).
- **`rkhunter-check.sh` reads completion from `/var/log/rkhunter.log`, not from the exit code**
  (#4598). rkhunter exits 1 on a warning and on an error alike, so a scan completed only if this
  run rewrote the log and the log holds its summary. It wrote one "rkhunter Scan" tile per host
  until B9's Cutover-2, and now writes one textfile verdict: DOWN on a scan that did not complete, and DOWN on any warning, suspect file or possible
  rootkit (#4638). `/etc/rkhunter.conf.local` exempts the warnings every host raised every
  week, so a healthy week has none. Its `ALLOW_SSH_ROOT_USER` follows whether the main
  `sshd_config` sets `PermitRootLogin`, because rkhunter reads no drop-in.
- **Postfix binds `inet_interfaces = loopback-only`** and needs `notify: Restart Postfix`: a
  reload does not rebind the socket, so the `0.0.0.0:25` listener would survive it.
- **The Pi's swap file is bring-up, not tuning.** Step 4 of `hosts/bootstrap-host.sh` creates
  it, ahead of its first heavy apt, because heavy apt on a 512 MB Zero 2 W OOMs without disk
  swap. The Pi's package depends on `linux-raspi` and `wireguard`.
- **The AIDE exclusions deploy as `/etc/aide/aide.conf.d/98_aide_local`, with no dot.**
  Debian's `aide.conf` includes that directory with the name pattern `^[a-zA-Z0-9_-]+$`, so a
  dotted name is skipped silently and `aide --config-check` still passes. The file was
  `98_aide_local.conf` from 2026-05-31 to 2026-10-20, and AIDE read none of its rules: the
  weekly check hashed Longhorn replicas, kubelet volumes and every container root filesystem
  (#5785). `aide --path-check=f:<path>` shows whether a path is in or out.
- Two templates carry host-specific config: `templates/98_aide_local.conf.j2`, the AIDE
  exclusions for high-churn paths (the Pi's Docker tree, app data, pseudo filesystems, on
  the cluster nodes the Longhorn, containerd, etcd, pod-volume, CSI-mount and pod-log paths,
  every running container's root filesystem mounted under `/run/k3s/`, daniel-box's
  `/srv/media` library and the swap files, and on every host the Claude Code worktrees and the
  per-user caches), and `templates/fail2ban_homelab.conf.j2`, the fail2ban
  jail. Pi-only tasks are individually `when:`-guarded rather than block-scoped, so a server run
  simply skips them.

## Setup-plane drift reader (retired in H7.63)

`setup-drift-check.sh` ran daily on daniel-box and daniel-server from
`kuma-check-setup-drift.timer`. It compared each `copy:`-deployed setup file with its repo
source, each rendered setup template's source checksum with the one stamped at render time, and
the checkout's age with a 14-day limit. H7.52 retired the deployer's timer, so nothing renders
setup templates any more, and H7.63 deleted the check, its library, its stamp fragments and its
`SetupDrift` rules. Each host package's `legacy:` list removes the timer, the script and its
files.

Two lessons from it still apply to any host check:

- **Verify a cron arm as the user the cron runs as.** The age arm read the tree as root against
  a checkout owned by `sys_user`, which is git's dubious-ownership case, so a bare `git log`
  failed on every run until the arm passed `-c safe.directory`. The hand-run that verified it
  ran as `ubuntu` and passed. `GIT_TEST_ASSUME_DIFFERENT_OWNER=1` reproduces the refusal in a
  test.
- **A stale checkout makes a render-versus-template comparison agree.** daniel-server was
  measured 39 commits behind origin on 2026-08-17, so the render arm read green exactly when the
  host was furthest behind, which is why the age was its own DOWN.

## Loki read-route witness (`loki_route_witness` tag)

`/usr/lib/homelab/loki-read-route-health` runs hourly on both prod cluster nodes and writes that
host's own `loki-read-route-health.prom` verdict, which `LokiReadRouteFailed` and
`LokiReadRouteNotReporting` read per host. Each node's package lists the `loki-route-witness`
component, and `hosts/components/loki-route-witness/component.yml` carries the contract.
`daniel-pi` is not in the route's ClientIP set, so it never lists the component.

**It is a systemd timer, `homelab-loki-route-witness.timer`.** Its service is
`Restart=on-failure` with `RestartSec=15min`: the script exits 1 after a `down` verdict, so a red
check reruns every 15 minutes until the route answers instead of sitting red until the next hourly
slot. The timer is `Persistent=true`, so a slot missed inside an outage runs at boot; the script's
boot-grace arm exits 1 without a verdict, and the restart carries the real verdict.
`systemctl status homelab-loki-route-witness` shows `auto-restart` while it is red, and
`journalctl -u homelab-loki-route-witness` holds the runs.

**The witness needs the textfile directory group-writable.** It runs as `sys_user`, and the
kubelet creates the directory root 0755 for node-exporter's hostPath. The component's tmpfiles.d
line holds it at root, group `sys_user`, mode 2775.

**Why it is a host check and not a monitor-bridge check.** `loki-homelab`'s read route is guarded
by a ClientIP set of node-owned addresses, and its only caller is `probe.py loki-query` or
`loki-labels` running as a host process. A pod prober arrives with a pod IP, so it either fails
for a reason the operator never hits or has to be granted an address no real caller uses.

**Why both nodes.** Which address a host process arrives as depends on where the traefik pod
sits — the same node gives that node's cni0 gateway, another node gives the sender's flannel.1
address. #1693 left the route dead from daniel-server and healthy from daniel-box, so a single
witness would have reported green throughout. Each host writes its own verdict, which
Prometheus labels with the host's origin, so one host's `up` cannot satisfy the other's
deadline.

**The verdict is the response body, never the exit code.** `probe.py` builds its curl argv
without `-f`, so a route Traefik refuses returns `404 page not found` with exit **0**.
`hosts/components/loki-route-witness/loki_route_health.py` decides — valid JSON, `status: success`,
non-empty `data` — and `hosts/lib/tests/test_loki_route_health.py` holds that 404 as a DOWN case.
`ansible/tests/setup/test_loki_route_witness.py` guards the host list, the deadline against
the timer period, the exit-code contract (a down verdict exits 1), and that the script does not
push to Kuma, which M6 batch B2's Cutover-2 removed. B2's token PR retired both tokens a week later.

## The generated-docs refresh cron, in detail

- **Why it weights test modules at all (#2274).** CI's `pytest_shard.py --check-durations` step
  rejects the PR that introduces an unweighted module costing `RUNNER_HEAVY_FAIL_SECONDS` or
  more and leaves the repair to a human. One costing `RUNNER_HEAVY_SECONDS` up to that bound
  only annotates the run (#4010). It says nothing about a lighter one, and an unweighted file is
  packed at the suite median of 0.0s — so without the cron the split is un-skewed rather than
  measured. `--record-missing`, never `--record`: it measures only the files the table lacks,
  and writes the same bytes back when there are none, so an ordinary run leaves no diff.
- **Why it also refreshes present weights (#4888).** `--record-missing` never touches an entry
  the table holds, and the gate's under-recorded arm fires only at `UNDER_WEIGHT_RATIO` (20x).
  Three modules measured 1.66x to 1.98x their weights in a serial run on 2026-10-13 and nothing
  noticed. A lower CI ratio cannot catch that, because the runner measures correctly recorded
  entries 1.1x to 13x their weight. So the cron runs `--refresh-drifted 8`, which re-measures
  one hash slice of the table serially on this host, and every entry once in four days. It
  rewrites only an entry more than `DRIFT_RATIO` (1.5x) from what it measured, so noise inside
  that band leaves no diff.
- **Why a refresh can keep a drifted weight (#7132).** Two agreeing runs defend against one-off
  load, not against a cost that is real on this host alone. The cron wrote 85.09s for a module
  CI measured at 2.08s, and CI's gate failed the cron's PR. `--refresh-drifted` compares the
  reading with `pytest_shard_runner_weights.json`. At `STALE_WEIGHT_FLOOR_SECONDS` and
  `STALE_WEIGHT_FAIL_RATIO` or more of CI's seconds, it keeps the recorded weight and names the
  module in the journal under the tag `docs-refresh`.
- **Why a new row can hold CI's seconds (#7136).** `--record-missing` applies the same comparison
  to a new row, which has no recorded weight to keep. It writes the module's seconds from
  `pytest_shard_runner_weights.json` and names the module in the journal. Writing no row would
  leave the module unweighted, so every tick would measure it twice for as long as the local cost
  lasts. The row stays stable: `--refresh-drifted` refuses to raise it, and `assign` packs the
  module on the runner table regardless.
- **A red suite is not a case degradation saves**: the script runs the suite before its commit
  and exits 1 on a failure, because no prek hook runs the suite: #2827 moved the `pytest` hook
  to pre-push, and it was removed on 2026-10-02. What degradation saves is the suite being fine and the measurement alone
  failing — the `timeout` firing, or a durations report that parses to nothing. The `timeout` is
  there because this script holds the git-tree lock for its whole life.
- **A generator that writes outside the three staged paths is a defect**: the write is unstaged,
  and an unstaged file in the primary checkout parks every deploy on the box.
- **Its abort valves** are the shared `/var/lock/server-git-tree.lock`, the dirty-tree gate at
  the top (build the site, change no file), the unlanded-branch guard (`publish_pr.py unlanded`,
  read against origin rather than the open-PR list), and the commit-failure stamp under
  `/var/lib/homelab/docs-refresh.d`, which is what lets the next run tell its own leftover dirt
  from a human's.
- **Each failing run keeps its own log** in that directory, named `failure-<UTC start>.log`,
  and the script prunes all but the newest five (#4764). One overwritten file lost the
  2026-10-12 06:17 traceback to the 18:17 failure of a different test.
- **A red suite reports pytest's own verdict**: the count line and each `FAILED`/`ERROR` node id
  from the `short test summary info` block. The output's tail is only the fallback, for a run
  with no such block. On 2026-10-12 the tail was a shutdown traceback that named no test.
- **Before adding a fourth staged path**, or a prek hook matching one of the three, check the
  hook cannot fail on generator output — a hook that can wedges this cron on every run, and
  `end-of-file-fixer` and Vale have both already tried. Vale is the one kept exception, on
  purpose (#3008): it gates `docs/assets/generated/fragments/` so a generator's prose is linted
  in CI rather than only by a hand `vale docs` run, and a Vale error there aborts the cron's
  commit and reports DOWN until the generator is fixed.
- **`PUSH_STATUS` defaults to `down`** (`docs-refresh.sh.j2`), so a code path that forgets to
  set it reports a failure rather than a false UP.

## The eval sweep, in detail

- **Its mode is binary, with no dial.** Either `anthropic_api_key` exists and the sweep runs
  hermetic, or it is empty and the whole run is a no-op that reports UP with the reason logged
  (`eval-run.sh.j2`'s header). There is no non-hermetic fallback, because a subscription run's
  numbers are noise `evals/trend.py` refuses to trend anyway.
- **The key arrives as a systemd credential, not in the script.** The `eval-sweep` component
  of `homelab-daniel-box` lists `anthropic_api_key` under `credentials:`, and
  `homelab-eval-sweep.service` loads it with `LoadCredential=`. `eval-run` reads it from
  `$CREDENTIALS_DIRECTORY`, so reading the script never prints the key (#4607).
- **The authoritative source is the chezmoi eval engine's own grading**
  (`~/.local/share/chezmoi/evals/run-evals.mjs`) — never a cached or hand-edited report.
- **Its abort valves** are the shared git-tree lock (a dirty tree or another in-flight commit
  means the sweep's result is thrown away rather than recorded against a tree it did not grade)
  and the unlanded-branch guard: an `evals-history/*` branch still on origin skips the run
  rather than stacking a second one, and it reads origin rather than the open-PR list because a
  failed `gh pr create` leaves a branch with no PR.
- **Each sweep's per-run output is kept** under `/var/lib/homelab/eval-run.d/sweeps/<UTC
  stamp>/`: every agent's `--json` report (agent text, assertion failures, judge reason) and the
  last MiB of the console log. The newest 8 sweeps stay. Read these before editing a case that
  fell below its threshold; `history.json` holds only the scores and each sweep's
  total cost (#4019, #4273).
- **A failed suite or commit keeps its own log** at `/var/lib/homelab/eval-run.d/failure-<UTC
  start>.log`, and the script prunes all but the newest five. A red suite reports pytest's count
  line and failing node ids. The code is docs-refresh's, and a test holds the two copies
  identical (#4764).
- **A REGRESSED case still gets committed** — the data is real — but reports the run DOWN with
  the regression named, matching autofix-bridge's "act, but do not launder the result" pattern
  rather than silently dropping the regression or silently blocking the commit.
  `evals/trend.py:classify` decides what counts: a miss after a stable run, or a second miss in
  a row. A single miss after a mixed window is FLAKY, lands in the PR body, and keeps the verdict UP,
  because at k=3 one unlucky trial misses a 2/3 threshold (#5796).
- **Before widening scope** (grading `run-live.mjs`, changing cadence, adding a case directory
  the loop should skip), read the last few PRs this cron opened (`evals-history/*`,
  squash-merged) for what actually regressed or flaked.

## The journald cap, the rsyslog filter, and what a forensic read loses

The `journald` tag's two files are one change. `50-homelab.conf` caps the journal
(`SystemMaxUse=1G`, `MaxLevelStore=notice`) and sets `MaxLevelSyslog=info`;
`/etc/rsyslog.d/49-homelab-info-filter.conf` then discards info for every facility that reaches
rsyslog *through* journald. Deploying either alone is a defect.

**Why the two levels differ.** journald owns `/dev/log`, so rsyslog only ever sees what journald
forwards, and `Store` and `Syslog` are applied to their sinks independently. `MaxLevelSyslog`
read `notice` from 2026-08-01 to 2026-08-29 and deleted the host's SSH authentication trail:
`Accepted publickey ... SHA256:` and every `pam_unix(...:session)` line is priority info. sshd's
`LogLevel VERBOSE` was correct throughout and irrelevant — an application's log level bounds
what it emits, not what survives. Two things depended on those lines: which key authenticated is
recorded nowhere else, since auditd has the login but never the fingerprint, and the `crowdsec`
node agent tails `auth.log` for the SSH brute-force signal.

**The filter's exemptions are load-bearing.** `auth` and `authpriv` carry the records; `kern`
and `mail` must be exempt because they do *not* arrive via journald — rsyslog reads kernel
messages itself through `imklog`, and postfix writes to its own listen socket — so discarding
their info would remove lines from `kern.log` and `mail.log` that are present today. Measured
cost of the pair: +0.34 MB/day/host, against a +7 MB/day/host floor without the filter.
The `rsyslog-filter` component ships the file on every host whose `has_rsyslog` is true, and
reconcile runs `rsyslogd -N1` before the try-restart, so a fragment that does not parse holds the
host. AppArmor confines `rsyslogd` to `/etc/rsyslog.conf` and `/etc/rsyslog.d/**`, so a parse
check of a candidate in a temporary directory fails on every run.

**What the cap costs a forensic read, and how to read a boot in spite of it.** Priority-info
lines are gone from the journal, and from `/var/log/syslog` for every facility except `kern`,
`auth`, `authpriv` and `mail`. That set includes k3s's own output, every `Starting` and `Started`
unit line, and timesyncd's `Initial clock synchronization` step. An empty `journalctl -u k3s`
window therefore means *nothing at notice or above*, not *nothing happened*; the systemd
`Failed with result` lines are the only trace of a crash loop in the journal. The cause is in
`/var/log/k3s.log`, which `docs/k3s-node-plane-crons-and-incidents.md` (*k3s's own output is in
`/var/log/k3s.log`, not the journal*) explains. Kernel info lines DO survive, in `/var/log/syslog` only: `NIC Link is Up`, veth and cni0
bridge events, `PM: suspend entry`. A read that finds nothing in the journal is not finished
until it has grepped syslog.

**The second trap is the clock.** daniel-box's RTC is dead (`PM: RTC time: 00:03:09, date:
2024-01-01` at every boot), so a boot with no network runs on the clock systemd restores from
`/var/lib/systemd/timesync/clock` — the *previous* shutdown's time — until the first NTP step,
and that step is itself an info line nobody stores. Detect it from the journal's
`__MONOTONIC_TIMESTAMP` against `__REALTIME_TIMESTAMP`: a jump in their difference is a step, and
every wall stamp before it is early by that amount. Issue #1804 is the worked case — the box ran
5h08m with the wall clock 65 min slow, so the same event read as "boot 13:57" in the journal and
"boot 15:02" from monotonic, and the first `Link is Up` of the boot read as a mid-life iSCSI
reset.

## The iGPU gate

The `host-hardening` component ships `/etc/modprobe.d/i915.conf` (`options i915 enable_guc=2`)
under `has_igpu`, and the rebuild procedure in `docs/host-packages.md` runs `update-initramfs -u`
and the reboot by hand. Nothing reboots the host for it: the option tunes the machine running the
whole homelab.

`has_igpu` means the host passes `/dev/dri` through to jellyfin and tdarr, which is
vendor-neutral, since AMD does VAAPI through the same node, while `enable_guc` is Intel-only. The
role's task also checked that `/sys/module/i915` exists, and the package has no such check. A
future AMD or NVIDIA host with `has_igpu: true` needs its own flag before it lists this file.

## See also

- `ansible/roles/setup/initial_setup/CLAUDE.md` — the rules, the granular tag inventory and the
  two autonomous-role contracts.
- [Pi host tuning record](pi-host-tuning-record.md) — the Pi's own tuning, including the
  `has_rsyslog: false` mask that keeps the `rsyslog-filter` component off the Pi.
