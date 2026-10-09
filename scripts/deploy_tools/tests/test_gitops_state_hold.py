"""`gitops_state.py clear-hold`: `hold_sha` and its `hold_plane` lines go together, or not at all.

Every rule is a pair, for the reason `test_gitops_state.py` gives: a clear that dropped
everything and one that dropped nothing read the same from the passing side alone. The hand
`rm` of `hold_sha` this replaces left the `hold_plane` lines behind (#3930).

Run: uv run pytest scripts/deploy_tools/tests/test_gitops_state_hold.py
"""

import fcntl
import json
import os
from pathlib import Path

import pytest

from deploy_tools import gitops_state
from gitops_hold import HOLD_CLEAR_CMD
from lib.repo_paths import REPO

SHA = "2d25ced3" + "0" * 32
OTHER = "f00dfeed" + "0" * 32


def _owed(cls: str, subject: str) -> str:
    return json.dumps(
        {"class": cls, "subject": subject, "origin": SHA, "at": 1000}, sort_keys=True
    )


SETUP_PLANE = _owed("hold_plane", "ansible/initial_setup.yml gitops_deploy")
DEPLOY_PLANE = _owed("hold_plane", "ansible/deploy.yml sonarr")
DEFERRED = _owed("k8s_deferred", "radarr")


def _held(state_dir: Path, sha: str = SHA) -> None:
    (state_dir / "hold_sha").write_text(sha + "\n")
    (state_dir / "owed.jsonl").write_text(
        f"{SETUP_PLANE}\n{DEFERRED}\n{DEPLOY_PLANE}\n"
    )


def test_clear_hold_drops_hold_sha_with_every_hold_plane_line(
    tmp_path, run, capsys, journal
):
    """CLEAN half: both planes go with the hold, and the other class's line stays."""
    _held(tmp_path)
    assert run(tmp_path, "clear-hold", SHA) == 0
    assert not (tmp_path / "hold_sha").exists()
    assert (tmp_path / "owed.jsonl").read_text().splitlines() == [DEFERRED]
    out = capsys.readouterr().out
    assert "ansible/initial_setup.yml gitops_deploy" in out
    assert "ansible/deploy.yml sonarr" in out
    assert journal == [
        (SHA, ["ansible/initial_setup.yml gitops_deploy", "ansible/deploy.yml sonarr"])
    ]


def test_clear_hold_refuses_a_sha_that_is_not_the_one_held(
    tmp_path, run, capsys, journal
):
    """FLAGGED half: a stale SHA leaves both markers whole and journals nothing."""
    _held(tmp_path, OTHER)
    before = (tmp_path / "owed.jsonl").read_text()
    assert run(tmp_path, "clear-hold", SHA) == 1
    assert (tmp_path / "hold_sha").read_text().strip() == OTHER
    assert (tmp_path / "owed.jsonl").read_text() == before
    assert f"hold is {OTHER}" in capsys.readouterr().err
    assert journal == []


def test_clear_hold_refuses_when_nothing_is_held(tmp_path, run, capsys):
    """No hold is a mismatch too, so a typo cannot read as a successful clear."""
    assert run(tmp_path, "clear-hold", SHA) == 1
    assert "hold is clear" in capsys.readouterr().err


def test_clear_hold_refuses_while_the_tree_lock_is_held(
    tmp_path, tree_lock, run, capsys
):
    """A tick holding the lock may be writing a new plane; the clear waits, then refuses."""
    _held(tmp_path)
    fd = os.open(tree_lock, os.O_RDONLY | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert run(tmp_path, "clear-hold", SHA) == 1
    finally:
        os.close(fd)
    assert (tmp_path / "hold_sha").read_text().strip() == SHA
    assert "is held" in capsys.readouterr().err


def test_the_printed_clear_hold_command_is_one_this_parser_runs(tmp_path, run):
    """`HOLD_CLEAR_CMD` is what `probe.py gitops-state` and the tick wrapper print.

    A rename of the verb here that the constant did not follow would hand every operator a
    command argparse rejects.
    """
    _held(tmp_path)
    script = Path(gitops_state.__file__).relative_to(REPO).as_posix()
    prefix, _, verb = HOLD_CLEAR_CMD.partition(f" {script} ")
    assert prefix == "uv run python", HOLD_CLEAR_CMD
    assert run(tmp_path, *verb.split(), SHA) == 0
    assert not (tmp_path / "hold_sha").exists()


def test_orphaned_drops_hold_plane_lines_left_with_no_hold_sha(
    tmp_path, run, capsys, journal
):
    """CLEAN half: what a hand `rm hold_sha` left goes, and the other class's line stays."""
    _held(tmp_path)
    (tmp_path / "hold_sha").unlink()
    assert run(tmp_path, "clear-hold", "--orphaned") == 0
    assert (tmp_path / "owed.jsonl").read_text().splitlines() == [DEFERRED]
    assert "orphaned" in capsys.readouterr().out
    assert journal == [
        ("-", ["ansible/initial_setup.yml gitops_deploy", "ansible/deploy.yml sonarr"])
    ]


def test_orphaned_refuses_while_a_hold_is_set(tmp_path, run, capsys, journal):
    """FLAGGED half: a live hold's planes are not orphans, so both markers stay whole."""
    _held(tmp_path)
    before = (tmp_path / "owed.jsonl").read_text()
    assert run(tmp_path, "clear-hold", "--orphaned") == 1
    assert (tmp_path / "hold_sha").read_text().strip() == SHA
    assert (tmp_path / "owed.jsonl").read_text() == before
    assert "not --orphaned" in capsys.readouterr().err
    assert journal == []


def test_clear_hold_takes_a_sha_or_orphaned_not_both(tmp_path, run):
    for argv in (("clear-hold",), ("clear-hold", SHA, "--orphaned")):
        with pytest.raises(SystemExit) as exc:
            run(tmp_path, *argv)
        assert exc.value.code == 2
