"""The render's interpreter: the built tree's `host_python_version`, never the unit's (#7023).

The Python 3.15.2 cutover left the builder unable to build for six hours. The installed unit's
`HOMELAB_PYTHON` named 3.14, the render of every later commit needed 3.15, and the unit carrying
3.15 shipped in a package only that render could build. These tests drive the real
`Builder.render` with a `run` stub that records each call.

Run: uv run pytest hosts/lib/tests/test_host_package_builder_python.py
"""

import subprocess
from pathlib import Path

import host_package_builder as hb
import pytest

UNIT_PIN = "3.14.6"


class Recorder:
    """A `subprocess.run` stand-in that records argv and env, and exits with `exits[argv[1]]`."""

    def __init__(self, **exits: int):
        self.calls: list[tuple[list[str], dict | None]] = []
        self.exits = exits

    def __call__(self, argv, env=None, **_):
        self.calls.append((argv, env))
        return subprocess.CompletedProcess(argv, self.exits.get(argv[1], 0))


def tree_pinning(tmp_path: Path, line: str | None) -> Path:
    tree = tmp_path / "tree"
    pin = tree / "ansible/inventory/group_vars/all.yml"
    pin.parent.mkdir(parents=True)
    pin.write_text("---\nother: 1\n" + (f"{line}\n" if line else ""))
    return tree


def render(tmp_path: Path, line: str | None, **exits: int) -> Recorder:
    run = Recorder(**exits)
    builder = hb.Builder(
        state_dir=tmp_path / "state", python=UNIT_PIN, uv="uv", run=run
    )
    builder.render(tree_pinning(tmp_path, line), tmp_path / "out")
    return run


def test_a_tree_pinning_another_python_installs_and_renders_on_the_trees_pin(tmp_path):
    run = render(tmp_path, 'host_python_version: "3.15.2"')
    (install, install_env), (render_argv, _) = run.calls
    assert install == ["uv", "python", "install", "--no-bin", "3.15.2"]
    assert install_env is not None
    assert install_env["UV_PYTHON_DOWNLOADS"] == "manual"
    i = render_argv.index("--python")
    assert render_argv[i + 1] == "3.15.2"
    assert "--no-python-downloads" in render_argv


def test_a_tree_with_no_pin_renders_on_the_units_python(tmp_path):
    run = render(tmp_path, None)
    assert run.calls[0][0][-1] == UNIT_PIN
    render_argv = run.calls[1][0]
    assert render_argv[render_argv.index("--python") + 1] == UNIT_PIN


def test_an_install_that_fails_reads_build_and_never_renders(tmp_path):
    with pytest.raises(hb.Failure) as caught:
        render(tmp_path, "host_python_version: 3.15.2", python=1)
    assert caught.value.reason == "build"
    assert "install 3.15.2 exited 1" in str(caught.value)


def test_a_pin_that_is_not_a_version_reads_build_and_runs_nothing(tmp_path):
    run = Recorder()
    builder = hb.Builder(
        state_dir=tmp_path / "state", python=UNIT_PIN, uv="uv", run=run
    )
    with pytest.raises(hb.Failure) as caught:
        builder.render(
            tree_pinning(tmp_path, 'host_python_version: "--x"'), tmp_path / "out"
        )
    assert caught.value.reason == "build"
    assert run.calls == []
