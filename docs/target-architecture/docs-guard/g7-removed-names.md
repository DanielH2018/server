# G7 — A PR that removes a name fails while a doc or comment still uses it

Status: IN PROGRESS. Steps 1 to 4 are done: the shared predicate, the heading pointer
guard, the two new target classes as advice, and the rename fixer.

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
- **Two more target classes.** Beside instruction-file sections the rule reads live docs
  and code comments. A live doc is any Markdown file that is no instruction file and sits
  outside `docs/archive/`, the ADRs, the slice plans and the generated pages. The rule
  skips text under `**HISTORY —` and `generated_from` blocks. It stays driven by the PR's
  own diff, so it needs no state.
- **A comment names a token, and holds none for its own class.** Only a full-line `#` or
  `//` comment counts as a comment, and a docstring counts as code. A name removed only
  from a comment line seeds no finding. A comment finding reports `file:line`. A comment
  has no backticks, so its token needs an underscore, a dot or two hyphens.
- **A doc's name is held by any non-Markdown content, comments included.** The plan first
  let a comment hold nothing for docs too. Step 3 measured that change and dropped it.
- **A ramp.** Each new class is advice: `scripts/dev/repo_guards.py` prints it after its
  guards and changes no exit code. A warning in a hook that passes reaches nobody, which
  is what G9 found for the notice. A class becomes an error once fewer than one graded row
  in five is a false alarm.
- **One read of the tree.** The rule holds every tracked non-Markdown file in memory and
  answers from there. One `git grep` per token scans the tree each time: the first build
  took 5 minutes over a 150-commit range that removed 3,600 tokens, and the in-memory read
  takes 7 seconds. The commit hook reads the tree only when an instruction file names a
  removed token.
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
- **A rename fixer.** `fact_status.py mv <old> <new> [--root <dir>]` rewrites the
  references a rename left behind. A session runs it; no hook does. It rewrites the places
  the removed-name rules read: instruction files, live docs and full-line comments. It
  skips `**HISTORY —` text, generated blocks and archives. `--root` points it at a store
  outside the repo, such as the memory store on daniel-box. Under a moved directory it
  rewrites a reference only when the resulting path is tracked, because a directory
  seldom moves whole.

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
-    vanished-identifier: reads CLAUDE.md sections
-    still_present(): one git grep per token
+    Code: the tracked non-Markdown tree, read once; holds(), comment_uses()
+    vanished_advice(): live docs and code comments, printed by repo_guards.py
+    a class is advice until its replay sample grades under 20% false alarms
+  renames.py     move(): rewrite one old-to-new mapping in docs and comments
 scripts/dev/fact_status.py
+  mv <old> <new> [--root <dir>] [--dry-run]
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
3. The two new target classes, as advice. Done: *Step 3 result* below has the replay and
   the graded samples.
4. `fact_status.py mv`. Done. On the tree at `fc561e541` a dry run of the mapping
   `ansible/tests` to `tests` finds 68 references in 53 files, and every one lands on a
   tracked path. On the memory store the same mapping finds 7 it can rewrite and leaves 5,
   all under `ansible/tests/setup/`, whose tests moved somewhere else.
5. One PR per target class that the graded replay clears, turning its warnings into
   errors.

## Step 3 result

A replay ran the widened rule over the 1,896 first-parent commits of the 30 days to
2026-11-14, with each commit's parent as the base. A spread sample of each class was
graded: a row is a false alarm when the sentence or comment it names was not stale.

| Class | Rows | Commits | Graded | False alarms |
|---|---|---|---|---|
| Live docs, name held by nothing | 184 | 53 | 19 | 6, or 32% |
| Live docs, name held only by a comment | 61 | 25 | 6 | 5, or 83% |
| Comments, any identifier shape | 847 | 141 | 25 | 12, or 48% |
| Comments, the narrowed shape | 444 | 94 | 31 | 5 to 8, or 16% to 26% |

- **Neither class is under the bar, so both ship as advice.** The comment class is close:
  five of its rows are false alarms, and three more could be graded either way.
- **The comment-holder change is dropped for docs.** A name that survives only in comments
  was an external one in five of six rows, such as `SUDO_ASKPASS` and `kern.log`.
- **The narrowed shape removes 403 comment rows.** Eleven of the twelve false alarms in
  the first comment sample were hyphenated English compounds, such as `pre-migration`.
- **The doc false alarms are sentences about the past.** Four of the six say the thing is
  gone, without the `**HISTORY —` marker. The other two name an upstream file and a host
  path.
- **The plans, the archive and the ADRs stay out.** The plans alone gave 3,437 rows, and
  they describe the tree as it stood when each was written.
- The window held the host migration and the tree rename, so a quiet month gives fewer
  rows. One session graded every row.

On master at `8140e8cf9` the advice over the last 150 commits lists 53 removed names in 74
places. 43 of the names are paths under `ansible/` in comments that the tree rename left
behind.

The replay is a scratch program: it holds each commit's tree in memory and updates it per
commit, in 98 seconds. It joins `fact_status.py report` in step 5, which is the first step
that needs a second measurement.

## Verification

Each step ships a pair of tests: an input the rule accepts and one it rejects. Step 3's
replay counted rows per commit over 30 days, by target class.

## Risks and what is not verified

- One session graded the step 3 samples, and the largest sample held 31 rows. Step 5
  grades again before a class blocks.
- Advice changes no exit code, so a session can ignore it. Step 5 is the remedy for a
  class that grades under the bar, and no remedy exists for one that does not.
- A removed name that survives as a substring of a longer name stays silent. So does a
  doc's name that a comment still holds.

## Complexity delta

Adds one docs guard, one subcommand, one module and one advice source in
`scripts/dev/repo_guards.py`. Replaces two doc-set definitions with one, and one
`git grep` per removed token with one read of the tree. Widens one rule.
