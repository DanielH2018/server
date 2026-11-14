# GitHub API quota

GitHub meters API calls per identity, per hour. Every Claude session on a host spends the same
identity's quota, so one session's burst of calls refuses every other session, the lander and
the operator's own terminal. This page records what exhausts the quota, the design that
contains a burst inside the session that caused it, and the slices that build it.

**Status: proposed on 2026-11-14.** The operator has not accepted the design, and no slice is
built. On acceptance, *The design* becomes an ADR in the PR that builds slice 1.

| Slice | What it delivers | State |
|---|---|---|
| 1 | A `gh` shim that records each call per session, and `gh-quota status` | Planned |
| 2 | The session ceiling and the reserve, enforced by the shim | Planned |
| 3 | The REST readers that bypass `gh` record and obey the same ledger | Planned |
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

Two figures are not measured: what one session spends in a normal hour, and how many GraphQL
points each `gh` subcommand costs. Slice 1 exists to measure both, and slice 2 takes its
numbers from them.

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
| Agent sessions: interactive, fan-out, background | `claudemachineacct1` | nothing in its own class, which is what the shim is for |

GitHub enforces this partition, so it needs no code here. It is complete when
[slice 6 of the agent-user plan](claude-agent-user.md#status) is: fan-out batches, background
jobs and desktop-app sessions start as `claude`. This page adds no work to that slice. It adds
one reason to finish it.

### A `gh` shim that owns the budget

One module owns the quota: a shim installed as `/usr/local/bin/gh`. Its interface is `gh`
itself, so no caller changes.

```text
caller ── gh …            /usr/local/bin/gh, the shim
            │
            ├─ classify     bucket (graphql | core | free) and weight, from the argv
            ├─ admit        read the ledger; pass, hold a few seconds, or refuse
            ├─ record       one ledger row: session, bucket, weight, argv class, time
            └─ exec         /usr/bin/gh with the caller's argv, environment and stdio

at most once every 30 s, on the way through:
            └─ sample       GET /rate_limit, stored beside the rows as ground truth
```

**Why a shim reaches every caller.** Each `PATH` in use lists `/usr/local/bin` before
`/usr/bin`: a session's shell, systemd's default for a unit, and the three directories
`ghread` searches. `gh` itself comes from the `apt-github-cli` component as `/usr/bin/gh`.

**The ledger** is one SQLite file per quota identity under `/run/gh-quota/`. `/run` is a
tmpfs, and a quota window lasts one hour, so a reboot loses nothing worth keeping. The
identity is the calling uid when the token comes from that user's `gh` login. It is a
fingerprint of the token when the caller sets `GH_TOKEN`.

**The session key** is the first of these that exists:

1. `CLAUDE_CODE_SESSION_ID`, which Claude Code exports to every Bash call. A subagent shares
   its parent's value, so its spend counts as the session's.
2. The systemd unit name from `/proc/self/cgroup`, for a timer or the lander.
3. `operator`, for a call with neither.

**Classification** is a table in the shim: subcommand to bucket and weight. `gh auth`,
`gh config`, `gh version` and `gh api rate_limit` are free and write no row. A weight is an
estimate, because the shim does not see the HTTP requests `gh` makes. The ground-truth sample
corrects for that: `gh-quota status` scales each session's recorded weight so that the
sessions sum to what GitHub counted. What GitHub counted beyond the recorded rows is the spend
that bypassed the shim, and the tool prints it as `unattributed`.

**The mode** is one of `off`, `record` and `enforce`, read from `/etc/gh-quota.conf`, which
the host package renders from a host variable. `off` execs `gh` and does nothing else.

**The shim fails open.** A ledger it cannot open, lock or parse makes it exec `gh` anyway and
count the failure in a second file that `gh-quota status` reads. The shim's own outage must
not become a GitHub outage.

### The two rules the shim enforces

| Rule | Condition | Who it stops |
|---|---|---|
| Session ceiling | The session's scaled spend in the window has reached its ceiling for that bucket. | That session only |
| Reserve | GitHub's `remaining` for the bucket is at or below the reserve. | Every caller except class `priority` and `operator` |

- A caller is class `priority` when it exports `GH_QUOTA_CLASS=priority`. `land.sh` and the
  lander unit set it. The ledger records the class, so a session that sets it for itself
  shows in `gh-quota status`.
- The starting values are a ceiling of 20% of the bucket and a reserve of 20%. Both are
  placeholders until slice 1 has measured a week of normal spend.
- The design defends against a runaway loop, not against a session that evades the shim on
  purpose. A session can reach `/usr/bin/gh` directly. `unattributed` is how that shows.

**The shim holds briefly and otherwise refuses.** A hold shorter than 20 seconds happens
inside the shim, which sleeps and then runs the call. A longer one is a refusal: exit status
75 and one line on stderr.

```text
GH-QUOTA: verdict=refused scope=session bucket=graphql spent=1004 ceiling=1000 retry_s=1240
```

A long sleep inside the shim is the wrong shape for a session. The Bash tool stops a command
after 120 seconds by default, and a stopped command reports nothing. A refusal that names its
wait lets the session run `cc-wait` or do other work.

`scripts/lib/gh_rate_limit.py` owns what a rate-limit refusal means. It learns the
`GH-QUOTA:` line in slice 2, so `is_rate_limited` and `wait_s` answer for the shim's refusal
as they do for GitHub's. `gh_read.py`, `pace.py` and `await_ci.py` then wait on it with no
change of their own.

### REST readers that do not run `gh`

`host_lib.github_get` is the one request shape every host REST reader shares: `await_ci.py`,
checkout-sync, the builder and `renovate-notify`. It calls the ledger module's `admit` and
`record` directly, so those readers obey the two rules and appear in the ledger.

`github_get` also sends `If-None-Match` with the `ETag` of its last answer for the same path,
kept in the ledger. `await_ci.py` polls the same run list every 20 seconds, and a poll that
returns `304` costs nothing.

### Identical reads cost one call

The shim keeps the stdout of a read it has just run, keyed by identity and exact argv, for 15
seconds. A second caller with the same argv inside that time gets the kept answer and makes
no GitHub call. A caller that arrives while the first is still running waits for it instead
of starting its own.

- Only an allow-list of read subcommands is kept: `pr list`, `pr view`, `issue list`,
  `issue view`, `run list`, `run view` and a `gh api` GET. Only an exit status of 0 is kept.
- Any write through the shim drops every kept answer for that repository, so a session reads
  its own write.
- A write made elsewhere is up to 15 seconds stale. The polling loops here wait 20 to 30
  seconds between reads, so the staleness is inside one poll.

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

## What the design leaves out

- **No intercepting proxy.** A proxy counts every request exactly, covers a caller that never
  runs `gh`, and keeps the token out of a session. It needs TLS interception: a local CA that
  `gh`, Python and Node each trust through a different setting. Its outage is the outage it
  exists to prevent. The revisit condition is measurable: `unattributed` above 20% of a
  bucket over a week, after slice 3.
- **No identity per session.** GitHub's terms forbid it, as above.
- **No webhook receiver.** GitHub recommends webhooks over polling, and a receiver needs a
  public route and a state store. The 15-second read cache removes most duplicate polls for
  far less. Revisit it if the ledger shows polling is still most of the spend after slice 4.
- **No GitHub App for the reads the lander makes.** An installation token has its own quota,
  and minting one needs an RS256 signature the standard library cannot make. The identity
  split already keeps agent sessions out of the quota the lander spends.
- **The operator's PC.** The shim ships in a host package. A session on the PC spends
  `DanielH2018`'s quota with no ledger. The ground-truth sample on each host still counts
  that spend, as `unattributed`.

## Alternatives not taken

| Alternative | One session cannot starve the others | Agents cannot starve the lander or the operator | Cuts total demand | Why not |
|---|---|---|---|---|
| Wait after a 403, as today | No | No | No | The first 403 arrives when the pool is empty. |
| Identity split alone | No | Yes | No | Kept, but one agent session still starves the rest. |
| A reader daemon that mirrors GitHub state | For reads | Partly | Yes | A daemon and a second copy of state; the read cache gets most of it with neither. |
| An intercepting proxy | Yes, exactly | Yes | Yes | See *What the design leaves out*. |

## Complexity

The design adds one host component: a shim, a ledger module, a CLI and their tests. It adds
one process start to every `gh` call.

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

The shim runs in `record` mode on both hosts. It refuses nothing.

```diff
 hosts/components/
+  gh-quota/
+    component.yml          ships the files below where has_claude_code is true
+    files/gh_shim.py       -> /usr/local/bin/gh
+    files/gh_quota.py      -> /usr/lib/homelab/gh_quota.py: ledger, classify, sample
+    files/gh_quota_cli.py  -> /usr/local/bin/gh-quota
+    templates/gh-quota.conf.j2
+    tests/test_gh_quota.py
+    CLAUDE.md
 inventory/group_vars/all.yml
+  gh_quota_mode: record
 docs/github-quota.md
```

- **To exercise it,** run `gh pr list` in a session, then `gh-quota status --all`. The
  session appears with one `pr list`.
- **The tests** cover the session key order, the classification of each subcommand in the
  table, the scaling against a ground-truth sample, and the fail-open path with an unreadable
  ledger. A census test names the subcommands the repo's tools run, so a subcommand the table
  does not classify fails the suite.
- **It is done when** a week of ledger rows gives the two missing figures: the spend of a
  normal session-hour, and the points per subcommand.

### Slice 2: enforce the two rules

```diff
 hosts/components/gh-quota/
   files/gh_quota.py        + admit(): the session ceiling and the reserve
   files/gh_shim.py         + hold under 20 s, else exit 75 and the GH-QUOTA line
   files/gh_quota_cli.py    + release
 scripts/lib/gh_rate_limit.py   + reads the GH-QUOTA line
 scripts/deploy_tools/land.sh   + exports GH_QUOTA_CLASS=priority
 inventory/group_vars/all.yml   gh_quota_mode: enforce, ceiling and reserve from slice 1
```

- **To exercise it,** set a ceiling of 5 points for one session through the override file,
  run six `gh pr list`, and read the refusal. Run `gh pr list` in a second session and see it
  pass. Run `gh-quota release` and see the first session pass.
- **The tests** pair each rule with one caller it must refuse and one it must pass, and hold
  `gh_rate_limit.wait_s` against the refusal line's `retry_s`.

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
   files/gh_shim.py         + serve a kept answer; wait on a read in flight
```

- **To exercise it,** run the same `gh issue list` from two sessions inside 15 seconds.
  `gh-quota status --all` shows one call and one kept answer.
- **The tests** cover the write that drops a kept answer, the non-zero exit that is not
  kept, and two concurrent callers that produce one upstream call.

## Open questions

1. **Does `CLAUDE_CODE_SESSION_ID` reach a fan-out batch's `gh` calls with a value of its
   own?** A batch is its own `claude` process, so it should. Slice 1's first day of rows
   confirms it or shows every batch under one key.
2. **Whose account owns `homelab_builder_github_token`?** The builder and checkout-sync read
   with it. If `DanielH2018` owns it, those timers spend the operator's quota, and the ledger
   keys them by token fingerprint apart from the operator's own calls.
3. **Is a fixed ceiling enough, or does the ceiling need to shrink as sessions multiply?**
   With 39 sessions and a 20% ceiling, five simultaneous runaways still empty the pool. The
   reserve protects the lander either way. Slice 1's data says whether a fair share per live
   session is worth its extra rule.
4. **Does anything outside the repo call GitHub with the session's token?** The desktop
   app's pull-request pane is one candidate. `unattributed` measures it.
