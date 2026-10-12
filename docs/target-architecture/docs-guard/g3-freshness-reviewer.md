# G3 — (optional) Notice dispatches the freshness reviewer

Status: PLANNED

Depends on: [G2](g2-retire-lock.md)

Unblocks: none

## Goal and exit criterion

After G2, the notice is a table in a CI job summary, and agent sessions do not read job
summaries. G2's root `CLAUDE.md` row tells a session to re-read each listed section whose
prose the PR left unchanged. Only a session that opens the job summary meets that row. G3
puts the same instruction in front of the session that opens the PR, in the result of its
`gh pr create` call. The session that moved the atom then re-reads the sections itself.

G3 also gives `homelab-docs-freshness-reviewer` a first input tier built from the notice.
That tier serves a dispatch that names a base ref. Only the operator makes such a dispatch:
`.claude/skills/homelab-review/SKILL.md` dispatches the reviewer from its *Docs vs
live-config drift* row and names no base ref.

**The hint asks for a re-read, not for a reviewer dispatch.** The user-level
`~/.claude/CLAUDE.md`, section *Verification & self-checking*, tells a session not to
dispatch a review subagent on its own initiative to double-check finished work. A hook's
context is not the user asking. A hint that named a dispatch would therefore ask for the
exact action a session is told to skip. The title keeps the name in the contracts index,
and *Open questions* proposes a rename.

The notice stays the check. Nothing gates on the session's re-read or on the reviewer's
output. ADR-0019 rejected a non-deterministic re-verifier as a gate, and G3 adds no gate.

**G3 is optional, and a measurement decides whether to build it.** The measurement reads one
population in every window: the notice rows of kind `moved`. G1's Design lists a path atom
only as `gone`, never as `moved`, so `moved` rows are content atoms only. G2's decision on
`gone` path rows therefore cannot change this population.

- The figure is the share of `moved` rows whose prose the same commit edited. It is
  `moved_prose_edited / moved`, read from the `notice_replay` object that
  `fact_status.py report --json` prints. G1 PR 4 adds those two keys; see *Baseline*.
- Thirty days after G2's PR 3 merges, run
  `uv run python scripts/dev/fact_status.py report --days 30 --json` and compute the figure.
- Compare it with the same figure from G1's exit report, which ADR-0020 records in full.
  G1's figure was taken while the lock forced a re-read.
- The **operator decides** whether to build G3. The suggested rule is to build it when the
  post-G2 figure is under half of G1's. With about five content moves a month, that
  comparison is a judgment, not a statistic.

**Exit criterion, if built.** A session runs `gh pr create` on a branch whose committed head
moves a cited atom without editing the citing section. The PreToolUse context names that
section and asks the session to re-read it against the branch's diff. The same command adds
no context when the branch's commits move nothing cited, or when they edit every listed
section. A reviewer dispatched with a base ref lists the notice's sections as its first
tier.

## Baseline at `70591c307`

- **The reviewer's input tiers.** `.claude/agents/homelab-docs-freshness-reviewer.md` builds
  its input in four tiers:
    1. sections `fact_status.py lint` flags, or `lint --changed-since <ref>` when the
       dispatch names a base ref;
    2. UNVERIFIED sections, which G2 removes;
    3. sections whose cited files changed most in 30 days;
    4. sections of `docs/` and `.claude/rules/` pages, ranked by churn.

  It stops at about 25 sections. It is read-only, and its *Rules* say nothing gates on it.
- **One skill dispatches the reviewer, with no base ref.** `git grep -n
  docs-freshness-reviewer` finds one dispatcher: `.claude/skills/homelab-review/SKILL.md`,
  in the row *Docs vs live-config drift*. That table names the agent and passes no base
  ref. The other hits are the agent file, `docs/archive/`, the eval corpus under `evals/`,
  and `scripts/dev/tests/test_review_metrics.py`, which reads only the name.
- **The user forbids an unasked review dispatch.** The user-level `~/.claude/CLAUDE.md`,
  section *Verification & self-checking*, reads: "Don't dispatch a review subagent on your
  own initiative to double-check work you already finished." Its source is the chezmoi
  dotfiles repository, outside this one.
- **Every Bash call already passes through one dispatcher.** `.claude/hooks/bash-pretool.py`
  runs every PreToolUse:Bash arm in one process.
    - `_DECISION_ARMS` holds two decision arms.
    - `_CONTEXT_ARM` holds one context arm, `inject-nested-docs`. It is a single pair, not a
      tuple of pairs. `test_bash_pretool.py` reads `_CONTEXT_ARM[0]` in its arm census
      against `EXPECTED_ARMS`.
    - `_REWRITE_ARMS` holds two rewrite arms.
    - The hook's registration sets `timeout: 15`.
    - `bash-pretool.py:collect` takes a `load` parameter. Its docstring says the tests hand
      it a loader that returns a real arm with parts redirected, because each arm is loaded
      fresh with `exec_module` and cannot be monkeypatched from outside.
