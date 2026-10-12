# G2 — Retire the lock; ADR amending 0019

Status: PLANNED

Depends on: [G1](g1-diff-notice.md)

Unblocks: [G3](g3-freshness-reviewer.md), [G5](g5-shrink-root-claude-md.md)

## Goal and exit criterion

After G2, `docs/facts.lock` and everything that writes or reads it are gone:

- the `verify`, `reverify`, `forget` and `status` subcommands;
- the `facts-reverify-changed` prek hook;
- the hash test `test_every_recorded_atom_hashes_as_recorded`;
- the injection line that names unverified sections.

`fact_status.py lint` stays, with its prose rules. A new suite test runs it over every
section, so a citation that stops resolving still fails CI. The G1 notice becomes the only
signal that a diff moved a cited atom, and it never blocks.

A new ADR amends [ADR-0019](../../adr/0019-fact-support-stops-at-the-repo-store.md).
ADR-0019 retires the lock when `report --days 60` shows fewer than two actual content moves.
At `70591c307` the figure was three, so its own test was not met. PR 1 measures the figure
again. If that run shows fewer than two, ADR-0019's own criterion fires, and ADR-0020
records the retirement under that criterion rather than as an override.

**Exit criterion.** A PR that moves a cited symbol, YAML key, test or marker gets a green
`prek (lint + validate + tests + secrets)` context. The PR's
`hooks (lint + validate + secrets + docs)` job carries a `notice` annotation that names the
section citing the moved atom. An agent reads the annotation through the check-runs
annotations API, as *Verification* shows. In addition:

- `git ls-files docs/facts.lock` prints nothing;
- `fact_status.py --help` lists exactly `notice`, `lint` and `report`;
- the new ADR's status is `Accepted`;
- issue #4314 is closed by the PR that deletes the lock.

## Baseline at `70591c307`

**The writers of `docs/facts.lock`.**

- `scripts/dev/fact_status.py:cmd_verify`, with `--unverified`.
- `scripts/dev/fact_status.py:cmd_forget`.
- `scripts/dev/fact_status.py:cmd_reverify`, which the prek hook `facts-reverify-changed`
  runs on every commit that touches a `CLAUDE.md`.
- `scripts/lib/facts/lock.py:write_lock` is the one write path that all three call.

**The readers.**

- `test_every_recorded_atom_hashes_as_recorded` in `test_facts_lock_matches_tree.py`
  (`ansible/tests/repo/`). It blocks in the `pytest` job, and at commit time through the
  `static-ratchet-tests` prek hook.
- `test_facts_lock_units_lint_clean.py` (`ansible/tests/repo/`). It holds the 185 sections
  with a lock row lint-clean, and it blocks.
- `fact_status.py status` and `report`, through `lock.read_lock` and `lock.build_repo_edb`.
  On an absent file, `read_lock` returns `{}`, so every citing section would read
  UNVERIFIED.
- The `lock-tampered` rule in `scripts/lib/facts/lint.py:lint_sections`, through
  `lock.lock_tampered`. That function returns False for an absent file, so lint survives
  the deletion.
- `.claude/hooks/_facts_line.py:facts_line`, which `inject-nested-docs.py` calls for every
  `CLAUDE.md` it delivers whole. `_lock_units` raises on an absent file, and `unverified_sections`
  catches the error and returns None, so the line disappears on its own.
- Tier 2 of `.claude/agents/homelab-docs-freshness-reviewer.md` runs
  `fact_status.py status | grep '^UNVERIFIED'`. Its tier 3 `git log` path filter excludes
  `docs/facts.lock`.
- `scripts/lib/facts/removed.py:LOCK_PATH`, which is excluded from `_CODE`.
  `evidence.py` imports it.

**The importers of `lock.py`, `evidence.py` and `relations.py`.** This command lists them:

```bash
git grep -n -E -e 'lib\.facts\.(lock|evidence|relations)\b' \
  -e 'from \.(lock|evidence|relations) import' -- scripts/ .claude/ ansible/
```

At `70591c307` it prints 17 lines. The importers outside the three modules and their four
test modules are these:

- `scripts/dev/fact_status.py` imports eight names from `lib.facts.lock`, and `derive` and
  `status_of` from `lib.facts.relations`.
- `scripts/lib/facts/report.py` imports `LOCK_REL` and `parse_lock` from `.lock`.
- `scripts/lib/facts/lint.py` imports `LOCK_REL` and `lock_tampered` from `.lock`.
- `scripts/dev/tests/test_facts_cli.py` imports `read_lock`.
- `scripts/lib/tests/test_facts_lint.py` imports `LOCK_REL` and `write_lock`.
- `scripts/lib/tests/test_facts_report.py` imports `LOCK_REL` and `verify_units`.
- `test_facts_lock_matches_tree.py` and `test_facts_lock_units_lint_clean.py` import from
  `lib.facts.lock`.
- `.claude/hooks/_facts_line.py` names `lib.facts.lock` in a docstring. It imports nothing
  from it.

Two shared helpers live inside modules G2 deletes:

- `scripts/lib/facts/lock.py:repo_citations` and its `Citations` alias. They use only
  `citations.py` functions. `cmd_report` calls `repo_citations`, and `report` keeps a
  ranking that needs it.
- `scripts/lib/facts/evidence.py:_source_at` reads a file at a revision with `git show`.
  G1's *Baseline* names it as a primitive the notice can read both ends through.

**Configuration and censuses that name the lock or its tests.**

- `prek.toml`:
    - the `facts-reverify-changed` block;
    - the `static-ratchet-tests` entry, its `name`, and the `^docs/facts\.lock$` arm of its
      `files` regex;
    - the ordering comments above `regen-role-glance`, `facts-reverify-changed`,
      `facts-lint-changed` and `static-ratchet-tests`. The `facts-lint-changed` comment
      names `test_facts_lock_units_lint_clean.py`.
- `test_static_ratchet_hook.py` (`ansible/tests/repo/`): its `EXPECTED_MODULES` set, and the
  `docs/facts.lock` case in its parametrize list.
- `scripts/tests/test_census_rows_test_modules.py`: a `must_find` set that names
  `test_facts_lock_matches_tree.py` and `scripts/lib/tests/test_facts_lock.py`.
- `.gitleaks.toml`: an allowlist for `generic-api-key` on `docs/facts.lock`.
- `scripts/docs/reference/decisions.py:_SELF_PATHS` names the lock. Two tests in
  `scripts/docs/tests/test_gen_reference_decisions.py` assert the exclusion:
  `test_the_facts_lock_is_excluded`, and one line of
  `test_live_tree_excludes_its_own_generator_and_test_file`.
