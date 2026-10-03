---
paths:
  - "scripts/**"
  - "ansible/filter_plugins/**"
  - "ansible/tests/**"
  - "ansible/roles/**/tests/**"
  - ".claude/hooks/**"
---

# Python layout — where a module and its tests go

The long form, with the measurements and the options not taken, is
`docs/python-code-organization.md`. This file is the part you need while placing a file, plus
the three rules every new check meets and the one that retires it (the last four sections).
They stay here because this file loads where a new check lands (#2811).

## Cross-directory imports need a `sys.path` bootstrap

A script imports across `scripts/` subdirectories with `from <dir> import <mod>`, and only
after a `sys.path` bootstrap. A directly-invoked script gets its OWN directory on `sys.path`
and nothing else — `pythonpath` in `pyproject.toml` is a pytest setting, so a cross-directory
import that pytest resolves still raises `ModuleNotFoundError` under the cron or prek hook that
actually runs it. Every module reaching outside its own directory therefore carries its own
aliased insert (`_sys.path.insert(0, _Path(__file__).resolve().parents[1])`); copy that from a
sibling. It goes on the module that needs it, never on a shared one — a single bootstrap in an
imported module only works for whoever imports it first.

A module under a `tests/` directory carries none of this — a `test_*.py`, a `conftest.py` or a
`_*.py` fixture module a test imports: pytest is its only invoker, and `pythonpath` lists
`scripts/` and every subdirectory but `validate` and `diagnostics`. A test under those two
inserts its own package directory; any other insert `pythonpath` already supplies is dead
weight, and `scripts/tests/test_test_module_bootstraps_present.py` refuses it.

The subdirectories have **no `__init__.py`** on purpose: they resolve as PEP 420 namespace
packages, and adding one would change how pytest names the test modules under them.

Verify a moved or new entry point by RUNNING it (`uv run python scripts/<dir>/<name>.py
--help`), not by running the suite — the suite is exactly the thing that cannot see this.

## Tests sit in a sibling `tests/`, never beside the code

`testpaths` in `pyproject.toml` names every suite, with a comment saying what each covers. Four
shapes recur: repo-wide guards in `ansible/tests/` (deploy ordering, the auto-deploy gates, the
documented-path and macro checks), a role's own cluster-side logic under
`ansible/roles/<plane>/<role>/tests/` (the code it covers stays in `files/`, which is what the
role ships), the Bash classifier in `.claude/hooks/tests/`, and `scripts/<dir>/tests/`.

A `tests/` sibling keeps a test out of every `files/` ship list and lets the deployer's
test-only path rule stay a directory check (ENFORCED by
`ansible/tests/repo/test_testpaths_covers_every_test_file.py`). A test in a `tests/` directory
reaches its module through a `sys.path` bootstrap pointing at the sibling `files/`, or through
`pythonpath` where the module is shared across roles. Every such bootstrap lands in the one
session a full run shares, so a top-level `files/*.py` needs a basename no other role's
`files/` or `pythonpath` root uses. Two roles' `app.py` failed six tests only in a full run
(#2608). `ansible/tests/repo/test_pythonpath_module_basenames.py` enforces it. A role that ships a `files/*.py` with
logic gets a `tests/` directory, which the `ansible/roles/*/*/tests` glob in `testpaths` collects.

`ansible/tests/` is grouped by what a guard reads: `deploy/` (the deploy play, gitops_deploy
and the rollout gates), `k8s/` (manifest render and workload hygiene across roles),
`longhorn/` (backup, snapshot, revert), `setup/` (the host plane: k3s install, crons, DNS,
UPS, the Pi), `staging/`, `services/` (one role each) and `repo/` (CI, docs and the suite's
own guards). The modules guards share — `_helpers.py`, `_k8s_render.py`, `_check_mode.py`,
`_role_census.py` — and `conftest.py` stay at the root, reachable from every subdirectory
because `pyproject.toml` puts `ansible/tests` on `pythonpath`. A new guard goes in the directory
whose name answers "what does it read", and keeps a unique basename — there are no
`__init__.py` files, so pytest names modules by basename alone.

A property of the rendered k8s manifests that is one selector and one predicate goes in as a
row of `ansible/tests/k8s/test_rendered_properties.py`, not as a new file. The row carries its
floor, its named members and its red/green fixtures, and a row whose selector stops matching
fails with `subject gone: delete this row`. A property needing a second render context, a
cross-document join or a long exemption list keeps its own file. The test file runs every
row; once it nears the 500-line test-module cap, a row goes in `_config_property_rows.py` or
`_workload_property_rows.py` beside it.

## No tests under `ansible/filter_plugins/`

Ansible's plugin loader imports every `.py` there at deploy time and would choke on the
`pytest` import. `test_every_suite_file_sits_in_a_tests_directory` (in
`ansible/tests/repo/test_testpaths_covers_every_test_file.py`) refuses a test file beside the
plugin. A `filter_plugins/tests/` subdirectory would pass it and still load, because
`PluginLoader._get_paths_with_context` globs two levels of subdirectories.

## A new check ships with a proof it can go red

Any validator, guard, health check or probe lands with a paired test: one input it must accept,
and one input it must reject. A check is only ever observed passing, so without the rejecting
half there is no evidence it can fail. `volume-claim`'s short-circuit shipped behind 16 passing
tests and a mutation test, then fired for 0 of 25 claims across two full deploys. `image-smoke`'s
bare-boot rule never caught a real image problem across 11 failures. Both read green throughout.
`scripts/validate/tests/test_validate_compose_templates.py` is the worked example: every rule
there is a `..._is_clean` / `..._is_flagged` pair, so a rule that silently stopped matching fails
its own test. Name the pair that way.

## A check that finds its subject by pattern names a member it must find

A guard that globs for the files it checks (`validate_*.py`, a `gen_reference_` prefix, a
one-level `DIR.glob("*.py")`) returns an EMPTY set the moment those files are renamed or move one
directory down, and an `all(...)` over nothing passes. The red-proof pair cannot see this: both
halves still fire on the inputs the test hands them. Assert non-vacuity against something
concrete — `assert len(found) >= <n>`, or better a frozenset of names the census must contain,
so the failure names the member that went missing. `KNOWN_CONSUMERS` in
`scripts/diagnostics/tests/test_probe_boundaries.py` is the worked example. Nine guards broke this
way in six consecutive PRs (#838, #846, #852, two in #858, four in the monitor-bridge package
move), and the non-vacuity assertion alone caught every one.

## A check that reaches over a network measures its transport first

The paired test proves the *verdict* can go red. It says nothing about whether the fetch that
feeds the verdict returns in time, and a check whose source is slow fails open on every slow
cycle behind a green monitor. Time each endpoint against the live source, more than once. The
Pi-detached arm (PR #482) polled glances' `/api/4/containers`, which took 4.43s on an idle Pi and
then timed out at the 10s `HTTP_TIMEOUT` on the next call, where the sibling endpoints answer in
0.03-0.06s. PR #484 reshaped it so the cheap signal decides and the expensive one only explains.
**A slow source is a design input:** make it conditional on the cheap signal having already
fired, and make its failure downgrade the diagnosis rather than the verdict.

## A check retires when its subject does

The three rules above price a new check. Nothing priced deleting one, so checks only
accumulated: in the 90 days to 2026-09-28 the repo added 889 test files and deleted 62, a ratio
of 14 to 1, while the service count held near 50. Delete a check in the PR that obsoletes it,
on either of two conditions.

- **Its subject is gone.** The code path, rule or failure shape it watches no longer exists. A
  check over a missing subject still passes — the census comes back empty and the `all(...)`
  over nothing is true — so a green run is not evidence it is still worth keeping.
- **A census or schema check already covers it.** `ansible/tests/_k8s_render.py`'s
  role censuses and `scripts/validate/k8s_manifests.py`'s strict OpenAPI validation are the two
  that usually swallow a single-shape guard. Name the covering check in the PR body and say
  which inputs it accepts that the deleted one rejected.

Drop the check's `ENFORCED` citation from the prose in the same PR.
`ansible/tests/repo/test_documented_paths_exist.py::test_every_cited_test_exists` fails on a
node id that no longer resolves, so a deletion that leaves the citation behind arrives as a
docs failure rather than as the deliberate retirement it was.

A check that pins a skill's restatement of a tool's enum is a third case. It retires in the
same PR that makes the tool print its own contract, together with the prose it pinned. #3023
retired the two tests that pinned the deploy and land skills' copies this way, once `deploy.sh`
and `land.sh` printed their exit codes and verdicts themselves.