- **The dispatcher's `DECIDED:` block forbids a silent arm.** The module docstring's block
  that begins `DECIDED: every arm runs under its own` says: "What it must never become is
  silent: the traceback goes to stderr named by arm". `collect` sends an arm's exception to
  `_report`, which names the arm on stderr.
- **`inject-nested-docs` does not swallow errors inside the dispatcher.**
  `inject-nested-docs.py:context` catches nothing itself. `build_context` and the helpers it
  calls catch only `OSError` around specific reads, and `injected_this_session` also catches
  `ValueError`. The blanket
  `except Exception: sys.exit(0)` runs only under `__main__`, so inside the dispatcher an
  error reaches `collect`, which reports it.
- **The payload budget is shared.** `inject-nested-docs.py:INLINE_MAX_CHARS` is 7,500 and
  `INLINE_MAX_LINES` is 190. Their comment names the harness caps: 10,000 characters locally,
  and 8,000 characters or 200 lines on the remote path, "with headroom for the wrapper text".
  The harness prefixes the context with `PreToolUse:Bash hook additional context: `, which is
  41 characters, as observed in a session transcript on 2026-10-11. That transcript also
  shows the context arriving with the tool's result, after the command ran.
- **The facts library is slow to import.** From `scripts/`, the command
  `uv run --no-sync python -X importtime -c 'import lib.facts.lint, lib.facts.atoms'` printed
  a cumulative 29,061 µs for `lib.facts.lint` and 12,004 µs for `lib.facts.atoms` on
  2026-10-11. `collect` runs `load_arm` for every arm on every Bash call.
  `.claude/hooks/_facts_line.py`'s docstring sets the existing discipline: its `lib.facts`
  import is deferred, so only a `CLAUDE.md` injection pays for it. G2 PR 4 deletes
  `_facts_line.py`. `session-health.py` makes the same `sys.path` insert,
  `sys.path.insert(0, os.path.join(REPO, "scripts"))`, and G2 leaves it in place.
- **The hooks already parse a `gh` subcommand.** `block-footguns.py:issue_create_by_hand_problem`
  matches `gh issue create` with `_hook_common.split_stages`, `strip_shell_keywords` and
  `invokes`. It reads `-R`/`--repo` with `_hook_common.gh_repo`. `split_stages` raises
  `Unsplittable` for text it cannot read, and `Unsplittable.missing` is true when the
  `claude_guard` package is not deployed.
- **A branch push runs no CI.** `.github/workflows/ci.yml` runs on `pull_request`, on
  `merge_group`, on `workflow_dispatch`, and on `push` with `branches: [master]`. So
  `gh pr create` starts the first CI run for a branch.
- **Fan-out sessions stop at `gh pr create`.** `scripts/dev/fanout_lib/brief.py:REVIEW_LANDING`
  and `_landing` tell the session to "Open the PR with `gh pr create` and STOP there".
  `_finishing` requires the final message to end with the PR URL, and
  `.claude/hooks/fanout-stop.py` enforces that. `render_brief` assembles the sections, and
  `scripts/dev/tests/test_fanout_brief.py` holds them.
- **Sessions open PRs with `gh pr create`.** `scripts/deploy_tools/publish_pr.py` is the
  crons' path, and it runs `gh` through `subprocess`, so no PreToolUse hook sees it. The
  user-level guard reads `gh pr create` titles, so the command shape is stable.
- **Hook tests can build real repositories.** `scripts/lib/git_testing.py:init_repo` and
  `commit` run git under `scrubbed_env`. `.claude/hooks/tests/test_hook_common.py` already
  imports `lib.git_testing` from a hook test. The `repo` fixture of `test_inject_nested_docs.py`
  makes only an empty `.git` directory, with no commit and no ref. The census row
  `tests-run-no-git-write-through-subprocess` in `scripts/tests/test_census_rows_test_modules.py`
  covers `.claude/hooks/` through `_test_module_rules._TEST_ROOTS`, and it refuses a raw
  `subprocess.run(["git", "init"])`.
- **CI cannot run the reviewer.** The repository is public and CI stays on hosted runners,
  per ADR-0018. `.github/claude-cli/` pins a Claude Code CLI only to validate the deck mod,
  with no credentials. Running an agent in CI would need an API key in Actions secrets, and
  that is a GitHub settings change, which is **operator-only** under C8. G3 does not
  propose it.
