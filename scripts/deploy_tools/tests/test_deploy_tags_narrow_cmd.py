"""The `deploy_tags.py narrow` command wrapper: what reaches stdout, stderr and the exit code.

Split out of `test_deploy_tags_narrow.py` when that module crossed its 500-line cap. The
rules themselves are tested there; this covers only `narrow_cmd`'s contract with
`deploy_narrow.narrow_deploy_plane`, which reads stdout straight: the tag list and nothing
else on exit 0, an empty stdout for "reaches no rendered output", and `DEPLOY_BROAD` with
the reason on stderr for every refusal. The checkout is `_narrow_fixtures.build_tree`.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_tags_narrow_cmd.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import narrow_broad
from deploy_tools.exit_codes import DEPLOY_BROAD, DEPLOY_OK

from _narrow_fixtures import DECLARED, GROUP_VARS, Tree, _refs, _repo_git, build_tree


@pytest.fixture
def tree(tmp_path) -> Tree:
    return build_tree(tmp_path)


def test_the_command_prints_the_tags_and_exits_zero(tree: Tree, capsys):
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("10.0.0.0/24", "10.3.0.0/24"),
    )
    old, new = _refs(tree)
    rc = narrow_broad.narrow_cmd(old, new, cwd=tree.root, declared=DECLARED, callers={})
    assert rc == DEPLOY_OK
    out = capsys.readouterr()
    assert out.out.strip() == "radarr,sonarr"
    assert "lan_subnet" in out.err


def test_the_command_prints_nothing_and_exits_zero_when_it_narrows_to_nothing(
    tree: Tree, capsys
):
    tree.write(
        "ansible/inventory/group_vars/all.yml",
        GROUP_VARS.replace("nobody-reads-this", "still-nobody"),
    )
    old, new = _refs(tree)
    rc = narrow_broad.narrow_cmd(old, new, cwd=tree.root, declared=DECLARED, callers={})
    assert rc == DEPLOY_OK
    assert capsys.readouterr().out == ""


def test_the_command_exits_three_when_it_cannot_narrow(tree: Tree, capsys):
    tree.write("ansible/inventory/hosts.ini", "[all]\ndaniel-box\ndaniel-pi\n")
    old, new = _refs(tree)
    rc = narrow_broad.narrow_cmd(old, new, cwd=tree.root, declared=DECLARED, callers={})
    assert rc == DEPLOY_BROAD
    assert "hosts.ini" in capsys.readouterr().err


def test_the_command_exits_three_when_a_ref_cannot_be_read(tree: Tree, capsys):
    """A ref git cannot resolve is a refusal, not a traceback the deployer logs as a crash.

    `declared` is left unset on purpose: that is what sends `service_tags_at` at the ref.
    """
    old = _repo_git(tree.root, "rev-parse", "HEAD")
    rc = narrow_broad.narrow_cmd(old, "0" * 40, cwd=tree.root, callers={})
    assert rc == DEPLOY_BROAD
    assert "could not read the range" in capsys.readouterr().err
