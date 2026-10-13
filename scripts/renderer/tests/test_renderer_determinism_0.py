"""R1's determinism check over the render targets at position 0, plus the check's red-proofs.

Positions 1 to 3 are the sibling `test_renderer_determinism_<k>.py` modules. One module per
position, because `--dist loadscope` keeps a module on one xdist worker, and only separate
modules spread the cost of rendering every tag twice. `_render_audit.py` holds the check and
says what it proves.

Run: uv run pytest scripts/renderer/tests/test_renderer_determinism_0.py
"""

from pathlib import Path

from _render_audit import (
    MODULE_PREFIX,
    PARTS,
    check_part,
    module_position,
    run_step,
    tree_differences,
)
from _render_audit_child import AUDIT_REFUSED
from lib.repo_paths import REPO


def test_its_positions_render_identically_and_read_only_the_repo(
    tmp_path, record_property
):
    check_part(__file__, tmp_path, record_property)


def test_one_module_checks_each_position():
    # A missing module would drop its tags from the check, and every other module stays green.
    here = Path(__file__).parent
    positions = sorted(
        module_position(p.name) for p in here.glob(f"{MODULE_PREFIX}*.py")
    )
    assert len(positions) >= 4, positions
    assert positions == list(range(PARTS))


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


def test_the_audit_hook_refuses_a_symlink_out_of_an_allowed_root(tmp_path):
    link = tmp_path / "hosts"
    link.symlink_to("/etc/hostname")
    refused = run_step({"allow": [str(tmp_path)], "step": "open", "path": str(link)})
    assert refused.returncode == AUDIT_REFUSED
    assert f"refused open of {link}" in refused.stderr


def test_the_audit_hook_refuses_every_socket_connect():
    # The hook ends the process before the connect is attempted, so nothing need listen.
    refused = run_step(
        {"allow": [str(REPO)], "step": "connect", "host": "127.0.0.1", "port": 9}
    )
    assert refused.returncode == AUDIT_REFUSED
    assert "refused socket.connect to ('127.0.0.1', 9)" in refused.stderr
