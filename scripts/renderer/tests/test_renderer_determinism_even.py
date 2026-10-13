"""R1's determinism check over the render targets at even positions, plus its red-proofs.

The odd positions are `test_renderer_determinism_odd.py`. Two modules, because `--dist
loadscope` keeps a module on one xdist worker, and one module rendering every tag twice would
be the heaviest in the suite. `_render_audit.py` holds the check and says what it proves.

Run: uv run pytest scripts/renderer/tests/test_renderer_determinism_even.py
"""

from _render_audit import check_half, run_step, tree_differences
from _render_audit_child import AUDIT_REFUSED
from lib.repo_paths import REPO


def test_even_positions_render_identically_and_read_only_the_repo(tmp_path):
    check_half(0, tmp_path, "even positions")


def test_a_tree_differing_in_one_file_is_flagged(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    for root in (first, second):
        (root / "cluster/littlelink").mkdir(parents=True)
        (root / "cluster/littlelink/service.yaml").write_text("port: 80\n")
    assert tree_differences(first, second) == []

    (second / "cluster/littlelink/service.yaml").write_text("port: 81\n")
    (second / "cluster/littlelink/extra.yaml").write_text("")
    assert tree_differences(first, second) == [
        "differs: cluster/littlelink/service.yaml",
        "only in second: cluster/littlelink/extra.yaml",
    ]


def test_the_audit_hook_refuses_an_open_outside_the_repo():
    refused = run_step({"allow": [str(REPO)], "step": "open", "path": "/etc/hostname"})
    assert refused.returncode == AUDIT_REFUSED
    assert "refused open of /etc/hostname" in refused.stderr

    allowed = run_step(
        {"allow": [str(REPO)], "step": "open", "path": str(REPO / "pyproject.toml")}
    )
    assert allowed.returncode == 0, allowed.stderr


def test_the_audit_hook_refuses_every_socket_connect():
    # The hook ends the process before the connect is attempted, so nothing need listen.
    refused = run_step(
        {"allow": [str(REPO)], "step": "connect", "host": "127.0.0.1", "port": 9}
    )
    assert refused.returncode == AUDIT_REFUSED
    assert "refused socket.connect to ('127.0.0.1', 9)" in refused.stderr
