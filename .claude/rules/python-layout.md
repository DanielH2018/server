---
paths:
  - "scripts/**"
  - "ansible/filter_plugins/**"
  - "ansible/tests/**"
  - "ansible/roles/**/tests/**"
  - ".claude/hooks/**"
---

# Python layout — where a module and its tests go

This file is what you need while placing a file, plus the rules every new check meets. The
long form, with the measurements and the incidents behind each rule, is
`docs/python-code-organization.md`.

## Cross-directory imports need a `sys.path` bootstrap

A directly-invoked script gets its OWN directory on `sys.path` and nothing else.
`pythonpath` in `pyproject.toml` is a pytest setting, so a cross-directory import that pytest
resolves still raises `ModuleNotFoundError` under the cron or prek hook that runs it. Every
module reaching outside its own directory carries its own aliased insert
(`_sys.path.insert(0, _Path(__file__).resolve().parents[1])`); copy it from a sibling. It goes
on the module that needs it, never on a shared one.

A module under a `tests/` directory carries none of this: pytest is its only invoker, and
`pythonpath` lists `scripts/` and every subdirectory but `validate` and `diagnostics`. A test
under those two inserts its own package directory. Any other insert is dead weight, and
`scripts/tests/test_test_module_bootstraps_present.py` refuses it.

The subdirectories have **no `__init__.py`**: they are PEP 420 namespace packages, and adding
one changes how pytest names the test modules under them.

Three or more flat siblings sharing a first or last `_`-word, one of them an entrypoint, go in a
`<prefix>_lib/` package beside it, as `land_lib/` does. The `module-families-live-in-a-package`
row of `ansible/tests/repo/test_census_rows_layout.py` enforces it.

Verify a moved or new entry point by RUNNING it (`uv run python scripts/<dir>/<name>.py
--help`). The suite cannot see a missing bootstrap.

## Tests sit in a sibling `tests/`, never beside the code

`testpaths` in `pyproject.toml` names every suite. The shapes: repo-wide guards in
`ansible/tests/`, a role's own logic under `ansible/roles/<plane>/<role>/tests/` (the code stays
in `files/`, which the role ships), guards over the host packages and their components in
`hosts/tests/`, the Bash classifier in `.claude/hooks/tests/`, and `scripts/<dir>/tests/`.
`ansible/tests/repo/test_testpaths_covers_every_test_file.py` enforces
it.

A role test imports its own role's `files/` modules by bare name: `ansible/roles/conftest.py`
puts that one directory on `sys.path`. A module shared across roles goes on `pythonpath`. A
role that ships a `files/*.py` with logic gets a `tests/` directory. A top-level `files/*.py`
needs a basename no other role's `files/` or `pythonpath` root uses, because a full run shares
one session; the `no-two-import-roots-share-a-module-basename` row of
`ansible/tests/repo/test_census_rows_python.py` enforces it.

`ansible/tests/` is grouped by what a guard reads: `deploy/`, `k8s/`, `longhorn/`, `staging/`,
`services/` (one role each) and `repo/` (CI, docs and the suite's own guards). A guard over the
host packages and their components under `hosts/` goes in `hosts/tests/`. The shared modules
(`_helpers.py`, `_k8s_render.py`, `_role_census.py`) and `conftest.py` stay at the root of
`ansible/tests/`, which `pythonpath` puts in reach of `hosts/tests/` too. A new guard keeps a unique basename, since pytest names modules by basename alone.

**A one-selector, one-predicate check is a row, not a file.**
- A property of the rendered k8s manifests goes in `ansible/tests/k8s/test_rendered_properties.py`,
  or in one of the `_*_property_rows.py` files beside it once that file nears the line cap.
  Extra reject and accept cases go in `more_red` and `more_green`.
- A textual census of the tracked tree goes in a `Census` row of
  `ansible/tests/repo/test_census_rows_python.py`, `test_census_rows_text.py`,
  `test_census_rows_roles.py`, `test_census_rows_layout.py` or `test_census_rows_suite.py`.
  A rule over the test modules goes in `scripts/tests/test_census_rows_test_modules.py` or
  `test_census_rows_test_renders.py`.
- `ansible/tests/_row_table.py` is the harness the tables share. A property needing a second
  render context, a cross-document join or a long exemption list keeps its own file.

## A filter plugin's tests go in `ansible/tests/`

`ansible/filter_plugins/` is a `pythonpath` root that the renderer and the render guards import
by bare module name (`scripts/lib/ansible_jinja_env.py`). Its tests sit in the `ansible/tests/`
subdirectory for what the filter derives, as `deploy/test_toposort.py` does for `toposort.py`.
`testpaths` does not reach `ansible/filter_plugins/`, so `test_repo_has_no_orphaned_test_files`
refuses a test placed there.

## A new check ships with a proof it can go red

Any validator, guard, health check or probe lands with a paired test: one input it must
accept, one it must reject. Without the rejecting half there is no evidence it can fail.
`scripts/validate/tests/test_validate_compose_templates.py` is the worked example: name the
pair `..._is_clean` / `..._is_flagged`.

## A check that finds its subject by pattern names a member it must find

A guard that globs for its files returns an EMPTY set once they are renamed or moved, and an
`all(...)` over nothing passes. Assert non-vacuity against a frozenset of names the census must
contain, so the failure names the member that went missing. `KNOWN_CONSUMERS` in
`scripts/diagnostics/tests/test_probe_boundaries.py` is the worked example.

## A check that reaches over a network measures its transport first

Time each endpoint against the live source, more than once. A slow source is a design input:
make the slow fetch conditional on a cheap signal having fired, and make its failure downgrade
the diagnosis rather than the verdict.

## A check retires when its subject does

Delete a check in the PR that obsoletes it, when either holds:

- **Its subject is gone.** A check over a missing subject still passes, so green is no
  evidence it is worth keeping.
- **A census or schema check covers it.** `ansible/tests/_k8s_render.py`'s role censuses and
  `scripts/validate/k8s_manifests.py`'s OpenAPI validation usually swallow a single-shape
  guard. Name the covering check in the PR body.

A check that pins a skill's restatement of a tool's enum retires in the PR that makes the tool
print its own contract. Drop the check's `ENFORCED` citation in the same PR:
`ansible/tests/repo/test_documented_paths_exist.py::test_every_cited_test_exists` fails on a
node id that no longer resolves.
