# G1 — Diff notice alongside the lock

Status: PLANNED

Depends on: none

Unblocks: [G2](g2-retire-lock.md), and through G2 [G3](g3-freshness-reviewer.md) and
[G5](g5-shrink-root-claude-md.md)

## Goal and exit criterion

A pull request's CI run lists every `CLAUDE.md` section that cites an atom the PR moved. The
list is a notice: it never fails a job. `docs/facts.lock` and its hash test stay in force for
the whole slice, so the notice runs beside the lock for 30 days and is measured against it.

The notice exists to replace the lock in [G2](g2-retire-lock.md). The lock still forces a
`verify` on every moved atom during G1, so the count of notices that led to a prose edit is
confounded by the lock. That count is recorded, but it does not decide G2. Parity decides
G2: every lock move of a content atom (symbol, YAML, test or marker) must also appear in the
notice. Path atoms are outside parity. Since `96365b0b5` a path atom hashes its own path, so
only a change to the hashing code can move its lock hash (see *Baseline*).

**Exit criterion.** Thirty days after the CI-side lower bound `<ts>`, three checks pass. Each
one is a command in *Verification*. `<ts>` starts as PR 5's merge timestamp. It moves to the
merge timestamp of any later PR that changes `scripts/lib/facts/notice.py`, changes one of
the CI steps, or fixes the cause of a `notice-step-failed` artifact or of a content-form
`miss`. *Steps*, item 6, gives the reason and the one exception. The *`<ts>` commit* is the
squash commit of the PR that set `<ts>`.

1. **The replay.** `fact_status.py report --days 30` prints a *Notice replay* table that
   shows all of the following:
    - zero content-form parity misses, or each miss explained in the G2 ADR. A fix for a
      miss removes it from this check. The replay runs today's code, which no longer
      reports the miss, and the fix PR moves `<ts>` (*Steps*, item 6);
    - zero replay crashes;
    - every hashing-code move and every split move in the window, by name;
    - the number of first-parent commits that carried a notice, against the window's total;
    - the number of `moved` rows, and how many of them had their section's prose edited in
      the same commit. These are the `notice_replay` keys `moved` and `moved_prose_edited`.
2. **No CI crash.** No `notice-step-failed` artifact created after `<ts>` records a notice
   defect. The upload step runs only when the notice step's `outcome` is not `success`, so a
   crash and a timeout both upload one (see *Design*). The step runs master's code, never
   the PR's, so a PR's own edit to the notice's import chain cannot upload one.
    - An artifact created before `<ts>` does not fail the check. The G2 ADR names each one
      created between PR 5's merge and `<ts>`, with the PR that moved `<ts>` past it.
    - An artifact created after `<ts>` is named in the G2 ADR, and does not fail the check,
      in three cases that *Verification* defines. Its run used code older than
      the `<ts>` commit, its failure came in the step's `setup` stage, or its PR changed one
      of the CI steps. Any other artifact after `<ts>` fails the check.
3. **Delivery.** Take every content-form lock move committed after `<ts>`, outside the
   `hashing` class, whose lock commit maps to a PR. That PR's last `hooks` run carries the
   notice annotations that *Verification* describes. A run that carries 9 row notices
   without the move's unit passes only when all 9 are content-form rows, and the sample
   records it as `capped`. The sample names three cases rather than failing them:
    - a run that did not use the final code, judged by the base its summary names, or, when
      it carries no summary, by whether its job has the notice step at all;
    - a `miss` or `split` move that the G2 ADR explains under check 1;
    - a lock commit with no associated PR.

**What the G2 ADR records.** The figures go into the ADR that G2 writes, and this list
names every record G1 assigns to it. Each item points at the text that defines it, so each
condition is stated once.

- the output of the three checks, from the commands in *Verification*;
- the `notice_replay` JSON verbatim, because `report` cannot re-run an earlier window (see
  *Baseline*);
- each content-form `miss` that check 1 explains, and each `miss` that *Design* names under
  *A hashing-code change can reclassify earlier commits*;
- each `notice-step-failed` artifact that check 2 names rather than fails, and the count of
  PR runs since `<ts>` (*Verification*, §2);
- each run, move and commit that check 3 names rather than grades, and each `capped` run
  (*Verification*, §3);
- each PR that moved `<ts>`, and each mechanical `notice.py` change that did not
  (*Steps*, item 6);
- two items from *Open questions*: the decision on whether the notice keeps `gone` rows,
  and the gap that agent sessions do not read job summaries.

## Baseline at `70591c307`

- **The lock is 22 days old and busy.** `git log --reverse -- docs/facts.lock` starts at
  `120e560ef` on 2026-09-19. In the 30 days to `70591c307`, 116 commits touched the lock, out
  of 1,282 first-parent commits. The commands are
  `git log --since=30.days --oneline 70591c307 -- docs/facts.lock | wc -l` and
  `git log --first-parent --since=30.days --format=%p 70591c307 | wc -l`, run on 2026-10-11.
- **One master commit is usually one PR, but nothing guarantees it.** The `--format=%p`
  listing above has one parent on all 1,282 lines, so no merge commit reached master.
  `gh api repos/DanielH2018/server` returns `allow_squash_merge`, `allow_rebase_merge` and
  `allow_merge_commit` all `true`. `land_lib/merge.py` sends `merge_method=squash`, but only
  for merges that go through `land.sh`. A rebase merge of a PR with several commits leaves
  one first-parent commit per branch commit.
    - No two of the 1,282 commits share a committer timestamp:
      `git log --first-parent --since=30.days --format=%ct 70591c307 | uniq -c | awk '$1>1' | wc -l`
      prints 0. That is consistent with no multi-commit rebase merge, but it is a heuristic,
      not proof.
    - For 2026-10-04 to 2026-10-10, `git log --first-parent` lists 455 commits, and the
      round-1 review counted 435 merged PRs. The gap is unexplained.
    - The replay therefore runs per commit, and *Design* says how it classifies a move that
      is split across the commits of one PR.
- **The lock's yield.** `fact_status.py report --days 60` printed 115 lock commits. The
  content forms gave 10 potential moves and 3 actual moves (prose edited in the same
  commit): symbol 2/1, YAML 1/0, test 7/2, marker 0/0. Path atoms gave 27/3.
- **The comparison the notice makes, replayed by hand over the lock's history.** A scratch
  script walked every first-parent lock commit since 2026-09-18 with `report.py:_lock_at`.
  For each moved atom it checked whether the commit's
  `git diff --no-renames --name-only` touched the cited file. For each content atom it also
  compared `atoms.py:atom_content` at the parent and at the commit.
    - All 10 content-form moves had their file in the diff and a different `atom_content`.
      None came from a commit that touched the hashing code.
    - All 27 path-form moves date from `96365b0b5` (2026-09-28) or earlier. Eight of them are
      in `96365b0b5` itself, with the cited file absent from that commit's diff.
- **The hashing code changes often.** The lock's atom identity depends on three modules.
  `citations.py:parse_citations` and `citations.py:_PATTERNS` set each citation's path and
  selector, `atoms.py:atom_content` normalises the text, and `yaml_fast.py` parses YAML.
    - `git log --first-parent 96365b0b5^..70591c307 -- scripts/lib/facts/atoms.py scripts/lib/yaml_fast.py scripts/lib/facts/citations.py`
      lists 9 commits in 13 days. Six of them touch `citations.py`. Such a commit can move
      lock hashes with no cited atom in its diff.
    - `uv.lock` pins `pyyaml` at 6.0.3, and
      `git log --first-parent --since=2026-09-19 -S'pyyaml-6.0.3.tar.gz' 70591c307 -- uv.lock`
      prints nothing, so the pin did not move in the lock's lifetime.
- **The lock's size.** `docs/facts.lock` holds 185 rows. Their atoms are 53 content atoms in
  41 files and 302 path atoms, counted from the lock's JSON. `fact_status.py status` grades
  185 sections IN and 266 CONVENTION, with no OUT or UNVERIFIED section.
- **ADR-0019's own retirement test is not met.** The ADR retires the lock when
  `report --days 60` shows fewer than two actual content moves. The figure is 3. Issue #4314
  schedules that test.
- **The hashing code reads text, but the section walk reads the checkout.**
    - Four functions read a revision. `atoms.py:atom_content` takes a file's text and strips
      docstrings, so a docstring-only edit does not move an atom. `evidence.py:_source_at`
      and `lint.py:bodies_at` read with `git show`. `report.py:_lock_at` reads the lock at a
      revision.
    - The section walk reads only the checkout. `citations.py:repo_docs` and
      `citations.py:tracked_files` run `git ls-files` on the index. `lock.py:repo_citations`
      reads each doc from disk and filters with today's tracked set. The docstring of
      `lint.py:first_cited_units` says that it judges older citations against today's
      tracked set.
    - `citations.py:in_tree` is a pure function of a citation and a set, so it accepts any
      revision's set.
- **`git ls-tree` does not glob.** `git ls-tree -r --name-only 70591c307 -- CLAUDE.md '**/CLAUDE.md'`
  lists 1 doc. The unfiltered `git ls-tree -r --name-only 70591c307`, filtered by basename,
  lists 87, the same count as `git ls-files -- CLAUDE.md '**/CLAUDE.md'`. A full
  `git ls-tree -r 70591c307` took 7 ms. One
  `git log --first-parent --no-renames --name-status` over 30 days took 64 ms.
- **`atom_content` raises on a file it cannot parse.** A symbol or test atom calls
  `ast.parse` with no handler. A YAML atom catches only `KeyError` around `safe_load`, and
  `scripts/lib/yaml_fast.py` raises `yaml.YAMLError` subclasses on bad YAML.
- **The whole-tree lint is clean and fast.** `uv run python scripts/dev/fact_status.py lint`
  exits 0 with no output in about 0.3 seconds on this worktree.
- **CI has what the notice needs.** The `hooks` job checks out with `fetch-depth: 0`, and
  the workflow holds `permissions: contents: read`. The step *Collect this PR's changed
  files* carries `if: github.event_name == 'pull_request'` and runs
  `scripts/dev/pr_changed_files.sh`, which fetches `origin/$BASE_REF`. The workflow's
  `concurrency` block sets `cancel-in-progress` for `pull_request` runs, so a push to a PR
  cancels that PR's previous run.
