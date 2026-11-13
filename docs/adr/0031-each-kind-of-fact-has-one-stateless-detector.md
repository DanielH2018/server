---
id: "0031"
title: Each kind of documented fact has one home and one stateless detector
status: Accepted
date: 2026-11-13
governs: []
---

# ADR-0031: Each kind of documented fact has one home and one stateless detector

## Status

Accepted on 2026-11-13. The operator asked for an end-state plan for facts and memory,
said that earlier decisions were open to reversal, and ruled on the two questions the plan
left open:

1. The notice may block merges again, under the two thresholds in
   [G9](../target-architecture/docs-guard/g9-blocking-gate.md).
2. A replica memory store becomes read-only. An outbox for memories written on a replica
   host is deferred until a count justifies it.

Amends [ADR-0019](0019-fact-support-stops-at-the-repo-store.md): fact support covers
`.claude/rules`, skills and agents as well as `CLAUDE.md`. ADR-0019's ruling on memory
stands. Amends [ADR-0023](0023-fact-support-is-a-diff-notice.md): the notice stays
advisory until G9's measurement passes, and G9 records the flip in its own ADR.

G9's measurement ran on 2026-11-13 and failed its first threshold: 3 of the 48 moved rows
that nobody edited left a false sentence, against the 20% the gate needed. The gate is not
built. Where this record says the gate, the advisory notice does that job, and no ADR
takes over the `DECIDED:` marker on the notice step. G11 no longer waits on G9.

Slices G6 to G11 under `docs/target-architecture/docs-guard/` carry the work.

## Context

**The audit.** At `4d85f67c5`, generated content was the only class of fact that could not
go stale. The rest was detected late, by a review session after the merge.

| Store | Size | What detected a stale claim | Gap |
|---|---|---|---|
| Generated reference pages, fragments, glance blocks | 10 pages, 28 fragments, 69 blocks | A hook or the docs-refresh cron regenerates them | None |
| `CLAUDE.md` | 80 files, 411 sections | The lint blocks an unresolved citation. The notice reports a moved atom and never blocks. | 176 sections cited nothing. 172 of the 235 citing sections cited paths only. No hook delivered the notice. |
| `.claude/rules`, skills, agents | 29 files | Path and test-node existence | No lint and no notice |
| Hand-written `docs/**` | 94 pages | Existence, links, 2 quoted-value rows | The freshness page is advisory |
| Memory store | 47 live files | `memory-upkeep.py`: cited path exists | It reported 0 of 10 dead paths, because a path in any worktree counted |
| Code comments | not counted | None | 3 of the 10 open stale-fact findings |

Ten findings open on 2026-11-13 were single stale-fact corrections filed by review
sessions. At least six were a doc or a comment that still named a thing a PR had removed
or renamed.

**The first design did not survive review.** It computed a state for every claim from git
history and called a claim attested at its last edit. Three review passes confirmed 26,
then 17, then 20 findings at medium or higher. Each repair added a rule, and each rule drew
findings of its own: any edit attests, then a trivial edit attests every atom in the
section, then a moved paragraph attests. A text diff cannot show that a reader checked a
sentence. The same design gave memory a multi-writer git repository, which drew eight
findings for a 47-file store, and it gave values an inline span syntax that needed a
rewriter, edit protection and placement rules.

A fourth pass replayed the 63 confirmed findings against the design this record accepts.
34 were moot, 23 resolved, 3 were stated limits and 3 still applied. That pass confirmed
13 findings in all, and every fix narrowed the design.

**What others do.** No tool surveyed verifies prose truth deterministically. The
deterministic options are to generate the text, to execute it, or to flag it when its
source changes. GitHub's Copilot memory verifies a memory's code citations when a session
uses it, and GitHub rejected offline curation on cost. No shipped agent-memory system
surveyed detects staleness without a model.

## Decision

Each kind of sentence that can be wrong has one home and one detector. No detector keeps
or infers state across commits: each compares two trees, or a file against a tree.

