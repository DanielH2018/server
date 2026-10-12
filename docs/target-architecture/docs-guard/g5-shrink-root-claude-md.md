# G5 — Shrink root CLAUDE.md

Status: PLANNED

Depends on: [G2](g2-retire-lock.md)

Unblocks: none

## Goal and exit criterion

The root `CLAUDE.md` loads whole into every session. A fan-out batch also passes it as text to
every phase that runs on held settings. After G5 the root holds directives and pointers.
Mechanism prose moves to the place that loads when the mechanism is in play:

- a path-scoped `.claude/rules/` file;
- a skill;
- a `docs/` page;
- the `--help` text of the tool the prose restated.

G5 keeps every heading. It moves text out of sections; it does not delete sections. G5 also
keeps every citation in a `CLAUDE.md`, because only a `CLAUDE.md` citation is checked (see
*Baseline*).

**Exit criterion.**

- `wc -l CLAUDE.md` prints fewer than 200 lines, down from 309.
- `wc -m CLAUDE.md` prints at most 17,000 characters, down from 25,903. Lines alone could be
  met by joining lines, so the character figure keeps the change honest.
- Every heading present at `70591c307` is still present. Pointers in code, skills and
  setup roles name these headings.
- Every in-tree citation the root held just before each G5 PR is still cited by some
  `CLAUDE.md` after it, or that PR's body lists it as deliberately dropped, with the reason.
  The baseline is the root before each PR, not at `70591c307`, because G2 lands first and
  replaces a *Where to Look* row.
- `MIN_GRADED_PARAGRAPHS` in `test_claude_md_paragraphs_have_one_home.py` is at least 10 and
  below the graded count the last G5 PR measured.
- `prek run --all-files` passes.
- `uv run pytest` passes. That run includes `test_claude_md_paragraphs_have_one_home.py`,
  which proves that no paragraph still in the root is copied verbatim into another doc. It
  cannot see a paragraph that has left the root. The one-home proof for a moved paragraph is
  the PR-body check in *Verification*: one `git grep` that prints exactly one hit, in the
  destination the per-section list names.
- A held fan-out phase gets `.claude/rules/` text appended to its prompt (PR 0): the reviewer
  and each resumed phase get the rules the batch's range matches, and the implement phase
  after a red phase gets every rule.
- [G4](g4-drop-size-test.md)'s report prints the root line count under 200, if G4 has
  landed.

## Baseline at `70591c307`

**Size.** `CLAUDE.md` is 309 lines and 25,903 characters (`wc -l -m CLAUDE.md`). The
section line spans, taken from `grep -n '^#' CLAUDE.md`, are:

| Section | Lines | Kind |
|---|---|---|
| title | 2 | — |
| Project Overview | 12 | facts, partly restating `docs/reference/` |
| Directory Structure | 15 | pointer tree, plus one paragraph about `roles/containers/` |
| Where to Look (task → start here) | 23 | pointers. G2 replaces one row |
| Adding a New Service | 27 | pointers to two skills, plus two facts |
| Common Commands | 18 | directives, plus `deploy.sh` mechanics |
| Checking a k8s change without deploying it | 6 | pointer |
| After a PR Merges — Pull, Deploy, Verify | 21 | resident directive, plus plane mechanics |
| The procedure | 16 | pointer to `land-after-merge`, plus restated rules |
| Working alongside other sessions | 7 | directive |
| When to wait | 24 | resident directive list |
| Shell Commands — Shape Them to Auto-Approve | 11 | resident directive |
| `kubectl` — what actually decides | 7 | resident directive |
| Claude Tooling in This Repo (`.claude/`) | 12 | pointers, plus restated tool usage |
| Review & Memory Hygiene (making judgment cumulative) | 29 | directives, plus marker mechanics |
| Parallel Claude Sessions | 16 | directives, plus banner mechanics |
| Secrets Management | 29 | three resident safety rules, plus SOPS mechanics |
| Pre-commit Hooks | 15 | one directive, plus hook mechanics |
| Python & Tests | 19 | pointer, plus restated `pyproject.toml` facts |

**Who reads it.**

- The harness loads it at session start.
- A fan-out batch in this repo runs every phase after the implementer on held settings,
  `claude --setting-sources user` (`scripts/dev/fanout_lib/review/review.py:Pipeline._held_settings`).
  The reviewer, the implement phase that follows a red phase, and every phase that resumes
  it get the start-time root as text (`Pipeline._reviewer`, `Pipeline._red_claude_md`).
- The `DECIDED:` marker in the `held_hooks.py` module docstring that starts *in this repo's
  batch, every phase that starts after the implementer ran* records a probe from
  2026-10-09. The flag drops the project `CLAUDE.md` and the project skills from a fresh
  session, and those phases lose `.claude/rules/` as well. Text that G5 moves into a rule or
  a skill therefore never reaches a held phase today.
- An inference from the code, not yet confirmed by a run: `pointed_settings` keeps the
  project `bash-pretool` hook, and its `inject-nested-docs` arm reads `.claude/rules/` from
  the checkout root of each path a Bash command names (`inject-nested-docs.py:docs_for`). A
  held phase would then get a rule through Bash, and only through Bash: on any named path for
  `inject-nested-docs.py:READ_RULES`, and on a write for every other rule. A Read or an Edit
  gets nothing.
- `fact_status.py report` ranked `CLAUDE.md#…` sections at 183 doc reads in the 2.5 days of
  instructions log it covered, the most-read doc after monitor-bridge's.
- `uv run python evals/ablate.py rank --sections CLAUDE.md`, run against the instructions
  log on 2026-10-11, counts the sessions naming each `## ` section, over 1,243 transcripts:

