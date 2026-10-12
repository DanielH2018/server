# G4 — Drop the CI size test

Status: IN PROGRESS

PR 1a (issue #4694) moves the inline-budget arithmetic into `_inline_budget.py`.

Depends on: none. PR 2 waits on an **operator-only** ruling (step 0). The optional PR 3
lands after G2's PR 2.

Unblocks: none. [G5](g5-shrink-root-claude-md.md) uses this slice's report to measure the
root `CLAUDE.md`.

## Goal and exit criterion

After G4, a role `CLAUDE.md` has one size budget: the one `inject-nested-docs.py` applies
when it reads the doc into a session. A doc over that budget is delivered as its head plus
an outline of the sections it cut, and that behaviour already exists. CI no longer fails a
PR because a role doc grew. The ratchet table that let a doc stay over the ceiling by
recording its count goes too.

G4 reverses the per-role-doc ratchet that the operator chose in #2826. It also removes the
CI threshold that the accepted reason on #2584 relies on. PR 2 therefore opens only after
the operator rules on that reversal (step 0).

To keep size visible, the hook gains a `--report` mode. The report prints every instruction
file the hook serves, with its size, its margin under each of the hook's two limits, and
whether the hook delivers it whole or as a head. The arithmetic and the report walk live in
a hook library module, `_inline_budget.py` in `.claude/hooks/`, which the hook imports.

**Exit criterion.** The following command exits 0, and it exits non-zero on any error. It
prints the budgets it computes from the hook's own constants on its first line. It then
prints one row per tracked `CLAUDE.md` other than the root one and one row per
`.claude/rules/*.md`, tightest margin first. Its last line is the root `CLAUDE.md`.

```bash
uv run --no-sync python .claude/hooks/inject-nested-docs.py --report
```

In addition, `git grep -n -e _doc_size -e RoleDocNearCeiling -e OVER_CEILING` returns
nothing outside `docs/archive/`, `docs/reference/` and `docs/target-architecture/`. The
generated `docs/reference/backlog.md` quotes the closed issue #2584, so it is excluded.

A role doc grown past the report's own budget for it then passes
`uv run pytest ansible/tests/repo`, and the report shows it as `head` with a negative
character margin. `ansible/roles/setup/deploy_ui/CLAUDE.md` plus 500 characters is the
worked case in *Verification*.

## Baseline at `70591c307`

- **Two budgets exist, and one is derived from the other.**
    - The read-time budget: `.claude/hooks/inject-nested-docs.py:INLINE_MAX_CHARS` is 7,500
      and `INLINE_MAX_LINES` is 190. `_fits_inline` builds the block a whole doc would
      take: the header, a newline, `text.rstrip()` and a newline. The block may hold at
      most 7,272 characters and 188 newlines. Those are the two limits less `_PREAMBLE`,
      which is 228 characters and 2 newlines.
    - The CI budget: `MAX_CHARS` in `_doc_size.py` (`ansible/tests/`) derives 7,070 from the
      hook. It subtracts the 228-character preamble, a 200-character `HEADER_ALLOWANCE` and
      two newlines.
    - `_doc_size.py:hook_inline_max_chars` reads the `INLINE_MAX_CHARS = <int>` line from
      the hook file's text with the regex `_HOOK_BUDGET`. While `_doc_size.py` exists, that
      literal must stay in the hook file.
    - `test_the_live_hooks_budget_is_the_numbers_this_repo_was_measured_against`, in
      `test_role_claude_md.py`, pins 7,500, 7,272 and 7,070. It is the only check that pins
      7,500 and 7,272. The hook's comment says `INLINE_MAX_CHARS` is "Pinned by
      `test_inject_nested_docs.py`", but that module asserts only `INLINE_MAX_CHARS < 8000`.
    - Issue #3245 found twelve docs that passed the old ceiling while the hook truncated
      them, which is why the ceiling became derived.
- **A doc's budget depends on its trigger.** `_header(doc, trigger)` embeds the path that
  triggered the doc. I loaded the hook module and measured the header for
  `ansible/roles/setup/deploy_ui/CLAUDE.md`:
    - With the doc's own path as the trigger, the header is 106 characters, which leaves
      the doc 7,164.
    - With its directory, `ansible/roles/setup/deploy_ui`, the header is 96 characters,
      which leaves the doc 7,174.
    - The directory is the shortest trigger a `CLAUDE.md` can get. `docs_for` returns a
      `CLAUDE.md` only for a path at or below its directory, and `named_paths` resolves a
      directory to itself.
    - A rule's trigger is any path its `paths:` globs match (`docs_for`), so a rule has no
      fixed shortest header. The rule file's own path usually matches none of its globs.
- **The CI test weighs each doc at its longest trigger.**
  `test_role_claude_md.py:_role_docs_with_longest_trigger` pairs each role doc with the
  longest tracked path under its role. `test_role_claude_md.py:test_every_role_doc_is_inlined_whole`
  asks `_fits_inline` about each pair. Its docstring says: "It fails for a doc over the
  ceiling whatever a row's `over_ceiling` says: those lists waive the census, not the hook."
  `test_the_header_allowance_covers_every_role_docs_longest_trigger` holds every longest
  header within the 200-character `HEADER_ALLOWANCE`. A role doc of at most 7,070
  characters therefore fits at its longest trigger.
- **The head notice states the payload limit, not the doc's budget.** `_notice` prints
  "This file is N chars, over the 7,500-char hook budget". `_head_block`, and so the notice,
  runs whenever `_fits_inline` is false. Two kinds of doc would read a false sentence:
    - a doc between its own budget, about 7,165 characters for a role doc, and 7,500;
    - a doc cut by the line limit while under the character limit.

  No doc in the tree is either kind. The only heads are `.claude/rules/facts.md`, at 9,501
  characters, and `.claude/rules/python-layout.md`, at 10,448.
  `test_every_role_doc_is_inlined_whole` keeps every role doc out of that range. The
  tallest doc other than the root one is `python-layout.md`, at 156 lines.
- **Two heads in one command: the first takes the payload.**
    - `inject-nested-docs.py:build_context` keeps candidates in `named_paths` and `docs_for`
      order, and `docs_for` lists ancestor `CLAUDE.md` files before rules.
    - Whole docs render first. Only the first head's `head_floor` is reserved. The code
      comment reads: "Only the first head is reserved for: it spends nearly all it is
      given, so a second head in the same command defers whatever the whole docs leave."
    - The second head's `_head_block` then returns `None`, and `build_context` skips it
      without recording it, so a later command naming the path injects it.
    - `.claude/rules/facts.md` is in `READ_RULES`, and its globs include `**/CLAUDE.md`.
      `.claude/rules/python-layout.md` is an editing rule whose globs include
      `ansible/roles/**/tests/**`. Both are heads.
    - No role doc is a head, so in a command that names a role doc and one of these rules,
      the rule is the first head and gets the payload.
- **What the CI test enforces.** `test_role_claude_md.py` (`ansible/tests/repo/`, 495 lines)
  checks each role doc on three planes, `k8s`, `setup` and `containers`, for these things:
    - that it exists;
    - that it has at least `MIN_NON_BLANK_LINES` (8) non-blank lines;
    - for k8s roles, that it mentions `--tags` beside the role's name;
    - that it is at most `MAX_CHARS` long, unless the plane's `over_ceiling` entry records a
      count;
    - for k8s roles, a `RoleDocNearCeiling` warning above `WARN_FRACTION` (0.9).

  Its bottom half asks the hook's own `_fits_inline` about a doc of exactly `MAX_CHARS`, and
  about every real role doc.
- **Two operator rulings stand behind the ratchet.**
    - #2826, closed 2026-09-28, replaced the 400-line ceiling with "a 7,500-character
      ratchet on role docs". It came from an overturn review that day, split from #2808.
      The review chose a ratchet over a hard test, because a hard test "would fail 30 docs
      at once and force a mass cut of operative rules".
    - The CI ceiling's failure message, in `test_role_claude_md.py:_doc_problems`, cites
      #2126, #2826 and #2920.
    - #2584 was closed as accepted on 2026-09-25. Its reason in `docs/reference/backlog.md`
      reads: "Operator decision 2026-09-25: keep the pytest-only surface. ... CI already
      fails hard at MAX_LINES=400, the one threshold that must hold. A census page reaches
      nobody mid-edit". G4 removes the hard CI threshold that reason relies on. The G4
      report is an on-demand surface, the kind that reason says reaches nobody mid-edit.
    - `.claude/rules/python-layout.md` (*A check retires when its subject does*) allows a
      check to retire on one of two conditions: its subject is gone, or a census or schema
      check covers it. Neither holds for the size arm. Role-doc size still exists, and no
      other check covers it.
    - A ruling with a code site is recorded as a `# DECIDED` marker there (root
      `CLAUDE.md`, *Review & Memory Hygiene*). The `homelab-review` skill's step 3 tells
      each reviewer to run `grep -rn '# DECIDED'` before flagging. The docs-refresh cron
      regenerates `docs/reference/decisions.md` from those markers with
      `scripts/docs/reference/decisions.py`.
- **The ratchet reached its aim.**
    - #2826's body, on 2026-09-28: "30 of 93 CLAUDE.md files exceed 7,500 characters, by
      353,117 characters in total."
    - At `70591c307`, `git ls-files 'ansible/roles/*CLAUDE.md' | xargs wc -m` lists 86 role
      docs, and none is over 7,070. The largest is `ansible/roles/setup/deploy_ui/CLAUDE.md`
      at exactly 7,070. Of the 87 tracked `CLAUDE.md` files, only the root one, at 25,903
      characters, is over 7,500.
    - All three `over_ceiling` lists are empty. Of the 86 role docs, 21 sit within 200
      characters of 7,070, and 36 sit in the warning band.
- **What the events after #2826 show.**
    - Run 36485716039 on master, created at 21:23:00 UTC on 2026-09-28, two seconds after
      #2826 closed, concluded `failure`. `gh run view 36485716039 --log-failed` prints
      `claude-otel: CLAUDE.md is 24585 chars, past the 21032 its OVER_CEILING reason
      records`, from the k8s role-doc test of that day, since merged into
      `test_role_claude_md.py`. The recorded-count arm caught a doc that grew while #2826
      merged. The gate was correct, and the cost was a red master.
      [design.md](../design.md) cites the run among the red master runs this area caused.
    - #3245, closed 2026-10-02, was a bug in the CI copy of the budget. Deriving `MAX_CHARS`
      from the hook fixed it, and no later failure of that kind is on record.
    - In #2982, closed 2026-09-29, a session raised `initial_setup`'s recorded count from
      30,677 to 32,177 instead of taking the trim the entry named. That convention still
      exists, but its three tables are empty.
    - #4008, closed 2026-10-09, counted 3 PRs that went red on the size arm between
      2026-10-05 and 2026-10-09, the last with `claude_code: CLAUDE.md is 7109 chars, over
      the 7070-char ceiling`. Its fix added `test_role_claude_md.py` to `prek.toml`'s
      `static-ratchet-tests` entry, so the size arm fails at commit time instead of on the PR.
- **Nothing measures what a session does after a head injection.** The hook docstring's
  #2650 figures cover outline injections: sessions read the full doc after 23% of them.
  `instructions.log` records each injection with its trigger, and nothing records a later
  read of the injected file.
- **The hook swallows every error and exits 0.** Its docstring says "it always exits 0" and
  "swallows every error and exits 0". The `__main__` block handles `-h` and `--help` before
  `try: sys.exit(main()) except Exception: sys.exit(0)`, so only the help path can exit
  non-zero.
- **The hook already handles an oversized doc.** `_head_block` cuts at a heading, and the
  trailer names up to `TRAILER_MAX_HEADINGS` (40) headings it did not reach.
  `.claude/hooks/tests/test_inject_nested_docs_head.py` (141 lines) covers that path.
- **Four places tie the hook to the CI test.**
    - The hook's docstring says the head path retires only when the `over_ceiling` lists
      are empty.
    - `docs/claude-tooling.md` names `_doc_size.py:MAX_CHARS` in the hook's section.
    - It names it again in the facts-line paragraph, which G2's PR 4 deletes.
    - A docstring in `.claude/hooks/tests/test_facts_line.py` names
      `_doc_size.MAX_CHARS`. G2's PR 4 deletes that file.
- **`docs/claude-tooling.md` quotes the budget, and no check re-derives the quote.** Its
  hook section says "The payload is 7,500 chars" and "the 7,272 the preamble leaves".
  `ansible/tests/repo/test_docs_quote_current_values.py:ROWS` has one row for that page,
  for `RECENT_RESTART_SECONDS`.
- **The hook is 7 lines under its module cap.**
    - `wc -l .claude/hooks/inject-nested-docs.py` prints 593.
      `ansible/tests/_ratchet.py:NON_TEST_CAP` is 600, and
      `ansible/tests/repo/module_length_allowlist.txt` has no entry for the hook.
    - `tracked_python_files` in `ansible/tests/_ratchet_census.py` counts every tracked
      `.py`, hook files included.
    - The `DECIDED` header of `module_length_allowlist.txt` (#3661) says to split a module
      over the cap when each piece reads alone, and to use a `conjoined:` reason only when
      every split leaves a piece conjoined with its parent.
    - `.claude/hooks/tests/test_inject_nested_docs.py` is 456 lines against
      `_ratchet.py:TEST_CAP`, which is 500.
- **The hook already imports library siblings by bare name.** It imports `_facts_line` and
  `_hook_common`. Each opens with a `# gen-hooks: library` block and a `reason:` line, and
  `scripts/dev/gen_hook_settings.py` rejects a hook file that declares no block. `rule_globs`
  and `_header` use no module constant. `rule_globs` needs only `re` and `_QUOTES`.
  `.claude/hooks/tests/` reaches `rule_globs`, `_fits_inline` and the constants through the
  hook module, and no test there patches a hook constant.
- **The instructions log does not record the delivery form.** `inject-nested-docs.py:context`
  appends ` trigger=<path>` and, for a subagent, an agent field to each row. Nothing records
  whether the doc went whole or as a head. `evals/ablate.py`, `evals/ranking.py` and
  `scripts/lib/facts/report.py` read that log. `build_context` returns the chosen docs as
  `(doc, trigger)` pairs, and many call sites in `.claude/hooks/` unpack that shape.
- **The static-ratchet prek hook runs this test.** `prek.toml`'s `static-ratchet-tests`
  entry names `test_role_claude_md.py`, and its comment cites the role-doc size check as one
  of the reasons the hook exists (#4008).

## Design

The read-time budget is the one that decides what a session actually reads. G4 deletes the
copy of that budget in CI, and with it the recorded-count table that let a doc stay over the
ceiling. A doc that grows past the budget is not an error. It is a doc read as a head, and
the report names it.

**The case the operator weighs in step 0.** The ratchet did its job. 30 of 93 `CLAUDE.md`
files were over 7,500 characters on 2026-09-28, and no role doc is over 7,070 at
`70591c307`. The events after #2826 do not show the gate failing:

- Run 36485716039 was the gate catching a real overrun. The red master came from a merge
  race, not from a wrong verdict.
- #3245 was a bug, fixed on 2026-10-02 by deriving the ceiling.
- #2982 was a convention used the wrong way, and its tables are empty.

The case for G4 rests on what the gate costs at present, and on what a head costs instead:

- **The gate's cost.** 21 role docs sit within 200 characters of the ceiling, so the next
  addition to any of them needs a trim in the same commit. #4008 counted 3 PRs red on the
  size arm in 5 days. Since #4008, `static-ratchet-tests` runs the check at commit time, so
  that cost arrives as a failed commit and a trim in the middle of unrelated work, not as a
  red PR.
- **A head's cost.** A doc over its budget is read as its operative top plus an outline of
  the rest. How often a session then reads the rest is unmeasured. The 23% figure from
  #2650 covers outline injections, not heads.
- **What G4 gives up.** After G4, no surface reaches an author mid-edit before a doc
  crosses its budget. That is the concern #2584's reason names. After the doc crosses, the
  head notice reaches every session that reads the doc through Bash, and the report names
  the doc on demand.
- **A side effect on rule injection.** A role doc that grows into a head becomes the first
  head in a command that also selects a head rule, because `docs_for` lists the `CLAUDE.md`
  first. The rule's head then defers to a later command. A Bash read of a grown role
  `CLAUDE.md` defers `facts.md`, and a first write under a grown role's `tests/` lands
  without `python-layout.md`. G4 accepts this rather than reordering `build_context`, and
  step 0 asks the operator to accept it too.
- **Reversibility.** PR 2 gets harder to revert every week it is live. *Rollback* gives the
  measure.

That trade is the operator's call, so step 0 asks for it.

**The library module.** PR 1a moves the arithmetic that needs no hook constant into
`_inline_budget.py` in `.claude/hooks/`, a `# gen-hooks: library` module the hook imports by
bare name:

- `header(doc, trigger)`, moved from the hook's `_header`;
- `rule_globs(rule_text)`, moved from the hook;
- `inline_margins(text, header, max_chars, max_lines)`, which returns the characters and
  newlines left under the two limits it is given. It builds the same block `_fits_inline`
  builds.

The hook keeps `INLINE_MAX_CHARS`, `INLINE_MAX_LINES` and `_PREAMBLE`, and passes the limits
in. That keeps the literal `_doc_size.py:hook_inline_max_chars` reads in the hook file.
`_fits_inline` becomes "both margins are at least 0" for the limits less `_PREAMBLE`. The
hook imports `header` as `_header`, and `rule_globs` under its own name. The call sites, and
the tests that reach those names through the hook module, do not change. Each piece reads
alone: the library is the payload arithmetic and the rule-glob parser, and the hook is the
delivery. The hook drops to about 560 lines, so no allowlist line is needed.

**The report.** PR 1b adds `report_rows(root, max_chars, max_lines)` and a printing function
to the library. The hook's `__main__` dispatches `--report [--root DIR]` to them with its own
constants. The report walks `git ls-files` for `*CLAUDE.md` and `.claude/rules/*.md`. For
each doc, it builds the header from one trigger:

- **A `CLAUDE.md`** uses its own directory. That is the shortest header the hook can build
  for it, so `head` means the doc is a head on every trigger.
- **A rule** uses the shortest tracked path that its `paths:` globs match. The report tests
  each path with the same `PurePosixPath.full_match` call that `docs_for` makes. That
  trigger is representative, not a bound, because a session can name a shorter directory
  or an untracked path.
- **A rule whose globs match no tracked path** gets `form` `none`, since the hook never
  injects it. The row stays visible instead of dropping out of the report.

The report then measures each doc with `inline_margins`, the same computation `_fits_inline`
uses. The report, the notice and the delivery therefore share one computation.

The report prints these columns:

| Column | Meaning |
|---|---|
| `chars` | the doc's length in code points |
| `lines` | the doc's line count |
| `form` | `whole`, `head` or `none` |
| `chars_left` | characters left under the character limit, negative when over |
| `lines_left` | newlines left under the line limit, negative when over |
| `doc` | the repo-relative path |

- `form` is `head` when either margin is negative.
- Rows sort by the tighter of the two margins. Each margin is taken as a fraction of its
  limit, 7,272 characters or 188 newlines, so heads come first.
- A `whole` row is marked `near` when `chars_left` is under 200 or `lines_left` is under 10.
  The character mark covers a deeper trigger, which lengthens the header. The line mark
  covers growth only, because the header is always one line.
- The first line prints the budgets the report computed from the hook's constants: the
  7,500-character and 190-line payload, and the 7,272 characters and 188 newlines left after
  the preamble.
- The root `CLAUDE.md` is printed on a separate last line. The harness loads it whole at
  session start, and the hook never injects it. That line shows its line count against
  the target Anthropic recommends, under 200 lines.
- The mode reads no stdin and writes nothing.

**The report fails loudly.** The `__main__` block dispatches `--report` before its
`try`/`except`, as it does `-h`, so the hook's error swallow does not cover the report.
`report_rows` runs `git ls-files` with `check=True`, and it raises when the walk finds no
`CLAUDE.md` other than the root one. Any exception ends the process with a traceback and a
non-zero status. PR 1b rewrites the docstring's two "exits 0" sentences to say they hold for
the hook path, not for `--report`.

The owner of the arithmetic is the hook and its library, so the report and the delivery
cannot disagree. That disagreement is what #3245 found between `_doc_size.py` and the hook.

**The tooling doc stops quoting the budget.** PR 1b replaces "The payload is 7,500 chars"
and "the 7,272 the preamble leaves" in `docs/claude-tooling.md` with a pointer to the
report's first line, which computes both. A row in `test_docs_quote_current_values.py:ROWS`
was the alternative. Its readers take a bare integer literal by regex, and 7,272 is
`INLINE_MAX_CHARS - len(_PREAMBLE)`, which no regex reads. The same PR's report paragraph
says that two heads in one command defer the second.

**The head notice states the doc's own budget.** `_notice` takes the header as well. It
prints the doc's characters and lines against the budget that `inline_margins` leaves the
doc beside that header. `_head_overhead` and `_head_block` already hold the header. The
notice's length already counts toward the head arithmetic, so the head stays inside the
payload.

**The budget pin moves to the hook's tests.** The test that pins 7,500 and 7,272 leaves
with the size arm. PR 2 adds the same pin to `.claude/hooks/tests/test_inject_nested_docs.py`,
beside the `< 8000` assertion:

- `INLINE_MAX_CHARS` is 7,500 and `INLINE_MAX_LINES` is 190.
- The limits less `_PREAMBLE` are 7,272 characters and 188 newlines.

The hook comment "Pinned by `test_inject_nested_docs.py`" then becomes true.

**The ruling is written where the gate stood.** PR 2 adds a `# DECIDED` comment block to
`test_role_claude_md.py`, above `_doc_problems`. Its first sentence says that role-doc size
has no CI gate, by the operator's ruling on #2826. The block names the ruling's issue and
says that `.claude/hooks/inject-nested-docs.py --report` prints each doc's margin. A reviewer
who runs the `homelab-review` grep finds it before flagging the missing gate.

**What stays in `test_role_claude_md.py`:**

- the `PLANES` rows, without the `warn_band` and `over_ceiling` fields;
- `MIN_NON_BLANK_LINES` and the `--tags` mention;
- `test_census_finds_its_named_members` and its red half;
- `test_every_role_has_an_adequate_claude_md`, without the size arm;
- the fixture tests for a missing, short or untagged doc;
- `test_the_planes_cover_every_tracked_role_doc`. Its census becomes a plain `git ls-files`
  list of role docs, without the longest-trigger pairing.

**The optional third PR makes the report read-weighted.**

- The hook appends ` form=head` to the instructions-log row of a doc it delivered as a head.
- `context` finds those docs from the rendered block that `build_context` returns with each
  doc, which #4653 added to size the row's `bytes=` field. A head block carries the
  `_notice` line, and a whole-doc block never does.
- `fact_status.py report` ranks the docs read as a head by how often they were read.
- That turns "which docs are too long" into "which long docs are read most." That is the
  order a trim should follow.
- G2's PR 2 rewrites `scripts/lib/facts/report.py` and the `report` bullet in
  `.claude/rules/facts.md`. PR 3 edits both, so it lands after G2's PR 2 and builds on that
  rewrite. If the operator rejects G2's ADR, PR 3 builds on `report.py` as it stands.

```diff
 .claude/hooks/
+  _inline_budget.py (gen-hooks library: header, rule_globs, inline_margins,
+                     report_rows and the report printer)
   inject-nested-docs.py
-    rule_globs and _header bodies (moved; imported back under the same names)
+    --report [--root DIR], dispatched before the try/except
     _fits_inline: inline_margins with the hook's own limits
     _notice: states the doc's own budget for its header, not INLINE_MAX_CHARS
     docstring: exit-status sentences; head path no longer tied to a CI list
 .claude/hooks/tests/
+  test_inject_nested_docs_report.py (the margin, report and exit-status tests)
   test_inject_nested_docs.py        + the budget pin (7,500 / 190 / 7,272 / 188)
   test_inject_nested_docs_head.py   + the notice pair
 ansible/tests/
-  _doc_size.py
   repo/test_role_claude_md.py
-    MAX_CHARS ceiling arm, over_ceiling, warn_band, RoleDocNearCeiling,
-    the ceiling fixtures, the hook-arithmetic tests
+    a # DECIDED block: no CI size gate, by operator ruling
 pyproject.toml
-  "always::_doc_size.RoleDocNearCeiling"
 docs/claude-tooling.md
-  the 7,500 and 7,272 figures; both _doc_size.py:MAX_CHARS sentences
+  the report paragraph, and a pointer to its budget line
 (optional, after G2's PR 2)
   inject-nested-docs.py:context   + " form=head" on a head row
   scripts/lib/facts/report.py     + docs read as a head, ranked by reads
```

**Two-writer rule (C4).** Not applicable in its literal sense, since no ownership moves.
The analogous property holds after G4: one module pair, the hook and its library, owns the
budget, and the report reads the hook's own constants.

## Steps, as PRs

0. **Operator-only: rule on retiring the ratchet** (no code).
    - The operator decides whether the per-role-doc ratchet chosen in #2826 retires, on the
      evidence in *Baseline* and the case in *Design*. That evidence includes the
      counter-evidence: the ratchet took role docs from 30 over 7,500 to none over 7,070.
    - A yes also accepts two consequences. A role doc that grows into a head defers a head
      rule in the same command (*Design*). PR 2 becomes effectively one-way once docs grow
      past 7,070, because its revert must trim them (*Rollback*).
    - On a yes, the operator records the ruling on #2826 and #2584. The accepted reason on
      #2584 then no longer rests on a CI threshold. PR 2's `# DECIDED` block links that
      record.
    - On a no, PR 2 does not open. PRs 1a and 1b stay as a report beside the CI test.
    - PRs 1a and 1b do not wait on this step.
1. **PR 1a: move the arithmetic into a library** (about 100 lines, about 55 of them moved).
   No behaviour changes.
    - Add `_inline_budget.py` to `.claude/hooks/`, opening with `# gen-hooks: library` and
      a `reason:` line that names the hook as its importer.
    - Move `rule_globs` and `_header`'s body into it, and add `inline_margins`.
    - Import them into the hook under the names the hook uses. Make `_fits_inline` call
      `inline_margins` with `INLINE_MAX_CHARS - len(_PREAMBLE)` and
      `INLINE_MAX_LINES - _PREAMBLE.count("\n")`.
    - Keep `INLINE_MAX_CHARS`, `INLINE_MAX_LINES` and `_PREAMBLE` in the hook, for
      `_doc_size.py:hook_inline_max_chars`.
    - Add the `inline_margins` pair to a new `test_inject_nested_docs_report.py` in
      `.claude/hooks/tests/`: a block at exactly 7,272 characters and 188 newlines has both
      margins at 0, and one character more gives a character margin of -1.
    - The hook ends near 560 lines. The existing hook tests run unchanged, which is the
      check that nothing moved in behaviour.
2. **PR 1b: the report** (about 170 lines, about half of them tests).
    - Add `report_rows` and the printing function to `_inline_budget.py`.
    - Add `--report` and `--root` handling to the hook's `__main__` block, before the
      `try`, beside `-h`. Name `--report` in the docstring's usage paragraph, and rewrite
      the docstring's two "exits 0" sentences to exclude it.
    - Add the report tests to `test_inject_nested_docs_report.py`.
    - Add the report's paragraph to the hook's section of `docs/claude-tooling.md`. It
      states that two heads in one command defer the second. Replace the 7,500 and 7,272
      figures in that section with a pointer to the report's first line.
    - The size test still stands, so the report starts life beside it. Every role doc then
      reads `whole`, and `.claude/rules/python-layout.md` and `.claude/rules/facts.md` read
      `head`. That gives a known first output to check.
    - The hook ends near 570 lines.
3. **PR 2: drop the CI size test** (about 70 lines added, about 330 deleted). This PR opens
   only after step 0 rules yes.
    - Delete `_doc_size.py` from `ansible/tests/`.
    - Delete the `"always::_doc_size.RoleDocNearCeiling"` entry, and the comment above it,
      from `pyproject.toml`.
    - Remove the size arms and tests listed in *Design* from `test_role_claude_md.py`, and
      rewrite its module docstring. Add the `# DECIDED` block that *Design* describes.
    - Add the budget pin to `.claude/hooks/tests/test_inject_nested_docs.py`, as *Design*
      describes.
    - Change `_notice` to state the doc's own budget. Add its paired tests to
      `.claude/hooks/tests/test_inject_nested_docs_head.py`.
    - Rewrite the hook docstring sentence that ties the head path to `over_ceiling`.
    - Rewrite both `_doc_size.py:MAX_CHARS` sentences in `docs/claude-tooling.md`, whether
      or not G2's PR 4 has landed:
        - In the hook's section, "A role doc is held under that effective budget by
          `ansible/tests/_doc_size.py:MAX_CHARS`" becomes a pointer to `--report`, which
          prints each doc's budget.
        - In the facts-line paragraph, "so `ansible/tests/_doc_size.py:MAX_CHARS` still
          promises a whole doc" becomes "so the line never turns a whole doc into a head".
          G2's PR 4 still deletes the whole paragraph later.
    - Rewrite the docstring in `.claude/hooks/tests/test_facts_line.py` that names
      `_doc_size.MAX_CHARS`, unless G2's PR 4 has already deleted the file.
    - In `prek.toml`'s `static-ratchet-tests` comment, drop the role-doc size check from the
      list of reasons. Keep the entry: the module still runs the existence and tag checks.
    - In the PR body, state that this retirement meets neither of the two conditions in
      `.claude/rules/python-layout.md` (*A check retires when its subject does*). State
      that it rests on the operator ruling from step 0, and link that ruling.
    - Do not edit `docs/reference/decisions.md`. The docs-refresh cron regenerates it and
      picks up the `# DECIDED` block.
4. **PR 3 (optional): a read-weighted report** (about 100 lines, half of them tests). This PR
   lands after G2's PR 2.
    - Add ` form=head` to the row tail in `inject-nested-docs.py:context`, found as *Design*
      describes. `build_context` and its callers do not change.
    - Add the ranking to `scripts/lib/facts/report.py`.
    - Check that `evals/ablate.py` and `evals/ranking.py` parse the row tail as `key=value`
      pairs, and extend their parsers if not.
    - The hook ends near 580 lines.

PRs 1a and 1b land before PR 2. That way the size stays visible on the day its gate goes.
The PR numbers 2 and 3 are the ones G2's plan cites.

Every PR that touches the hook runs `test_module_length_ratchet.py`. If a PR would take the
hook past 600 lines, it also moves `_outline` and `_head_lines` into the library. Both are
pure functions of their arguments. The PR does not add an allowlist line.

## Verification

Exit criterion:

```bash
uv run --no-sync python .claude/hooks/inject-nested-docs.py --report; echo "exit=$?"
git grep -n -e _doc_size -e RoleDocNearCeiling -e OVER_CEILING \
  -- ':!docs/archive/' ':!docs/reference/' ':!docs/target-architecture/'
git grep -n -e '7,272' -e '7,500 chars' -- docs/claude-tooling.md
uv run pytest -q -p no:cacheprovider ansible/tests/repo/test_role_claude_md.py \
  ansible/tests/repo/test_module_length_ratchet.py .claude/hooks/tests
```

The report command is run without a pipe, so `exit=0` is the report's own status. Both
`git grep` commands print nothing. The tests pass.

Proof that the gate is gone: in a scratch worktree, append 500 characters of prose to
`ansible/roles/setup/deploy_ui/CLAUDE.md`, without committing. The doc then holds about
7,570 characters, past the 7,174 its directory trigger leaves it. Run:

```bash
uv run pytest -q -p no:cacheprovider ansible/tests/repo/test_role_claude_md.py
uv run --no-sync python .claude/hooks/inject-nested-docs.py --report | grep deploy_ui
```

The test passes, and the report shows the doc as `head` with a negative `chars_left`.
Restore the file with `git restore`.

Red-proof for the margin helper (PR 1a), in the report test module: a block at both limits
has margins of 0 and 0, and one more character gives -1.

Red-proof for the report (PR 1b), as paired tests in the report test module, using a
fixture checkout:

- `..._doc_over_the_char_limit_reports_head`: a doc of `INLINE_MAX_CHARS` characters is
  reported as `head`, with a negative `chars_left`.
- `..._doc_under_the_budget_reports_whole`: a short doc is reported as `whole`.
- `..._doc_over_the_line_limit_reports_head`: a doc of 200 short lines is reported as
  `head`, with a negative `lines_left` and a positive `chars_left`.
- `..._doc_at_the_line_limit_reports_whole`: a doc whose block has exactly 188 newlines is
  reported as `whole`.
- `..._margin_under_200_chars_is_marked_near` and `..._margin_under_10_lines_is_marked_near`,
  each beside a doc just outside the mark that is not marked.
- `..._a_rule_matching_no_tracked_path_reports_none`.
- `..._root_claude_md_is_reported_by_lines_not_form`: the root doc appears only on the last
  line, with its line count.
- `..._report_on_a_fixture_checkout_exits_0` and
  `..._report_outside_a_git_checkout_exits_non_zero`: the hook run as a subprocess with
  `--report --root` at the fixture exits 0, and with `--root` at a directory that is not a
  git checkout exits non-zero. Without the dispatch before the `try`, the second test fails,
  because the swallow turns the error into exit 0.
- `..._report_with_no_claude_md_raises`: a fixture checkout holding only the root
  `CLAUDE.md` exits non-zero rather than printing an empty table.
- Non-vacuity, on the live tree: the report lists at least 80 `CLAUDE.md` rows. It must
  include `ansible/roles/k8s/traefik/CLAUDE.md` and `.claude/rules/python-layout.md`.

Red-proof for PR 2's notice, in the head test module:

- `..._notice_for_a_doc_just_over_its_budget_states_that_budget`: a doc 50 characters over
  its own budget, and under `INLINE_MAX_CHARS`, gets a notice whose number is that budget.
  Today's `_notice` fails this test, because it prints 7,500.
- `..._notice_for_a_line_limited_doc_states_the_line_budget`: a doc cut by the line limit
  gets a notice that names its line budget.

Red-proof for the moved pin: an edit that adds a word to `_PREAMBLE` fails it until the
pinned 7,272 changes.

For PR 3, if built: a head injection writes ` form=head` into the fixture log, a whole
injection does not, and the ranking orders two fixture docs by read count.

## Rollback

- **PR 1a** is a revert, made after any revert of PR 1b, which imports from the library.
  It moves `rule_globs` and `_header` back into the hook, which returns to 593 lines.
- **PR 1b** is a revert, made after any revert of PR 2. It removes the CLI mode, its tests
  and the report paragraph, and it restores the two figures in `docs/claude-tooling.md`.
- **PR 2** is a revert, and its cost grows every week PR 2 is live.
    - The revert restores `test_every_role_doc_is_inlined_whole`. That test weighs each
      role doc at its longest tracked trigger and fails a doc that does not fit, whatever
      `over_ceiling` says. The revert PR must therefore trim every role doc that fails
      `_fits_inline` at its longest trigger.
    - An `over_ceiling` entry helps only a doc over 7,070 that still fits at its longest
      trigger.
    - The measure of the revert's cost is the count of role docs over 7,070 characters,
      from `git ls-files 'ansible/roles/*CLAUDE.md' | xargs wc -m`. The docs that must be
      trimmed are a subset of that list, while every longest header stays within the
      200-character `HEADER_ALLOWANCE` that the restored allowance test checks.
    - The report understates the cost. Its `head` and `near` rows weigh a doc at its
      shortest trigger, not its longest.
    - The revert restores the old `_notice` too, which is false for a doc in the band PR 2
      opened.
    - Once docs grow, PR 2 is one-way in practice. Step 0 asks the operator to accept that.
- **PR 3** is a revert. Rows already written with ` form=head` stay in the log. Every
  reader tolerates the extra key, which PR 3's check establishes.
- **A partial failure.** If PR 2's `pyproject.toml` edit and the `_doc_size.py` deletion were
  split, pytest would fail at configuration time on every run, because pytest resolves the
  `filterwarnings` category when it parses the list. They are one commit for that reason.

## Risks

Each risk is checked against `docs/failure-classes.md`.

- **The operator rejects the reversal.** Then PR 2 never opens, and the exit criterion is
  not reached. PRs 1a and 1b still add a report that reads the hook's own arithmetic. Step
  0 comes before PR 2 so that no work is spent on a PR the operator would close.
- **Role docs grow, and sessions read more of them as heads.** The head path delivers the
  operative top of the doc and names every cut section. The report names each doc that
  crossed. Nothing forces a trim any more. How often a session reads the rest of a head is
  unmeasured. The trade G4 makes is against the gate's cost at present: #4008 counted 3 red
  PRs in 5 days, and since #4008 that cost is a failed commit and a forced trim, not a red
  PR.
- **A grown role doc defers a head rule.** This is a behaviour change G4 causes.
  `build_context` reserves the payload for the first head, and a `CLAUDE.md` comes before
  the rules. A role doc that becomes a head therefore defers `facts.md` on a Bash read of
  that doc, and `python-layout.md` on a first write under that role's `tests/`. The rule
  arrives on a later command that names a matching path. G4 accepts this in step 0 rather
  than reordering `build_context`, and PR 1b's report paragraph documents it.
- **PR 2 becomes hard to revert.** Each doc that grows past 7,070 adds a trim to the revert
  PR. *Rollback* names the measure, and step 0 asks the operator to accept it.
- **A head's first line could state a false budget.** After PR 2, role docs can reach the
  band between their own budget and 7,500. PR 2's `_notice` states each doc's own budget,
  so the sentence stays true.
- **Failure class 2, a check that observes a proxy.** Characters are a proxy for "a session
  reads all of it". The hook measures the real property at delivery, so the report reads
  the hook rather than restating its numbers.
- **Failure class 3, an empty result read as clean.** A report that silently walks nothing
  would print an empty table, and the hook's error swallow would turn a crash into exit 0.
  The report dispatches before the swallow, raises on an empty walk, and the exit criterion
  reads its status without a pipe. The non-vacuity test names two members it must find.
- **Failure class 4, a selector that drifts from the hazard.** The report finds its docs
  with `git ls-files` patterns, and a rule's trigger comes from its `paths:` globs. The
  non-vacuity test fails when a pattern stops matching. A rule whose globs stop matching
  any tracked path prints as `none` instead of disappearing.
- **Failure class 8, prose that asserts facts nothing re-derives.** `docs/claude-tooling.md`
  states 7,500 and 7,272, and no check reads that page for them. PR 1b replaces both figures
  with a pointer to the report's first line, which computes them. PR 2 moves the pin on the
  hook's constants into the hook's own tests rather than dropping it.
- **A later review re-flags the missing gate.** PR 2's `# DECIDED` block sits where the
  gate stood, and the `homelab-review` grep finds it. `.claude/rules/python-layout.md` is
  not amended. It is a head at 10,448 characters, its retirement section is past the cut,
  and a one-off ruling belongs at its code site.
- **The hook grows back toward its module cap.** It ends near 560 lines after PR 1a, 570
  after PR 1b and PR 2, and 580 after PR 3. A PR that would pass 600 moves `_outline` and
  `_head_lines` into the library. G2's PR 4 deletes the facts-line code from the hook,
  which lowers the count.
- **The floor and a member meet other areas' deletions.** The floor of 80 `CLAUDE.md` rows
  and the `ansible/roles/k8s/traefik/CLAUDE.md` member depend on role docs that R4, D6 and
  H7 delete. The slice that deletes them lowers the floor and swaps the member in the same
  PR. `.claude/rules/python-layout.md` is the member that survives every area.
- **The prek hook's reason weakens.** `static-ratchet-tests` was justified partly by
  role-doc size failures (#4008). Its other seven modules still justify it, so PR 2 edits
  only the comment.
- **Interplay with Area 5.** H7 deletes most role trees, and with them most role docs and
  most of `test_role_claude_md.py`'s subject. G4 shrinks that test first, so H7 has less to
  delete.

## Tests and docs that change in the same PR

- **PR 1a:**
    - `_inline_budget.py` in `.claude/hooks/`, with its `# gen-hooks: library` block;
    - the `inline_margins` pair, in the new `test_inject_nested_docs_report.py` in
      `.claude/hooks/tests/`.
- **PR 1b:**
    - the report and exit-status tests, in `test_inject_nested_docs_report.py`;
    - the usage paragraph and the two "exits 0" sentences of `inject-nested-docs.py`'s
      docstring;
    - the report's paragraph in `docs/claude-tooling.md`, and the pointer that replaces the
      7,500 and 7,272 figures.
- **PR 2:**
    - `test_role_claude_md.py`, its docstring and its `# DECIDED` block;
    - `pyproject.toml`'s `filterwarnings`;
    - `inject-nested-docs.py`'s `_notice` and docstring;
    - the notice pair in `test_inject_nested_docs_head.py`;
    - the budget pin in `test_inject_nested_docs.py`;
    - `docs/claude-tooling.md`: the `_doc_size.py:MAX_CHARS` sentence in the hook's
      section, and the one in the facts-line paragraph;
    - the docstring in `.claude/hooks/tests/test_facts_line.py`, if G2 has not deleted it;
    - the `static-ratchet-tests` comment in `prek.toml`.
- **PR 3:**
    - the log and ranking tests;
    - the `report` bullet in `.claude/rules/facts.md`;
    - the instructions-log paragraph in `docs/claude-tooling.md`.

## Complexity delta

- **Removed:** about 330 lines.
    - `_doc_size.py`, 164 lines;
    - about 170 lines of `test_role_claude_md.py`;
    - one `filterwarnings` exception;
    - the `over_ceiling` recorded-count convention;
    - two budget figures quoted in `docs/claude-tooling.md`.
- **Added:**
    - one hook library module of about 140 lines, of which about 35 move from the hook.
      It holds `inline_margins` and the `--report` walk;
    - the `--report` dispatch and the `_notice` change in the hook, about 15 lines;
    - about 110 lines of tests, including the pin, the exit-status pair and the notice pair;
    - one `# DECIDED` block;
    - optionally, a log field and a ranking of about 100 lines.
- **Not added:** a module-length allowlist line. The hook stays under 600 lines.

## Open questions

- **Should the report be a generated `docs/reference/` page?** That would put it on the docs
  site without anyone running a command. Resolution: not in G4. A generator under
  `scripts/docs/` would import `_inline_budget.py` from `.claude/hooks/` through a
  `sys.path` insert. That is a bare-name import, unlike the by-path load of the hyphenated
  hook that `_doc_size.py` needed, but it still couples the docs generator to the hook
  tree. Revisit at G5's verification: if G5 is the report's only user, promote the report
  to a generated page that runs the hook's `--report` rather than importing it.
- **Should the report weigh a `CLAUDE.md` against its longest trigger instead?** The
  longest trigger would make `whole` mean "whole on every trigger", and it is the measure a
  revert of PR 2 faces. Resolution: keep the shortest. `head` then means "head on every
  trigger", which is the claim a trim needs, and the `near` mark flags the band between the
  two. *Rollback* names the `wc -m` count as the revert's measure instead. Revisit if
  sessions report heads on docs the report lists as `whole`.
- **How does the ruling reach `docs/reference/backlog.md`?** That page shows #2584 with its
  accepted reason, and `scripts/docs/reference/backlog.py` takes the reason from the
  issue's close. A stale reason there would invite a later review to re-flag G4.
  Resolution: in step 0, the operator chooses how the ruling is recorded, and PR 2 checks
  that the regenerated page shows it.
- **Does the slice index need a G4 edge?** The index in `contracts.md` gives G4 no
  dependency, and PRs 1a, 1b and 2 need none. The optional PR 3 follows G2's PR 2, because
  both edit `scripts/lib/facts/report.py` and the `report` bullet in `.claude/rules/facts.md`.
  Resolution: this revision reports the edge to the contracts owner as "G2 (PR 3 only)".
  Until the row changes, this plan carries the ordering.

## Review log

### Round 1 (per-slice adversarial review)

Findings raised, and what this revision did with each. All eight were fixed, and none was
refuted. Each was checked against the tree at `70591c307` before the fix.

- **G4-R1-01 (high): the exit criterion fails if G4 lands before G2's PR 4.** Fixed.
  `git grep` printed `docs/claude-tooling.md`'s facts-line sentence naming
  `_doc_size.py:MAX_CHARS`. PR 2 now rewrites that sentence whether or not G2 has landed.
  The conditional is gone from *Steps* and *Tests and docs*.
- **G4-R1-02 (high): the plan reverses two operator rulings without naming them.** Fixed.
  *Baseline* names #2826, the ratchet the operator chose on 2026-09-28. It also names #2584,
  closed as accepted on 2026-09-25, and the python-layout retirement rule. *Design* states
  the evidence that came after #2826: #2982, #3245 and run 36485716039. Step 0 is an
  **operator-only** ruling before PR 2. PR 2's body says the retirement meets neither
  python-layout condition. *Design* states that no surface reaches an author mid-edit after
  G4.
- **G4-R1-03 (medium): PR 1 pushes the hook past the 600-line cap.** Fixed. The hook is 593
  lines, and an allowlist entry must match its file exactly. PR 1 adds a `# conjoined:`
  line, and PRs 2 and 3 restate the max when the hook grows. The report tests go in a new
  module, because `test_inject_nested_docs.py` is 456 lines against a cap of 500.
- **G4-R1-04 (medium): two false sizing claims.** Fixed. The header for `deploy_ui` measured
  106 characters with the doc's path as trigger and 96 with its directory. A `CLAUDE.md`
  row now uses the directory, which is the shortest trigger. A rule row uses the shortest
  tracked path its globs match, described as a representative trigger. The exit criterion
  is restated for a doc past the report's own budget.
- **G4-R1-05 (medium): the head notice states a wrong number after G4.** Fixed. PR 2 makes
  `_notice` state the doc's own budget for its header, with a paired test that today's
  notice fails.
- **G4-R1-06 (medium): `margin` ignores the line limit.** Fixed. The report prints
  `chars_left` and `lines_left`, sorts by the tighter one and marks `near` on either. Paired
  line-limit fixtures are added, and the open question that the finding contradicted is
  replaced.
- **G4-R1-07 (medium): PR 3 overlaps G2's PR 2.** Fixed. PR 3 lands after G2's PR 2, which
  owns the `report.py` rewrite. `context` recomputes `head_floor` for each chosen doc, so
  the shape of the chosen pairs does not change. The edge is reported to G2 and to the
  contracts owner, since neither file is this slice's to edit.
- **G4-R1-08 (medium): Risks miss failure classes 8 and 4.** Fixed.
  `docs/failure-classes.md` confirms both class numbers. PR 2 moves the 7,500 and 7,272 pin
  into `test_inject_nested_docs.py`, and the report prints its computed budgets. *Risks*
  names both classes.

### Round 2 (per-slice adversarial review)

Seven findings were raised. All seven were fixed, two of them in part as noted, and none
was refuted. Each was checked against the tree at `70591c307` before the fix.

- **G4-R2-01 (high): PR 2's rollback is contradicted by the test the revert restores.**
  Fixed. `test_role_claude_md.py:test_every_role_doc_is_inlined_whole` pairs each doc with
  `_role_docs_with_longest_trigger`, and its docstring says the `over_ceiling` lists "waive
  the census, not the hook". *Rollback* now says the revert must trim every role doc that
  fails `_fits_inline` at its longest trigger. It says an `over_ceiling` entry helps only a
  doc that still fits there. The measure is the count of role docs over 7,070 from
  `git ls-files 'ansible/roles/*CLAUDE.md' | xargs wc -m`. That list bounds the trim set,
  because `HEADER_ALLOWANCE` covers every longest header. *Rollback* says the report's
  shortest-trigger rows understate the cost. Step 0 and *Risks* say PR 2 is one-way in
  practice once docs grow.
- **G4-R2-02 (high): step 0's case mischaracterizes its evidence and omits the
  counter-evidence.** Fixed.
    - `gh run view 36485716039 --log-failed` printed `claude-otel: CLAUDE.md is 24585
      chars, past the 21032 its OVER_CEILING reason records`. *Baseline* and *Design* now
      call the run a correct verdict and a red master from a merge race.
    - #3245 is described as a fixed bug, and #2982 as a convention whose tables are empty.
    - #2826's body reads "30 of 93 CLAUDE.md files exceed 7,500 characters". `wc -m` at
      `70591c307` finds none of 86 role docs over 7,070, and only the root `CLAUDE.md`
      (25,903) of 87 over 7,500. *Design* gives this as counter-evidence.
    - #4008's body records 3 PRs red on the size arm between 2026-10-05 and 2026-10-09,
      and its fix moved the check into `static-ratchet-tests`. *Design* argues from that
      cost.
    - Follow-through after a head injection is stated as unmeasured. `instructions.log`
      records injections and not later reads, and #2650's 23% covers outline injections.
- **G4-R2-03 (medium): a role doc that becomes a head defers `facts.md` and
  `python-layout.md`.** Fixed by accepting the change explicitly, as the finding allowed.
  `build_context` reserves only `floors[0]`, and `docs_for` lists ancestor `CLAUDE.md`
  files before rules, so the finding's mechanism holds. *Design*, step 0 and *Risks* name
  the deferral, and PR 1b's report paragraph documents it. `build_context` is not
  reordered.
- **G4-R2-04 (medium): the exit criterion cannot go red.** Fixed. The `__main__` block
  dispatches `--report` before `try: sys.exit(main()) except Exception: sys.exit(0)`, as it
  does `-h`. `report_rows` uses `git ls-files` with `check=True` and raises on an empty
  walk. PR 1b rewrites the docstring's two "exits 0" sentences. The exit-criterion command
  runs without a pipe. Paired subprocess tests cover a fixture checkout (exit 0) and a
  directory outside git (non-zero).
- **G4-R2-05 (medium): the operator ruling is never written into the tree.** Fixed in part.
  PR 2 adds a `# DECIDED` block to `test_role_claude_md.py` above `_doc_problems`. The
  `homelab-review` skill's step 3 greps `'# DECIDED'`, so a comment block is used rather
  than a docstring line. Two parts of the required fix were declined.
    - `.claude/rules/python-layout.md` is not amended. It is a head at 10,448 characters,
      and a one-off ruling belongs at its code site.
    - PR 2 does not regenerate `docs/reference/decisions.md`. The docs-refresh cron stages
      `docs/reference/` (`.claude/rules/generated-docs.md`) and regenerates the page from
      the marker.
- **G4-R2-06 (medium): nothing re-derives the 7,500 and 7,272 in `docs/claude-tooling.md`.**
  Fixed. `test_docs_quote_current_values.py:ROWS` has a row for `docs/claude-tooling.md` only
  for `RECENT_RESTART_SECONDS`. Its `_python_constant` reader takes a bare integer literal by
  regex, and 7,272 is computed from `_PREAMBLE`. PR 1b therefore replaces both figures with a
  pointer to the report's first line. *Design*, *Verification* and the failure-class-8 risk
  no longer claim the pin covers the doc. *Verification* adds a `git grep` that the figures
  are gone.
- **G4-R2-07 (medium): the `conjoined:` reason rests on a false premise.** Fixed by
  splitting.
    - `_facts_line.py` and `_hook_common.py` are bare-name library siblings with
      `# gen-hooks: library` blocks.
    - PR 1a moves `rule_globs` and `_header` into `_inline_budget.py` and adds
      `inline_margins` there. PR 1b adds the report walk to it.
    - The constants stay in the hook, because `_doc_size.py:hook_inline_max_chars` reads
      the `INLINE_MAX_CHARS = <int>` literal from the hook file.
    - The hook ends near 560 to 580 lines across the PRs, so the allowlist line is gone.
    - Open question 1 is corrected.
    - PR 1 is split into 1a and 1b to keep each under about 200 lines. PRs 2 and 3 keep
      their numbers, which G2's plan cites.