- **G1 provides everything G3 calls or reads.** `scripts/lib/facts/` holds no `notice.py`
  at `70591c307`, and `scripts/dev/fact_status.py:main` registers no `notice`
  subcommand. G1's *Steps* add them:
    - G1 PR 2 adds `notice.moved_sections(repo, base, head)`. With no head it diffs the
      merge base against the working tree. With `head=REV` it reads every input at `REV`
      through the revision backend: the diff, both `atom_content` ends, and the head side of
      the prose comparison.
    - G1 PR 3 adds `notice.resolve_base(repo, ref, head)`, which returns
      `git merge-base REF <head>`, or `None` when either ref does not resolve. The same PR
      adds the `notice` subcommand. Its `--since REF` resolves through that function, with
      `<head>` set to `--head REV` or to `HEAD`, and it maps `None` to exit 2.
      `notice --format json` prints a list of objects with the keys `unit`, `atom`, `form`,
      `kind` and `prose_edited`.
    - G1 PR 4 adds the `notice_replay` keys `moved` and `moved_prose_edited` to
      `report --json`.

  All three land before G3 starts. G3 depends on G2. G2 PR 1 opens only when G1's exit
  criterion holds, and G1 grades that criterion no earlier than 30 days after G1 PR 5,
  its last PR, merges. G3 therefore adds no fallback for any of the three.

## Design

There are three parts: a context arm, a context budget that the dispatcher owns, and a first
tier for the reviewer.

**The arm, `freshness-notice`.** It is a library module, `.claude/hooks/freshness-notice.py`,
with a `# gen-hooks: library` header, as `inject-nested-docs.py` has.

What it reads:

- **The command.** A substring precheck returns None unless the text holds `gh`, `pr` and
  `create` in that order. Only then does the arm split the command with
  `_hook_common.split_stages` and look for a stage that `invokes` `("gh", "pr", "create")`,
  as `block-footguns.py:issue_create_by_hand_problem` does for `gh issue create`.
- **The checkout.** It is the payload's `cwd`, or the directory of a literal `cd <dir>`
  stage before the `gh pr create` stage. `git -C <dir> rev-parse --show-toplevel` names it.
- **The base.** It is `origin/HEAD`, which names the default branch. PR 3 adds the
  `--base`/`-B <branch>` form, read as `origin/<branch>`. The arm resolves the merge base
  with `notice.resolve_base(repo, ref, head="HEAD")`, which G1 PR 3 adds.
  `fact_status.py notice --since` calls the same function, so the arm and the CLI cannot
  resolve a base two ways.
- **The head.** It is `HEAD`, the committed tip, which is what the PR carries. The arm calls
  `notice.moved_sections(repo, merge_base, head="HEAD")`. Uncommitted edits are not in the
  PR, so the arm ignores them in both directions. An uncommitted prose fix does not suppress
  a row, and an uncommitted scratch change adds none.
- **The rows.** The arm keeps rows with `prose_edited: false`, of any kind, one per `unit`.

The arm returns None only in these cases, which are the expected no-ops:

- the precheck fails, or no stage invokes `gh pr create`;
- the `gh pr create` stage carries `-H`/`--head`, so the PR's head is another branch;
- the stage carries `-R`/`--repo` that names a repository other than the checkout's
  `origin`. In PR 2, any `-R` or `-B` returns None, and PR 3 narrows the case;
- `split_stages` raises `Unsplittable` that is not `missing`, meaning the splitter refused
  the quoting;
- a `cd` stage's argument holds `$` or a backtick;
- the checkout directory is not inside a git work tree;
- no row has `prose_edited: false`.

When the base ref does not resolve, the arm returns one line instead of None. Examples are an
unset `origin/HEAD`, or a `-B` that names an unfetched branch. The line names the ref and the
`notice` command to run by hand. Silence there would read as "nothing moved", which is
failure class 3.

