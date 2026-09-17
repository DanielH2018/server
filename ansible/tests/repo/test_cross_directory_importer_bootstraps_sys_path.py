"""A `scripts/` module that imports a sibling directory inserts `scripts/` on `sys.path` first.

Repo CLAUDE.md: a directly-invoked script gets its OWN directory on `sys.path` and nothing
else, and `pythonpath` in pyproject.toml is a pytest setting — so `from lib import yaml_fast`
in `scripts/docs/build_docs.py` resolves under pytest and raises `ModuleNotFoundError` under
the cron or prek hook that actually runs it. Every module reaching outside its directory
therefore carries its own `_sys.path.insert(0, ...)` ABOVE the import. 37 modules held that
on 2026-09-17 and nothing checked it; the suite is exactly the thing that cannot see it.

Scope: tracked `scripts/**/*.py` outside any `tests/` directory. A test module reaches its
subject through `pythonpath`, which is what pytest is for.

Run: uv run pytest ansible/tests/repo/test_cross_directory_importer_bootstraps_sys_path.py
"""

import re
import subprocess
from pathlib import Path

from _helpers import REPO

SCRIPTS = REPO / "scripts"
IMPORT = re.compile(r"^(?:from|import) (\w+)(?:[ .]|$)", re.MULTILINE)
BOOTSTRAP = re.compile(r"sys\.path\.insert\(")

# Two modules that have carried the bootstrap since the `scripts/` split, so an emptied census
# fails by name rather than passing on `all([])`.
KNOWN_CROSS_IMPORTERS = frozenset({"docs/build_docs.py", "validate/k8s_manifests.py"})


def _tracked_script_modules() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "scripts/**/*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO / rel for rel in listed.split("\0") if rel and "/tests/" not in rel]


def missing_bootstrap(module: Path, text: str, sibling_dirs: set[str]) -> bool:
    """True when `module` imports a sibling directory before any `sys.path.insert(`."""
    own = module.parent.name
    cross = [
        m.start()
        for m in IMPORT.finditer(text)
        if m.group(1) in sibling_dirs and m.group(1) != own
    ]
    if not cross:
        return False
    boot = BOOTSTRAP.search(text)
    return boot is None or boot.start() > min(cross)


def test_every_cross_directory_importer_bootstraps_first():
    sibling_dirs = {p.name for p in SCRIPTS.iterdir() if p.is_dir()}
    modules = _tracked_script_modules()
    checked = {
        str(m.relative_to(SCRIPTS))
        for m in modules
        if any(
            f.group(1) in sibling_dirs and f.group(1) != m.parent.name
            for f in IMPORT.finditer(m.read_text())
        )
    }
    assert KNOWN_CROSS_IMPORTERS <= checked, sorted(checked)
    bad = sorted(
        str(m.relative_to(REPO))
        for m in modules
        if missing_bootstrap(m, m.read_text(), sibling_dirs)
    )
    assert bad == [], (
        "cross-directory import with no `sys.path.insert` above it (copy the aliased insert "
        f"from a sibling): {bad}"
    )


def test_an_import_above_the_bootstrap_is_flagged():
    """Red-proof: same import, bootstrap before it passes, after it fails, absent fails."""
    module = Path("scripts/docs/x.py")
    dirs = {"lib", "docs"}
    assert not missing_bootstrap(
        module, "import sys\nsys.path.insert(0, 'x')\nfrom lib import y\n", dirs
    )
    assert missing_bootstrap(
        module, "from lib import y\nimport sys\nsys.path.insert(0, 'x')\n", dirs
    )
    assert missing_bootstrap(module, "from lib import y\n", dirs)
    assert not missing_bootstrap(module, "from docs import y\nimport os\n", dirs)
