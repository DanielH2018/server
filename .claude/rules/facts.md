---
paths:
  - "CLAUDE.md"
  - "**/CLAUDE.md"
  - ".claude/rules/**"
  - ".claude/skills/**/SKILL.md"
  - ".claude/agents/**"
  - "scripts/lib/facts/**"
  - "scripts/dev/fact_status.py"
---

# Fact support — how an instruction file cites what holds it up

An instruction file is a `CLAUDE.md`, a rule under `.claude/rules/`, a skill's `SKILL.md` or
an agent under `.claude/agents/`. `scripts/lib/facts/citations.py:is_fact_doc` is the one
definition, and the lint, the notice and the report all read it.

Each kind of sentence in an instruction file has one home and one detector. No detector
keeps state between commits: each compares two trees, or a doc against the tree.
`docs/adr/0031-each-kind-of-fact-has-one-stateless-detector.md` records the decision.

| A sentence that | Lives in | Is caught by |
|---|---|---|
| states a value: a count, a port, a timeout | a row under `scripts/docs/doc_values_lib/` | `regen-doc-values`, which rewrites the value at commit time |
| states a behaviour | a test whose body carries `# fact: <doc>#<heading>`, cited as a test node | the test run |
| describes code | a symbol, YAML key or `DECIDED:` marker citation | the diff notice, when a PR moves the atom |
| names a path, symbol or host | the backticked name | the lint, when it does not resolve or a branch removed it |
| records history or a decision | a `**HISTORY —` paragraph, a `# DECIDED:` marker or an ADR | nothing: it cannot go stale |
| tells the reader what to do | the sentence itself | nothing: it cites nothing |

Bind a sentence to its home when you write it. Prose that describes the tree and cites
nothing is the one kind no detector reaches: a change under it draws no row anywhere.
`fact_status.py report` ranks what is left of it.

- **A value row is the only home that repairs the doc.** The hook rewrites the number and
  fails once, like a formatter. `.claude/rules/generated-docs.md` has the steps to add a row.
  It corrects a count and never the list beside it, so name the members of a list rather
  than counting them, or the count owes a row. A value Renovate bumps gets no row: name its
  key instead, as the grammar below says.
- **The notice is advice, not a gate.** CI prints it on a PR as annotations and a job
  summary, and `scripts/dev/repo_guards.py` prints the rows whose prose the branch left
  unedited. Neither fails. `.github/workflows/ci.yml:DECIDED: the notice never blocks a merge`
  holds the ruling, and `docs/target-architecture/docs-guard/g9-blocking-gate.md` has the
  measurement that kept it.
- **Three shapes have no citation.** A path atom needs a slash, so a root-level file such as
  `renovate.json` cannot be cited. A YAML atom is a dotted key, so it cannot select one entry
  of a list. An absence, such as "neither Deployment pushes a heartbeat", has no atom. Hold
  any of the three with a test, or leave it as prose.

## Sections and the citation grammar

A section is the text under one heading up to the next heading of any level, so every
citation has exactly one owning section, keyed `<doc>#<heading text>`. Heading text is not
scanned for citations: a backticked span in a heading names the section, never an atom in it.

The grammar is closed — `scripts/lib/facts/citations.py:FORMS` — and a backticked span is
support only in one of these five shapes:

```text
# a repo path; a directory keeps its trailing slash
k8s/roles/traefik/
# a Python symbol
scripts/lib/kubectl.py:kubectl
# a YAML key
k8s/roles/traefik/defaults/main.yml:traefik_k8s_https_port
# a test node
scripts/lib/tests/k8s/test_kubectl.py::test_asking_for_staging_against_a_prod_kubectl_is_flagged
# a DECIDED: marker, cited by text prefix
scripts/deploy_tools/land_lib/land_tags.py:DECIDED: name the changed keys
```

The examples are fenced because the lint reads every instruction file: unfenced, each one
would be support for this section rather than an illustration of the form. The fence hides
them from this lint only: `tests/repo/docs/test_documented_paths_exist.py::test_every_cited_test_exists`
reads every line of every doc, fenced or not, so the test node in the example names a real
test, and renaming that test fails the guard here.

A path atom hashes existence, so it breaks only when the file goes away; cite a symbol, a YAML
key, a decision marker or a test node to pin content. A citation is support only when it
names a tracked file or a directory holding one, so an untracked path is prose. The reasons
are in the module docstrings under `scripts/lib/facts/`.

The lint refuses a citation that cannot be support. Each rule is an error unless it says
otherwise.