Every other exception propagates to `collect`. That includes `Unsplittable` with `missing`,
an `ImportError` from `lib.facts`, and any error raised inside `moved_sections`. `_report`
then names `freshness-notice` on stderr, and every other arm's output stands. This honours
the `DECIDED:` block in `bash-pretool.py`'s docstring that begins `DECIDED: every arm runs
under its own`.

**What the arm costs a Bash call.** The `sys.path` insert of the arm's own checkout's
`scripts/`, copied from `session-health.py`, sits inside the `gh pr create` branch. The
import sits there too, as `importlib.import_module("lib.facts.notice")`. That call returns
the `sys.modules` entry, so a test can put a stub there even after another test imported
the real module. Every other Bash call pays neither the 29 ms import nor any git call.

**The text.** It is at most `MAX_CHARS` = 300 characters and `MAX_LINES` = 3 lines, both
constants in the arm. Its command carries `--head HEAD`, so a session that runs it reads the
same committed tree as the arm, not its working tree:

```text
[freshness-notice] Moved atoms, unchanged prose in 2 CLAUDE.md sections: <doc>#<heading>, <doc>#<heading>.
Re-read each against this branch's diff. Full list: fact_status.py notice --since origin/HEAD --head HEAD
```

The fixed text takes about 185 characters, which leaves room for one or two section names. The arm names sections in order while they fit,
then adds `(+N more)`. When no name fits, the count and the command still carry the hint.

**The dispatcher owns the budget.** `_CONTEXT_ARM` becomes `_CONTEXT_ARMS`, a tuple with
`inject-nested-docs` first and `freshness-notice` second.

- `inject-nested-docs` stays first. It records each doc as delivered before the harness sees
  the text, so a doc cut off by a truncation would never be sent again in that session.
- `collect` joins the non-empty contexts with a blank line.
- `collect` holds `_CONTEXT_MAX_CHARS` = 7,900 and `_CONTEXT_MAX_LINES` = 198. Lines are
  counted as newlines, the unit `inject-nested-docs.py:_fits_inline` uses with
  `INLINE_MAX_LINES`, so all three line constants share one unit. The 100
  characters between 7,900 and the remote cap of 8,000 cover the 41-character wrapper. A
  context that would push the join past either cap is dropped and reported on stderr,
  naming its arm. The remote path therefore never truncates the joined text.
- The arithmetic leaves room for both arms: 7,500 + 2 + 300 = 7,802 characters, and
  190 + 2 + 2 = 194 newlines. The join adds two newlines, and a 3-line text holds two. That
  is 195 lines, under the remote cap of 200. A test pins both sums against the constants.
- The docstring's merge paragraph gains two sentences, on the order and the budget.

**The fan-out brief.** `brief.py:render_brief` gains a short section before *Landing* in this
repository's briefs, in every landing mode:

```text
## Docs this branch may leave stale
When a `[freshness-notice]` context arrives with your `gh pr create` result, re-read each
section it names against your diff. Fix and push any sentence your change made false before
the landing command and before your final message. A section that still holds needs no edit.
```

The replay cannot tell a fan-out squash commit from any other, so fan-out sessions must
receive the same instruction, or the 30-day population would mix sessions told to act with
sessions told to stop. In a review batch, the section does not duplicate the pipeline's
reviewer. It asks the PR's own session to re-read, not to dispatch.

**The reviewer's tier 1.** When the dispatch names a base ref, the reviewer runs
`uv run python scripts/dev/fact_status.py notice --since <ref> --head HEAD --format json`.
The reviewer checks sentences against the code at HEAD, so `--head HEAD` keeps its tier on
the same tree. It takes every
row whose `prose_edited` is false, then every other row. That tier gets up to 10 of the
roughly 25 sections. The lint tier becomes tier 2, and the churn tiers follow unchanged.
Without a base ref, the tier is empty and the reviewer behaves as after G2. Every
`homelab-review` dispatch has no base ref, so the tier serves an operator's dispatch only.

```diff
 .claude/hooks/
+  freshness-notice.py      context arm: gh pr create -> notice at HEAD -> re-read hint
   bash-pretool.py          _CONTEXT_ARM -> _CONTEXT_ARMS; _CONTEXT_MAX_CHARS, _CONTEXT_MAX_LINES
 .claude/hooks/tests/
+  tests for the arm; dispatcher tests for two context arms and the budget
 scripts/dev/fanout_lib/brief.py
+  "Docs this branch may leave stale" section, this repository's briefs only
 .claude/agents/homelab-docs-freshness-reviewer.md
+  tier 1: notice rows for the dispatch's base ref
```

```text
PreToolUse:Bash  "gh pr create ..."
  bash-pretool.collect
    decision arms (unchanged)
    context arms, joined, capped at _CONTEXT_MAX_CHARS / _CONTEXT_MAX_LINES
      inject-nested-docs.context(payload)          docs for paths the command names
      freshness-notice.context(payload)
        precheck, split_stages, invokes gh pr create       otherwise None
        importlib.import_module("lib.facts.notice")        deferred to this branch
        notice.resolve_base(repo, "origin/HEAD", "HEAD")   None -> one line naming the ref
        notice.moved_sections(repo, base, head="HEAD")     rows with prose_edited false
    rewrite arms (unchanged)
```

**Two-writer rule (C4).** Not applicable. G3 moves no ownership. The notice stays the only
check, and the arm and the reviewer only read it.

## Steps, as PRs

1. **Two context arms and a budget in the dispatcher** (about 80 lines, most of them tests).
    - Change `_CONTEXT_ARM` to `_CONTEXT_ARMS` in `bash-pretool.py`, holding only
      `inject-nested-docs`. `collect`'s `context_arm` parameter becomes `context_arms`.
    - Add `_CONTEXT_MAX_CHARS`, `_CONTEXT_MAX_LINES`, the rule that drops and reports an
      oversized context, and the docstring sentences.
    - Update the arm census in `test_bash_pretool.py` to read `_CONTEXT_ARMS`. Add stub
      second arms that prove the join, that an arm that raises loses only its own context,
      and that an oversized context is dropped and named on stderr.
    - No behaviour changes: `inject-nested-docs`'s own caps sit under the dispatcher's.
2. **The arm, with the default base** (about 230 lines, about half of them tests).
    - Add `.claude/hooks/freshness-notice.py` and register it as the second context arm. Add
      it to `EXPECTED_ARMS` in `test_bash_pretool.py`.
    - Run `uv run python scripts/dev/gen_hook_settings.py --check`. A library arm adds no
      `settings.json` entry, and the check confirms that.
    - Add the brief section to `brief.py:render_brief`, with its test.
    - In `docs/claude-tooling.md`, add the arm to the list under `### bash-pretool`, and add a
      `### freshness-notice (a bash-pretool arm)` subsection.
    - The PR body records the arm's measured time for `gh pr create` on a live branch.
