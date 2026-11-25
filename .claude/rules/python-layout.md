---
paths:
  - "scripts/**"
  - "tests/**"
  - "k8s/roles/**/tests/**"
  - ".claude/hooks/**"
---

# Python layout — where a module and its tests go

This file is what you need while placing a file, plus the rules every new check meets. The
long form, with the measurements and the incidents behind each rule, is
`docs/python-code-organization.md`.

## Import dotted from the installed root

`scripts/` is the import root (`docs/adr/0039-scripts-is-installed-in-place.md`). In a venv
the editable install's `.pth` supplies it, and outside one the launcher's `PYTHONPATH` does.
So every import of a module under `scripts/` is dotted from the root (`from lib.git import
git`, `from dev.fanout_lib import place`, `from deploy_tools.land_lib import policy`) and needs
no insert. That holds inside a module's own directory too: `from dev import label_worktree`,
never `import label_worktree`. One file imported under two names is two module objects, so a
test that patches one leaves the other untouched.

`pythonpath` in `pyproject.toml` is a pytest setting, so pytest resolving a bare import
proves nothing about the cron or hook that runs it. It lists `scripts/` and no subdirectory of
it, so a bare sibling import fails under pytest too. The `scripts-import-their-siblings-dotted`
row of `tests/repo/test_census_rows_layout.py` flags one.

A module that imports `host_lib`, `ci_verdict`, `builder_ci_grade`, `gh_quota` or a role module
`scripts/lib/repo_paths.py:ROLE_MODULES` lists by bare name imports `lib.host_modules` first
(`from lib import host_modules  # noqa: F401`). A module a host package copies (`shared:`)
ships under the same `lib/` path it has in the checkout and imports dotted, but keeps its own
insert of `hosts/lib/`, because the package does not ship `lib/host_modules.py`. The
`no-sys-path-change-under-scripts` row refuses every other `sys.path` change under `scripts/`,
test modules included; its `SYS_PATH_WRITERS` allow-list names each one that remains.

A test under `scripts/` imports the module it covers dotted (`from diagnostics import probe`).
A fixture beside it (`_land_fakes.py`) resolves by bare name, because pytest prepends the
test's own directory. A file a Job stages beside its siblings keeps the staged layout's bare
import, and the tests bind that name: `scripts/renderer/tests/conftest.py`
binds `changed_set` for `flux_soak.py`. Outside `scripts/`,
`scripts/tests/test_test_module_bootstraps_present.py` refuses a test module's insert that
`pythonpath` already supplies.

The subdirectories have **no `__init__.py`**: they are PEP 420 namespace packages, and adding
one changes how pytest names the test modules under them.

Three or more flat siblings sharing a first or last `_`-word, one of them an entrypoint, go in a
`<prefix>_lib/` package beside it, as `land_lib/` does. The `module-families-live-in-a-package`
row of `tests/repo/test_census_rows_layout.py` enforces it.

Verify a moved or new entry point by RUNNING it (`uv run python scripts/<dir>/<name>.py
--help`). The suite cannot see a missing bootstrap.

## Tests sit in a sibling `tests/`, never beside the code

`testpaths` in `pyproject.toml` names every suite. The shapes: repo-wide guards in
`tests/`, a role's own logic under `k8s/roles/<role>/tests/` (the code stays
in `files/`, which the role ships), guards over the host packages and their components in
`hosts/tests/`, the Bash classifier in `.claude/hooks/tests/`, and `scripts/<dir>/tests/`.
`tests/repo/test_testpaths_covers_every_test_file.py` enforces
it.

**A suite is grouped by subject.** A directory holds at most 40 test modules. A larger suite is
split into subject directories, named for the family its tests share
(`scripts/dev/tests/fanout/`, `findings/`), and then holds no test module beside them. A new test
goes in the group for its subject. Four rules keep a group collectable:

- A module keeps its basename when it moves, family prefix included.
- A helper that several groups import (`_fanout_fakes.py`) stays at the suite's root with a
  `conftest.py`, because pytest prepends a conftest's directory to `sys.path`. The suite's root
  is in `[tool.ty.environment] extra-paths`. A helper one group imports moves with it.
- A test module that imports another test module sits in the same group as it.
- A group takes no name that a `pythonpath` root holds, such as `lib`, `bridge` or `checks`.

The `test-modules-are-grouped-by-subject` row of `tests/repo/test_census_rows_layout.py` enforces
the cap and the grouping. Its `allow` names each suite that is still flat.

