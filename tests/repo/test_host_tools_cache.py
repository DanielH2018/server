"""The homelab-tools download cache in ci.yml's `host_packages_build` job (#7449).

The key step's shell runs here against a scratch render, so each test reads the key a real run
would compute. A key that ignored the pins would restore a cache without the bumped pin's file,
and the bump would still download; a key that ignored nothing else is what lets an unchanged
run skip every release request.

Run: uv run pytest tests/repo/test_host_tools_cache.py
"""

import json
from pathlib import Path

from _helpers import REPO
from lib import yaml_fast
from lib.proc_testing import run

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _steps() -> list[dict]:
    return yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]["host_packages_build"][
        "steps"
    ]


def _step(name: str) -> dict:
    (step,) = [step for step in _steps() if step.get("name") == name]
    return step


def _key(tmp_path: Path, pins: dict[str, dict]) -> str:
    """The key the key step writes for a render whose head amd64 tree holds `pins`."""
    temp = tmp_path / "runner"
    for side, tree_pins in (("base", {"sops": {"sha256": "a" * 64}}), ("head", pins)):
        tree = temp / "render" / side / "homelab-tools" / "amd64"
        tree.mkdir(parents=True, exist_ok=True)
        (tree / "tools.json").write_text(json.dumps(tree_pins))
    output = tmp_path / "output"
    output.unlink(missing_ok=True)
    result = run(
        [
            "bash",
            "-c",
            _step("Key the homelab-tools cache by the rendered pins")["run"],
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "RUNNER_TEMP": str(temp),
            "GITHUB_OUTPUT": str(output),
        },
    )
    assert result.returncode == 0, result.stderr
    (line,) = output.read_text().splitlines()
    assert line.startswith("key=homelab-tools-")
    return line.removeprefix("key=")


def test_unchanged_pins_give_the_same_key(tmp_path: Path) -> None:
    pins = {"sops": {"sha256": "b" * 64}}
    assert _key(tmp_path / "one", pins) == _key(tmp_path / "two", pins)


def test_a_bumped_pin_changes_the_key(tmp_path: Path) -> None:
    old = _key(tmp_path / "old", {"sops": {"sha256": "b" * 64}})
    assert _key(tmp_path / "new", {"sops": {"sha256": "c" * 64}}) != old


def test_the_cache_holds_the_fetch_steps_directory_under_the_exact_key_alone() -> None:
    names = [step.get("name") for step in _steps()]
    restore = _step("Restore the homelab-tools cache")
    fetch = "Fetch the pinned homelab-tools binaries and check their sha256"
    assert names.index("Restore the homelab-tools cache") < names.index(fetch)
    assert restore["uses"].startswith("actions/cache@")
    assert restore["with"]["path"] == "${{ runner.temp }}/tools-cache"
    assert restore["with"]["key"] == "${{ steps.tools-key.outputs.key }}"
    assert "restore-keys" not in restore["with"]
    assert '--cache "$RUNNER_TEMP/tools-cache"' in _step(fetch)["run"]