3. **Stacked and cross-repository PRs** (about 80 lines, most of them tests).
    - Read `--base`/`-B <branch>` as the base `origin/<branch>`.
    - Compare a `-R`/`--repo` value with the checkout's `origin`, read once with
      `git remote get-url origin` as `owner/name`, ignoring case. A match proceeds, and any
      other value returns None.
    - Update the `docs/claude-tooling.md` subsection.
4. **The reviewer tier** (about 30 lines).
    - Add tier 1 to the agent file, and update its `description:` and the tier caps in
      *Input*.

## Verification

Red-proof for each new check, as paired tests in `.claude/hooks/tests/`. The fixture
repository is built with `lib.git_testing.init_repo` and `commit`, as
`test_hook_common.py` already does from a hook test. The fixture commits a base with a
`CLAUDE.md` section that cites a function in `a.py`, and branches from it. It sets
`refs/remotes/origin/master` with `lib.git_testing.git(repo, "update-ref", ...)`, and
points `refs/remotes/origin/HEAD` at it with `git(repo, "symbolic-ref", ...)`.

PR 2:

- `..._gh_pr_create_on_a_moved_atom_is_named`: a committed change to the cited function's
  body, with the section's prose left alone, gives a context that contains the exact section
  key `CLAUDE.md#<heading>`.
- `..._gh_pr_create_with_no_moved_atom_is_silent`: a branch that changes nothing cited
  returns None.
- `..._edited_section_is_not_named`: a branch that moves the atom and edits the section's
  prose returns None.
- `..._other_gh_commands_are_silent`: `gh pr view` on the first branch returns None.
- `..._uncommitted_prose_edit_does_not_suppress_a_committed_move`: the move is committed and
  the prose edit sits only in the working tree. The section is still named.
- `..._uncommitted_atom_change_is_not_named`: the atom changes only in the working tree, and
  the arm returns None.
- `..._base_that_moved_on_is_ignored`: `origin/master` advances past the branch point and
  changes the atom, while the branch does not. The arm returns None. This pins the arm's
  merge-base resolution, not just the function's.
- `..._unresolvable_base_is_named`: with no `origin/HEAD`, the context is one line naming
  `origin/HEAD`, and nothing raises.
- `..._text_never_exceeds_the_caps`: with 20 listed sections, the text is at most
  `MAX_CHARS` characters and `MAX_LINES` lines, and it still carries the count.
- `..._a_command_without_gh_pr_create_never_imports_the_facts_library`: the test removes
  `lib.facts.notice`, `lib.facts.lint` and `lib.facts.atoms` from `sys.modules` with
  `monkeypatch.delitem(..., raising=False)`. It loads the arm with `bash-pretool.load_arm`
  and calls `context` for `ls /tmp`. None of the three modules is in `sys.modules`
  afterwards.
- A sentinel: `notice.resolve_base(fixture, "origin/HEAD", "HEAD")`, G1's function, equals
  the fixture's base commit. The silent tests above therefore cannot pass because the
  fixture's base failed to resolve.
- Dispatcher, the error path: `collect` runs with a `load` that returns stub decision arms, a
  stub first context arm, and the real `freshness-notice`. `monkeypatch.setitem` puts a stub
  `lib.facts.notice` in `sys.modules` whose `moved_sections` raises. On `gh pr create` in
  the fixture, stderr names `freshness-notice` and the stub first arm's text still arrives.
- Dispatcher, the budget: `INLINE_MAX_CHARS + 2 + MAX_CHARS <= _CONTEXT_MAX_CHARS` and
  `INLINE_MAX_LINES + 2 + (MAX_LINES - 1) <= _CONTEXT_MAX_LINES`, all in newlines. A
  second case runs `collect` with a stub first arm and the real arm on the 20-section
  fixture. The stub returns exactly `INLINE_MAX_CHARS` characters holding
  `INLINE_MAX_LINES` newlines. The joined text is within both caps and carries both arms'
  text.
- Brief: `render_brief` for this repository carries the *Docs this branch may leave stale*
  section on the landing host, on another host, and in a review batch. A brief for another
  repository's target does not carry it.

