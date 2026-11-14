# GitHub API quota

GitHub meters API calls per identity, per hour. Every Claude session on a host spends the same
identity's quota, so one session's burst of calls refuses every other session, the lander and
the operator's own terminal. This page records what exhausts the quota, the design that
contains a burst inside the session that caused it, and the slices that build it.

**Status: proposed on 2026-11-14.** The operator has not accepted the design, and no slice is
built. On acceptance, *The design* becomes an ADR in the PR that builds slice 1.

| Slice | What it delivers | State |
|---|---|---|
| 1 | A ledger that `ghread` and `lib/gh.py` record each call in, and `gh-quota status` | Planned |
| 2 | The session ceiling and the reserve, enforced inside those two wrappers | Planned |
| 3 | The REST readers that do not run `gh` record and obey the same ledger | Planned |
| 4 | Identical reads from many sessions cost one GitHub call | Planned |
| Identity | Agent sessions stop spending the operator's quota | Owned by [slice 6 of the agent-user plan](claude-agent-user.md#status) |

## What exhausts the quota

Every figure is a measurement from 2026-11-14 on daniel-box, with 39 live sessions.

- **The exhausted quota is GraphQL, not REST.** At 15:00 UTC the operator account showed
  `graphql` at 5000 of 5000 points used and `core` at 3158 of 5000 requests used. `gh pr` and
  `gh issue` read through GraphQL. `gh api` and `hosts/lib/host_lib.py:github_get` read through
  REST.
- **Agent sessions spend the operator's quota.** A session that starts as `ubuntu` reads
  `~ubuntu/.config/gh/hosts.yml`, which holds the login of `DanielH2018`. Fan-out batches,
  background jobs and desktop-app sessions start as `ubuntu`. A session that starts as
  `claude` spends the quota of `claudemachineacct1`.
- **The lander spends the operator's quota too.** `claude-land@.service` runs as the
  operator's user and uses the operator's `gh` token, because it merges and deploys.
- **Every handler acts after GitHub refuses.** `scripts/lib/gh_rate_limit.py` computes the
  wait from a 403, and `land_lib/gh_read.py`, `land_lib/pace.py` and `await_ci.py` call it.
  GitHub sends the first 403 when the quota is already empty for every caller.
- **Nothing attributes a call to a session.** No log or ledger records which session made
  which GitHub call, so "which session spiked" has no answer.
- **The polls in flight were identical list reads.** At the time of the measurement the
  running callers were two `findings.py claims --json` (each a `gh issue list --label`) and
  two `gh pr list`.

Three figures are not measured: what one session spends in a normal hour, how many GraphQL
points each `gh` subcommand costs, and how much quota is spent outside the wrappers that
already exist. Slice 1 exists to measure all three, and slice 2 takes its numbers from them.

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

1. **One pool.** Every caller on a host spends one identity's quota.
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
| A `gh` call from a repo Python tool | `scripts/lib/gh.py` | `findings.py`, fan-out, `land_lib`, the probes. The `git-and-gh-go-through-lib` census row in `tests/repo/test_census_rows_python.py` refuses a bare `["gh", …]` elsewhere. |
| A REST read with no `gh` | `hosts/lib/host_lib.py:github_get` | `await_ci.py`, checkout-sync, the builder, `renovate-notify` |

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

A write stays bare on purpose. `ghread` refuses every write by construction, which is what
makes its blanket allow rule safe. A session writes a few times per pull request, and
GitHub's own limit of 80 content-creating requests per minute bounds a loop of them.

### A ledger the three owners call

```text
session shell read ── ghread ──────────────┐
repo Python tool ──── lib/gh.py ───────────┼── gh_quota: admit, record, sample
host REST reader ──── host_lib.github_get ─┘       │
                                                   └── ledger under /run/gh-quota/
cc-wait gh-pr, gh-ci ─ ghread, from slice 1 on

bare gh (a session's write, the operator's terminal)
        └── not recorded; GitHub still counts it, and it prints as `unattributed`
```

`gh_quota` is one Python module with three functions.

| Function | What it does |
|---|---|
| `admit(bucket, weight)` | Reads the ledger and returns pass, a hold in seconds, or a refusal. |
| `record(bucket, weight, argv_class)` | Writes one ledger row: session, bucket, weight, class, time. |
| `sample()` | At most once every 30 seconds, stores `GET /rate_limit` beside the rows as ground truth. |