| Section | Sessions |
|---|---|
| Where to Look | 18 |
| After a PR Merges | 14 |
| Secrets Management | 13 |
| Common Commands | 10 |
| Shell Commands | 9 |
| Project Overview, Review & Memory Hygiene, Parallel Claude Sessions | 8 each |
| Claude Tooling, Python & Tests | 7 each |
| Pre-commit Hooks | 5 |
| Directory Structure, Adding a New Service | 4 each |

**Citations, and what checks them.** The root holds 53 in-tree citation occurrences, which
are 41 distinct spans. The count comes from `scripts/lib/facts/citations.py:parse_citations`
filtered by `citations.in_tree`, run section by section and over the whole file.

- By section: 21 in *Where to Look*, 8 in *Secrets Management*, 4 each in *Adding a New
  Service* and *The procedure*, 3 each in *Review & Memory Hygiene* and *Python & Tests*, and
  10 across seven other sections.
- Four are not plain paths, and all four stay in the root:
    - `deploy_git.py:_CI_NO_VERDICT_CONCLUSIONS` and the `test_cancelled_is_declared_no_verdict`
      node, in *The procedure*;
    - `deploy_changes.py:_BROAD_MANUAL_PREFIXES`, in *When to wait*;
    - `block-footguns.py:issue_create_by_hand_problem`, in its *Where to Look* row.
- `test_documented_paths_exist.py` (`ansible/tests/repo/`) checks three shapes in every doc
  that `_helpers.py:discover_docs` lists, which is every `CLAUDE.md` plus `.claude/` and
  `docs/`. The three shapes are a `file:line` citation, a bare `ansible/tests/…py` path, and
  a test node id, which joins a test file and a test name with a double colon. Its docstring
  says "Only citations carrying a line number count", plus those two test shapes.
- A bare repo path or a `file.py:symbol` atom is checked only inside a `CLAUDE.md`. There the
  lint's `unresolved-atom` rule resolves it, over `citations.repo_docs`, which reads
  `git ls-files -- CLAUDE.md **/CLAUDE.md`. G1's notice and G2's whole-tree lint read the same
  set. The same citation in a skill, a rule or a `docs/` page is checked by nothing.

**Tests that pin root text.**

- `test_ci_cancelled_is_not_a_verdict.py` (`ansible/tests/deploy/`) asserts that the root
  holds `_CI_NO_VERDICT_CONCLUSIONS` and the literal ``` `docs/landing.md` owns the rule ```.
  Its `test_cancelled_is_declared_no_verdict` carries `# fact: CLAUDE.md#The procedure`.
- `test_claude_md_paragraphs_have_one_home.py` (`ansible/tests/repo/`):
    - `test_no_root_paragraph_is_also_in_a_skill_rule_or_docs_page` fails when a root
      paragraph of `MIN_CHARS` (240) characters or more also appears verbatim in another doc
      that `discover_docs` lists. It compares only paragraphs still in the root.
    - `test_the_census_grades_the_root_file` asserts at least `MIN_GRADED_PARAGRAPHS` (20)
      graded blocks. The test's own `paragraphs()` finds 28. A heading has no blank line
      after it, so it joins its first paragraph's block, and trimming an opener can take
      that block under 240.
    - Eleven blocks sit between 240 and 434 characters: 240 (*Do not ask permission*), 244
      (*It deploys `HEAD`*), 245 (*Hand the merge*), 285 (the *Python & Tests* opener), 288
      (the *Where to Look* intro), 292 (*kubectl*), 320 (*A failing run*), 322 (*The tick
      pulls*), 366 (*A `cancelled` CI run*), 375 (*Checking a k8s change*) and 434
      (*Suites*). The 651-character `roles/containers/` block leaves the root, and the
      466-character `containers_list` block shrinks to one line.
- The `root-claude-md-has-no-self-disclaiming-prose` row of `test_census_rows_text.py`
  (`ansible/tests/repo/`).
- `test_checksum_annotations_documented.py` (`ansible/tests/k8s/`). It checks any
  `checksum/<name>` the root names, and skips when the root names none.

**Headings named from elsewhere.** These must survive, verified with
`git grep -n "When to wait\|After a PR Merges\|Secrets Management\|Python & Tests\|Common Commands\|Adding a New Service\|Pre-commit Hooks\|Review & Memory Hygiene\|Working alongside"`.

- *When to wait*:
    - `.claude/plugins/deck/hooks/register.tsx`;
    - `scripts/diagnostics/probe_lib/landing_blockers.py`;
    - `scripts/deploy_tools/land_lib/outcome.py`;
    - `scripts/dev/fanout_lib/brief.py`;
    - `ansible/roles/setup/claude_code/defaults/main.yml`;
    - `ansible/roles/setup/renovate_agent/templates/prompt.txt.j2`;
    - `ansible/roles/setup/deploy_ui/files/deploy_ui_writes.py`.
- *After a PR Merges — Pull, Deploy, Verify*: the `deploy` and `land-after-merge` skills.
- *Secrets Management*: the `add-secret` skill and `ansible/roles/setup/sops_setup/CLAUDE.md`.
- *Review & Memory Hygiene*: `scripts/docs/reference/decisions.py`, in a string the
  generator renders.
- *Python & Tests*: several test docstrings in `ansible/tests/`.
- *Common Commands*: `ansible/README.md`.
- *Adding a New Service*: the `new-container` skill.

Three of these are non-Markdown setup-role files, and an edit there re-applies a role, so
G5 renames no heading rather than chase the pointers. A Markdown file is different:
`deploy_changes.py:is_doc` classifies any `.md` path as documentation that reaches no host,
and `services_from_changed_paths` tests it ahead of every plane branch.

**Pointers to section content.** A pointer can name a heading and also lean on what the
section says. The sweep command in *Verification* prints 125 such lines outside the root,
`docs/archive/`, `docs/target-architecture/` and `docs/facts.lock`. Examples:

- `ansible/tests/setup/test_setup_cron_roles_have_a_contract.py` cites the *Where to Look*
  cron row in an assertion message.
- `docs/break-glass.md` cites *Project Overview* for "Ansible runs from daniel-box".
- `ansible/tests/k8s/test_uncovered_roles_justifications_resolve.py`,
  `scripts/diagnostics/tests/test_probe_registry.py` and `scripts/validate/asset_pins.py`
  cite the non-vacuity rule in *Python & Tests*.
- `scripts/secrets_mgmt/consumers.py` cites *Common Commands*.
- The `add-secret` skill says "CLAUDE.md *Secrets Management* has why".
- Two are already stale: `scripts/lib/invocation_sites.py` and `scripts/lib/render_guard.py`
  cite *Directory Structure* for the `sys.path` bootstrap, which `.claude/rules/python-layout.md`
  holds. Nothing catches this drift.

**Destinations that exist, and their limits.**

- `.claude/rules/secrets.md` is 18 lines, with globs for `ansible/vars/**` and
  `ansible/inventory/**`. `inject-nested-docs.py:READ_RULES` also injects it on a read. Its
  opening paragraph says the root covers the SOPS workflow and that the file adds only
  path-specific detail. `ansible/.sops.yaml` matches neither glob.
- `.claude/rules/docker.md` is 44 lines, for `ansible/roles/containers/**`.
- `.claude/rules/k8s-templates.md` is 18 lines, for k8s `templates/` and `files/`.
- `.claude/rules/python-layout.md` is 10,448 characters. That is over the hook's 7,500-
  character payload, so it already arrives as a head, and G5 adds nothing to it. Its `paths:`
  cover `scripts/**`, `ansible/tests/**`, `ansible/filter_plugins/**`, role `tests/**` and
  `.claude/hooks/**`, but not alert-rule templates.
- `docs/python-code-organization.md`, `docs/secret-rotation.md`, `docs/deploying.md`,
  `docs/landing.md`, `docs/claude-tooling.md`, `docs/claude-shell-permissions.md` and
  `docs/adr/index.md` all exist.
- The `deploy` skill already holds the lock mechanics and the three-mode table for `prek`,
  `--check` and `--dry-run`. `docs/deploying.md` already lists the tree-lock holders.
- The skills `deploy`, `new-k8s-service`, `land-after-merge`, `add-secret` and
  `worktree-cleanup` exist under `.claude/skills/`.

**Other slices that edit the same root sections.**

- D2.3 adds one sentence on flux-owned services to *After a PR Merges*.
- D3.2 adds one sentence on the cluster secrets file to *Secrets Management*.
- R4 PR 2 edits *Adding a New Service*, and R4 PR 7 removes the manifests-role row and the
  config-restart row from *Where to Look*.
- D6.8 rewrites *After a PR Merges* and *When to wait*. `d6-delete-old-path.md`, in its risk
  *Coordination with G5 and R4*, already says D6.8 runs after G5 if G5 has started.
- H7.15 drops `-e target=daniel-pi` from the host list in *Project Overview*.
- H1.9 edits `CLAUDE.md` prose that names `host_vars/daniel-box.yml` as the service list.

## Design

**A classification rule, applied per paragraph.** A paragraph stays resident when a
session needs it before it touches any file that would trigger a rule. A tool call such as
`ExitWorktree` or `deploy.sh` triggers no rule. Everything else moves to the narrowest place
that loads when it applies:

1. a rule whose `paths:` glob matches the files the mechanism lives in;
2. the skill that runs the procedure;
3. the `docs/` page that owns the reasoning;
4. the `--help` of the tool whose output the paragraph restated.

Each moved paragraph has exactly one destination. Where a destination already holds the
text, the root copy is cut, and the per-section list says "cut" and names that owner.

**Citations stay in a `CLAUDE.md`.** A citation is a pointer, and only a `CLAUDE.md`
citation is checked. G5 therefore moves only prose that cites nothing.

- Every in-tree citation in a trimmed paragraph stays in the root, in the pointer sentence
  that replaces the paragraph. A destination may name the same path as well, unchecked.
- A citation may leave the root only when another `CLAUDE.md` still cites it, or when the PR
  body lists it as deliberately dropped, with the reason.
- The four non-path atoms in *Baseline* stay in their sections.
- The citations that would otherwise travel with a moved paragraph, and so stay in the root:
    - `ansible/inventory/host_vars/daniel-pi.yml`, from the `roles/containers/` paragraph;
    - `ansible/.sops.yaml`, `ansible/bootstrap.yml` and `ansible/secret_rotation.yml`, from
      the secrets mechanics;
    - `scripts/dev/new_k8s_service.py`, `ansible/templates/`, `ansible/deploy.yml` and
      `ansible/filter_plugins/toposort.py`, from *Adding a New Service*;
    - `.github/workflows/ci.yml`, from *Python & Tests*.
- One planned deliberate drop: `docs/archive/k3s-migration/`, in the migration-history
  clause of *Project Overview*. It points at history, and the archive is not a place a
  session acts from.
- Widening `citations.repo_docs` to rules and skills was rejected here. It changes the
  interface G1 and G2 build on, and G5 does not need it if citations stay put.

**Resident sentences.** These stay on the classification test, word for word or compressed
to one line:

- the merge-and-verify directive, and its override of the harness's no-merge default;
- *When to wait*, every condition;
- the three secrets safety rules: never decrypt, `git diff` of `secrets.yml` prints
  plaintext, and never commit plaintext;
- a host that cannot decrypt yet fails at the secret-load `pre_task`, with the pointer to
  the `add-secret` skill and `ansible/bootstrap.yml`;
- the shell shapes that auto-approve;
- the read-only `kubectl` service account;
- run Ansible through `uv run`, deploy through `deploy.sh`, and `deploy.sh` deploys `HEAD`,
  not the working tree;