- `rejected-form`: a `file:line` citation. A line number moves under every edit above it, so
  cite the symbol or the marker instead.
- `dir-without-slash`: a directory cited without its trailing slash.
- `unresolved-atom`: a symbol, key, marker or test the tree does not hold.
- `ambiguous-marker`: a marker prefix that matches more than one line. Cite more of its text.
- `one-way-test` (warning): a cited test node whose body carries no `# fact: <doc>#<heading>`
  for the citing section, so the support points one way. The other direction is a test, not
  a lint rule: a `# fact:` comment whose doc or heading is gone fails
  `tests/repo/docs/test_fact_comments_resolve.py::test_every_fact_comment_names_a_section_that_exists`.
- `renovate-pin`: a YAML key whose value Renovate bumps. Every bump would move the atom, which
  puts a notice on every bump PR for a section whose prose the bump cannot change. Cite the
  file as a path atom and name the key in prose. The rule derives the managed keys from
  `renovate.json`'s `customManagers`, plus the built-in github-actions manager, whose default
  file patterns `scripts/lib/facts/pins.py` restates. A claim that must hold for one specific
  version belongs in a test, which the bump PR runs.

Three more rules read the document's shape. `duplicate-heading` fires when two sections of
one doc share a heading, because they collapse onto one key; rename one. `unbalanced-fence`
fires on a fence with no closing marker, which turns the text after it into live prose.
`date-as-verification` fires on "verified" next to a date, because a date cannot show that a
claim still holds.

## Rules that read prose, and the history marker

The notice compares cited atoms only, so a sentence that cites nothing can go stale with no
row to show it. Some lint rules therefore read the prose itself.

- `retired-host` (error) flags a host-shaped word that names no directory under
  `hosts/` holding a `vars.yml`. The rule derives the host prefix from those files, never
  from a list. A word inside a path or a longer name, such as `/srv/artifacts/daniel-box-claude`
  or the domain `daniel-hunter.com`, is not a host mention.
