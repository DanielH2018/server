"""`HOST_INPUTS` covers every tracked file a real host render opens (H3.9a item 1).

One render of daniel-pi's package runs in `_host_inputs_child.py`, under an audit hook that
records every `open`. A tracked file it opened that falls under no member means a change to
that file could alter a host package while the landing reads the PR as reaching no host.
Untracked files are left out: a PR changes only tracked ones, and the render also opens
`ansible.log` and the worktree's `.git` file. The child runs with no bytecode cache, so each
import opens its tracked `.py` rather than an untracked `.pyc`.

Run: uv run pytest scripts/renderer/tests/test_renderer_host_inputs.py
"""

import ast
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

from lib.git import git
from lib.proc_testing import run
from lib.repo_paths import REPO, SCRIPTS
from renderer.renderer_lib import host_inputs
from renderer.renderer_lib.host_inputs import HOST_INPUTS
from renderer.renderer_lib.host_sources import SHARED_FILES

CHILD = Path(__file__).resolve().with_name("_host_inputs_child.py")
PI_SPEC = "hosts/daniel-pi/package.yml"


def under_a_member(path: str, members: Iterable[str]) -> bool:
    """Whether the repo-relative `path` is a file member or sits under a directory member."""
    return any(
        path.startswith(member) if member.endswith("/") else path == member
        for member in members
    )


def outside_inputs(
    opened: Iterable[str], tracked: frozenset[str], members: Iterable[str]
) -> list[str]:
    """Each tracked repo-relative path among the real paths `opened` that is under no member."""
    repo = os.path.realpath(REPO)
    found = set()
    for path in opened:
        if not path.startswith(repo + os.sep):
            continue
        relative = path[len(repo) + 1 :]
        if relative in tracked and not under_a_member(relative, members):
            found.add(relative)
    return sorted(found)


@pytest.fixture(scope="module")
def render(tmp_path_factory) -> dict:
    """The child's record of one daniel-pi render: its exit code and every real path opened."""
    tmp = tmp_path_factory.mktemp("host-inputs")
    record = tmp / "record.json"
    spec = {"host": "daniel-pi", "out": str(tmp / "out"), "record": str(record)}
    result = run(
        # `-B` and an empty bytecode prefix make every import open its `.py`. With a warm
        # `__pycache__` the loader opens only the `.pyc`, which is untracked, so no Python
        # member's files would reach the audit.
        [sys.executable, "-B", str(CHILD), json.dumps(spec)],
        cwd=REPO,
        env={
            **os.environ,
            "PYTHONPATH": str(SCRIPTS),
            "PYTHONPYCACHEPREFIX": str(tmp / "pycache"),
            "ANSIBLE_LOCAL_TEMP": str(tmp / "ansible"),
        },
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(record.read_text())


@pytest.fixture(scope="module")
def tracked() -> frozenset[str]:
    return frozenset(git("ls-files", cwd=REPO).stdout.splitlines())


def test_every_tracked_file_a_host_render_opens_is_under_a_member_is_clean(
    render, tracked
):
    assert render["rc"] == 0
    outside = outside_inputs(render["opened"], tracked, HOST_INPUTS)
    assert not outside, (
        f"opened by a host render, under no HOST_INPUTS member: {outside}"
    )


def test_the_render_opened_the_pi_package_spec(render):
    opened = {
        os.path.relpath(p, os.path.realpath(REPO))
        for p in render["opened"]
        if p.startswith(os.path.realpath(REPO) + os.sep)
    }
    assert PI_SPEC in opened


def test_a_read_under_no_member_is_flagged(render, tracked):
    without_hosts = [m for m in HOST_INPUTS if m != "hosts/"]

    assert PI_SPEC in outside_inputs(render["opened"], tracked, without_hosts)


def test_an_imported_module_under_no_member_is_flagged(render, tracked):
    without_renderer = [m for m in HOST_INPUTS if m != "scripts/renderer/"]

    assert "scripts/renderer/homelab_render.py" in outside_inputs(
        render["opened"], tracked, without_renderer
    )


def test_host_inputs_names_the_secrets_file_and_the_shared_kuma_lib():
    assert "ansible/vars/secrets.yml" in HOST_INPUTS
    assert "ansible/roles/setup/initial_setup/files/kuma-push-lib.sh" in HOST_INPUTS
    assert set(SHARED_FILES.values()) <= set(HOST_INPUTS)


def test_host_inputs_module_imports_nothing():
    # The landing reads the constant; an import here would load into every landing.
    tree = ast.parse(Path(host_inputs.__file__).read_text())
    assert not [
        n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
