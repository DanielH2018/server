---
id: "0016"
title: Code scanning stays on default setup, and false positives are removed in code
status: Accepted
date: 2026-09-02
governs:
  - scripts/diagnostics/probe_lib/ha.py:283
  - ansible/roles/k8s/homelab-mcp/files/safe_reads.py:238
---

# ADR-0016: Code scanning stays on default setup, and false positives are removed in code

## Status

Accepted.

## Context

Code scanning runs under GitHub's **default setup** — `gh api
repos/DanielH2018/server/code-scanning/default-setup` reports `state: configured`,
`query_suite: default`, `languages: [actions, python]`, `threat_model: remote`, weekly
schedule. Default setup reads no CodeQL configuration file, so neither `query-filters` nor
`paths-ignore` is available and every false positive is handled by dismissing one alert by
hand.

**Hand-dismissals do not survive a refactor.** Alert identity is fingerprinted on location, so
a dismissed alert returns under a new number as soon as an edit moves the line. Five
recurrences are on the record:

| Original | Returned as | What moved |
|---|---|---|
| #8 | #42 | the `infra_map` package rename |
| #29 | #43 | a `land_tags.py` refactor |
| #11 | #36 | `scripts/dev/test_gen_hosts_block.py` moved into `tests/` |
| #17 | #37 | `homelab-mcp/files/test_safe_reads.py` moved into `tests/` |
| #24 | #44 | `probe_ha.py` became `probe_lib/ha.py` (PR #858) |

Across the repo's whole history that is 44 alerts: 31 dismissed by hand, 12 fixed in code,
1 open at the time of writing.

Four options existed.

**Advanced setup** — a hand-owned `codeql.yml` workflow plus a config — buys `query-filters`
and `paths-ignore`. It costs a workflow that must reproduce what default setup does for free:
both languages, the `remote` threat model, the weekly schedule, and the `merge_group` trigger
this repo's merge queue depends on. A workflow that omits one of those reads as the filters
working while coverage quietly narrows.

**A config file applied to default setup** — the `github-codeql-config-file` repository
property announced 2026-08-04 — would give the same levers without owning a workflow. It does
not apply here: repository custom properties are an organization feature, and this repo is
owned by a user account (`gh api repos/DanielH2018/server/properties/values` returns 404).

**Inline suppression comments** (`# codeql[py/...]`) move with the line, which is exactly the
failure mode above. GitHub code scanning does not act on them by itself: CodeQL records them
as SARIF `suppressions` data, and something has to read that and call the alerts API —
`advanced-security/dismiss-alerts` is the action that does. That path needs advanced setup
plus a second moving part.

**Removing the flagged shape in code** is what PR #863 did for two alerts, and it is the only
fix that holds across a move.

The filters advanced setup would buy cannot express what the remaining noise needs.
`paths-ignore` drops a file from *every* query and `query-filters` filters a query across
*every* file; there is no path×query intersection. The 13 remaining
`py/incomplete-url-substring-sanitization` alerts are `assert "name" in list_of_strings`
membership checks in tests — `ansible/tests/k8s/test_k8s_manifests_rbac.py`,
`scripts/dev/tests/test_gen_hosts_block.py` and siblings. Ignoring those paths would also
strip `py/clear-text-logging-sensitive-data` from them, which is the query covering the secret
discipline this repo cares most about; and one of the paths,
`ansible/roles/k8s/homelab-mcp/tests/test_safe_reads.py`, tests
`allowed_hosts_and_origins` — the repo's only genuine Host-header allowlist, and precisely
what the noisy query exists to check.

## Decision

Code scanning stays on default setup. A false positive is removed at its source where a
source can be removed, and hand-dismissed where it cannot.

`py/insecure-protocol` on `ssl.create_default_context()` is removed at its source: both call
sites now state the protocol floor with `ctx.minimum_version = ssl.TLSVersion.TLSv1_2` behind
a `tls_context()` helper. The floor is already 1.2 on this Python, so no handshake changes —
the assignment makes the guarantee explicit and denies the query the shape it reports.

The `assert "name" in list_of_strings` membership assertions stay as they are and stay
dismissed. Rewriting idiomatic test assertions to satisfy an imprecise query costs more
readability than the dismissals cost effort.

## Consequences

**A recurrence still costs a hand-dismissal.** The measured rate is 5 over the repo's history,
one API call each:

```
gh api -X PATCH repos/DanielH2018/server/code-scanning/alerts/<n> \
  -f state=dismissed -f dismissed_reason="false positive" -f dismissed_comment="…"
```

That is the price this decision accepts. It rises if the membership-assertion files are moved
or renamed in bulk, and a single refactor that moves all 13 would make advanced setup worth
re-opening.

**Coverage stays whole.** No query is excluded and no path is exempt, so a real finding in a
test file is still reported — including in the one test that exercises the Host-header
allowlist.

**The `tls_context()` helper is duplicated across two trees.** `probe_lib/ha.py` and
`homelab-mcp/files/safe_reads.py` each carry their own copy, because the MCP server's `files/`
ship list cannot import from `scripts/`. Two five-line copies is the cost of that boundary.

**Whether the fix silences the query is confirmed by the scan, not by reading it.** Default
setup analyzes pull requests, so alert #44's fate is visible on this PR before it merges. If
the query still reports the shape, the fallback is the hand-dismissal that was already the
status quo — nothing is lost.

**Reversing this means enabling advanced setup**, which disables default setup and is a
repository security-settings change. Do it deliberately, not as a side effect of landing a
workflow file.

## Governs

- `scripts/diagnostics/probe_lib/ha.py:283` — the `tls_context()` marker in the probe library.
- `ansible/roles/k8s/homelab-mcp/files/safe_reads.py:238` — the same marker in the MCP
  server's ship list.

Both markers explain why the TLS floor is stated rather than inherited. Neither enforces the
setup choice itself; nothing does, because a setup choice lives in repository settings rather
than in the tree.
