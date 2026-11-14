# G10 — A row re-derives each stated value, and a hook rewrites it

Status: IN PROGRESS. Step 1, the registry and its check, is done.

Depends on: the tree rename of
[#8186](https://github.com/DanielH2018/server/issues/8186), which moves the test file this
slice retires.

Unblocks: [G11](g11-binding-sweep.md), which adds rows.

[ADR-0031](../../adr/0031-each-kind-of-fact-has-one-stateless-detector.md) records the
decision this slice serves.

## Goal and exit criterion

A count, a schedule or a cap typed into prose goes stale when the tree changes.
`test_docs_quote_current_values.py` holds two rows that re-derive such a value and compare
it with the doc. This slice makes the rows a registry, adds a fixer, and opens it to every
doc class. The prose keeps its plain literal, so no markup enters a file an agent reads.

Exit: a commit that changes the list the docs-refresh cron stages rewrites the count in
`.claude/rules/generated-docs.md` and fails once, as `regen-doc-fragments` does.

## Baseline at `4d85f67c5`

- Two rows exist, over two pages.
- [#8324](https://github.com/DanielH2018/server/issues/8324) is the class: a hand count in
  a doc read 16 where the tree held 19.
- ADR-0019 removed the `count-as-fact` lint rule, which guessed nouns. About 80% of its 17
  warnings were stable descriptions.

## Design

- A row names the doc, one regex with one capture group, the file the value comes from,
  and how to read it from that file. A second phrasing of the same value gets its own
  row, and so does a second doc. A row may carry a formatter for a number written as a
  word. `scripts/docs/doc_values.py` holds the rows and the check. It sits beside the
  fragment generator, which calls its fixer from step 2 on.
- The check compares each captured text with the tree's value. It keeps the existing rule
  that a row matching nothing fails, so a prose edit cannot turn a row into a check of
  nothing.
- `--fix` substitutes the captured group. It refuses when the regex matches more often
  than the row declares. A prek hook runs `--fix` and fails once. The hook's file pattern
  covers each row's docs and sources. A CI test runs the check for a commit that skipped
  the hook.
- A Renovate-managed value never gets a row, because Renovate runs no hook and a bump PR
  must not arrive red ([#4012](https://github.com/DanielH2018/server/issues/4012)).
  `version-as-fact` stays a warning.
- The slice tables in `docs/target-architecture/design.md` become a fragment generated
  from each slice file's status line.
- A row is added in the PR that fixes a value escape, and by G11. No lint guesses which
  numbers are values.

```diff
 scripts/docs/
+  doc_values.py
+    VALUES: rows of (doc, one capture regex, source, read, expected matches, formatter)
+    stale(): fails a row that matches nothing
+    the fixer refuses on too many matches
   gen_doc_fragments.py
+    --fix also runs the VALUES fixer
+    FRAGMENTS += the slice status table
 prek
   regen-doc-fragments   files pattern from the rows
-test_docs_quote_current_values.py     its two rows move into VALUES
+scripts/docs/tests/test_doc_values.py: every VALUES row matches the tree
```

## Steps, as PRs

1. The registry and its check, with the two rows moved in and the old test file deleted.
   Done. A row holds four fields. The expected match count and the formatter arrive
   with the step that reads them.
2. The fixer and the prek hook, with the row for the count of paths the docs-refresh cron
   stages.
3. The slice status fragment.

## Verification

Each step ships a row the check accepts and one it rejects. For step 2, change a derived
value and commit: the hook rewrites the literal.

## Risks and what is not verified

- A value written in a phrasing no row matches stays unchecked. The escape register shows
  these, and each one adds a row.

## Complexity delta

Adds one registry and one fixer. Removes one test file.