- **The CLI's import chain is under active edit.** `scripts/dev/fact_status.py` imports
  `lib.facts.lint`, `lib.facts.lock`, `lib.facts.relations`, `lib.facts.report`, `lib.git`
  and `lib.repo_paths` at module top, and those modules import more of `scripts/lib/`.
  `git log --first-parent --since=30.days --oneline 70591c307 -- scripts/lib/facts scripts/dev/fact_status.py scripts/lib/git.py scripts/lib/yaml_fast.py | wc -l`
  prints 15. A PR that breaks any module on that chain crashes every command that imports
  it from the PR's checkout.
- **The repository root comes from the script's own path.** `scripts/lib/repo_paths.py:REPO` is
  `parents[2]` of the module's `__file__`. A copy of `scripts/` in a second worktree
  therefore treats that worktree as the repository. Git worktrees share one object store
  and the `origin/*` refs.
- **Nothing in the `hooks` job runs `uv` before the notice's position.** The steps up to
  *Collect this PR's changed files* run `systemd-analyze`, install Vale and run
  `scripts/dev/pr_changed_files.sh`, which calls no `uv`. The setup action installs uv with
  a download cache keyed on `uv.lock`. The notice step's first `uv` call therefore builds an
  environment, and a cache miss downloads over the network. The tree at `70591c307` holds
  2,751 files and 24 MB, from `git ls-tree -r -l 70591c307`.
- **A hung step fails the job, whatever `continue-on-error` says.** The `hooks` job sets
  `timeout-minutes: 20`, and no step in `ci.yml` sets its own. `continue-on-error` covers a
  step's failure, not the job's timeout, and the `prek` gate job requires the `hooks` job to
  succeed.
- **`lib.git` strips `GIT_*` only for one-shot calls.** `lib.git.git` builds an environment
  without any `GIT_*` variable and runs one `subprocess.run`, so it cannot host a long-lived
  process. The `git-and-gh-go-through-lib` row in `test_census_rows_python.py` (in
  `ansible/tests/repo/`) finds raw calls with `_raw_call_offence`, which matches only
  `subprocess.run`. The four raw `subprocess.Popen` calls under `scripts/` start `tee`, `uv`
  or an argv held in a variable, so none of them names a program in `_ROUTED`.
- **`report` cannot re-run an earlier window.** `fact_status.py report` takes `--days`,
  `--log`, `--top` and `--json`. `report.py:build` takes a `now` that the CLI does not expose,
  so a later run measures a different 30 days.
- **CI run listings are large, capped and costly to expand.**
    - `gh api 'repos/DanielH2018/server/actions/workflows/ci.yml/runs?event=pull_request&created=2026-09-11..2026-10-10&per_page=1' --jq .total_count`
      printed 1889. The same call for 2026-10-10 alone printed 262.
    - The round-1 review measured a cap of 1,000 results on a run listing filtered by
      `created` and `event`.
    - Reading the steps of one run costs one more call. All of these calls draw on the
      5,000-an-hour quota of the user's token, which `land.sh` and other sessions share.
- **Artifacts list cheaply.**
  `gh api 'repos/DanielH2018/server/actions/artifacts?name=<name>&per_page=100'` filters the
  whole repository's artifacts by name in one call. Artifact retention is 90 days, from
  `gh api repos/DanielH2018/server/actions/permissions/artifact-and-log-retention`. No
  workflow under `.github/workflows/` uses `actions/upload-artifact` at `70591c307`.
- **The delivery check's three calls answer as the plan needs.** Each was run once on
  2026-10-11.
    - `gh api repos/DanielH2018/server/commits/70591c307/pulls` maps that squash commit to
      PR 4415 and its head `5f1864c4c`.
    - `gh api 'repos/DanielH2018/server/commits/5f1864c4ceae276f2e2fc0f0e37871bf38e65283/check-runs?check_name=hooks%20(lint%20%2B%20validate%20%2B%20secrets%20%2B%20docs)'`
      returns exactly one check run, named after the `hooks` job.
    - `gh api repos/DanielH2018/server/check-runs/114489748326/annotations` (CodeQL's
      `Analyze (python)` on the same head) returns a fileless `notice` annotation with
      `path` `.github` and its message text intact. No `hooks` run at baseline emits an
      annotation, so PR 5's own run is the first proof for a workflow-command notice.
    - `gh api repos/DanielH2018/server/actions/jobs/114489753739`, with the id of that
      head's `hooks` check run, returns the job's `run_attempt`, 1, and its 12 step names.
      A check run's id is also its job's id, so one call says whether a run had the notice
      step.
- **The dispatch census lists PR-only steps.** `SCOPED_STEPS` in
  `test_ci_dispatch_sweeps_everything.py` (in `ansible/tests/repo/`) names the steps that
  must not fire on a `workflow_dispatch`, because they diff against `github.base_ref`. Its
  `_step_conditions` reads every named step with an `if:`, whether it runs a script or uses
  an action.
- **A separate CI job would need gating.** `test_every_ci_job_is_gated.py` in
  `ansible/tests/repo/` holds every job to the `prek` gate, so the notice is a step, not a job.

## Design

The notice is one function and one subcommand. The function compares each cited atom at the
merge base with the same atom at the head, reading both through `atoms.atom_content`. The lock
compares an atom with a hash recorded weeks earlier, so an unrelated commit can invalidate it.
The notice compares only the two ends of one diff, so it moves only when the PR itself moved
the atom.

**Reading a revision.** `notice.py` carries its own reader, because the section walk in
`citations` and `lock` reads the checkout (see *Baseline*). The reader has two backends.

- *At a revision*, it runs `git ls-tree -r -z <rev>` once. That listing gives the tracked set
  at the revision and the blob id of every path. Text comes from one long-lived
  `git cat-file --batch` process. The reader caches each doc's sections by blob id, so a doc
  that no commit changed is split into sections once per run.
    - The reader asks `cat-file` only for blob ids that the listing gives. A path absent at
      the revision, such as the base side of a file the PR adds, returns `None` from the
      listing and sends no request. A `missing` reply can then come only from a damaged
      object store, and the reader raises on it rather than waiting for a size.
    - A new `lib.git` helper starts the `cat-file` process. It shares one environment builder
      with `lib.git.git`, so the process sees no `GIT_*` variable. An inherited `GIT_DIR`,
      as in a hook-run test, cannot point it at another repository.
- *At the working tree*, used only when no head revision is given, it takes the tracked set
  and the docs from `git ls-files` and reads text from disk. It skips a listed file that is
  missing on disk. The live-tree proof in *Verification* needs this backend, because it
  runs on an uncommitted edit.

`--since REF` resolves through `notice.resolve_base(repo, ref, head)`, which returns
`git merge-base REF <head>`, or `None` when either ref does not resolve. `<head>` is the
`--head REV` argument when it is given, and `HEAD` otherwise. The CLI maps `None` to exit 2.
[G3](g3-freshness-reviewer.md) calls the same function.

**What a row means.**

- **Sections** are those of every `CLAUDE.md` at the head, split with `citations.sections`.
  Their citations come from `citations.parse_citations`, with no tracked-set filter at this
  point.
- **Changed paths** come from one `git diff --no-renames --name-status <base> [<head>]`, so
  a rename reads as a delete and an add. The function skips every citation whose path the
  diff did not touch. A directory citation counts as touched when any diff path starts with
  its prefix.
- **Support is judged at both ends.** The function keeps a citation when `citations.in_tree`
  accepts it against the base's tracked set or the head's. A file the PR deletes is tracked
  at the base only, so its citation survives to become a `gone` row.
- **A content atom** is listed as `moved` when `atom_content` differs between the two ends.
  It is listed as `gone` when it resolves at the base and not at the head. It is not listed
  when it resolves only at the head, because the lock holds no earlier hash for it either.
- **A path atom** is listed as `gone` when it is tracked at the base and not at the head. A
  path atom never lists as `moved`, which matches its existence hash in the lock.
- **A marker prefix that matches twice** raises `Ambiguous`, and the row reads `ambiguous`.
- **A cited file that does not parse** at either end gives a row that reads `unparsable`.
  The function catches `SyntaxError` and `ValueError` from `ast.parse`, and `yaml.YAMLError`
  from `safe_load`, around each atom. Any other exception propagates, because it is a defect
  in the notice rather than in the PR.
- **Each row carries `prose_edited`.** It is true when `lint.bodies_at` gives the section a
  different body at the base than the reader gives it at the head. A row with
  `prose_edited: false` is the one a reader should look at.
- **The exit code never carries a finding.** `notice` exits 0 with or without rows, and with
  `unparsable` rows too. It exits 2 only when `--since` or `--head` names a ref git cannot
  resolve.

The merge base differs between CI and a local branch, but the diff it gives does not. On CI,
`git merge-base origin/master <merge ref>` is master's tip, the merge ref's first parent. On
a local branch it is the fork point. Both diffs hold the PR's own changes, so the notice
covers exactly what the PR adds. `pr_changed_files.sh` uses the same three-dot diff.

Output formats come from one function, `notice.render(rows, summary, fmt)`, so the
subcommand in `fact_status.py` only parses arguments. Every format lists rows in one order.
Content-form rows come first: `moved`, then content `gone`, then `ambiguous`, then
`unparsable`. Path-form `gone` rows come last. Rows of one kind sort by `<doc>#<heading>`.

- `text` (the default) prints one line per row:
  `<kind> <doc>#<heading> <atom> (prose edited|prose unchanged)`.
  It ends with `parsed <N> citations; <K> in <M> changed files; base <sha>`, where `<sha>`
  is the first 12 characters of the merge base. An empty notice on a code PR still shows
  how much it read. In CI the base is also the revision whose code ran (see below), so the
  line says which code produced the notice.