**One home, three importers.** The module ships in a host package as
`/usr/lib/homelab/gh_quota.py`, because a host timer imports only root-owned code.
`scripts/lib/gh.py` imports the repo's copy of the same file, as `scripts/lib/worktrees.py`
imports `worktree_holders.py`. `ghread` imports the packaged path when it exists. On a host
without the package, such as the operator's PC, `ghread` runs as it does without this design.

**The ledger** is one SQLite file per quota identity under `/run/gh-quota/`. `/run` is a
tmpfs, and a quota window lasts one hour, so a reboot loses nothing worth keeping. The
identity is the calling uid when the token comes from that user's `gh` login. It is a
fingerprint of the token when the caller sets `GH_TOKEN`.

**The session key** is the first of these that exists:

1. `CLAUDE_CODE_SESSION_ID`, which Claude Code exports to every Bash call. `ghread` passes it
   through: its environment filter drops six prefixes, and `CLAUDE_` is not one. A subagent
   shares its parent's value, so its spend counts as the session's.
2. The systemd unit name from `/proc/self/cgroup`, for a timer or the lander.
3. `operator`, for a call with neither.

**Classification** is a table in the module: `gh` subcommand to bucket and weight.
`gh auth`, `gh config`, `gh version` and `gh api rate_limit` are free and write no row. A
weight is an estimate, because the module does not see the HTTP requests `gh` makes. The
ground-truth sample corrects for that: `gh-quota status` scales each session's recorded
weight so that the sessions sum to what GitHub counted.

**`unattributed` is what reached GitHub around the three owners.** It is what GitHub counted
beyond the recorded rows: a session's writes, the operator's terminal and the operator's PC.
`gh-quota status` prints it on its first line. A bare `gh` is never refused by the ledger.
That is the right answer for the operator's terminal. For a session it covers writes only,
once claude-guard routes every read.

**The mode** is one of `off`, `record` and `enforce`, read from `/etc/gh-quota.conf`, which
the host package renders from a host variable. In `off`, each function returns at once.

**The module fails open.** A ledger it cannot open, lock or parse makes `admit` pass and
counts the failure in a second file that `gh-quota status` reads. The ledger's own outage
must not become a GitHub outage.

### The two rules `admit` enforces

| Rule | Condition | Who it stops |
|---|---|---|
| Session ceiling | The session's scaled spend in the window has reached its ceiling for that bucket. | That session only |
| Reserve | GitHub's `remaining` for the bucket is at or below the reserve. | Every caller except class `priority` and `operator` |

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
as they do for GitHub's. `gh_read.py`, `pace.py` and `await_ci.py` then wait on it with no
change of their own.

### REST readers that do not run `gh`

`host_lib.github_get` is the one request shape every host REST reader shares. It calls
`admit` and `record` itself, so those readers obey the two rules and appear in the ledger.

`github_get` also sends `If-None-Match` with the `ETag` of its last answer for the same path,
kept in the ledger. `await_ci.py` polls the same run list every 20 seconds, and a poll that
returns `304` costs nothing.

### Identical reads cost one call

`gh_quota` keeps the stdout of a read a wrapper has just run, keyed by identity and exact
argv, for 15 seconds. A second caller with the same argv inside that time gets the kept
answer and makes no GitHub call. A caller that arrives while the first is still running waits
for it instead of starting its own.

- Only an allow-list of read subcommands is kept: `pr list`, `pr view`, `issue list`,
  `issue view`, `run list`, `run view` and a `gh api` GET. Only an exit status of 0 is kept.
- A write through `lib/gh.py` drops every kept answer for that repository.
- A write the ledger does not see is up to 15 seconds stale: one made elsewhere, or a bare
  `gh pr create` in a session's shell. The polling loops here wait 20 to 30 seconds between
  reads, so the staleness is inside one poll.

This turns the second root-cause property around for the reads that matter. 39 sessions that
run `findings.py claims` in the same quarter minute cost one call, not 39.

### `gh-quota`, the way to read and reverse it

```text
$ gh-quota status
graphql 3120/5000 used, resets 15:21 UTC; 37 sessions passing, 2 refused; 4% unattributed
  refused  7c1e90aa  ceiling  1004/1000  issue list x212, pr view x40
  refused  a93b0d51  reserve
--all lists every session; --bucket core for REST
```

