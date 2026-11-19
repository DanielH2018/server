"""Tests for scripts/validate/render_templates.py, the entry point over the render families.

Each family's own rules, and the known-bad fixture each must flag, are in
`test_render_<family>.py` beside this file. This module runs every family over the real tree
and holds the entry point's summary and exit code.
"""

import shutil
from collections.abc import Callable

import pytest
from validate import render_templates as rt


def _clean(ok: Callable[[str], None]) -> tuple[int, int]:
    ok("  [ok]   x")
    return 3, 0


def _broken(ok: Callable[[str], None]) -> tuple[int, int]:
    return 2, 1


FAMILIES = ("compose", "setup", "shell", "units")


def test_the_entry_point_runs_every_family():
    assert set(rt.FAMILIES) == set(FAMILIES)


@pytest.mark.parametrize("name", FAMILIES)
def test_each_family_checks_the_real_tree_clean(name):
    if name == "units" and shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not on PATH")
    checked, failures = rt.FAMILIES[name](print)
    assert checked > 0
    assert failures == 0


def test_a_family_failure_fails_the_run_and_is_named_in_the_summary(capsys):
    assert rt.main([], {"clean": _clean, "broken": _broken}) == 1
    assert (
        capsys.readouterr()
        .out.strip()
        .endswith("5 template(s) checked (clean 3, broken 2 (1 failed)), 1 failure(s).")
    )


def test_clean_families_pass_and_print_no_ok_line_without_verbose(capsys):
    assert rt.main([], {"clean": _clean}) == 0
    assert "[ok]" not in capsys.readouterr().out


def test_verbose_passes_the_ok_lines_through(capsys):
    assert rt.main(["-v"], {"clean": _clean}) == 0
    assert "  [ok]   x\n" in capsys.readouterr().out