- `markdown` prints the same rows as a table, for `$GITHUB_STEP_SUMMARY`.
- `github` prints one `::notice` workflow command for the summary line first, then one per
  row, up to 9 rows. GitHub keeps at most 10 notice annotations per step, so the summary line
  and 9 rows fill the cap. The order keeps a rename's path rows from pushing a content row
  past the cap. The job summary carries the full list.
    - A row notice carries the unit as its `title` and `<kind> <form> <atom>` as its
      message, so a reader of the annotations can tell a content-form row from a path row.
    - A message escapes `%`, CR and LF as `%25`, `%0D` and `%0A`. A property value, such as
      the `title`, also escapes `:` and `,` as `%3A` and `%2C`. A heading can hold any of
      these characters.
- `json` prints a list of objects with the keys `unit`, `atom`, `form`, `kind` and
  `prose_edited`. `kind` is one of `moved`, `gone`, `ambiguous` and `unparsable`.
  [G3](g3-freshness-reviewer.md) consumes this shape.

**The CI step runs master's code, not the PR's.** The PR's checkout holds the PR's own
`scripts/lib/`. A PR that breaks a module on the CLI's import chain, such as a half-finished
refactor, would crash a step that ran that copy (see *Baseline*). That crash says nothing
about the notice, and an abandoned PR would leave no fix to merge. The step therefore runs
the merge base's copy of the code against the PR's merge commit.

- The step computes the base as `git merge-base "origin/$BASE_REF" "$GITHUB_SHA"`. That is
  the master commit on which GitHub built the merge commit the job checked out. The step
  adds it as a second worktree with
  `git worktree add --detach "$RUNNER_TEMP/notice-code" <base>`.
- `lib.repo_paths.REPO` comes from the script's path, so the base copy treats
  `$RUNNER_TEMP/notice-code` as the repository, and its `HEAD` is the merge base. The step
  therefore passes `--head "$GITHUB_SHA"`, so both ends go through the revision backend.
  The worktrees share the object store and `origin/*`, so `--since "origin/$BASE_REF"`
  resolves there to the same merge base.
- The step syncs the base copy's environment with `uv sync --frozen` and runs the notice
  with `uv --directory "$RUNNER_TEMP/notice-code" run --frozen`. A PR's `uv.lock` therefore
  cannot change what the notice imports either.
- A PR that changes `notice.py` or the hashing code does not see that change in its own
  run. Its tests run in the `pytest` job, and every run after it merges uses it.

**A failure of the notice step must stay visible.** With `continue-on-error: true`, GitHub
records a failed step's `outcome` as `failure` but its `conclusion` as `success`. The jobs
API reports the `conclusion`, so a query over step conclusions cannot see a crash. Listing
runs to read their steps is also capped and costly (see *Baseline*). The notice step
therefore leaves an artifact on failure, and one name-filtered call lists every such
artifact.

- The notice step carries `id: notice` and `timeout-minutes: 5`. Its script runs two
  stages and records them in `$RUNNER_TEMP/notice.err`, into which every command's stderr
  also goes.
    - It writes `stage: setup`, computes the base and writes `base <sha>`. It then adds the
      worktree and syncs the environment.
    - It writes `stage: notice` and runs the notice twice, once per output format. It then
      runs `git worktree remove --force` on the base copy, so the later steps see the one
      worktree they expect. `scripts/lib/worktrees.py` runs `git worktree list`, and a
      step that failed or timed out leaves the copy registered until the runner goes.
    - The last `stage:` line of an uploaded `notice.err` therefore says whether the failure
      came before any notice code ran. A timeout kills the script, but the lines written
      before it stay in the file.
- A hang must not reach the job's 20-minute timeout, which `continue-on-error` does not cover
  (see *Baseline*). The step's own timeout ends a hang as a step failure, so the job goes
  on. The notice itself expects to run in under a second. The `setup` stage checks out about
  2,750 files and syncs one environment from uv's cache. 5 minutes leaves wide headroom for
  both, and PR 5's body records each stage's measured time.
- Two steps follow it, and both run only under
  `github.event_name == 'pull_request' && steps.notice.outcome != 'success'`. The condition
  reads `!= 'success'` so that it holds for a timeout whether GitHub records the outcome as
  `failure` or as `cancelled`.
    - *Notice step failed* prints a `::warning title=notice-step-failed::` annotation, with
      the outcome in its message, and a job-summary line. It is a separate step because a
      timeout kills the notice step's script before any line of its own could print.
    - *Upload notice failure* uses `actions/upload-artifact`, pinned by commit SHA with a
      version comment as `actions/checkout` is. It uploads `notice.err` as an artifact named
      `notice-step-failed`.
    - Both carry `continue-on-error: true` and `timeout-minutes: 5`, so a failed or hung
      upload cannot fail the job either.
- A run that GitHub cancels before the notice step finishes leaves no notice and no
  artifact. A cancelled run does not run the two follow-up steps, because their conditions
  carry the implicit `success()`. The next push to that PR runs the step again, so the query
  needs no exclusion for cancelled runs.

**The replay** in `report` runs `moved_sections` once per first-parent commit in the window,
with the parent as the base and the commit as the head. It uses the revision backend, so it
needs no checkout, and it grades each commit against that commit's own docs and tracked set.

- **No commit is filtered out.** The replay calls the function for every first-parent commit
  in the window, and the function skips a diff that touches no cited path. The window starts
  at the last lock commit before the `--days` boundary, so a split move at the window's edge
  keeps its earlier half. At baseline cost, one `ls-tree` and one `diff` per commit, 1,282
  commits take about 15 seconds. PR 4's body records the measured time.
- **A crash is recorded, not raised.** An exception from `moved_sections` on one commit
  becomes a `crashed` entry with the commit and the exception type, and the replay goes on.
  The replay always runs the current `notice.py` over the whole window, so a fix merged
  mid-soak clears the entry on the next run. The CI-side checks cannot re-run old PR runs,
  so a `notice.py` change moves their lower bound instead (see *Steps*, item 6).
- **Parity covers the four content forms.** The replay compares its rows with the lock's own
  moves, which are the `(commit, unit, atom)` triples that `report.precision` already walks.
  That walk already skips a row whose `python` changed. Each content-form lock move takes
  the first of these classes that applies:
    - `listed`: the replay listed the unit and atom at the same commit.
    - `hashing`: the commit touches `scripts/lib/facts/atoms.py`,
      `scripts/lib/facts/citations.py` or `scripts/lib/yaml_fast.py`, or it changes the
      `pyyaml` version in `uv.lock`. Any of these can move the lock hash with no change to
      the atom. The replay reads the `pyyaml` version with `tomllib` at the parent and at the
      commit, so a `uv.lock` change to any other package, such as a Renovate bump, is not
      `hashing`.
    - `split`: the replay listed the unit and atom at an earlier first-parent commit, after
      the lock commit that recorded the atom's previous hash. A rebase merge of several
      commits leaves this shape. The window starts at that recording commit, not at the
      previous lock commit, because the commit that moved the atom may itself have touched
      the lock for another row.
    - `miss`: anything else. Only this class counts against parity.
- **A hashing-code change can reclassify earlier commits.** The replay grades every commit
  with today's hashing code, while each lock move was hashed by its own commit's code. After
  a later normalisation change, such as one that strips more of the AST, the replay can see
  no change at a commit whose own code moved the lock hash. That move then reads `miss` in a
  commit that touches no hashing file. The G2 ADR names every `miss` whose commit predates
  the window's last hashing-code change, together with that change. The baseline scratch
  replay found no such move.
- **The table names every move outside `listed`.** Path-form lock moves are counted beside
  the table as context, together with the `gone` rows. The lock commits in which a row's
  `python` changed are named beside the table too, because `precision` skips them.

```diff
 scripts/lib/
   git.py                 + a helper that starts a long-lived git process
                          ~ git(): shares the GIT_*-stripping environment builder
 scripts/lib/facts/
+  notice.py              Tree: the revision and working-tree readers
+                         moved_sections(repo, base, head) -> list[Notice]
+                         resolve_base(repo, ref, head) -> merge-base sha, or None
+                         render(rows, summary, fmt) -> str
   report.py              + notice_replay(): per-commit notices, crashes, parity classes
                          ~ precision(): also returns its (commit, unit, atom) moves
 scripts/dev/fact_status.py
+  notice [--since REF] [--head REV] [--format text|markdown|github|json]
 ansible/tests/repo/test_census_rows_python.py
                          ~ _raw_call_offence(): also matches subprocess.Popen
 .github/workflows/ci.yml   (hooks job)
+  step "Notice: CLAUDE.md sections citing atoms this PR moved"   (id: notice)
+    if: github.event_name == 'pull_request'
+    continue-on-error: true, timeout-minutes: 5
+    stage setup: merge base, worktree $RUNNER_TEMP/notice-code, uv sync --frozen there
+    stage notice: that copy's fact_status.py notice --head "$GITHUB_SHA"
+  step "Notice step failed"            (the warning annotation and summary line)
+  step "Upload notice failure"         (actions/upload-artifact, artifact notice-step-failed)
+    both: if: github.event_name == 'pull_request' && steps.notice.outcome != 'success'
+    both: continue-on-error: true, timeout-minutes: 5
```

```text
fact_status.py notice --since origin/master [--head REV]
  notice.resolve_base(repo, "origin/master", REV or HEAD)  the base, or exit 2
  notice.moved_sections(repo, base, head=REV or None)
    git diff --no-renames --name-status <base> [<REV>]    changed and deleted paths
    Tree(base); Tree(REV) or the working tree             tracked sets, doc and source text
    citations.sections / parse_citations                  sections at the head, unfiltered
    citations.in_tree against the base set or head set    support at either end
    atoms.atom_content(c, text at base)                   one end
    atoms.atom_content(c, text at head)                   the other end
    lint.bodies_at(repo, base, doc)                       prose_edited
  notice.render(rows, summary, fmt)                       content-form rows first

In CI, from the base copy, so REV is the merge commit and never the copy's HEAD:
uv --directory "$RUNNER_TEMP/notice-code" run --frozen \
  python scripts/dev/fact_status.py notice --since "origin/$BASE_REF" --head "$GITHUB_SHA"
```

**Two-writer rule (C4).** Not applicable. G1 moves no ownership: the lock stays the only
blocking check, and the notice writes nothing to the tree.

## Steps, as PRs