PR 3:

- `..._explicit_base_is_used`: the branch is stacked on a parent branch that moved the atom.
  With `--base parent` the arm returns None, and without `--base` it names the section.
- `..._another_repo_is_silent`: `-R other/repo` returns None, and `-R` naming the fixture's
  `origin` as `owner/name` names the section.

Live check, from a scratch worktree, before PR 2 merges. Create a throwaway branch with
`git switch -c g3-live-check`. Change the value of
`scripts/lib/render_guard.py:BUILT_IMAGE_TAG_STUBS` and commit the change. Then run:

```bash
printf '%s' '{"tool_name":"Bash","tool_input":{"command":"gh pr create --title x"},"cwd":"'"$PWD"'","session_id":"t"}' \
  | uv run --no-sync python .claude/hooks/bash-pretool.py
```

The output's `additionalContext` must name the section of
`ansible/roles/k8s/image-builder/CLAUDE.md` that cites the symbol. Return with `git switch -`,
delete the branch with `git branch -D g3-live-check`, and run the command again. The context
must be gone.

Reviewer tier: dispatch the agent with the scratch branch's base ref, and check that its
tier-count line reports at least one tier-1 section. This check reads an agent's output, so
it is a manual check, not a test.

Measurement after 30 days: `fact_status.py report --days 30 --json` gives the post-G3
figure, `moved_prose_edited / moved`. The PR that keeps or deletes the arm sets it beside
the figures from G1 and from the 30 days after G2. That PR also states that the hint asked
the PR's own session to re-read the sections, not to dispatch a reviewer. It lists the entry
points the arm misses, from *Risks*, because the replay counts every PR.

## Rollback

- **PR 1** is a revert. The tuple holds one arm, and its caps never bind, so nothing changes.
- **PR 2** is a revert, which removes the arm, its registration and the brief section
  together. A one-line removal from `_CONTEXT_ARMS` stops the context at once, but it leaves
  a brief section that names a context that no longer arrives. Use the revert.
- **PR 3** is a revert. The arm returns to PR 2's silence on `-B` and `-R`.
- **PR 4** is a revert. The reviewer returns to the G2 tier order.
- **A partial failure.** If the arm raises, `collect` reports it on stderr, names
  `freshness-notice`, and drops its context. Every other arm's output stands, per the
  dispatcher's `DECIDED:` block. This path is live, because the arm lets every unexpected
  error propagate.

## Risks

Each risk is checked against `docs/failure-classes.md`.

- **The context pushes the payload past the remote cap.** The dispatcher caps the joined
  text at 7,900 characters and 198 newlines, and drops and names an arm that would pass
  either.
  A test pins `INLINE_MAX_CHARS` plus the join plus the arm's cap under the dispatcher's
  cap, so the drop path fires only if a constant changes without the test.
- **The hint conflicts with a user instruction.** It would, if it asked for a reviewer
  dispatch. It asks the PR's own session to re-read, which no instruction forbids. The
  reviewer's tier serves dispatches that the operator asks for.
- **A non-deterministic reviewer reads as a gate.** ADR-0019's objection still holds. No CI
  job or hook reads the reviewer's output, and the agent's *Rules* already say nothing
  gates on it.
- **Empty is read as clean.** This is failure class 3. An arm that returns None for a broken
  checkout looks the same as an arm that found nothing. The arm therefore lets unexpected
  errors propagate, and it names an unresolved base in one line. Its silent cases are listed
  in *Design*, and each one has a test or comes from a flag in the session's own command.
  The exact-key assertion and the base sentinel keep the fixture from passing vacuously.
- **Latency on every Bash call.** The precheck returns before any import or git call unless
  the text holds `gh`, `pr` and `create`. The 29 ms import and the git calls run only for
  `gh pr create`. That cost is the changed-files read G1 measures, well under the hook's
  15-second timeout. PR 2's body records the measured time.
- **The notice runs in a different context.** This is failure class 7.
    - In CI the notice runs on the PR's merge ref. The arm runs on the committed `HEAD`
      against its merge base with `origin/HEAD`. Both read the same commits whenever
      `HEAD` is what the branch pushes.
    - Uncommitted edits are excluded on purpose, and two tests hold that.
    - When local `HEAD` is ahead of the pushed branch and `gh pr create` does not push it,
      the arm reads commits the PR does not carry yet. They reach the PR with the next
      push, so the hint is early, not wrong.
    - A stale `origin/HEAD` older than the branch point gives an older merge base and a
      larger diff. That is safe, because it can only add sections.
- **The arm misses some entry points.** These PRs never get the hint, and CI's notice step
  still covers each of them:
    - commits pushed after the PR opens, such as review fix-ups and the resumed session of
      a fan-out review phase;
    - `gh stack submit` and `gh api .../pulls`;
    - `scripts/deploy_tools/publish_pr.py`, which runs `gh` through `subprocess`;
    - the GitHub web UI;
    - `gh pr create -H`.

  The measurement counts every PR, so a low yield can come from a missed entry point as
  well as from an ignored hint. The PR that keeps or deletes the arm says which.