- `version-as-fact` (warning) flags a backticked `image:tag` span whose image the tree pins.
  The pins are the `*_image` keys in role defaults and inventory vars, plus the `image:` lines
  in the Pi's compose templates. A tag no pin carries reads as stale, and the message names
  the pin's variable and file. A tag that matches still restates a value Renovate moves, so
  name the variable instead (`registry` (`registry_k8s_image`)). It stays a warning because
  a Renovate PR cannot edit prose and must not arrive red (#4012). A `generated_from` block
  is skipped, since it regenerates from the pin. A version that is not an `image:tag` span is
  out of scope, such as speedtest's upstream `v1.14.7`: its pin is `latest@sha256`, so the
  tree holds no tag to compare it with.
- `vanished-identifier` (error) runs only under `lint --changed-since <ref>`, which is what
  the `facts-lint-changed` hook passes. It takes the identifier-shaped tokens on the lines the
  branch removed from non-Markdown files: six or more characters holding `_`, `.`, `/` or `-`,
  and not a version or a number. It keeps the ones that no tracked non-Markdown file holds and
  no tracked path ends with. Any section that still names one in backticks is a finding,
  whether or not the branch edited that section. The branch runs from the merge base with
  `<ref>` to the working tree, so a commit that fixes its own sentence stays silent. Versions
  are exempt because `version-as-fact` owns them. To resolve a finding, edit the sentence or
  mark it as history. A name that the tree builds from parts, such as the
  `monitor-bridge-script-` prefix a template joins to each module's name, never appears
  literally, so a doc naming one rendered ConfigMap reads as vanished. Name it as the tree
  spells it.
- Two more classes of removed name are advice, not lint rules. `scripts/dev/repo_guards.py`
  prints them after its guards as a `removed names:` list, and they change no exit code.
  The first is a section of a live doc that names the identifier in backticks: any Markdown
  file that is no instruction file and is outside the archive, the ADRs, the slice plans
  and the generated pages (`scripts/lib/facts/citations.py:is_live_doc`). The second is a
  full-line `#` or `//` comment that names an identifier the branch took out of code, when
  only comments still hold it. A comment has no backticks, so its identifier needs an
  underscore, a dot or two hyphens (`scripts/lib/facts/removed.py:comment_name`).
  `scripts/lib/facts/lint.py:vanished_advice` builds the list. A class becomes an error
  when fewer than one graded row in five is a false alarm;
  `docs/target-architecture/docs-guard/g7-removed-names.md` holds the measured shares.

A paragraph or bullet that opens with `**HISTORY —` is history, and every prose rule skips it.
A sentence such as "it lived on the staging guest until 2026-09-28" is correct exactly because
the thing it names is gone, and the marker says so. A history bullet takes its indented
continuation lines and nested bullets with it; a history paragraph ends at the next blank line.
`scripts/lib/facts/citations.py:HISTORY_MARKER` is the one definition.

## Commands

- `fact_status.py lint [--changed-since <ref>]` reports the citations that cannot be support
  and the prose rules above, and exits 1 on an error rule. `lock-retired` is one of those
  rules: it fires when a stale branch's merge or rebase brings `docs/facts.lock` back, and
  the fix is to take the deletion.
- `fact_status.py report` reads git and the instructions log over `--days` (30 by default).
  The window ends now, or at 00:00 UTC on the date `--until` names. It ranks every section that cites nothing by how often its doc loaded, the most-read
  first. A section with no prose is not ranked: a title, a parent whose text sits under its
  subheadings, a rule's frontmatter, or a section that holds only a `generated_from` block
  (`scripts/lib/facts/citations.py:has_prose`). The counts come from
  `.claude/logs/instructions.log` in the primary checkout and its rotated `.1`, so the span
  the report prints is usually days, not the window, and a checkout with no log ranks every
  section at 0 reads. The freshness reviewer
  (`.claude/agents/homelab-docs-freshness-reviewer.md`) reads this ranking. Next, the report
  ranks the docs delivered as a head by their `form=head` rows, which is the order a trim
  should follow. Then it prints the instruction bytes each session loaded. Last, it replays
  the comment class of the removed-name rule once per first-parent commit in the window
  (`scripts/lib/facts/replay.py:comment_replay`), which calls the function the advice calls,
  `scripts/lib/facts/removed.py:stale_comment_uses`. `--json` prints the
  same as one object. Its `comment_replay.rows` list holds each comment row as its commit, the
  removed name and `path:line` at that commit, which is what G7 step 5's grading reads. The
  replay walks every commit in the window and takes minutes, where the rest takes about a
  second, so `--no-replay` leaves it out for a caller that reads only the log's lists, such as the
  freshness reviewer.
  `scripts/lib/facts/report.py` says where each count comes from.
- `fact_status.py notice --since <ref> [--head <rev>]` lists every section that cites an
  atom the diff moved. The diff runs from the merge base of `<ref>` and the head to the head,
  and the head is the working tree unless `--head` names a revision. It compares the atom
  at the two ends of the diff, never against a recorded hash, so a row means the diff itself
  moved the atom. A row's kind is `moved`, `gone`, `ambiguous` or `unparsable`, and each row
  says whether the diff also edited the section's prose. `--format` takes `text`,
  `markdown`, `github`, `json` or `reread`; `scripts/dev/repo_guards.py` prints `reread`,
  which lists only the sections whose prose the branch left unchanged. The rules for each
  row and each format are on `scripts/lib/facts/notice.py:moved_sections` and
  `scripts/lib/facts/notice.py:render`. The command exits 0 with rows or without, and exits
  2 only when git cannot resolve the merge base.
- `fact_status.py mv <old> <new> [--root <dir>] [--dry-run]` rewrites the references a
  rename left behind. To fix the `removed names:` list after a move, run it once per moved
  file or directory. It rewrites the places that list reads: instruction files and live
  docs outside history and generated blocks, and full-line comments in other tracked
  files. Code and docstrings stay as they are. It refuses when `<old>` is still tracked
  or `<new>` is not. Under a moved directory it rewrites a reference only when the path
  that results is tracked, and it lists the rest as left. `--root` rewrites the Markdown
  under a directory outside the repo, such as a memory store, in place of the repo: such
  a store has no `git diff`, so run it with `--dry-run` first.
  `scripts/lib/facts/renames.py:move` has the rules.

`lint` blocks through two checks. `tests/repo/docs/test_every_section_lints_clean.py` fails CI on
any error rule in any section, except `vanished-identifier`, which needs a range. The
`facts-lint-changed` prek hook lints the sections the branch edits, and runs
`vanished-identifier` over every section. It fires on a commit that touches an instruction
file or any non-Markdown file other than the paths the docs-refresh and eval-run crons commit.
`tests/repo/docs/test_every_section_lints_clean.py::test_the_lint_hook_fires_on_exactly_the_instruction_files`
holds the hook's `files` pattern against `is_fact_doc`.
`docs/adr/0023-fact-support-is-a-diff-notice.md` records why the notice replaced the stored
hashes and their blocking test.
