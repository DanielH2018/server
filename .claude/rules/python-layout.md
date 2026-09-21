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
`docs/python-code-organization.md`. This file is the part you need while placing a file.

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
`pythonpath` where the module is shared across roles. A role that ships a `files/*.py` with
logic adds its `tests/` directory to `testpaths`.

`ansible/tests/` is grouped by what a guard reads: `deploy/` (the deploy play, gitops_deploy
and the rollout gates), `k8s/` (manifest render and workload hygiene across roles),
`longhorn/` (backup, snapshot, revert), `setup/` (the host plane: k3s install, crons, DNS,
UPS, the Pi), `staging/`, `services/` (one role each) and `repo/` (CI, docs and the suite's
own guards). `_helpers.py`, `_k8s_render.py` and `conftest.py` stay at the root, reachable
from every subdirectory because `pyproject.toml` puts `ansible/tests` on `pythonpath`. A new
guard goes in the directory whose name answers "what does it read", and keeps a unique
basename — there are no `__init__.py` files, so pytest names modules by basename alone.

## No tests under `ansible/filter_plugins/`

Ansible's plugin loader imports every `.py` there at deploy time and would choke on the
`pytest` import. `test_every_suite_file_sits_in_a_tests_directory` (in
`ansible/tests/repo/test_testpaths_covers_every_test_file.py`) refuses a test file beside the
plugin. A `filter_plugins/tests/` subdirectory would pass it and still load, because
`PluginLoader._get_paths_with_context` globs two levels of subdirectories.
