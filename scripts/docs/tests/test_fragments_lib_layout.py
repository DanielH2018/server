"""The fragment libraries live in `scripts/docs/fragments_lib/`, behind `gen_doc_fragments.py` (#4351).

The move is navigation only. Its "no behaviour change" oracle is
`test_gen_doc_fragments.py::test_every_committed_fragment_matches_what_the_generator_writes_now`,
which regenerates every fragment and compares it with the committed copy, so this file does
not run the generator again (#4371). It checks the layout: the libraries sit in the package,
the entrypoint takes their fragments from it, and both resolve their imports when run directly
from outside the repo. The tests find the package's modules by globbing it, so they hold
whatever basenames the modules take inside it.

Run: uv run pytest scripts/docs/tests/test_fragments_lib_layout.py
"""

import importlib
import os
import re
import subprocess
import sys

import gen_doc_fragments as g
from lib.repo_paths import REPO

DOCS = REPO / "scripts" / "docs"
LIB = DOCS / "fragments_lib"
ENTRYPOINT = DOCS / "gen_doc_fragments.py"
DOMAINS = ("bridge", "deploy", "hosts", "storage")
FLAT = tuple(f"fragments_{d}" for d in DOMAINS)


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True, timeout=60
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def _lib_modules() -> list[str]:
    """Stems of the tracked modules directly under fragments_lib/."""
    prefix = "scripts/docs/fragments_lib/"
    return sorted(
        p[len(prefix) : -len(".py")]
        for p in _tracked()
        if p.startswith(prefix) and p.endswith(".py") and "/" not in p[len(prefix) :]
    )


def _clean_env() -> dict[str, str]:
    """The environment a cron or a shell gives a directly-invoked script: no PYTHONPATH."""
    return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def test_the_four_libraries_live_in_a_namespace_package():
    tracked = set(_tracked())
    still_flat = sorted(
        f"scripts/docs/{m}.py" for m in FLAT if f"scripts/docs/{m}.py" in tracked
    )
    assert still_flat == [], f"still beside the entrypoint: {still_flat}"
    assert "scripts/docs/fragments_lib/__init__.py" not in tracked
    assert len(_lib_modules()) == len(DOMAINS), _lib_modules()


def test_each_library_resolves_its_imports_when_run_directly(tmp_path):
    modules = _lib_modules()
    assert len(modules) == len(DOMAINS), modules
    for stem in modules:
        run = subprocess.run(
            [sys.executable, str(LIB / f"{stem}.py")],
            cwd=tmp_path,
            env=_clean_env(),
            capture_output=True,
            timeout=120,
            text=True,
        )
        assert run.returncode == 0, f"{stem}: {run.stderr[-2000:]}"


def test_the_entrypoint_takes_each_library_fragment_from_the_package():
    modules = _lib_modules()
    assert len(modules) == len(DOMAINS), modules
    for stem in modules:
        built = importlib.import_module(f"fragments_lib.{stem}").FRAGMENTS
        assert built, f"fragments_lib.{stem} builds no fragment"
        for name, build in built.items():
            assert g.FRAGMENTS.get(name) is build, (
                f"{name}: the entrypoint does not build it with fragments_lib.{stem}"
            )


def test_the_entrypoint_answers_help_from_outside_the_repo(tmp_path):
    helped = subprocess.run(
        [sys.executable, str(ENTRYPOINT), "--help"],
        cwd=tmp_path,
        env=_clean_env(),
        capture_output=True,
        timeout=120,
        text=True,
    )
    assert helped.returncode == 0, helped.stderr[-2000:]


def test_no_tracked_module_imports_the_flat_names():
    flat_import = re.compile(
        r"^\s*(?:import|from)\s+(?:" + "|".join(FLAT) + r")\b", re.MULTILINE
    )
    offenders = sorted(
        p
        for p in _tracked()
        if p.endswith(".py")
        and flat_import.search((REPO / p).read_text(errors="replace"))
    )
    assert offenders == []


def test_no_tracked_file_names_the_old_paths():
    old_path = re.compile(r"scripts/docs/fragments_(?:" + "|".join(DOMAINS) + r")\.py")
    offenders = []
    for p in _tracked():
        path = REPO / p
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if old_path.search(text):
            offenders.append(p)
    assert offenders == []
