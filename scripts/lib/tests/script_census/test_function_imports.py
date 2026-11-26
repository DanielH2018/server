"""Every entry point's function-local imports execute with the module loaded from the root.

WHY `--help` IS NOT ENOUGH. `test_entry_points_answer_help.py` catches a broken top-level
import, but an import inside a subcommand never runs under `--help`. One shape:
`ha_state_model refresh` raising `ModuleNotFoundError: probe_core` while `--help` passes.
`importlib.util.find_spec` would pass it too, because the function-local
`diagnostics.probe_ha` resolved; what failed was probe_ha's own top-level `import probe_core`.
So the check imports and executes each function-local import, transitively.

THE CONTRACT these tests hold `lib.function_imports` to:

- `check(path, *, scripts_root=SCRIPTS, exemptions=EXEMPTIONS) -> list[Finding]` loads `path`
  by dotted name with only `scripts_root` on the path.
- `Finding` has `file` (posix path relative to `scripts_root.parent`), `function` (the
  enclosing function's name), `module` (`ImportFrom.module` for a `from` import, the dotted
  name for a plain one, so `from ha_state_checks import check_errors` is `ha_state_checks`) and
  `kind`:
  `"broken"` for an import that raised, `"stale-exemption"` for an exemption whose import is
  no longer in that function.
- `EXEMPTIONS`: `{(file, function, module): reason}`, keyed the same way as a `Finding`.
- `ENTRY_POINTS`: the entry-point paths the check covers, stated in the module.

Run: uv run pytest scripts/lib/tests/script_census/test_function_imports.py
"""

import os
import subprocess
import sys
from pathlib import Path

BOOTSTRAP = (
    "import sys as _sys\n"
    "from pathlib import Path as _Path\n\n"
    "_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))\n"
)


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _keys(findings) -> set[tuple[str, str, str, str]]:
    return {(f.file, f.function, f.module, f.kind) for f in findings}


# -- the function-local import resolves, its own top-level import does not ---


def _issue_674_tree(tmp_path: Path, *, fixed: bool) -> Path:
    """The `ha_state_model` layout under names no real module shares, so no caller PYTHONPATH can mask it.

    `fx_diag/fx_ha.py` imports its sibling bare, as `diagnostics/probe_ha.py` imported
    `probe_core`, which the root alone cannot resolve; `fixed=True` imports it dotted.
    """
    scripts = tmp_path / "scripts"
    _write(scripts, "fx_diag/fx_core.py", "VALUE = 1\n")
    sibling = "from fx_diag import fx_core" if fixed else "import fx_core"
    _write(
        scripts,
        "fx_diag/fx_ha.py",
        f"{sibling}\n\n\ndef get():\n    return fx_core.VALUE\n",
    )
    _write(
        scripts,
        "fx_app/fx_model.py",
        '"""Model the fixture state."""\n' + BOOTSTRAP + "\nimport argparse\n\n\n"
        "def cmd_refresh():\n"
        "    from fx_diag import fx_ha\n\n"
        "    return fx_ha.get()\n\n\n"
        "def main(argv=None):\n"
        "    parser = argparse.ArgumentParser(description=__doc__)\n"
        '    parser.add_argument("cmd", choices=["refresh"])\n'
        "    parser.parse_args(argv)\n"
        "    return cmd_refresh()\n\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main())\n",
    )
    return scripts