- one worktree per session, and the worktree name sets the branch;
- never pass `discard_changes` to `ExitWorktree`;
- a new check ships with a proof it can go red, a named member, and a measured transport;
- the rule to file a finding rather than remember it.

**The target per section.** The total is about 192 lines. Each destination is a file already
in the tree.

- **title**: 2 → 2.
- **Project Overview**: 12 → 8.
    - Stays: the hosts list and the `docs/reference/services.md` pointer.
    - Cut: the migration-history clause, with its deliberate drop above, and the count
      caveat. `docs/reference/services.md` owns the count.
- **Directory Structure**: 15 → 13.
    - Stays: the tree, and one line naming `roles/containers/` as the Pi's roles, with
      `ansible/inventory/host_vars/daniel-pi.yml` as the source of truth.
    - Moves: the `roles/containers/` paragraph, to `.claude/rules/docker.md`.
- **Where to Look**: 23 → 22.
    - Stays: one pointer per row, each with its citations.
    - Moves: the claim mechanics in the backlog row, to `findings.py claim --help`.
- **Adding a New Service**: 27 → 12.
    - Stays: default to k3s; one line per skill, keeping `scripts/dev/new_k8s_service.py` and
      `ansible/templates/`; the ordering fact as one line keeping `ansible/deploy.yml` and
      `ansible/filter_plugins/toposort.py`.
    - Moves: the stale-Secret-key fact, to `.claude/rules/k8s-templates.md`, which loads on
      a `templates/secret.yaml.j2` edit.
- **Common Commands**: 18 → 10.
    - Stays: `uv run`, `deploy.sh`, deploys `HEAD`, and the exit-code pointer to
      `docs/deploying.md`.
    - Cut: lock interleaving. `docs/deploying.md`, *Who holds the tree lock*, owns it.
- **Checking a k8s change**: 6 → 3.
    - Stays: one sentence on which mode reaches an API server.
    - Cut: the rest. The `deploy` skill's three-mode table owns it.
- **After a PR Merges**: 21 → 14.
    - Stays: the directive, the override, and the pointer to
      `ansible/roles/setup/gitops_deploy/CLAUDE.md`.
    - Cut: the plane explanation. That file's *Safety* section owns it.
- **The procedure**: 16 → 8.
    - Stays: the `land-after-merge` pointer, the `_CI_NO_VERDICT_CONCLUSIONS` sentence, the
      literal ``` `docs/landing.md` owns the rule ```, and the test node.
    - Cut: the restated rules. `docs/landing.md` owns them.
- **Working alongside other sessions**: 7 → 4.
    - Cut: the exit codes. `land.sh --help` prints them.
- **When to wait**: 24 → 20.
    - Stays: every condition and `_BROAD_MANUAL_PREFIXES`, compressed, and `probe.py landing`
      named as the check that reads them.
- **Shell Commands**: 11 → 9, and **`kubectl`**: 7 → 5.
    - Cut: the detail. `docs/claude-shell-permissions.md` owns it.
- **Claude Tooling**: 12 → 7.
    - Moves: probe usage, to `docs/claude-tooling.md`.
- **Review & Memory Hygiene**: 29 → 11.
    - Stays: the `# DECIDED:` directive with its `.claude/skills/homelab-review/SKILL.md`
      citation; the artifact rule as one line keeping `docs/adr/` and
      `docs/claude-agent-user.md`; the file-a-finding rule.
    - Moves: the marker form and the grep example, to `docs/adr/index.md`.
    - Moves: the closing flags, to `findings.py close --help`.
- **Parallel Claude Sessions**: 16 → 11.
    - Stays: one worktree per session; the naming rule as one line; `discard_changes`.
    - Moves: the banner derivation, to `docs/claude-tooling.md`.
- **Secrets Management**: 29 → 16.
    - Stays: the three safety rules word for word; the cannot-decrypt pointer; one line each
      for `ansible/.sops.yaml` and for rotation, keeping `ansible/secret_rotation.yml` and
      `docs/secret-rotation.md`.
    - Moves: the `.sops.yaml` mechanics and the `grep -c` key check, to
      `.claude/rules/secrets.md`.
- **Pre-commit Hooks**: 15 → 6.
    - Stays: the suite-is-not-in-prek directive and the untracked-root-file rule.
    - Cut: the hook history. `prek.toml`'s own comments hold it. G5 confirms each fact is
      there before cutting it.
- **Python & Tests**: 19 → 11.
    - Stays: the red-proof directive as one line, the `.claude/rules/python-layout.md`
      pointer, and the `.github/workflows/ci.yml` sentence.
    - Moves: the uv virtual-project facts, to `docs/python-code-organization.md`.
    - Cut: the suite list. The `testpaths` comment in `pyproject.toml` owns it.

