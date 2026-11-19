---
id: "0040"
title: Dead names, copied tests and unused parameters are gated, and blind excepts are not
status: Accepted
date: 2026-11-19
governs:
  - "pyproject.toml#BLE001 and ARG005 stay off"
---

# ADR-0040: Dead names, copied tests and unused parameters are gated, and blind excepts are not

## Status

Accepted. It follows ADR-0038, which put design in front of the fan-out reviewer and set
every gate aside.

## Context

ADR-0038 found that each design standard reached the tree as a sweep, and that no sweep left a
check behind. The same day the operator asked for the gates it had set aside. Each candidate
was measured on `master` before it was built, and the measurement decided which became a gate.

**Dead names.** Five days after the 2026-11-14 vulture sweep, vulture reported nine names,
eight of them added since. Two were dead and were deleted. Six had a caller vulture cannot
see: a fixture a test requests for its effect, a keyword in a fake's signature, a function
that a script with no `.py` suffix calls. One was dead in a file whose changes need the
operator's approval to land (#9341). A
tenth appeared while the change waited to land: a dataclass field that the fan-out run
manifest records and no code reads back.

**Copied tests.** `scripts/dev/duplicate_tests.py` reported 20 groups of tests with identical
bodies, 62 tests in 42 files. The signal is exact: two tests are a copy when their arguments,
decorators and parsed bodies match.

**Tests named for a removal.** The `prune-tests` name patterns matched 160 tests in 101 files,
and most are sound tests of code that handles a removal. The skill puts the true hits for the
neighbouring pattern at about one in twenty.

**Unused parameters and blind excepts.** At the time, ruff's rules flagged:

| Rule | Whole tree | Outside tests |
|---|---|---|
| `ARG001` unused function argument | 646 | 41 |
| `ARG002` unused method argument | 113 | 3 |
| `ARG005` unused lambda argument | 608 | 21 |
| `BLE001` blind except | 90 | 84 |

A read of 8 of the 44 `ARG001` and `ARG002` hits outside tests found 3 dead parameters and 5
slots a fixed signature must accept. The hits inside tests are fakes that mirror the signature
of what they replace. The 84 blind excepts outside tests cluster where a broad catch is the
design: hooks that must not crash a session, monitor-bridge checks that must not take the
exporter down, and the byte-pinned karakeep script. No commit in ADR-0038's rework sample was
about a broad except.

## Decision

Three classes fail a commit, and one rule is prose with a reviewer behind it.

- **A Python name nothing uses** fails the `vulture` prek hook. `[tool.vulture] ignore_names`
  in `pyproject.toml` is its allowlist, and each entry sits under a comment naming its caller.
- **A test whose body is a copy of another test's** fails the third ratchet,
  `tests/_ratchet_census.py:COPIES`, against `tests/repo/copied_tests_allowlist.txt`. Entries
  only fall.
- **A parameter a function never reads** fails ruff's `ARG001` and `ARG002` outside tests. A
  slot a fixed signature must accept takes a leading underscore. The files that held a hit
  when the rules were selected are per-file ignores (#9354).
- **A cleanup ends with a check.** A change that removes instances of a pattern adds the check
  that fails on the next instance, or its commit message says why no check can. `CLAUDE.md`
  states it, the fan-out reviewer reports its absence as a `convention` finding, and the PR
  template carries a checkbox.

Four candidates stay off. `BLE001` and `ARG005` are not selected. Tests named for a removal
are not gated: the fan-out reviewer's `design` category covers them. Complexity limits are not
selected, because the rework sample held no commit that one would have caught.

## Consequences

The vulture hook took 6.5 seconds when it was added, on every commit that touches a `.py`
file, `pyproject.toml` or `uv.lock`. `scripts/dev/prek_durations.py` reports what it costs.

vulture cannot tell three sound shapes from dead code: a fixture requested for its effect, a
keyword in a fake's signature, and a dataclass field kept as a record. Each new one costs a
session one line in `ignore_names`. The estimate when the hook landed was one such edit a day
across all sessions, and the fourth class appeared within the hour.

A gate over the whole tree lands against a moving `master`. The vulture change went red once
on a name that landed while it stood in the landing line. A change that adds such a gate
refreshes its allowlist up to the merge.

Nothing stops a session listing a dead name in `ignore_names`, or adding its file to the
per-file ignores. The entry is visible in the diff, and for the ratchets the merge-base check
refuses a raised entry.

Two options from ADR-0038 wait on other work, and each has a finding that holds its design:

- A census of raw-dict signatures outside parse modules waits on the parse-at-the-boundary
  findings (#9393).
- A retirement manifest for a migration waits on the next migration (#9394). It is the only
  option that reaches the larger half of the rework, the code a migration made obsolete.

#9390 repeats ADR-0038's rework sample after 2026-12-19, with the starting value of each count
above. Revisit this decision on its result: if the share of rework that removes wrong-shape
code has not fallen, or if `ignore_names` has grown mostly by dead code that sessions chose to
list, the gates cost more than they return.

## Governs

`pyproject.toml` carries the one marker, where `BLE001` and `ARG005` would be selected. The
gates themselves are the `vulture` hook in `prek.toml`, `tests/_ratchet_census.py:COPIES` and
the `ARG001` and `ARG002` entries of `[tool.ruff.lint] select`.
