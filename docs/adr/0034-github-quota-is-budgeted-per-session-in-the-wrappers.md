---
id: "0034"
title: GitHub quota is budgeted per session, inside the wrappers that already run gh
status: Accepted
date: 2026-11-14
governs:
  - "scripts/lib/gh.py#the budget lives in the wrappers that already run gh, not in a shim"
---

# ADR-0034: GitHub quota is budgeted per session, inside the wrappers that already run gh

## Status

Accepted. The operator accepted the design on 2026-11-14 and asked for its first slice.
[GitHub API quota](../github-quota.md) holds the design in full, the measurements behind it
and the state of each slice.

## Context

GitHub meters API calls per identity, per hour, and counts every token of one user against
one quota. Every Claude session that starts as `ubuntu` on a host uses the operator's `gh`
login, so the sessions, the lander and the operator's terminal spend one pool.

On 2026-11-14 that pool ran dry with 39 sessions live on daniel-box: the GraphQL quota read
5000 of 5000 points used. Three properties produced it together.

- **One pool, metered twice.** Nothing separates one session's spend from another's. GitHub
  meters the pool per hour and meters bursts per minute. The same day, a backfill of about
  130 project-board writes in a few seconds tripped the per-minute limit, and GitHub refused
  GraphQL for every session on the login for about five minutes.
- **Demand grows with the session count.** Each session polls on its own. The callers in
  flight at the measurement were identical list reads from different sessions.
- **No owner.** `scripts/lib/gh_rate_limit.py` computes a wait from GitHub's 403, and GitHub
  sends the first 403 when the quota is already empty for every caller. No record said which
  session had spent it.

The operator's first thought was a local proxy with a rate limit per session. The first
draft of the design put the budget in a shim installed ahead of `gh` on `PATH`. The operator
ruled that out the same day: GitHub already has one shim, `ghread`, and a second one beside
it is a second thing to understand.

Three owners already stood in front of GitHub. `ghread` reads for a session's shell, and
claude-guard's `ghread-route` rule denies the bare `gh` form of each read and names `ghread`.
`scripts/lib/gh.py` runs `gh` for the repo's Python tools, and the
`git-and-gh-go-through-lib` census row refuses a bare call elsewhere.
`hosts/lib/host_lib.py:github_get` is the one REST request shape.

## Decision

The budget is a library, `gh_quota`, that those three owners call. It adds no executable to
the path a `gh` call takes. A ledger under `/run/gh-quota` counts each call against the
session that made it and stores GitHub's own count beside it, and `gh-quota status` reports
both. The same ledger later refuses a session that has spent its ceiling, holds a reserve for
the lander and the operator, paces the identity's writes, and answers an identical read from
a short cache. The slices on
the design page build those in that order, and the first one only records, so the ceiling is
set from measured spend.

## Alternatives not taken

- **A `gh` shim on `PATH`.** It reaches every caller with no change to any of them, a bare
  `gh` included. It is a second GitHub shim, and it shadows a binary by its position on
  `PATH`. claude-guard already routes a session's reads to `ghread`, which gets the same
  coverage for reads without it.
- **An intercepting proxy.** It counts every request exactly and keeps the token out of a
  session. It needs TLS interception with a local CA that `gh`, Python and Node each trust
  through a different setting, and its outage is the outage it exists to prevent.
- **An identity per session.** GitHub's terms of service, section H, forbid sharing tokens
  to exceed its rate limits, and GitHub hid the `DanielClaudeBot` account on 2026-10-10.
- **A daemon that mirrors GitHub state, or a webhook receiver.** Each adds a process and a
  second copy of state. A short read cache in the ledger removes most duplicate polls with
  neither.

## Consequences

- **A bare `gh` is outside the ledger.** A session's writes and the operator's terminal are
  neither counted nor refused. GitHub still counts them, and `gh-quota status` reports the
  difference as spend beyond the ledger. That figure is the test of this decision: if it
  stays large after every read is routed, the shim on `PATH` is the alternative to reopen.
- **Two repositories carry one design.** `ghread`, `cc-wait` and claude-guard live in the
  dotfiles repo, and `ghread` imports a module a homelab host package ships. The import is
  optional, so neither repo breaks without the other, but a slice that touches `ghread`
  needs a pull request in each.
- **The ledger fails open.** A ledger that cannot be written lets the call through and counts
  the failure, so a full tmpfs or a corrupt file costs attribution, never GitHub access. A
  silent stretch of failures therefore reads as low spend, and `gh-quota status` prints the
  failure count for that reason.
- **Call counts are not GraphQL points.** The ledger counts calls, and one GraphQL call can
  cost several points. Until the per-call cost is fitted from the recorded windows, spend
  beyond the ledger overstates what went around the wrappers for that bucket.
- **Reversing it is cheap.** `gh_quota_mode: "off"` in a host's host_vars makes every call
  into the module return at once from that host's next package version.

## Governs

- `scripts/lib/gh.py`, at the `gh_quota.record` call in `gh`.