- **The hint arrives after the command ran.** PreToolUse context reaches the session with the
  tool result, so the PR and its first CI run exist before the session reads the hint. A fix
  costs one more CI run. The arm is advisory, so G3 accepts that cost. *Open questions*
  covers an earlier trigger.
- **A fan-out session is told to stop at `gh pr create`.** The brief section tells it to
  re-read and push a fix before its final message. Without that section, the brief and the
  hint would give opposite instructions.
- **A session ignores the hint.** No mitigation in G3. The 30-day measurement shows whether
  the hint changed the yield. If it did not, the arm is a candidate for deletion under
  *A check retires when its subject does* in `.claude/rules/python-layout.md`.

## Tests and docs that change in the same PR

- **PR 1:** `test_bash_pretool.py`, and `bash-pretool.py`'s docstring.
- **PR 2:**
    - the arm tests in `.claude/hooks/tests/`, and the error-path and budget tests in
      `test_bash_pretool.py`, with `EXPECTED_ARMS`;
    - `scripts/dev/tests/test_fanout_brief.py`;
    - `docs/claude-tooling.md`;
    - a pytest shard-weight row, which the docs-refresh cron adds.
- **PR 3:** the two arm test pairs, and the `docs/claude-tooling.md` subsection.
- **PR 4:** the agent file. No test reads its tiers. `scripts/dev/tests/test_review_metrics.py`
  reads only the agent's name.

## Complexity delta

- **Added:**
    - one context arm, about 110 lines;
    - a tuple and a two-constant budget in the dispatcher, about 20 lines;
    - one brief section, about 8 lines;
    - one reviewer tier;
    - about 220 lines of tests.
- **Removed:** nothing.
- **Recovered:** G2's re-read instruction reaches the session that opened the PR, not only a
  reader of the job summary. The reviewer gets a targeted tier for a dispatch that names a
  base ref.

## Open questions

- **Should the arm also fire on `git push`?** A branch push runs no CI, since `ci.yml`'s `push`
  trigger lists only `master`. A hint at the first push would therefore arrive before any CI
  run, and later pushes would be covered too. The cost is a per-session record of the `HEAD`
  each hint covered, so that a push followed by `gh pr create` does not repeat it, plus
  refspec parsing. Resolution: add it if the 30-day measurement shows fix commits landing
  after `gh pr create` more often than before it.
- **Should the arm fire on `land.sh --pr`?** The context arrives after the command ran, and
  `land.sh` merges the PR. A hint there arrives after the merge, too late to act on.
  Resolution: no.
- **Should the hint name a reviewer dispatch?** Not under the user-level instruction quoted in
  *Baseline*. A dispatch would need a carve-out in the user-level `CLAUDE.md`, which lives in
  the chezmoi dotfiles repository. Resolution: the hint asks for a re-read. Revisit only if
  the operator adds that carve-out.
- **Should the arm dispatch automatically?** A hook cannot start a subagent, and making it
  try would turn an advisory reader into an implicit gate. Resolution: no.
- **Should CI run the reviewer?** It needs an API key in a public repository's Actions
  secrets. Resolution: out of scope. Any such proposal is an **operator-only** settings
  change and would amend ADR-0018.
- **The slice's name no longer fits.** The contracts slice index and `design.md` call G3
  "Notice dispatches the freshness reviewer". G3 no longer dispatches. Resolution: a
  cross-slice note proposes the name "Notice reaches the PR's session" for the contracts
  slice index and for `design.md`. This title follows once the contracts change.

## Review log

### Round 1 (per-slice adversarial review)

Raised: ten findings, three high and seven medium. All ten were fixed, and none was refuted.

- **G3-R1-01 (high).** *The hint tells the session to dispatch a review subagent, and the
  user's standing instruction forbids that.* Fixed with resolution (a). The hint asks the
  PR's own session to re-read the named sections, which is the action of G2's root
  `CLAUDE.md` row. The reviewer's tier stays for dispatches the operator asks for. The Goal,
  Design, Risks and the measurement say so. The confirmed fact is that
  `homelab-review/SKILL.md` dispatches the reviewer with no base ref. A cross-slice note
  proposes renaming the slice.
- **G3-R1-02 (high).** *Swallowing every error contradicts the dispatcher's `DECIDED:` marker.*
  Fixed. The arm returns None only in the listed no-op cases. It returns one line for an
  unresolved base, which goes one step past the requested None, toward visibility. Every
  other error propagates to `collect`. The broken-checkout test became a pair: an
  unresolvable base, and a stubbed `moved_sections` that raises and is named on stderr.
  Failure class 3 and the `DECIDED:` block are named. The finding's claim about
  `inject-nested-docs.py:context` checks out at `70591c307`.
