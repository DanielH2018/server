# G10 — A row re-derives each stated value, and a hook rewrites it

Status: DONE. Step 1, the registry with its check, merged as
[#8565](https://github.com/DanielH2018/server/pull/8565). Step 2, the fixer with its hook,
merged as [#8675](https://github.com/DanielH2018/server/pull/8675) and met the exit
criterion. The operator declined step 3 on 2026-11-14.

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
  row, and so does a second doc. The reader returns the text the doc should state, so a
  number written as a word needs no separate formatter. `scripts/docs/doc_values.py`
  holds the rows, the check and the fixer, beside the fragment generator.
- The check compares each captured text with the tree's value. It keeps the existing rule
  that a row matching nothing fails, so a prose edit cannot turn a row into a check of
  nothing.
- A row's pattern matches exactly once. The plan had a declared match count per row. No
  row needed one above one, and a second match is a number the row's author never read,
  so the fixer leaves it and the check fails.
- The script substitutes the captured group and exits 1. The `regen-doc-values` prek hook
  runs it, and the hook's file pattern covers each row's doc and source. A CI test runs
  the check for a commit that skipped the hook.
- The hook is its own, not a mode of `regen-doc-fragments`. The docs-refresh cron runs the
  fragment generator too, and a doc rewritten from the cron would sit outside the paths
  the cron stages, which parks `homelab-checkout-sync`. A second entry point makes that
  impossible. No row may name a path the cron stages, and a test holds that.
- A Renovate-managed value never gets a row, because Renovate runs no hook and a bump PR
  must not arrive red ([#4012](https://github.com/DanielH2018/server/issues/4012)).
  `version-as-fact` stays a warning.
- A row is added in the PR that fixes a value escape, and by G11. No lint guesses which
  numbers are values.

```diff
 scripts/docs/
+  doc_values.py       the entry point the hook runs
+    VALUES: rows of (doc, one capture regex, source, read)
+    stale(): fails a row that matches nothing, or twice
+    fix(): rewrites the captured value; main() exits 1 when a doc changed
 prek
+  regen-doc-values      files: each row's doc and source
-test_docs_quote_current_values.py     its two rows move into VALUES
+scripts/docs/tests/test_doc_values.py: every VALUES row matches the tree
+tests/repo/test_doc_values_hook.py: the hook's pattern covers every row
```

## Steps, as PRs

1. The registry and its check, with the two rows moved in and the old test file deleted.
   Done. A row holds four fields. The expected match count and the formatter arrive
   with the step that reads them.
2. The fixer and the prek hook, with the row for the count of paths the docs-refresh cron
   stages. Done. Two rows hold that count in `.claude/rules/generated-docs.md`: the
   cardinal that opens the list, and the ordinal in "adding a fifth". With a fifth path
   added to the cron's `git add`, the script rewrote both and exited 1, then exited 0.
3. The slice status fragment. Declined on 2026-11-14. The plan had the slice tables in
   `docs/target-architecture/design.md` generated from each slice file's status line.
   That page was archived on 2026-11-13, and its opening paragraph says a slice's
   `Status:` line records what merged and that the page does not repeat it. A generated
   fragment would add a second copy of the status to an archived page.

## Verification

Each step ships a row the check accepts and one it rejects. For step 2, change a derived
value and commit: the hook rewrites the literal.

## Risks and what is not verified

- A value written in a phrasing no row matches stays unchecked. The escape register shows
  these, and each one adds a row.
- The fixer corrects a count and not the list beside it. After the rewrite the doc reads
  "Five paths" above four names. The hook fails once so the author reads the sentence.

## Complexity delta

Adds one registry with its fixer, and one prek hook. Removes one test file.