| Command | What it does |
|---|---|
| `gh-quota status` | The count line, then only the sessions that are refused or near their ceiling. |
| `gh-quota status --all` | Every session, with its spend by argv class. |
| `gh-quota release <session>` | Lifts that session's ceiling until the window resets. Operator only. |
| `gh-quota mode` | Prints the mode. The mode changes through the host variable and the next package version. |

`release` is the way out of a refusal, and `status` is the way to see one. `release` writes
`/run/gh-quota/override`, which the operator's user owns. While a session still runs as
`ubuntu`, that session can write the file too. Slice 6 of the agent-user plan closes that.

`gh-quota` is a reporting tool. It runs no `gh` call for a caller, so it is not a second shim.

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
  public route and a state store. The 15-second read cache removes most duplicate polls for
  far less. Revisit it if the ledger shows polling is still most of the spend after slice 4.
- **No GitHub App for the reads the lander makes.** An installation token has its own quota,
  and minting one needs an RS256 signature the standard library cannot make. The identity
  split already keeps agent sessions out of the quota the lander spends.

## Alternatives not taken

| Alternative | One session cannot starve the others | Agents cannot starve the lander or the operator | Cuts total demand | Why not |
|---|---|---|---|---|
| Wait after a 403, as today | No | No | No | The first 403 arrives when the pool is empty. |
| Identity split alone | No | Yes | No | Kept, but one agent session still starves the rest. |
| A `gh` shim on `PATH` | Yes | Yes | Yes | A second shim beside `ghread`. See *What the design leaves out*. |
| A reader daemon that mirrors GitHub state | For reads | Partly | Yes | A daemon and a second copy of state; the read cache gets most of it with neither. |
| An intercepting proxy | Yes, exactly | Yes | Yes | See *What the design leaves out*. |

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

The module runs in `record` mode on both hosts. It refuses nothing.

```diff
 hosts/components/
+  gh-quota/
+    component.yml          ships the files below where has_claude_code is true
+    files/gh_quota.py      -> /usr/lib/homelab/gh_quota.py: ledger, classify, sample
+    files/gh_quota_cli.py  -> /usr/local/bin/gh-quota
+    templates/gh-quota.conf.j2
+    tests/test_gh_quota.py
+    CLAUDE.md
 scripts/lib/gh.py          gh(): record each call
 inventory/group_vars/all.yml
+  gh_quota_mode: record
 docs/github-quota.md

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
- **The tests** cover the session key order, the classification of each subcommand in the
  table, the scaling against a ground-truth sample, and the fail-open path with an unreadable
  ledger. A census test names the subcommands the repo's tools run, so a subcommand the table
  does not classify fails the suite.
- **It is done when** a week of ledger rows gives the three missing figures: the spend of a
  normal session-hour, the points per subcommand, and the `unattributed` share.

### Slice 2: enforce the two rules

```diff
 hosts/components/gh-quota/
   files/gh_quota.py        + admit(): the session ceiling and the reserve
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
- **The tests** pair each rule with one caller it must refuse and one it must pass, and hold
  `gh_rate_limit.wait_s` against the refusal line's `retry_s`.
- **It waits on a decision** when slice 1 measured `unattributed` above 20%: enforcing a
  ceiling that most of a session's spend goes around protects little.

### Slice 3: the REST readers

```diff
 hosts/lib/host_lib.py          github_get: admit, record, If-None-Match
 scripts/deploy_tools/await_ci.py   - its own rate-limit wrapper
```

- **To exercise it,** run `await_ci.py` against a finished SHA twice and read `core` in
  `gh-quota status --all`: the second run's polls record as `304` at no cost.

### Slice 4: identical reads cost one call

```diff
 hosts/components/gh-quota/
   files/gh_quota.py        + kept answers: allow-list, 15 s, dropped on a write
 scripts/lib/gh.py          + serve a kept answer; wait on a read in flight

 dotfiles repo, its own PR:
   home/dot_local/bin/executable_ghread   + the same two behaviours
```

- **To exercise it,** run the same `findings.py claims` from two sessions inside 15 seconds.
  `gh-quota status --all` shows one call and one kept answer.
- **The tests** cover the write that drops a kept answer, the non-zero exit that is not
  kept, and two concurrent callers that produce one upstream call.

## Open questions

1. **How much quota do a session's bare writes spend?** claude-guard routes the reads, so the
   writes are what stays outside the ledger. The design rests on that being small, and slice
   1 measures it as `unattributed`.
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
   app's pull-request pane is one candidate. `unattributed` measures it.