- **G3-R1-03 (high).** *The arm diffs the working tree, but the PR carries the committed
  head.* Fixed. The arm passes `head="HEAD"`. The live check commits on a throwaway branch
  and deletes it afterwards. Two tests pin the exclusion of uncommitted edits, and the
  class-7 risk text is corrected. The hint's command and the reviewer's tier-1 command carry
  `--head HEAD`, so both read the same committed tree as the arm.
- **G3-R1-04 (medium).** *The plan never resolves the merge base, and ignores `--base` and
  `--repo`.* Fixed. `notice.resolve_base` is shared with the CLI. PR 3 adds `-B` and an `-R`
  check against `origin`, and PR 2 stays silent on both. `-H` is silent. Tests cover a base
  that moved on, an explicit `--base`, and `-R`.
- **G3-R1-05 (medium).** *The named fixture cannot exercise git.* Fixed. The fixture uses
  `lib.git_testing.init_repo`/`commit`, with `origin/master` and `origin/HEAD` refs. An
  exact-key assertion and a base sentinel make the tests non-vacuous.
- **G3-R1-06 (medium).** *Fan-out sessions are told to stop at `gh pr create`.* Fixed. PR 2
  adds a brief section in this repository's briefs, with its test in `test_fanout_brief.py`.
- **G3-R1-07 (medium).** *A module-level import of the facts library would add about 27 ms
  to every Bash call.* Fixed. The measurement here was 29 ms. The import and the `sys.path`
  insert sit inside the `gh pr create` branch, and a test asserts that `lib.facts.notice`
  stays out of `sys.modules` for any other command.
- **G3-R1-08 (medium).** *The decision compares two figures that may count different rows.*
  Fixed. The comparison reads only `moved` rows, which G2's decision on `gone` rows cannot
  change. The keys are named, and a cross-slice note asks G1 to split them.
- **G3-R1-09 (medium).** *The hint fires only at PR creation.* Fixed by listing the missed
  entry points in Risks and in the measurement. The open questions are now consistent: the
  context arrives after any command, `land.sh` is too late, and a `git push` trigger is an
  open question with a measured resolution. `ci.yml` runs on a push only for `master`.
- **G3-R1-10 (medium).** *The combined context budget is not enforced or tested.* Fixed. The
  dispatcher caps the joined text at 7,900 characters and 198 newlines, and drops and names
  an oversized arm. The arm's cap is 300 characters and 3 lines, and a test pins the sums
  in characters and in newlines.

### Round integration-2

Raised: one finding, X2-18, of medium severity. G3's part is fixed, and nothing was
refuted. G2's part was fixed in G2, and G2's own round integration-2 entry records it.

- **X2-18 (medium).** *G2 PR 1's entry condition restates G1's old soak bound, and the bound
  cannot be met under G1's current rules.* G3's part: the *Goal* said G1 PR 3 names the
  `moved` and `moved_prose_edited` keys, and G3 carried a fallback that added
  `notice.resolve_base` itself. Fixed.
    - Confirmed against G1's *Steps*. Item 3 adds `resolve_base` and `render` to
      `notice.py` with the `notice` subcommand. Item 4 adds `notice_replay.moved` and
      `notice_replay.moved_prose_edited`. G1's round c1 entry G1-C1-04 also says that
      `moved_sections(..., head=REV)` reads every input at `REV`. At `70591c307`,
      `scripts/lib/facts/` holds no `notice.py`, and
      `scripts/dev/fact_status.py:main` registers no `notice` subcommand.
    - The *Goal* now says G1 PR 4. The last *Baseline* bullet names which G1 PR adds each
      of the three things G3 reads. It also says why all three exist before G3 PR 1: G3
      depends on G2, and G2 PR 1 opens only on G1's exit criterion.
    - The fallback is gone. The *Design* `diff` no longer lists `notice.py` or
      `fact_status.py`, PR 2 lost its conditional `resolve_base` item, and *Tests and docs*
      lost the conditional `resolve_base` test. The base bullet in *Design* and the
      sentinel in *Verification* name `resolve_base` as G1's function.
    - The *Open questions* entry that asked G1 for three things is removed. G1's *Steps*
      provide all three, so nothing is left to resolve.
    - G2's part was checked here as well. Outside its *Review log*, G2 no longer says
      "stricter", "prints nothing" about the artifact query, or "either CI step".
    - PR 2 stays at about 230 lines, which is consistent with *Complexity delta* without
      the `resolve_base` item. Its test share now reads about half, which matches the
      arm's 110 lines in *Complexity delta*. *Rollback*, *Risks* and *Complexity delta* never named the fallback, so they
      are unchanged.
    - Round 1 entries G3-R1-04 and G3-R1-08 stay as written, because they are history.