**Restated tool output moves to the tool.** Where a paragraph restates what a command
prints, G5 confirms the command prints it before cutting the paragraph. If the command does
not, G5 adds the sentence to that subcommand's argparse `help=` or `epilog` in the same PR, and
names the tool's help in the root. The candidates are `findings.py claim`, `findings.py
close`, `land.sh --help` and `probe.py --list`.

**Held fan-out phases get the rules their diff matches.** A held phase gets the root as text
and no rules or skills, so prose moved into a rule would leave those phases. PR 0 closes
that before any text moves.

- At start, the pipeline reads every `.claude/rules/*.md` from `SOURCE_ROOT`, as it reads
  `project_claude_md`, and parses each file's globs with `inject-nested-docs.py:rule_globs`.
- Before the reviewer and each resumed phase, it appends the text of every rule whose globs
  match a path in the batch's range, from its base to its head. That range holds the
  implementer's edits, so it selects the rules for what the phase reviews or fixes.
- The implement phase after a red phase gets every rule. Its range, `base..red`, holds only
  the red author's tests, so it would select `python-layout.md` and miss the templates or
  secrets the implementer edits next. All seven rules total 28,585 characters at
  `70591c307` (`wc -m .claude/rules/*.md`), about 7,000 tokens, once per batch.
- The helper lives in `held_hooks.py`, which owns what a held phase gets. `review.py` calls
  it where it appends `project_claude_md`.
- Skills stay out of reach of held phases. G5 moves into a skill only procedure text that an
  interactive session follows, such as deploying or adding a secret. The land phase already
  carries its whole `land.sh` command.

**The two-writer rule (C4), applied to prose.** C4's literal scope, a `containers_list`
entry, is not touched. Its principle applies to each paragraph: one paragraph, one home, per
commit. A PR that moves a paragraph deletes it from the root in the same commit.
`test_claude_md_paragraphs_have_one_home.py` catches a root paragraph of 240 characters or
more that is still copied. The PR-body `git grep` check catches a moved paragraph copied
into two destinations.

**Ordering with other slices.** PR 4 always runs. It does not wait for D or H slices.

- Whichever PR lands second rebases onto the other's text. Neither waits.
- A slice that lands after G5 adds a pointer to the trimmed section, not prose. D2.3 and
  D3.2 each add one sentence, which fits that rule.
- D6.8 rewrites *After a PR Merges* and *When to wait* as they stand when it runs, as
  `d6-delete-old-path.md` already plans.

```diff
 CLAUDE.md                          309 lines -> about 192; 25,903 chars -> at most 17,000
 .claude/rules/docker.md            + roles/containers/ paragraph
 .claude/rules/k8s-templates.md     + stale Secret keys after a key is removed
 .claude/rules/secrets.md           ~ opening paragraph; + ansible/.sops.yaml glob
                                    + .sops.yaml scope, the grep -c key check
 docs/adr/index.md                  + the marker form and grep example
 docs/claude-tooling.md             + SessionStart banner derivation, probe usage
 docs/python-code-organization.md   + uv virtual-project facts, if not already there
 scripts/dev/findings.py (help text, if missing)   + claim and close semantics
 scripts/dev/fanout_lib/review/held_hooks.py       + rules matching a held phase's range
 scripts/dev/fanout_lib/review/review.py           ~ appends them beside project_claude_md
 ansible/tests/repo/test_claude_md_paragraphs_have_one_home.py   ~ MIN_GRADED_PARAGRAPHS
 docs/, .claude/, scripts/, ansible/tests/   ~ pointers whose content moved
```

## Steps, as PRs

Each PR keeps every heading and runs the checks in *Verification* before push:

- `uv run python scripts/dev/fact_status.py lint --changed-since origin/master`;
- `uv run pytest ansible/tests/repo ansible/tests/deploy ansible/tests/k8s`;
- the citation diff, the graded count, the pointer sweep and the one-home check.

The order puts PR 0 first, because it must land before any text moves. PRs 1 to 4 follow the
order below. The ranking in *Baseline* gives no reason to reorder them: each PR mixes sections
with high and low counts.

0. **Held phases get matching rules** (about 80 diff lines).
    - Before the PR, check the Bash-arm inference. Read the instructions log for the last
      fan-out batch. `log-instructions` is a pointed project hook too, so a held-phase
      session that ran Bash on a governed path logs a `bash_path_match` row. Record in the PR
      body whether such a row exists. PR 0 runs either way. A missing row means PR 0 is the
      only way a rule reaches a held phase.
    - `held_hooks.py` and `review.py` change as *Design* says.
    - The tests join `test_fanout_review_red_green.py` in `scripts/dev/tests/`, beside its
      held-settings assertion. `test_fanout_review.py` is at 464 lines of the 500-line cap.
    - The red-proof pair: a range touching `ansible/vars/x.yml` puts `secrets.md`'s text in
      the reviewer's appended prompt, and a range touching only `docs/x.md` puts no rule
      text there.
    - A third test: the implement phase after a red phase carries every rule's text.
1. **Secrets, Pre-commit Hooks, Python & Tests** (about 120 diff lines; the root loses about
   30 lines).
    - Moves to `.claude/rules/secrets.md` and `docs/python-code-organization.md`.
    - Rewrites the opening paragraph of `.claude/rules/secrets.md`. It names the root as the
      home of the safety rules and this file as the home of the `.sops.yaml` and key-check
      mechanics.
    - Adds `ansible/.sops.yaml` to that rule's `paths:`. The file is tracked, so
      `test_rules_globs_match_a_tracked_file.py` (`.claude/hooks/tests/`) still passes.
    - The three secrets safety rules stay word for word, because a session can run
      `git diff` without naming the file.
2. **Project Overview, Directory Structure, Adding a New Service, Common Commands, Checking a
   k8s change** (about 140 diff lines; the root loses about 32).
    - Moves to `.claude/rules/docker.md` and `.claude/rules/k8s-templates.md`. The rest is
      cut to pointers at the `deploy` skill and `docs/deploying.md`.
3. **Where to Look, Claude Tooling, Review & Memory Hygiene, Parallel Claude Sessions**
   (about 150 diff lines; the root loses about 29).
    - Moves to `docs/claude-tooling.md`, `docs/adr/index.md`, and `findings.py` help text
      where it is missing.
    - `scripts/docs/reference/decisions.py` quotes the *Review & Memory Hygiene* heading, and
      the heading stays. If its rendered string says the root defines the marker form, the
      sweep retargets it at `docs/adr/index.md`, and the docs-refresh cron regenerates the
      page.
4. **After a PR Merges, The procedure, Working alongside, When to wait, Shell Commands,
   `kubectl`** (about 110 diff lines; the root loses about 26).
    - Cuts to pointers at the `land-after-merge` skill, `docs/landing.md` and
      `docs/claude-shell-permissions.md`.
    - Keep `_CI_NO_VERDICT_CONCLUSIONS` and the literal ``` `docs/landing.md` owns the rule ```
      in *The procedure*.
    - PR 4 runs whether or not a D or H slice has work open on these sections. The
      *Ordering with other slices* rule in *Design* settles any overlap.

The arithmetic is 309 − 30 − 32 − 29 − 26 = 192. PRs 1 to 3 alone reach about 218.

## Verification

Exit criterion:

```bash
wc -l -m CLAUDE.md
git show 70591c307:CLAUDE.md | grep '^##* ' | sort > /tmp/g5-headings-before.txt
grep '^##* ' CLAUDE.md | sort | comm -23 /tmp/g5-headings-before.txt -
uv run pytest -q -p no:cacheprovider ansible/tests/repo ansible/tests/deploy ansible/tests/k8s
prek run --all-files
uv run --no-sync python .claude/hooks/inject-nested-docs.py --report | tail -1
```

- `wc` prints fewer than 200 lines and at most 17,000 characters.
- `comm` prints nothing: no heading was lost. One line it may print is
  `# One-time: install uv …`, which is a shell comment inside the *Python & Tests* fence,
  not a heading. PR 1 may drop that fence.
- The tests and hooks pass.
- The report's last line shows the root under 200 lines, when G4 is in place.

**Citation diff, every PR.** These commands print each in-tree citation the PR removed from
the root:

```bash
git fetch origin master
git show origin/master:CLAUDE.md > /tmp/g5-root-before.md
uv run --no-sync python -I -c '
import sys; sys.path.insert(0, "scripts")
from pathlib import Path
from lib.facts import citations as c
tracked = c.tracked_files(Path("."))
cites, _ = c.parse_citations(Path(sys.argv[1]).read_text())
print(*sorted({x.raw for x in cites if c.in_tree(x, tracked)}), sep="\n")
' /tmp/g5-root-before.md > /tmp/g5-cites-before.txt
```

Run the same Python on `CLAUDE.md` into `/tmp/g5-cites-after.txt`, then
`LC_ALL=C comm -23 /tmp/g5-cites-before.txt /tmp/g5-cites-after.txt`. Python's `sorted()`
orders by code point, and `LC_ALL=C` makes `comm` compare the same way. Each line `comm`
prints is either still cited by another `CLAUDE.md`, shown by
`git grep -l -F '<citation>' -- '*CLAUDE.md'`, or listed in the PR body as deliberately
dropped, with the reason.

The figures in this section are values at `70591c307`: 41 distinct citations here, 28 graded
blocks and 125 sweep lines below. G2 lands before G5 and changes the root, so PR 0 re-measures
all three and records them in its PR body.

**Graded count, every PR.** This prints the count the non-vacuity floor reads, using the
test's own split:

```bash
uv run --no-sync python -I -c 'import sys; sys.path[:0] = ["scripts", "ansible/tests", "ansible/tests/repo"]; from test_claude_md_paragraphs_have_one_home import paragraphs; print(len(paragraphs(open(sys.argv[1]).read())))' CLAUDE.md
```

- At `70591c307` it prints 28.
- The projection from the block lengths in *Baseline*: PR 1 about 26, PR 2 about 20, PR 3
  about 19, PR 4 about 15.
- The PR body prints the measured count. When a PR takes the count under
  `MIN_GRADED_PARAGRAPHS`, the same PR sets the constant to the measured count minus 2 and
  records the measurement in the PR body. The floor never drops below 10. A broken split
  yields one block for the whole file, so a floor of 10 still catches it.

**Pointer sweep, every PR.** This lists the pointers outside the root that name the root or
one of its sections:

```bash
git grep -n -E "(repo-root|root|repo) .?CLAUDE\.md|CLAUDE\.md.{0,8}(Project Overview|Directory Structure|Where to Look|Adding a New|Common Commands|Checking a k8s|After a PR Merges|The procedure|Working alongside|When to wait|Shell Commands|Claude Tooling|Review & Memory|Parallel Claude|Secrets Management|Pre-commit|Python & Tests)" -- ':!CLAUDE.md' ':!docs/archive/' ':!docs/target-architecture/' ':!docs/facts.lock'
```

- At `70591c307` it prints 125 lines.
- For each line about a section the PR trims, check whether the content it leans on moved.
  A pointer that only names a heading stays valid.
- Retarget a pointer whose content moved in the same PR when the file is any Markdown file
  or sits under `docs/`, `.claude/`, `scripts/` or `ansible/tests/`. A Markdown file is safe
  anywhere, setup roles included, because `deploy_changes.py:is_doc` routes it to no plane.
- The sweep reads files outside the root. Inside the root, each PR also greps for bold
  cross-references to the sections it trims, such as "see **Python & Tests**" in *Common
  Commands*, and repoints any whose content left. At `70591c307`,
  `grep -n -E 'see \*\*[A-Z]' CLAUDE.md` prints two, in *Common Commands* and *Pre-commit
  Hooks*, and both name *Python & Tests*, which PR 1 trims.
- Leave every other file under `ansible/` unedited, including role `files/`, `tasks/`,
  `templates/` and `defaults/`, inventory and playbooks. An edit there is a setup-plane or
  k8s change for a comment. File each such pointer with `findings.py open` instead, and list
  the issue numbers in the PR body.

**One-home check, every moved paragraph.** The PR body quotes, for each moved paragraph:

```bash
git grep -n -F '<a distinctive phrase>' -- '*CLAUDE.md' .claude docs ':!docs/archive/' ':!docs/target-architecture/'
```

It must print exactly one line, in the destination the per-section list names, and none in
the root. Pick a phrase that sits on one line in the destination, because `git grep` does not
match across a line break. For each tool whose help absorbed a paragraph, the PR body quotes
the help output, for example `uv run python scripts/dev/findings.py claim --help`.

**PR 0.** The red-proof pair in *Steps* is the proof that the new append can go red, in both
directions. Its named member is `secrets.md`.

**Optional, operator-only: section ablation.** `run` needs `ANTHROPIC_API_KEY`, which an agent
never decrypts, and each run stays under the $10 cap the operator set.

- `uv run python evals/ablate.py run --dry-run --agent skeptic --section "<heading>"`
  prints the plan and its price, for free.
- On `master`, a `run` with the same arguments compares the whole root with the root minus
  that section. That bounds what trimming the section could cost.
- On the PR branch, the baseline arm runs the trimmed root. Comparing it with `master`'s
  baseline arm measures the trim itself.
- `--section` takes only `## ` headings. *The procedure*, *When to wait*, *Checking a k8s
  change* and *kubectl* are measured with their parent sections.

G5 adds no doc-size check. The size measurement is G4's report, which has its own red-proof.
G5 adds no root line-count test, because G4 removes CI size gates on purpose.

## Rollback

- **PR 0** is a revert. Held phases go back to the root alone.
- **PRs 1 to 4** are each a revert. A revert puts the paragraphs back in the root and leaves
  the copies in their destinations. `test_claude_md_paragraphs_have_one_home.py` then fails
  for any moved paragraph of 240 characters or more. So a revert PR also deletes those
  copies. `git revert` of the PR's commit does both, because the original PR did both. It
  also restores `MIN_GRADED_PARAGRAPHS` and the retargeted pointers.
- **A partial failure** is a session that misses a moved rule. It happens when the session
  acted before touching any file that loads the rule. The mitigation is the classification
  rule: only paragraphs a path or a procedure triggers move. A miss found in review moves
  that paragraph back, as a one-paragraph PR.

## Risks

Each risk is checked against `docs/failure-classes.md`.

- **A directive moves behind a trigger that never fires.** This is failure class 7: the
  rule loads in the developer's context, not in the agent's.
    - Example: a secrets rule moves to `.claude/rules/secrets.md`, but `git diff` names no
      path, so the rule never loads.
    - Mitigation: the resident sentences in *Design* stay by the classification rule.
    - Mitigation: each PR body names the trigger for every moved paragraph.
    - Mitigation, optional: the operator-only ablation run measures the effect of a trim.
- **Held fan-out phases lose context.** Every phase on held settings gets the root as text
  and loses project skills and `.claude/rules/`. This is failure class 7 again: the phase
  runs in a narrower context than an interactive session.
    - Mitigation: PR 0 appends to the reviewer and each resumed phase the rules that match
      the batch's range, and every rule to the implement phase after a red phase, whose
      range cannot predict what it edits.
    - Residual: skills stay out of reach. G5 moves into skills only procedure text that an
      interactive session follows.
- **Moved citations lose their check.** This is failure class 4, a guard's selector drifting
  from the hazard's extent: the lint reads only `CLAUDE.md` files, and the prose moves out of
  them. It is also failure class 8, prose that nothing re-derives.
    - Mitigation: citations stay in a `CLAUDE.md`, and the citation diff proves it per PR.
- **The non-vacuity floor trips.** Trimming takes graded blocks under 240 characters.
    - Mitigation: the PR that crosses the floor lowers it to just below its measured count,
      and never below 10.
- **Pointers to headings break.** Seven files in setup roles, scripts, a deck-mod hook and a
  generator name root headings. Keeping every heading avoids editing setup-role files, which
  would re-apply roles for a doc change.
- **Pointers to section content go stale.** This is failure class 8. Two already have.
    - Mitigation: the pointer sweep in each PR. A pointer in a deployable path is filed, not
      edited.
- **Line count is met by joining lines.** The character ceiling in the exit criterion
  prevents it.
- **Other slices rewrite the same sections.** D2.3, D3.2, D6.8, H1.9, H7.15, and R4 PRs 2 and
  7 edit sections G5 trims. *Baseline* lists each.
    - Mitigation: the *Ordering with other slices* rule in *Design*. Whichever lands second
      rebases, and a later slice adds a pointer rather than prose.

## Tests and docs that change in the same PR

- **Every PR:**
    - `CLAUDE.md` and each destination file;
    - each pointer the sweep retargets;
    - `MIN_GRADED_PARAGRAPHS` in `test_claude_md_paragraphs_have_one_home.py`, in the PR
      whose measured count falls under it. The projection puts that in PR 2 or PR 3, and
      again in PR 4.
    - No `# fact:` line changes. Keeping headings keeps the section keys those lines name.
- **PR 0:** `held_hooks.py`, `review.py`, and three tests in
  `test_fanout_review_red_green.py`: the red-proof pair and the every-rule test. The
  `held_hooks.py` docstring's marker gains one sentence saying held phases get rules as text.
- **PR 1:** the opening paragraph and `paths:` of `.claude/rules/secrets.md`.
- **PR 3:** `scripts/dev/findings.py` or `scripts/dev/findings_lib/` help strings, if the
  claim or close semantics are missing from `--help`.
    - Their tests in `scripts/dev/tests/` gain one assertion per added help sentence. The
      assertion runs the subcommand with `--help` and checks for one distinctive phrase.
- **PR 4:** the `land-after-merge` skill's opening paragraph. It says the root owns
  *When to wait*, and that stays true.

## Complexity delta

- **Removed:** about 117 lines and about 9,000 characters from the file every session and
  every held fan-out phase loads.
- **Added:**
    - about the same prose in path-scoped rules, skills and docs, which load only when their
      trigger fires;
    - at most a few `--help` sentences;
    - PR 0: one helper in `held_hooks.py`, its calls in `review.py`, and three tests. That
      is one small mechanism, and it also closes a gap that predates G5.
- **Changed:** one test constant, `MIN_GRADED_PARAGRAPHS`, lowered with a recorded
  measurement.
- **Net:** no new file, no new test module, and no new CI gate.

## Open questions

- **Does the Bash arm of `inject-nested-docs` run in a held phase?** It is inferred from
  code, not observed. Resolution: PR 0's first step reads the instructions log for a
  `bash_path_match` row from a held-phase session. PR 0 runs either way, so the answer
  changes only the PR body.
- **Should citations in rules and skills be checked?** Widening `citations.repo_docs` to
  `.claude/rules/*.md` and `.claude/skills/*/SKILL.md` would let a citation move with its
  paragraph. It changes the interface G1's notice and G2's lint build on, and it needs its own
  red-proof and named members. Resolution: G5 does not need it, because citations stay in a
  `CLAUDE.md`. It is reported to G2 as a contract gap, not taken here.
- **What about the 86 role `CLAUDE.md` files?** The area design calls for a one-time rewrite
  of all 87 `CLAUDE.md` files to directives and pointers. G5 covers only the root. G2's
  `report` ranks the sections that cite nothing by reads, which gives the order for that
  rewrite. Resolution: no slice owns it. This is reported as a contract gap.
- **Is 200 lines the right target for this repo?** Anthropic recommends it, as quoted
  in the area design. Resolution: the exit criterion uses it with a character ceiling beside
  it. G4's report keeps showing both after G5.

## Review log

### Round 1 (per-slice adversarial review)

Raised: 3 high, 7 medium. Fixed: all 10. Refuted: one claim inside G5-R1-03.

- **G5-R1-01 (high)**: citations moved out of the root lose every check that reads them.
  **Fixed.** Measured 53 in-tree occurrences and 41 distinct spans with
  `citations.parse_citations` and `citations.in_tree`. Confirmed that
  `citations.repo_docs` lists only `CLAUDE.md` files and that `test_documented_paths_exist.py`
  checks only `file:line`, bare `ansible/tests/…py` and node-id shapes. Chose option (c):
  *Design* keeps every citation in a `CLAUDE.md`, *Verification* adds the per-PR citation
  diff, the Baseline sentence is corrected, and *Risks* names failure classes 4 and 8. The
  exit bullet compares each PR's root with the root just before it, because G2 changes the
  root before G5 starts.
  Option (b) is recorded as an open question and a note to G2.
- **G5-R1-02 (high)**: the non-vacuity floor conflicts with "no test file changes".
  **Fixed.** Measured 28 graded blocks with the test's own `paragraphs()`. *Verification*
  prints the count per PR and projects it. The PR that crosses the floor lowers it to the
  measured count minus 2, never below 10. *Tests and docs* and *Complexity delta* match.
- **G5-R1-03 (high)**: skipping PR 4 makes the exit criterion wait on an unlisted slice.
  **Fixed** with option (b): PR 4 always runs, the "has a PR open" trigger is gone, and the
  arithmetic is restated (PRs 1 to 3 reach about 218, PR 4 about 192). **Refuted in part:**
  the evidence said the D6 and H7 plan files do not exist, but `ls docs/target-architecture/deploy
  docs/target-architecture/host` lists `d6-delete-old-path.md` and `h7-delete-ansible.md`.
  D6's risk *Coordination with G5 and R4* already says D6.8 runs after G5 if G5 has started,
  which supports option (b).
- **G5-R1-04 (medium)**: other slices edit the same sections. **Fixed.** *Baseline* lists
  D2.3, D3.2, D6.8, H1.9, H7.15, and R4 PRs 2 and 7. *Design* states the ordering rule. The
  stale `fact_status.py verify` steps in D2, D3, D6, H1, H2, H7 and M1 are reported as
  cross-slice notes.
- **G5-R1-05 (medium)**: the one-home test does not prove a moved paragraph has one home.
  **Fixed.** The exit criterion says what the test proves. The per-section list names one
  destination per moved paragraph, or "cut" with the owner. The PR-body `git grep` must print
  exactly one hit and is named as the one-home proof.
- **G5-R1-06 (medium)**: implementer phases also get only the root as text. **Fixed.** The
  first point is answered by the `held_hooks.py` `DECIDED:` marker: held phases lose project
  skills and `.claude/rules/`. The second is labelled as an inference with a check before
  PR 0. PR 0 decides the resolution before any text moves, with a red-proof pair. The
  implement phase after a red phase gets every rule, because `base..red` holds only tests
  and would not select the rules for the files it edits next.
- **G5-R1-07 (medium)**: the resident list leaves out pre-file directives. **Fixed.**
  *Resident sentences* adds `discard_changes`, `uv run` with deploys-`HEAD`, the red-proof
  directive and the cannot-decrypt pointer. The per-section list names what stays in each
  section.
- **G5-R1-08 (medium)**: `secrets.md`'s opening paragraph turns false, and its globs miss
  `ansible/.sops.yaml`. **Fixed.** PR 1 rewrites the paragraph and adds the glob.
  `git ls-files ansible/.sops.yaml` prints the path, so the globs test still passes.
- **G5-R1-09 (medium)**: pointers to section content are not swept. **Fixed.** Confirmed
  the stale pointers in `invocation_sites.py` and `render_guard.py`. *Verification* adds a
  sweep that prints 125 lines at `70591c307`, with a retarget-or-file rule by path. Setup-role
  Markdown is safe to edit, because `deploy_changes.py:is_doc` routes any `.md` path to no
  plane. Non-Markdown files under `ansible/` are filed, not edited. The sweep also covers
  the root's own "see **Python & Tests**" cross-references.
- **G5-R1-10 (medium)**: the section-ablation harness is unused. **Fixed.** *Baseline*
  records the `ablate.py rank --sections CLAUDE.md` output. *Verification* adds the optional,
  operator-only run.