A role test imports its own role's `files/` modules by bare name: `k8s/conftest.py`
puts that one directory on `sys.path`. A module shared across roles goes on `pythonpath`. A
role that ships a `files/*.py` with logic gets a `tests/` directory. A top-level `files/*.py`
needs a basename no other role's `files/` or `pythonpath` root uses, because a full run shares
one session; the `no-two-import-roots-share-a-module-basename` row of
`tests/repo/test_census_rows_python.py` enforces it.

`tests/` is grouped by what a guard reads: `deploy/`, `k8s/`, `longhorn/`, `staging/`,
`services/` (one role each) and `repo/` (CI, docs and the suite's own guards). A guard over the
host packages and their components under `hosts/` goes in `hosts/tests/`. The shared modules
(`_helpers.py`, `_k8s_render.py`, `_role_census.py`) and `conftest.py` stay at the root of
`tests/`, which `pythonpath` puts in reach of `hosts/tests/` too. A new guard keeps a unique basename, since pytest names modules by basename alone.

**A one-selector, one-predicate check is a row, not a file.**
- A property of the rendered k8s manifests goes in `tests/k8s/test_rendered_properties.py`,
  or in one of the `_*_property_rows.py` files beside it once that file nears the line cap.
  Extra reject and accept cases go in `more_red` and `more_green`.
- A textual census of the tracked tree goes in a `Census` row of
  `tests/repo/test_census_rows_python.py`, `test_census_rows_text.py`,
  `test_census_rows_roles.py`, `test_census_rows_layout.py` or `test_census_rows_suite.py`.
  A rule over the test modules goes in `scripts/tests/test_census_rows_test_modules.py` or
  `test_census_rows_test_renders.py`.
- A property of a GitHub Actions workflow or action goes in a row of
  `tests/repo/test_census_rows_workflows.py`, from the `_*_workflow_rows.py` module for its
  workflow. `tests/repo/_workflow_rows.py:judge` runs a rule over the parsed file, and
  `tests/repo/_workflow_rows.py:mutants` builds the red half as the real file with one property
  broken.
- An ordering or flag rule over an operator-only runbook's section goes in a `RunbookRule`
  row of `tests/repo/test_runbook_rules.py`: the doc, the heading, the unit, a selector, and
  the strings it requires, the pattern it forbids or the step that must come first.
- `tests/_row_table.py` is the harness the tables share. A property over a second render is
  still a row: it passes that render as the row's `source` (`_prepull_property_rows.py`).
  One needing a long exemption list keeps its own file.
- The `a-single-predicate-check-is-a-row` row of `tests/repo/test_census_rows_suite.py`
  refuses a module in any directory under `tests/` that reads tracked text, by a walk or by
  one file's path, and runs no code: no subprocess other than `git ls-files`, no render and
  no scratch tree, whatever its number of test functions. A call into the `renderer`
  package or `tests/k8s/_property_table.py` counts as a render. A file that must stay
  one goes in that row's `allow`, with a reason naming the join or the render that keeps it.

## A new check ships with a proof it can go red

Any validator, guard, health check or probe lands with a paired test: one input it must
accept, one it must reject. Without the rejecting half there is no evidence it can fail.
`scripts/validate/tests/test_render_compose.py` is the worked example: name the
pair `..._is_clean` / `..._is_flagged`.

## A check that finds its subject by pattern names a member it must find

A guard that globs for its files returns an EMPTY set once they are renamed or moved, and an
`all(...)` over nothing passes. Assert non-vacuity against a frozenset of names the census must
contain, so the failure names the member that went missing. `KNOWN_CONSUMERS` in
`scripts/diagnostics/tests/probe_cli/test_probe_boundaries.py` is the worked example.

## A check that reaches over a network measures its transport first

Time each endpoint against the live source, more than once. A slow source is a design input:
make the slow fetch conditional on a cheap signal having fired, and make its failure downgrade
the diagnosis rather than the verdict.

## A check retires when its subject does

Delete a check in the PR that obsoletes it, when either holds:

- **Its subject is gone.** A check over a missing subject still passes, so green is no
  evidence it is worth keeping.
- **A census or schema check covers it.** `tests/_k8s_render.py`'s role censuses and
  `scripts/validate/render_lib/k8s.py`'s OpenAPI validation usually swallow a single-shape
  guard. Name the covering check in the PR body.

`scripts/dev/repo_guards.py --drop-candidates --cited` lists each check file under `tests/`
that cites an issue or a date and has not failed on master within its window. The list proposes candidates for these two
tests. It is not a third condition: a check that never fails can still be load-bearing.

A check that pins a skill's restatement of a tool's enum retires in the PR that makes the tool
print its own contract. Drop the check's `ENFORCED` citation in the same PR:
`tests/repo/test_documented_paths_exist.py::test_every_cited_test_exists` fails on a
node id that no longer resolves.
