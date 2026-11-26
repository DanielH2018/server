"""The parts of `lib.function_imports` beyond the #4403 red contract.

An exemption whose import no longer fails is reported, and a module whose own top level raises
is reported rather than passing.

Run: uv run pytest scripts/lib/tests/script_census/test_function_imports_exemptions.py
"""

from pathlib import Path

from lib.function_imports import check


def _tree(tmp_path: Path, body: str, top: str = "") -> tuple[Path, Path]:
    scripts = tmp_path / "scripts"
    (scripts / "fx_pkg").mkdir(parents=True)
    mod = scripts / "fx_pkg" / "fx_mod.py"
    mod.write_text(
        top + f"\n\ndef run():\n    {body}\n\n\nif __name__ == '__main__':\n    run()\n"
    )
    return scripts, mod


def test_an_exemption_whose_import_now_succeeds_is_reported_unused(tmp_path):
    scripts, mod = _tree(tmp_path, "import json")
    key = ("scripts/fx_pkg/fx_mod.py", "run", "json")
    findings = check(mod, scripts_root=scripts, exemptions={key: "r"})
    assert [(f.module, f.kind) for f in findings] == [("json", "unused-exemption")]


def test_a_module_whose_top_level_raises_is_reported_unloadable(tmp_path):
    scripts, mod = _tree(tmp_path, "import json", top="raise RuntimeError('boom')")
    findings = check(mod, scripts_root=scripts, exemptions={})
    assert [f.kind for f in findings] == ["unloadable"]
    assert "boom" in findings[0].detail
