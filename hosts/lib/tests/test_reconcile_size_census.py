"""Reconcile stays under its line ceiling (H7, *Reconcile's line ceiling*, exit observation 7).

The host plane grows a second mechanism beside dpkg, and design.md's *Open questions* 7 makes
this ceiling the trigger for reconsidering it. The census counts the source lines of
`hosts/lib/homelab-reconcile` and of the `hosts/lib/` modules only it imports
(`_reconcile_size.py`).

Run: uv run pytest hosts/lib/tests/test_reconcile_size_census.py
"""

import math
from pathlib import Path

import pytest
from _reconcile_size import census, source_lines

from lib.repo_paths import REPO

LIB = REPO / "hosts" / "lib"

# The ceiling is (B + 720) * 1.3, rounded up to the next 50.
# B is the count of source lines in `hosts/lib/homelab-reconcile` at H3.2b's merge commit,
# c954fe7c7, measured with `source_lines`: 326 of that file's 467 lines.
# 720 is the growth the slices state: about 100 source lines in H4.1b and 50 in H4.3b1's
# udev arm, then 120 in H7.2a, 150 in H7.2b, 100 in H7.2c and 200 in H7.2d.
# The 30% is headroom for fixes inside that scope, not room for another arm.
#
# DECIDED: raising CEILING is an operator ruling, never part of an ordinary change. Crossing it
# is design.md's trigger to cut reconcile, to raise the ceiling in the H7 ADR with a reason, or
# to switch the host plan to ansible-pull. The ceiling and its derivation are in
# docs/target-architecture/host/h7-delete-ansible.md, *Reconcile's line ceiling*.
BASE_LINES = 326
STATED_GROWTH = 720
CEILING = 1400


def derived_ceiling() -> int:
    return math.ceil((BASE_LINES + STATED_GROWTH) * 1.3 / 50) * 50


def over_ceiling(lib: Path, ceiling: int) -> str | None:
    """The failure message when the counted files exceed `ceiling`, else None."""
    counted = census(lib)
    total = sum(counted.values())
    if total <= ceiling:
        return None
    parts = ", ".join(f"{name}={lines}" for name, lines in counted.items())
    return (
        f"reconcile has {total} source lines, over its ceiling of {ceiling} ({parts})"
    )


def test_the_ceiling_is_the_derived_number():
    assert CEILING == derived_ceiling() == 1400


def test_reconcile_is_under_its_ceiling():
    assert over_ceiling(LIB, CEILING) is None


def test_the_census_counts_the_script_itself():
    counted = census(LIB)
    assert counted["homelab-reconcile"] > 400
    assert sum(counted.values()) <= CEILING


def test_the_counter_ignores_blanks_comments_and_docstrings():
    text = '"""Module doc\nover two lines."""\n\n# comment\nX = 1\n\n\ndef f():\n    """Doc."""\n    return (\n        2\n    )\n'
    assert source_lines(text) == 5


# ── red proofs ──────────────────────────────────────────────────────────────────────────


def _lib(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def test_a_ceiling_one_line_below_the_count_fails_and_prints_both_numbers(tmp_path):
    lib = _lib(
        tmp_path,
        {"homelab-reconcile": "#!/usr/bin/python3\n" + "x = 1\n" * 10},
    )
    assert over_ceiling(lib, 10) is None
    message = over_ceiling(lib, 9)
    assert message is not None
    assert "10 source lines" in message
    assert "ceiling of 9" in message


def test_a_helper_only_reconcile_imports_carries_the_count_over_the_ceiling(tmp_path):
    lib = _lib(
        tmp_path,
        {
            "homelab-reconcile": "#!/usr/bin/python3\nimport helper\nx = 1\n",
            "helper.py": "y = 1\n" * 10,
        },
    )
    assert census(lib) == {"homelab-reconcile": 2, "helper.py": 10}
    assert over_ceiling(lib, 11) is not None


def test_a_helper_inside_a_package_is_counted_with_its_package(tmp_path):
    lib = _lib(
        tmp_path,
        {
            "homelab-reconcile": "#!/usr/bin/python3\nfrom pkg import arm\n",
            "pkg/__init__.py": '"""Doc."""\n',
            "pkg/arm.py": "y = 1\n" * 4,
        },
    )
    assert census(lib) == {
        "homelab-reconcile": 1,
        "pkg/__init__.py": 0,
        "pkg/arm.py": 4,
    }


def test_a_module_another_program_imports_belongs_to_neither(tmp_path):
    # `shared.py` is imported by `homelab-secrets` too, so growing it is not reconcile's growth.
    lib = _lib(
        tmp_path,
        {
            "homelab-reconcile": "#!/usr/bin/python3\nimport own\nimport shared\n",
            "homelab-secrets": "#!/usr/bin/python3\nimport shared\n",
            "own.py": "a = 1\n",
            "shared.py": "import deeper\nb = 1\n",
            "deeper.py": "c = 1\n" * 50,
        },
    )
    assert census(lib) == {"homelab-reconcile": 2, "own.py": 1}


def test_a_function_level_import_is_followed(tmp_path):
    lib = _lib(
        tmp_path,
        {
            "homelab-reconcile": "#!/usr/bin/python3\ndef f():\n    import late\n",
            "late.py": "z = 1\n" * 3,
        },
    )
    assert census(lib) == {"homelab-reconcile": 2, "late.py": 3}


@pytest.mark.parametrize("name", ["not_a_module", "os.path"])
def test_an_import_that_names_no_file_here_counts_nothing(tmp_path, name):
    lib = _lib(tmp_path, {"homelab-reconcile": f"#!/usr/bin/python3\nimport {name}\n"})
    assert census(lib) == {"homelab-reconcile": 1}