1. **The revision reader** (about 150 lines, half of them tests).
    - Add `scripts/lib/facts/notice.py` with `Tree` and its two backends. Give it the module
      docstring the other `scripts/lib/facts/` modules carry, and no `sys.path` insert
      beyond the one its siblings use.
    - Add the `lib.git` helper that starts a long-lived git process. Move the `GIT_*`
      stripping out of `lib.git.git` into one environment builder that both call.
    - Widen `_raw_call_offence` in `test_census_rows_python.py` to match `subprocess.Popen`
      as well as `subprocess.run`, with a red subject that starts `git cat-file --batch`
      through a raw `Popen`. Add `lib/facts/notice.py` to the row's `must_find`. The four raw
      `Popen` calls in *Baseline* name no routed program, so the widened row stays green.
    - Nothing calls `Tree` until PR 2. Its tests pin the revision behaviour that the
      checkout-bound section walk lacks.
2. **`moved_sections`** (about 240 lines, half of them tests).
    - Add `Notice` and `moved_sections` to `notice.py`, with the row rules in *Design*.
3. **The `notice` subcommand** (about 160 lines, half of them tests).
    - Add `resolve_base` and `render` to `notice.py`, with the four formats and the row
      order in *Design*.
    - Add a `notice` subcommand to `scripts/dev/fact_status.py` that calls them, and a line
      for it in the module docstring.
    - Add a `## Commands` bullet for `notice` to `.claude/rules/facts.md`.
4. **The replay and the parity table** (about 230 lines, half of them tests).
    - Add `notice_replay` to `scripts/lib/facts/report.py`, and have `precision` keep the list
      of moves it counts.
    - `render` gains a *Notice replay* table with these rows:
        - commits in the window;
        - commits carrying a notice;
        - notices, by kind;
        - prose-edited notices, by kind;
        - `moved` rows, and how many of them had their section's prose edited in the same
          commit;
        - crashes, by commit;
        - content-form lock moves by class, naming every move outside `listed`;
        - path-form lock moves, as context;
        - lock commits in which a row's `python` changed, by name, as context.
    - `--json` puts the same figures under `notice_replay`. Two keys carry the `moved` row:
      `moved` counts the `moved` rows, and `moved_prose_edited` counts those whose section's
      prose the same commit edited. A path atom never lists as `moved`, so both count
      content atoms only. [G3](g3-freshness-reviewer.md) reads these two keys.
    - `notice_replay.content_lock_moves` lists every content-form lock move with its lock
      commit, `committed_at`, unit, atom and class. `committed_at` is the commit time in UTC,
      in the `2026-10-11T12:00:00Z` form that `gh` prints for `mergedAt`, so the two compare
      correctly as strings. The delivery check in *Verification* reads that list. Each move
      was hashed by its own commit's code, so the list does not change when today's notice
      or hashing code changes.
    - `notice_replay.content_notices` lists the commit and unit of every content-form row.
      Check 3 does not read it, because today's code computes it. [G2](g2-retire-lock.md)
      keeps it after the lock, and with it `content_lock_moves`, goes.
    - Run the backfill below before merging. The PR fixes any content-form `miss` it finds.
5. **The CI steps** (about 100 lines).
    - In `.github/workflows/ci.yml`, add the notice step to the `hooks` job directly after
      *Collect this PR's changed files*. It has `id: notice`, runs only on `pull_request`,
      sets `continue-on-error: true` and `timeout-minutes: 5`, and passes `BASE_REF` through
      `env:`, as the scoping step does, so that `zizmor` accepts it.
    - The step's `setup` stage adds the merge base as a worktree and syncs its environment,
      as *Design* describes. Its `notice` stage runs that copy's
      `notice --since "origin/$BASE_REF" --head "$GITHUB_SHA"` once with `--format github`,
      and appends the `--format markdown` output to `$GITHUB_STEP_SUMMARY`. Both stages
      write their `stage:` line and copy stderr into `$RUNNER_TEMP/notice.err`.
    - Add *Notice step failed* and *Upload notice failure* directly after it, as *Design*
      describes.
    - Add all three step names to `SCOPED_STEPS` in `test_ci_dispatch_sweeps_everything.py`.
      Its `truthy` evaluator reads each `&&` arm and treats a term that is not a
      `github.event_name` comparison as false, so all three conditions evaluate false on a
      dispatch.
    - `ci.yml` is in `FULL_SWEEP_PATHS`, so this PR runs the full hook sweep. That is expected.
    - Before merge, prove the failure path twice, each time with a temporary commit.
        - Push a commit that points the notice's `--since` at `origin/does-not-exist`, so
          that `notice` exits 2 in the `notice` stage.
        - Push a commit that sets the notice step's `timeout-minutes` to 1 and runs
          `sleep 120` in the `setup` stage. This proves that a timeout reaches the follow-up
          steps and leaves the job green.
        - For each run, the `hooks` job must read `success`, and the run must carry the
          `notice-step-failed` warning. The artifact query from *Verification*, with a lower
          bound earlier than the first push, must print both runs. The last `stage:` line of
          each downloaded `notice.err` must read `notice` for the first run and `setup` for
          the second.
    - Then prove that the PR's own code cannot crash the step, with a third temporary
      commit. It adds `raise ImportError("notice proof")` at the top of the PR's
      `scripts/lib/facts/lint.py`, which `fact_status.py` imports. The run must carry the
      `parsed <N> citations` notice, and the artifact query must not print the run. This
      commit also fails the `pytest` job and may fail a hook, so this proof reads the
      annotations and the artifact listing, not a job's conclusion.
    - Then push the real commit. On its run, make the three delivery calls from
      *Verification*, using this PR's head. The annotations must include the
      `parsed <N> citations` notice, with its message and its base intact. That run is the
      first proof that a workflow-command notice reaches the annotations endpoint. The
      squash merge drops the temporary commits.
6. **Soak.** Thirty days after `<ts>`, run the commands in *Verification* and paste their
   output into the G2 ADR PR.
    - `<ts>` starts as PR 5's merge timestamp. It moves to the merge timestamp of any later
      PR that changes `scripts/lib/facts/notice.py`, changes one of the three CI steps, or
      fixes the cause of a `notice-step-failed` artifact or of a content-form `miss`,
      wherever that fix lives. Each move restarts the 30 days for all three checks. A
      mechanical `notice.py` update, defined below, is the one change to `notice.py` that
      does not move it.
    - The restart exists for the CI-side checks. The replay re-runs the current code, so a
      fix clears its `crashed` entry over the whole window. A PR run cannot be re-run with
      new code, so an artifact from before a fix stays in the listing for 90 days, and a
      run that the old code produced shows the old code's annotations.
    - Moving `<ts>` does not by itself select runs of the final code. Check 2 selects
      artifacts by creation time and check 3 selects lock moves by commit time, and either
      can still reach a run of older code. A run started before `<ts>`, a re-run of such a
      run, or a run queued across `<ts>` used a merge base without the `<ts>` commit. Each
      check therefore also reads the base the run recorded, in `notice.err` or in the
      summary line, and names a run of older code rather than grading it. A run with no
      summary is judged by whether its job has the notice step at all, which the jobs
      endpoint lists. Its start time cannot decide, because a re-run starts after its code.
    - One change to `notice.py` does not move `<ts>`: one that only follows a renamed or
      reshaped function that `notice.py` imports from `citations`, `atoms`, `lint` or
      `lib.git`. It qualifies on two conditions. Its diff leaves `render` and
      `resolve_base` alone, so the CI-only paths are untouched. Its PR body records that, in
      a scratch worktree at the PR's head,
      `fact_status.py report --days 30 --json | jq -S .notice_replay` prints identical bytes
      before and after `git restore --source <merge-base> -- scripts/lib scripts/dev/fact_status.py`.
      The two runs go back to back on one `HEAD`, so they read one window with the new code
      and the old.
    - Any other change to `notice.py` that is not a fix waits until after G2's ADR PR.
    - The functions `notice.py` imports are therefore not frozen. Other sessions keep
      editing `scripts/lib/facts/`, and a PR that renames one of those functions updates
      `notice.py` under the rule above. The table names every hashing-code move in the
      window. Check 3 samples lock moves, which each commit hashed with its own code, so a
      hashing-code change does not move `<ts>` unless it fixes the cause of a `miss`.
    - A miss fix moves `<ts>` wherever it lives, because check 3 would otherwise fail on it.
      A fix in `citations.py`, for example, turns the earlier move into `listed` in the
      replay. That move's lock commit still has its old run, which lacks the unit's notice,
      and a `listed` move has no escape in check 3.
    - The G2 ADR lists every PR that moved `<ts>`, with the run ID of each artifact it
      explains, and every mechanical `notice.py` change that did not move it.

**The backfill.** PR 4 runs the replay over the lock's whole history, with a `--days` value
that reaches back to 2026-09-19. Path-form moves and `hashing` moves found that way are
expected, and the table lists them by name. Only a content-form `miss` is fixed, in PR 4,
before the soak starts. The scratch replay in *Baseline* found no such miss.

## Verification

Run every command below on the day the 30 days after `<ts>` end, and take `<ts>` in the ISO
8601 UTC form that `gh pr view <n> --json mergedAt --jq .mergedAt` prints for the PR that
set it. A run on a later day passes `--days` as the whole days since `<ts>`, rounded up, in
place of 30.

**1. The replay.**

```bash
uv run python scripts/dev/fact_status.py report --days 30
uv run python scripts/dev/fact_status.py report --days 30 --json | jq '.notice_replay'
```

The table must show the figures in *Goal and exit criterion*, item 1. The G2 ADR records the
JSON object verbatim, including `moved` and `moved_prose_edited`.

**2. No CI crash.** The query costs one call per 100 artifacts:

```bash
gh api --paginate \
  'repos/DanielH2018/server/actions/artifacts?name=notice-step-failed&per_page=100' \
  --jq '.artifacts[] | [.created_at, .workflow_run.id, .workflow_run.head_branch] | @tsv'
```

Timestamps in that form compare correctly as strings. A line created before `<ts>` does not
fail the check. When it falls after PR 5's merge, the G2 ADR names it with the PR that moved
`<ts>` past it, as *Steps*, item 6, requires.