def test_issue_674_unfixed_is_flagged_while_help_still_passes(tmp_path):
    """The unfixed layout turns the check red while `--help` still passes."""
    from lib.function_imports import check

    scripts = _issue_674_tree(tmp_path, fixed=False)
    entry = scripts / "fx_app" / "fx_model.py"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    helped = subprocess.run(
        [sys.executable, str(entry), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert helped.returncode == 0, helped.stderr

    findings = check(entry, scripts_root=scripts, exemptions={})
    assert _keys(findings) == {
        ("scripts/fx_app/fx_model.py", "cmd_refresh", "fx_diag", "broken")
    }


def test_issue_674_fixed_is_clean(tmp_path):
    from lib.function_imports import check

    scripts = _issue_674_tree(tmp_path, fixed=True)
    entry = scripts / "fx_app" / "fx_model.py"
    assert check(entry, scripts_root=scripts, exemptions={}) == []


def test_a_break_two_hops_below_a_resolvable_import_is_flagged(tmp_path):
    """`find_spec("fx_tools.fx_helper")` succeeds here; only executing the chain reaches the break."""
    from lib.function_imports import check

    scripts = tmp_path / "scripts"
    _write(scripts, "fx_tools/fx_helper.py", "from fx_tools import fx_middle\n")
    _write(scripts, "fx_tools/fx_middle.py", "import fx_module_that_does_not_exist\n")
    entry = _write(
        scripts,
        "fx_tools/fx_tool.py",
        '"""Tool."""\n\n\ndef run():\n    from fx_tools import fx_helper\n\n'
        '    return fx_helper\n\n\nif __name__ == "__main__":\n    run()\n',
    )
    findings = check(entry, scripts_root=scripts, exemptions={})
    assert _keys(findings) == {
        ("scripts/fx_tools/fx_tool.py", "run", "fx_tools", "broken")
    }

    _write(scripts, "fx_tools/fx_middle.py", "import json\n")
    assert check(entry, scripts_root=scripts, exemptions={}) == []


# -- a bare sibling import, which only the module's own directory would resolve ---


def _sibling_tree(tmp_path: Path, *, function: str = "run", pad: int = 0) -> Path:
    """`fx_pkg/fx_mod.py` imports its sibling bare inside `function`.

    `pad` adds comment lines above the function, to move the import without changing it.
    """
    scripts = tmp_path / "scripts"
    _write(scripts, "fx_pkg/fx_sibling.py", "X = 1\n")
    _write(
        scripts,
        "fx_pkg/fx_mod.py",
        '"""Module."""\n'
        + "# padding\n" * pad
        + f"\n\ndef {function}():\n    import fx_sibling\n\n    return fx_sibling.X\n\n\n"
        f'if __name__ == "__main__":\n    {function}()\n',
    )
    return scripts


MOD_KEY = ("scripts/fx_pkg/fx_mod.py", "run", "fx_sibling")


def test_a_bare_sibling_import_is_flagged_though_a_by_path_run_would_resolve_it(
    tmp_path,
):
    from lib.function_imports import check

    scripts = _sibling_tree(tmp_path)
    mod = scripts / "fx_pkg" / "fx_mod.py"
    assert _keys(check(mod, scripts_root=scripts, exemptions={})) == {
        (*MOD_KEY, "broken")
    }


# -- exemptions: keyed by file, function and module, and refused once stale ----------------


def test_an_exemption_survives_lines_inserted_above_the_import(tmp_path):
    from lib.function_imports import check

    exemptions = {MOD_KEY: "reached only from this module's own __main__ dispatch"}
    scripts = _sibling_tree(tmp_path)
    mod = scripts / "fx_pkg" / "fx_mod.py"
    assert check(mod, scripts_root=scripts, exemptions=exemptions) == []

    scripts = _sibling_tree(tmp_path, pad=20)
    assert check(mod, scripts_root=scripts, exemptions=exemptions) == []


def test_an_exemption_whose_import_left_its_function_is_reported_stale(tmp_path):
    from lib.function_imports import check

    exemptions = {MOD_KEY: "reached only from this module's own __main__ dispatch"}
    scripts = _sibling_tree(tmp_path, function="run_moved")
    mod = scripts / "fx_pkg" / "fx_mod.py"
    findings = check(mod, scripts_root=scripts, exemptions=exemptions)
    assert _keys(findings) == {
        (*MOD_KEY, "stale-exemption"),
        ("scripts/fx_pkg/fx_mod.py", "run_moved", "fx_sibling", "broken"),
    }


# -- the real tree -------------------------------------------------------------------------

# Non-vacuity: `ha_state_model.py` is the #674 module and the classifier calls it a library
# so a census borrowed from `--help` would lose it.
MUST_COVER = frozenset(
    {"ha_state_model.py", "validate_ha_config.py", "longhorn_reap.py", "probe.py"}
)


def test_the_entry_point_set_names_the_modules_it_is_measured_against():
    from lib.function_imports import ENTRY_POINTS

    names = {Path(p).name for p in ENTRY_POINTS}
    assert MUST_COVER <= names, sorted(MUST_COVER - names)
    assert len(names) >= 60, len(names)  # the --help census held 72 .py entry points


def test_every_entry_point_function_local_import_executes():
    from lib.function_imports import ENTRY_POINTS, check

    findings = []
    for path in sorted(ENTRY_POINTS):
        findings += check(Path(path))
    assert findings == [], "\n".join(
        sorted(f"{f.kind}: {f.file} {f.function} -> {f.module}" for f in findings)
    )
