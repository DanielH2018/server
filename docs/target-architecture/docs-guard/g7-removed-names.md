# G7 — A PR that removes a name fails while a doc or comment still uses it

Status: IN PROGRESS. Steps 1 and 2, the shared predicate and the heading pointer guard,
are done.

Depends on: the tree rename of
[#8186](https://github.com/DanielH2018/server/issues/8186), which landed on 2026-11-13.

Unblocks: [G9](g9-blocking-gate.md), whose replay reads the doc set this slice defines,
and [G11](g11-binding-sweep.md), which sweeps that set.

[ADR-0031](../../adr/0031-each-kind-of-fact-has-one-stateless-detector.md) records the
decision this slice serves.

## Goal and exit criterion

The largest class of stale-fact escape is a doc or comment that still names a thing a PR
removed or renamed. This slice widens two rules that exist, so the PR that removes the
name fails. `facts-lint-changed` runs both rules at commit time, and CI's hooks job runs
them on every PR.

Exit: a commit that deletes a function named in a skill and in a code comment fails the
hook, and the hook names both lines.

## Baseline at `4d85f67c5`

- The lint finds its docs through `repo_docs` in `scripts/lib/facts/citations.py`. The
  notice finds its docs through its own `CLAUDE.md` basename filter in
  `scripts/lib/facts/notice.py`. Both read `CLAUDE.md` only.
- `vanished-identifier` reads `CLAUDE.md` sections. It counts a name as present when any
  tracked non-Markdown file holds it, so a surviving comment keeps the name alive.
- At least six of the ten open stale-fact findings were removed or renamed names, in docs
  and comments the rule does not read.

## Design

- **One doc set.** One predicate in `citations.py` replaces both definitions. It covers
  `CLAUDE.md`, `.claude/rules`, skills and agents, so the lint, the notice and the replay
  read the same files. G5 declined this widening because it changed an interface G1 and
  G2 were building on, and both are done. The `facts-lint-changed` hook's file pattern
  follows the predicate.
- **More targets for `vanished-identifier`.** Beside instruction-file sections it reads
  hand-written `docs/**` and code comments. It skips `docs/archive/**`, ADR bodies, files
  with a `generated_from:` banner, text under `**HISTORY —`, and `DECIDED:` comment
  blocks. The rule stays driven by the PR's own diff, so it needs no state.
- **Comment text does not hold a name.** Only a full-line `#` or `//` comment counts as a
  comment, and a docstring counts as code. A name removed only from a comment line seeds
  no finding. The rule classifies only the lines `git grep -n` returns for a removed name,
  so it reads no whole file. Generated non-Markdown files leave the holder search through
  prek's exclude list. A code-comment finding reports `file:line`.
- **A ramp.** Each widened target class, and the comment-holder change, starts as a
  warning. A replay over the last 30 days of merged PRs counts the rows each PR would have
  received, and a sample is graded. A class becomes an error once fewer than one graded
  row in five is a false alarm. The existing `CLAUDE.md` error class changes only when the
  holder change passes the same test.
- **A heading pointer.** A doc that sends its reader to one part of another doc writes the
  doc's path in backticks and the part's name in italics. A docs guard fails when the doc
  is not tracked, or when no heading or bold lead-in in it holds that text
  ([#8221](https://github.com/DanielH2018/server/issues/8221) is this class). The plan
  first named two other shapes, `<doc>#<heading text>` in backticks and a link's slug. The
  instruction files held none of the first and one of the second, so the guard reads the
  shape the tree uses. It is a guard and not a citation form, because a form would make
  every edit under the heading a notice row, and G9 measured what such rows are worth.
  The guard skips the plans under `docs/target-architecture/`, which point at the tree as
  it stood when each was written.
- **A rename fixer.** `fact_status.py mv <old> <new> [--root <dir>]` rewrites citations
  and absolute links after a rename. A session runs it; no hook does. It skips
  `**HISTORY —` text and archives. `--root` points it at a store outside the repo, such as
  the memory store on daniel-box.

```diff
 scripts/lib/facts/
   citations.py
-    repo_docs(): CLAUDE.md only
+    is_fact_doc(path): CLAUDE.md, .claude/rules, skills, agents; repo_docs() calls it
+    heading_pointers(), names_a_part(): a doc path, then a part's name in italics
   notice.py
-    Tree.docs: its own CLAUDE.md basename filter
+    Tree.docs: calls is_fact_doc
   lint.py, removed.py
-    vanished-identifier: a name held only by a comment counts as present
-    vanished-identifier: reads CLAUDE.md sections
+    a full-line # or // comment does not hold a name; a removed comment line seeds none
+    targets: instruction files, hand-written docs/**, code comments
+    a class is a warning until its replay sample grades under 20% false alarms
+  renames.py     rewrite citations and absolute links for one old-to-new mapping
 scripts/dev/fact_status.py
+  mv <old> <new> [--root <dir>]
 prek: facts-lint-changed
+  its files pattern follows is_fact_doc
 tests/repo/test_documented_paths_exist.py
+  every heading pointer in the live docs names a part that exists
```

## Steps, as PRs

1. The shared predicate. The lint reads rules, skills and agents, and the errors it finds
   there are fixed in this PR. Done: the widened lint found one error and four warnings.
   At `1559855d5` the doc set is 109 files and 577 sections, 258 of which cite nothing.
   The 30-day replay shows 98 moved rows over it, against 93 over `CLAUDE.md` alone.
2. The heading pointer guard. Done: at `4356e0993` the live docs and instruction files
   held 85 pointers. 2 were stale and both were real: a skill named a heading that
   `docs/landing.md` had renamed, and a runbook named a doc by its path from before the
   tree rename. The PR fixes both.
3. The widened `vanished-identifier` targets and the comment-holder change, as warnings,
   with the replay that counts their rows.
4. `fact_status.py mv`.
5. One PR per target class that the graded replay clears, turning its warnings into
   errors.

## Verification

Each step ships a pair of tests: an input the rule accepts and one it rejects. Step 3's
replay prints rows per merged PR over 30 days, by target class.

## Risks and what is not verified

- The false-alarm share of the widened rule is unmeasured. Step 3 measures it before any
  class blocks.
- A removed name that survives as a substring of a longer name stays silent.

## Complexity delta

Adds one docs guard, one subcommand and one module. Replaces two doc-set definitions
with one. Widens one rule.
