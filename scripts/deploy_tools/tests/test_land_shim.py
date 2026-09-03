"""land.sh is an exec shim over land.py, and stays the name everything invokes.

These run the shim as a process: the suite cannot see a broken sys.path bootstrap, and a
directly-invoked script gets only its own directory on sys.path.

Run: uv run pytest scripts/deploy_tools/tests/test_land_shim.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_SH = Path(__file__).resolve().parent.parent / "land.sh"


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
        'exec uv run python "$(dirname "$(readlink -f "$0")")/land.py" "$@"'
    ]


def test_a_bad_argument_exits_2_through_the_shim():
    r = subprocess.run(
        ["bash", str(_SH), "--pr"], capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 2
