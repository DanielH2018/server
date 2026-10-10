---
id: "0019"
title: Fact support stops at the repo store, and the lock carries its own retirement test
status: Accepted
date: 2026-10-10
governs: []
---

# ADR-0019: Fact support stops at the repo store, and the lock carries its own retirement test

## Status

Accepted.

## Context

The fact-support design of 2026-09-19 planned seven slices. Slices 1–3 shipped. They give
every `CLAUDE.md` section a status derived from the atoms it cites, record those atoms'
hashes in `docs/facts.lock`, and fail CI when a recorded atom moves. Slices 4–7 would have
extended the same model to the Claude memory store:

- support records in each memory file's frontmatter, with a generated status line;
- a setup role running a reconcile timer every 30 minutes, with a status file and a
  SessionStart banner;
- an agent re-verifier dispatched once per OUT entry;
- batch conversion of the memory entries.

The slices were never built. A survey on 2026-10-10 measured what the shipped half
does and what the rest would buy:

- 153 of the 192 lock rows held path atoms only. A path atom hashes existence since
  `96365b0b5`, so for those sections IN meant that the files they name still exist.
- Content atoms (symbol, YAML, test and marker) moved 10 times in the 30 days to
  2026-10-10, and 3 of those moves came with a prose fix. That is the lock's real yield.
- The memory store is shrinking. #2808 moves durable memory claims into tracked docs and
  checks, and 103 memory entries were archived against 68 live. `memory-upkeep.py` already
  reports, at session start, a memory that names a deleted path.
- Code for slices 4–7 sat in the shipped half and never ran. The probe citation
  form was recognised but never hashed, so the 17 sections that cited a probe graded UNKNOWN
  permanently. The UNDECLARED status and the `supported_by_out` relation were always empty.

A reconcile timer, a heartbeat, an autonomous-role contract, a status file, a SessionStart
hook and an agent loop whose verdicts are not deterministic would cost more than the
shrinking store they protect.

## Decision

Fact support covers `CLAUDE.md` sections only. Slices 4–7 are not built. The probe citation
form, the UNKNOWN and UNDECLARED statuses, and the relations that served only the memory
store are removed. The four statuses left are IN, OUT, UNVERIFIED and CONVENTION.

The lock stays while its content atoms find prose fixes. When `fact_status.py report
--days 60` shows fewer than two actual moves across the symbol, YAML, test and marker forms
together, the lock is retired: `docs/facts.lock`, `verify`, `reverify` and their CI tests
go, and `lint` stays with its prose rules. Path atoms are left out of that count, because
they hash existence and never record a prose fix.

The `count-as-fact` lint rule is removed in the same change. Its pattern matched any count
word next to a noun such as `files` or `roles`. About 80% of its 17 standing warnings were
stable descriptions, and none of them had been acted on. The three that could go stale were
rewritten by hand, one of them as a symbol citation.

## Consequences

**A section that cites `probe.py <subcommand>` grades on its other citations, or as
CONVENTION.** No check confirms that a probe subcommand a doc names still exists. Nothing
confirmed it before either, because the probe form was never hashed.

**A memory entry gets no status line and no re-verification.** Memory staleness stays with
`memory-upkeep.py` and with promoting durable claims into tracked docs and checks.

**The lock now has an exit.** `report` measures the criterion from git alone, so retiring
the lock is a measurement, not a debate. Reversing this record means building slices 4–5
from the 2026-09-19 design. That design was a local plan file and was never committed, so
this record is the only tracked trace of it.

## Governs

None. The decision is a scope, and no single line enforces it.
`scripts/lib/facts/relations.py:STATUSES` holds the four remaining statuses, and
`scripts/lib/tests/test_facts_relations.py::test_status_census` pins them.
