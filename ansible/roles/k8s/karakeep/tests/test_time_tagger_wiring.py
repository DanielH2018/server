"""The two seams this role owns around the vendored time-tagger script.

`files/karakeep-time-tagger.py` is upstream's bytes, pinned by sha256 in
`ansible/tests/services/test_karakeep_time_tagger_script.py`; its own logic is upstream's to
test, and its imports (`fire`, `loguru`, `karakeep_python_api`, ...) are not in this repo's
dev group, so nothing here imports it. What this role does own is how the deployment DRIVES
it, and both halves fail as a crashloop the checksum pin cannot see:

- the flags `deployment-time-tagger.yaml.j2` passes on the command line must be parameters of
  the script's `main()` — `fire.Fire(main)` rejects an unknown flag at startup, so a rename
  upstream that the pin is later moved to follow takes the tagger down on the next deploy;
- every top-level module the script imports must come from a package the container's
  `uv pip install ...` line installs, or from the image's standard library.

Both are read statically: the template as text, the script through `ast`.

Run: uv run pytest ansible/roles/k8s/karakeep/tests
"""

import ast
import re
import sys
from pathlib import Path

ROLE = Path(__file__).resolve().parents[1]
SCRIPT = ROLE / "files" / "karakeep-time-tagger.py"
DEPLOYMENT = ROLE / "templates" / "deployment-time-tagger.yaml.j2"

# The import name a distribution provides, where the two differ.
_MODULE_OF = {"beautifulsoup4": "bs4", "karakeep-python-api": "karakeep_python_api"}


def _main_params(source: str) -> set[str]:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return {a.arg for a in node.args.args + node.args.kwonlyargs}
    raise AssertionError("the vendored script defines no top-level main()")


def _script_invocation(template: str) -> str:
    match = re.search(r"python /script/karakeep-time-tagger\.py([^\n]*)", template)
    assert match, (
        "deployment-time-tagger.yaml.j2 no longer runs the script by that path"
    )
    return match.group(1)


def flags_passed(invocation: str) -> set[str]:
    return set(re.findall(r"--([A-Za-z_]+)", invocation))


def _installed_modules(template: str) -> set[str]:
    match = re.search(r"uv pip install ((?:\S+==\S+\s*)+)", template)
    assert match, "deployment-time-tagger.yaml.j2 no longer pins the script's packages"
    dists = [spec.split("==")[0] for spec in match.group(1).split()]
    return {_MODULE_OF.get(d, d.replace("-", "_")) for d in dists}


def top_level_imports(source: str) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_every_flag_the_deployment_passes_is_a_main_parameter():
    flags = flags_passed(_script_invocation(DEPLOYMENT.read_text()))
    assert flags, "the deployment passes no flags; the seam this test checks is gone"
    unknown = flags - _main_params(SCRIPT.read_text())
    assert not unknown, (
        f"deployment-time-tagger.yaml.j2 passes {sorted(unknown)} but the vendored main() "
        "takes no such parameter — fire.Fire rejects it and the container crashloops"
    )


def test_an_unknown_flag_is_flagged():
    assert flags_passed(" --cache_file /tmp/x --reset_al=True") - _main_params(
        SCRIPT.read_text()
    ) == {"reset_al"}


def test_every_import_the_script_makes_is_installed():
    needed = top_level_imports(SCRIPT.read_text()) - sys.stdlib_module_names
    assert "karakeep_python_api" in needed, (
        "the import census no longer sees the API client"
    )
    missing = needed - _installed_modules(DEPLOYMENT.read_text())
    assert not missing, (
        f"the script imports {sorted(missing)} but the container's `uv pip install` line "
        "installs no package providing it"
    )


def test_an_import_no_pin_provides_is_flagged():
    assert top_level_imports("import fire\nfrom rich import print\n") - {"fire"} == {
        "rich"
    }
