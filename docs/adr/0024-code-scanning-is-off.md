---
id: "0024"
title: Code scanning is off
status: Accepted
date: 2026-10-18
governs: []
---

# ADR-0024: Code scanning is off

Supersedes [ADR-0016](0016-code-scanning-stays-on-default-setup.md).

## Status

Accepted.

## Context

ADR-0016 kept CodeQL on GitHub's default setup and removed false positives in code. Default
setup cannot be narrowed by path or by event: it runs on every pull request and on every
push to master.

During the D5 fan-out on 2026-10-17, CI demand ran above the account's 20-job runner cap
(#5526). From 22:00 to 00:00Z, CodeQL ran 94 times, as 368 jobs and 424 job-minutes. That
was 16% of all runner minutes, second only to `ci.yml`. CodeQL's jobs queue for the same
runners as the CI gate's jobs, so every one of them lengthens the master CI wait that a
landing pays. In that window a master push run waited a median of 6 minutes for its first
job.

#5526 named three options: drop languages, move to an advanced-setup workflow that runs on
push and weekly only, or turn code scanning off. CodeQL's record is in ADR-0016: to
2026-09-02 it raised 44 alerts, 12 fixed in code and 31 dismissed by hand as false
positives.

## Decision

Code scanning is off. The operator ruled on 2026-10-18: "you can just remove CodeQL
entirely." The operator turned default setup off the same day, and
`gh api repos/DanielH2018/server/code-scanning/default-setup --jq .state` reports
`not-configured`. No workflow in `.github/workflows/` runs CodeQL, and no ruleset or
required check names it.

## Consequences

- **CI loses a static-analysis pass on every pull request.** prek still runs ruff, ty,
  gitleaks, the repo's guard hooks and the test suite. The 12 alerts fixed in code measure
  what CodeQL caught. The 31 hand dismissals measure what it cost.
- **The `# DECIDED:` markers that ADR-0016 governs stay.** Each states a TLS floor
  explicitly. That is correct code with or without a scanner.
- **Comments that name a CodeQL rule as the reason for a code shape describe history.**
  Nothing re-checks them. Examples are in `ansible/tests/repo/test_census_rows_python.py` and
  `scripts/deploy_tools/land_lib/land_tags.py`.
- **To reverse:** run `gh api -X PATCH repos/DanielH2018/server/code-scanning/default-setup
  -f state=configured` as the operator, then supersede this ADR. Alerts dismissed before
  2026-10-18 stay dismissed.