For each line created after `<ts>`, download its `notice.err` with one more call, into a
scratch directory:

```bash
gh run download <run-id> -R DanielH2018/server -n notice-step-failed -D <scratch-dir>
```

The G2 ADR names that line, and it neither fails the check nor moves `<ts>`, when one of
these holds:

- **The run used older code.** Its `base` line names a revision that does not contain the
  `<ts>` commit, so `git merge-base --is-ancestor <ts-commit> <base>` exits 1. A re-run, or
  a run queued across `<ts>`, has this shape.
- **The failure came before the notice ran.** The last `stage:` line reads `setup`, so
  `git worktree add` or `uv sync` failed, for example on a network error. No notice code
  runs in that stage.
- **The PR changed the step itself.** The run's PR changed one of the three CI steps, and
  the ADR quotes the hunk. The script that failed was the PR's own. If that PR merges, it
  moves `<ts>` anyway.

Any other line after `<ts>` records a notice defect: master's code failed on a PR's inputs.
It fails the check until the PR that fixes its cause merges and moves `<ts>`. The step runs
the merge base's code, so a PR's own broken import chain cannot produce such a line, and an
abandoned PR cannot leave one without a defect behind it. The deliberate failures of PR 5's
proof happened before PR 5 merged, so they also sit before `<ts>`. That proof also shows the
query prints a line for a real failure.

For the ADR's context, one more call counts the PR runs in the window. The value of
`created` is `>=<ts>`, URL-encoded:

```bash
gh api 'repos/DanielH2018/server/actions/workflows/ci.yml/runs?event=pull_request&created=%3E%3D<ts>&per_page=1' \
  --jq .total_count
```

**3. Delivery.** List the lock moves to sample. Each was hashed by its own commit's code, so
the list does not depend on today's notice or hashing code:

```bash
uv run python scripts/dev/fact_status.py report --days 30 --json \
  | jq -r --arg ts '<ts>' '.notice_replay.content_lock_moves[]
      | select(.class != "hashing" and .committed_at > $ts)
      | "\(.commit) \(.unit) \(.class)"'
```

For each lock commit, run three calls. They map the commit to its PR, find that PR's last
`hooks` run, and read its annotations:

```bash
gh api repos/DanielH2018/server/commits/<sha>/pulls --jq '.[0].head.sha'
gh api 'repos/DanielH2018/server/commits/<head>/check-runs?check_name=hooks%20(lint%20%2B%20validate%20%2B%20secrets%20%2B%20docs)' \
  --jq '.check_runs | max_by(.started_at) | .id'
gh api repos/DanielH2018/server/check-runs/<id>/annotations \
  --jq '.[] | [.annotation_level, .title, .message] | @tsv'
```

First decide whether the run used the final code. The notice step ran the merge base's
code, and the summary annotation names that base.

- When the summary names a base that contains the `<ts>` commit, by
  `git merge-base --is-ancestor <ts-commit> <base>`, the run is graded.
- When it names a base without the `<ts>` commit, the sample skips the run and names it. A
  re-run of an old run, or a run queued across `<ts>`, has this shape.
- When the run carries no summary, its start time does not settle the question, because a
  re-run of a run from before PR 5 starts late but runs the old workflow file. Read the
  job's steps instead. A check run's id is also its job's id:

  ```bash
  gh api repos/DanielH2018/server/actions/jobs/<id> \
    --jq '[.run_attempt, (.steps | map(.name))]'
  ```

  A job without the notice step predates PR 5's steps, and the sample skips and names it.
  A job with the step but no summary is graded, and it fails, unless check 2 named its
  `notice-step-failed` artifact, in which case it is skipped and named here too.

A graded run passes when both of these hold:

- one notice annotation carries the `parsed <N> citations` line, with N at least the floor
  of 300 that the non-vacuity test uses;
- a notice annotation names the move's unit, or the run carries 9 row notices and every one
  of them is a content-form row. The sample records the second case as `capped`. A run with
  9 row notices that include a path-form row, and no notice for the unit, fails. The row
  order puts every content-form row before a path row, so such a run emitted every
  content-form row it had.

A `split` move maps through its lock commit. A rebase merge of one PR places that commit in
the same PR as the commit that moved the atom, and that PR's run lists the unit. A move can
also span two PRs. In one example, a semantic merge leaves master's lock stale, and a
fix-forward PR runs `verify` without touching the cited file. The lock commit then maps to
a PR whose run carries no notice for the unit, and the replay reads the move as `miss` or
`split`. A graded run of a `miss` or `split` move that lacks the unit's notice is therefore
named, not failed, when the G2 ADR explains that move under check 1. A `listed` move has no
such escape. A commit with no associated PR is skipped and named.

At about five content moves a month, the check costs about 15 calls, and at most about 30.
A `notice-step-failed` warning in any sampled run would also show here, so the sample
cross-checks check 2.

**Red-proof for each new check.** All are paired tests in `scripts/lib/tests/` against a
fixture git repository. Each test commits a base, branches, and calls `moved_sections` or
the reader:

- `..._changed_symbol_body_is_listed`: a commit that changes the body of a cited function
  lists that section as `moved`.
- `..._docstring_only_change_is_not_listed`: a commit that changes only the cited function's
  docstring lists nothing.
- `..._uncited_change_is_not_listed`: a commit that changes a function no section cites lists
  nothing.
- `..._deleted_cited_path_is_listed_gone`: a commit that deletes a cited file lists the
  section as `gone`. This pins the base-side support judgement.
- `..._deleted_file_under_cited_dir_is_listed_gone`: a commit that deletes the last file
  under a cited directory lists the section as `gone`.
- `..._edited_section_reads_prose_edited`: a commit that moves an atom and edits the citing
  section's prose sets `prose_edited` on its row.
- `..._base_that_moved_on_is_ignored`: a base branch that changed the atom after the branch
  point lists nothing. This test pins the merge-base semantics.
- `..._since_resolves_against_head`: with `--head` naming an older commit of a branch, the
  merge base and the rows come from that commit, not from `HEAD`.
- `..._unparsable_python_is_listed` and `..._unparsable_yaml_is_listed`: a head with a syntax
  error in a cited `.py` file, or a YAML error in a cited `.yml` file, lists an `unparsable`
  row and returns. `..._changed_symbol_body_is_listed` is the parsable half of the pair.
- `..._section_gone_at_head_is_read_at_the_revision`: a fixture adds a section that cites an
  atom, moves the atom in commit C, and then deletes the section. `moved_sections(C^, C)`
  lists the section. The same section read through the working-tree backend at the final
  commit lists nothing, which proves the two backends differ where they should.
- `..._symbol_deleted_from_surviving_file_is_listed_gone`: a commit that deletes a cited
  function from a file that stays lists the section as `gone`.
  `..._changed_symbol_body_is_listed` is the half in which the function survives.
- `..._added_cited_file_is_not_listed` and `..._added_cited_symbol_is_not_listed`: a commit
  that adds a file, or adds a function to an existing file, together with a section that
  cites it, lists nothing. `..._changed_symbol_body_is_listed` is the half in which the atom
  exists at the base.
- `..._ambiguous_marker_is_listed`: a commit that makes a cited `DECIDED:` marker prefix match
  two lines lists the section as `ambiguous`. Its pair, `..._changed_marker_is_listed_moved`,
  edits the one matching line and lists `moved`.
- Reader tests, in PR 1:
    - `..._path_absent_at_revision_reads_none`: asked for a path the revision does not hold,
      the reader returns `None`. A read of a present path on the same `cat-file` process
      then returns that path's text, which proves no reply was left unread. A present path
      returning its text is the other half.
    - `..._reader_ignores_inherited_git_dir`: with `GIT_DIR` set to a second fixture
      repository, the reader still lists and reads the first fixture. Its other half is the
      census row's red subject, a raw `subprocess.Popen` of `git cat-file --batch`, which
      the widened `_raw_call_offence` flags.
- Emitter tests, in PR 3:
    - `..._github_format_caps_at_nine_rows`: `render` in `github` format, given 12 rows of
      which 3 are path-form, prints exactly 10 notices. The first is the summary, carrying
      `parsed <N> citations`. The other 9 are content-form rows in the order *Design* gives,
      so no path row is among them. Its pair passes 5 rows and gets the summary and all 5.
      In both, the summary ends with `base` and the first 12 characters of the base it was
      given.
    - `..._github_format_escapes_properties_and_messages`: a heading that holds `%` and `,`
      reads `%25` and `%2C` in the notice's `title`, and a message that holds a line feed
      reads `%0A`.
- A CLI test in `scripts/dev/tests/test_facts_cli.py` asserts that `notice` exits 0 with
  rows present, exits 0 with an `unparsable` row, and exits 2 when `resolve_base` cannot
  resolve the ref.
- Replay tests in `scripts/lib/tests/test_facts_report.py`:
    - a fixture history with one lock commit and a matching notice reads `listed`, with
      zero misses;
    - the same history with a notice stub that returns nothing reads one named `miss`;
    - a lock commit that also touches `scripts/lib/facts/atoms.py` reads `hashing`, not
      `miss`, and so does one that touches `scripts/lib/facts/citations.py`;
    - a lock commit that changes the `pyyaml` version in `uv.lock` reads `hashing`. Its pair
      changes another package's version in `uv.lock`, under a notice stub that returns
      nothing, and reads `miss`;
    - a window holding one prose-edited `moved` row, one unedited `moved` row and one `gone`
      row reports `moved` 2 and `moved_prose_edited` 1;
    - each `content_lock_moves` entry carries `committed_at` in the form that ends in `Z`;
    - a two-commit fixture, where commit A moves the atom and commit B updates the lock,
      reads `split`;
    - the same fixture, where commit A also updates the lock for another row, still reads
      `split`, which pins the window to the commit that recorded the atom's previous hash;
    - a history that moves an atom in commit C and deletes the citing section later still
      lists the section at C, with zero misses;
    - a notice stub that raises on one commit gives one `crashed` entry, and the replay
      still reports the other commits.
