"""`cluster_vars.py check` on the real tree: the gate that D3.2c adds to CI's `pytest` job.

The cluster files are produced by the operator's `cluster_vars.py write`. This test runs the
full `check`, render included, with the default base (the merge base of `HEAD` with
`origin/master`), so a PR that changes a cluster-referenced key in `secrets.yml` without
regenerating the cluster files fails here. It decrypts nothing.

Run: uv run pytest scripts/secrets_mgmt/tests/test_cluster_vars_tree.py
"""

from render_share import sharing_placeholder_renders
from renderer.renderer_lib import placeholders
from secrets_mgmt.cluster_vars import main


def test_the_committed_cluster_files_pass_check(capsys):
    with sharing_placeholder_renders(placeholders):
        code = main(["check"])
    err = capsys.readouterr().err
    assert code == 0, err
