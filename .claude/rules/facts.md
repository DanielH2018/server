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
definition, and the lint, the notice and the replay all read it.

A section of an instruction file names what holds it up by citing atoms. A section
is the text under one heading up to the next heading of any level, so every citation has
exactly one owning section, keyed `<doc>#<heading text>`. Heading text is not scanned for
citations: a backticked span in a heading names the section, never an atom in it.

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
scripts/lib/tests/test_kubectl.py::test_asking_for_staging_against_a_prod_kubectl_is_flagged
# a DECIDED: marker, cited by text prefix
scripts/deploy_tools/land_lib/land_tags.py:DECIDED: fire on ANY change
```

A test node must carry `# fact: <doc>#<heading>` in its body, or the lint warns that the
support points one way. The examples are fenced because the lint reads every `CLAUDE.md`:
unfenced, each one would be support for this section rather than an illustration of the
form. The fence hides them from this lint only:
`tests/repo/test_documented_paths_exist.py::test_every_cited_test_exists` reads
every line of every doc, fenced or not, so the test node in the example names a real test,
and renaming that test fails the guard here.

A `file:line` citation is rejected: a line number moves under every edit above it. Cite the
symbol or the marker instead.

A path atom hashes existence, so it breaks only when the file goes away; cite a symbol, a YAML
key, a decision marker or a test node to pin content. A citation is support only when it
names a tracked file or a directory holding one, so an untracked path is prose. The reasons
are in the module docstrings under `scripts/lib/facts/`.

A YAML key whose value Renovate bumps is not citable as a YAML atom. Every bump would move the
atom, which puts a notice on every bump PR for a section whose prose the bump cannot change.
Cite the file as a path atom and name the key in prose. The lint's `renovate-pin`
rule derives the managed keys from `renovate.json`'s `customManagers`, plus the built-in
github-actions manager, whose default file patterns `scripts/lib/facts/pins.py` restates. A
claim that must hold for one specific version belongs in a test, which the bump PR runs.

## Rules that read prose, and the history marker

The notice compares cited atoms only, so a sentence that cites nothing can go stale with no
row to show it. Some lint rules therefore read the prose itself.

- `retired-host` (error) flags a host-shaped word that names no file in
  `inventory/host_vars/`. The rule derives the host prefix from the inventory, never
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
- `fact_status.py report` reads git and the instructions log over `--days`
  (30 by default). It ranks every section that cites nothing by how
  often its doc loaded. The counts come from `.claude/logs/instructions.log` in the
  primary checkout and its rotated `.1`, so the span the report prints is usually days, not
  the window. Next, it ranks the docs delivered as a head by their `form=head` rows, which
  `inject-nested-docs.py` writes, rules included. That is the order a trim should follow.
  Then it prints the instruction bytes each session loaded, summed from the
  `bytes=` field `log-instructions.py` ends each row with. A user-level file such as
  `~/.claude/CLAUDE.md` counts. A subagent's row and a row without the field do not. The
  harness fires no InstructionsLoaded event for the auto-memory `MEMORY.md` or the output
  style, so `session-health.py` writes a `session_start` row for each at SessionStart. A
  compaction reloads `MEMORY.md` without the event as well, so the hook writes a `compact` row
  for it then. The output style sits in the system prompt and gets no `compact` row.
  Last, it prints the *Notice replay*. The replay runs `notice` once per first-parent commit
  in the window, with the parent as the base, and counts the rows by kind.
  `scripts/lib/facts/replay.py:notice_replay` has the counts.
  `--json` prints the same as one object. Under `notice_replay`, `moved` counts the replay's
  `moved` rows, and `moved_prose_edited` counts the ones whose commit also edited the
  section's prose. `content_notices` names the commit and section of each row whose atom is
  not a path atom.
- `fact_status.py notice --since <ref> [--head <rev>]` lists every section that cites an
  atom the diff moved. The diff runs from the merge base of `<ref>` and the head to the head,
  and the head is the working tree unless `--head` names a revision. It compares the atom
  at the two ends of the diff, never against a recorded hash, so a row means the diff itself
  moved the atom. A row's kind is `moved`, `gone`, `ambiguous` or `unparsable`, and each row
  says whether the diff also edited the section's prose. `--format` takes `text`,
  `markdown`, `github` or `json`. The rules for each row and each format are on
  `scripts/lib/facts/notice.py:moved_sections` and `scripts/lib/facts/notice.py:render`. The
  command exits 0 with rows or without, and exits 2 only when git cannot resolve the merge
  base. `docs/target-architecture/docs-guard/g1-diff-notice.md` is the plan that built it.

`docs/adr/0023-fact-support-is-a-diff-notice.md` records why the notice replaced the stored
hashes and their blocking test. A moved atom gets a notice and never blocks a merge. `lint`
still blocks, through two checks.
`tests/repo/test_every_section_lints_clean.py` fails CI on any error rule in any
section, except `vanished-identifier`, which needs a range. The `facts-lint-changed` prek hook
lints the sections the branch edits, and runs `vanished-identifier` over every section. It
fires on a commit that touches an instruction file or any non-Markdown file other than the
paths the docs-refresh and eval-run crons commit.
`tests/repo/test_every_section_lints_clean.py::test_the_lint_hook_fires_on_exactly_the_instruction_files`
holds the hook's `files` pattern against `is_fact_doc`.
