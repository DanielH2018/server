"""The operator's half of the `manual_plane` marker: clearing one role's line.

Every rule is a pair. A command that cleared everything and one that cleared nothing read the
same from the passing side alone, and this one is destructive in the direction that matters:
clearing a role nobody applied silences the page that says so.

Run: uv run pytest scripts/deploy_tools/tests/test_gitops_state.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deploy_tools import gitops_state

K3S = "abc123def4567890 ansible/k3s-bringup.yml k3s 1000.0"
COMMON = "def456abc7890123 none common 2000.0"


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    (tmp_path / "manual_plane").write_text(f"{K3S}\n{COMMON}\n")
    return tmp_path / "manual_plane"


def _run(state_dir: Path, *args: str) -> int:
    return gitops_state.main(["--state-dir", str(state_dir), *args])


def test_clearing_one_role_leaves_the_other(marker, capsys):
    assert _run(marker.parent, "clear-manual-plane", "k3s") == 0
    assert marker.read_text().splitlines() == [COMMON]
    assert "k3s" in capsys.readouterr().out


def test_clearing_a_role_that_is_not_pending_exits_zero_and_says_so(marker, capsys):
    """An operator clearing twice, or naming a role nobody recorded, has nothing to fix."""
    assert _run(marker.parent, "clear-manual-plane", "renovate_agent") == 0
    assert marker.read_text().splitlines() == [K3S, COMMON]
    assert "not pending" in capsys.readouterr().out


def test_clearing_the_last_role_removes_the_marker(tmp_path, capsys):
    (tmp_path / "manual_plane").write_text(f"{K3S}\n")
    assert _run(tmp_path, "clear-manual-plane", "k3s") == 0
    assert not (tmp_path / "manual_plane").exists()


def test_an_absent_marker_is_not_an_error(tmp_path, capsys):
    assert _run(tmp_path, "clear-manual-plane", "k3s") == 0
    assert "not pending" in capsys.readouterr().out


def test_a_state_directory_this_user_cannot_write_says_who_owns_it(marker, capsys):
    """The state directory is 0750 and owned by the deployer's user, so the wrong shell gets
    a PermissionError.

    A traceback there reads as a broken script rather than as "run this as the deploy user".
    """
    marker.parent.chmod(0o500)
    try:
        assert _run(marker.parent, "clear-manual-plane", "k3s") == 1
        err = capsys.readouterr().err
        assert "cannot write" in err
    finally:
        marker.parent.chmod(0o700)


def test_the_role_is_resolved_through_the_deployers_own_tag_map(marker):
    """The marker's key is `setup_role_tag(role)`, so this must not match on the bare name.

    Today every role the marker can hold is tagged by its own name, which is what makes the
    two indistinguishable — and exactly why the resolution belongs here rather than in a
    reader's head.
    """
    assert gitops_state.marker_key("k3s") == "k3s"