- Non-vacuity: a test runs `moved_sections` on the live tree against `HEAD` and asserts that
  the parsed-citation count is at least 300. A second assertion runs both reader backends at
  `HEAD` and asserts that they list the same docs, at least 80 of them. That proves the walk
  reaches the real docs at a revision. At `70591c307` the lock records 355 atoms, and the
  tree holds 87 `CLAUDE.md` files.

**Live-tree proof, after PR 3 and before PR 5 merges.** In a scratch branch of a worktree,
change the value of `scripts/lib/render_guard.py:BUILT_IMAGE_TAG_STUBS`. Do not commit the
change. Then run:

```bash
uv run python scripts/dev/fact_status.py notice --since origin/master
```

The output must list the `ansible/roles/k8s/image-builder/CLAUDE.md` section that cites the
symbol. Discard the change afterwards with `git restore scripts/lib/render_guard.py`.

The first PR after PR 5 that moves a cited atom must show the table in its run summary and
the annotations in its `hooks` run. The lock forces such a PR roughly every few days, so no
synthetic PR is needed. Check 3 repeats this proof for every such PR at day 30.

## Rollback

- **PRs 1 to 3** are reverts. Nothing outside `fact_status.py` reads `notice.py` until PR 4.
- **PR 4** is a revert. `report` returns to its two tables.
- **PR 5** is a revert. The steps write only the job summary, annotations and, on a failure,
  an artifact that expires after 90 days. The base copy is a worktree under
  `$RUNNER_TEMP`, which goes with the runner. A failing step cannot block a merge, because all
  three steps carry `continue-on-error: true` and the `hooks` job stays green. A hung step
  cannot block one either. Each step's `timeout-minutes: 5` ends the hang as a step failure,
  which `continue-on-error` covers, long before the job's 20-minute timeout, which it does
  not. PR 5's proof exercises both paths before merge.
- **A partial failure** leaves the lock fully in force. The worst case is a notice that is
  wrong or missing on some PRs. The parity table shows the first case. The artifact query
  and the delivery check show the second.

## Risks

Each risk is checked against `docs/failure-classes.md`.

- **The step fails quietly.** `continue-on-error` turns a crash into a green job. This is
  failure class 3, where an empty result reads as clean. Four things mitigate it:
    - the `parsed <N> citations` line, which the delivery check reads back;
    - the `notice-step-failed` artifact, which one API call enumerates for the whole window;
    - the replay's `crashed` entries, which need no API call;
    - the blocking unit tests in the `pytest` job, which run on every PR.
- **The notice runs but nobody sees it.** A wrong base, a broken `github` emitter or a lost
  summary all exit 0. This is also failure class 3. The delivery check reads the
  annotations of every PR that moved a content atom, so it sees what a reader saw.
- **An author's syntax error crashes the notice.** A PR head with an unparsable cited file
  would otherwise crash the step and count as a CI crash. The `unparsable` row kind turns it
  into a row, and its paired tests prove it.
- **The PR's own code crashes the step.** This crash source is distinct from a notice
  defect. `fact_status.py` imports six modules at module top, they import more, and 15
  commits touched that chain in 30 days (see *Baseline*). A step that ran the PR's copy
  would crash on a half-finished refactor. An abandoned PR's artifact would then fail check
  2 with no fix to merge, and a fixed one would restart the soak. This is failure class 2,
  where a step failure stands in for a notice defect. The step runs the merge base's code
  in the merge base's environment, so the PR's copy of `scripts/lib/` never runs. PR 5's
  third temporary commit proves it.
- **The step fails for a reason outside the notice.** `git worktree add` or the `uv sync`
  network fetch can fail on a runner. The `setup` stage line in `notice.err` marks such a
  failure, and check 2 names it without failing. No notice code runs in that stage, so the
  rule cannot hide a notice defect.
- **The CI notice lags a PR's own change to the notice.** A PR that changes `notice.py` or
  the hashing code shows master's behaviour in its own run. Its tests run in the `pytest`
  job. Every run after it merges uses it, and the change moves `<ts>` unless it is the
  mechanical kind that *Steps*, item 6, exempts.
- **The replay grades a commit against today's docs.** This is failure class 7, where the
  checkout is not the thing being checked. The revision backend reads each commit's own docs
  and tracked set, and `..._section_gone_at_head_is_read_at_the_revision` pins it.
- **A move is split across commits.** A rebase merge can put the atom change and the lock
  update in two first-parent commits. The `split` class names such a move and keeps it out
  of the miss count. A move can also span two PRs, so its lock commit's run carries no
  notice for the unit. Check 3 then names it when the G2 ADR explains it under check 1,
  rather than failing a run that had nothing to list.
- **A hung step outlasts `continue-on-error`.** A job timeout fails the `hooks` job, and with
  it the required `prek` gate, on every PR. The step-level `timeout-minutes: 5` turns a hang
  into a step failure first. The reader sends `cat-file` only blob ids from its listing, so
  the one request that could wait on a reply that never comes is not made. PR 5's
  timeout proof shows that a timed-out step leaves the job green and uploads the artifact.
- **The hashing code changes during the soak.** A commit to `atoms.py`, `citations.py` or
  `yaml_fast.py`, or a `pyyaml` bump, can move lock hashes with no atom in its diff. The
  `hashing` class names each such move. The replay grades old commits with today's hashing
  code, so a later change can turn an earlier move into a `miss`. This is failure class 7.
  The G2 ADR names every `miss` that predates the window's last hashing-code change, and
  check 3 samples lock moves, which each commit hashed with its own code.
- **The soak restarts.** A change to `notice.py` or a CI step, or a fix to a CI crash or to a
  content-form miss, moves `<ts>` and restarts the 30 days. Old PR runs cannot be re-run with
  new code, so this is the only way checks 2 and 3 can grade the final code. The cost is a
  later G2. A change to
  `notice.py` that is not a fix waits until after G2's ADR PR, so only a real defect costs
  time. The one exception is a mechanical update that follows a renamed import, which the
  replay proof in *Steps*, item 6, shows changes no row. Without it, any rename in
  `citations`, `atoms` or `lint` would either restart the soak or wait for G2.
- **A run of older code reaches a CI-side check.** Check 2 selects artifacts by creation
  time and check 3 selects lock moves by commit time. A re-run, a queued run or a PR whose
  last push came before `<ts>` can still carry older code. This is failure class 7, where
  the run is not the code being checked. Each run records its base, in `notice.err` and in
  the summary line, and both checks name a run whose base lacks the `<ts>` commit.
- **The long-lived `cat-file` process reads another repository.** A raw `subprocess.Popen`
  inherits `GIT_DIR`, and a hook-run test sets it. This is failure class 7. The `lib.git`
  helper strips `GIT_*`, a reader test sets `GIT_DIR` to a second repository, and the
  widened census row flags a raw `Popen` of `git`.
- **The yield figure is confounded.** The lock forces a re-read of every moved atom, so
  "notices that led to a prose edit" measures behaviour under the lock. The exit criterion
  therefore rests on parity, and the yield is reported as context.
- **The sample is small.** About five content moves a month reach master. To extend the
  sample, PR 4 replays the lock's whole history since 2026-09-19 before the soak starts.
- **The notice observes a proxy.** It sees an atom change, not a sentence going false. This
  is failure class 2. The lock used the same proxy, so G1 neither gains nor loses here. The
  notice's improvement is that the proxy no longer blocks.
- **The CI checkout is not the developer's checkout.** CI checks out a merge ref, and a
  developer runs a branch. This is failure class 7. The two merge bases differ, but both
  diffs hold only the PR's own changes. The fixture test with a base that moved on pins
  that.
- **A failed upload hides a crash from check 2.** The upload step carries
  `continue-on-error: true`, so an upload that fails leaves no artifact. The
  `notice-step-failed` warning annotation remains, because a separate step prints it before
  the upload, and the delivery sample may see it. The replay's `crashed` entries cover any
  crash that also happens on master.
- **Annotations are capped.** GitHub keeps at most 10 notice annotations per step. The step
  emits the summary line and at most 9 rows, and the job summary carries the rest. The
  emitter puts content-form rows before path rows, so a rename's path rows cannot push a
  content row past the cap. A `capped` run passes check 3 only when all 9 emitted rows are
  content-form rows.
- **The API budget.** The three exit checks cost about 1 + 1 + 30 calls against the shared
  5,000-an-hour quota, plus one download for each artifact created after `<ts>`. Check 3's
  base test is a local `git` call. No check lists the window's runs one by one.
- **The non-vacuity floor meets other areas' deletions.** The floor of 300 parsed citations
  counts the role docs. R4, D6 and H7 delete role trees and their `CLAUDE.md` files. The
  slice that deletes them lowers this floor in the same PR, as the owner of a census row
  does. The floor fails loudly first, so the deletion cannot pass it silently.
- **Runtime.** The whole-tree lint takes about 0.3 seconds. The notice reads only changed
  files at two revisions, so its CI cost should be smaller still. The `setup` stage adds a
  checkout of about 2,750 files and an environment sync. Later `uv run` steps in the job
  sync the PR's own environment, so the sync is extra work, mostly served from uv's cache.
  PR 5's body records each stage's measured time, and PR 4's body records the replay's.

## Tests and docs that change in the same PR

- **PR 1:**
    - the reader tests, including the backend half of the non-vacuity test, the absent-path
      test and the inherited-`GIT_DIR` test, in `scripts/lib/tests/`;
    - a test in `scripts/lib/tests/test_git.py` that the shared environment builder drops
      an inherited `GIT_DIR`;
    - the widened `_raw_call_offence`, its red `Popen` subject and the `must_find` entry in
      `test_census_rows_python.py`;
    - `lib.git`'s module docstring, which names the long-lived helper.
- **PR 2:**
    - the paired `moved_sections` tests and the citation-count non-vacuity test in
      `scripts/lib/tests/`.
- **PR 3:**
    - the emitter tests in `scripts/lib/tests/`;
    - the CLI test in `scripts/dev/tests/test_facts_cli.py`;
    - the `notice` bullet in `.claude/rules/facts.md`;
    - `scripts/dev/fact_status.py`'s module docstring.
