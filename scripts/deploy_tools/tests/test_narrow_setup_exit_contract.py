"""`narrow_setup.py`'s exit codes are named in `lib/exit_codes.py`, and 3 keeps one meaning (#4453).

`narrow_cmd` exits `DEPLOY_BROAD` (3) for "cannot narrow". `narrow_setup.py` exited 3 for the
opposite answer, "this role needs nothing", so a reader of one contract misread the other. Both
cases drive `narrow_setup.main` with the argv the deployer builds, so the deployer's copy of the
code is checked against what the script actually returns.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_setup_exit_contract.py
"""

import pytest

from deployer_lib import deploy_narrow
import narrow_setup
from lib.exit_codes import DEPLOY_BROAD, contract

from _narrow_fixtures import _refs
from _setup_role_fixtures import PLAYBOOK, ROLE, Tree, build

NARROW_SETUP = "scripts/deploy_tools/narrow_setup.py"


@pytest.fixture
def tree(tmp_path) -> Tree:
    return build(tmp_path)


def _main(tree, playbook):
    old, new = _refs(tree)
    argv = deploy_narrow.narrow_setup_argv("demo", "demo", playbook, old, new)
    return narrow_setup.main([*argv[5:], "--repo", str(tree.root)])


def _declared():
    codes = contract(NARROW_SETUP)
    assert codes, f"{NARROW_SETUP} declares no exit contract in lib/exit_codes.py"
    return {code.value for code in codes}


def test_a_range_needing_nothing_does_not_exit_with_the_broad_code(tree):
    tree.write(f"{ROLE}/CLAUDE.md", "# Demo\n")
    rc = _main(tree, PLAYBOOK)
    assert rc != DEPLOY_BROAD, "3 means 'cannot narrow' to narrow_cmd"
    assert rc in _declared()
    assert rc == deploy_narrow.NARROW_SETUP_NOTHING_EXIT


def test_a_refusal_exits_with_a_code_the_contract_declares(tree):
    tree.write(f"{ROLE}/templates/alpha.conf.j2", "a\n")
    rc = _main(tree, "ansible/absent.yml")
    assert rc != 0
    assert rc in _declared()
