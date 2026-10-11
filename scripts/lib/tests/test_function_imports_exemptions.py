"""The parts of `lib.function_imports` beyond the #4403 red contract.

An exemption whose import no longer fails is reported, an off-tree exemption suppresses in the
path mode too, and a module whose own top level raises is reported rather than passing.

Run: uv run pytest scripts/lib/tests/test_function_imports_exemptions.py
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


KEY = ("scripts/fx_pkg/fx_mod.py", "run", "fx_gone")


def test_an_exemption_whose_import_now_succeeds_is_reported_unused(tmp_path):
    scripts, mod = _tree(tmp_path, "import json")
    (scripts / "fx_user.py").write_text("from fx_pkg import fx_mod\n")
    key = ("scripts/fx_pkg/fx_mod.py", "run", "json")
    findings = check(
        mod, mode="dotted", scripts_root=scripts, exemptions={key: "r"}, off_tree={}
    )
    assert [(f.module, f.kind) for f in findings] == [("json", "unused-exemption")]


def test_an_off_tree_exemption_suppresses_in_the_path_mode_and_a_dotted_one_does_not(
    tmp_path,
):
    scripts, mod = _tree(tmp_path, "import fx_gone")
    assert (
        check(
            mod, mode="path", scripts_root=scripts, exemptions={}, off_tree={KEY: "r"}
        )
        == []
    )
    findings = check(
        mod, mode="path", scripts_root=scripts, exemptions={KEY: "r"}, off_tree={}
    )
    assert [(f.module, f.kind) for f in findings] == [("fx_gone", "broken")]


def test_a_module_whose_top_level_raises_is_reported_unloadable(tmp_path):
    scripts, mod = _tree(tmp_path, "import json", top="raise RuntimeError('boom')")
    findings = check(mod, mode="path", scripts_root=scripts, exemptions={}, off_tree={})
    assert [f.kind for f in findings] == ["unloadable"]
    assert "boom" in findings[0].detail