- **PR 4:**
    - the replay tests in `scripts/lib/tests/test_facts_report.py`;
    - `report.py`'s module docstring, which gains the third measurement;
    - the `report` bullet in `.claude/rules/facts.md`, which names the `moved` and
      `moved_prose_edited` keys.
- **PR 5:**
    - the three `SCOPED_STEPS` entries in `test_ci_dispatch_sweeps_everything.py`;
    - the step comments in `ci.yml`, saying why no step can fail the job, why each carries
      its own timeout, why the failure leaves an artifact, why the notice runs the merge
      base's code with `--head "$GITHUB_SHA"`, and what each `stage:` line means to check 2.
- **Generated pages.** `docs/reference/scripts.md` lists the added module after the next
  docs-refresh run. No PR hand-edits it.

## Complexity delta

- **Added:**
    - `notice.py`, about 200 lines, of which the reader is about 50 and `render` and
      `resolve_base` about 40;
    - the `lib.git` helper and its shared environment builder, about 15 lines;
    - the subcommand, about 15 lines;
    - the replay and its parity classes, about 110 lines;
    - three CI steps, about 70 lines with their comments, of which the `setup` stage that
      runs the merge base's code is about 15;
    - one pinned action, `actions/upload-artifact`, which Renovate's github-actions manager
      keeps current;
    - one more call shape in the census row `git-and-gh-go-through-lib`, about 5 lines;
    - about 430 lines of tests.
- **Removed:** nothing in G1. G2 removes about 3,800 lines of lock machinery.

## Open questions

- **Should the notice also print at commit time?** A prek hook with `verbose = true` could
  print it and exit 0. Resolution: not in G1. A prek hook runs only on staged files, while a
  code-only PR needs the notice in CI. G3 covers the local surface if the post-G2 yield shows
  the CI notice goes unread.
- **Agent sessions do not read job summaries.** `gh pr checks` shows neither the summary nor
  the annotations. Resolution: no gap during G1, because the lock still blocks. G2's ADR
  names the gap, and G3 is its remedy.
- **Are `gone` path rows noise?** A rename lists every section citing the old path.
  Resolution: the replay counts `gone` rows separately from `moved` rows, and the G2 ADR
  decides from that count whether the notice keeps them. Meanwhile the row order puts path
  rows last, so they never take an annotation slot from a content row.
- **Does the delivery check need a script?** It is about 15 calls by hand at day 30.
  Resolution: keep it a documented recipe in G1. If G2 keeps the delivery check after the
  lock goes, G2 turns it into a subcommand with a paired test.
- **G2 and G3 restated G1 in words that rounds c1 and c2 made stale.** G1 may not edit
  either slice, so this is a request to their owners. Rounds c2 and integration-2 raised it.
    - G2's PR 1 entry condition restated G1's three checks. It said that check 2's query
      "prints nothing", that the bound moves only when a later G1 PR changed "either CI
      step", and that "G1's step 6 lets a `notice.py` fix during the soak skip the restart".
      None of these held. Check 2 allows artifacts before `<ts>` and three named kinds after
      it. `<ts>` moves on any change to `notice.py` except a mechanical one, on a change to
      any of three steps, and on a fix for a crash or a miss. The step runs the merge base's
      code.
    - G3's *Goal* said that G1 PR 3 names `moved` and `moved_prose_edited`. G1 PR 4 adds
      them.
    - G3 carried a fallback that added `notice.resolve_base` "unless G1 already has it".
      G1 PR 3 adds the function and its tests, and G3 depends on G2, which opens only after
      G1's exit criterion holds. The fallback could therefore never fire.
  Resolution: both owners applied the change in round integration-2. G2's PR 1 entry
  condition cites G1's exit items 1 to 3 and *Steps*, item 6, and says that G1's text
  governs. G2's ADR *Context* bullet points at G1's sections, which hold *What the G2 ADR
  records*. G3 says G1 PR 4 and names `resolve_base` as G1's function. Wherever G2 or G3
  still differs, G1's text governs.

## Review log

### Round 1 (per-slice adversarial review)

Findings raised, all seven fixed. None refuted.

- **G1-R1-01 (high): the exit query could not enumerate the 30-day window, and its pass
  condition could never hold.** Fixed. Re-measured:
  `gh api '…/workflows/ci.yml/runs?event=pull_request&created=2026-09-11..2026-10-10&per_page=1'`
  reports `total_count` 1889, and 262 for 2026-10-10 alone. Both exceed the old
  `--limit 500`. The concurrency block confirms the cancelled-run point. The fix drops
  run-by-run listing altogether.
    - *Notice step failed* uploads a `notice-step-failed` artifact.
    - One name-filtered `actions/artifacts` call, bounded below by PR 5's ISO merge
      timestamp, lists every failure. Its pass condition is an empty listing.
    - The replay records `crashed` entries with no API call.
    - The delivery sample cross-checks both, so the exit checks cost about 32 calls in all.
    - Cancelled runs need no exclusion, because they upload nothing.
    - The deliberate failure-path run falls before the lower bound.
- **G1-R1-02 (high): the section walk read the checkout, not the replayed revision.**
  Fixed. Confirmed in the tree: `citations.py:repo_docs` and `citations.py:tracked_files`
  run `git ls-files`, and `lock.py:repo_citations` reads from disk. *Design* adds the
  reader with a revision backend and a working-tree backend. `--since` resolves against
  `--head`. The replay calls the function for every first-parent commit, so no lock commit
  can be filtered out. Three tests pin it: `..._section_gone_at_head_is_read_at_the_revision`,
  `..._since_resolves_against_head`, and a replay test whose cited section is deleted after
  the move. PR 1 split into a reader PR and a `moved_sections` PR for the extra code.
    - Correction to the finding's suggested command: `git ls-tree -r --name-only <rev> -- CLAUDE.md '**/CLAUDE.md'`
      lists 1 doc at `70591c307`, because `ls-tree` does not glob. The plan uses the
      unfiltered listing filtered by basename, which lists 87.
- **G1-R1-03 (medium): parity had no exclusion for lock moves that no diff can see.**
  Fixed. A scratch replay reproduced the finding's figures: 27 path-form moves, all at or
  before `96365b0b5`, 8 of them in that commit with the file absent from its diff. It also
  found 10 content-form moves, all with the file in the diff and a changed `atom_content`.
  Parity now covers the content forms only. A `hashing` class names moves in commits that
  touch `atoms.py` or `yaml_fast.py`, and a context row names the lock commits whose rows
  changed `python`. The backfill fixes only a content-form `miss`. The "merge nothing
  further" freeze became a rule that the table names every hashing-code move.
- **G1-R1-04 (medium): one master commit does not provably stand for one PR.** Fixed.
  Confirmed: `allow_rebase_merge` and `allow_merge_commit` are `true`. *Baseline* drops the
  "replay over PRs" sentence. It records the shared-committer-timestamp count of 0 as a
  heuristic only. A `split` class keeps a move split across a PR's commits out of the miss
  count. Its window starts at the lock commit that recorded the atom's previous hash.
- **G1-R1-05 (medium): a parse error in a cited file crashed the notice.** Fixed.
  Confirmed: `atoms.py:atom_content` calls `ast.parse` with no handler and catches only
  `KeyError` around YAML. The `unparsable` row kind and its paired tests cover it, and the
  CLI test asserts exit 0 on such a row. A `notice.py` fix during the soak needs no
  restart, because the replay re-runs the current code. A CI-step fix restarts the soak.
- **G1-R1-06 (medium): the exit criterion never checked what CI showed.** Fixed. Exit check
  3 reads the `hooks` annotations of every PR that moved a content atom. It asserts the
  `parsed <N> citations` notice with N of at least 300, and the unit's own notice unless
  the 10-annotation cap explains its absence. *Baseline* records one run of each of its
  three calls against PR 4415. PR 5's pre-merge proof runs them again against its own
  `hooks` run, the first one that emits a workflow-command notice.
- **G1-R1-07 (medium): the `gone` rows need the base's tracked set.** Fixed. *Design* parses
  head-side citations without the filter and keeps one that `citations.in_tree` accepts at
  either end. A directory counts as touched when any diff path falls under its prefix. A
  test covers a deleted file under a cited directory.

### Round c1 (per-slice adversarial review)

Findings raised: one high and six medium. All seven fixed. None refuted.

- **G1-C1-01 (high): the R1-05 "no restart" fix held only for the replay.** Fixed. This
  corrects G1-R1-05, whose last two sentences no longer describe the plan.
    - Confirmed: artifact retention is 90 days, so a crash fixed mid-soak left check 2
      failing. Verification §2 both required an empty listing and asked the ADR to explain
      each line.
    - Check 3 sampled rows that today's code computes. Option (a) of the finding protects
      that sample from `notice.py` changes only. The hashing code also feeds it, and that
      code moved in 9 commits in 13 days (the *Baseline* command), so a restart on each
      change would never end and no restart would let drift fail the sample.
    - The fix has two parts. `<ts>` now starts at PR 5's merge and moves to the merge of any
      later PR that changes `notice.py` or a CI step, or that fixes the cause of a
      `notice-step-failed` artifact. Checks 2 and 3 grade only what follows `<ts>`, and the
      30 days restart with it.
    - Check 3 now samples `notice_replay.content_lock_moves`, the lock's own content-form
      moves outside `hashing`. Each commit hashed its move with its own code, so
      the sample does not depend on today's code. Each entry carries `committed_at` in the
      `Z` form, which compares as a string with `<ts>`. `content_notices` stays in the JSON,
      because G2 keeps it after the lock goes, but no G1 check reads it.
    - Exit item 2 and Verification §2 state one rule: no artifact after `<ts>`, and the ADR
      names each artifact before it with the PR that moved `<ts>`.
- **G1-C1-02 (medium): a hung notice step escapes `continue-on-error`.** Fixed. Confirmed in
  `ci.yml`: the `hooks` job sets `timeout-minutes: 20`, no step sets its own, and the `prek`
  gate's step *Require all four halves to have succeeded* reads `needs.hooks.result`.
    - Each of the three steps now carries `timeout-minutes: 5`.
    - A timeout kills the notice step's script, so the warning moved to a separate *Notice
      step failed* step, ahead of *Upload notice failure*.
    - Both follow-up steps test `outcome != 'success'`, which holds for a timeout whether
      GitHub records it as `failure` or as `cancelled`.
    - PR 5 proves the timeout path with a temporary commit before merge.
    - The reader asks `cat-file` only for blob ids from its own listing, so a path absent at
      the base sends no request. A reader test pins that.
    - The Rollback sentence names the step timeout as the mechanism for a hang.