- `scripts/lib/tests/test_facts_lint.py:test_rule_census` lists `lock-tampered`.
- `.claude/rules/facts.md`:
    - a `paths:` glob for `docs/facts.lock`, which
      `test_rules_globs_match_a_tracked_file.py` (`.claude/hooks/tests/`) reads;
    - the opening paragraph, which says a section's status is derived;
    - the `renovate-pin` paragraph, whose reason is that a Renovate PR cannot run `verify`
      and stays red (#4012);
    - the opener of *Rules that read prose*, which says a sentence can go stale "behind an
      IN grade";
    - the *Commands* section about the lock.
- `scripts/lib/facts/pins.py`: the module docstring gives the same reason for `renovate-pin`.
  It names `test_every_recorded_atom_hashes_as_recorded` and `fact_status.py verify`.
- Docstrings and comments in modules G2 keeps:
    - `scripts/lib/facts/atoms.py`: the module docstring names `lock.check_lock`;
    - `scripts/lib/facts/citations.py:sections`: the docstring names the unit `facts.lock`
      is keyed by;
    - `scripts/lib/facts/lint.py:lint_sections`: the docstring names `lock.check_lock`, and
      a comment names `lock.repo_citations`;
    - `scripts/lib/facts/report.py`: the module docstring says the report measures whether
      the lock pays for itself.
- The root `CLAUDE.md` *Where to Look* row that starts *CI fails on
  `test_every_recorded_atom_hashes_as_recorded`*.
- `docs/claude-tooling.md`: one paragraph on the `_facts_line` injection line.
- `mkdocs.yml` lists every ADR in its nav by hand.
  `ansible/tests/repo/test_adr_links.py:test_the_nav_lists_every_adr` fails an ADR the nav
  leaves out.

**Generated pages that carry rows from the lock's code.** `docs/reference/decisions.md`
holds `path:line` rows for the `DECIDED:` markers in `scripts/lib/facts/evidence.py`,
`scripts/lib/facts/lock.py` and `.claude/hooks/inject-nested-docs.py` (the facts-line
marker). The line-numbered-path test in `test_documented_paths_exist.py` still checks those
rows. Precedent such as `343f5414d` regenerates the page in the PR that moves a
marker-bearing file.

**ADR state.**

- ADR-0019's *Governs* section cites the `test_status_census` node in
  `scripts/lib/tests/test_facts_relations.py`. `test_every_cited_test_exists` checks that
  citation, so it must change when that test goes.
- The highest ADR is 0019. ADR-0017 amends ADR-0011, and ADR-0011's *Status* section says
  so in one paragraph. That pair is the amendment precedent.
- `VALID_STATUS` in `test_adr_links.py` (`ansible/tests/repo/`) accepts `Accepted`,
  `Proposed`, `Rejected` and `Superseded by ADR-NNNN`.
- `docs/adr/index.md` *Superseding*: an ADR is never deleted or rewritten to match a
  reversed decision.

**CI surfaces an agent can read.** `gh run view --help` lists the run JSON fields, and none
of them is the job summary. `gh pr checks` shows neither the summary nor the annotations.
The annotations of one job come from
`gh api repos/DanielH2018/server/check-runs/<job-id>/annotations`. The job id is the last
path segment of the job's `detailsUrl` in `gh pr view <n> --json statusCheckRollup`. Both
calls answered for PR #4440 on 2026-10-11.

**The whole-tree lint.** `uv run --frozen python scripts/dev/fact_status.py lint` exits 0
with no output, in 0.29 seconds, over all 451 sections. `lint.WARN_RULES` holds
`one-way-test` and `version-as-fact`. Every other rule in `lint.RULES` is an error.
`retired-host` reads `ansible/inventory/hosts.ini`, which H7 keeps as renderer data.

**Sizes, by `wc -l`.**

| Deleted whole | Lines |
|---|---|
| `docs/facts.lock` (generated) | 1,471 |
| `scripts/lib/facts/lock.py`, `evidence.py`, `relations.py` | 761 |
| their tests: `test_facts_lock.py`, `test_facts_lock_rows.py`, `test_facts_lock_evidence.py`, `test_facts_relations.py` | 711 |
| `test_facts_lock_matches_tree.py`, `test_facts_lock_units_lint_clean.py` | 59 |
| `.claude/hooks/_facts_line.py` and its test | 201 |

Partial deletions add roughly 600 lines:

- four subcommands of `fact_status.py` and their CLI tests;
- the precision half of `report.py`;
- the lock half of `.claude/rules/facts.md`;
- the `prek.toml`, `.gitleaks.toml`, `decisions.py` and hook edits.

That totals about 3,800 lines. The design document's 4,077 counted `report.py` and the CLI
whole.

**Open work this slice must not trip over.** `ansible/roles/setup/common/tasks/agent_user.yml`
names `facts-reverify-changed` in a comment. Per that role's `CLAUDE.md`, any edit to a
`tasks/*.yml` there re-applies the setup roles that import it, so G2 leaves the comment
alone. `scripts/deploy_tools/land_lib/merge.py` names `docs/facts.lock` in a `DECIDED:`
comment about #2266. That comment is history and stays.

## Design

Ownership of "this diff moved something a section relies on" moves from the lock to the
notice. The lock is a stored attestation: a verified hash that any later commit can
invalidate, and a blocking test that fails until someone re-verifies. The notice compares
the two ends of one diff and blocks nothing. What stays blocking is the part that never
depended on stored state: lint's rules over the tree as it stands.

**The ADR.** Use the next free number, which is ADR-0020 at `70591c307`. The file is
`docs/adr/0020-fact-support-is-a-diff-notice.md`, titled *Fact support is a diff notice, and
the lock is retired*.

- *Context.*
    - ADR-0019's criterion, with the `70591c307` figure (content potential 10, actual 3)
      and the figure PR 1 measures.
    - The lock's cost: 116 lock commits in 30 days out of 1,282, Renovate PRs held red
      (#4012), and run 35729601483.
    - Every record that G1's plan assigns to the G2 ADR. G1 names them in its *Goal and
      exit criterion*, *Steps*, item 6, and *Verification*, and that text is the list. It
      includes the output of G1's three exit checks pasted in full, the `notice_replay`
      JSON verbatim, and the explanation of each parity miss G1 left unfixed. It also
      includes each artifact, run, move and PR that those sections name rather than fail,
      each PR that moved `<ts>`, and each mechanical `notice.py` change that did not.
- *Decision.*
    - Delete the lock, its four subcommands, the reverify hook and the hash test.
    - Keep `lint`, and run it over every section in the suite. The suite test blocks on
      every error rule, not only on citation resolution. *The whole-tree lint test* below
      gives the reason.
    - The notice is the advisory signal.
    - When PR 1 measures two or more actual moves, ADR-0019's criterion no longer decides,
      because the notice covers the sections that criterion was measuring. When it
      measures fewer than two, the criterion fired, and this record names the replacement.
- *Consequences.*
    - A moved atom no longer blocks a merge.
    - Failure class 8 loses blocking coverage for content atoms.
    - Agent sessions cannot read the job summary. `gh pr checks` and `gh run view` show
      neither the summary nor the annotations. An agent reads the notice through the
      check-runs annotations API, or runs `fact_status.py notice --since origin/master`
      locally. G3 is the remedy that brings the notice to a reviewer without either step.
    - `report`'s notice replay, over a plain `--days` window, is the yield measurement
      from now on, and G3 is the escalation path if the yield falls.
    - Reversing the decision means restoring the lock from git history and re-verifying every
      section that moved since. *Rollback* in this slice gives the order.
- *Governs.* One marker: `# DECIDED: the notice never blocks a merge (ADR-0020)`, added in
  the same PR to the notice step's comment in `.github/workflows/ci.yml`. The frontmatter
  lists `.github/workflows/ci.yml#the notice never blocks a merge`.
  `test_adr_links.py` then holds the marker and the record to each other.

**The whole-tree lint test.** It replaces `test_facts_lock_units_lint_clean.py`, which held
only the 185 sections with a lock row. The new test calls `lint.lint_sections(REPO, None)`
and fails on any finding whose rule is not in `lint.WARN_RULES`. That is every error rule
in `lint.RULES`, over all 451 sections. `vanished-identifier` is the one error rule it does
not run, because that rule needs a `since` range. At `70591c307` the call returns nothing.

The test does not reintroduce an attestation. It reads only the tree at one commit and
stores nothing. It blocks on more than citation resolution, and the choice is deliberate.
Four rules can turn it red from a diff that edits no `CLAUDE.md`:

- `unresolved-atom`: the diff deletes or renames what a section cites.
- `ambiguous-marker`: the diff adds a second `DECIDED:` marker that a cited prefix matches.
- `retired-host`: the diff removes a host from `ansible/inventory/hosts.ini`. No slice in
  the index retires a host. H7 keeps `ansible/inventory/` as renderer data, so the rule
  stays live.
- `renovate-pin`: the diff changes `renovate.json` so that a manager selects a YAML key a
  section cites. M2's PR 2 and H5.7 each add a Renovate manager, so either can meet this.

The other error rules read only `CLAUDE.md` text. Only a doc edit can trip them, and
`facts-lint-changed` catches that edit at commit time first.

In each case the red PR is the one that made a section wrong. `vanished-identifier`
already holds that contract. Filtering the test to the resolution rules would let the prose
rules drift on sections no PR edits. The next author to edit such a section would then meet
the finding through `facts-lint-changed`, for a change they did not make.

**The `lock-retired` rule.** It replaces `lock-tampered` in `lint.RULES`. It is an error
when `docs/facts.lock` exists. `lint.py` holds the path as its own constant, so the rule
needs no import from `lock.py`. Its message tells the author to take the deletion:
`git rm docs/facts.lock`.

The rule exists for one case. A branch cut before the deletion still runs the reverify hook
from its own `prek.toml`, so it keeps writing the lock. Its PR then conflicts on the deleted
file, and a careless resolution adds the file back. The whole-tree lint test turns that red.

**The order of the PRs follows the two-writer rule (C4), applied to a file.** C4's literal
scope is a `containers_list` entry, which G2 does not touch. Its principle applies to
`docs/facts.lock`: no commit leaves a writer of the lock without the lock, or a blocking
reader of the lock without its writers.

- Every writer leaves in the same commit as the file: `verify`, `forget`, `reverify` and
  the prek hook. Otherwise the reverify hook would write a fresh lock on the next
  `CLAUDE.md` commit.
- Every reader that blocks, or that misreads an absent file, leaves no later than that
  commit. The hash test and `status` leave in it. The reviewer's tier 2 and the lint-clean
  test over locked sections leave one PR earlier.
- Readers that fail closed on an absent file leave afterwards, as dead code: `_facts_line`,
  and `lock_tampered` once lint stops importing it.
- Helpers that kept modules share move out of the deleted modules before the deletion:
  `repo_citations` and `Citations`, and `_source_at` if G1's code imports it.

**Other slices' lock steps.** Many slices re-verify a `CLAUDE.md` section with
`fact_status.py verify` and commit `docs/facts.lock` when they edit that section. G2 adds no
dependency edge to any of them. Instead, each such step opens with "Until G2 retires
`docs/facts.lock`", the wording D6 and R4 use. The lock counts as retired once PR 3 is on
master. This check reports it:

```bash
git fetch origin master
git cat-file -e origin/master:docs/facts.lock
```

`git cat-file -e` exits 0 while master holds the lock, and exits 128 once PR 3 has merged.
It also exits 128 when the fetch failed and `origin/master` is missing. If a step is skipped
for that reason while the lock exists, and the PR moved a recorded atom, the hash test turns
the PR red. The wrong skip is therefore loud. A branch cut before PR 3 rebases onto master before its lock step, and the step then does
not run. A step that commits only what the `facts-reverify-changed` hook writes needs no
condition, because PR 3 deletes the hook.

```diff
 docs/
-  facts.lock
+  adr/0020-fact-support-is-a-diff-notice.md
 mkdocs.yml                + ADR-0020 nav line
 scripts/dev/fact_status.py
-  status, verify, reverify, forget
   notice, lint, report
 scripts/lib/facts/
-  lock.py  evidence.py  relations.py
   atoms.py  citations.py  lint.py  notice.py  pins.py  removed.py  report.py
   citations.py: + repo_citations, Citations (moved from lock.py)
                 + source_at (moved from evidence.py, only if G1 imports it)
   lint.py:  - lock-tampered   + lock-retired   - first_cited_units
   report.py: - precision, parity, status ranking, lock-commit window anchor
              + sections citing nothing, by reads
 ansible/tests/repo/
-  test_facts_lock_matches_tree.py  test_facts_lock_units_lint_clean.py
+  a whole-tree lint test (every section lint-clean, non-vacuous)
 .claude/hooks/
-  _facts_line.py  tests/test_facts_line.py
   inject-nested-docs.py: - facts line
 .claude/agents/homelab-docs-freshness-reviewer.md: - tier 2
 prek.toml
-  facts-reverify-changed
   static-ratchet-tests: lock test -> whole-tree lint test
```

## Steps, as PRs

The PR numbers stay as other slices cite them: G3 and G4 name G2's PR 2, PR 3 and PR 4.
The pre-flip PR therefore takes the label PR 2b.

- **PR 1 — ADR-0020, proposed** (about 120 lines).
    - **Entry condition.** PR 1 opens only when G1's exit criterion holds. That criterion
      is the three checks in G1's *Goal and exit criterion*, items 1 to 3, run with the
      commands in G1's *Verification*. G1's *Steps*, item 6, defines the lower bound
      `<ts>` those checks grade from, when it moves, and the 30 days that follow it. G2
      does not restate the checks or the bound. Where this plan and G1's text differ,
      G1's text governs.
    - Paste the output of all three checks into the ADR's *Context*, with every record that
      G1 assigns to the G2 ADR (see *Design*).
    - If any check fails, stop G2. The cause is fixed in a G1 follow-up PR. G1's *Steps*,
      item 6, decides whether that PR moves `<ts>` and so restarts the 30 days. PR 1 opens
      only after a later run of all three checks passes.
    - Run `uv run python scripts/dev/fact_status.py report --days 60` and paste its output
      into the ADR's *Context*. `precision` still exists here, because PR 2 deletes it.
    - Write the ADR with `status: Proposed`, and its *Status* section reading *Proposed*.
    - Add the `DECIDED:` marker to the notice step's comment in `ci.yml`.
    - Add a paragraph to ADR-0019's *Status* section. Its wording depends on the figure:
        - two or more actual content moves: *Amended by ADR-0020. The lock is retired by a
          measured replacement, not by this record's criterion.*
        - fewer than two: *Amended by ADR-0020. This record's retirement criterion fired on
          <date>, and ADR-0020 records the replacement.*
    - Add the row to `docs/adr/index.md`.
    - Add the ADR-0020 line to the ADR list in the `mkdocs.yml` nav, after ADR-0019's.
    - **Operator-only:** approve the ADR by setting `status: Accepted` and the *Status* text
      before merge. PRs 2 to 5 do not merge until master carries the accepted record.
- **PR 2 — `report` stops reading the lock** (about 120 lines added and 150 deleted, most
  of the deletions G1's parity code and tests).
    - Move `repo_citations` and the `Citations` alias from `lock.py` into `citations.py`.
      `lock.py` imports both back from `citations.py`, so its callers and tests keep working
      until PR 5.
    - Re-run the first `git grep` from *Verification*. G1 lands between `70591c307` and
      this PR. Assign any match G1 added to the PR that deletes what it names, and add it
      to the table in *Verification*.
    - Run the import census from *Baseline*. If G1's `notice.py` or `report.py` imports
      `evidence._source_at`, move it into `citations.py` as `source_at`, beside
      `tracked_files`, and repoint every importer. `evidence.py` then imports it from
      `citations.py` until PR 5.
    - Remove `precision`, `_lock_at`, the G1 parity rows and the status ranking from
      `scripts/lib/facts/report.py`. `report.py` drops its `.lock` import. The G1 parity
      rows are these three:
        - content-form lock moves by class, with the `listed`, `hashing`, `split` and
          `miss` classes;
        - path-form lock moves;
        - the lock commits in which a row's `python` changed.
    - Return the replay window to the plain `--days` boundary. G1 starts the window at the
      last lock commit before that boundary, and only the `split` class needs that anchor.
      The anchor leaves with `split`. Otherwise, after PR 3 the last lock commit is the
      deletion itself, and a `report --days N` run more than N days after the flip would
      replay every commit since the flip.
    - Keep G1's notice replay: the commits in the window, the commits carrying a notice,
      the notices by kind, the prose-edit counts, the `crashed` entries and
      `content_notices`. The `notice_replay` keys `moved` and `moved_prose_edited`, which
      G3 reads, stay.
    - Rank the sections that cite nothing by doc reads: these are the CONVENTION sections,
      derived from `citations.repo_citations` alone.
    - `cmd_report` stops calling `build_repo_edb`, `derive` and `status_of`, and takes
      `repo_citations` from `citations.py`.
    - Fix the `lint.py` comment in `lint_sections` that names `lock.repo_citations`.
    - The lock still exists, so this PR changes no gate.
- **PR 2b — whole-tree lint test and reviewer tier 2** (about 90 lines added, 40 deleted).
    - Add the whole-tree lint test to `ansible/tests/repo/`, with its red-proof pair and its
      non-vacuity floor.
    - Delete `test_facts_lock_units_lint_clean.py`. The new test checks every section,
      which includes the 185 it held.
    - Rewrite the `prek.toml` comment above `facts-lint-changed`, which names the deleted
      test.
    - In the freshness reviewer, drop tier 2, renumber the tiers, and update the
      `description:` frontmatter. Keep `':!docs/facts.lock'` in the tier 3 path filter:
      `git log --since=30.days` still lists the lock's history for 30 days after PR 3.
    - The lock and the hash test still exist, so no writer loses its file and no blocking
      reader loses its writer.
- **PR 3 — the flip** (about 100 hand-written lines added and 490 deleted, plus the
  1,471-line generated lock).
    - Delete:
        - `docs/facts.lock`;
        - `test_facts_lock_matches_tree.py`;
        - `cmd_status`, `cmd_verify`, `cmd_reverify`, `cmd_forget` and their argparse
          entries, which span 142 lines of `fact_status.py`;
        - their tests in `scripts/dev/tests/test_facts_cli.py`, which span 184 lines, and
          that file's `read_lock` import.
    - `fact_status.py` drops its `lib.facts.lock` and `lib.facts.relations` imports.
    - Replace `lock-tampered` with `lock-retired` in `lint.py`, which drops its `.lock`
      import. Rewrite the `lint_sections` docstring that names `lock.check_lock`.
    - In `scripts/lib/tests/test_facts_lint.py`, replace the tampered test with the
      `lock-retired` pair, update `test_rule_census`, and drop the `lib.facts.lock` import.
    - `prek.toml`:
        - delete the `facts-reverify-changed` block;
        - in `static-ratchet-tests`, swap the lock test for the whole-tree lint test and drop
          `facts.lock` from the `name` and the `files` regex;
        - rewrite the ordering comments above `regen-role-glance` and
          `static-ratchet-tests`.
    - Update the censuses and allowlist entries:
        - `EXPECTED_MODULES` and the parametrize list in `test_static_ratchet_hook.py`;
        - the `must_find` entry in `test_census_rows_test_modules.py`. Replace the deleted
          module with one the census still finds, chosen by
          `git grep -l 'subprocess.run(\["git"' -- '*test*.py'`;
        - the `.gitleaks.toml` allowlist block;
        - `_SELF_PATHS` and its comment in `decisions.py`, with the two test assertions.
    - `.claude/rules/facts.md`:
        - drop the `docs/facts.lock` glob;
        - rewrite the opening paragraph without statuses;
        - rewrite the `renovate-pin` paragraph's reason: without the lock, citing a managed
          key as a YAML atom puts a notice on every bump PR, for a section whose prose the
          bump cannot change;
        - rewrite the opener of *Rules that read prose* without the IN grade;
        - rewrite *Commands* around `notice`, `lint` and `report`, pointing at ADR-0020.
    - Rewrite the `scripts/lib/facts/pins.py` module docstring with the same reason as the
      `renovate-pin` paragraph. It names no deleted test or subcommand.
    - Rewrite the `citations.py:sections` docstring sentence about the unit the lock is
      keyed by.
    - Replace the root `CLAUDE.md` lock row with one line: when a PR changes code that a
      `CLAUDE.md` section cites, run `uv run python scripts/dev/fact_status.py notice
      --since origin/master`, and re-read each listed section whose prose the PR left
      unchanged. `.claude/rules/facts.md` has the grammar.
    - The PR body carries `Closes #4314` and names the stale-branch instruction: take the
      deletion.
    - The PR body also says that the flip turns off every other slice's lock step, under
      the condition in *Design*. It names the slices whose plans carried such a step at
      review time: D2, D3, D4, D6, H1, H2, H5, H7, M1, M2, M3, M6, R2 and R4.
    - This PR splits no further. What remains is every writer, the hash test, `status`, and
      the docs and censuses that name them.
        - Splitting a writer from the file leaves a writer without its file.
        - `status` cannot leave first. Eight of the CLI tests for `verify`, `forget` and
          `reverify` assert through `main(["status", ...])`, and the usage errors in
          `cmd_verify` and `cmd_forget` tell the user that `fact_status.py status` lists the
          keys. Deleting `status` first means rewriting tests and messages this PR deletes.
        - The docs describe the commands this PR deletes, so they change with them.
- **PR 4 — the injection line** (about 20 lines added, about 230 deleted).
    - Delete `.claude/hooks/_facts_line.py` and `.claude/hooks/tests/test_facts_line.py`.
    - In `inject-nested-docs.py`, remove the import and the `facts=` parameter of `render`.
      Remove the `facts_line(...)` element that `build_context` adds to each candidate, the
      `DECIDED:` marker on the facts line, and the docstring paragraph.
    - Keep `facts.md` in `READ_RULES`, because it still governs the citations a
      `CLAUDE.md` read leans on.
    - Delete the paragraph in `docs/claude-tooling.md`.
    - Regenerate `docs/reference/decisions.md` with
      `uv run python scripts/docs/reference/decisions.py --out docs/reference/decisions.md`.
- **PR 5 — dead library code** (about 40 lines added, about 1,500 deleted).
    - **Precondition: the import census.** Run the census command from *Baseline*. After
      PRs 2 to 4, it prints exactly these seven lines, and nothing else:
        - two in `scripts/lib/facts/lock.py`, importing `.evidence` and `.relations`;
        - two in `scripts/lib/tests/test_facts_lock.py`;
        - one each in `test_facts_lock_evidence.py`, `test_facts_lock_rows.py` and
          `test_facts_relations.py`.
    - Any other line is a surviving importer. Move or delete it in its own PR first.
    - Delete `lock.py`, `evidence.py` and `relations.py`, with their four test modules.
    - Delete `lint.first_cited_units`, the reverify path's only caller.
    - Delete `removed.LOCK_PATH`, and its exclusion in `_CODE`.
    - Rewrite the `atoms.py` module docstring that names `lock.check_lock`.
    - Remove the second deleted module from the census `must_find`.
    - In ADR-0019's *Governs* section, replace the node citation with the module's basename
      and a pointer to ADR-0020. `test_every_cited_test_exists` would otherwise fail.
    - Regenerate `docs/reference/decisions.md` and `docs/reference/scripts.md` with their
      generators.

## Verification

Exit criterion, on master after PR 5:

```bash
git ls-files docs/facts.lock | wc -l
uv run python scripts/dev/fact_status.py --help
git grep -n -e facts-reverify-changed -e test_every_recorded_atom_hashes_as_recorded \
  -e verify_units -e read_lock -e lock_tampered \
  -- ':!docs/adr/' ':!docs/archive/' ':!docs/target-architecture/' \
     ':!ansible/roles/setup/common/tasks/agent_user.yml'
git grep -n -E -e 'lib\.facts\.(lock|evidence|relations)\b' \
  -e 'from \.(lock|evidence|relations) import' -- scripts/ .claude/ ansible/
uv run pytest -q -p no:cacheprovider scripts/lib/tests scripts/dev/tests scripts/docs/tests \
  ansible/tests/repo .claude/hooks/tests
prek run --all-files
```

The first command prints `0`. Both `git grep` commands print nothing. The suite and the
hooks pass.

The first `git grep` matches these files at `70591c307`. Each match leaves in the PR named:

| Match | Cleared by |
|---|---|
| `test_facts_lock_units_lint_clean.py` | PR 2b |
| `scripts/lib/tests/test_facts_report.py` (`verify_units`) | PR 2 |
| `test_facts_lock_matches_tree.py` | PR 3 |
| `scripts/dev/fact_status.py` (imports and calls) | PR 3 |
| `scripts/dev/tests/test_facts_cli.py` (`read_lock`) | PR 3 |
| `scripts/lib/facts/lint.py` (`lock_tampered`) | PR 3 |
| `scripts/lib/facts/pins.py` (module docstring) | PR 3 |
| `.claude/rules/facts.md` (*Commands*) | PR 3 |
| root `CLAUDE.md` (*Where to Look* row) | PR 3 |
| `prek.toml` (hook block and ordering comments) | PR 3 |
| `.claude/hooks/_facts_line.py` (docstring) | PR 4 |
| `scripts/lib/facts/lock.py` and its three test modules | PR 5 |

Prose the grep cannot see is assigned in *Steps*. PR 2 fixes the `lint.py` comment and
`report.py`'s docstring. PR 3 fixes the `facts.md` opening paragraph and the opener of
*Rules that read prose*, and the `citations.py` and `lint.py` docstrings. PR 5 fixes the
`atoms.py` docstring.

**The PR proof.** Use the first merged PR after PR 3 that moves a cited atom. To find it,
run the notice over each master commit since PR 3 merged. G1 defines `--head`. `xargs -t`
echoes each command before it runs, so each block of output follows the SHA it covers:

```bash
git log --first-parent --reverse --format=%H <pr3-sha>..origin/master \
  | xargs -t -I{} uv run python scripts/dev/fact_status.py notice --since '{}^' --head {} 2>&1
```

For a SHA whose block lists a row, find its PR, then the `hooks` job's notice annotations:

```bash
gh pr list --state merged --search <sha> --json number --jq '.[].number'
gh pr checks <n>
gh pr view <n> --json statusCheckRollup \
  --jq '.statusCheckRollup[] | select(.name == "hooks (lint + validate + secrets + docs)") | .detailsUrl'
gh api repos/DanielH2018/server/check-runs/<job-id>/annotations \
  --jq '.[] | select(.annotation_level == "notice") | .message'
```

The job id is the last path segment of the `detailsUrl`. `gh pr checks` must show
`prek (lint + validate + tests + secrets)` as `pass`. The annotations must name the section.
The job summary carries the same table. Reading it is an operator browser step, because no
`gh` command shows it.

If no such PR arrives within 7 days, open a draft PR from a scratch branch. The PR changes
only the value of `ansible/roles/k8s/valheim/defaults/main.yml:valheim_k8s_rollout_timeout`.
Read its CI, then close it without merging and delete the branch. A draft PR never reaches master,
so nothing deploys.

Red-proof for each new check, as paired tests:

- **The whole-tree lint test** (`ansible/tests/repo/`).
    - A fixture repository whose `CLAUDE.md` cites a missing symbol is flagged with
      `unresolved-atom`.
    - The same fixture, citing a symbol that exists, is clean.
    - Non-vacuity: the live walk reaches at least 400 sections. It must find a section of the
      root `CLAUDE.md` and one of `ansible/roles/k8s/traefik/CLAUDE.md`.
- **The plain replay window** (`scripts/lib/tests/test_facts_report.py`, PR 2).
    - A fixture history has a lock commit well before the `--days` boundary and commits on
      both sides of the boundary. The replay's first commit falls inside the boundary. G1's
      anchor fails this test, because it starts the window at the lock commit.
    - The same history with no lock commit gives the same window.
- **`lock-retired`** (`scripts/lib/tests/test_facts_lint.py`).
    - A fixture with a `docs/facts.lock` file yields one `lock-retired` error.
    - The same fixture without the file yields none.
- **The ADR marker.** `test_adr_links.py` already proves each direction can fail. PR 1's
  red-proof is that an anchor naming no marker fails there, which its own
  `test_a_text_anchor_that_names_no_marker_is_flagged` case shows.
- **The ADR nav line.** `test_the_nav_lists_every_adr` fails PR 1 if the `mkdocs.yml` line
  is missing, because it runs over every `docs/adr/[0-9]*.md`.

Before pushing PR 3, run the lint the hook runs, because the PR removes identifiers that
docs name:

```bash
uv run python scripts/dev/fact_status.py lint --changed-since origin/master
```

## Rollback

The PRs undo in reverse order. A later PR deletes code that an earlier PR's revert would
restore an import of, so a revert reaches back only through the PRs after it.

- **PR 1 — operator-only.** ADR-0020 is `Accepted` before it merges, and
  `docs/adr/index.md` forbids deleting or rewriting an accepted record.
    - Before merge, the operator closes the PR.
    - After merge, the operator either sets ADR-0020 to `Rejected` or writes a new ADR that
      supersedes it. ADR-0019's *Status* paragraph stays as the record, and gains one
      sentence that names the reversal.
    - Do this only before PR 3, or together with PR 3's undo, so the record never
      contradicts the tree.
- **PR 2** is a revert. `precision` reads the lock's git history through `lock.parse_lock`,
  so the revert needs `lock.py`. After PR 5, revert PR 5 first.
- **PR 2b** is a revert. The restored test reads the lock and fails on an empty one. Revert
  it only while the lock exists: before PR 3, or after PR 3's revert.
- **PR 3** is a revert, but the revert PR cannot be a bare `git revert`.
    - Before PR 4 and PR 5 merge, PR 3's revert stands alone. `lock.py`, `evidence.py`,
      `relations.py` and `lint.first_cited_units` still exist, so the restored subcommands
      and tests import cleanly.
    - After PR 5, revert PR 5 first. Then revert PR 4 if the facts line is wanted. Then
      revert PR 3.
    - The restored lock holds the hashes from the day of the flip. Any cited atom that moved
      since then turns the restored hash test red on the revert PR itself.
    - The revert PR therefore runs `fact_status.py status`, re-reads each OUT section, and
      runs `verify` on it. A heading renamed in the meantime (G5 renames none on purpose)
      needs `forget`.
    - The cost grows with the time since the flip.
- **PR 4** is a revert. `_facts_line.py` reads the lock file itself and imports nothing
  from `lock.py`. Without a lock, the restored line shows nothing, because the reader fails
  closed.
- **PR 5** is a revert. Its precondition proved that no kept module imports the three
  modules, so the revert restores modules nothing outside them imports. The restored
  `lock.py` imports `repo_citations` from `citations.py`, where PR 2 moved it.
- **A partial failure.** PR 3 is one squash commit, so the flip is atomic on master. The
  partial state that can occur is a stale branch adding `docs/facts.lock` back.
  `lock-retired` turns that branch's CI red.

## Risks

Each risk is checked against `docs/failure-classes.md`.

- **Failure class 8, prose that nothing re-derives, loses blocking coverage for content
  atoms.** A moved symbol no longer forces a re-read.
    - Mitigation: the notice lists the section on the PR, as annotations and in the job
      summary.
    - Mitigation: an agent reads the annotations through the check-runs API, or runs the
      notice locally, as the root `CLAUDE.md` row says.
    - Mitigation: the replay in `report` measures whether authors still act on it.
    - Mitigation: G3 is the escalation if they do not.
    - ADR-0020 records the trade and the agent-visibility gap.
- **G1's notice is wrong, crashes, or never reaches the PR when G2 starts.** G2 makes the
  notice the only signal. A parity miss, a crash, or an annotation that never reaches the PR
  at that point leaves moves unreported.
    - Mitigation: PR 1's entry condition is G1's exit criterion. It cites G1's three exit
      checks and G1's *Steps*, item 6, rather than restating them, so a later change to
      G1's rules cannot leave G2 grading against a stale copy. It stops G2 when any check
      fails.
    - Mitigation: ADR-0020's *Context* carries all three outputs, so the record shows the
      delivery evidence that PR 3 relies on.
- **Branches cut before PR 3.** Their own `prek.toml` still runs the reverify hook, and
  their own tests still include the hash test. After rebasing they conflict on
  `docs/facts.lock`.
    - Mitigation: the PR body says to take the deletion.
    - Mitigation: `lock-retired` catches a wrong resolution.
    - Mitigation: `land.sh` merges master into a stale PR branch before it merges, so the
      conflict surfaces there rather than on master.
- **`vanished-identifier` blocks PR 3.** PR 3 removes identifiers that the root
  `CLAUDE.md` names, such as `test_every_recorded_atom_hashes_as_recorded`. The hook's lint
  fails until the row changes in the same PR. Step 3 includes the row, and *Verification*
  runs the lint before push.
- **Failure class 4, a census that drifts from its subject.**
    - Three censuses name deleted modules: `must_find`, `EXPECTED_MODULES`, and the
      static-ratchet `files` regex. Each fails loudly rather than silently, and each changes
      in the PR that deletes its member.
    - The import census guards PR 5. A surviving importer of a deleted module would fail
      only when imported. PR 5's precondition lists the exact expected lines, so a new
      importer, such as one G1 adds, shows up before the deletion.
- **Generated pages go stale.** `decisions.md` rows point at markers in deleted files. The
  line-numbered-path test would fail on them, so PRs 4 and 5 regenerate the page. Neither
  PR hand-edits it.
- **Failure class 3, an empty result read as clean.** A whole-tree lint that silently walks
  nothing would pass. The non-vacuity floor and the two named members guard it.
- **The floor and a member meet other areas' deletions.** The floor of 400 sections and the
  `ansible/roles/k8s/traefik/CLAUDE.md` member both depend on role docs. R4, D6 and H7
  delete role trees. The slice that deletes a member swaps it for a surviving doc, such as
  the root `CLAUDE.md`, and lowers the floor in the same PR. The test fails loudly until it
  does.
- **The whole-tree lint test blocks a PR in another area.** It blocks on every error rule,
  not only on resolution.
    - A Renovate manager added by M2's PR 2 or H5.7 turns red any section that cites a key
      the manager selects, under `renovate-pin`. That PR changes the citation to a path
      atom.
    - A host removed from `ansible/inventory/hosts.ini` turns red every section naming it,
      under `retired-host`. No slice in the index removes a host.
    - In both cases the PR that made the section wrong is the one held red.
- **A stale comment in a setup role.** `agent_user.yml` keeps naming
  `facts-reverify-changed`. Editing it alone would re-apply `renovate_agent` and
  `claude_code` for a comment. PR 3 files a finding to fix it with that file's next
  functional change.
- **Another slice runs a lock step after PR 3.**
    - On a branch based on master after PR 3, the step fails loudly.
      `scripts/dev/fact_status.py:main` builds its subcommands with
      `add_subparsers(dest="cmd", required=True)`. Argparse therefore rejects a removed
      subcommand with `invalid choice` and exits 2. That was measured at `70591c307` with
      an unknown subcommand. Nothing on master writes the lock after PR 3.
    - The quiet case is a pipeline that reads only stdout. Argparse writes its error to
      stderr, so `fact_status.py status | grep ...` prints nothing after PR 3, and the
      check reads as clean. That is failure class 3, an empty result read as clean. D6's
      *Verification* held such a line at review time, and the line is reported to D6.
    - A branch cut before PR 3 can still write the lock, and its rebase can add the file
      back. `lock-retired` turns that branch red, as it does for any stale branch.
    - Mitigation: each slice's lock step runs only under the condition in *Design*, and
      PR 3's body names the slices whose plans carry one.
- **Interplay with other areas.** After G2, the deletions in R4, D6 and H7 meet the notice
  (advisory) and the whole-tree lint (blocking on every error rule) instead of the hash
  test. Each of those PRs edits the sections that name what it deletes, which
  `vanished-identifier` already requires.

## Tests and docs that change in the same PR

- **PR 1:**
    - the ADR, ADR-0019's *Status* paragraph, `docs/adr/index.md` and the `mkdocs.yml`
      nav line;
    - the `ci.yml` marker. `ci.yml` is a full-sweep path, so the PR runs every hook.
- **PR 2:**
    - `scripts/lib/tests/test_facts_report.py`, which drops its `lib.facts.lock` import.
      G1's replay tests there change with the parity rows and the anchor:
        - the tests of the `listed`, `miss` and `hashing` classes, and both `split` tests,
          leave with parity. The second `split` test pins the window to a lock commit, so
          it would fail under the plain boundary;
        - the test whose citing section is deleted after the move keeps its listing
          assertion and drops its zero-miss assertion;
        - the `crashed` test stays;
        - the plain-window pair from *Verification* joins the file;
    - `test_report_json_ranks_the_sections_not_in` in `scripts/dev/tests/test_facts_cli.py`,
      because the ranking it asserts changes;
    - a pair in `scripts/lib/tests/test_facts_citations.py` for `citations.repo_citations`:
      a tracked path citation is kept, and an untracked one is dropped. The only test that
      calls it at `70591c307`,
      `test_facts_lock.py:test_a_reader_given_the_parsed_citations_reads_them_not_the_tree`,
      also exercises `check_lock` and `verify_units`, so it stays and leaves with PR 5;
    - the `report` bullet in `.claude/rules/facts.md`, `report.py`'s module docstring, and
      the `lint.py` comment.
- **PR 2b:**
    - the whole-tree lint test and its pair, and the deleted lint-clean test;
    - the `prek.toml` comment above `facts-lint-changed`;
    - the freshness reviewer's tiers and `description:`.
- **PR 3:**
    - the `lock-retired` pair and `test_rule_census` in `test_facts_lint.py`;
    - `test_facts_cli.py`, `test_static_ratchet_hook.py`, `test_census_rows_test_modules.py`
      and `test_gen_reference_decisions.py`;
    - `.claude/rules/facts.md`: the glob, the opening paragraph, the `renovate-pin`
      paragraph, the opener of *Rules that read prose*, and *Commands*;
    - the root `CLAUDE.md` row;
    - the module docstrings of `fact_status.py` and `pins.py`, and the docstrings of
      `lint.lint_sections` and `citations.sections`.
- **PR 4:** `docs/claude-tooling.md`, `inject-nested-docs.py`'s docstring, and the
  regenerated `docs/reference/decisions.md`.
- **PR 5:**
    - ADR-0019's *Governs* sentence and the census `must_find`;
    - the `atoms.py` module docstring;
    - the regenerated `docs/reference/decisions.md` and `docs/reference/scripts.md`.
- **The shard-weights table.** `scripts/dev/pytest_shard_weights.json` keeps rows for the
  deleted tests until the docs-refresh cron drops them. `scripts/dev/pytest_shard.py` says a
  stale row makes CI slower, never wrong, so no PR edits the table by hand.

## Complexity delta

- **Removed:** about 3,800 lines, itemised in the *Baseline* table. Also removed:
    - one prek hook, one gitleaks allowlist and one generator exclusion;
    - four CLI subcommands;
    - the four-status algebra;
    - every `verify` commit. That was 116 lock commits in 30 days.
- **Added:**
    - ADR-0020, about 80 lines, and its nav line;
    - the whole-tree lint test, about 40 lines;
    - `lock-retired` and its pair, about 40 lines;
    - one `DECIDED:` marker.
- **Moved, not added:** `repo_citations` and `Citations` into `citations.py`, and
  `_source_at` too if G1 imports it.

## Open questions

- **Which ADR number?** D6 also writes an ADR, superseding 0010. Resolution: PR 1 takes the
  next free number when it opens and renames on conflict before merge. This is reported as
  a contract gap.
- **Does the notice keep `gone` path rows?** Resolution: decided in PR 1 from G1's replay
  counts, and recorded in the ADR.
- **Does `renovate-pin` keep its rule?** PR 3 rewrites its reason. Without the lock, a
  citation of a managed key costs a notice on every bump PR, not a red PR. Resolution: keep
  the rule in G2. Revisit it with 30 days of notice data, and retire it if Renovate-managed
  citations add no notices worth reading.
- **When does `lock-retired` retire?** Its subject is a branch older than PR 3. Resolution:
  PR 3 files a finding to delete the rule once
  `gh pr list --state open --search "created:<<flip date>"` returns nothing, or 60 days
  after the flip, whichever is later.
- **Who owns the one-time rewrite of the 87 `CLAUDE.md` files?** The area design moves facts
  into generated blocks, `docs/reference/`, `DECIDED:` markers and `--help` text. No slice
  in the index owns that rewrite. Resolution: report it as a contract gap. `report`'s
  ranking of sections that cite nothing, by reads, is the work queue for whichever slice
  takes it.
- **Does G1's `notice.py` import from `evidence.py` or `lock.py`?** G1's plan names
  `evidence._source_at` as a primitive but does not say where the notice reads both ends.
  Resolution: PR 2's census decides. A G1 importer gets the helper moved into
  `citations.py` in PR 2. This is reported to G1 as a note.

## Review log

### Round 1

Findings raised, all fixed except where marked:

- **G2-R1-01 (high): PR 5 deletes three modules without an import census, and G1's notice
  builds on `evidence.py`.** Fixed.
    - The census command was run at `70591c307`, and its 17 lines are in *Baseline*. It
      confirmed the finding's importers. It also found two the finding missed:
      `test_facts_report.py` imports `verify_units`, and `test_facts_lint.py` imports
      `write_lock`.
    - `repo_citations` lives in `lock.py`, and `cmd_report` calls it. PR 2 moves it and
      `Citations` into `citations.py`.
    - PR 2 moves `_source_at` into `citations.py` when a G1 module imports it.
    - PR 2 and PR 3 now name every import they drop. PR 5 carries the census as a
      precondition, with the exact seven lines it must print.
    - The PR 5 rollback sentence now holds once those moves land.
- **G2-R1-02 (high): the *Verification* grep cannot print nothing.** Fixed.
    - The grep was re-run at `70591c307`. `scripts/lib/facts/pins.py`'s docstring was the
      one match no PR cleared.
    - PR 3 rewrites that docstring and the `renovate-pin` paragraph in
      `.claude/rules/facts.md`.
    - *Verification* now maps every match to the PR that clears it. It also assigns the
      stale prose the grep cannot see: the opening paragraph and the opener of *Rules that
      read prose* in `facts.md`, and the docstrings in `atoms.py`, `citations.py`,
      `lint.py` and `report.py`.
- **G2-R1-03 (medium): PR 1 omits the `mkdocs.yml` nav entry.** Fixed. Confirmed against
  `test_adr_links.py:test_the_nav_lists_every_adr` and the hand-written ADR list in
  `mkdocs.yml`. PR 1 adds the line, and a red-proof bullet names the test.
- **G2-R1-04 (medium): the whole-tree lint test blocks on every lint rule, not only on
  resolution.** Fixed.
    - The *Design* and *Risks* text now say it blocks on every error rule in `lint.RULES`,
      except `vanished-identifier`, which needs a range.
    - The choice is to accept that, and ADR-0020's *Decision* names it.
    - The diffs that can trip it from outside a `CLAUDE.md` are named: a host removal
      (`retired-host`), and a Renovate manager from M2's PR 2 or H5.7 (`renovate-pin`).
    - H7 keeps `ansible/inventory/`, so `retired-host` stays live.
- **G2-R1-05 (medium): agents cannot read job summaries.** Fixed.
    - The gap is in ADR-0020's *Consequences*.
    - `gh run view --help` was checked: its JSON fields hold no summary.
    - The PR proof reads the notice annotations of the `hooks` job through
      `gh api .../check-runs/<job-id>/annotations`. That call and the `detailsUrl` lookup
      were run against PR #4440.
    - The exit criterion names the annotation, and the summary is an operator browser step.
    - The root `CLAUDE.md` row points at `fact_status.py notice --since origin/master`.
- **G2-R1-06 (medium): no entry gate on G1's exit criterion, and no re-measure of
  ADR-0019's criterion.** Fixed.
    - PR 1 has an entry condition on G1's parity table and on the *Notice step failed*
      query, with a stop.
    - PR 1 re-runs `report --days 60` while `precision` still exists.
    - The ADR-0019 *Status* paragraph and the ADR's *Decision* are conditional on that
      figure. The Goal labels three as the `70591c307` figure.
- **G2-R1-07 (medium): per-PR rollbacks ignore the order between the PRs.** Fixed.
  *Rollback* states the reverse order, and what PR 3's revert needs before and after
  PRs 4 and 5. It also covers PR 2's dependence on `lock.parse_lock`, and PR 2b's on the
  lock file.
- **G2-R1-08 (medium): the PR 1 rollback deletes an accepted ADR.** Fixed. PR 1's undo is
  **operator-only**. The operator either sets ADR-0020 to `Rejected` or supersedes it with
  a new ADR, and ADR-0019's *Status* paragraph stays.
- **G2-R1-09 (medium): PR 3 exceeds the size budget, and "cannot split further" is false.**
  Partly fixed, partly refuted.
    - Fixed: PR 2b moves out the whole-tree lint test, the deletion of
      `test_facts_lock_units_lint_clean.py`, and reviewer tier 2.
    - The PR keeps its number, because G3 and G4 cite G2's PR 3 and PR 4.
    - PR 3's counts are restated from measured spans at `70591c307`. The four subcommands
      and their argparse entries span 142 lines of `fact_status.py`. Their CLI tests span
      184 lines of `test_facts_cli.py`.
    - Refuted: `status` cannot leave first. In `scripts/dev/tests/test_facts_cli.py`, eight
      tests of `verify`, `forget` and `reverify` assert through `main(["status", ...])`.
      Examples are `test_verify_then_status_is_clean`, `test_forget_then_status_is_clean`
      and `test_reverify_folds_a_prose_edit_into_the_commit`. `cmd_verify` and
      `cmd_forget` also print "`fact_status.py status` lists the keys" on a usage error.
      Moving `status` out first would rewrite tests and messages that PR 3 deletes.
- **G2-R1-10 (medium): the PR proof relies on report output G1 does not define.** Fixed
  without new code. The proof runs G1's `notice --since <sha>^ --head <sha>` over each
  first-parent master commit since PR 3. It then finds the PR with `gh pr list --search
  <sha>`, and reads the annotations API. A per-commit list in `report.py` would enlarge the
  PR 2 diff that G4's PR 3 builds on.

### Round integration

Findings raised: one.

- **X-21 (medium): many slices run `fact_status.py verify` and commit `docs/facts.lock`
  unconditionally, though G2 deletes both with no ordering against them.** Fixed in G2's
  part. Partly refuted, and the rest is reported to the slices that own the steps.
    - Fixed: *Design* gains *Other slices' lock steps*. It states that G2 adds no
      dependency edge, and that each lock step carries D6's and R4's condition. It also
      gives the check for that condition: `git cat-file -e origin/master:docs/facts.lock`
      after a fetch. At `70591c307` the check exits 0. A path absent from `origin/master`
      exits 128, measured with a missing path.
    - Fixed: *Risks* gains *Another slice runs a lock step after PR 3*. PR 3's body names
      the slices whose plans carry a lock step.
    - Refuted in part: on master after PR 3, a `verify` step cannot recreate the lock.
      `scripts/dev/fact_status.py:main` uses `add_subparsers(dest="cmd", required=True)`,
      and an unknown subcommand exits 2 with `invalid choice`, measured at `70591c307`. Only
      a branch cut before PR 3 can write the lock, and `lock-retired` already covers it.
    - Found beyond the finding: D6's *Verification* runs
      `fact_status.py status | grep -v ' IN '` with no condition. After PR 3 the pipeline
      prints nothing and reads as clean. At `70591c307` it prints every section, because
      `status` prints its grade at the start of the line and `' IN '` never matches.
      `status | awk '{print $1}' | sort | uniq -c` printed 266 `CONVENTION` and 185 `IN`.
    - Reported, not edited here. At this round's first read, five steps had no condition:
      D2.3, D3.2, the H2 step that re-records a section, H7.2e and M1's *Tests and docs*
      line. D2, H2 and M1 added the condition in their own X-21 entries during this round.
      D3.2 and H7.2e still lacked it when this entry was written. The finding did not
      list D3, so D3.2 and D6's *Verification* line go to their owners as notes. D4, H1, H5
      and R2 already carry the condition. M2 and M3 commit only what the deleted hook
      writes, so they need none.

### Round 10

Findings raised: two. Both are fixed, and none is refuted.

- **G2-R10-01 (high): PR 1's entry gate points at a G1 check that G1 replaced, and it
  leaves out two of G1's three exit checks.** Fixed.
    - Confirmed against G1. G1's *Design* section on the notice step's failures replaced
      the step-conclusion query with the `notice-step-failed` artifact. Its *Baseline*
      records 1889 PR runs in 30 days against a 1,000-result cap on run listings. G1's
      *Goal and exit criterion* has three checks: the replay, no CI crash, and delivery.
      The old gate named only the replay's parity half and a query G1 no longer defines.
    - PR 1's entry condition now names G1's three checks one by one. Check 1 requires zero
      unexplained content-form misses and zero replay crashes. Check 2 is the artifact
      query, bounded below by PR 5's merge timestamp, or by a later CI-step PR's merge as
      G1's step 6 says. Check 3 is the delivery sample, with each `capped` run recorded and
      each commit without a PR named.
    - The ADR's *Context* bullet in *Design* and the PR 1 steps now paste all three outputs.
      The stop rule covers any failed check, and a restart moves check 2's lower bound.
    - The gate departs from G1's step 6 on purpose, and PR 1 says so. A miss fixed in
      `notice.py` during the soak passes check 1, but the commit's old CI run carries no
      annotation, so check 3 fails and the window restarts. G1's step 6 waives that
      restart. G2 does not, because check 3 cannot re-run the old runs.
    - The *Risks* entry now names delivery as a third failure mode and the three checks as
      its mitigation. The Round 1 entry G2-R1-06 stays as written, because it is history.
- **G2-R10-02 (medium): the notice replay's window start still depends on the lock after
  PR 2 and PR 3.** Fixed.
    - Confirmed against G1's *Design* section on the replay: the window starts at the last
      lock commit before the `--days` boundary, for the `split` class only. G2's PR 2 kept
      the replay and did not touch the window.
    - PR 2 now returns the window to the plain `--days` boundary and drops the anchor with
      `split`. It spells out which parity rows leave, and which replay keys stay, among them
      the `moved` and `moved_prose_edited` keys G3 reads.
    - *Verification* gains the paired test in `scripts/lib/tests/test_facts_report.py`. A
      fixture with a lock commit well before the boundary starts its replay inside the
      boundary, and a fixture with no lock commit gives the same window.
    - *Tests and docs*, PR 2, names the G1 replay tests that leave or change with parity
      and the anchor. The Design `diff` names the anchor. PR 2's estimate is restated as
      about 120 lines added and 150 deleted, so its hand-written additions stay inside the
      100–200 line guide.

### Round integration-2

Findings raised: one. It is fixed in G2's part, and the G3 part is reported to G3's owner.

- **X2-18 (medium): G2 PR 1's entry condition restates G1's old soak bound, and the bound
  cannot be met under G1's current rules.** Fixed.
    - Confirmed against G1. G1's *Goal and exit criterion* grades every check from one
      moving bound `<ts>`. Check 2 accepts an artifact created before `<ts>` and names
      three kinds created after it. G1's *Steps*, item 6, moves `<ts>` on any change to
      `notice.py` except a mechanical one, on a change to any of the three CI steps, and on
      a fix for a `notice-step-failed` artifact. No `notice.py` fix skips the restart.
    - G2's old check 2 could not be met. It required G1's artifact query to print nothing
      after a bound that moved only on a CI-step change. G1's query in *Verification* has
      no date filter, and an artifact stays in its listing for 90 days. G1 names three
      kinds of artifact after `<ts>` rather than failing them, among them a `setup`-stage
      failure on a network error. Such an artifact has no notice defect to fix, so G2's
      old stop rule demanded a fix PR that could not exist. G1's open question on stale G2
      and G3 text names the same three G2 sentences.
    - PR 1's entry condition cites G1's exit items 1 to 3 and *Steps*, item 6, and
      says that G1's text governs. The stop rule leaves the restart to G1's step 6.
    - The paragraph saying the gate was stricter than G1's soak is gone. G1's step 6 no
      longer waives the restart after a `notice.py` fix, so the departure it described no
      longer exists.
    - The ADR's *Context* bullet in *Design* records every item G1 assigns to the G2
      ADR, and names G1's sections as the list. The *Risks* entry on G1's notice cites G1
      the same way. *Verification*, *Rollback*, *Tests and docs* and *Complexity delta*
      do not restate G1's checks, so they are unchanged.
    - Round 10's entry G2-R10-01 stays as written, because it is history. Its check 2
      bound and its bullet on departing from G1's step 6 describe the text this round
      replaced.
    - Not edited here: G3's *Goal* says G1 PR 3 names `moved` and `moved_prose_edited`,
      and G3 carries a `resolve_base` fallback. G1's *Steps* add the keys in PR 4 and
      `notice.resolve_base` in PR 3. This goes to G3 as a cross-slice note.
