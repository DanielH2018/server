"""The tmpfs /tmp on both Claude hosts ages Claude scratchpads and pytest trees out within a day.

Run: uv run pytest hosts/tests/claude_code/test_claude_tmp_age.py
"""

import shutil
from pathlib import Path

import pytest
from lib.proc_testing import run

from renderer.renderer_lib import host as host_mode

CONF = "/usr/lib/tmpfiles.d/homelab-claude-tmp-age.conf"
SCRATCH = "tmp/user/1000/claude-1000/-home-ubuntu-server/25387b63/scratchpad"
AGENT_SCRATCH = "tmp/user/900/claude-900/-var-lib-claude-server/0f1e2d3c/scratchpad"
PYTEST_TREES = ("tmp/user/1000/pytest-of-ubuntu", "tmp/pytest-of-claude")
KEPT = (
    "tmp/user/1000/uv-1.lock",
    "tmp/user/1000/claude-1000/-home-ubuntu-server/25387b63/tasks/t.output",
)


def packaged_lines(host: str) -> list[list[str]]:
    package = host_mode.render_host(host)
    assert package.activations[CONF] == "tmpfiles"
    text = package.files[CONF][0].decode()
    return [
        line.split() for line in text.splitlines() if line and not line.startswith("#")
    ]


def clean(root: Path, lines: list[list[str]], conf: Path) -> None:
    tmpfiles = shutil.which("systemd-tmpfiles")
    if tmpfiles is None:
        pytest.skip("systemd-tmpfiles is not installed")
    conf.write_text("".join(" ".join(fields) + "\n" for fields in lines))
    run([tmpfiles, f"--root={root}", "--clean", str(conf)], check=True)


def plant(root: Path) -> list[Path]:
    planted = []
    for tree in (
        SCRATCH,
        AGENT_SCRATCH,
        *(f"{t}/pytest-3/test_x0" for t in PYTEST_TREES),
    ):
        (root / tree).mkdir(parents=True)
        planted.append(root / tree / "deb.log")
        planted[-1].write_text("x")
    for kept in KEPT:
        (root / kept).parent.mkdir(parents=True, exist_ok=True)
        (root / kept).write_text("x")
    return planted


@pytest.mark.parametrize("host", ["daniel-box", "daniel-server"])
def test_each_claude_host_ages_files_in_scratchpads_and_pytest_trees_within_a_day(host):
    ages = {fields[5] for fields in packaged_lines(host)}
    assert ages == {"1d"}


def test_the_globs_empty_scratchpads_and_pytest_trees_and_keep_their_roots(tmp_path):
    root = tmp_path / "root"
    planted = plant(root)
    expired = [[*fields[:5], "0"] for fields in packaged_lines("daniel-box")]
    clean(root, expired, tmp_path / "expired.conf")
    assert [p for p in planted if p.exists()] == []
    assert (root / SCRATCH).is_dir() and (root / AGENT_SCRATCH).is_dir()
    assert all(
        (root / tree).is_dir() and not any((root / tree).iterdir())
        for tree in PYTEST_TREES
    )
    assert all((root / kept).exists() for kept in KEPT)


def test_a_file_younger_than_the_age_survives_the_cleanup(tmp_path):
    root = tmp_path / "root"
    planted = plant(root)
    clean(root, packaged_lines("daniel-box"), tmp_path / "packaged.conf")
    assert all(p.exists() for p in planted)
