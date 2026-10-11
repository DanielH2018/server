"""The catalog and facts libraries live in `scripts/docs/catalog_lib/`, behind unmoved generators.

#4352 moves the six libraries out of `scripts/docs/` into a PEP 420 namespace package. The
generators that import them keep their paths, because the `docs-refresh` cron and the
`generated_from:` banners name those paths. Each generator still runs the way the cron and
the prek hooks run it, by file path from any working directory.

Run: uv run pytest scripts/docs/tests/test_catalog_lib_layout.py
"""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from lib.repo_paths import REPO

DOCS = "scripts/docs"
LIB = f"{DOCS}/catalog_lib"
LIBRARIES = (
    "catalog_backup",
    "catalog_facts",
    "catalog_model",
    "catalog_render",
    "glance_facts",
    "route_facts",
)
# Each generator, and the libraries it must load from catalog_lib (named members, not a glob).
GENERATORS = {
    f"{DOCS}/service_catalog.py": (
        "catalog_backup",
        "catalog_facts",
        "catalog_model",
        "catalog_render",
        "route_facts",
    ),
    f"{DOCS}/gen_role_glance.py": (
        "catalog_backup",
        "catalog_facts",
        "catalog_model",
        "glance_facts",
    ),
    f"{DOCS}/reference/networking.py": ("route_facts",),
}

# Runs a script the way `python <path>` does (its own directory at sys.path[0]), then reports
# the file every loaded module came from, so the test judges by origin and not by import name.
_RUN_AS_SCRIPT = """
import json, os, runpy, sys
path = sys.argv[1]
sys.argv = [path, *sys.argv[2:]]
sys.path[0] = os.path.dirname(path)
code = 0
try:
    runpy.run_path(path, run_name="__main__")
except SystemExit as exc:
    code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
files = sorted({m.__file__ for m in list(sys.modules.values()) if getattr(m, "__file__", None)})
print("\\n__LOADED__" + json.dumps({"code": code, "files": files}))
"""


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True, timeout=60
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def _run_generator(
    script: str, args: list[str], cwd: Path
) -> tuple[int, list[str], str]:
    proc = subprocess.run(
        [sys.executable, "-c", _RUN_AS_SCRIPT, str(REPO / script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,  # gen_role_glance --check renders every role, which takes seconds
    )
    marker = proc.stdout.rfind("__LOADED__")
    assert marker != -1, f"{script} never reached the report:\n{proc.stderr[-3000:]}"
    report = json.loads(proc.stdout[marker + len("__LOADED__") :])
    loaded = [
        Path(f).resolve().relative_to(REPO).as_posix()
        for f in report["files"]
        if Path(f).resolve().is_relative_to(REPO)
    ]
    return report["code"], loaded, proc.stdout[:marker] + proc.stderr


def _assert_libraries_come_from_catalog_lib(script: str, loaded: list[str]) -> None:
    for name in GENERATORS[script]:
        assert f"{LIB}/{name}.py" in loaded, (
            f"{script} did not load {name} from {LIB}/; it loaded "
            f"{[p for p in loaded if p.startswith(DOCS)]}"
        )
        assert f"{DOCS}/{name}.py" not in loaded, (
            f"{script} still loads the flat {DOCS}/{name}.py"
        )


def test_the_six_libraries_are_tracked_in_catalog_lib_and_not_flat():
    tracked = set(_tracked())
    assert {f"{LIB}/{n}.py" for n in LIBRARIES} <= tracked, sorted(
        f"{LIB}/{n}.py" for n in LIBRARIES if f"{LIB}/{n}.py" not in tracked
    )
    assert not {f"{DOCS}/{n}.py" for n in LIBRARIES} & tracked


def test_catalog_lib_is_a_namespace_package_with_no_init_file():
    in_package = [p for p in _tracked() if p.startswith(f"{LIB}/")]
    assert in_package, f"nothing is tracked under {LIB}/"
    assert f"{LIB}/__init__.py" not in in_package


def test_catalog_lib_is_the_only_entry_by_that_name_on_any_import_root():
    """Every `pythonpath` directory is one import root, so the package name must be unique."""
    roots = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"][
        "ini_options"
    ]["pythonpath"]
    tracked = _tracked()
    owners = sorted(
        root
        for root in roots
        if any(
            p == f"{root}/catalog_lib.py" or p.startswith(f"{root}/catalog_lib/")
            for p in tracked
        )
    )
    assert owners == [DOCS]


@pytest.mark.parametrize("where", ["repo-root", "elsewhere"])
@pytest.mark.parametrize("script", sorted(GENERATORS))
def test_each_generator_answers_help_from_its_old_path_on_catalog_lib(
    script, where, tmp_path
):
    cwd = REPO if where == "repo-root" else tmp_path
    code, loaded, output = _run_generator(script, ["--help"], cwd)
    assert code == 0, output[-3000:]
    _assert_libraries_come_from_catalog_lib(script, loaded)


def test_the_glance_generator_on_catalog_lib_finds_no_stale_block(tmp_path):
    """No role CLAUDE.md glance block changes: `--check` writes nothing and exits 0.

    The regen-role-glance prek hook keeps the committed blocks fresh, so they are a stable
    oracle for "the move changed no output".
    """
    script = f"{DOCS}/gen_role_glance.py"
    code, loaded, output = _run_generator(script, ["--check"], tmp_path)
    assert code == 0, output[-3000:]
    _assert_libraries_come_from_catalog_lib(script, loaded)


def test_no_tracked_file_names_a_library_at_its_flat_path():
    """A path a doc, hook, test or role names is an interface: every caller moves with it."""
    flat = re.compile(
        r"(?<![\w/])(?:scripts/)?docs/(" + "|".join(LIBRARIES) + r")\.py\b"
    )
    me = Path(__file__).resolve().relative_to(REPO).as_posix()
    hits = []
    for path in _tracked():
        if path == me:
            continue
        try:
            text = (REPO / path).read_text()
        except UnicodeDecodeError, IsADirectoryError, FileNotFoundError:
            continue
        hits += [f"{path}: {m.group(0)}" for m in flat.finditer(text)]
    assert hits == []


def test_the_regenerated_scripts_page_lists_the_libraries_under_catalog_lib():
    """docs/reference/scripts.md is generated: the move regenerates it rather than editing it."""
    page = (REPO / "docs/reference/scripts.md").read_text()
    listed = set(re.findall(r"`(scripts/docs/[\w/]+\.py)`", page))
    assert {f"{LIB}/{n}.py" for n in LIBRARIES} <= listed
    assert not {f"{DOCS}/{n}.py" for n in LIBRARIES} & listed
