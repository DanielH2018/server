# GitHub API quota

GitHub meters API calls per identity, per hour. Every Claude session on a host spends the same
identity's quota, so one session's burst of calls refuses every other session, the lander and
the operator's own terminal. This page records what exhausts the quota, the design that
contains a burst inside the session that caused it, and the slices that build it.

**Status: accepted on 2026-11-14.**
[ADR-0034](adr/0034-github-quota-is-budgeted-per-session-in-the-wrappers.md) records the
decision. The ledger records and refuses nothing.

| Slice | What it delivers | State |
|---|---|---|
| 1 | A ledger that `ghread` and `lib/gh.py` record each call in, and `gh-quota status` | Deployed on 2026-11-14: [server#8717](https://github.com/DanielH2018/server/pull/8717) for the module and `lib/gh.py`, and [dotfiles#1336](https://github.com/DanielH2018/dotfiles/pull/1336) for `ghread`, `cc-wait` and the claude-guard rule. Its week of measurement ends on 2026-11-21. |
| 2 | The session ceiling, the reserve and the write pace, enforced inside those two wrappers | Planned |
| 3 | The REST readers that do not run `gh` record and obey the same ledger | Recording built: `github_rest.github_get` records each authenticated read, and `forge.py`'s `GitHub` client each REST request. `admit` and `If-None-Match` are planned. |
| 4 | Identical reads from many sessions cost one GitHub call | Not planned: the operator ruled it out on 2026-11-26, from the measurement in [Issue and board state is read live](#issue-and-board-state-is-read-live) |
| Identity | Agent sessions stop spending the operator's quota | Owned by [slice 6 of the agent-user plan](claude-agent-user.md#status) |

## What exhausts the quota

Every figure is a measurement from 2026-11-14 on daniel-box, with 39 live sessions.

- **The exhausted quota is GraphQL, not REST.** At 15:00 UTC the operator account showed
  `graphql` at 5000 of 5000 points used and `core` at 3158 of 5000 requests used. `gh pr` and
  `gh issue` read through GraphQL. `gh api` and `hosts/lib/github_rest.py:github_get` read through
  REST.
- **Agent sessions spend the operator's quota.** A session that starts as `ubuntu` reads
  `~ubuntu/.config/gh/hosts.yml`, which holds the login of `DanielH2018`. Fan-out batches,
  background jobs and desktop-app sessions start as `ubuntu`. A session that starts as
  `claude` spends the quota of `claudemachineacct1`.
- **The lander spends the operator's quota too.** `claude-land@.service` runs as the
  operator's user and uses the operator's `gh` token, because it merges and deploys.
- **Every handler acts after GitHub refuses.** `scripts/lib/gh_rate_limit.py` computes the
  wait from a 403, and `land_lib/gh_read.py` and `await_ci.py` call it. A `gh` caller sleeps
  through `wait_out`, which re-reads the quota and ends the wait at its reset.
  GitHub sends the first 403 when the quota is already empty for every caller.
- **Nothing attributes a call to a session.** No log or ledger records which session made
  which GitHub call, so "which session spiked" has no answer.
- **The polls in flight were identical list reads.** At the time of the measurement the
  running callers were two `findings.py claims --json` (each a `gh issue list --label`) and
  two `gh pr list`.

Three figures are not measured: what one session spends in a normal hour, how many GraphQL
points each `gh` subcommand costs, and how much quota is spent outside the wrappers that
already exist. Slice 1 exists to measure all three, and slice 2 takes its numbers from them.

### What other sessions reported the same day

Two other sessions measured these on 2026-11-14. This page's session did not repeat them.

- **A burst of writes blocks every session through a second limit.** A backfill sent about
  130 project-board writes in a few seconds. GitHub allows 80 content-creating requests per
  minute, and it refused GraphQL for every session on the login for about 5 minutes. The
  session that ran the backfill then paced its own writes: batches of 10 with an 8-second
  pause.
- **The `findings.py claims` read is frequent, not expensive.** One call costs 1 to 2
  GraphQL points. Its caller was `probe.py landing --json`, which sessions ran repeatedly.
- **The hourly spend is bursty.** After the 15:21 UTC reset the login spent about 40 points
  per minute for 20 minutes. In the hour that ran dry it spent about 180 per minute. A
  15-minute watch after the reset caught no burst.
- **The landing slots read one bucket.** `land_lib/slots.py` lowers the count of landing
  slots across both hosts when the REST quota runs low. It does not read the GraphQL quota.

## GitHub's rules the design rests on

| Rule | Source |
|---|---|
| All of one user's tokens share one quota: personal access tokens, OAuth tokens and GitHub App user tokens. | [REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) |
| REST and GraphQL are separate quotas: 5,000 requests and 5,000 points per hour for a user. | [GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api) |
| A GraphQL query costs its connection count divided by 100, with a minimum of 1 point. A query can ask for its own `rateLimit { cost }`. | [GraphQL rate limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api) |
| `GET /rate_limit` does not count against the primary quota. | [REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) |
| A REST conditional request that returns `304 Not Modified` does not count, when it carries an `Authorization` header. The pages state no GraphQL equivalent. | [REST best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api) |
| Secondary limits also apply: 100 concurrent requests, 900 REST points per minute, 2,000 GraphQL points per minute, 80 content-creating requests per minute and 500 per hour. | [REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) |
| "You may not share API tokens to exceed GitHub's rate limitations." | [Terms of Service, section H](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#h-api-terms) |

The last rule closes one tempting design. An identity per session multiplies the quota, and
GitHub's terms forbid sharing tokens for that purpose. GitHub also hid the `DanielClaudeBot`
account on 2026-10-10 ([the agent-user plan](claude-agent-user.md)), so an account that reads
as automated abuse costs more than a rate limit.

## The root cause

Three properties hold together, and each one alone is harmless.

1. **One pool, metered twice.** Every caller on a host spends one identity's quota. GitHub
   meters it per hour, and it meters bursts per minute, so one session can empty the hour or
   trip the minute for all of them.
2. **Demand grows with the session count.** Each session polls GitHub on its own, so 39
   sessions ask the same question 39 times.
3. **No owner.** No module admits or refuses a call before it is made. Each caller handles
   the refusal on its own, after the pool is empty.

A rate limiter addresses the third property. The design below addresses all three, in the
order of what each costs to build.

## The design

### One quota identity per class of caller

| Class of caller | Identity | Its quota is safe from |
|---|---|---|
| The operator's terminal and the operator's PC | `DanielH2018` | every agent session |
| The lander, which merges and deploys as the operator | `DanielH2018` | every agent session |
| Agent sessions: interactive, fan-out, background | `claudemachineacct1` | nothing in its own class, which is what the ledger is for |

GitHub enforces this partition, so it needs no code here. It is complete when
[slice 6 of the agent-user plan](claude-agent-user.md#status) is: fan-out batches, background
jobs and desktop-app sessions start as `claude`. This page adds no work to that slice. It adds
one reason to finish it.

### The wrappers that already stand in front of GitHub

Three paths reach GitHub from a host, and each already has one owner.

| Path | Owner | Who uses it |
|---|---|---|
| A read from a session's shell | `ghread`, the one GitHub shim, shipped by the dotfiles repo | every session, with no permission prompt |
| A `gh` call from a repo Python tool | `scripts/lib/gh.py` | `findings.py`, fan-out, `land_lib`, the probes. The `git-and-gh-go-through-lib` census row in `tests/repo/census_rows/test_census_rows_python.py` refuses a bare `["gh", …]` elsewhere. |
| A REST read with no `gh` | `hosts/lib/github_rest.py:github_get` | `await_ci.py`, checkout-sync, the builder, `renovate-notify`. `forge.py`'s `GitHub` client sends through its own transport and records through `github_rest.count_github_call`. |

**The design adds no shim.** The operator ruled on 2026-11-14 that GitHub gets one shim, and
`ghread` is it. The budget is a library that those three owners call.

**claude-guard already sends a session's bare read to `ghread`.** A replay through
`claude-guard replay --deny` on 2026-11-14 gave these verdicts:

| A session types | Verdict | Where it goes |
|---|---|---|
| `gh pr list`, `view`, `checks`, `diff`; `gh issue list`, `view`; `gh run list`, `view`; `gh search` | Denied by the `ghread-route` rule | `ghread`, named in the denial |
| `gh run watch` | Blocked by a hook | `cc-wait gh-ci`, named in the denial |
| `gh api <path>`, a GET | Not denied; prompts | Stays bare |
| `gh pr create`, `edit`, `comment`, `ready` | Allowed | Stays bare, because `ghread` never writes |

Two bare paths remain that read, and slice 1 closes both in the dotfiles repo:

- **A bare `gh api` GET.** The `ghread-route` rule extends to it, because `ghread api` sends
  the same request.
- **`cc-wait`'s `gh-pr` and `gh-ci` sources.** `cc_wait/github.py` runs a bare `gh` every 30
  seconds for each distinct wait. It reads through `ghread` instead.

A session's write stays bare. `ghread` refuses every write by construction, which is what
makes its blanket allow rule safe, and a session writes a few times per pull request. The
ledger therefore cannot pace a bare write, and a session that loops on one can still trip
GitHub's burst limit. A repo tool's write goes through `lib/gh.py`, where the write pace
below applies.

### A ledger the three owners call

```text
session shell read ── ghread ──────────────┐
repo Python tool ──── lib/gh.py ───────────┼── gh_quota: record, and admit from slice 2
host REST reader ─ github_rest.github_get ─┘       │
                                                   └── ledger under /var/lib/gh-quota/
cc-wait gh-pr, gh-ci ─ ghread, from slice 1 on

bare gh (a session's write, the operator's terminal)
        └── not recorded; GitHub still counts it, and it prints as `beyond the ledger`
```

`gh_quota` is one Python module. A wrapper calls one function before it runs `gh`.

| Function | What it does |
|---|---|
| `record(args)` | Takes the arguments the wrapper is about to hand `gh`. It names the call's bucket and class itself, adds one to the session's count, and stores `GET /rate_limit` beside the counts when `SAMPLE_EVERY_S` has passed. |
| `admit(args)`, from slice 2 | Reads the ledger and returns pass, a hold in seconds, or a refusal. |

**One home, three importers.** The module ships in a host package as
`/usr/lib/homelab/gh_quota.py`, because a host timer imports only root-owned code.
`scripts/lib/gh.py` imports the repo's copy of the same file through a path insert. `ghread`
imports the packaged path when it exists. On a host without the package, such as the
operator's PC, `ghread` runs as it does without this design.

**The ledger** is one SQLite file per quota identity under `/var/lib/gh-quota/`. The
directory is on disk because the record is read days later: a spike is investigated after
the hour it happened in, and the ledger keeps each window for 8 days. Both hosts reboot
every Sunday, and a ledger on a tmpfs loses those days at each reboot. A write to the disk
costs a `gh` call about 4 ms, measured on daniel-box, against 0.3 ms on a tmpfs. The
identity is a fingerprint of the token `gh` uses for the call: `GH_TOKEN` or `GITHUB_TOKEN`
when the caller sets one, and otherwise what `gh auth token` prints in the caller's
environment. `gh` owns that choice, which also depends on its config directory and on the
login it has stored, so the module asks `gh` and does not repeat the rule. The question
costs about 30 ms per counted call and no GitHub call. The uid alone is not an identity:
on 2026-11-14 one uid's ledger held two live windows per bucket, which one token cannot have
([#8738](https://github.com/DanielH2018/server/issues/8738)), and a call was
counted in whichever window ended later. A caller whose `gh` has no token gets the uid as
its identity. Two tokens of one GitHub user spend one quota and get two ledger files.

**A counted call writes the ledger in one transaction.** `record` claims a due sample in one
short write, runs `gh api rate_limit` while holding no lock, and then writes the sample's
rows, the prune and the call's own row under one `BEGIN IMMEDIATE`. A writer waits up to
`LOCK_WAIT_S` (5 seconds) for another writer, and then the call goes ahead unrecorded with
a `database is locked` line in `failures.<uid>`. On 2026-11-18 a 1-second wait dropped 25
calls between 20:36 and 21:27 UTC, while about 15 sessions wrote during a burst and the host
was short of memory ([#9055](https://github.com/DanielH2018/server/issues/9055)). The wait delays a
counted `gh` call only while another writer holds the lock. The ledger stays in SQLite's
rollback journal, not WAL: WAL's `-wal` and `-shm` files take the writer's `umask`, and a
reader running as another uid could then not open the ledger.

**Each sample names its caller.** The ledger's `sampler` table, keyed by window and bucket
like `truth`, stores three facts about the last caller that sampled that window: its session
key, the resolved path of its `gh`, and the names of its `GH_*` and `GITHUB_*` variables.
It never stores their values. Each line of a `failures.<uid>` file carries the failing
caller's session key after its timestamp. On 2026-11-15 one token file on daniel-box held two
pools' windows at once, and nothing in the ledger said which caller sampled the second pool
([#8741](https://github.com/DanielH2018/server/issues/8741)).

The session key did not name it. Both of the second pool's samples that evening, at 21:06
and 21:34 UTC, read `operator`, `/usr/bin/gh` and no `GH_*` or `GITHUB_*` names. A sample of
the first pool at 21:09 also read `operator`. `operator` covers every process outside a
Claude session and outside a `.service` unit, such as an ssh login or a tmux scope.

**Each sample also names its process.** The `sampler_process` table, keyed the same way,
stores the sampling process's full `/proc/self/cgroup` line and the `comm` names of the
process and its ancestors, joined by `<`, such as `python3<bash<tmux: server`. It stores
`comm` only, never a command line, because an argv can carry a token. To name a window's
caller, read both tables beside `truth`. daniel-box has no `sqlite3` binary, so read them
with Python:

```bash
uv run python3 -c "import sqlite3; db = sqlite3.connect('file:/var/lib/gh-quota/tok-<fingerprint>@1000.sqlite?mode=ro', uri=True); [print(r) for r in db.execute('SELECT t.window, t.bucket, t.used, s.session, s.gh, s.env_names, p.cgroup, p.ancestry FROM truth t JOIN sampler s USING (window, bucket) LEFT JOIN sampler_process p USING (window, bucket) ORDER BY t.window')]"
```

**GitHub answers one token from two counters per bucket.** The `sampler` rows from
2026-11-15 to 2026-11-18 showed no second caller. The same sessions read both pools, with the
same `/usr/bin/gh` and no `GH_*` or `GITHUB_*` names. Session `185cff7e` read the counter that
resets at :29 at 22:29, then the counter that resets at :50 at 23:20. GitHub chooses which
counter answers, so neither the caller nor the token separates them
([#8741](https://github.com/DanielH2018/server/issues/8741)). `core` has two counters too:
windows ending at 22:17 and 22:34 UTC on 2026-11-18 were live at once, and so were windows
ending at 00:23 and 00:34 on 2026-11-19. GitHub's reported reset for one `core` window also moves by up to 45
seconds between samples.

The ledger handles both in two ways:

- A sample whose reset is within `RESET_DRIFT_S` (120 seconds) of a stored window updates that
  window. Any reset further away is a window of its own.
- `status` and a new call's window both follow the freshest sample, ordered by `sampled`, not
  the live window that ends last. A stale sample of the other counter can outlive a fresh one
  by 21 minutes.

Neither change tells a bucket's two counters apart. Slice 2's ceiling has to hold against the
larger of them, because GitHub can refuse a call on either.

**The session key** is the first of these that exists:

1. `CLAUDE_CODE_SESSION_ID`, which Claude Code exports to every Bash call. `ghread` passes it
   through: its environment filter drops six prefixes, and `CLAUDE_` is not one. A subagent
   shares its parent's value, so its spend counts as the session's.
2. The systemd unit name from `/proc/self/cgroup`, for a timer or the lander.
3. `operator`, for a call with neither.

**Classification** is `classify` in the module: a `gh` argv to a bucket and a call class,
such as `graphql` and `pr list`. `gh auth`, `gh config`, `gh version` and
`gh api rate_limit` are free and write no row. A `gh api` write records as `api mutation` or
`api write`, apart from a read.

**The ledger counts calls, not points.** The module does not see the HTTP requests `gh`
makes, and one GraphQL call can cost several points. `gh-quota status` therefore prints the
recorded calls beside what GitHub counted, and does not scale one to fit the other. The cost
of each call class is fitted later from the recorded windows.

**Spend beyond the ledger is what GitHub counted above the recorded calls.** For `core` it is
what reached GitHub around the three owners: a session's writes, the operator's terminal and
the operator's PC. For `graphql` it also holds the points a recorded call cost above one.
`gh-quota status` prints it on each count line. A bare `gh` is never refused by the ledger.
That is the right answer for the operator's terminal. For a session it covers writes only,
once claude-guard routes every read.

**Most core spend beyond the ledger is not the other host.** daniel-box and daniel-server
spend one quota through two tokens, so each host keeps its own ledger file and reports the
other host's calls as beyond the ledger. The two ledgers name different identities
(`tok-2edb9dd14a9a` on daniel-box, `tok-0b068a5c1034` on daniel-server), but their windows
share GitHub's reset epoch to the second. A measurement on 2026-11-21 joined both hosts'
`truth` and `spend` tables on that reset epoch. It kept the 70 closed core windows from
2026-11-14 to 2026-11-21 whose last sample came within 60 seconds of the reset:

- GitHub counted 121,458 core calls in those windows.
- daniel-box's ledger recorded 40,722 of them, so its `gh-quota status` reads 66% beyond.
- daniel-server's ledger recorded 9,254, so its `gh-quota status` reads 92% beyond.
- Both ledgers together leave 71,482 calls, or 59%, attributed to neither host.

The other host therefore explains about 7 points of daniel-box's share. On daniel-server it
explains 33 points, because daniel-box makes most of the spend. One busy hour shows the same
shape: the window that reset at 02:34 UTC on 2026-11-19 counted 4,447 calls, of which
daniel-box recorded 1,298 and daniel-server 43, which leaves 3,106 (70%) beyond both ledgers.
A single `gh-quota status` snapshot can read far lower: 31% on 2026-11-20 was one window
part-way through, and that window ended at 25% once both hosts were subtracted. The
measurement predates slice 3's recording, so the host REST readers are part of that 59%.

**Since slice 3's recording, both ledgers leave about a quarter.** The same join on 2026-11-23
kept the 12 closed core windows from 20:00 UTC on 2026-11-22 to 15:00 UTC on 2026-11-23.
GitHub counted 4,509 calls in them. The two ledgers recorded 3,346, which leaves 1,163, or 26%,
attributed to neither host. Each window left between 38 and 203 calls. The quietest windows
left 38, 38 and 46, so a floor of about 40 calls an hour is spent whatever the sessions do.
The rest rises with session activity, up to about 200 an hour.

**Since the exporter's conditional reads deployed, a quiet hour leaves nothing beyond the
ledgers.** The join on 2026-11-26 read every ledger file on both hosts whose identity has a
`truth` sample on the operator's counter: `tok-2edb9dd14a9a`, `tok-0b068a5c1034`, and
`tok-efaef766d6d0`, the token checkout-sync and the builder read with. It kept the 46 closed
core windows from 00:00 UTC on 2026-11-23 to 21:12 UTC on 2026-11-26. The six quietest, on
2026-11-24 and 2026-11-25, counted between 4 and 42 calls each. Each left between -1 and 4
calls beyond the ledgers. A ledger can record a call that GitHub did not count, such as a 304,
so a window can read slightly negative. In the window that reset at 20:05 UTC on 2026-11-25,
GitHub counted 4 calls, while the exporter's `up` series read 1 for the whole hour. Without
conditional reads the exporter alone spent about 24 an hour. The busy windows still leave
the residual that rises with session activity. Across all 46 windows, GitHub counted 19,990
calls, and 6,291 of them, or 31%, are attributed to no ledger. Thirteen identities with no
`truth` sample hold 2,601 more calls in one window on 2026-11-26, more than that window's
residual. They are excluded, because those calls never reached GitHub on the operator's
counter.

The callers that still spend the operator's core quota beyond both ledgers:

| Caller | Calls | Why no ledger records it |
|---|---|---|
| monitor-bridge's deploy-pipeline exporter | Only the reads whose answer changed. It sends two GETs per 300 s cycle, plus a compare when a green run postdates `prod`'s or `prod`'s run is off the page, each with its URL's last `ETag`, and GitHub does not count the 304 an unchanged read gets (`k8s/roles/monitor-bridge/files/bridge/net.py:_get_json_conditional`) | It runs in the cluster, where no host ledger file is reachable |
| `promote` | a few per master push, through `PROMOTE_TOKEN` | It runs on a GitHub Actions runner, which has no gh-quota module |
| airflow's `github_daily_rollup` | a few pages of 100 workflow runs, once a day at 02:00 UTC | It runs in the cluster |
| `github-ruleset-drift` and `github-interaction-limit` | about 10 a day | They call `curl` from bash, not `github_rest` |
| a session's bare `gh` write, and the operator's terminal and PC | unmeasured | `ghread` refuses writes, and the operator's calls go around every wrapper |

The exporter explained most of the floor while it sent unconditional reads, about 24 calls an
hour. The quiet windows of 2026-11-24 and 2026-11-25 show it spends next to nothing since.
The table's other callers spend too little to explain the busy-hour residual. By
elimination, that residual is a session's bare `gh` writes and the operator's own calls.
Nothing has counted either of them.

**`github_rest.github_get` records each authenticated read** as `api rest <path template>`, such as `api rest repos/{o}/{r}/actions/runs`, under the token the
read sends and against the caller's session key. A systemd timer such as the builder or
checkout-sync therefore shows under its own service name, and the builder's token gets a
ledger file of its own. `gh-quota status --all` also totals each `api rest` template across the sessions of a window, below the session lines. `gh-quota status` joins the identities whose windows in a bucket
share GitHub's limit and a reset within `RESET_DRIFT_S`, because every token one user owns
spends that user's one quota. It prints one count line per joined window, named by its
identities joined with `+`, and that line's spend beyond the ledger subtracts the recorded
calls of every identity in it. The builder token's reads therefore count as recorded on the
operator's line. The other host's calls still count as beyond the ledger, so a figure for
both hosts still needs the join on GitHub's reset epoch described above. A window joins only
while its sample reads the same counter as the others: one user's count only rises within a
window, so a later sample that counts less than an earlier one belongs to another user. Two
different users whose windows reset within `RESET_DRIFT_S` and whose samples happen to rise
with time are still joined, because no sample names a token's owner. An anonymous read spends the source IP's limit, so it is not
recorded. `github_rest` loads the module from `/usr/lib/homelab/gh_quota.py` by path,
because a component's copy sits beside its own script, where a bare import cannot find the
module. A host without the gh-quota component therefore records nothing.
`homelab-builder.service` and `homelab-renovate-notify.service` list `/var/lib/gh-quota` in
their `ReadWritePaths=`, because their sandboxes would otherwise refuse the write
(`hosts/lib/tests/host_lib/test_github_rest.py::test_every_sandboxed_unit_that_reads_through_github_rest_can_write_the_ledger`).

**The mode** is `off` or `record`, and slice 2 adds `enforce`. The component's
`gh_quota_mode` default renders it into `/etc/gh-quota.conf`, and a host's host_vars can
override it. In `off`, `record` returns at once. `GH_QUOTA_MODE=off` in one call's
environment is `off` for that call: the test suite sets it, so a test's fake `gh` call never
lands in the host's ledger.

**The module fails open.** A ledger it cannot open, lock or parse lets the call through and
counts the failure in a second file that `gh-quota status` reads. `status` counts the
failures of the same 8 days the ledger keeps, and a failure file at its size limit drops
the older lines. The ledger's own outage must not become a GitHub outage. For the same reason `scripts/lib/gh.py` imports the module
optionally: a copy of the tree without it runs `gh` uncounted. A fan-out batch from another
repo runs `findings.py` from a snapshot of this tree, and `SNAPSHOT_PATHS` carries the
module so those calls are counted.

### The three rules `admit` enforces

| Rule | Condition | Who it stops |
|---|---|---|
| Session ceiling | The session's spend in the window has reached its ceiling for that bucket. | That session only |
| Reserve | GitHub's `remaining` for the bucket is at or below the reserve. | Every caller except class `priority` and `operator` |
| Write pace | The identity's last write through a wrapper was less than one second ago. | That write, until its turn |

- The write pace is one queue for the identity, not one per session, because GitHub's burst
  limit is per identity. Each write takes the identity's next one-second turn and waits for
  it. One write a second is GitHub's own guidance for mutations, and it stays under the limit
  of 80 a minute. A backfill of 130 writes then takes 130 seconds and trips nothing.
- A caller is class `priority` when it exports `GH_QUOTA_CLASS=priority`. `land.sh` and the
  lander unit set it. The ledger records the class, so a session that sets it for itself
  shows in `gh-quota status`.
- The starting values are a ceiling of 20% of the bucket and a reserve of 20%. Both are
  placeholders until slice 1 has measured a week of normal spend.
- The design defends against a runaway loop, not against a session that evades the ledger on
  purpose.

**A wrapper holds briefly and otherwise refuses.** A hold shorter than 20 seconds happens
inside the wrapper, which sleeps and then runs the call. A longer one is a refusal. `ghread`
exits with status 75. `lib.gh.gh` returns the same failed process a refused `gh` returns.
Both carry one line on stderr:

```text
GH-QUOTA: verdict=refused scope=session bucket=graphql spent=1004 ceiling=1000 retry_s=1240
```

A long sleep inside a wrapper is the wrong shape for a session. The Bash tool stops a command
after 120 seconds by default, and a stopped command reports nothing. A refusal that names its
wait lets the session run `cc-wait` or do other work.

`scripts/lib/gh_rate_limit.py` owns what a rate-limit refusal means. It learns the
`GH-QUOTA:` line in slice 2, so `is_rate_limited` and `wait_s` answer for the ledger's refusal
as they do for GitHub's. `gh_read.py` and `await_ci.py` then wait on it with no
change of their own.

### REST readers that do not run `gh`

`github_rest.github_get` is the one request shape every host REST reader shares. It calls
`admit` and `record` itself, so those readers obey the same rules and appear in the ledger.

`github_get` also sends `If-None-Match` with the `ETag` of its last answer for the same path,
kept in the ledger. `await_ci.py` polls the same run list every 20 seconds, and a poll that
returns `304` costs nothing.

### Identical reads are not shared

**HISTORY —** The design kept each read's stdout for 15 seconds, keyed by identity and argv,
so that a second session asking the same question within that time made no GitHub call. The
operator ruled it out on 2026-11-26. Sessions rarely ask the same question within 15 seconds,
so it would save about 3 reads a day. [Issue and board state is read live](#issue-and-board-state-is-read-live)
holds the measurement. A `cc-wait` source that declares `remote` still shares one read per
interval among its waiters.

### `gh-quota`, the way to read and reverse it

```text
$ gh-quota status
tok-2edb9dd14a9a graphql: 3120/5000 used, resets 15:20 UTC; 2 sessions made 1012 calls; 2108 beyond the ledger (68%); 1 at or over 20%; --all lists 1 more
  7c1e90aa-0000-4000-8000-000000000001  1000 calls  issue list x960, pr view x40
tok-2edb9dd14a9a writes: 12/80 in the last minute, 140/500 in the last hour
```

Each open quota window gets one count line per bucket, naming every identity that shares it. Under it come only
the sessions at or above `ATTENTION_SHARE` of the bucket's limit. Each identity that wrote in
the last hour then gets one writes line, described below.

| Command | What it does |
|---|---|
| `gh-quota status` | A count line per shared window and bucket, then only the sessions over the share. |
| `gh-quota status --all` | Every session, with its calls by class. |
| `gh-quota status --json` | The same windows as one line of compact JSON. Each row also carries `writes_minute` and `writes_hour`, summed over its identities. |
| `gh-quota mode` | Prints the mode. The mode changes through `gh_quota_mode` and the next package version. |
| `gh-quota release <session>`, from slice 2 | Lifts that session's ceiling until the window resets. Operator only. |

`release` is the way out of a refusal, and `status` is the way to see one. `release` writes
`/var/lib/gh-quota/override`, which the operator's user owns. While a session still runs as
`ubuntu`, that session can write the file too. Slice 6 of the agent-user plan closes that.

`gh-quota` is a reporting tool. It runs no `gh` call for a caller, so it is not a second shim.

GitHub publishes no count for the secondary limits, so the primary count lines can read
healthy while GitHub refuses writes. On 2026-11-18 about ten landings and agents wrote at
once, and GitHub refused `gh pr ready` with `API rate limit already exceeded` while the
GraphQL quota had 4595 of 5000 calls left. The same landing succeeded five minutes later. A
secondary limit can therefore arrive in the primary quota's words.
`scripts/lib/gh_rate_limit.py:remedy` reads the named quota's headroom to tell the two apart. A refusal with calls left is the secondary limit, and the landing says
to wait and re-run instead of repeating the refused call.

The writes line is the ledger's own count against the content-creating limits. `record`
stamps each write with its time, and `status` counts an identity's writes in the last minute
and the last hour against 80 and 500 (`WRITES_PER_MINUTE`, `WRITES_PER_HOUR`). A write is a
`gh api` call that sends a body or a non-GET method, a GraphQL mutation, or a porcelain
subcommand in `_WRITE_SUBCOMMANDS`, such as `pr create` or `pr ready`
(`hosts/components/gh-quota/files/gh_quota.py:is_write`). The count can err in both
directions. A bare `gh` in a session's shell never reaches the ledger, so its writes are
missing. `record` runs before the call, so a write GitHub refused is still counted.

## What the design leaves out

- **No `gh` shim on `PATH`.** A program installed as `/usr/local/bin/gh` reaches every caller,
  a bare `gh` included, with no change to any of them. It is also a second GitHub shim beside
  `ghread`, and it shadows a binary by its position on `PATH`. The operator ruled it out on
  2026-11-14. claude-guard's `ghread-route` rule does the same job for a session's reads by
  denying the bare form and naming `ghread`.
- **No intercepting proxy.** A proxy counts every request exactly and keeps the token out of
  a session. It needs TLS interception: a local CA that `gh`, Python and Node each trust
  through a different setting. Its outage is the outage it exists to prevent.
- **No identity per session.** GitHub's terms forbid it, as above.
- **No webhook receiver.** GitHub recommends webhooks over polling, and a receiver needs a
  public route and a state store. GitHub sends Projects events only for an organization's
  projects, so a receiver could not see the board. The other reasons are in
  [Issue and board state is read live](#issue-and-board-state-is-read-live).
- **No GitHub App for the reads the lander makes.** An installation token has its own quota,
  and minting one needs an RS256 signature the standard library cannot make. The identity
  split already keeps agent sessions out of the quota the lander spends.

## Alternatives not taken

| Alternative | One session cannot starve the others | Agents cannot starve the lander or the operator | Cuts total demand | Why not |
|---|---|---|---|---|
| Wait after a 403, as today | No | No | No | The first 403 arrives when the pool is empty. |
| Identity split alone | No | Yes | No | Kept, but one agent session still starves the rest. |
| A `gh` shim on `PATH` | Yes | Yes | Yes | A second shim beside `ghread`. See *What the design leaves out*. |
| A reader daemon that mirrors GitHub state | For reads | Partly | Yes | A daemon and a second copy of state, for a saving measured at about 1% of the GraphQL quota ([Issue and board state is read live](#issue-and-board-state-is-read-live)). |
| An intercepting proxy | Yes, exactly | Yes | Yes | See *What the design leaves out*. |

## Issue and board state is read live

The repo does not keep a local copy of its issues, claims or Project board, and refreshes
none by diffs. The operator asked for the measurement and ruled on 2026-11-26. Every figure
below was measured on 2026-11-25 from both hosts' ledgers and 8 days of session transcripts.

**The exhaustion of 2026-11-18 and 2026-11-19 came from one poller.** The `operator` session
key made 35,697 calls on those two days. Its `variable get`, `issue list` and `api graphql`
counts were near equal, which matches `probe.py landing --json` run once a minute in every
session by the deck mod. The ledger keeps no argv, so the match is an inference. Commit
`780c95548` removed the mod. From 2026-11-21 to 2026-11-25 the highest GraphQL window in
either ledger was 1,657 of 5,000 points, and the highest core window was 3,125 of 5,000.

**What a local copy could save is small.** Each figure is an upper bound per day:

- **Quota:** about 1,280 GraphQL points of 120,000. Both hosts made about 320 open-list
  reads a day. Each costs 3 points for the list and 1 for the issue fields.
- **Unchanged reads:** about 80% of those, an estimate. 79% of 10-minute slots over 77 hours
  held no issue event and no comment.
- **Wall time:** about 3 minutes. Agents ran `findings.py next`, `list` or `claims` about 48
  times a day. Each spends 3.2 to 4.3 seconds in `gh`.
- **Tokens:** none from a cache, because the command prints the same answer. A diff-style
  output could omit about 5,500 of 2.4 million tokens of tool output.

**The state is smaller than a diff of it.** The register held 45 open issues, one page of
254 KB over REST. A `since=` read covering one day returned 261 KB, because it carries
closed issues and pull requests too.

**Few reads are shared.** Of 383 agent runs of `next`, `list` and `claims`, 27 came within 15
seconds of another session's run, and 71 within 90 seconds.

**A copy would also be wrong in ways GitHub does not signal.**

- A cross-reference does not move an issue's `updated_at`. In 16 of 119 issues whose last
  event was a cross-reference, it was newer than `updated_at`.
- A board edit does not move the issue's `updated_at` either. 39 of 271 project items were
  newer than their issue, by up to 1,139 seconds.
- A comment does move it, so a new claim does.
- A read-only probe cannot tell whether a label, milestone, issue type, issue field,
  sub-issue or blocked-by change moves it. None was the last event on any of 300 issues.
- A blocker that closes changes nothing on the issue it blocked, so a copy would keep
  hiding an issue that has just become free.
- GitHub documents its repository events API as 30 seconds to 6 hours behind. It does not
  redeliver a failed webhook, and it sends `projects_v2_item` events only for an
  organization's projects.

A claim decides who works an issue, and the earliest `Claim:` comment wins
([the claim record](issue-claiming-and-fanout.md#the-claim-record)). A cached claim is
therefore a race, so every option kept the claim reads live.

**Options weighed.** The estimated line counts are for each option's own code:

- A shared reader writing a snapshot and a change log: 500 to 800 lines, a unit and an alert.
  It would keep one copy per user and per host, and still need full reads to catch the holes
  above.
- A git-backed mirror: 600 lines or more, plus push races.
- A webhook store: 500 lines or more, a public route, and a full reconcile on top.
- The 15-second shared read of slice 4: about 80 lines in two repos, for about 3 reads a day.

**Revisit** when open `claude` issues pass 100, so that the list takes two pages. Also
revisit when a GraphQL window reaches 50% on two different days and no runaway session
explains it.

## Complexity

The design adds one module, one reporting CLI and their tests, in one host component. It adds
no executable to the path a `gh` call takes and no process start to any call.

It adds one coupling across repositories: `ghread`, in the dotfiles repo, imports a module a
homelab host package ships. The import is optional, so neither repo breaks without the other.

It removes nothing on the day it lands. It retires three things over time, and each slice
names the one it retires:

- The memory entry on agent polling and the corrections filed against it in
  [#8420](https://github.com/DanielH2018/server/issues/8420), in slice 1. The ledger answers
  the question that entry answers from one incident.
- The separate rate-limit handling in `await_ci.py`, in slice 3. `github_get` owns it.
- The reason for the anonymous-poll constants in `await_ci.py`, if the ledger shows no
  anonymous read in a month.

## The slices

Each slice is exercisable on its own, and each one changes this page's status table in the
same PR.

### Slice 1: record, and read the record

The module runs in `record` mode on both hosts. It refuses nothing. Both halves are deployed:
`gh-quota mode` prints `record` on daniel-box and daniel-server, and a session's `ghread`
reads appear in `gh-quota status --all` under its session id.

A repo tool's calls join the ledger as its worktree picks up master. A session runs the
`scripts/lib/gh.py` of its own branch, so a worktree cut before 2026-11-14 records nothing
through it until it is rebased. `ghread` is installed per user, so its reads are counted for
every session at once.

```diff
 hosts/components/
+  gh-quota/
+    component.yml          ships the files below on each host that lists it;
+                           its default is gh_quota_mode: record
+    files/gh_quota.py      -> /usr/lib/homelab/gh_quota.py, the module
+    files/gh_quota_cli.py  -> /usr/bin/gh-quota, the `status` and `mode` command
+    templates/gh-quota.conf.j2
+    tmpfiles.d/homelab-gh-quota.conf   creates /var/lib/gh-quota
+    tests/test_gh_quota.py           classifying, sampling and windows
+    tests/test_gh_quota_failures.py  a failed sample, a locked or corrupt file, two writers
+    tests/_gh_quota_fakes.py, tests/conftest.py   the fake gh, clock and ledger both share
 hosts/daniel-box/package.yml, hosts/daniel-server/package.yml   + gh-quota
 scripts/lib/gh.py          gh(): record each call
 scripts/dev/fanout_lib/launch.py   the fan-out snapshot carries the module
 tests/leakguard.py         GH_QUOTA_MODE=off for the whole suite
 docs/adr/0034-…            the decision

 dotfiles repo, its own PR:
   home/dot_local/bin/executable_ghread   record each call when the module is installed
   cc-wait: cc_wait/github.py             read through ghread, not a bare gh
   claude-guard: ghread-route             + a bare `gh api` GET, with its case table
```

- **To exercise it,** run `ghread pr list` and `findings.py claims` in a session, then
  `gh-quota status --all`. The session appears with both calls. Type `gh api
  repos/<owner>/<repo>/pulls` and read the denial that names `ghread api`.
- **The claude-guard change ships as a case table,** not as prose: each bare read it must
  refuse, and each write and each `gh api` call with a method or a field that it must pass.
- **The tests** cover the classification, the session key order, the count per session in
  both buckets, the sample interval, the identity of each token, the status lines and the two
  fail-open paths. One more drives `lib.gh.gh` and reads the count back from the ledger.
- **It is done when** a week of ledger rows gives the three missing figures: the spend of a
  normal session-hour, the points per call class, and the share of spend beyond the ledger.

### Slice 2: enforce the three rules

```diff
 hosts/components/gh-quota/
   files/gh_quota.py        + admit(): the session ceiling, the reserve and the write pace
   files/gh_quota_cli.py    + release
 scripts/lib/gh.py              + hold under 20 s, else fail with the GH-QUOTA line
 scripts/lib/gh_rate_limit.py   + reads the GH-QUOTA line
 scripts/deploy_tools/land.sh   + exports GH_QUOTA_CLASS=priority
 inventory/group_vars/all.yml   gh_quota_mode: enforce, ceiling and reserve from slice 1

 dotfiles repo, its own PR:
   home/dot_local/bin/executable_ghread   + hold under 20 s, else exit 75 and the line
```

- **To exercise it,** set a ceiling of 5 points for one session through the override file,
  run six `ghread pr list`, and read the refusal. Run `ghread pr list` in a second session
  and see it pass. Run `gh-quota release` and see the first session pass.
- **To exercise the write pace,** run ten `gh issue comment` calls through `lib/gh.py` from
  two sessions at once and read their start times: no two fall inside one second.
- **The tests** pair each rule with one caller it must refuse and one it must pass, and hold
  `gh_rate_limit.wait_s` against the refusal line's `retry_s`.
- **It waits on a decision** when slice 1 measured `core` spend beyond the ledger above 20%.
  A ceiling that most of a session's spend goes around protects little.

### Slice 3: the REST readers

```diff
 hosts/lib/github_rest.py       github_get: admit, record, If-None-Match
 scripts/deploy_tools/await_ci.py   - its own rate-limit wrapper
```

- **To exercise it,** run `await_ci.py` against a finished SHA twice and read `core` in
  `gh-quota status --all`: the second run's polls record as `304` at no cost.

### Slice 4: not planned

The operator ruled slice 4 out on 2026-11-26, and
[Issue and board state is read live](#issue-and-board-state-is-read-live) holds the
measurement behind the ruling.

## Open questions

1. **How much quota do a session's bare writes spend?** claude-guard routes the reads, so the
   writes are what stays outside the ledger. The design rests on that being small, and slice
   1 measures it as spend beyond the ledger.
2. **Does `CLAUDE_CODE_SESSION_ID` reach a fan-out batch's calls with a value of its own?** A
   batch is its own `claude` process, so it should. Slice 1's first day of rows confirms it
   or shows every batch under one key.
3. **Whose account owns `homelab_builder_github_token`?** The builder and checkout-sync read
   with it. If `DanielH2018` owns it, those timers spend the operator's quota, and the ledger
   keys them by token fingerprint apart from the operator's own calls.
4. **Is a fixed ceiling enough, or does the ceiling need to shrink as sessions multiply?**
   With 39 sessions and a 20% ceiling, five simultaneous runaways still empty the pool. The
   reserve protects the lander either way. Slice 1's data says whether a fair share per live
   session is worth its extra rule.
5. **Does anything outside the repo call GitHub with the session's token?** The desktop
   app's pull-request pane is one candidate. Spend beyond the ledger measures it.
6. **Which path did the 2026-11-14 backfill's writes take?** The write pace covers a write
   through `lib/gh.py`. If the backfill ran a bare `gh` in a shell loop, the pace would not
   have held it, and a session's bare write needs an owner too.