```text
a sentence that can be wrong            detector                                 compares
├─ names a path, symbol, flag or host → lint: the citation resolves              doc vs tree
│                                       vanished-identifier: a PR removed it     PR base vs PR head
├─ states a value                     → a value row re-derives it; --fix writes  doc vs tree
├─ states a behaviour                 → a test with a `# fact:` backref          the test run
├─ describes code                     → the gate: a cited atom moved in this PR  PR base vs PR head
├─ memory, citing the repo            → memory-upkeep: the citation resolves     memory vs origin/master
└─ history, decision                  → `**HISTORY —`, ADR                       nothing; cannot go stale
```

| Store | End state | Slice |
|---|---|---|
| `CLAUDE.md`, rules, skills, agents | The lint, the blocking gate, value rows | G7, G9, G10, G11 |
| Hand-written `docs/**` | The removed-name check and value rows. The gate does not cover it. | G7, G10 |
| Code comments | The removed-name check | G7 |
| Memory | One resolver against `origin/master`, a checked index and archive, a read-only replica | G6, G8 |
| `docs/archive/**`, ADR bodies | Unchanged. They describe the tree as it was. | — |

## Consequences

**Two classes are repaired by a tool, and the rest are only detected.** A rename fixer
rewrites a renamed path, and a value fixer rewrites a number. A behaviour is as true as
its test. A description is delivered, not proven: the gate guarantees that the session
changing the code sees the sentence and records a decision, and a trivial edit or a rote
acknowledgement satisfies it. G9 audits a sample of both.

**Memory is checked for existence only.** A memory whose cited code changed under a
surviving name is not detected. At the audit the store held 10 content citations, which
did not justify a stored baseline. The promotion ladder answers it: a durable repo fact
moves into the repo, where the gate reads it.

**A session on a replica host cannot save a memory.** Before this record the write was
deleted at the next sync without a message. G8 makes it fail at once and names the route.

**What the end state does not catch:**

- Prose that cites nothing and names nothing. G11 shrinks it, and nothing detects it.
- A comment that is wrong without naming a removed thing.
- A removed name that survives as a substring of a longer name.
- A description in hand-written `docs/**` whose code changed with no name removed.
- A merge race: a PR green at its base while master gains a doc that names what the PR
  removes.
- A commit that reaches master without land.sh's squash.

**Considered and dropped**, each for a reason the review confirmed:

| Idea | Why it went |
|---|---|
| A claim state computed from git history, attested at the last edit | A text diff cannot show that a reader checked a sentence |
| A changed-since-written state for memory, read from the `modified` key | The same inference, on a baseline nobody maintains |
| Memory checked by this repo's facts library | A dotfiles hook cannot import it inside its time budget, and ADR-0019 assigns the check to `memory-upkeep.py` |
| A debt register, a frozen baseline, a reopen marker | They exist only to manage stored or inferred state |
| State lines injected when a session reads a doc | It needs the state model, several hooks and a cache |
| A private git repository for memory | Eight findings for a 47-file store, and it makes unattended sessions on a replica host writers |
| A generated `MEMORY.md`, or a hook that repairs it on each write | Both race the harness prompt that appends pointer lines |
| Inline value spans | A syntax, a rewriter, edit protection and placement rules, where a regex row needs none |
| A separate CI job for the gate | The aggregate job reads a job skipped on a push as a failure |
| A ratchet on sections that cite nothing | It fails restructures and moves, as the role-doc size test did before the operator dropped it |

**Later, on evidence.** Each waits for a count from G11 or from the findings register:

- a changed-since-written state for memory, once the store holds enough content citations
  to justify a stored baseline;
- the gate over hand-written `docs/**`, if description escapes keep arriving from docs
  pages after G7 and G10;
- a command citation checked against a tool's argparse parser;
- an outbox for memories from a replica host, if more than a handful of refused writes in
  the 30 days after G8 were operator preferences or held security findings;
- an expiry date on a memory that states an external fact, and a probe citation for a
  live-cluster claim.

**How to tell it worked.** Each number comes from `fact_status.py report` or the findings
register: stale-fact findings filed by review sessions per month, each tagged with the
detector that should have caught it; removed-name rows by target class and their graded
false-alarm share; gated sections by outcome; the share of sections that cite nothing,
weighted by load; and unresolved memory citations at session start. A slice that lands and
moves none of its numbers is inert.

**Reversing this record** means restoring the advisory notice, which is one CI step, and
narrowing the doc predicate back to `CLAUDE.md`. The value rows and the removed-name check
stand without the gate.

## Governs

None. G9's own ADR takes over the `DECIDED:` marker on the notice step in
`.github/workflows/ci.yml` when the gate starts to block.
