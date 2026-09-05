"""land.sh is an exec shim over land.py, and stays the name everything invokes.

These run the shim as a process: the suite cannot see a broken sys.path bootstrap, and a
directly-invoked script gets only its own directory on sys.path.

Run: uv run pytest scripts/deploy_tools/tests/test_land_shim.py
"""

import os
import subprocess
from pathlib import Path

_SH = Path(__file__).resolve().parent.parent / "land.sh"


def _env_without_the_active_venv() -> dict[str, str]:
    """`os.environ` with the markers that would let a nested `uv run` skip project resolution.

    The suite itself runs under `uv run`, which exports `VIRTUAL_ENV`; a nested `uv run`
    then reuses that environment instead of resolving a project from its working directory.
    A session invoking land.sh from a scratch directory has no such variable, so a test that
    inherits one cannot see the failure this file exists to catch.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k != "VIRTUAL_ENV" and not k.startswith("UV_")
    }


def test_help_prints_the_contract_through_the_shim():
    r = subprocess.run(
        ["bash", str(_SH), "--help"], capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 0, r.stderr
    assert "Verdicts printed on stdout" in r.stdout


def test_the_shim_holds_no_logic():
    code = [
        ln
        for ln in _SH.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    assert code == [
        'cd "$(dirname "$(readlink -f "$0")")/../.." || exit 1',
        'exec uv run python scripts/deploy_tools/land.py "$@"',
    ]


def test_the_shim_runs_from_any_cwd(tmp_path):
    """`uv run` resolves the project from its caller's working directory.

    Without the shim's own cd, a landing started from anywhere outside a checkout dies at
    import with `ModuleNotFoundError: yaml` -- before land.py can print a verdict or
    annotate anything.
    """
    r = subprocess.run(
        ["bash", str(_SH), "--help"],
        cwd=tmp_path,
        env=_env_without_the_active_venv(),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert r.returncode == 0, r.stderr
    assert "Verdicts printed on stdout" in r.stdout


def test_a_bad_argument_exits_2_through_the_shim():
    r = subprocess.run(
        ["bash", str(_SH), "--pr"], capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 2
