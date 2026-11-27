"""What `host_targets` says when a merge-commit render fails on a module the venv lacks.

Run: uv run pytest scripts/deploy_tools/tests/land_deploy/test_land_host_render_failed.py
"""

from pathlib import Path

import pytest

from deploy_tools.land_lib import host_wait
from deploy_tools.land_lib.host_wait import HostTarget, RenderFailed
from test_land_host_wait import MERGE, PARENT, _git, _reads, _snapshots

ADDS_PYDANTIC = (
    "  host targets: the render of tools failed because the primary .venv lacks "
    "'pydantic', a dependency this merge adds to uv.lock; the venv gains it when "
    "homelab-checkout-sync next runs, so this landing waits on every host once\n"
)
GENERIC = "  host targets: the render of tools failed; waiting on every host\n"


@pytest.mark.parametrize(
    ("parent_lock", "said"),
    [("version = 1\n", ADDS_PYDANTIC), ('[[package]]\nname = "pydantic"\n', GENERIC)],
    ids=["lock-changed", "lock-unchanged"],
)
def test_a_missing_module_is_named_as_the_merges_new_dependency_only_when_uv_lock_changed(
    tmp_path, capsys, parent_lock, said
):
    snapshot, roots = _snapshots(
        tmp_path, {MERGE: ["daniel-pi"], PARENT: ["daniel-pi"]}, packages=["tools"]
    )
    (roots[MERGE] / "uv.lock").write_text('[[package]]\nname = "pydantic"\n')
    (roots[PARENT] / "uv.lock").write_text(parent_lock)
    trees = {("a", "tools"): RenderFailed("pydantic"), ("b", "tools"): {}}
    reads = _reads(snapshot, trees, git=_git(stdout={"ls-tree": "hosts/daniel-pi\n"}))
    assert host_wait.host_targets(Path("/p"), MERGE, reads) == [HostTarget("daniel-pi")]
    assert capsys.readouterr().out == said