- **G1-C1-03 (medium): red-proofs were missing for three row rules and the emitter.**
  Fixed. Added paired tests for a symbol deleted from a surviving file, for an added cited
  file or symbol, and for an ambiguous marker. Added two emitter tests: the 9-row cap with
  the summary first and no path row among 9 content rows, and the escaping of `%`, `,` and
  a line feed.
- **G1-C1-04 (medium): the keys G3 requests were not in G1.** Fixed. Confirmed in
  `g3-freshness-reviewer.md`, *Open questions*, and in `fact_status.py`, whose `report`
  takes only `--days`, `--log`, `--top` and `--json`.
    - PR 4 adds `notice_replay.moved` and `notice_replay.moved_prose_edited`, with a table
      row and a test. The keys belong to the replay, so they land in PR 4, not in PR 3 as
      G3 states.
    - PR 3 adds `notice.resolve_base(repo, ref, head)`, which returns `None` for a ref that
      does not resolve. The CLI maps `None` to exit 2.
    - G3's third request needs no change. `moved_sections(..., head=REV)` already reads the
      diff, both `atom_content` ends and the head side of the prose comparison at `REV`
      through the revision backend.
    - Because `report.py:build` takes a `now` that the CLI does not expose, the G2 ADR
      records the `notice_replay` JSON verbatim.
- **G1-C1-05 (medium): the long-lived `cat-file` process bypassed `lib.git`.** Fixed.
  Confirmed: `lib.git.git` strips `GIT_*` around one `subprocess.run`, and
  `_raw_call_offence` matches only `run`.
    - PR 1 adds a `lib.git` helper for long-lived processes that shares the environment
      builder, and a reader test with `GIT_DIR` set to a second repository.
    - PR 1 also widens `_raw_call_offence` to `Popen` and adds `lib/facts/notice.py` to the
      row's `must_find`. Checked first: the four raw `Popen` calls under `scripts/` start
      `tee`, `uv` or a variable argv, none of them in `_ROUTED`.
- **G1-C1-06 (medium): the `hashing` class omitted `citations.py` and the PyYAML pin.**
  Fixed. Confirmed: `git log --first-parent --since=2026-09-19 70591c307 -- scripts/lib/facts/citations.py`
  lists 8 commits.
    - `hashing` now covers `citations.py`, plus a change of the `pyyaml` version in
      `uv.lock`, read with `tomllib` at both ends. Any other `uv.lock` change, such as a
      Renovate bump, stays out of `hashing`, so a bump cannot hide a miss.
    - *Baseline* records 9 hashing-code commits since `96365b0b5^`, and a `pyyaml` pin
      that stayed at 6.0.3. *Design* states that a later hashing change can reclassify an
      earlier move, and the ADR names any such `miss`.
- **G1-C1-07 (medium): check 3's verdict for a `capped` run was undefined.** Fixed.
    - The emitter orders rows `moved`, content `gone`, `ambiguous`, `unparsable`, then
      path rows. Each row notice carries its kind and form in its message.
    - Exit item 3 and Verification §3 pass a `capped` run only when all 9 emitted rows are
      content-form rows.

### Round c2 (per-slice adversarial review)

Findings raised: one high and four medium. All five fixed. None refuted.

- **G1-C2-01 (high): check 2 counted a crash caused by the PR's own broken code as a notice
  crash.** Fixed with the finding's option (a), plus a narrow rule for external causes.
    - Confirmed at `70591c307`: `scripts/dev/fact_status.py` imports `lib.facts.lint`,
      `lib.facts.lock`, `lib.facts.relations`, `lib.facts.report`, `lib.git` and
      `lib.repo_paths` at module top. The finding's `git log … | wc -l` prints 15. Nothing
      in the `hooks` job runs `uv` before the notice's position, so the step's first `uv`
      call builds an environment and can need the network.
    - The step now runs the merge base's code from a second worktree, in that worktree's
      environment, against `--head "$GITHUB_SHA"`. `scripts/lib/repo_paths.py:REPO` comes from the
      script's path, which is why `--head` must be explicit. The PR's copy of `scripts/lib/`
      never runs.
    - The step records `stage: setup`, the base, and `stage: notice` in `notice.err`. Check 2
      names three kinds of artifact after `<ts>` without failing them: a run of older code,
      a failure in the `setup` stage, and a failure of a PR's own change to the step.
    - PR 5 gains a third temporary commit that breaks `scripts/lib/facts/lint.py` in the PR.
      Its run must carry the summary notice and leave no artifact. *Risks* names the import
      chain as a crash source distinct from a notice defect.
- **G1-C2-02 (medium): check 3 selected moves by merge time, so it could grade a run of old
  code, or a run with no notice step.** Fixed. The summary line now ends with
  `base <sha>`, and in CI the base is the revision whose code ran. Check 3 grades a run
  only when that base contains the `<ts>` commit. Comparing `started_at` with `<ts>`, as the
  finding proposed, misreads a re-run, which starts after its code. A re-run of a run from
  before PR 5 has no notice step and would fail. A run with no summary is therefore judged
  by its job's steps: `gh api repos/DanielH2018/server/actions/jobs/114489753739`, the
  `hooks` check run of PR 4415's head, returned `run_attempt` 1 and 12 step names on
  2026-10-11. A job without the notice step is skipped and named, the same way as a commit
  with no PR. Step 6 no longer claims that moving `<ts>` alone limits the checks to the
  final code.
- **G1-C2-03 (medium): check 3 had no disposition for a `miss` or `split` that check 1
  lets the ADR explain.** Fixed. A graded run of a `miss` or `split` move that lacks the
  unit's notice is named when the G2 ADR explains that move under check 1. Verification §3
  no longer says that a `split` always maps to one PR, and gives the fix-forward example.
  Not reproduced: the 60-day scratch replay found no such move.
- **G1-C2-04 (medium): G2 and G3 contradicted G1 after round c1.** Fixed in G1's scope.
  Confirmed in `g2-retire-lock.md`, PR 1's entry condition, and in
  `g3-freshness-reviewer.md`, *Goal and exit criterion*. G1 may not edit either slice, so
  an Open question records the stale sentences, and this round's cross-slice notes ask G2
  to cite G1's exit items and step 6, and ask G3 to say PR 4.
- **G1-C2-05 (medium): moving `<ts>` on every `notice.py` change froze the functions it
  imports.** Fixed. A `notice.py` change that only follows a renamed or reshaped import
  does not move `<ts>`, on two conditions. It leaves `render` and `resolve_base` alone, and
  its PR records identical `notice_replay` bytes from new and old code on one `HEAD`, back
  to back. Run on different days, `report` reads different windows, because the CLI does
  not expose `now`. "No freeze is needed" became the statement that those functions are not
  frozen, and why.

### Round integration-2 (integration review)

Findings raised: one medium. Fixed in G1's scope, and the owners of G2 and G3 applied
their parts in the same round. None refuted.

- **X2-18 (medium): G2 PR 1's entry condition restates G1's old soak bound, which G1's
  rules no longer allow it to meet.** Fixed. The finding repeats the G1-C2-04 request,
  which round c2 sent as a cross-slice note and which G2 and G3 had not applied.
    - Confirmed in `g2-retire-lock.md` and `g3-freshness-reviewer.md` as the finding quotes
      them. G2's entry condition said check 2's query "prints nothing", bounded by PR 5's
      merge or a later change to "either CI step". G1 has three CI steps and one moving
      bound `<ts>`, and check 2 names three kinds of artifact after it. G3's *Goal* said
      "G1 PR 3 names those two keys", where G1's *Steps*, item 4, adds them. G3's PR 2 kept
      a `resolve_base` fallback for a function that G1's *Steps*, item 3, adds with its
      tests.
    - Precision on the finding's "allows no skip". *Steps*, item 6, exempts one change to
      `notice.py` from moving `<ts>`: a mechanical update that follows a renamed import.
      That change is not a fix. Every fix moves `<ts>`, so the finding holds for fixes.
    - G2's "stricter" paragraph guarded a case that G1 now cannot produce. Checking it
      found one gap. A miss fixed outside `notice.py`, for example in `citations.py`, moved
      no `<ts>` under the old triggers. The replay then read the move as `listed`, and
      check 3 graded the lock commit's old run, which lacks the unit's notice, with no
      escape. *Steps*, item 6, therefore moves `<ts>` on a fix for a content-form `miss`,
      wherever it lives, as it already did for a crash fix. The *Goal* paragraph and the
      *Risks* entry *The soak restarts* list the same trigger. Check 3 samples only
      `content_lock_moves` with `committed_at` after `<ts>`, so a move whose old run lacks
      the unit's notice is never graded once its fix has merged.
    - G1's part of the cause was exit item 1's clause "each miss fixed in the notice
      code". G2 read it as a fix that skips the restart. The clause is gone: item 1 accepts
      an explained miss, and says that a fix removes its miss from the replay and moves
      `<ts>`.
    - *Goal and exit criterion* gains *What the G2 ADR records*, one list of every record G1
      assigns to that ADR. It points at check 1, *Design*'s reclassification rule, checks 2
      and 3 in *Verification*, *Steps*, item 6, and two *Open questions*. G2's ADR
      *Context* bullet cites G1's sections as the list, so it reaches this one.
    - The owners of G2 and G3 applied their halves in this round. G2's PR 1 entry
      condition cites G1's exit items 1 to 3 and *Steps*, item 6, and its "stricter"
      paragraph is gone. G3 says G1 PR 4 and dropped the `resolve_base` fallback. The open
      question on stale G2 and G3 text records both.
    - The one step change is the new `<ts>` trigger in *Steps*, item 6, with the matching
      *Risks* edit. It adds no command, test or PR. *Verification*, *Rollback*, *Tests and
      docs* and *Complexity delta* are unchanged.
